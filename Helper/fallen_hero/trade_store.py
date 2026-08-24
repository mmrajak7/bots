"""
Fallen Hero Trade Store — CRUD operations with Google Drive sync.

Strategy: Reverse Jade Lizard (Bull Put Spread + Naked Short Call)
  - BUY OTM Put (long put, protective)
  - SELL ATM/NTM Put (short put, credit)
  - SELL OTM Call (naked short, credit)

Credit strategy: total_credit = put_spread_credit + call_credit
Zero downside risk when total_credit >= put_spread_width.

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
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from bcs import drive_store
from common.locked_store import LockTimeout, LockedStoreMixin

logger = logging.getLogger(__name__)

# -- Paths -----------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()       # Helper/fallen_hero/
PROJECT_ROOT = SCRIPT_DIR.parent                    # Helper/
LOG_DIR = PROJECT_ROOT / 'logs'
LOCAL_TRADES_FILE = LOG_DIR / 'fallen_hero_trades.json'
#: One lock PER BOOK. The monitor writes all three every
#: poll; a shared lock would serialise them for no reason.
LOCK_FILE = LOG_DIR / 'fallen_hero_trades.lock'
CONFIG_FILE = PROJECT_ROOT / 'config' / 'fallen_hero_config.json'

# -- Required fields for add_trade validation ------------------------------
REQUIRED_FIELDS = [
    'stock', 'long_put_symbol', 'short_put_symbol', 'short_call_symbol',
    'spot_symbol', 'exchange', 'quantity',
    'entry_long_put_price', 'entry_short_put_price', 'entry_short_call_price',
    'long_put_strike', 'short_put_strike', 'short_call_strike',
    'put_spread_width', 'put_spread_credit', 'call_credit', 'total_credit',
    'breakeven', 'sl_spot', 'entry_date', 'entry_spot', 'expiry',
]


def _load_config() -> dict:
    """Load Fallen Hero config from config/fallen_hero_config.json."""
    if not CONFIG_FILE.exists():
        logger.warning("Config file not found at %s, using defaults", CONFIG_FILE)
        return {}
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _resolve_credentials_path(config: dict) -> Optional[Path]:
    """Resolve credentials path based on platform and env var override."""
    # Env var takes precedence
    env_path = os.environ.get('FH_GOOGLE_CREDS')
    if env_path:
        return Path(env_path)

    drive_cfg = config.get('google_drive', {})
    if platform.system() == 'Windows':
        path_str = drive_cfg.get('credentials_path_windows')
    else:
        path_str = drive_cfg.get('credentials_path_linux')

    return Path(path_str) if path_str else None


class FallenHeroStore(LockedStoreMixin):
    """Trade CRUD with local file + Google Drive sync.

    Usage:
        store = FallenHeroStore()
        store.initialize()           # Auth Drive, download, fallback to local
        trades = store.load_trades() # From cache, zero network
        store.add_trade({...})       # Validate, save local + Drive
        store.maybe_sync()           # Re-download from Drive if stale
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

    def _lock_path(self) -> Path:
        """Resolved at call time, not import time.

        `_save_local` and `_read_local` read the module global, so a
        test that redirects the store by patching LOCAL_TRADES_FILE
        must move the lock with it. A class attribute would freeze the
        lock in the real logs/ directory while the data went to
        tmp_path -- locked, but not against the writer that matters.
        """
        return LOCK_FILE

    def _data_path(self) -> Path:
        """Same call-time resolution as _lock_path, for the same reason."""
        return LOCAL_TRADES_FILE

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
            "FallenHeroStore initialized: %d trades (%d open), drive=%s",
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
            file_name = drive_cfg.get('file_name', 'fallen_hero_trades.json')
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
        """Download from Drive, then merge the result under the store lock.

        The download is a NETWORK call and stays OUTSIDE the lock. Holding a
        cross-process mutex across an HTTP round-trip would stall the other
        process's whole cycle -- and this runs every poll on the live monitor.

        Merge strategy (per trade ID, higher version wins):
          - Protects against lost trades when a prior Drive upload failed
          - Picks up new trades added from other machines
          - If merge differs from Drive, re-uploads to Drive
        """
        if not self._drive_file_id:
            logger.info("No file on Drive yet, loading local")
            self._load_local()
            return
        try:
            drive_trades = drive_store.download_json(
                self._drive_service, self._drive_file_id
            )
            diverged = False
            with self._mutate(drive=False):
                # _mutate has already refreshed self._trades from disk, so the
                # old `base = self._trades if self._trades else _read_local()`
                # special case is gone: disk is always the base now.
                self._trades = self._merge_trades(self._trades, drive_trades)
                drive_versions = {t['id']: t.get('version', 0) for t in drive_trades}
                merged_versions = {t['id']: t.get('version', 0) for t in self._trades}
                diverged = drive_versions != merged_versions
            self._last_sync_time = time.time()
            if diverged:
                logger.info("Merge diverged from Drive, re-uploading")
                self._upload_to_drive()
        except LockTimeout as e:
            # Another process holds the store. The cache stays as it is and the
            # next poll retries -- a missed refresh is not a reason to abandon
            # the poll, which is what raising here would do.
            logger.warning("Drive merge skipped, store busy: %s", e)
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
            file_name = drive_cfg.get('file_name', 'fallen_hero_trades.json')
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
            with self._read_lock():
                with open(LOCAL_TRADES_FILE, encoding='utf-8') as f:
                    data = json.load(f)
            if not isinstance(data, list):
                raise ValueError(f"Expected list, got {type(data).__name__}")
            return data
        except (json.JSONDecodeError, ValueError) as e:
            # File is corrupt -- back it up so we can investigate, start fresh
            backup = LOCAL_TRADES_FILE.with_suffix(
                f'.corrupt.{int(time.time())}.json'
            )
            try:
                LOCAL_TRADES_FILE.rename(backup)
            except OSError:
                pass  # Can't rename -- at least don't crash
            logger.critical(
                "Trade file CORRUPT (%s). Backed up to %s. Starting empty!",
                e, backup
            )
            # B7: a log line is not an alert. The monitor turns this marker
            # into a Telegram BEFORE it concludes "all trades closed" and
            # stops watching every open position.
            self._flag_corruption(str(e), backup)
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
        """Merge two trade lists. Per trade ID, higher version wins."""
        by_id: dict = {}

        for t in base:
            by_id[t['id']] = t

        for t in incoming:
            tid = t['id']
            if tid not in by_id:
                by_id[tid] = t
            elif t.get('version', 0) > by_id[tid].get('version', 0):
                by_id[tid] = t

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
        on both Windows (NTFS) and Linux (same filesystem).
        """
        LOG_DIR.mkdir(exist_ok=True)
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(LOG_DIR), suffix='.tmp', prefix='fh_trades_'
            )
            with os.fdopen(fd, 'w') as f:
                fd = None  # os.fdopen takes ownership of fd
                json.dump(self._trades, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(LOCAL_TRADES_FILE))
            tmp_path = None  # replaced successfully, don't clean up
        except Exception:
            if fd is not None:
                os.close(fd)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise

    def _migrate_trades(self):
        """Backfill version, lot_size, lots, downside_risk fields on existing trades."""
        with self._mutate():
            changed = False
            for t in self._trades:
                if 'version' not in t:
                    t['version'] = 1
                    changed = True
                if 'lot_size' not in t:
                    t['lot_size'] = t['quantity']
                    t['lots'] = 1
                    changed = True
                if 'lots' not in t:
                    t['lots'] = t['quantity'] // t['lot_size']
                    changed = True
                if 'downside_risk' not in t:
                    psw = t.get('put_spread_width', 0)
                    tc = t.get('total_credit', 0)
                    t['downside_risk'] = max(0, psw - tc)
                    changed = True

            if changed:
                logger.info("Migrated %d trades (added version/lot_size/lots/downside_risk)",
                            len(self._trades))

    # -- Read Operations (from cache, zero network) ------------------------

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
        """Reset a 'closing' trade back to 'open' for re-monitoring."""
        with self._mutate():
            for t in self._trades:
                if t['id'] == trade_id and t.get('status') == 'closing':
                    t['status'] = 'open'
                    t['version'] = t.get('version', 0) + 1
                    t.pop('close_reason', None)
                    t.pop('close_started', None)
                    self._sync_locked = False
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
        """Allocate the next trade ID.

        Delegates to the mixin, which also consults a monotonic
        high-water sidecar — `max(live) + 1` reissues ids 1, 2, 3
        after a quarantine empties the book, and a reissued id is not
        a cosmetic problem: `_merge_trades` resolves by id, so the
        Drive copy of the original trade and the new one become the
        same record and the higher version silently wins.

        Not a pure read: it advances the sidecar. Its only caller is
        `add_trade`, inside the lock, which is where allocation has to
        happen anyway.
        """
        return self.allocate_id()

    # -- Write Operations (local first, then Drive) ------------------------

    def add_trade(self, trade_dict: dict) -> dict:
        """Validate, assign ID, save local + Drive. Returns the trade.

        Validations:
          1. All 22 required fields present
          2. lot_size: positive, quantity is a multiple
          3. put_spread_credit = entry_short_put_price - entry_long_put_price
          4. total_credit = put_spread_credit + call_credit
          5. breakeven = short_call_strike + total_credit
          6. put_spread_width = short_put_strike - long_put_strike
          7. Strike ordering: long_put < short_put < short_call
          8. Warning if downside_risk > 0
          9. Warning if short put or short call is ITM
         10. SL must be below breakeven

        Raises:
            ValueError: If required fields missing or validation fails.
        """
        # 1. Required fields
        missing = [f for f in REQUIRED_FIELDS if f not in trade_dict]
        if missing:
            raise ValueError(f"Missing required fields: {missing}")

        # 2. Lot size validation
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
            trade_dict['lot_size'] = quantity
            trade_dict['lots'] = 1

        # 3. put_spread_credit consistency
        expected_psc = round(
            trade_dict['entry_short_put_price'] - trade_dict['entry_long_put_price'], 2
        )
        actual_psc = round(trade_dict['put_spread_credit'], 2)
        if abs(expected_psc - actual_psc) > 0.05:
            raise ValueError(
                f"put_spread_credit mismatch: "
                f"short_put({trade_dict['entry_short_put_price']}) - "
                f"long_put({trade_dict['entry_long_put_price']}) = {expected_psc}, "
                f"but got {actual_psc}"
            )

        # 4. total_credit consistency
        expected_tc = round(actual_psc + trade_dict['call_credit'], 2)
        actual_tc = round(trade_dict['total_credit'], 2)
        if abs(expected_tc - actual_tc) > 0.05:
            raise ValueError(
                f"total_credit mismatch: "
                f"put_spread_credit({actual_psc}) + call_credit({trade_dict['call_credit']}) "
                f"= {expected_tc}, but got {actual_tc}"
            )

        # 5. breakeven consistency
        expected_be = round(trade_dict['short_call_strike'] + actual_tc, 2)
        actual_be = round(trade_dict['breakeven'], 2)
        if abs(expected_be - actual_be) > 0.05:
            raise ValueError(
                f"breakeven mismatch: "
                f"short_call_strike({trade_dict['short_call_strike']}) + "
                f"total_credit({actual_tc}) = {expected_be}, but got {actual_be}"
            )

        # 6. put_spread_width consistency
        expected_psw = round(
            trade_dict['short_put_strike'] - trade_dict['long_put_strike'], 2
        )
        actual_psw = round(trade_dict['put_spread_width'], 2)
        if abs(expected_psw - actual_psw) > 0.05:
            raise ValueError(
                f"put_spread_width mismatch: "
                f"short_put_strike({trade_dict['short_put_strike']}) - "
                f"long_put_strike({trade_dict['long_put_strike']}) = {expected_psw}, "
                f"but got {actual_psw}"
            )

        # 7. Strike ordering: long_put < short_put < short_call
        lp = trade_dict['long_put_strike']
        sp = trade_dict['short_put_strike']
        sc = trade_dict['short_call_strike']
        if not (lp < sp):
            raise ValueError(
                f"Invalid strike order: long_put({lp}) must be < short_put({sp})"
            )
        if not (sp < sc):
            raise ValueError(
                f"Invalid strike order: short_put({sp}) must be < short_call({sc})"
            )

        # Compute downside_risk
        downside_risk = max(0, round(actual_psw - actual_tc, 2))
        trade_dict['downside_risk'] = downside_risk

        # 8. Warning if downside_risk > 0
        if downside_risk > 0:
            logger.warning(
                "DOWNSIDE RISK: total_credit (%.2f) < put_spread_width (%.2f). "
                "Downside risk = %.2f per share",
                actual_tc, actual_psw, downside_risk
            )

        # 9. Warning if short put or short call is ITM
        entry_spot = trade_dict['entry_spot']
        if sp > entry_spot:
            logger.warning(
                "Short put strike %.2f is ITM (spot = %.2f)", sp, entry_spot
            )
        if sc < entry_spot:
            logger.warning(
                "Short call strike %.2f is ITM (spot = %.2f)", sc, entry_spot
            )

        # 10. SL must be below breakeven (FH risk is upside, SL triggers above)
        sl = trade_dict['sl_spot']
        be = trade_dict['breakeven']
        if sl >= be:
            raise ValueError(
                f"sl_spot ({sl}) must be below breakeven ({be}). "
                f"Fallen Hero SL triggers on upside move toward breakeven."
            )

        # ID allocation is a read-check-write, so it belongs INSIDE the lock:
        # two processes racing here hand the same id to two different trades,
        # after which every lookup by id is ambiguous and the monitor may close
        # the wrong one.
        with self._mutate():
            trade_dict['id'] = self.next_trade_id()
            trade_dict['version'] = 1
            # Broker account this trade belongs to. The real values are
            # account numbers, so they are named in config, never here — this
            # repo is PUBLIC.
            trade_dict.setdefault('account_id', None)
            trade_dict.setdefault('status', 'open')
            trade_dict.setdefault('exit', None)
            trade_dict.setdefault('notes', '')

            self._trades.append(trade_dict)

        logger.info(
            "Added trade #%d: %s %s/%s PE + %s CE, credit=%.2f",
            trade_dict['id'], trade_dict['stock'],
            trade_dict['long_put_symbol'], trade_dict['short_put_symbol'],
            trade_dict['short_call_symbol'], trade_dict['total_credit']
        )
        return trade_dict

    def update_trade_exit(self, trade_id: int, exit_data: dict):
        """Mark a trade as closed with exit details. Saves local + Drive."""
        with self._mutate():
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
            logger.info("Trade #%d closed: %s", trade_id, exit_data.get('exit_reason', ''))

    def begin_close(self, trade_id: int, reason: str) -> bool:
        """Acquire close-lock on a trade. Returns True if lock acquired.

        Sets status to 'closing' with version bump. Saves to local + Drive
        immediately so other machines see the lock before placing orders.
        Prevents the 2x close-order bug from concurrent processes.

        Returns False if trade is not 'open' (already closing/closed).
        """
        with self._mutate():
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
                    logger.info(
                        "Trade #%d close-lock acquired: %s", trade_id, reason
                    )
                    return True
            logger.error("Trade #%d not found for begin_close", trade_id)
            return False

    def set_trade_status(self, trade_id: int, status: str, **extra_fields):
        """Update trade status and optional extra fields. Saves local + Drive."""
        with self._mutate():
            for t in self._trades:
                if t['id'] == trade_id:
                    t['status'] = status
                    t['version'] = t.get('version', 0) + 1
                    for k, v in extra_fields.items():
                        t[k] = v
                    self._sync_locked = False
                    logger.info("Trade #%d status -> %s", trade_id, status)
                    return
            logger.error("Trade #%d not found for set_trade_status", trade_id)

    def update_trade_fields(self, trade_id: int, **fields):
        """Update arbitrary fields on a trade. Saves local only (no Drive upload).

        Used for lightweight state updates like trailing SL that change every
        few seconds -- Drive sync happens on the normal maybe_sync() cycle.
        """
        with self._mutate(drive=False):
            for t in self._trades:
                if t['id'] == trade_id:
                    for k, v in fields.items():
                        t[k] = v
                    return True
            return False

    # -- Sync --------------------------------------------------------------

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

    # -- Display -----------------------------------------------------------

    def list_trades(self):
        """Print formatted table of all trades."""
        trades = self._trades
        if not trades:
            print("No trades found.")
            return

        print(f"\n{'ID':>3}  {'Stock':<12} {'Status':<8} {'Put Spread':<16} "
              f"{'Short Call':>10}  {'Expiry':<12} {'Credit':>8} {'Lots':>8} "
              f"{'SL Spot':>8} {'Breakeven':>10}")
        print("-" * 115)

        for t in trades:
            # Extract strikes for display
            lp_strike = t.get('long_put_strike', '?')
            sp_strike = t.get('short_put_strike', '?')
            sc_strike = t.get('short_call_strike', '?')
            put_spread_str = f"{lp_strike}/{sp_strike} PE"
            call_str = f"{sc_strike} CE"

            lots = t.get('lots', '?')
            lot_size = t.get('lot_size', '?')
            lots_str = f"{lots}x{lot_size}"

            print(f"{t['id']:>3}  {t['stock']:<12} {t['status']:<8} "
                  f"{put_spread_str:<16} {call_str:>10}  {t['expiry']:<12} "
                  f"{t['total_credit']:>8.2f} {lots_str:>8} "
                  f"{t['sl_spot']:>8.1f} {t['breakeven']:>10.2f}")

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


# -- Backward-compatible free functions (delegate to singleton) ------------

_store: Optional[FallenHeroStore] = None


def get_store() -> FallenHeroStore:
    """Get or create the singleton FallenHeroStore."""
    global _store
    if _store is None:
        _store = FallenHeroStore()
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
