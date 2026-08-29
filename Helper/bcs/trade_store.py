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
from common import store_contract
from common.locked_store import LockTimeout, LockedStoreMixin
from common.option_symbols import check_leg_types
from common import layered_config

logger = logging.getLogger(__name__)

# ── Paths ───────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()       # Helper/bcs/
PROJECT_ROOT = SCRIPT_DIR.parent                   # Helper/
LOG_DIR = PROJECT_ROOT / 'logs'
LOCAL_TRADES_FILE = LOG_DIR / 'bcs_trades.json'
#: One lock PER BOOK. The monitor writes all three every
#: poll; a shared lock would serialise them for no reason.
LOCK_FILE = LOG_DIR / 'bcs_trades.lock'
CONFIG_FILE = PROJECT_ROOT / 'config' / 'bcs_config.json'

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
LEG_TYPES = {'long_symbol': 'CE', 'short_symbol': 'CE'}


#: The per-leg exit prices a BCS close records. An explicit ``None`` in one of
#: these is an ACKNOWLEDGED unknown (the leg was already flat, or its fill could
#: not be recovered); a MISSING key is a hand-written exit with no per-leg
#: detail and is left alone. Mirrors `fallen_hero.trade_store.EXIT_FILL_FIELDS`
#: so the two books share one vocabulary rather than inventing two.
EXIT_FILL_FIELDS = ('short_fill', 'long_fill')


def exit_is_approximate(trade: dict) -> bool:
    """Is this closed trade's P&L known to be an approximation?

    The BCS twin of `fallen_hero.trade_store.exit_is_approximate`, and it reads
    BOTH the top-level marker and the one inside `exit`: the top-level copy is
    for readers that scan trade dicts (`list_trades`, `journal_report`, the
    digest), and records closed before the marker existed carry neither.

    Absence means EXACT, deliberately. ~450 historical records predate this;
    defaulting them to approximate would print a caveat on every line, which is
    how a caveat stops being read.
    """
    if trade.get('exit_approximate') is True:
        return True
    ex = trade.get('exit') or {}
    return ex.get('pnl_approximate') is True


def bound_bcs_exit(trade: dict, exit_data: dict) -> dict:
    """Bound a vertical-spread exit to what the structure can arithmetically do.

    **N14.** `bcs/spread_monitor.py` logged "P&L is approximate (long fill
    only)" when one leg was found already flat — and then persisted a record
    that read as exact. The missing leg is counted at 0.00, so the figure is
    not merely uncertain, it is WRONG IN A KNOWN DIRECTION, and nothing
    downstream (`pnl_net`, the digest, the journal report) could tell.

    Two things, and only two. It never invents a price and never computes a
    P&L the caller did not supply — that would be the `0.0`-seed defect
    (`feedback_a_default_that_looks_like_a_value`) with better manners.

    **1. MARK approximate** when a leg fill is explicitly `None`.

    **2. CLAMP `exit_spread` to ``[0, width]``** and re-derive the P&L from it.
    A vertical's exit value cannot be negative (expiry is always available and
    costs nothing) nor exceed its width; both bounds are MATHEMATICAL and both
    are reachable, which per CLAUDE.md's "Valuation bounds — clamp the
    arithmetic, refuse the estimate" makes them clamps rather than estimates to
    defer on. This is the write boundary on purpose: a hand-built `exit_data`
    gets the same treatment as a machine-built one, so the guard does not
    depend on which code produced the number. It is the same bound PIIND #50
    breached (`-112.4%` on a `-100%`-capped structure) for want of one existing
    anywhere.

    LOUD when the clamp binds. A clamp that fires silently turns a visible bug
    into a plausible number, which is the failure it exists to catch rather
    than a fix for it.

    Returns a NEW dict; the caller's is never mutated.
    """
    out = dict(exit_data or {})

    unpriced = [f for f in EXIT_FILL_FIELDS if f in out and out[f] is None]
    if unpriced:
        out['pnl_approximate'] = True
        out.setdefault('unpriced_legs', unpriced)

    try:
        width = float(trade['spread_width'])
        exit_net = float(out['exit_spread'])
    except (KeyError, TypeError, ValueError):
        # No width on the record, or no exit value in the exit. Nothing to
        # bound against — and refusing the whole write over a missing optional
        # field would strand a close already made at the broker.
        return out
    if width <= 0:
        return out

    bounded = min(max(exit_net, 0.0), width)
    if bounded == exit_net:
        return out

    out['exit_spread'] = bounded
    out['pnl_clamped_from'] = exit_net
    out['pnl_approximate'] = True

    # Re-derive rather than scale: for a vertical the P&L is exactly
    # `exit_spread - net_debit`, so the clamped value has one correct answer
    # and guessing a ratio would be inventing a second.
    try:
        debit = float(trade['net_debit'])
    except (KeyError, TypeError, ValueError):
        return out
    out['pnl_per_share'] = bounded - debit
    qty = trade.get('quantity')
    try:
        out['total_pnl'] = out['pnl_per_share'] * float(qty) if qty else None
    except (TypeError, ValueError):
        out['total_pnl'] = None

    logger.error(
        'BCS #%s %s: exit_spread %.2f is outside [0, %.2f] — arithmetically '
        'impossible for a vertical. Clamped to %.2f and marked approximate. '
        'A leg was mispriced or counted at 0.00; read the exit fills.',
        trade.get('id'), trade.get('stock'), exit_net, width, bounded)
    return out


