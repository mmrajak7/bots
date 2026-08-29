"""
Bear Put Spread Trade Store — CRUD operations with Google Drive sync.

Mirrors BCS trade_store.py architecture (2-leg debit spread).
Key difference: BPS is bearish (long higher-strike PE, short lower-strike PE).
SL triggers when spot RISES (>= sl_spot), TP triggers when spot DROPS (<= target_spot).

Architecture:
  - In-memory cache for all reads (zero network on poll cycles)
  - Local file written FIRST on every write (data safety)
  - Drive sync: download on startup, upload on writes
  - Periodic re-sync from Drive (pick up changes from other machines)
  - If Drive fails at any point: fall back to local, log warning
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
from common.option_symbols import check_leg_types
from common import layered_config

logger = logging.getLogger(__name__)

# ── Paths ───────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()       # Helper/bear_put/
PROJECT_ROOT = SCRIPT_DIR.parent                   # Helper/
LOG_DIR = PROJECT_ROOT / 'logs'
LOCAL_TRADES_FILE = LOG_DIR / 'bear_put_trades.json'
#: One lock PER BOOK. The monitor writes all three every
#: poll; a shared lock would serialise them for no reason.
LOCK_FILE = LOG_DIR / 'bear_put_trades.lock'
CONFIG_FILE = PROJECT_ROOT / 'config' / 'bear_put_config.json'

# ── Required fields for add_trade validation ────────────────────────────────
REQUIRED_FIELDS = [
    'stock', 'long_symbol', 'short_symbol', 'spot_symbol', 'exchange',
    'quantity', 'entry_long_price', 'entry_short_price', 'net_debit',
    'spread_width', 'target_spot', 'sl_spot', 'sl_spread', 'expiry',
]


#: What this book's legs must be. Checked on every `add_trade`, because
#: `bcs/spread_monitor.py` picks SL_SPOT and TP DIRECTION from which store a
#: record came out of — so a record filed in the wrong book has its stops
#: inverted, and on the first poll of a healthy position both `spot <= sl_spot`
#: and `spot >= target` can be true at once. See common/option_symbols.py.
LEG_TYPES = {'long_symbol': 'PE', 'short_symbol': 'PE'}


def _load_config() -> dict:
    """Load BPS config from config/bear_put_config.json."""
    # TWO LAYERS: config/bear_put_config.defaults.json (tracked, secret-free)
    # under config/bear_put_config.json (untracked, secrets). See
    # common/layered_config.py — including why the overlay wins.
    return layered_config.load('bear_put_config')


def _resolve_credentials_path(config: dict) -> Optional[Path]:
    """Resolve credentials path based on platform and env var override."""
    env_path = os.environ.get('BPS_GOOGLE_CREDS')
    if env_path:
        return Path(env_path)

    drive_cfg = config.get('google_drive', {})
    if platform.system() == 'Windows':
        path_str = drive_cfg.get('credentials_path_windows')
    else:
        path_str = drive_cfg.get('credentials_path_linux')

    return Path(path_str) if path_str else None


class BearPutStore(LockedStoreMixin):
    """Bear Put Spread trade CRUD with local file + Google Drive sync.

    Usage:
        store = BearPutStore()
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
            "BearPutStore initialized: %d trades (%d open), drive=%s",
            len(self._trades), open_count,
            'enabled' if self._drive_enabled else 'disabled'
        )

    def _init_drive(self, drive_cfg: dict):
        """Authenticate with Google Drive."""
        creds_path = _resolve_credentials_path(self._config)
        if not creds_path or not creds_path.exists():
            logger.warning(
                "Drive credentials not found at %s, running local-only", creds_path
            )
            return

        try:
            self._drive_service = drive_store.get_drive_service(creds_path)
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('file_name', 'bear_put_trades.json')
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
        """Upload current trades to Drive."""
        if not self._drive_enabled:
            return

        try:
            drive_cfg = self._config.get('google_drive', {})
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('file_name', 'bear_put_trades.json')
            self._drive_file_id = drive_store.upload_json(
                self._drive_service, folder_id, file_name,
                self._trades, self._drive_file_id
            )
            self._last_sync_time = time.time()
        except Exception as e:
            logger.error("Drive upload failed: %s. Local file is safe.", e)

    def _read_local(self) -> list:
        """Read trades from local JSON file (corruption-safe)."""
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
            backup = LOCAL_TRADES_FILE.with_suffix(
                f'.corrupt.{int(time.time())}.json'
            )
            try:
                LOCAL_TRADES_FILE.rename(backup)
            except OSError:
                pass
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
            logger.info("Loaded %d BPS trades from local file", len(self._trades))
        else:
            logger.info("No local BPS trades file found, starting empty")

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
        """Write trades to local JSON file (atomic via temp + replace)."""
        LOG_DIR.mkdir(exist_ok=True)
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(LOG_DIR), suffix='.tmp', prefix='bps_trades_'
            )
            with os.fdopen(fd, 'w') as f:
                fd = None
                json.dump(self._trades, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(LOCAL_TRADES_FILE))
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

    def _migrate_trades(self):
        """Backfill version, lot_size, lots fields on existing trades."""
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
            if changed:
                logger.info("Migrated %d BPS trades (added version/lot_size/lots)", len(self._trades))

    # ── Read Operations (from cache, zero network) ──────────────────────

    def load_trades(self) -> list:
        return self._trades

    def get_open_trades(self) -> list:
        return [t for t in self._trades if t.get('status') == 'open']

    def get_closing_trades(self) -> list:
        return [t for t in self._trades if t.get('status') == 'closing']

    def recover_closing_trade(self, trade_id: int):
        with self._mutate():
            for t in self._trades:
                if t['id'] == trade_id and t.get('status') == 'closing':
                    t['status'] = 'open'
                    t['version'] = t.get('version', 0) + 1
                    t.pop('close_reason', None)
                    t.pop('close_started', None)
                    self._sync_locked = False
                    logger.warning("BPS trade #%d recovered from 'closing' -> 'open'", trade_id)
                    return True
            return False

    def find_open_trade(self, stock: str, trade_id: Optional[int] = None) -> Optional[dict]:
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

        Delegates to the mixin, which also consults a monotonic high-water
        sidecar — `max(live) + 1` reissues ids 1, 2, 3 after a quarantine
        empties the book, and a reissued id is not a cosmetic problem:
        `_merge_trades` resolves by id, so the Drive copy of the original trade
        and the new one become the same record and the higher version silently
        wins.

        Not a pure read: it advances the sidecar. Its only caller is
        `add_trade`, inside the lock, which is where allocation belongs anyway.
        """
        return self.allocate_id()

    # ── Write Operations (local first, then Drive) ──────────────────────

    def add_trade(self, trade_dict: dict) -> dict:
        """Validate, assign ID, save local + Drive. Returns the trade.

        BPS-specific validations:
          - sl_spot should be above entry_spot (stock rising = bad for BPS)
          - target_spot should be below entry_spot (stock dropping = good for BPS)
        """
        missing = [f for f in REQUIRED_FIELDS if f not in trade_dict]
        if missing:
            raise ValueError(f"Missing required fields: {missing}")

        # Refuse a record whose legs contradict this book. Cheap here, and the
        # only other place it could be caught is the monitor — after the trade
        # is already open and being managed with inverted stops.
        wrong = check_leg_types(trade_dict, LEG_TYPES)
        if wrong:
            raise ValueError(
                "Leg types do not match this book: " + "; ".join(wrong)
                + ". Save it through the store for its own structure.")

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
            trade_dict['lot_size'] = quantity
            trade_dict['lots'] = 1

        # BPS directional validations
        entry_spot = trade_dict.get('entry_spot')
        if entry_spot:
            if trade_dict['sl_spot'] < entry_spot:
                logger.warning(
                    "BPS sl_spot (%.1f) is BELOW entry_spot (%.1f). "
                    "BPS risk is UPSIDE — SL should be above entry.",
                    trade_dict['sl_spot'], entry_spot
                )
            if trade_dict['target_spot'] > entry_spot:
                logger.warning(
                    "BPS target_spot (%.1f) is ABOVE entry_spot (%.1f). "
                    "BPS profits from DECLINE — target should be below entry.",
                    trade_dict['target_spot'], entry_spot
                )

        # net_debit cross-check
        expected_debit = round(trade_dict['entry_long_price'] - trade_dict['entry_short_price'], 2)
        if abs(trade_dict['net_debit'] - expected_debit) > 0.10:
            logger.warning(
                "BPS net_debit (%.2f) doesn't match long-short (%.2f-%.2f=%.2f)",
                trade_dict['net_debit'],
                trade_dict['entry_long_price'], trade_dict['entry_short_price'],
                expected_debit
            )

        # ID allocation is a read-check-write, so it belongs INSIDE the lock:
        # two processes racing here hand the same id to two different trades,
        # after which every lookup by id is ambiguous and the monitor may close
        # the wrong one.
        with self._mutate():
            trade_dict['id'] = self.next_trade_id()
            trade_dict['version'] = 1
            trade_dict.setdefault('account_id', None)
            trade_dict.setdefault('status', 'open')
            trade_dict.setdefault('exit', None)

            self._trades.append(trade_dict)

        logger.info("Added BPS trade #%d: %s %s/%s",
                     trade_dict['id'], trade_dict['stock'],
                     trade_dict['long_symbol'], trade_dict['short_symbol'])
        return trade_dict

    def update_trade_exit(self, trade_id: int, exit_data: dict):
        """Mark a trade as closed with exit details."""
        with self._mutate():
            found = False
            for t in self._trades:
                if t['id'] == trade_id:
                    t['status'] = 'closed'
                    t['exit'] = exit_data
                    t['version'] = t.get('version', 0) + 1
                    found = True
                    break

            self._sync_locked = False

            if not found:
                logger.error("BPS trade #%d not found for exit update", trade_id)
                return
            logger.info("BPS trade #%d closed: %s", trade_id, exit_data.get('exit_reason', ''))

    def begin_close(self, trade_id: int, reason: str) -> bool:
        """Acquire close-lock on a trade."""
        with self._mutate():
            for t in self._trades:
                if t['id'] == trade_id:
                    if t['status'] != 'open':
                        logger.warning(
                            "BPS trade #%d status is '%s', cannot begin close",
                            trade_id, t['status']
                        )
                        return False
                    t['status'] = 'closing'
                    t['close_reason'] = reason
                    t['close_started'] = datetime.now().isoformat()
                    t['version'] = t.get('version', 0) + 1
                    self._sync_locked = True
                    logger.info("BPS trade #%d close-lock acquired: %s", trade_id, reason)
                    return True
            logger.error("BPS trade #%d not found for begin_close", trade_id)
            return False

    def begin_recovery(self, trade_id: int, reason: str) -> bool:
        """M14 - take the close-lock on a FROZEN record so recovery can finish it.

        The ONE door out of `partial_close`, and a separate door on purpose.

        **Do not overload `begin_close`.** Its `status == 'open'` check is the
        concurrency lock that stops two processes both closing the same trade -
        the 2x-order shape that cost real money in Feb 2026. Widening it to
        also accept `partial_close` would weaken that guarantee for every
        ordinary close in order to serve a rare one.

        Refuses every other state, and each refusal earns its own reasoning:
        `'open'` was never frozen and belongs to `begin_close`; `closing`
        means an attempt is already in flight, and a second would be the 2x
        order again; `'closed'` is terminal, and ordering on a booked
        record is what S3 exists to forbid.

        Returns False rather than raising - "somebody else got there first" is
        an ordinary answer on a shared store, and the caller branches on it.
        """
        with self._mutate():
            for t in self._trades:
                if t['id'] != trade_id:
                    continue
                if t['status'] != 'partial_close':
                    logger.warning(
                        "BPS trade #%d status is '%s', cannot begin recovery",
                        trade_id, t['status'])
                    return False
                t['status'] = 'closing'
                t['close_reason'] = reason
                t['close_started'] = datetime.now().isoformat()
                t['version'] = t.get('version', 0) + 1
                self._sync_locked = True
                logger.info("BPS trade #%d recovery-lock acquired: %s",
                            trade_id, reason)
                return True
            logger.error("BPS trade #%d not found for begin_recovery", trade_id)
            return False

    def get_frozen_trades(self) -> list:
        """Records stuck at `partial_close` - live legs, nothing monitoring them.

        This is the state a close lands in when a leg failed AFTER orders went
        out. It drops out of the open book, so before M14 nothing retried it,
        nothing re-alerted, and one Telegram at freeze time was the entire
        lifecycle of a position that may be live at the broker with its stops
        dead. That is the unwatched-position failure that has cost this account
        real money twice.

        Read-only. No caller may treat these as open positions.
        """
        return [t for t in self._trades if t.get('status') == 'partial_close']

    def get_residue_trades(self) -> list:
        """S3 - records BOOKED CLOSED that still show a live leg at the broker.

        `reconcile_after_close` reads the broker's own view after a close
        reports success. When it finds a leg that is not flat, the record is
        already `closed`: it is out of the open book, out of
        `get_frozen_trades()`, and out of every sweep there is. Before this
        method the entire lifecycle of that fact was ONE Telegram — the same
        invisible-position shape M14 exists to end, one door over.

        Deliberately NOT the frozen list. A frozen record has a watcher (the
        recovery sweep) and a nag of its own; a second one for the same
        position would be noise. This names only the records nothing else can
        see, which is why the status filter is part of the query rather than
        left to the caller.

        Read-only, and terminal: no caller may place an order on the strength
        of this list. The record is closed — there is no close lock to take,
        no stop to re-arm, and the residue may be a leg the owner is holding
        on purpose. Escalate, never act.
        """
        return [t for t in self._trades
                if t.get('status') == 'closed'
                and (t.get('reconcile_residue') or {}).get('state') == 'open']



    def set_trade_status(self, trade_id: int, status: str, **extra_fields):
        """Update trade status and optional extra fields."""
        with self._mutate():
            for t in self._trades:
                if t['id'] == trade_id:
                    t['status'] = status
                    t['version'] = t.get('version', 0) + 1
                    for k, v in extra_fields.items():
                        t[k] = v
                    self._sync_locked = False
                    logger.info("BPS trade #%d status -> %s", trade_id, status)
                    return
            logger.error("BPS trade #%d not found for set_trade_status", trade_id)

    def update_trade_fields(self, trade_id: int, **fields):
        """Update arbitrary fields on a trade. Local only (no Drive upload)."""
        with self._mutate(drive=False):
            for t in self._trades:
                if t['id'] == trade_id:
                    for k, v in fields.items():
                        t[k] = v
                    return True
            return False

    # ── Sync ────────────────────────────────────────────────────────────

    def maybe_sync(self, force: bool = False):
        """Re-sync from Drive if stale."""
        if not self._drive_enabled:
            return
        if self._sync_locked:
            return
        elapsed = time.time() - self._last_sync_time
        if force or elapsed >= self._sync_interval:
            self._sync_from_drive()

    # ── Display ─────────────────────────────────────────────────────────

    def list_trades(self):
        """Print formatted table of all BPS trades."""
        trades = self._trades
        if not trades:
            print("No bear put spread trades found.")
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
            strikes_str = f"{long_strike}/{short_strike} PE"

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

_store: Optional[BearPutStore] = None


def get_store() -> BearPutStore:
    """Get or create the singleton BearPutStore."""
    global _store
    if _store is None:
        _store = BearPutStore()
        _store.initialize()
    return _store


def load_trades() -> list:
    return get_store().load_trades()


def add_trade(trade_dict: dict) -> dict:
    return get_store().add_trade(trade_dict)


def update_trade_exit(trade_id: int, exit_data: dict):
    get_store().update_trade_exit(trade_id, exit_data)
