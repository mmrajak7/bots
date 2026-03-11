"""
Price Alert Manager for Kayal Trading Terminal

Manages price alerts with persistence to JSON file.
Alerts trigger when price crosses above (>) or drops below (<) a threshold.
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PriceAlert:
    """Represents a single price alert."""
    alert_id: str
    symbol: str
    condition: str  # '<' (below) or '>' (above)
    trigger_price: float
    comments: str = ""

    def is_triggered(self, ltp: float) -> bool:
        """Check if alert is triggered by the given LTP."""
        if ltp is None or ltp <= 0:
            return False

        if self.condition == '<':
            return ltp < self.trigger_price
        elif self.condition == '>':
            return ltp > self.trigger_price
        return False


class PriceAlertManager:
    """
    Manages price alerts with file persistence.

    Thread-safe implementation for use with GUI timers.
    """

    def __init__(self, filepath: str):
        """
        Initialize alert manager.

        Args:
            filepath: Path to JSON file for persistence (e.g., 'data/alerts.json')
        """
        self._filepath = Path(filepath)
        self._alerts: Dict[str, PriceAlert] = {}
        self._lock = Lock()

        # Ensure directory exists
        self._filepath.parent.mkdir(parents=True, exist_ok=True)

        # Load existing alerts
        self._load()

    def _load(self) -> None:
        """Load alerts from file."""
        if not self._filepath.exists():
            return

        try:
            with open(self._filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)

            with self._lock:
                self._alerts.clear()
                for item in data:
                    alert = PriceAlert(
                        alert_id=item.get('alert_id', str(uuid.uuid4())),
                        symbol=item.get('symbol', ''),
                        condition=item.get('condition', '>'),
                        trigger_price=float(item.get('trigger_price', 0)),
                        comments=item.get('comments', '')
                    )
                    if alert.symbol and alert.trigger_price > 0:
                        self._alerts[alert.alert_id] = alert

            logger.info(f"[ALERTS] Loaded {len(self._alerts)} alerts from {self._filepath}")

        except Exception as e:
            logger.warning(f"[ALERTS] Failed to load alerts: {e}")

    def _save(self) -> None:
        """Save alerts to file. Must be called with lock held."""
        try:
            data = [asdict(alert) for alert in self._alerts.values()]
            with open(self._filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"[ALERTS] Failed to save alerts: {e}")

    def add_alert(
        self,
        symbol: str,
        condition: str,
        trigger_price: float,
        comments: str = ""
    ) -> PriceAlert:
        """
        Add a new price alert.

        Args:
            symbol: Trading symbol (e.g., 'NIFTY25JAN23500CE')
            condition: '<' for below, '>' for above
            trigger_price: Price threshold
            comments: Optional comments/notes

        Returns:
            The created PriceAlert
        """
        alert = PriceAlert(
            alert_id=str(uuid.uuid4()),
            symbol=symbol.strip().upper(),
            condition=condition if condition in ('<', '>') else '>',
            trigger_price=float(trigger_price),
            comments=comments.strip() if comments else ""
        )

        with self._lock:
            self._alerts[alert.alert_id] = alert
            self._save()

        logger.info(f"[ALERTS] Added: {alert.symbol} {alert.condition} {alert.trigger_price}")
        return alert

    def remove_alert(self, alert_id: str) -> bool:
        """
        Remove an alert by ID.

        Args:
            alert_id: The alert ID to remove

        Returns:
            True if removed, False if not found
        """
        with self._lock:
            if alert_id in self._alerts:
                alert = self._alerts.pop(alert_id)
                self._save()
                logger.info(f"[ALERTS] Removed: {alert.symbol} {alert.condition} {alert.trigger_price}")
                return True
        return False

    def clear_all(self) -> int:
        """
        Clear all alerts.

        Returns:
            Number of alerts cleared
        """
        with self._lock:
            count = len(self._alerts)
            self._alerts.clear()
            self._save()

        logger.info(f"[ALERTS] Cleared {count} alerts")
        return count

    def get_all_alerts(self) -> List[PriceAlert]:
        """
        Get all active alerts.

        Returns:
            List of PriceAlert objects
        """
        with self._lock:
            return list(self._alerts.values())

    def get_alert(self, alert_id: str) -> Optional[PriceAlert]:
        """
        Get a specific alert by ID.

        Args:
            alert_id: The alert ID

        Returns:
            PriceAlert or None if not found
        """
        with self._lock:
            return self._alerts.get(alert_id)

    def update_alert(
        self,
        alert_id: str,
        trigger_price: float = None,
        condition: str = None,
        comments: str = None
    ) -> bool:
        """
        Update an existing alert.

        Args:
            alert_id: The alert ID to update
            trigger_price: New trigger price (optional)
            condition: New condition (optional)
            comments: New comments (optional)

        Returns:
            True if updated, False if not found
        """
        with self._lock:
            if alert_id not in self._alerts:
                return False

            alert = self._alerts[alert_id]

            # Update fields if provided
            if trigger_price is not None:
                alert.trigger_price = float(trigger_price)
            if condition is not None and condition in ('<', '>'):
                alert.condition = condition
            if comments is not None:
                alert.comments = comments.strip()

            self._save()

        logger.info(f"[ALERTS] Updated: {alert.symbol} {alert.condition} {alert.trigger_price}")
        return True

    def check_alerts(self, ltp_map: Dict[str, float]) -> List[PriceAlert]:
        """
        Check all alerts against current LTP values.

        Triggered alerts are automatically removed.

        Args:
            ltp_map: Dict mapping symbol -> LTP

        Returns:
            List of triggered alerts (already removed from active alerts)
        """
        triggered: List[PriceAlert] = []

        with self._lock:
            to_remove = []

            for alert_id, alert in self._alerts.items():
                ltp = ltp_map.get(alert.symbol)
                if ltp is not None and alert.is_triggered(ltp):
                    triggered.append(alert)
                    to_remove.append(alert_id)

            # Remove triggered alerts
            for alert_id in to_remove:
                del self._alerts[alert_id]

            if to_remove:
                self._save()

        for alert in triggered:
            logger.info(f"[ALERTS] TRIGGERED: {alert.symbol} {alert.condition} {alert.trigger_price}")

        return triggered

    def __len__(self) -> int:
        """Return number of active alerts."""
        with self._lock:
            return len(self._alerts)


# Test if run directly
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Test the manager
    mgr = PriceAlertManager('test_alerts.json')

    # Add alerts
    mgr.add_alert('NIFTY25JAN23500CE', '>', 150.0, 'Entry signal')
    mgr.add_alert('NIFTY25JAN23500PE', '<', 100.0, 'Exit signal')

    print(f"Active alerts: {len(mgr)}")
    for a in mgr.get_all_alerts():
        print(f"  {a.symbol} {a.condition} {a.trigger_price} - {a.comments}")

    # Test trigger
    ltp_map = {'NIFTY25JAN23500CE': 160.0}  # Above 150
    triggered = mgr.check_alerts(ltp_map)
    print(f"Triggered: {[a.symbol for a in triggered]}")
    print(f"Remaining: {len(mgr)}")

    # Cleanup
    os.remove('test_alerts.json')
