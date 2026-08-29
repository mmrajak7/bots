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
from common import store_contract
from common.locked_store import LockTimeout
from common.spread_store import SpreadStoreBase
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


class BearPutStore(SpreadStoreBase):
    """Bear Put Spread trade CRUD with local file + Google Drive sync.

    Usage:
        store = BearPutStore()
        store.initialize()       # Auth Drive, download, fallback to local
        trades = store.load_trades()  # From cache, zero network
        store.add_trade({...})   # Validate, save local + Drive
        store.maybe_sync()       # Re-download from Drive if stale
    """

    #: This store's module, so `SpreadStoreBase` resolves this
    #: book's data file, lock and logger rather than its own.
    _MODULE = __name__

    # NO `_bound_exit` OVERRIDE, and that is now VISIBLE.
    # `bcs` and `fallen_hero` each clamp a booked exit to what their
    # structure can be worth; this book never grew one. It used to be
    # an absent call in a method nobody compared; it is an
    # unoverridden hook now, which is the difference between a gap
    # and an oversight. No bound is applied to a bear put spread yet.


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


    # ── Sync ────────────────────────────────────────────────────────────


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
