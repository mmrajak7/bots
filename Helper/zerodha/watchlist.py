"""
Watchlist Store — CRUD operations with Google Drive sync.

Captures trade ideas from MCP brainstorming sessions and watches for
price-based entry conditions. Alerts via Telegram when conditions are met.

Architecture (mirrors bcs/trade_store.py):
  - In-memory cache for all reads (zero network on poll cycles)
  - Local file written FIRST on every write (data safety)
  - Drive sync: download on startup, upload on writes
  - Periodic re-sync from Drive (pick up changes from other machines)
  - If Drive fails at any point: fall back to local, log warning

Reuses bcs.drive_store for all Google Drive operations (no duplication).
"""

import json
import logging
import os
import platform
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from bcs import drive_store
from common import layered_config

logger = logging.getLogger(__name__)

# -- Paths -----------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()       # Helper/zerodha/
PROJECT_ROOT = SCRIPT_DIR.parent                    # Helper/
LOG_DIR = PROJECT_ROOT / 'logs'
LOCAL_FILE = LOG_DIR / 'watchlist.json'
CONFIG_FILE = PROJECT_ROOT / 'config' / 'watchlist_config.json'

# -- Required fields for add_item validation --------------------------------
REQUIRED_FIELDS = [
    'stock', 'spot_symbol', 'strategy', 'thesis', 'alert',
]

VALID_CONDITIONS = ('above', 'below')
VALID_STATUSES = ('watching', 'triggered', 'entered', 'expired', 'cancelled')


def _load_config() -> dict:
    # TWO LAYERS: config/watchlist_config.defaults.json (tracked, secret-free)
    # under config/watchlist_config.json (untracked, secrets). See
    # common/layered_config.py — including why the overlay wins.
    return layered_config.load('watchlist_config')


def _resolve_credentials_path(config: dict) -> Optional[Path]:
    env_path = os.environ.get('WATCHLIST_GOOGLE_CREDS')
    if env_path:
        return Path(env_path)

    drive_cfg = config.get('google_drive', {})
    if platform.system() == 'Windows':
        path_str = drive_cfg.get('credentials_path_windows')
    else:
        path_str = drive_cfg.get('credentials_path_linux')

    return Path(path_str) if path_str else None


