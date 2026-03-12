"""
BCS Trade Store — CRUD operations with Google Drive sync.

Architecture:
  - In-memory cache for all reads (zero network on poll cycles)
  - Local file written FIRST on every write (data safety)
  - Drive sync: download on startup, upload on writes
  - Periodic re-sync from Drive (pick up changes from other machines)
  - If Drive fails at any point: fall back to local, log warning

Principle: Local file is ALWAYS written first. Drive is nice-to-have
but never blocks trading operations.
"""

import json
import logging
import os
import platform
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import drive_store

logger = logging.getLogger(__name__)

# ── Paths ───────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()       # Helper/bcs/
PROJECT_ROOT = SCRIPT_DIR.parent                   # Helper/
LOG_DIR = PROJECT_ROOT / 'logs'
LOCAL_TRADES_FILE = LOG_DIR / 'bcs_trades.json'
CONFIG_FILE = PROJECT_ROOT / 'config' / 'bcs_config.json'

# ── Required fields for add_trade validation ────────────────────────────────
REQUIRED_FIELDS = [
    'stock', 'long_symbol', 'short_symbol', 'spot_symbol', 'exchange',
    'quantity', 'entry_long_price', 'entry_short_price', 'net_debit',
    'spread_width', 'target_spot', 'sl_spot', 'sl_spread', 'expiry',
]


def _load_config() -> dict:
    """Load BCS config from config/bcs_config.json."""
    if not CONFIG_FILE.exists():
        logger.warning("Config file not found at %s, using defaults", CONFIG_FILE)
        return {}
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _resolve_credentials_path(config: dict) -> Optional[Path]:
    """Resolve credentials path based on platform and env var override."""
    # Env var takes precedence
    env_path = os.environ.get('BCS_GOOGLE_CREDS')
    if env_path:
        return Path(env_path)

    drive_cfg = config.get('google_drive', {})
    if platform.system() == 'Windows':
        path_str = drive_cfg.get('credentials_path_windows')
    else:
        path_str = drive_cfg.get('credentials_path_linux')

    return Path(path_str) if path_str else None


