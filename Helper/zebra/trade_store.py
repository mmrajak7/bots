"""Zebra trade store — CRUD + Drive sync.

Lifecycle: watching → triggered → entered → exited.
A signal can also be 'cancelled' from watching/triggered.

Mirrors pyramid/bcs store pattern: local-first JSON, Drive-secondary,
atomic writes, version-based merge, singleton access.
"""

import copy
import json
import logging
import os
import platform
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config as cfg
from common.filelock import LockTimeout, exclusive

logger = logging.getLogger(__name__)

# Non-mfe_* fields the batched per-poll write is allowed to carry. Kept as an
# explicit allowlist rather than loosening the prefix check: that check exists
# because callers pass whole-state patches, so one typo'd key would silently
# overwrite `status` or `debit` on a live position. The spot-corroboration
# reference rides this write for the same reason the peaks do — it updates once
# per open position per poll, and a write per trade per cycle is precisely what
# the batching removed.
_BATCHED_POLL_FIELDS = frozenset({'corrob_spot', 'corrob_value', 'corrob_t'})


def in_cohort(trade: dict, cohort: Optional[str] = None) -> bool:
    """Was this position opened by the CURRENT engine?

    ONE definition, imported by every reader, because the alternative is each
    report inventing its own date test and them quietly disagreeing about what
    the scoreboard means.

    A record with no `cohort` stamp is LEGACY by construction — the 383 records
    that predate the stamp were priced mid-mid, ran unvetted, and were made by
    an engine that no longer exists. Absence is the answer, not a reason to
    guess from `entry_date`.
    """
    stamped = trade.get('cohort')
    if not stamped:
        return False
    return stamped == (cohort or cfg.COHORT_START)


def cohort_split(trades: list, cohort: Optional[str] = None) -> tuple:
    """(current-engine trades, legacy trades). Order matters — current first,
    because it is the one anybody is actually asking about."""
    want = cohort or cfg.COHORT_START
    current = [t for t in trades if in_cohort(t, want)]
    legacy = [t for t in trades if not in_cohort(t, want)]
    return current, legacy


def _load_config() -> dict:
    if not cfg.CONFIG_FILE.exists():
        logger.warning("Config %s not found, using defaults", cfg.CONFIG_FILE)
        return {}
    with open(cfg.CONFIG_FILE) as f:
        return json.load(f)


def _resolve_credentials_path(config: dict) -> Optional[Path]:
    env_path = os.environ.get('ZEBRA_GOOGLE_CREDS')
    if env_path:
        return Path(env_path)
    drive_cfg = config.get('google_drive', {})
    if platform.system() == 'Windows':
        path_str = drive_cfg.get('credentials_path_windows')
    else:
        path_str = drive_cfg.get('credentials_path_linux')
    return Path(path_str) if path_str else None


