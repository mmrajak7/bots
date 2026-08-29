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
from common import store_contract
from common.locked_store import LockTimeout
from common.spread_store import SpreadStoreBase
from common.option_symbols import check_leg_types
from common import layered_config

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


#: What this book's legs must be. Checked on every `add_trade`, because
#: `bcs/spread_monitor.py` picks SL_SPOT and TP DIRECTION from which store a
#: record came out of — so a record filed in the wrong book has its stops
#: inverted, and on the first poll of a healthy position both `spot <= sl_spot`
#: and `spot >= target` can be true at once. See common/option_symbols.py.
LEG_TYPES = {'long_put_symbol': 'PE', 'short_put_symbol': 'PE',
             'short_call_symbol': 'CE', 'long_call_symbol': 'CE'}


def _load_config() -> dict:
    """Load Fallen Hero config from config/fallen_hero_config.json."""
    # TWO LAYERS: config/fallen_hero_config.defaults.json (tracked, secret-free)
    # under config/fallen_hero_config.json (untracked, secrets). See
    # common/layered_config.py — including why the overlay wins.
    return layered_config.load('fallen_hero_config')


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


#: The four leg-price fields an FH exit record may carry. Same names the
#: monitor writes, so a hand-built exit and a machine-built one are the same
#: shape and there is one vocabulary, not two.
EXIT_FILL_FIELDS = ('short_call_fill', 'long_call_fill',
                    'short_put_fill', 'long_put_fill')


def exit_is_approximate(trade: dict) -> bool:
    """Is this closed trade's P&L known to be an approximation?

    True when a leg could not be priced, or when the figure had to be clamped
    to the structure's ceiling. Read BOTH the top-level marker and the one
    inside `exit`: the top-level copy exists for readers that scan trade dicts
    (`list_trades`, `journal_report`, the dashboard) and older records predate
    it entirely, so neither alone is sufficient.

    Absence means EXACT — deliberately, and it is the safe default here in a
    way it would not be on the order path: a book of ~450 historical records
    was closed before this marker existed, and defaulting those to approximate
    would print a caveat on every line, which is how a caveat stops being read.
    """
    if trade.get('exit_approximate') is True:
        return True
    ex = trade.get('exit') or {}
    return ex.get('pnl_approximate') is True


def bound_fh_exit(trade: dict, exit_data: dict) -> dict:
    """Bound a Fallen Hero exit to what the structure can arithmetically do.

    Two things, and only two. It never invents a price and never computes a
    P&L the caller did not supply — that would be the `0.0`-seed defect (D1/D4)
    with better manners.

    **1. CLAMP `pnl_per_share` to `total_credit`.** For a Fallen Hero,

        close_cost   = SC + SP - LC - LP
        pnl_per_share = total_credit - close_cost

    and both long strikes sit further OTM than the shorts they hedge, so for
    the same expiry `SC >= LC` and `SP >= LP`: `close_cost` is non-negative and
    the P&L can never exceed the credit. Every leg expiring worthless REACHES
    that bound, so it is mathematically achievable — which per CLAUDE.md's
    "Valuation bounds — clamp the arithmetic, refuse the estimate" makes it a
    CLAMP, not an estimate to defer on. The defect that motivated it (D4)
    reported `pnl_per_share 102.75` against a `total_credit` of 97.75, from an
    already-flat short put counted at 0.00; but the clamp is applied here,
    at the write boundary, precisely so it does not depend on which code
    produced the number. A hand-built `exit_data` gets it too.

    **2. MARK approximate.** An explicit `None` in any leg-fill field is an
    acknowledged unknown, and a record carrying one cannot present its P&L as
    exact. A MISSING field is not the same thing and is left alone: a human
    recording "I closed the whole thing for Rs X" has an exact number and no
    per-leg detail, and inferring doubt from silence would put a caveat on
    every hand-written record.

    It is LOUD when the clamp binds. A clamp that fires silently turns a
    visible bug into a plausible number, which is the failure it exists to
    catch rather than a fix for it.

    Returns a NEW dict; the caller's is never mutated.
    """
    out = dict(exit_data or {})

    # An explicitly-unknown leg price makes the whole figure approximate.
    if any(f in out and out[f] is None for f in EXIT_FILL_FIELDS):
        out['pnl_approximate'] = True
        out.setdefault('unpriced_legs',
                       [f for f in EXIT_FILL_FIELDS
                        if f in out and out[f] is None])

    try:
        credit = float(trade.get('total_credit'))
        pnl = float(out.get('pnl_per_share'))
    except (TypeError, ValueError):
        # No credit on the record, or no per-share P&L in the exit. Nothing to
        # bound against — and refusing the whole write over a missing optional
        # field would strand a close the owner has already made at the broker.
        return out

    if pnl <= credit:
        return out

    out['pnl_per_share'] = credit
    out['pnl_clamped_from'] = pnl
    out['pnl_approximate'] = True

    # Keep `total_pnl` consistent with the clamped per-share figure, or it
    # contradicts the very number it is derived from. Quantity is a required
    # field on `add_trade`, so it is normally there; the ratio fallback covers
    # a record that predates the check or was written by hand.
    qty = trade.get('quantity')
    try:
        if qty:
            out['total_pnl'] = credit * float(qty)
        elif out.get('total_pnl') is not None and pnl:
            out['total_pnl'] = float(out['total_pnl']) * (credit / pnl)
    except (TypeError, ValueError):
        out['total_pnl'] = None

    logger.error(
        'FH #%s %s: exit pnl_per_share %.2f EXCEEDS total_credit %.2f — '
        'arithmetically impossible for a Fallen Hero (close_cost cannot be '
        'negative). Clamped to %.2f and marked approximate. A leg was '
        'mispriced; read the exit fills.',
        trade.get('id'), trade.get('stock'), pnl, credit, credit)
    return out


