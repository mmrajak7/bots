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
from common.locked_store import LockTimeout
from common.spread_store import SpreadStoreBase
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


class TradeStore(SpreadStoreBase):
    """Trade CRUD with local file + Google Drive sync.

    Usage:
        store = TradeStore()
        store.initialize()       # Auth Drive, download, fallback to local
        trades = store.load_trades()  # From cache, zero network
        store.add_trade({...})   # Validate, save local + Drive
        store.maybe_sync()       # Re-download from Drive if stale
    """

    #: This store's module, so `SpreadStoreBase` resolves this
    #: book's data file, lock and logger rather than its own.
    _MODULE = __name__

    def _bound_exit(self, trade: dict, exit_data: dict) -> dict:
        """A bull call spread cannot be worth more than its width. Called by the base from `update_trade_exit`.

        The hook, not shared code: what a structure can be worth at
        exit is a fact about the STRUCTURE, not about how the book is
        stored.
        """
        return bound_bcs_exit(trade, exit_data)


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


    # ── Sync ────────────────────────────────────────────────────────────


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
    """Load all trades (backward compat).

    WHOLE BOOK: this is the raw accessor, and the cohort rule does not apply
    to this store at all — `logs/bcs_trades.json` holds hand-entered BCS
    trades, not the zebra cohort. Scoping here would hide records from the
    merge, the id allocator and the recovery sweeps.
    """
    return get_store().load_trades()


def add_trade(trade_dict: dict) -> dict:
    """Add a new trade (backward compat)."""
    return get_store().add_trade(trade_dict)


def update_trade_exit(trade_id: int, exit_data: dict):
    """Mark trade as closed (backward compat)."""
    get_store().update_trade_exit(trade_id, exit_data)