class ZebraStore:
    """Zebra trades with local JSON + Drive sync."""

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

    def initialize(self):
        drive_cfg = self._config.get('google_drive', {})
        if drive_cfg.get('enabled', False):
            self._init_drive(drive_cfg)
        if self._drive_enabled:
            self._sync_from_drive()
        else:
            self._load_local()

        watching = sum(1 for t in self._trades if t.get('status') == 'watching')
        triggered = sum(1 for t in self._trades if t.get('status') == 'triggered')
        entered = sum(1 for t in self._trades if t.get('status') == 'entered')
        logger.info(
            "ZebraStore: %d trades (%d watching, %d triggered, %d entered), drive=%s",
            len(self._trades), watching, triggered, entered,
            'enabled' if self._drive_enabled else 'disabled'
        )

    # ── Reads ─────────────────────────────────────────────────────────────
    def load_trades(self) -> list:
        return self._trades

    def get_by_status(self, status: str) -> list:
        return [t for t in self._trades if t.get('status') == status]

    def get_watching(self) -> list:
        return self.get_by_status('watching')

    def get_triggered(self) -> list:
        return self.get_by_status('triggered')

    def get_entered(self) -> list:
        return self.get_by_status('entered')

    def find(self, trade_id: int) -> Optional[dict]:
        for t in self._trades:
            if t.get('id') == trade_id:
                return t
        return None

    # ── Cross-process mutation guard ──────────────────────────────────────
    @contextmanager
    def _mutate(self, drive: bool = True, persist: bool = True):
        """Lock → refresh from disk → caller mutates → save → unlock → Drive.

        Every write path MUST go through this. As of 2026-08-10 there are two
        writer processes (zebra cron and the Claude vetting/review cron), and
        an unprotected read-modify-write silently loses trades: both read the
        same state, both write, and the second erases the first's change with
        no error and no corrupt file.

        The refresh matters as much as the lock. `self._trades` is a cache that
        goes stale the moment the OTHER process writes, so mutating it and
        saving would push stale data back over fresh. Inside the lock we
        re-read the disk and version-merge, making disk the truth before the
        caller touches anything. That is also why callers must call `find()`
        INSIDE the block — a dict fetched before the refresh is a detached
        object whose mutations would be silently dropped.

        Ordering is deliberate: the Drive upload is a NETWORK call and happens
        after the lock is released. Holding a mutex across an HTTP round-trip
        would stall the other cron's entire cycle (see feedback: drive-first —
        sync down before, upload after, never inside).
        """
        with exclusive(cfg.LOCK_FILE):
            self._trades = self._merge(self._read_local(), self._trades)
            # Rollback point. If the caller raises mid-mutation (e.g.
            # _apply_entry sets status='entered' and then a float() cast on a
            # later field blows up), the half-mutated trade must not linger in
            # self._trades — get_entered() would report a position that was
            # never persisted. Snapshot post-refresh state and restore it on
            # any exception; disk is untouched either way (save is skipped).
            snapshot = copy.deepcopy(self._trades)
            try:
                yield
            except BaseException:
                self._trades = snapshot
                raise
            # A mutation that mutated nothing must not rewrite the store. The
            # guarded advisory writers (reset_confirm, clear_blind) run for
            # every open position on every poll and their guard skips only the
            # FIELD assignment, not this save — so a quiet cycle with 24
            # positions was rewriting ~1 MB forty-eight times and taking the
            # cross-process lock for each, on a Pi that also runs the
            # live-money monitor. The snapshot already exists for rollback;
            # comparing against it is far cheaper than serialising to disk.
            changed = self._trades != snapshot
            if persist and changed:
                self._save_local()
        if persist and changed and drive:
            self._upload_to_drive()

    # ── Writes ────────────────────────────────────────────────────────────
    def add_signal(self, data: dict) -> dict:
        """Add a fresh signal at WATCH band entry (gap <= watch_gap_max).

        Required: stock, timeframe, direction, st_value, st_direction,
                  signal_price, signal_gap_pct.
        """
        required = ['stock', 'timeframe', 'direction', 'st_value',
                    'st_direction', 'signal_price', 'signal_gap_pct']
        missing = [f for f in required if f not in data]
        if missing:
            raise ValueError(f"Missing required fields: {missing}")

        stock = data['stock']
        timeframe = data['timeframe']
        direction = data['direction']

        # Dedup + id allocation must both happen INSIDE the lock: they are a
        # read-check-write, and two processes racing here would either double-add
        # the same signal or hand out the same id to two different trades.
        #
        # The dedup predicate is `shadow_of is None`, NOT `structure != 'bcs'`.
        # It used to be the latter, which was right only while every BCS was a
        # second record shadowing a zebra: a shadow is an observation, so it
        # must not block a new signal. Once a BCS became the position itself
        # (2026-08-12) that same test would have excluded every real position
        # from dedup and let duplicates open on one thesis. "Is this a shadow
        # of something else" is the property that actually matters, and it does
        # not change meaning when the structure does.
        with self._mutate():
            for t in self._trades:
                if (t.get('stock') == stock
                        and t.get('timeframe') == timeframe
                        and t.get('direction') == direction
                        and t.get('shadow_of') is None
                        and t.get('status') in ('watching', 'triggered', 'entered')):
                    raise ValueError(
                        f"{stock} {timeframe} {direction} already open as #{t['id']}"
                    )

            now = datetime.now()
            trade = {
                'id': self._next_id(),
                'version': 1,
                'status': 'watching',
                'stock': stock,
                'timeframe': timeframe,
                'direction': direction,            # CE or PE
                'st_value': data['st_value'],
                'st_direction': data['st_direction'],
                # trend_aligned is NOT stored — it is derived on demand from
                # direction + st_direction via cfg.is_trend_aligned (single source
                # of truth), so it can never drift from or be dropped by the schema.
                'signal_price': data['signal_price'],
                'signal_gap_pct': data['signal_gap_pct'],
                'signal_date': now.strftime('%Y-%m-%d'),
                'signal_time': now.strftime('%H:%M:%S'),
                'paper': data.get('paper', True),
                'notes': data.get('notes', ''),
            }
            self._trades.append(trade)
        logger.info(
            "WATCHING #%d %s %s %s spot=%.2f ST=%.2f gap=%.2f%%",
            trade['id'], stock, timeframe, direction,
            trade['signal_price'], trade['st_value'], trade['signal_gap_pct']
        )
        return trade

    def mark_triggered(self, trade_id: int, trigger_spot: float,
                       trigger_gap_pct: float,
                       alert_strikes: list) -> dict:
        """Promote watching → triggered. alert_strikes = candidate pairs from analyzer."""
        with self._mutate():
            # find() inside the lock: a dict fetched before the refresh is a
            # detached object and its mutations would be silently discarded.
            t = self._must_find(trade_id)
            if t['status'] != 'watching':
                raise ValueError(f"#{trade_id} status={t['status']}, can't trigger")
            now = datetime.now()
            t['status'] = 'triggered'
            t['triggered_at'] = now.isoformat()
            t['trigger_spot'] = trigger_spot
            t['trigger_gap_pct'] = trigger_gap_pct
            t['alert_strikes'] = alert_strikes
            t['version'] = t.get('version', 0) + 1
        logger.info("TRIGGERED #%d %s gap=%.2f%% (%d candidate pairs)",
                    trade_id, t['stock'], trigger_gap_pct, len(alert_strikes))
        return t

    def mark_entered(self, trade_id: int, entry_data: dict) -> dict:
        """Promote triggered → entered. User has placed the trade.

        Required in entry_data: long_strike, short_strike, long_symbol,
        short_symbol, debit, lot_size, lots, expiry.
        Computed: quantity, capital, tp_spot, sl_spot, debit_sl_value, dte.
        """
        required = ['long_strike', 'short_strike', 'long_symbol',
                    'short_symbol', 'debit', 'lot_size', 'lots', 'expiry']
        missing = [f for f in required if f not in entry_data]
        if missing:
            raise ValueError(f"mark_entered missing: {missing}")

        with self._mutate():
            t = self._must_find(trade_id)
            if t['status'] not in ('watching', 'triggered'):
                raise ValueError(f"#{trade_id} status={t['status']}, can't enter")
            self._refuse_if_book_full(trade_id)
            self._apply_entry(t, entry_data)
        logger.info(
            "ENTERED #%d %s %s/%s debit=%.2f qty=%d cap=Rs%.0f TP=%.2f SL=%.2f",
            trade_id, t['stock'], int(t['long_strike']), int(t['short_strike']),
            t['debit'], t['quantity'], t['capital'], t['tp_spot'], t['sl_spot']
        )
        return t

    def _apply_entry(self, t: dict, entry_data: dict) -> None:
        """Field-level entry mutation. Split out of mark_entered so the whole
        computation runs INSIDE the lock without a 60-line critical section
        being visually lost in the middle of the method."""
        lot_size = int(entry_data['lot_size'])
        lots = int(entry_data['lots'])
        quantity = lot_size * lots
        debit = float(entry_data['debit'])
        capital = round(debit * quantity, 2)
        entry_spot = float(entry_data.get('entry_spot', t.get('trigger_spot',
                                          t.get('signal_price'))))

        direction = t['direction']
        # Spot SL: adverse direction from entry_spot
        spot_sl_pct = float(entry_data.get('spot_sl_pct', cfg.SPOT_SL_PCT))
        if direction == 'CE':
            sl_spot = round(entry_spot * (1 - spot_sl_pct), 2)
        else:
            sl_spot = round(entry_spot * (1 + spot_sl_pct), 2)

        # TP: a swing level in the way (if the caller found one), else the ST
        # line, else the short strike. The override is computed in the monitor
        # because it needs candles and a kite handle; it arrives already
        # validated as lying strictly between spot and ST, so it can only ever
        # SHORTEN the target. See zebra/history.py:swing_tp.
        tp_target = cfg.TP_TARGET
        base = (float(entry_data['short_strike']) if tp_target == 'short_strike'
                else float(t['st_value']))
        if entry_data.get('tp_spot') is not None:
            # The LIVE ticket can quote a swing-shortened target, but this path
            # had no way to receive it — so the monitor watched the ST line
            # while the owner had been told a nearer level. The TP then fired
            # late, or never, in exactly the case the feature exists for
            # (price stalling at its own support short of the magnet).
            tp_spot = float(entry_data['tp_spot'])
            t['tp_source'] = 'manual'
        else:
            tp_spot = self._resolve_tp(
                t, entry_data.get('swing_tp'), base,
                'short_strike' if tp_target == 'short_strike' else 'st_line')

        debit_sl_value = round(debit * cfg.DEBIT_SL_PCT, 2)

        # DTE
        try:
            exp_date = datetime.strptime(entry_data['expiry'], '%Y-%m-%d')
            dte = (exp_date.date() - datetime.now().date()).days
        except Exception:
            dte = None

        now = datetime.now()
        t['status'] = 'entered'
        t['entry_date'] = now.strftime('%Y-%m-%d')
        t['entry_time'] = now.strftime('%H:%M:%S')
        t['entry_spot'] = entry_spot
        t['long_strike'] = float(entry_data['long_strike'])
        t['short_strike'] = float(entry_data['short_strike'])
        t['long_symbol'] = entry_data['long_symbol']
        t['short_symbol'] = entry_data['short_symbol']
        t['debit'] = debit
        t['lot_size'] = lot_size
        t['lots'] = lots
        t['quantity'] = quantity
        t['capital'] = capital
        t['expiry'] = entry_data['expiry']
        t['dte_at_entry'] = dte
        t['tp_spot'] = tp_spot
        t['sl_spot'] = sl_spot
        t['spot_sl_pct'] = spot_sl_pct
        t['debit_sl_value'] = debit_sl_value
        t['debit_sl_pct'] = cfg.DEBIT_SL_PCT
        # WHICH STRUCTURE, and therefore which valuation formula, for the rest
        # of the position's life. Absent, `_long_multiplier` falls back to 2
        # (zebra) and a hand-entered BCS is marked at `2*long - short`: the
        # debit SL never fires because the value reads roughly double, the
        # trail never arms because it needs a width, and the P&L is twice the
        # truth. `width` is derived here rather than passed so the two can
        # never disagree.
        # Only 'bcs' is stamped. A zebra stays unmarked, which is the existing
        # convention every reader already keys on (`structure != 'bcs'`), and
        # leaving it alone keeps this fix to the one case that was broken.
        structure = entry_data.get('structure') or 'zebra'
        if structure == 'bcs':
            t['structure'] = 'bcs'
            t['width'] = round(abs(float(entry_data['short_strike'])
                                   - float(entry_data['long_strike'])), 2)
        # Entry-time extrinsic of the short leg — feeds the intrinsic-floor
        # quote-sanity guard in the monitor (bad-quote false-SL protection).
        # Absent, the floor falls back to a flat 30%-of-debit guess, so the
        # NHPC-class garbage-quote protection runs degraded for the life of the
        # position. This is the LIVE path, where that guard matters most.
        if 'short_extrinsic_entry' in entry_data:
            t['short_extrinsic_entry'] = float(entry_data['short_extrinsic_entry'])
        # THE ENTRY BOOK, PER LEG. Zebra rows carried only the net `debit`,
        # which is not enough to cost the trade: STT is levied per leg, on that
        # leg's premium, so 179 of the 215 closed records simply cannot be
        # charged correctly and never will be. The BCS path has stored this
        # since fill pricing; the zebra path never did, and the gap only
        # surfaced when the two-month cohort needed a NET number.
        #
        # Same lesson as `exit_legs`: an option book cannot be reconstructed
        # after the fact. Write it down at the moment or the post-mortem is
        # guesswork forever.
        for _k in ('long_bid_entry', 'long_ask_entry', 'long_mid_entry',
                   'short_bid_entry', 'short_ask_entry', 'short_mid_entry'):
            if entry_data.get(_k) is not None:
                t[_k] = float(entry_data[_k])
        # WHICH PRICE CONVENTION this position is valued on, for the rest of
        # its life. `_structure_quote` reads it and falls through to MID when
        # absent — so a hand-entered LIVE trade, whose `debit` is what the
        # owner actually PAID (ask - bid), was then marked mid-mid every poll.
        # The debit SL is `mid <= 0.5 * debit`: measuring a fill-basis debit
        # against a mid-basis value fires the stop LATE by roughly half the
        # round-trip spread, and books exits at a price nobody could transact
        # at. The automated path has stamped this since fill pricing shipped;
        # this one never did. Stamped at entry and never changed afterwards —
        # flipping it under an open position would move its stop and trail
        # levels beneath it.
        basis = entry_data.get('pricing_basis')
        if basis:
            t['pricing_basis'] = basis
        self._stamp_cohort(t)
        t['version'] = t.get('version', 0) + 1

    def add_bcs_shadow(self, zebra_trade: dict, bcs: dict) -> dict:
        """Create a paper BCS trade shadowing a just-entered zebra trade.

        Born directly as 'entered' (it has no watching/triggered life of its
        own — the zebra signal already did that). `bcs` is the dict returned
        by strikes.analyze_bcs plus 'expiry' and 'entry_spot' set by caller.
        Tagged structure='bcs' + shadow_of=<zebra id> so reports can pair the
        A/B legs; excluded from all scanner dedup.
        """
        lot_size = int(bcs['lot_size'])
        lots = 1
        quantity = lot_size * lots
        debit = float(bcs['debit'])
        entry_spot = float(bcs['entry_spot'])
        direction = zebra_trade['direction']

        if direction == 'CE':
            sl_spot = round(entry_spot * (1 - cfg.SPOT_SL_PCT), 2)
        else:
            sl_spot = round(entry_spot * (1 + cfg.SPOT_SL_PCT), 2)

        try:
            exp_date = datetime.strptime(bcs['expiry'], '%Y-%m-%d')
            dte = (exp_date.date() - datetime.now().date()).days
        except Exception:
            dte = None

        now = datetime.now()
        # _next_id() + append must be inside the lock, or two processes racing
        # a shadow creation would hand the same id to two different trades.
        with self._mutate():
            trade = self._build_bcs_shadow(zebra_trade, bcs, now, lot_size, lots,
                                           quantity, debit, entry_spot,
                                           direction, sl_spot, dte)
            self._trades.append(trade)
        logger.info(
            "BCS SHADOW #%d (of #%d) %s %s %g/%g debit=%.2f qty=%d d/w=%s%%",
            trade['id'], zebra_trade['id'], trade['stock'], direction,
            trade['long_strike'], trade['short_strike'], debit, quantity,
            trade.get('debit_to_width_pct')
        )
        return trade

    @staticmethod
    def _bcs_entry_fields(bcs, now, lot_size, lots, quantity, debit,
                          entry_spot, sl_spot, dte, tp_spot) -> dict:
        """Every field that makes a record a BCS position.

        Shared by the shadow builder and by mark_entered_bcs so `structure`
        (and `width`, which the trail and the intrinsic floor both read) is
        stamped in ONE place. It used to live only in the shadow builder: a
        BCS promoted through any other path would have carried no `structure`
        key, been treated as a 2-long zebra by `_long_multiplier`, and had
        every structure value — quote, floor, P&L — doubled.
        """
        return {
            'status': 'entered',
            'structure': 'bcs',
            'entry_date': now.strftime('%Y-%m-%d'),
            'entry_time': now.strftime('%H:%M:%S'),
            'entry_spot': entry_spot,
            'long_strike': float(bcs['long_strike']),
            'short_strike': float(bcs['short_strike']),
            'long_symbol': bcs['long_symbol'],
            'short_symbol': bcs['short_symbol'],
            'debit': debit,
            'lot_size': lot_size,
            'lots': lots,
            'quantity': quantity,
            'capital': round(debit * quantity, 2),
            'expiry': bcs['expiry'],
            'dte_at_entry': dte,
            'tp_spot': tp_spot,
            'sl_spot': sl_spot,
            'spot_sl_pct': cfg.SPOT_SL_PCT,
            'debit_sl_value': round(debit * cfg.DEBIT_SL_PCT, 2),
            'debit_sl_pct': cfg.DEBIT_SL_PCT,
            'width': float(bcs['width']),
            'debit_to_width_pct': bcs.get('debit_to_width_pct'),
            # ── the entry books, persisted (2026-08-12) ──────────────────
            # Records used to keep only the derived `debit`, so the gap
            # between quoted and fillable could never be measured after the
            # fact — 42 BCS records, zero with a book on them. That made the
            # entry-cost gate impossible to calibrate and left the
            # `illiquid_book` post-mortem tag with no entry-side evidence.
            'pricing_basis': bcs.get('pricing_basis', 'mid'),
            'debit_mid': bcs.get('debit_mid'),
            'entry_cost': bcs.get('entry_cost'),
            'entry_cost_pct': bcs.get('entry_cost_pct'),
            'debit_to_width_pct_mid': bcs.get('debit_to_width_pct_mid'),
            'long_ask_entry': bcs.get('long_ask'),
            'long_bid_entry': bcs.get('long_bid'),
            'long_mid_entry': bcs.get('long_mid'),
            'short_ask_entry': bcs.get('short_ask'),
            'short_bid_entry': bcs.get('short_bid'),
            'short_mid_entry': bcs.get('short_mid'),
            'long_oi_entry': bcs.get('long_oi'),
            'short_oi_entry': bcs.get('short_oi'),
            'short_extrinsic_entry': float(bcs.get('short_extrinsic', 0)
                                           or bcs.get('short_extrinsic_entry', 0)),
            'entry_warnings': bcs.get('warnings', []),
        }

    def _build_bcs_shadow(self, zebra_trade, bcs, now, lot_size, lots, quantity,
                          debit, entry_spot, direction, sl_spot, dte) -> dict:
        """Assemble the shadow record. Called INSIDE the store lock so that
        _next_id() sees every trade another process may have just added."""
        trade = {
            'id': self._next_id(),
            'version': 1,
            'shadow_of': zebra_trade['id'],
            'stock': zebra_trade['stock'],
            'timeframe': zebra_trade['timeframe'],
            'direction': direction,
            'st_value': zebra_trade['st_value'],
            'st_direction': zebra_trade['st_direction'],
            'signal_price': zebra_trade['signal_price'],
            'signal_gap_pct': zebra_trade['signal_gap_pct'],
            'signal_date': zebra_trade.get('signal_date'),
            'signal_time': zebra_trade.get('signal_time'),
            'paper': True,
            'notes': f"BCS shadow of zebra #{zebra_trade['id']}",
        }
        trade.update(self._bcs_entry_fields(
            bcs, now, lot_size, lots, quantity, debit, entry_spot, sl_spot, dte,
            float(zebra_trade.get('tp_spot', zebra_trade['st_value']))))
        return trade

    @staticmethod
    def _resolve_tp(t: dict, swing, base: float, base_name: str) -> float:
        """TP: a swing level standing in the way, else the usual target.

        Shared by the zebra and BCS entry paths. They derived their TP
        independently before this, which is exactly how the BCS path — the one
        that actually trades — ends up quietly missing a feature.

        `swing` arrives already validated as lying strictly between spot and
        the ST line (zebra/history.py:swing_tp), so this can only ever SHORTEN
        the target. The original is kept on the record: a trade whose TP was
        moved has to be reviewable against the target it would otherwise have
        had, or the shortening can never be scored.
        """
        if isinstance(swing, dict) and swing.get('tp_spot'):
            t['tp_source'] = swing.get('kind', 'swing')
            t['tp_swing'] = swing
            t['tp_st_line'] = float(t['st_value'])
            return float(swing['tp_spot'])
        t['tp_source'] = base_name
        return float(base)

    def _stamp_cohort(self, t: dict) -> None:
        """Mark which engine opened this position. Stamped once, at entry.

        Derived from the config at ENTRY time and then frozen, rather than
        recomputed from `entry_date` on every read. Two reasons, both learned
        the hard way on `pricing_basis`:

        - Moving `cohort_start` later must not silently reclassify positions
          that are already open and already being measured.
        - `entry_date` is a display string; a cohort test that re-parses it on
          every read is one date-format change away from silently returning
          False for the whole book.
        """
        t['cohort'] = cfg.COHORT_START

    def _refuse_if_book_full(self, trade_id: int) -> None:
        """Cap concurrent open positions. MUST be called inside the lock.

        `MAX_OPEN_TRADES` was defined in config, asserted by a test, and read
        by NOTHING — the only portfolio-level risk control in the system was
        decorative, and the paper book duly reached 17 simultaneous positions
        against a stated cap of 8. In paper that only skews the record; with
        real money it is the difference between a bad day and an account.

        PAPER is deliberately exempt: capping entries would bias which trades
        the validation record contains, and an unentered paper signal costs
        nothing. The cap binds where it means something — live.

        Raises ValueError, which every caller already treats as "did not
        enter" (the monitor logs and leaves the row triggered; the CLI prints
        and exits non-zero). Refusing to open is always safe; the signal is
        simply not taken.
        """
        if cfg.PAPER_MODE or not cfg.MAX_OPEN_TRADES:
            return
        open_n = sum(1 for x in self._trades if x.get('status') == 'entered')
        if open_n >= cfg.MAX_OPEN_TRADES:
            raise ValueError(
                f"#{trade_id} refused: {open_n} positions already open, cap is "
                f"{cfg.MAX_OPEN_TRADES} (max_open_trades). Close something or "
                f"raise the cap deliberately.")

    def mark_entered_bcs(self, trade_id: int, bcs: dict) -> dict:
        """Promote a signal straight into a BCS position — ONE record.

        The BCS-only pipeline (2026-08-12). Where `add_bcs_shadow` creates a
        second record shadowing a zebra, this turns the signal itself into the
        position: no `shadow_of`, so it dedups like any other open trade, and
        the scanner will not hand out a duplicate on the same thesis.
        """
        lot_size = int(bcs['lot_size'])
        lots = 1
        quantity = lot_size * lots
        debit = float(bcs['debit'])
        entry_spot = float(bcs['entry_spot'])

        with self._mutate():
            t = self._must_find(trade_id)
            if t['status'] not in ('watching', 'triggered'):
                raise ValueError(f"#{trade_id} status={t['status']}, can't enter")
            self._refuse_if_book_full(trade_id)
            direction = t['direction']
            sl_spot = round(entry_spot * (1 - cfg.SPOT_SL_PCT), 2) \
                if direction == 'CE' else \
                round(entry_spot * (1 + cfg.SPOT_SL_PCT), 2)
            try:
                exp_date = datetime.strptime(bcs['expiry'], '%Y-%m-%d')
                dte = (exp_date.date() - datetime.now().date()).days
            except Exception:
                dte = None
            t.update(self._bcs_entry_fields(
                bcs, datetime.now(), lot_size, lots, quantity, debit,
                entry_spot, sl_spot, dte,
                self._resolve_tp(t, bcs.get('swing_tp'),
                                 float(t.get('tp_spot', t['st_value'])),
                                 'st_line')))
            self._stamp_cohort(t)
            t['version'] = t.get('version', 0) + 1
        logger.info(
            "ENTERED BCS #%d %s %g/%g debit=%.2f qty=%d cap=Rs%.0f d/w=%s%% "
            "TP=%.2f SL=%.2f",
            trade_id, t['stock'], t['long_strike'], t['short_strike'],
            debit, quantity, t['capital'], t.get('debit_to_width_pct'),
            t['tp_spot'], t['sl_spot'])
        return t

    def mark_exited(self, trade_id: int, exit_spot: float,
                    exit_debit: Optional[float],
                    reason: str, exit_legs: Optional[dict] = None) -> dict:
        """Close an entered trade. exit_debit = closing net debit per share
        (positive if still costs money to close, negative if closes for credit)."""
        with self._mutate():
            t = self._must_find(trade_id)
            # Status check inside the lock is what makes the exit idempotent
            # across processes: if the other cron already closed this trade,
            # we see 'exited' here and refuse, instead of double-booking a
            # close on stale in-memory state.
            if t['status'] != 'entered':
                raise ValueError(f"#{trade_id} status={t['status']}, can't exit")
            self._apply_exit(t, exit_spot, exit_debit, reason, exit_legs)
        return t

    @staticmethod
    def _bound_exit_value(t: dict, exit_debit: Optional[float]) -> Optional[float]:
        """Hold the booked value inside what the structure can be worth.

        The monitor already rejects impossible quotes, so in the automated path
        this should never bind. It exists because this is the LAST place every
        exit passes through, including `zebra close` where a human types the
        number, and because the invariant is worth enforcing where the record
        is written rather than trusting six callers to have checked.

        Bounds: a debit structure is never worth less than zero (you can always
        let it expire), and a vertical is never worth more than its width.
        PIIND #50 booked exit_debit -30.04 on a debit of 242.11 — -112.4% on a
        structure whose arithmetic floor is -100% — because nothing enforced
        the lower bound anywhere.
        """
        if exit_debit is None:
            return None
        try:
            v = float(exit_debit)
        except (TypeError, ValueError):
            return exit_debit
        lo = 0.0
        hi = None
        if t.get('structure') == 'bcs':
            try:
                w = float(t.get('width') or 0)
            except (TypeError, ValueError):
                w = 0.0
            if w > 0:
                hi = w
        bounded = v
        if bounded < lo:
            bounded = lo
        if hi is not None and bounded > hi:
            bounded = hi
        if bounded != v:
            logger.error(
                "EXIT VALUE OUT OF RANGE #%s %s: %.2f -> %.2f "
                "(structure=%s width=%s) — booking the bound, not the quote",
                t.get('id'), t.get('stock'), v, bounded,
                t.get('structure') or 'zebra', t.get('width'))
        return bounded

    def _apply_exit(self, t: dict, exit_spot: float,
                    exit_debit: Optional[float], reason: str,
                    exit_legs: Optional[dict] = None) -> None:
        """Field-level exit mutation — runs inside the store lock."""
        debit = float(t['debit'])
        qty = int(t['quantity'])
        exit_debit = self._bound_exit_value(t, exit_debit)
        if exit_debit is not None:
            # P&L per share = current value of structure - entry debit
            # If user closed at exit_debit (i.e., paid that much to unwind),
            # then they recovered (debit - exit_debit). But Zebra is a debit
            # trade: the structure has POSITIVE value when in profit, negative
            # of debit when worst case. exit_debit here = current mark of the
            # structure (long_value*2 - short_value), so P&L = exit_debit - debit.
            # We document this convention in monitor.
            pnl_per_share = float(exit_debit) - debit
        else:
            # Worst case: structure went to -debit (max loss)
            pnl_per_share = -debit
        pnl = round(pnl_per_share * qty, 2)
        pnl_pct = round((pnl_per_share / debit) * 100, 2) if debit > 0 else 0

        now = datetime.now()
        t['status'] = 'exited'
        t['exit_date'] = now.strftime('%Y-%m-%d')
        t['exit_time'] = now.strftime('%H:%M:%S')
        t['exit_spot'] = exit_spot
        t['exit_debit'] = exit_debit
        t['pnl'] = pnl
        t['pnl_pct'] = pnl_pct
        # NET OF CHARGES, beside the gross figure and never replacing it.
        # The two-month paper run exists to answer one question — does this
        # strategy clear its own costs — and the measured baseline says the
        # median trade is +0.90% gross but -0.79% NET. A cohort scored on
        # `pnl_pct` alone looks fine while losing money.
        #
        # `pnl` keeps its meaning so every earlier record stays comparable;
        # mixing the two definitions is the mistake `band_basis` exists to
        # prevent on the magnet stat. Best-effort: a costing failure must
        # never be able to stop a position being closed.
        try:
            from . import fees as fees_mod
            cost = fees_mod.round_trip_for_trade(t, exit_debit)
            t['fees'] = cost
            t['pnl_net'] = round(pnl - float(cost.get('total') or 0), 2)
            t['pnl_net_pct'] = (round((t['pnl_net'] / (debit * qty)) * 100, 2)
                                if debit > 0 and qty > 0 else 0)
        except Exception as e:
            logger.warning("fee stamp failed for #%d: %s — gross P&L stands",
                           t.get('id'), e)
        t['exit_reason'] = reason
        # THE BOOK WE EXITED ON. Entry books have been persisted since fill
        # pricing landed; exits kept only the two scalars, so the one direction
        # that has twice cost real money (ICICI Feb, NHPC Jul) was also the one
        # direction with no evidence. An option book cannot be reconstructed
        # after the fact -- if it is not written down at the moment of the
        # exit, the post-mortem is guesswork forever.
        if exit_legs:
            t['exit_legs'] = exit_legs
        t['version'] = t.get('version', 0) + 1
        logger.info(
            "EXITED #%d %s reason=%s spot=%.2f P&L=Rs%.0f (%.1f%%)",
            t['id'], t['stock'], reason, exit_spot, pnl, pnl_pct
        )

    def cancel(self, trade_id: int, reason: str) -> dict:
        """Cancel a watching/triggered signal."""
        with self._mutate():
            t = self._must_find(trade_id)
            if t['status'] not in ('watching', 'triggered'):
                raise ValueError(f"#{trade_id} status={t['status']}, can't cancel")
            t['status'] = 'cancelled'
            t['cancelled_at'] = datetime.now().isoformat()
            t['cancel_reason'] = reason
            t['version'] = t.get('version', 0) + 1
        logger.info("CANCELLED #%d %s: %s", trade_id, t['stock'], reason)
        return t

    def update_gap(self, trade_id: int, current_gap_pct: float) -> dict:
        """Cheap update of last seen gap on a watching signal.

        In-memory only — no save, no Drive, no version bump, so it needs no
        lock. Purely advisory display state; if the other process overwrites
        it nothing is lost that matters.
        """
        t = self._must_find(trade_id)
        t['last_gap_pct'] = current_gap_pct
        t['last_gap_at'] = datetime.now().isoformat()
        return t

    def set_alert_flag(self, trade_id: int, kind: str,
                       persist: bool = True) -> bool:
        """Idempotent: set <kind>_alerted_at on the trade if not already set.

        Returns True if the flag was newly set (caller should fire the alert),
        False if it was already set (alert already fired in a previous cycle).
        This is the persistent replacement for in-memory dedup that survives
        cron restarts.

        THE read-check-write that most needs the lock. Two processes racing it
        can both observe the flag unset and both return True — meaning two
        exits, two alerts, or in a live context two orders. That is exactly the
        shape of the Feb ICICI bug that put 4x BUY orders on the short leg.
        Test-and-set is only atomic while the lock is held.
        """
        newly_set = False
        with self._mutate(drive=persist, persist=persist):
            t = self.find(trade_id)
            if t:
                key = f"{kind}_alerted_at"
                if not t.get(key):
                    t[key] = datetime.now().isoformat()
                    t['version'] = t.get('version', 0) + 1
                    newly_set = True
        return newly_set

    def set_alert_flag_daily(self, trade_id: int, kind: str,
                             persist: bool = True) -> bool:
        """Like set_alert_flag, but fires at most once per calendar day.

        Used for recurring reminders (e.g. expiry T-3..T-1 daily nag) so the
        user keeps getting nudged each day until they close the position.
        Returns True if the flag was set/refreshed today (fire the alert);
        False if it already fired today.
        """
        newly_set = False
        with self._mutate(drive=persist, persist=persist):
            t = self.find(trade_id)
            if t:
                key = f"{kind}_alerted_at"
                today_str = datetime.now().strftime('%Y-%m-%d')
                if not str(t.get(key, '')).startswith(today_str):
                    t[key] = datetime.now().isoformat()
                    t['version'] = t.get('version', 0) + 1
                    newly_set = True
        return newly_set

    def clear_alert_flag(self, trade_id: int, kind: str,
                         persist: bool = True) -> None:
        """Un-set an alert flag so the next cycle may fire it again.

        Exists for one narrow case: an alert whose flag was claimed but whose
        SEND then failed. Claiming before sending is deliberate (two processes
        must not both send), but without this the failed message is simply lost
        — which for the exit escalation means the human is never asked about a
        position the bot has decided to hold indefinitely.
        """
        with self._mutate(drive=persist, persist=persist):
            t = self.find(trade_id)
            if t and t.get(f"{kind}_alerted_at"):
                t[f"{kind}_alerted_at"] = None
                t['version'] = t.get('version', 0) + 1

    # ── Quote-reliability confirm / blind counters ─────────────────────────
    # Advisory per-trade state for the DEBIT-SL value trigger. Persisted to the
    # LOCAL file only (no Drive upload, no version bump) so it survives cron
    # restarts on the server without churning Drive every 5-min poll. On a
    # version-tie merge the local copy wins, so these fields are never lost.
    def bump_confirm(self, trade_id: int, kind: str, persist: bool = True) -> int:
        """Increment a trigger-confirmation counter, restarting a stale streak.

        A streak whose last hit is older than cfg.CONFIRM_STALE_SEC restarts
        from zero — confirming polls must be reasonably contiguous, but
        unreliable polls in between (which simply don't call this) may not
        indefinitely block a genuine exit. Mirrors bcs.bump_confirm.
        """
        count = 0
        with self._mutate(drive=False, persist=persist):
            t = self.find(trade_id)
            if t:
                key = f"{kind}_confirm"
                tkey = f"{kind}_confirm_t"
                now = time.time()
                if now - float(t.get(tkey, 0.0)) > cfg.CONFIRM_STALE_SEC:
                    t[key] = 0
                t[key] = int(t.get(key, 0)) + 1
                t[tkey] = now
                count = t[key]
        return count

    def reset_confirm(self, trade_id: int, kind: str, persist: bool = True) -> None:
        """Clear a confirmation counter (a reliable non-trigger poll)."""
        with self._mutate(drive=False, persist=persist):
            t = self.find(trade_id)
            if t and t.get(f"{kind}_confirm"):
                t[f"{kind}_confirm"] = 0
                t[f"{kind}_confirm_t"] = time.time()

    def bump_blind(self, trade_id: int, persist: bool = True) -> int:
        """Increment the consecutive unusable-quote cycle counter."""
        count = 0
        with self._mutate(drive=False, persist=persist):
            t = self.find(trade_id)
            if t:
                t['debit_blind_cycles'] = int(t.get('debit_blind_cycles', 0)) + 1
                count = t['debit_blind_cycles']
        return count

    def clear_blind(self, trade_id: int, persist: bool = True) -> None:
        """Reset blindness state on the first usable quote (re-arms the alert)."""
        with self._mutate(drive=False, persist=persist):
            t = self.find(trade_id)
            if t and (t.get('debit_blind_cycles') or t.get('debit_blind_alerted')):
                t['debit_blind_cycles'] = 0
                t['debit_blind_alerted'] = False

    def corroboration_ref(self, trade_id: int) -> dict:
        """The last spot/value pair a RELIABLE poll observed for this trade.

        Persisted (local only, like the confirm counters) because zebra's cron
        process exits between cycles: the live monitor keeps this reference in
        memory only, which it can afford as a long-lived process, but here an
        in-memory reference would reset every 5 minutes and the veto would
        never have anything to compare against.
        """
        t = self.find(trade_id) or {}
        return {'spot': t.get('corrob_spot'), 'value': t.get('corrob_value'),
                't': float(t.get('corrob_t') or 0.0)}

    # The reference is ADVANCED through the batched `apply_mfe` write, not by a
    # setter here: it updates once per open position per poll, and a write per
    # trade per cycle is exactly what the batching exists to prevent. Only
    # RELIABLE readings may advance it — a garbage read that became the
    # baseline would make the next genuine move look uncorroborated, i.e. veto
    # the exit that should happen.

    def mark_blind_alerted(self, trade_id: int, persist: bool = True) -> bool:
        """Set the blind-alert flag once per blind spell. True if newly set."""
        newly_set = False
        with self._mutate(drive=False, persist=persist):
            t = self.find(trade_id)
            if t and not t.get('debit_blind_alerted'):
                t['debit_blind_alerted'] = True
                newly_set = True
        return newly_set

    # ── Max favourable excursion ───────────────────────────────────────────
    def apply_mfe(self, patches: dict, persist: bool = True) -> None:
        """Persist peak-excursion state for many trades in ONE write.

        `patches` is {trade_id: {field: value}}. Batched deliberately: this
        fires for every open position on every poll, and a write per trade
        would rewrite the whole ~1 MB store that many times per cycle while
        holding the cross-process lock each time.

        LOCAL only (drive=False, no version bump), like the confirm/blind
        counters: pushing a peak to Drive every 5 minutes would churn the
        network for data nobody reads until the trade closes. The values still
        REACH Drive, because `mark_exited` does a full drive=True write and its
        in-lock refresh picks them up — so the record lands exactly when it
        becomes worth keeping.

        Callers pass whole-state patches, so the key check is not decoration:
        one typo'd key in a dict-update would silently overwrite `status` or
        `debit` on a live position.
        """
        bad = sorted({k for f in patches.values() for k in f
                      if not str(k).startswith('mfe_')
                      and k not in _BATCHED_POLL_FIELDS})
        if bad:
            raise ValueError(
                f"apply_mfe only writes mfe_* fields or {sorted(_BATCHED_POLL_FIELDS)}, "
                f"got: {bad}")
        if not patches:
            return
        with self._mutate(drive=False, persist=persist):
            for trade_id, fields in patches.items():
                t = self.find(trade_id)
                if t:
                    t.update(fields)

    # ── Listing ───────────────────────────────────────────────────────────
    def list_trades(self, status_filter: Optional[str] = None):
        trades = self._trades
        if status_filter:
            trades = [t for t in trades if t.get('status') == status_filter]
        if not trades:
            print("No trades found.")
            return
        print(f"\n{'ID':>3} {'Status':<10} {'Stock':<12} {'Dir':<4} "
              f"{'TF':<8} {'Strikes':<14} {'Debit':>7} {'TP':>9} {'SL':>9}  Notes")
        print("-" * 110)
        for t in trades:
            strikes = '-'
            if t.get('long_strike') and t.get('short_strike'):
                strikes = f"{int(t['long_strike'])}/{int(t['short_strike'])}"
            debit = f"{t.get('debit', 0):.2f}" if t.get('debit') else '-'
            tp = f"{t.get('tp_spot', 0):.2f}" if t.get('tp_spot') else '-'
            sl = f"{t.get('sl_spot', 0):.2f}" if t.get('sl_spot') else '-'
            notes = (t.get('notes') or t.get('exit_reason') or '')[:30]
            if t.get('structure') == 'bcs':
                notes = f"[BCS] {notes}"[:36]
            print(f"{t['id']:>3} {t.get('status', '?'):<10} {t.get('stock', '?'):<12} "
                  f"{t.get('direction', '?'):<4} {t.get('timeframe', '?'):<8} "
                  f"{strikes:<14} {debit:>7} {tp:>9} {sl:>9}  {notes}")
        print()

    # ── Sync ──────────────────────────────────────────────────────────────
    def maybe_sync(self, force: bool = False):
        if not self._drive_enabled:
            return
        elapsed = time.time() - self._last_sync_time
        if force or elapsed >= self._sync_interval:
            self._sync_from_drive()

    # ── Private ───────────────────────────────────────────────────────────
    def _next_id(self) -> int:
        """Allocate an id that has never been used, even after a quarantine.

        `max(id) + 1` over the live list is an allocator only while the list is
        COMPLETE. It is not, in the one case that matters: `_read_local`
        quarantines a corrupt file and returns `[]`, and if Drive is also down
        `initialize()` takes the local branch, so the store legitimately starts
        empty — and this handed out 1, 2, 3 again.

        That is silently destructive rather than merely confusing. `_merge` is
        keyed on `id` with the higher `version` winning, so once Drive returns,
        a recycled id whose version has outrun the original REPLACES a genuine
        trade. The original is then gone from disk (quarantined) and from Drive
        (overwritten) — the only surviving copy is the `.corrupt.*.json` backup
        nobody knows to look in.

        A monotonic high-water mark on disk makes the collision impossible. It
        is advisory: if it is missing or unreadable we fall back to the live max
        and are no worse off than before.
        """
        live = max((t.get('id', 0) for t in self._trades), default=0)
        nid = max(live, self._read_high_water()) + 1
        self._write_high_water(nid)
        return nid

    @staticmethod
    def _high_water_path():
        # Keyed to the STORE FILE, not to LOG_DIR: the sequence belongs to one
        # book. A directory-wide sidecar would make two store files share an id
        # space, so opening a second store would silently skip ids in the first.
        return cfg.LOCAL_FILE.with_suffix('.nextid.json')

    def _read_high_water(self) -> int:
        try:
            return int(json.loads(
                self._high_water_path().read_text()).get('max_id_ever') or 0)
        except Exception:
            return 0

    def _write_high_water(self, value: int) -> None:
        """Best-effort and never fatal — a trade must not fail to save because
        a bookkeeping sidecar could not be written."""
        try:
            if value <= self._read_high_water():
                return
            cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
            self._high_water_path().write_text(
                json.dumps({'max_id_ever': int(value)}))
        except Exception as e:
            logger.warning("Could not persist the id high-water mark: %s", e)

    def _must_find(self, trade_id: int) -> dict:
        t = self.find(trade_id)
        if not t:
            raise ValueError(f"Trade #{trade_id} not found")
        return t

    def _init_drive(self, drive_cfg: dict):
        creds_path = _resolve_credentials_path(self._config)
        if not creds_path or not creds_path.exists():
            logger.warning("Drive credentials not found at %s, local-only", creds_path)
            return
        try:
            from bcs.drive_store import get_drive_service, find_file
            self._drive_service = get_drive_service(creds_path)
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('file_name', 'zebra_trades.json')
            self._drive_file_id = find_file(self._drive_service, folder_id, file_name)
            self._drive_enabled = True
            logger.info("Drive enabled, file_id=%s", self._drive_file_id)
        except Exception as e:
            logger.warning("Drive init failed: %s. Local-only.", e)

    def _sync_from_drive(self):
        try:
            from bcs.drive_store import download_json
            if self._drive_file_id:
                # Network call OUTSIDE the lock (same rule as _mutate's upload).
                drive_data = download_json(self._drive_service, self._drive_file_id)
                # The merge+save is a read-modify-write on the shared file and
                # MUST hold the store lock: unlocked, a trade the other cron
                # writes between our read and our _save_local is clobbered —
                # the exact silent lost-write this lock exists to prevent.
                # Base is disk-merged-with-memory (disk is truth), never bare
                # self._trades, which goes stale the moment the other process
                # writes.
                with exclusive(cfg.LOCK_FILE):
                    base = self._merge(self._read_local(), self._trades)
                    merged = self._merge(base, drive_data)
                    self._trades = merged
                    self._save_local()
                self._last_sync_time = time.time()
                drive_vers = {t['id']: t.get('version', 0) for t in drive_data}
                merged_vers = {t['id']: t.get('version', 0) for t in merged}
                if drive_vers != merged_vers:
                    logger.info("Merge diverged from Drive, re-uploading")
                    self._upload_to_drive()
            else:
                logger.info("No zebra file on Drive yet, loading local")
                self._load_local()
        except LockTimeout:
            # Lock busy for 30s+ — do NOT fall through to _load_local, which
            # would immediately re-attempt the same busy lock for another 30s.
            # In-memory state is untouched and still sane; next sync retries.
            logger.warning("Drive sync skipped: store lock busy")
        except Exception as e:
            # The `with exclusive` above has already released on unwind, so the
            # _load_local fallback (which takes the lock itself) cannot deadlock.
            logger.warning("Drive sync failed: %s. Using local.", e)
            self._load_local()

    def _upload_to_drive(self):
        if not self._drive_enabled:
            return
        try:
            from bcs.drive_store import upload_json
            drive_cfg = self._config.get('google_drive', {})
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('file_name', 'zebra_trades.json')
            self._drive_file_id = upload_json(
                self._drive_service, folder_id, file_name,
                self._trades, self._drive_file_id
            )
            self._last_sync_time = time.time()
        except Exception as e:
            logger.error("Drive upload failed: %s. Local is safe.", e)

    def _read_local(self) -> list:
        if not cfg.LOCAL_FILE.exists():
            return []
        try:
            with open(cfg.LOCAL_FILE) as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError(f"Expected list, got {type(data).__name__}")
            return data
        except (json.JSONDecodeError, ValueError) as e:
            backup = cfg.LOCAL_FILE.with_suffix(
                f'.corrupt.{int(time.time())}.json'
            )
            try:
                cfg.LOCAL_FILE.rename(backup)
            except OSError:
                pass
            logger.critical("File CORRUPT (%s). Backed up to %s.", e, backup)
            self._flag_corruption(str(e), backup)
            return []

    def _flag_corruption(self, err: str, backup) -> None:
        """Leave a marker the MONITOR turns into a Telegram.

        Quarantine is the single highest-consequence event in this system and it
        was log-only. The store goes empty; `check_entered` hits
        `if not entered: return` and exits before `_alert_monitoring_blind` can
        ever be reached — that alert requires a NON-empty book, so the total
        failure is precisely the case it cannot report. Every open position
        stops being monitored and the only witness is one CRITICAL line in a log
        nobody is tailing. "Blind means Telegram" is the fleet rule.

        A marker file rather than a direct send: this module deliberately has no
        Telegram dependency, and the cron process exits between cycles so an
        in-memory flag would not survive to the alerting layer.
        """
        try:
            cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
            (cfg.LOG_DIR / 'zebra_store_corrupt.json').write_text(json.dumps({
                'at': datetime.now().isoformat(timespec='seconds'),
                'error': err,
                'backup': str(backup),
            }))
        except Exception as e:
            logger.error("Could not write the corruption marker: %s", e)

    def _load_local(self):
        # Under the lock: an unlocked startup read can land mid-save from the
        # other cron. On Linux that silently returns a stale snapshot; on
        # Windows the open handle makes the writer's os.replace fail outright.
        # Only safe here because _load_local is never called from inside
        # _mutate — flock on a second fd in the same process would deadlock.
        with exclusive(cfg.LOCK_FILE):
            self._trades = self._read_local()
        if self._trades:
            logger.info("Loaded %d trades from local", len(self._trades))
        else:
            logger.info("No local zebra file, starting empty")

    @staticmethod
    def _merge(base: list, incoming: list) -> list:
        by_id = {t['id']: t for t in base}
        for t in incoming:
            tid = t['id']
            if tid not in by_id:
                by_id[tid] = t
            elif t.get('version', 0) > by_id[tid].get('version', 0):
                by_id[tid] = t
        return sorted(by_id.values(), key=lambda t: t['id'])

    def _save_local(self):
        cfg.LOG_DIR.mkdir(exist_ok=True)
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(cfg.LOG_DIR), suffix='.tmp', prefix='zebra_'
            )
            with os.fdopen(fd, 'w') as f:
                fd = None
                json.dump(self._trades, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            # POSIX rename is indifferent to open handles, but on Windows a
            # reader holding the destination open makes this fail outright.
            # Callers are all under the store lock, so a collision here means a
            # stray unlocked reader — retry briefly rather than lose the write.
            for attempt in range(5):
                try:
                    os.replace(tmp_path, str(cfg.LOCAL_FILE))
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
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


# ── Singleton ────────────────────────────────────────────────────────────
_store: Optional[ZebraStore] = None


def get_store() -> ZebraStore:
    global _store
    if _store is None:
        _store = ZebraStore()
        _store.initialize()
    return _store