class WatchlistStore:
    """Watchlist CRUD with local file + Google Drive sync.

    Usage:
        store = WatchlistStore()
        store.initialize()          # Auth Drive, download, fallback to local
        items = store.get_active()  # status == 'watching', from cache
        store.add_item({...})       # Validate, save local + Drive
        store.maybe_sync()          # Re-download from Drive if stale
    """

    def __init__(self, config: Optional[dict] = None):
        self._config = config or _load_config()
        self._items: list = []
        self._drive_service = None
        self._drive_file_id: Optional[str] = None
        self._drive_enabled = False
        self._last_sync_time = 0.0
        self._sync_interval = (
            self._config.get('google_drive', {}).get('sync_interval_sec', 300)
        )

    def initialize(self):
        """Startup: auth Drive, download fresh copy, fall back to local."""
        drive_cfg = self._config.get('google_drive', {})

        if drive_cfg.get('enabled', False):
            self._init_drive(drive_cfg)

        if self._drive_enabled:
            self._sync_from_drive()
        else:
            self._load_local()

        active = sum(1 for i in self._items if i.get('status') == 'watching')
        logger.info(
            "WatchlistStore initialized: %d items (%d watching), drive=%s",
            len(self._items), active,
            'enabled' if self._drive_enabled else 'disabled'
        )

    def _init_drive(self, drive_cfg: dict):
        creds_path = _resolve_credentials_path(self._config)
        if not creds_path or not creds_path.exists():
            logger.warning(
                "Drive credentials not found at %s, running local-only", creds_path
            )
            return

        try:
            self._drive_service = drive_store.get_drive_service(creds_path)
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('file_name', 'watchlist.json')
            self._drive_file_id = drive_store.find_file(
                self._drive_service, folder_id, file_name
            )
            self._drive_enabled = True
            logger.info(
                "Drive connected. File ID: %s",
                self._drive_file_id or '(will create on first write)'
            )
        except Exception as e:
            logger.warning("Drive init failed: %s. Running local-only.", e)

    def _sync_from_drive(self):
        """Download items from Drive and merge with local/in-memory state."""
        try:
            if self._drive_file_id:
                drive_items = drive_store.download_json(
                    self._drive_service, self._drive_file_id
                )
                base = self._items if self._items else self._read_local()
                merged = self._merge_items(base, drive_items)
                self._items = merged
                self._save_local()
                self._last_sync_time = time.time()
                drive_versions = {i['id']: i.get('version', 0) for i in drive_items}
                merged_versions = {i['id']: i.get('version', 0) for i in merged}
                if drive_versions != merged_versions:
                    logger.info("Merge diverged from Drive, re-uploading")
                    self._upload_to_drive()
            else:
                logger.info("No file on Drive yet, loading local")
                self._load_local()
        except Exception as e:
            logger.warning("Drive download failed: %s. Using local file.", e)
            self._load_local()

    def _upload_to_drive(self):
        if not self._drive_enabled:
            return

        try:
            drive_cfg = self._config.get('google_drive', {})
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('file_name', 'watchlist.json')
            self._drive_file_id = drive_store.upload_json(
                self._drive_service, folder_id, file_name,
                self._items, self._drive_file_id
            )
            self._last_sync_time = time.time()
        except Exception as e:
            logger.error("Drive upload failed: %s. Local file is safe.", e)

    def _read_local(self) -> list:
        if not LOCAL_FILE.exists():
            return []
        try:
            with open(LOCAL_FILE) as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError(f"Expected list, got {type(data).__name__}")
            return data
        except (json.JSONDecodeError, ValueError) as e:
            backup = LOCAL_FILE.with_suffix(
                f'.corrupt.{int(time.time())}.json'
            )
            try:
                LOCAL_FILE.rename(backup)
            except OSError:
                pass
            logger.critical(
                "Watchlist file CORRUPT (%s). Backed up to %s. Starting empty!",
                e, backup
            )
            return []

    def _load_local(self):
        self._items = self._read_local()
        if self._items:
            logger.info("Loaded %d watchlist items from local file", len(self._items))
        else:
            logger.info("No local watchlist file found, starting empty")

    @staticmethod
    def _merge_items(base: list, incoming: list) -> list:
        """Merge two item lists. Per item ID, higher version wins."""
        by_id: dict = {}

        for i in base:
            by_id[i['id']] = i

        for i in incoming:
            iid = i['id']
            if iid not in by_id:
                by_id[iid] = i
            elif i.get('version', 0) > by_id[iid].get('version', 0):
                by_id[iid] = i

        merged = sorted(by_id.values(), key=lambda i: i['id'])

        if len(merged) != len(base) or len(merged) != len(incoming):
            logger.warning(
                "Watchlist merge: base=%d + incoming=%d -> merged=%d",
                len(base), len(incoming), len(merged)
            )

        return merged

    def _save_local(self):
        """Write items to local JSON file (atomic)."""
        LOG_DIR.mkdir(exist_ok=True)
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(LOG_DIR), suffix='.tmp', prefix='watchlist_'
            )
            with os.fdopen(fd, 'w') as f:
                fd = None
                json.dump(self._items, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(LOCAL_FILE))
            tmp_path = None
        except Exception:
            if fd is not None:
                os.close(fd)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise

    # -- Read Operations (from cache, zero network) ------------------------

    def load_items(self) -> list:
        """Return all items from in-memory cache."""
        return self._items

    def get_active(self) -> list:
        """Return items with status == 'watching'."""
        return [i for i in self._items if i.get('status') == 'watching']

    def next_item_id(self) -> int:
        if not self._items:
            return 1
        return max(i['id'] for i in self._items) + 1

    # -- Write Operations (local first, then Drive) ------------------------

    def add_item(self, data: dict) -> dict:
        """Validate, assign ID, save local + Drive. Returns the item.

        Raises:
            ValueError: If required fields missing or alert config invalid.
        """
        missing = [f for f in REQUIRED_FIELDS if f not in data]
        if missing:
            raise ValueError(f"Missing required fields: {missing}")

        alert = data['alert']
        if not isinstance(alert, dict):
            raise ValueError("'alert' must be a dict with condition, price, message")
        if 'condition' not in alert or 'price' not in alert or 'message' not in alert:
            raise ValueError("'alert' must have: condition, price, message")
        if alert['condition'] not in VALID_CONDITIONS:
            raise ValueError(
                f"Invalid alert condition '{alert['condition']}'. "
                f"Must be one of: {VALID_CONDITIONS}"
            )
        if not isinstance(alert['price'], (int, float)) or alert['price'] <= 0:
            raise ValueError(f"Alert price must be a positive number, got {alert['price']}")

        data['id'] = self.next_item_id()
        data['version'] = 1
        data.setdefault('status', 'watching')
        data.setdefault('created_at', datetime.now().strftime('%Y-%m-%d'))
        data.setdefault('triggered_at', None)
        data.setdefault('triggered_price', None)
        data.setdefault('trade_id', None)
        data.setdefault('outcome', None)
        data.setdefault('outcome_notes', None)
        data.setdefault('plan', None)

        self._items.append(data)
        self._save_local()
        self._upload_to_drive()

        logger.info(
            "Added watchlist #%d: %s %s %s %.2f",
            data['id'], data['stock'], alert['condition'], data['strategy'],
            alert['price']
        )
        return data

    def trigger(self, item_id: int, price: float):
        """Mark item as triggered. One-shot — no further alerts."""
        for i in self._items:
            if i['id'] == item_id:
                i['status'] = 'triggered'
                i['triggered_at'] = datetime.now().isoformat()
                i['triggered_price'] = price
                i['version'] = i.get('version', 0) + 1
                self._save_local()
                self._upload_to_drive()
                logger.info("Watchlist #%d triggered at %.2f", item_id, price)
                return
        logger.error("Watchlist #%d not found for trigger", item_id)

    def update_status(self, item_id: int, status: str, **extra_fields):
        """Update item status (entered/expired/cancelled). Version bumps."""
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status '{status}'. Must be one of: {VALID_STATUSES}")

        for i in self._items:
            if i['id'] == item_id:
                i['status'] = status
                i['version'] = i.get('version', 0) + 1
                for k, v in extra_fields.items():
                    i[k] = v
                self._save_local()
                self._upload_to_drive()
                logger.info("Watchlist #%d status -> %s", item_id, status)
                return
        logger.error("Watchlist #%d not found for status update", item_id)

    # -- Sync --------------------------------------------------------------

    def maybe_sync(self, force: bool = False):
        """Re-sync from Drive if stale. Called from monitor loop."""
        if not self._drive_enabled:
            return

        elapsed = time.time() - self._last_sync_time
        if force or elapsed >= self._sync_interval:
            self._sync_from_drive()

    # -- Display -----------------------------------------------------------

    def list_items(self, status_filter: Optional[str] = None):
        """Print formatted table of watchlist items."""
        items = self._items
        if status_filter:
            items = [i for i in items if i.get('status') == status_filter]

        if not items:
            print("No watchlist items found.")
            return

        print(f"\n{'ID':>3}  {'Stock':<14} {'Status':<10} {'Strategy':<6} "
              f"{'Condition':<8} {'Price':>10}  {'Created':<12} {'Thesis'}")
        print("-" * 110)

        for i in items:
            alert = i.get('alert', {})
            thesis = i.get('thesis', '')
            if len(thesis) > 40:
                thesis = thesis[:37] + '...'

            triggered_info = ''
            if i['status'] == 'triggered' and i.get('triggered_at'):
                triggered_info = f" @ {i['triggered_price']:.2f}"

            print(f"{i['id']:>3}  {i['stock']:<14} {i['status']:<10} "
                  f"{i.get('strategy', '?'):<6} "
                  f"{alert.get('condition', '?'):<8} "
                  f"{alert.get('price', 0):>10.2f}  "
                  f"{i.get('created_at', '?'):<12} "
                  f"{thesis}{triggered_info}")

        watching = sum(1 for i in items if i.get('status') == 'watching')
        triggered = sum(1 for i in items if i.get('status') == 'triggered')
        print(f"\nWatching: {watching} | Triggered: {triggered} | Total: {len(items)}")


# -- Singleton -------------------------------------------------------------

_store: Optional[WatchlistStore] = None


def get_watchlist_store() -> WatchlistStore:
    """Get or create the singleton WatchlistStore."""
    global _store
    if _store is None:
        _store = WatchlistStore()
        _store.initialize()
    return _store
