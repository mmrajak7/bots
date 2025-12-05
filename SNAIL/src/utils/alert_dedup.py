"""
SNAIL Alert Deduplication

Prevents duplicate alerts from flooding Telegram within cooldown periods.

@file        alert_dedup.py
@description Alert deduplication and rate limiting
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 6.2
"""

import hashlib
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from pathlib import Path
import threading
from loguru import logger

from src.utils.config import PROJECT_ROOT
from src.utils.db import get_db_session


# =============================================================================
# CONSTANTS
# =============================================================================

# Default cooldowns (in seconds)
DEFAULT_COOLDOWNS = {
    'pnl_update': 300,         # 5 minutes
    'vix_warning': 600,        # 10 minutes
    'wing_approach': 300,      # 5 minutes
    'stop_loss': 60,           # 1 minute (urgent)
    'error': 300,              # 5 minutes
    'status': 60,              # 1 minute
    'entry': 0,                # No cooldown (important)
    'exit': 0,                 # No cooldown (important)
    'daily_summary': 0,        # No cooldown (once per day)
    'morning_summary': 0,      # No cooldown (once per day)
    'friday_decision': 0,      # No cooldown (important)
    'gap_alert': 0,            # No cooldown (important)
    'default': 60,             # 1 minute fallback
}

# Maximum alerts per hour (rate limiting)
MAX_ALERTS_PER_HOUR = 60
RATE_LIMIT_WINDOW = 3600  # seconds


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class AlertRecord:
    """
    Record of a sent alert.

    Attributes:
        alert_type: Type of alert
        content_hash: Hash of alert content
        timestamp: When alert was sent
        metadata: Additional metadata
    """
    alert_type: str
    content_hash: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DedupResult:
    """
    Result of deduplication check.

    Attributes:
        should_send: Whether alert should be sent
        reason: Reason if blocked
        cooldown_remaining: Remaining cooldown seconds
    """
    should_send: bool
    reason: str = ""
    cooldown_remaining: int = 0


# =============================================================================
# ALERT DEDUPLICATOR CLASS
# =============================================================================