class FallenHeroStore(SpreadStoreBase):
    """Trade CRUD with local file + Google Drive sync.

    Usage:
        store = FallenHeroStore()
        store.initialize()           # Auth Drive, download, fallback to local
        trades = store.load_trades() # From cache, zero network
        store.add_trade({...})       # Validate, save local + Drive
        store.maybe_sync()           # Re-download from Drive if stale
    """

    #: This store's module, so `SpreadStoreBase` resolves this
    #: book's data file, lock and logger rather than its own.
    _MODULE = __name__

    def _bound_exit(self, trade: dict, exit_data: dict) -> dict:
        """A reverse jade lizard's exit bound. Called by the base from `update_trade_exit`.

        The hook, not shared code: what a structure can be worth at
        exit is a fact about the STRUCTURE, not about how the book is
        stored.
        """
        return bound_fh_exit(trade, exit_data)


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

        # Refuse a record whose legs contradict this book. Cheap here, and the
        # only other place it could be caught is the monitor — after the trade
        # is already open and being managed with inverted stops.
        wrong = check_leg_types(trade_dict, LEG_TYPES)
        if wrong:
            raise ValueError(
                "Leg types do not match this book: " + "; ".join(wrong)
                + ". Save it through the store for its own structure.")

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


    # -- Sync --------------------------------------------------------------


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

            # '~' rather than a whole column: it has to survive the eye
            # skimming for the number, and a marker in the margin does not.
            status = t['status'] + ('~' if exit_is_approximate(t) else '')
            print(f"{t['id']:>3}  {t['stock']:<12} {status:<8} "
                  f"{put_spread_str:<16} {call_str:>10}  {t['expiry']:<12} "
                  f"{t['total_credit']:>8.2f} {lots_str:>8} "
                  f"{t['sl_spot']:>8.1f} {t['breakeven']:>10.2f}")

        open_trades = [t for t in trades if t['status'] == 'open']
        closed_trades = [t for t in trades if t['status'] == 'closed']
        print(f"\nOpen: {len(open_trades)} | Closed: {len(closed_trades)} "
              f"| Total: {len(trades)}")

        if closed_trades:
            total_pnl = sum(
                (t['exit'].get('total_pnl') or 0)
                for t in closed_trades if t.get('exit')
            )
            approx = [t for t in closed_trades if exit_is_approximate(t)]
            # The total is only as exact as its least exact term, so the
            # caveat rides on the TOTAL, not just on the rows. A reader who
            # only ever looks at the bottom line still sees it.
            note = (f"  (~ {len(approx)} of {len(closed_trades)} approximate: "
                    f"#{', #'.join(str(t['id']) for t in approx)})"
                    if approx else "")
            print(f"Closed P&L: Rs {total_pnl:+,.0f}{note}")


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