def _load_config() -> dict:
    """Load BCS config from config/bcs_config.json."""
    # TWO LAYERS: config/bcs_config.defaults.json (tracked, secret-free)
    # under config/bcs_config.json (untracked, secrets). See
    # common/layered_config.py — including why the overlay wins.
    return layered_config.load('bcs_config')


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


class TradeStore(LockedStoreMixin):
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
            with self._read_lock():
                with open(LOCAL_TRADES_FILE, encoding='utf-8') as f:
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
        with self._mutate():
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
        with self._mutate():
            for t in self._trades:
                # The table, not a literal. See `common/store_contract.py`.
                if t['id'] == trade_id and store_contract.allows(
                        store_contract.RECOVER_CLOSING,
                        t.get('status')):
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
            # No lot_size provided — treat quantity as 1 lot
            trade_dict['lot_size'] = quantity
            trade_dict['lots'] = 1

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

            self._trades.append(trade_dict)

        logger.info("Added trade #%d: %s %s/%s",
                     trade_dict['id'], trade_dict['stock'],
                     trade_dict['long_symbol'], trade_dict['short_symbol'])
        return trade_dict

    def update_trade_exit(self, trade_id: int, exit_data: dict):
        """Mark a trade as closed with exit details. Saves local + Drive."""
        with self._mutate():
            found = False
            for t in self._trades:
                if t['id'] == trade_id:
                    # THE CONTRACT, enforced rather than described.
                    # `common/store_contract.py`. Until 2026-08-30 this method
                    # had no status check at all: it would stamp 'closed' onto
                    # a record that was already closed, or onto one FROZEN at
                    # `partial_close` with legs live at the broker. The only
                    # thing between that and a real order was `begin_close`.
                    # The divergence from `ZebraStore` was recorded in a test
                    # named after it and left standing, which is what a
                    # specification nothing consults always becomes.
                    #
                    # RAISES rather than returning: every caller has ALREADY
                    # placed the closing orders, so a silent no-op would leave
                    # the position flat at the broker and open in the book. A
                    # refusal here is a bug upstream and has to be loud.
                    if not store_contract.allows(
                            store_contract.UPDATE_TRADE_EXIT, t['status']):
                        raise ValueError(store_contract.refusal(
                            store_contract.UPDATE_TRADE_EXIT, t['status'],
                            trade_id))
                    exit_data = bound_bcs_exit(t, exit_data)
                    t['status'] = 'closed'
                    t['exit'] = exit_data
                    # Surfaced at the TOP LEVEL as well as inside `exit`, for
                    # the same reason the FH store does it: every reader of
                    # this book scans trade dicts, and a marker findable only
                    # by opening a nested dict is one most readers will miss.
                    # Absent means exact; the key is never written False.
                    if exit_data.get('pnl_approximate'):
                        t['exit_approximate'] = True
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
                    # The table, not a literal. See `common/store_contract.py`
                    # for why the specification moved out of the test that held it.
                    if not store_contract.allows(
                            store_contract.BEGIN_CLOSE, t['status']):
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
                # The table, not a literal. `begin_recovery` is the
                # exact inverse of `begin_close`, and stating that as
                # data is the point of it being its own verb.
                if not store_contract.allows(
                        store_contract.BEGIN_RECOVERY, t['status']):
                    logger.warning(
                        "Trade #%d status is '%s', cannot begin recovery",
                        trade_id, t['status'])
                    return False
                t['status'] = 'closing'
                t['close_reason'] = reason
                t['close_started'] = datetime.now().isoformat()
                t['version'] = t.get('version', 0) + 1
                self._sync_locked = True
                logger.info("Trade #%d recovery-lock acquired: %s",
                            trade_id, reason)
                return True
            logger.error("Trade #%d not found for begin_recovery", trade_id)
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

    def get_entry_residue_trades(self) -> list:
        """Records carrying an ENTRY residue — a leg an entry left behind.

        The entry-side twin of `get_residue_trades`, and it exists for the
        same reason: `bcs/entry_executor.py` never unwinds an orphan leg (a
        corrective order through the book that just failed to fill is how a
        Feb-2026 stop became a four-fill loss), so it REPORTS one and stops.
        Until this method existed "reports" meant one Telegram: the orphan was
        in no store, so the frozen sweep, the residue sweep, the startup
        verification and `--list` all missed it, because every one of them
        reads RECORDS.

        NO status filter, and that is the difference from the post-close twin.
        An entry residue can sit on a record in any state: `open` when the
        round completed some spreads and orphaned a leg, or the pre-entry
        state when nothing filled at all and the record never became a
        position. The incident, not the status, is the query.

        Read-only and escalate-only. No caller may place an order on the
        strength of this list.
        """
        return [t for t in self._trades
                if (t.get('entry_residue') or {}).get('state') == 'open']




    def set_trade_status(self, trade_id: int, status: str, **extra_fields):
        """Update trade status and optional extra fields. Saves local + Drive.

        Used for state transitions: open → closing → closed / partial_close.
        """
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
        few seconds — Drive sync happens on the normal maybe_sync() cycle.
        """
        with self._mutate(drive=False):
            for t in self._trades:
                if t['id'] == trade_id:
                    for k, v in fields.items():
                        t[k] = v
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