class AlertDeduplicator:
    """
    Manages alert deduplication and rate limiting.

    Features:
    - Content-based deduplication (same message not sent twice)
    - Time-based cooldowns (minimum interval between same alert type)
    - Rate limiting (max alerts per hour)
    - Persistent storage (survives restarts)

    Attributes:
        cooldowns: Custom cooldown periods by alert type
        alert_history: Recent alert records
        db_path: Path to database
    """

    def __init__(
        self,
        cooldowns: Optional[Dict[str, int]] = None,
        db_path: Optional[Path] = None
    ):
        """
        Initialize deduplicator.

        Args:
            cooldowns: Custom cooldown periods
            db_path: Path to database
        """
        self.cooldowns = {**DEFAULT_COOLDOWNS, **(cooldowns or {})}
        self.db_path = db_path or PROJECT_ROOT / "data" / "snail.db"

        # In-memory cache for fast lookups
        self._cache: Dict[str, AlertRecord] = {}
        self._lock = threading.Lock()

        # Load recent alerts from database
        self._load_recent_alerts()

        logger.debug("Alert deduplicator initialized")

    def _load_recent_alerts(self) -> None:
        """Load recent alerts from database into cache."""
        try:
            with get_db_session(self.db_path) as conn:
                cursor = conn.cursor()

                # Load alerts from last hour
                cutoff = datetime.now() - timedelta(hours=1)
                cursor.execute('''
                    SELECT alert_type, content_hash, created_at
                    FROM alert_cooldowns
                    WHERE created_at > ?
                ''', (cutoff.isoformat(),))

                for row in cursor.fetchall():
                    key = f"{row[0]}:{row[1]}"
                    self._cache[key] = AlertRecord(
                        alert_type=row[0],
                        content_hash=row[1],
                        timestamp=datetime.fromisoformat(row[2])
                    )

                logger.debug(f"Loaded {len(self._cache)} recent alerts from database")

        except Exception as e:
            logger.warning(f"Could not load alert history: {e}")

    def _save_alert(self, record: AlertRecord) -> None:
        """Save alert record to database."""
        try:
            with get_db_session(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO alert_cooldowns
                    (alert_type, content_hash, created_at)
                    VALUES (?, ?, ?)
                ''', (
                    record.alert_type,
                    record.content_hash,
                    record.timestamp.isoformat()
                ))

        except Exception as e:
            logger.warning(f"Could not save alert record: {e}")

    def _cleanup_old_alerts(self) -> None:
        """Remove expired alerts from cache and database."""
        try:
            with self._lock:
                cutoff = datetime.now() - timedelta(hours=2)

                # Clean cache
                expired = [
                    key for key, record in self._cache.items()
                    if record.timestamp < cutoff
                ]
                for key in expired:
                    del self._cache[key]

            # Clean database
            with get_db_session(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    DELETE FROM alert_cooldowns
                    WHERE created_at < ?
                ''', (cutoff.isoformat(),))

                if cursor.rowcount > 0:
                    logger.debug(f"Cleaned up {cursor.rowcount} old alert records")

        except Exception as e:
            logger.warning(f"Alert cleanup error: {e}")

    @staticmethod
    def _hash_content(content: str) -> str:
        """Generate hash of alert content."""
        return hashlib.md5(content.encode()).hexdigest()[:16]

    def _get_cooldown(self, alert_type: str) -> int:
        """Get cooldown period for alert type."""
        return self.cooldowns.get(alert_type, self.cooldowns['default'])

    def _check_rate_limit(self) -> bool:
        """Check if we're within rate limit."""
        with self._lock:
            cutoff = datetime.now() - timedelta(seconds=RATE_LIMIT_WINDOW)
            recent_count = sum(
                1 for record in self._cache.values()
                if record.timestamp > cutoff
            )
            return recent_count < MAX_ALERTS_PER_HOUR

    def check(
        self,
        alert_type: str,
        content: str,
        force: bool = False
    ) -> DedupResult:
        """
        Check if alert should be sent.

        Args:
            alert_type: Type of alert
            content: Alert content
            force: Force send (bypass dedup)

        Returns:
            DedupResult with should_send and reason
        """
        if force:
            return DedupResult(should_send=True, reason="forced")

        # Check rate limit first
        if not self._check_rate_limit():
            return DedupResult(
                should_send=False,
                reason=f"Rate limit exceeded ({MAX_ALERTS_PER_HOUR}/hour)"
            )

        content_hash = self._hash_content(content)
        cache_key = f"{alert_type}:{content_hash}"
        cooldown_period = self._get_cooldown(alert_type)

        with self._lock:
            # Check if identical alert exists
            if cache_key in self._cache:
                record = self._cache[cache_key]
                elapsed = (datetime.now() - record.timestamp).total_seconds()

                if elapsed < cooldown_period:
                    remaining = int(cooldown_period - elapsed)
                    return DedupResult(
                        should_send=False,
                        reason=f"Duplicate alert (cooldown: {remaining}s)",
                        cooldown_remaining=remaining
                    )

            # Check type-based cooldown (any alert of same type)
            type_key_prefix = f"{alert_type}:"
            for key, record in self._cache.items():
                if key.startswith(type_key_prefix):
                    elapsed = (datetime.now() - record.timestamp).total_seconds()
                    if elapsed < cooldown_period:
                        remaining = int(cooldown_period - elapsed)
                        return DedupResult(
                            should_send=False,
                            reason=f"Alert type on cooldown ({remaining}s)",
                            cooldown_remaining=remaining
                        )

        return DedupResult(should_send=True)

    def record(self, alert_type: str, content: str) -> None:
        """
        Record that an alert was sent.

        Call this after successfully sending an alert.

        Args:
            alert_type: Type of alert
            content: Alert content
        """
        content_hash = self._hash_content(content)
        cache_key = f"{alert_type}:{content_hash}"

        record = AlertRecord(
            alert_type=alert_type,
            content_hash=content_hash,
            timestamp=datetime.now()
        )

        with self._lock:
            self._cache[cache_key] = record

        # Save to database
        self._save_alert(record)

        # Periodic cleanup
        if len(self._cache) > 100:
            self._cleanup_old_alerts()

    def check_and_record(
        self,
        alert_type: str,
        content: str,
        force: bool = False
    ) -> DedupResult:
        """
        Check if alert should be sent and record if yes.

        Convenience method combining check() and record().

        Args:
            alert_type: Type of alert
            content: Alert content
            force: Force send (bypass dedup)

        Returns:
            DedupResult
        """
        result = self.check(alert_type, content, force)

        if result.should_send:
            self.record(alert_type, content)

        return result

    def reset_cooldown(self, alert_type: str) -> None:
        """
        Reset cooldown for an alert type.

        Useful when conditions change and immediate alert is needed.

        Args:
            alert_type: Type of alert
        """
        with self._lock:
            type_prefix = f"{alert_type}:"
            keys_to_remove = [
                key for key in self._cache.keys()
                if key.startswith(type_prefix)
            ]
            for key in keys_to_remove:
                del self._cache[key]

        logger.debug(f"Reset cooldown for alert type: {alert_type}")

    def get_status(self) -> Dict[str, Any]:
        """
        Get deduplicator status.

        Returns:
            Status dictionary
        """
        with self._lock:
            cutoff = datetime.now() - timedelta(seconds=RATE_LIMIT_WINDOW)
            recent_count = sum(
                1 for record in self._cache.values()
                if record.timestamp > cutoff
            )

            # Group by type
            type_counts: Dict[str, int] = {}
            for record in self._cache.values():
                type_counts[record.alert_type] = type_counts.get(record.alert_type, 0) + 1

            return {
                'cached_alerts': len(self._cache),
                'recent_alerts_1h': recent_count,
                'rate_limit_remaining': MAX_ALERTS_PER_HOUR - recent_count,
                'alerts_by_type': type_counts
            }


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_deduplicator: Optional[AlertDeduplicator] = None


