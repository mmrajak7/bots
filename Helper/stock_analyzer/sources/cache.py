"""
File-based JSON cache for stock analyzer data sources.

Cache location: Helper/data/cache/stock_analyzer/
Key format:     {SYMBOL}_{source}_{YYYYMMDD}.json

Each source gets a configurable TTL. Stale entries are ignored on read
and cleaned up lazily (no background thread).
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent.resolve()          # stock_analyzer/sources/
PACKAGE_DIR = SCRIPT_DIR.parent                        # stock_analyzer/
PROJECT_ROOT = PACKAGE_DIR.parent                      # Helper/
CACHE_DIR = PROJECT_ROOT / 'data' / 'cache' / 'stock_analyzer'

# ── Default TTLs (hours) per source ──────────────────────────────────────────

DEFAULT_TTLS = {
    'screener': 24,         # Fundamentals change once per quarter
    'kite_technical': 1,    # Technicals need frequent refresh
    'trendlyne': 4,         # Analyst data refreshes a few times a day
}


class CacheManager:
    """Transparent file-based JSON cache.

    Usage:
        cache = CacheManager()
        data = cache.get('RELIANCE', 'screener')
        if data is None:
            data = expensive_fetch(...)
            cache.set('RELIANCE', 'screener', data)
    """

    def __init__(self, cache_dir: Optional[Path] = None,
                 ttl_overrides: Optional[dict] = None):
        self._cache_dir = cache_dir or CACHE_DIR
        self._ttl_overrides = ttl_overrides or {}
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        """Create cache directory if it doesn't exist."""
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Cannot create cache dir %s: %s", self._cache_dir, e)

    def _cache_key(self, symbol: str, source: str) -> str:
        """Build cache filename: SYMBOL_source_YYYYMMDD.json"""
        today = datetime.now().strftime('%Y%m%d')
        return f"{symbol.upper()}_{source}_{today}.json"

    def _cache_path(self, symbol: str, source: str) -> Path:
        return self._cache_dir / self._cache_key(symbol, source)

    def _ttl_seconds(self, source: str, ttl_hours: Optional[float] = None) -> float:
        """Resolve TTL in seconds: explicit > override > default."""
        if ttl_hours is not None:
            return ttl_hours * 3600
        hours = self._ttl_overrides.get(source, DEFAULT_TTLS.get(source, 24))
        return hours * 3600

    def get(self, symbol: str, source: str,
            ttl_hours: Optional[float] = None) -> Optional[Any]:
        """Read cached data if it exists and is not stale.

        Args:
            symbol:    Stock symbol (e.g. 'RELIANCE')
            source:    Source name (e.g. 'screener', 'kite_technical')
            ttl_hours: Override TTL for this call (None = use default)

        Returns:
            Cached data dict, or None if miss/stale/corrupt.
        """
        path = self._cache_path(symbol, source)
        if not path.exists():
            return None

        # Check staleness
        try:
            file_age = time.time() - path.stat().st_mtime
            ttl = self._ttl_seconds(source, ttl_hours)
            if file_age > ttl:
                logger.debug("Cache stale for %s/%s (age=%.0fs, ttl=%.0fs)",
                             symbol, source, file_age, ttl)
                return None
        except OSError:
            return None

        # Read
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            logger.debug("Cache hit: %s/%s", symbol, source)
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Cache read failed for %s/%s: %s", symbol, source, e)
            # Remove corrupt file
            try:
                path.unlink()
            except OSError:
                pass
            return None

    def set(self, symbol: str, source: str, data: Any) -> bool:
        """Write data to cache.

        Args:
            symbol: Stock symbol
            source: Source name
            data:   JSON-serializable data

        Returns:
            True if written successfully, False otherwise.
        """
        self._ensure_dir()
        path = self._cache_path(symbol, source)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, default=str, ensure_ascii=False)
            logger.debug("Cache set: %s/%s -> %s", symbol, source, path.name)
            return True
        except (OSError, TypeError) as e:
            logger.warning("Cache write failed for %s/%s: %s", symbol, source, e)
            return False

    def invalidate(self, symbol: str, source: str) -> bool:
        """Remove cached entry for a specific symbol+source.

        Returns True if file was deleted, False if not found or error.
        """
        path = self._cache_path(symbol, source)
        if not path.exists():
            return False
        try:
            path.unlink()
            logger.debug("Cache invalidated: %s/%s", symbol, source)
            return True
        except OSError as e:
            logger.warning("Cache invalidate failed for %s/%s: %s",
                           symbol, source, e)
            return False

    def invalidate_all(self, symbol: str) -> int:
        """Remove all cached entries for a symbol (all sources).

        Returns count of files deleted.
        """
        count = 0
        prefix = f"{symbol.upper()}_"
        try:
            for path in self._cache_dir.iterdir():
                if path.name.startswith(prefix) and path.suffix == '.json':
                    try:
                        path.unlink()
                        count += 1
                    except OSError:
                        pass
        except OSError:
            pass
        if count:
            logger.debug("Cache invalidated %d entries for %s", count, symbol)
        return count

    def cleanup_stale(self, max_age_hours: float = 48) -> int:
        """Remove cache files older than max_age_hours. Returns count deleted."""
        count = 0
        cutoff = time.time() - (max_age_hours * 3600)
        try:
            for path in self._cache_dir.iterdir():
                if path.suffix != '.json':
                    continue
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        count += 1
                except OSError:
                    pass
        except OSError:
            pass
        if count:
            logger.info("Cache cleanup: removed %d stale files", count)
        return count


# ── Module-level singleton ───────────────────────────────────────────────────

_cache: Optional[CacheManager] = None


def get_cache() -> CacheManager:
    """Return the module-level CacheManager singleton."""
    global _cache
    if _cache is None:
        _cache = CacheManager()
    return _cache