class TradeStore:
    """Trade CRUD with local file + Google Drive sync.

    Usage:
        store = TradeStore()
        store.initialize()       # Auth Drive, download, fallback to local
        trades = store.load_trades()  # From cache, zero network
        store.add_trade({...})   # Validate, save local + Drive
        store.maybe_sync()       # Re-download from Drive if stale
    """

    def __init__(self, config: Optional[dict] = None):
        self._config = config or _load_config()
        self._trades: list = []
        self._drive_service = None
        self._drive_file_id: Optional[str] = None
        self._drive_enabled = False
        self._last_sync_time = 0.0
        self._sync_interval = (
            self._config.get('google_drive', {}).get('sync_interval_sec', 300)
        )
        # Prevents Drive sync from overwriting in-memory state during
        # close operations. Set True by begin_close(), cleared by
        # update_trade_exit() or set_trade_status().
        self._sync_locked = False

    def initialize(self):
        """Startup: auth Drive, download fresh copy, fall back to local."""
        drive_cfg = self._config.get('google_drive', {})

        if drive_cfg.get('enabled', False):
            self._init_drive(drive_cfg)

        if self._drive_enabled:
            self._sync_from_drive()
        else:
            self._load_local()

        self._migrate_trades()

        open_count = sum(1 for t in self._trades if t.get('status') == 'open')
        logger.info(
            "TradeStore initialized: %d trades (%d open), drive=%s",
            len(self._trades), open_count,
            'enabled' if self._drive_enabled else 'disabled'
        )

    def _init_drive(self, drive_cfg: dict):
        """Authenticate with Google Drive. Sets _drive_enabled on success."""
        creds_path = _resolve_credentials_path(self._config)
        if not creds_path or not creds_path.exists():
            logger.warning(
                "Drive credentials not found at %s, running local-only", creds_path
            )
            return

        try:
            self._drive_service = drive_store.get_drive_service(creds_path)
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('file_name', 'bcs_trades.json')
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
        """Download trades from Drive and merge with local/in-memory state.

        Merge strategy (per trade ID, higher version wins):
          - Protects against lost trades when a prior Drive upload failed
          - Picks up new trades added from other machines
          - If merge differs from Drive, re-uploads to Drive
        """
        try:
            if self._drive_file_id:
                drive_trades = drive_store.download_json(
                    self._drive_service, self._drive_file_id
                )
                # Merge: current state (in-memory or local) vs Drive
                # On startup self._trades is [] so we also read local file
                base = self._trades if self._trades else self._read_local()
                merged = self._merge_trades(base, drive_trades)
                self._trades = merged
                self._save_local()
                self._last_sync_time = time.time()
                # If merge diverged from Drive, push merged state back
                drive_versions = {t['id']: t.get('version', 0) for t in drive_trades}
                merged_versions = {t['id']: t.get('version', 0) for t in merged}
                if drive_versions != merged_versions:
                    logger.info("Merge diverged from Drive, re-uploading")
                    self._upload_to_drive()
            else:
                # File doesn't exist on Drive yet — load local
                logger.info("No file on Drive yet, loading local")
                self._load_local()
        except Exception as e:
            logger.warning("Drive download failed: %s. Using local file.", e)
            self._load_local()

    def _upload_to_drive(self):
        """Upload current trades to Drive. Logs error on failure (local already saved)."""
        if not self._drive_enabled:
            return

        try:
            drive_cfg = self._config.get('google_drive', {})
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('file_name', 'bcs_trades.json')
            self._drive_file_id = drive_store.upload_json(
                self._drive_service, folder_id, file_name,
                self._trades, self._drive_file_id
            )
            self._last_sync_time = time.time()
        except Exception as e:
            logger.error("Drive upload failed: %s. Local file is safe.", e)

    def _read_local(self) -> list:
        """Read trades from local JSON file without setting self._trades.

        Corruption-safe: if the file is truncated/corrupt (e.g. crash during
        write), backs it up and returns empty list rather than crashing.
        """
        if not LOCAL_TRADES_FILE.exists():
            return []
        try:
            with open(LOCAL_TRADES_FILE) as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError(f"Expected list, got {type(data).__name__}")
            return data
        except (json.JSONDecodeError, ValueError) as e:
            # File is corrupt — back it up so we can investigate, start fresh
            backup = LOCAL_TRADES_FILE.with_suffix(
                f'.corrupt.{int(time.time())}.json'
            )
            try:
                LOCAL_TRADES_FILE.rename(backup)
            except OSError:
                pass  # Can't rename — at least don't crash
            logger.critical(
                "Trade file CORRUPT (%s). Backed up to %s. Starting empty!",
                e, backup
            )
            return []

    def _load_local(self):
        """Load trades from local JSON file into cache."""
        self._trades = self._read_local()
        if self._trades:
            logger.info("Loaded %d trades from local file", len(self._trades))
        else:
            logger.info("No local trades file found, starting empty")

    @staticmethod
    def _merge_trades(base: list, incoming: list) -> list:
        """Merge two trade lists. Per trade ID, higher version wins.

        This protects against data loss when:
          - A Drive upload failed (base has data incoming doesn't)
          - A new trade was added on another machine (incoming has data base doesn't)
          - A trade was closed locally but Drive still shows it open
        """
        by_id: dict = {}

        for t in base:
            by_id[t['id']] = t

        for t in incoming:
            tid = t['id']
            if tid not in by_id:
                # New trade from the other side
                by_id[tid] = t
            elif t.get('version', 0) > by_id[tid].get('version', 0):
                # Incoming has a newer version
                by_id[tid] = t
            # else: base version is equal or higher, keep it

        merged = sorted(by_id.values(), key=lambda t: t['id'])

        if len(merged) != len(base) or len(merged) != len(incoming):
            logger.warning(
                "Trade merge: base=%d + incoming=%d -> merged=%d",
                len(base), len(incoming), len(merged)
            )

        return merged

    def _save_local(self):
        """Write trades to local JSON file (atomic).

        Writes to a temp file first, then does os.replace() which is atomic
        on both Windows (NTFS) and Linux (same filesystem). This prevents
        data corruption if the process is killed mid-write.
        """
        LOG_DIR.mkdir(exist_ok=True)
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(LOG_DIR), suffix='.tmp', prefix='bcs_trades_'
            )
            with os.fdopen(fd, 'w') as f:
                fd = None  # os.fdopen takes ownership of fd
                json.dump(self._trades, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(LOCAL_TRADES_FILE))
            tmp_path = None  # replaced successfully, don't clean up
        except Exception:
            # Clean up temp file on failure
            if fd is not None:
                os.close(fd)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise

    def _migrate_trades(self):
        """Backfill version, lot_size, lots fields on existing trades."""
        changed = False
        for t in self._trades:
            if 'version' not in t:
                t['version'] = 1
                changed = True
            if 'lot_size' not in t:
                # For existing trades without lot_size:
                # assume quantity == 1 lot (safe default for backfill)
                t['lot_size'] = t['quantity']
                t['lots'] = 1
                changed = True
            if 'lots' not in t:
                t['lots'] = t['quantity'] // t['lot_size']
                changed = True

        if changed:
            logger.info("Migrated %d trades (added version/lot_size/lots)", len(self._trades))
            self._save_local()
            self._upload_to_drive()

    # ── Read Operations (from cache, zero network) ──────────────────────

    def load_trades(self) -> list:
        """Return all trades from in-memory cache. No network."""
        return self._trades

    def get_open_trades(self) -> list:
        """Return only open trades from cache."""
        return [t for t in self._trades if t.get('status') == 'open']

    def get_closing_trades(self) -> list:
        """Return trades stuck in 'closing' status (from a crashed close attempt)."""
        return [t for t in self._trades if t.get('status') == 'closing']

    def recover_closing_trade(self, trade_id: int):
        """Reset a 'closing' trade back to 'open' for re-monitoring.

        Used at startup to recover trades left in 'closing' from a previous crash.
        """
        for t in self._trades:
            if t['id'] == trade_id and t.get('status') == 'closing':
                t['status'] = 'open'
                t['version'] = t.get('version', 0) + 1
                t.pop('close_reason', None)
                t.pop('close_started', None)
                self._sync_locked = False
                self._save_local()
                self._upload_to_drive()
                logger.warning("Trade #%d recovered from 'closing' -> 'open'", trade_id)
                return True
        return False

    def find_open_trade(self, stock: str, trade_id: Optional[int] = None) -> Optional[dict]:
        """Find an open trade by stock name, optionally filtered by trade ID."""
        for t in self._trades:
            if t['status'] != 'open':
                continue
            if trade_id is not None and t['id'] != trade_id:
                continue
            if t['stock'].upper() == stock.upper():
                return t
        return None

    def next_trade_id(self) -> int:
        """Return the next available trade ID."""
        if not self._trades:
            return 1
        return max(t['id'] for t in self._trades) + 1

    # ── Write Operations (local first, then Drive) ──────────────────────

    def add_trade(self, trade_dict: dict) -> dict:
        """Validate, assign ID, save local + Drive. Returns the trade.

        Raises:
            ValueError: If required fields missing or lot_size validation fails.
        """
        # Validate required fields
        missing = [f for f in REQUIRED_FIELDS if f not in trade_dict]
        if missing:
            raise ValueError(f"Missing required fields: {missing}")

        # Validate lot_size
        lot_size = trade_dict.get('lot_size')
        quantity = trade_dict['quantity']

        if lot_size is not None:
            if lot_size <= 0:
                raise ValueError(f"lot_size must be positive, got {lot_size}")
            if quantity % lot_size != 0:
                raise ValueError(
                    f"quantity ({quantity}) must be a multiple of lot_size ({lot_size}). "
                    f"Got remainder: {quantity % lot_size}"
                )
            trade_dict['lots'] = quantity // lot_size
        else:
            # No lot_size provided — treat quantity as 1 lot
            trade_dict['lot_size'] = quantity
            trade_dict['lots'] = 1

        # Assign metadata
        trade_dict['id'] = self.next_trade_id()
        trade_dict['version'] = 1
        trade_dict.setdefault('account_id', None)  # e.g. "YL6478"
        trade_dict.setdefault('status', 'open')
        trade_dict.setdefault('exit', None)

        self._trades.append(trade_dict)
        self._save_local()
        self._upload_to_drive()

        logger.info("Added trade #%d: %s %s/%s",
                     trade_dict['id'], trade_dict['stock'],
                     trade_dict['long_symbol'], trade_dict['short_symbol'])
        return trade_dict

    def update_trade_exit(self, trade_id: int, exit_data: dict):
        """Mark a trade as closed with exit details. Saves local + Drive."""
        found = False
        for t in self._trades:
            if t['id'] == trade_id:
                t['status'] = 'closed'
                t['exit'] = exit_data
                t['version'] = t.get('version', 0) + 1
                found = True
                break

        self._sync_locked = False  # Always release lock, even if trade not found

        if not found:
            logger.error("Trade #%d not found for exit update", trade_id)
            return
        self._save_local()
        self._upload_to_drive()
        logger.info("Trade #%d closed: %s", trade_id, exit_data.get('exit_reason', ''))

    def begin_close(self, trade_id: int, reason: str) -> bool:
        """Acquire close-lock on a trade. Returns True if lock acquired.

        Sets status to 'closing' with version bump. Saves to local + Drive
        immediately so other machines see the lock before placing orders.
        Prevents the 2x close-order bug from concurrent processes.

        Returns False if trade is not 'open' (already closing/closed).
        """
        for t in self._trades:
            if t['id'] == trade_id:
                if t['status'] != 'open':
                    logger.warning(
                        "Trade #%d status is '%s', cannot begin close",
                        trade_id, t['status']
                    )
                    return False
                t['status'] = 'closing'
                t['close_reason'] = reason
                t['close_started'] = datetime.now().isoformat()
                t['version'] = t.get('version', 0) + 1
                self._sync_locked = True
                self._save_local()
                self._upload_to_drive()
                logger.info(
                    "Trade #%d close-lock acquired: %s", trade_id, reason
                )
                return True
        logger.error("Trade #%d not found for begin_close", trade_id)
        return False

    def set_trade_status(self, trade_id: int, status: str, **extra_fields):
        """Update trade status and optional extra fields. Saves local + Drive.

        Used for state transitions: open → closing → closed / partial_close.
        """
        for t in self._trades:
            if t['id'] == trade_id:
                t['status'] = status
                t['version'] = t.get('version', 0) + 1
                for k, v in extra_fields.items():
                    t[k] = v
                self._sync_locked = False
                self._save_local()
                self._upload_to_drive()
                logger.info("Trade #%d status -> %s", trade_id, status)
                return
        logger.error("Trade #%d not found for set_trade_status", trade_id)

    def update_trade_fields(self, trade_id: int, **fields):
        """Update arbitrary fields on a trade. Saves local only (no Drive upload).

        Used for lightweight state updates like trailing SL that change every
        few seconds — Drive sync happens on the normal maybe_sync() cycle.
        """
        for t in self._trades:
            if t['id'] == trade_id:
                for k, v in fields.items():
                    t[k] = v
                self._save_local()
                return True
        return False

    # ── Sync ────────────────────────────────────────────────────────────

    def maybe_sync(self, force: bool = False):
        """Re-sync from Drive if stale. Called from monitor loop.

        Only hits network if sync_interval_sec has elapsed (default 300s),
        or if force=True. Skipped entirely when _sync_locked is set
        (during close operations) to prevent overwriting in-progress state.
        """
        if not self._drive_enabled:
            return
        if self._sync_locked:
            return

        elapsed = time.time() - self._last_sync_time
        if force or elapsed >= self._sync_interval:
            self._sync_from_drive()

    # ── Display ─────────────────────────────────────────────────────────

    def list_trades(self):
        """Print formatted table of all trades."""
        trades = self._trades
        if not trades:
            print("No trades found.")
            return

        print(f"\n{'ID':>3}  {'Stock':<12} {'Status':<8} {'Strikes':<16} "
              f"{'Expiry':<12} {'Debit':>8} {'Lots':>8} {'Target':>8} "
              f"{'SL Spot':>8} {'SL Spread':>10}")
        print("-" * 110)

        for t in trades:
            long_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:CE|PE)\s*$', t['long_symbol'])
            short_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:CE|PE)\s*$', t['short_symbol'])
            long_strike = long_match.group(1) if long_match else '?'
            short_strike = short_match.group(1) if short_match else '?'
            strikes_str = f"{long_strike}/{short_strike} CE"

            lots = t.get('lots', '?')
            lot_size = t.get('lot_size', '?')
            lots_str = f"{lots}x{lot_size}"

            print(f"{t['id']:>3}  {t['stock']:<12} {t['status']:<8} "
                  f"{strikes_str:<16} {t['expiry']:<12} "
                  f"{t['net_debit']:>8.2f} {lots_str:>8} "
                  f"{t['target_spot']:>8.1f} {t['sl_spot']:>8.1f} "
                  f"{t['sl_spread']:>10.2f}")

        open_trades = [t for t in trades if t['status'] == 'open']
        closed_trades = [t for t in trades if t['status'] == 'closed']
        print(f"\nOpen: {len(open_trades)} | Closed: {len(closed_trades)} "
              f"| Total: {len(trades)}")

        if closed_trades:
            total_pnl = sum(
                t['exit'].get('total_pnl', 0)
                for t in closed_trades if t.get('exit')
            )
            print(f"Closed P&L: Rs {total_pnl:+,.0f}")


# ── Backward-compatible free functions (delegate to singleton) ──────────────

_store: Optional[TradeStore] = None


def get_store() -> TradeStore:
    """Get or create the singleton TradeStore."""
    global _store
    if _store is None:
        _store = TradeStore()
        _store.initialize()
    return _store


def load_trades() -> list:
    """Load all trades (backward compat)."""
    return get_store().load_trades()


def add_trade(trade_dict: dict) -> dict:
    """Add a new trade (backward compat)."""
    return get_store().add_trade(trade_dict)


def update_trade_exit(trade_id: int, exit_data: dict):
    """Mark trade as closed (backward compat)."""
    get_store().update_trade_exit(trade_id, exit_data)