def get_deduplicator(
    cooldowns: Optional[Dict[str, int]] = None
) -> AlertDeduplicator:
    """
    Get or create singleton deduplicator.

    Args:
        cooldowns: Optional custom cooldowns

    Returns:
        AlertDeduplicator instance
    """
    global _deduplicator

    if _deduplicator is None:
        _deduplicator = AlertDeduplicator(cooldowns)

    return _deduplicator


def reset_deduplicator() -> None:
    """Reset singleton deduplicator."""
    global _deduplicator
    _deduplicator = None


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def should_send_alert(
    alert_type: str,
    content: str,
    force: bool = False
) -> bool:
    """
    Check if alert should be sent.

    Convenience function using singleton deduplicator.

    Args:
        alert_type: Type of alert
        content: Alert content
        force: Force send

    Returns:
        True if alert should be sent
    """
    dedup = get_deduplicator()
    result = dedup.check_and_record(alert_type, content, force)
    return result.should_send


def record_alert_sent(alert_type: str, content: str) -> None:
    """
    Record that an alert was sent.

    Args:
        alert_type: Type of alert
        content: Alert content
    """
    get_deduplicator().record(alert_type, content)


def reset_alert_cooldown(alert_type: str) -> None:
    """
    Reset cooldown for an alert type.

    Args:
        alert_type: Type of alert
    """
    get_deduplicator().reset_cooldown(alert_type)


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    import sys
    import time

    logger.remove()
    logger.add(sys.stderr, level="DEBUG")

    print("\n" + "=" * 60)
    print("SNAIL Alert Deduplication Test")
    print("=" * 60)

    # Use in-memory for testing
    dedup = AlertDeduplicator()

    # Test basic deduplication
    print("\n[1] Basic Deduplication Test:")
    content = "Test alert message"

    result1 = dedup.check_and_record("status", content)
    print(f"    First check: should_send={result1.should_send}")

    result2 = dedup.check("status", content)
    print(f"    Second check: should_send={result2.should_send}, reason={result2.reason}")

    # Test different content
    print("\n[2] Different Content Test:")
    result3 = dedup.check_and_record("status", "Different message")
    print(f"    Different content: should_send={result3.should_send}")

    # Test force flag
    print("\n[3] Force Flag Test:")
    result4 = dedup.check("status", content, force=True)
    print(f"    Force send: should_send={result4.should_send}, reason={result4.reason}")

    # Test no-cooldown alert types
    print("\n[4] No-Cooldown Alert Types:")
    entry_content = "Entry executed"
    result5 = dedup.check_and_record("entry", entry_content)
    print(f"    First entry: should_send={result5.should_send}")
    result6 = dedup.check("entry", entry_content)
    print(f"    Same entry (no cooldown): should_send={result6.should_send}")

    # Test cooldown reset
    print("\n[5] Cooldown Reset Test:")
    dedup.reset_cooldown("status")
    result7 = dedup.check("status", content)
    print(f"    After reset: should_send={result7.should_send}")

    # Test status
    print("\n[6] Deduplicator Status:")
    status = dedup.get_status()
    print(f"    Cached alerts: {status['cached_alerts']}")
    print(f"    Recent (1h): {status['recent_alerts_1h']}")
    print(f"    Rate limit remaining: {status['rate_limit_remaining']}")
    print(f"    By type: {status['alerts_by_type']}")

    print("\n" + "=" * 60)
    print("Alert deduplication test complete!")
    print("=" * 60 + "\n")
