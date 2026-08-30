"""Present the zebra trade store through the interface `spread_monitor` uses.

Why an adapter and not a fourth store
-------------------------------------
`bcs/spread_monitor.py` is the only code in the fleet that can place a real
order, and until now it read three books — bcs, bear_put, fallen_hero. The BCS
cohort lives in a fourth, `logs/zebra_trades.json`, watched by
`zebra/monitor.py`, which is paper-only and has no order API at all. So the
cohort was watched by code that could not trade, and the code that could trade
could not see the cohort.

`ZebraStore` does not implement the interface the monitor consumes: it has
`get_entered` not `get_open_trades`, `mark_exited` not `update_trade_exit`, and
nothing at all for `begin_close` / `get_closing_trades` — the consume-once
close lock that keeps two processes from double-closing a position. Widening
either side would mean editing a live 460-record store or teaching the money
path a second schema. The adapter keeps both intact and puts the translation in
one file that can be tested on its own.

Two rules this file exists to enforce
-------------------------------------
**Cohort only.** The store holds 450 records from the dropped back-ratio
strategy. The order path must never see one. `in_cohort` from
`zebra.trade_store` is the single definition of that boundary — imported, never
re-derived.

**The exit rules travel with the TRADE.** The two engines do not agree: zebra
runs with the spot stop OFF (measured: a 3% stop cuts 40% of winners), a trail
anchored to max gain, and an unconditional 5-session time stop. The monitor
arms SL_SPOT as trigger #1, anchors its trail to 2x debit, and force-closes on
expiry day. Moving a position between engines must not silently re-arm a stop
its owner measured and switched off, so the adapter stamps the policy onto each
record and the monitor reads it from there.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from zebra import config as zcfg
from zebra.trade_store import in_cohort

logger = logging.getLogger(__name__)

#: zebra field -> the name `spread_monitor` reads. Renames only; nothing here
#: computes. A translation layer that derives a number can disagree with the
#: engine it is translating for, and then neither can be trusted.
FIELD_MAP = {
    'debit': 'net_debit',
    'width': 'spread_width',
    'tp_spot': 'target_spot',
    'debit_sl_value': 'sl_spread',
    # Fill-basis entry prices. The long was BOUGHT at the ask, the short SOLD
    # at the bid -- not a cosmetic choice: `entry_short_price` is what the
    # B21/B17 intrinsic floor derives its allowance from, and a mid there
    # would tighten the floor onto healthy books.
    'long_ask_entry': 'entry_long_price',
    'short_bid_entry': 'entry_short_price',
}

#: Exit policy for a cohort record. Read by the monitor instead of its own
#: module constants. Values are zebra's, because they are what this book was
#: measured with.
ZEBRA_EXIT_POLICY = {
    # Measured, not assumed: over 147 records a 3% spot stop cut 57/78 winners
    # at 2%, 31/78 at 3%, for a Rs 8.9L giveaway, while "caught" losers only
    # halved their depth. Spot ships as a VETO in this engine, never a trigger.
    'spot_sl_enabled': False,
    # Anchored to MAX GAIN (width - debit), not to debit. The two rules cross
    # at d/w = 1/3 and 34 of 42 records sit above it, so the gain-anchored one
    # arms earlier on ~80% of the book -- and it means the same thing across
    # spreads, which 2x-debit does not (43% of max gain at 30% d/w, 82% at 45%).
    'trail_policy': 'gain_anchored',
    # Trading SESSIONS before expiry, unconditional. Not the monitor's
    # warn-from-E-5-then-force-close-on-expiry-day, which fires FOUR SESSIONS
    # LATER -- so without this the handover would silently delete a stop.
    'time_policy': 'sessions_before_expiry',
    # The N the monitor reads instead of consulting zebra's config itself, so
    # the engine holding a trade does not need to know which book it came
    # from.
    #
    # **It is stamped at MAP time from the LIVE config, not frozen at entry**,
    # and the earlier wording here ("carried on the record") wrongly implied
    # otherwise. The consequence is real and was observed: moving
    # `time_sl_days_before_expiry` 5 -> 6 on 2026-08-29 retimed the SIX
    # already-open cohort positions one session earlier, on the next map.
    #
    # That direction is safe and is left deliberate — M10's rule is that the
    # session count is a FLOOR, never a ceiling, so a config change may only
    # pull a close EARLIER. What must not happen quietly is the reverse: a
    # LATER value would push open positions further into the delivery ramp
    # they were sized to clear. If this is ever raised, the open book needs
    # checking by hand, because nothing here will stop it.
    'time_stop_sessions': zcfg.TIME_SL_DAYS,
}


def map_trade(t: dict) -> dict:
    """One zebra record as `spread_monitor` expects to read it.

    A shallow copy with renames applied and the handful of fields the zebra
    schema simply does not carry filled in. The original keys are LEFT IN
    PLACE: `zebra.vet` and the digest read them by their own names, and
    stripping them would fork the record between the two readers.
    """
    out = dict(t)
    for src, dst in FIELD_MAP.items():
        if src in t and t[src] is not None:
            out[dst] = t[src]
    # Not in the zebra schema at all. Both are constants for this book: it is
    # stock options on NFO, and the underlying is the NSE equity.
    out.setdefault('exchange', 'NFO')
    if t.get('stock'):
        out.setdefault('spot_symbol', f"NSE:{t['stock']}")
    out.update(ZEBRA_EXIT_POLICY)
    return out


def _filled(v) -> Optional[float]:
    """A fill price, or None when the monitor had none to report.

    `_close_spread_inner` initialises both fill variables to 0.0 and the
    ALREADY_FLAT path leaves them there when `_find_last_fill_price` finds
    nothing in the order history. Zero is therefore "no price", not "traded at
    zero" — and persisting it under `price` would hand `zebra/fees.py` a
    fabricated book: it stops falling back to its estimate the moment it finds
    a price on both legs, so a zero would silently zero out the STT on the leg
    where STT actually lands. A missing price must LOOK missing.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _leg(price: Optional[float]) -> dict:
    """One leg of the persisted exit book."""
    return {'price': price, 'source': 'fill' if price is not None else None}


def _warn_if_unrecognised(reason: str, trade_id) -> None:
    """Say so, loudly, if this engine just emitted a reason no reader knows.

    The bug this closes was silent drift: the monitor's five reason strings
    meant nothing to `zebra.outcomes`, so a real stop scored as "no signal" and
    a take-profit cleared the arming gate. Catching it here puts the complaint
    next to the exit that caused it, in the log of the run that placed the
    order, instead of leaving it to be inferred from a gate weeks later.

    Wrapped whole. This is OBSERVABILITY sitting inside the only code path in
    the fleet that can place a real order, and an observability check that can
    raise is a new way to fail booking an exit that has already happened at the
    broker. It may complain; it may never interfere.
    """
    try:
        from zebra.outcomes import classify
        if not classify(reason)['known']:
            logger.warning(
                'Exit reason %r for cohort #%s is not in '
                'zebra.outcomes._EXIT_KIND. It is being STORED verbatim (that '
                'is correct — the record is forensic), but every reader will '
                'count it as nothing: no signal-quality label, and no credit '
                'at the arming gate. Add it to the map.', reason, trade_id)
    except Exception as e:                       # pragma: no cover - never fatal
        logger.debug('exit-reason vocabulary check skipped: %s', e)


class ZebraStoreAdapter:
    """`ZebraStore` wearing the BCS store interface. Cohort records only."""

    def __init__(self, store):
        self._store = store

    # -- reads ---------------------------------------------------------------

    def get_open_trades(self) -> List[dict]:
        """Cohort positions the monitor may manage, in monitor field names.

        NOT live references, unlike the BCS store. The monitor's writes go
        through `update_trade_fields` / `update_trade_exit` below, which reach
        the real record; handing out an aliased dict here would let a caller
        mutate a mapped copy and believe it had persisted.

        **Paper records ARE returned, and that is deliberate.** They must not
        be TRADED — `spread_monitor.close_spread` refuses them outright, and
        `update_trade_exit` below refuses to book one through this path — but
        withholding them here was tried on 2026-08-27 and reverted the same
        day, because this method is the WATCH path, not the order path:

          * `--list` reads it. Hiding the cohort here reproduces `b3fabf6`
            exactly — "Open: 0" with eight positions live — which is the
            misreport the bridge was built to end.
          * the dry-run evidence week reads it. `journal_report --compare`
            works by having the monitor poll the paper cohort, journal what it
            WOULD have done, and set that beside zebra's real paper booking.
            A monitor that cannot see the cohort has nothing to compare, and
            that comparison is the gate on arming anything.

        So the boundary is where the ORDER is, not where the read is. See
        `spread_monitor._record_says_paper`.
        """
        return [map_trade(t) for t in self._store.get_entered()
                if in_cohort(t)]

    def get_closing_trades(self) -> List[dict]:
        """Records stranded mid-close by a crash. RECOVERABLE — the sweep in
        `monitor_all` puts each one back to 'entered'.

        Deliberately NOT including 'partial_close'. The sweep calls
        `recover_closing_trade` on everything this returns and announces
        "Recovered from 'closing'. Re-monitoring."; the store refuses that
        transition for a frozen record, so widening this tuple would produce a
        daily Telegram claiming a recovery that did not happen. A frozen record
        is a DIFFERENT fact and gets its own method below.
        """
        return [map_trade(t) for t in self._store.load_trades()
                if t.get('status') == 'closing' and in_cohort(t)]

    def begin_recovery(self, trade_id: int, reason: str) -> bool:
        """M14 - the one door out of `partial_close`, for the recovery sweep.

        Straight through to the store, like `begin_close`. No cohort check
        here: the sweep selects what it acts on via `get_frozen_trades()`,
        which IS cohort-scoped, and a second filter downstream of the first
        would be a guard that cannot be observed failing
        (`feedback_a_second_guard_you_cannot_observe_is_decorative`).
        """
        return self._store.begin_recovery(trade_id, reason)

    def get_frozen_trades(self) -> List[dict]:
        """Cohort records frozen at 'partial_close' — live legs, no monitor.

        This is the state a close lands in when a leg failed AFTER orders went
        out: the short may be flat and the long not, or the book may be the
        flipped Feb-2026 shape. It is terminal by design — the position needs a
        human at the broker, and no automated path may put orders on top of it.

        What it must never be is INVISIBLE. `get_entered` skips it, so it
        leaves the monitored book; `get_closing_trades` skips it, so the
        crash-recovery sweep never mentions it; and until this method existed
        nothing in the bridge could name it at all. An unmonitored live
        position that nobody is told about is the failure that has cost real
        money here twice.

        Read-only, and no caller may treat these as open positions.
        """
        return [map_trade(t) for t in self._store.load_trades()
                if t.get('status') == 'partial_close' and in_cohort(t)]

    def get_residue_trades(self) -> List[dict]:
        """S3 - cohort records BOOKED EXITED that still show a live leg.

        The residue twin of `get_frozen_trades`, narrowed the same way and for
        the same reason: the sweep that reads this must never be handed a
        retired strategy's positions. The store method sees every generation;
        this sees the cohort.

        Mapped, because the residue sweep names legs by the BCS vocabulary
        (`short_symbol` / `long_symbol`) that `map_trade` produces. Handing it
        raw zebra records would give it a record with no legs it can read —
        which is exactly the false-clean this whole item is about.
        """
        return [map_trade(t) for t in self._store.load_trades()
                if t.get('status') == 'exited'
                and (t.get('reconcile_residue') or {}).get('state') == 'open'
                and in_cohort(t)]

    def get_entry_residue_trades(self) -> List[dict]:
        """Cohort records carrying an ENTRY residue — a leg an entry left.

        Cohort-narrowed like its post-close twin, and mapped for the same
        reason: the sweep names legs by the BCS vocabulary that `map_trade`
        produces.

        This is the ONE book that can actually hold one: `bcs/entry_executor.py`
        is only reached from `zebra/monitor._enter_as_bcs`. The other three
        answer the same question with an empty list rather than not answering
        it, so the sweep has no per-book special case.
        """
        return [map_trade(t) for t in self._store.load_trades()
                if (t.get('entry_residue') or {}).get('state') == 'open'
                and in_cohort(t)]

    def find_open_trade(self, stock: str, trade_id: Optional[int] = None):
        for t in self.get_open_trades():
            if trade_id is not None and t['id'] == trade_id:
                return t
            if trade_id is None and t.get('stock') == stock:
                return t
        return None

    def load_trades(self) -> List[dict]:
        """The raw zebra records, unscoped.

        WHOLE BOOK: this is the raw accessor, same rule as the store it wraps
        — scoping here would hide records from the merge, the id allocator and
        the recovery sweeps. The cohort-scoped views are the `get_*_trades`
        methods above, and `bcs/journal_report._load_zebra` scopes what it
        reads through here.
        """
        return self._store.load_trades()

    # -- writes --------------------------------------------------------------

    def begin_close(self, trade_id: int, reason: str) -> bool:
        """Take the close lock. False if this record is not open to be closed.

        The lock is what stops two processes placing the same closing order.
        It must persist BEFORE any order goes out, which is why this delegates
        to a store method that saves rather than setting a flag in memory.
        """
        return self._store.begin_close(trade_id, reason)

    def recover_closing_trade(self, trade_id: int):
        return self._store.recover_closing_trade(trade_id)

    def update_trade_exit(self, trade_id: int, exit_data: dict):
        """Book the exit. Translates the monitor's exit_data to `mark_exited`.

        `exit_debit` is the closing net debit PER SHARE, which is what the
        monitor computes as its exit spread value. Reason keeps the monitor's
        own vocabulary (SL_SPREAD, TP, ...) lowercased to match the store's --
        deliberately WITHOUT a `paper:` prefix, because a record closed through
        this path was closed by a real order.

        THE KEY NAMES ARE THE MONITOR'S, and they are asserted by
        `test_the_adapter_reads_the_keys_the_monitor_actually_writes`. Read
        `bcs/spread_monitor.py`, not this docstring, if they ever move: every
        `exit_data` dict it builds (the ALREADY_FLAT recovery path at
        `:2024` and the normal close path at `:2227`) carries

            exit_spot, exit_reason, exit_spread, short_fill, long_fill

        and NOTHING ELSE this method needs. It has never written `exit_value`
        or a bare `reason`, which is what this used to read: both `.get()`s
        returned None on every real call, so `exit_debit=None` sent
        `_apply_exit` down its "structure went to -debit" branch and booked
        MAX LOSS for every cohort exit — take-profits included — under
        `exit_reason='unknown'`. A silent -100% on a winner is worse than the
        crash it would have replaced, which is why this and the `mark_exited`
        precondition had to ship together.

        THE REASON IS STORED VERBATIM (lowercased), NOT NORMALISED. It is
        tempting to translate `SL_SPREAD` to zebra's `debit_sl` right here and
        give every reader one vocabulary — and it would be wrong. The record is
        FORENSIC: `already_flat_tp` says the monitor found both legs flat and
        recovered the price from order history rather than transacting it, and
        `expiry_force_close` says the 15:15 deadline fired, not that time ran
        out. Normalising at the write boundary destroys those facts in the one
        artefact that survives the process, and an option book cannot be
        reconstructed after the fact. Translation belongs at the READ boundary,
        where `zebra.outcomes.classify` does it from a single map.

        What DOES belong here is noticing when this engine emits a string that
        map has never heard of, at the moment it is written rather than weeks
        later when someone reads a gate. Detection only.

        NO PAPER CHECK HERE, and that was a deliberate reversal. A guard
        refusing a `paper: True` record was written on 2026-08-27 and taken
        back out: the only caller of this method is `_close_spread_inner`, and
        the only caller of THAT is `close_spread`, which refuses paper records
        before the vet, before the close lock and before any order. A second
        test of the same flag on the same record, downstream of the first and
        unreachable while the first stands, is the shape
        `feedback_a_second_guard_you_cannot_observe_is_decorative` describes —
        it cannot be observed failing, so it cannot be known to work.

        The property it was trying to buy is bought structurally instead:
        `test_the_paper_guard_sits_on_the_only_route_to_an_order` re-derives
        the call graph from the source and fails if a second route to booking
        ever appears.
        """
        # THE EXIT BOOK. The monitor reports the two fills as scalars; the
        # store persists a leg dict, and `zebra/fees.py:144` costs the exit
        # from `exit_legs[side]['price']` when it is there and falls back to a
        # decay estimate when it is not. These are REAL FILLS — the best exit
        # prices this system will ever hold — so they go in under 'price' and
        # the net figure stops being an estimate for bridged closes.
        #
        # An explicit `exit_legs` in exit_data wins: a caller that assembled a
        # full book knows more than two scalars do.
        legs = exit_data.get('exit_legs')
        if not legs:
            short_fill = _filled(exit_data.get('short_fill'))
            long_fill = _filled(exit_data.get('long_fill'))
            if short_fill is not None or long_fill is not None:
                # No symbols here on purpose. They are not in `exit_data` --
                # reading a key the producer does not write is the exact defect
                # this method just had, and a `None` symbol in the persisted
                # book is worse than no symbol at all. The record already
                # carries `long_symbol` / `short_symbol`.
                legs = {
                    'long': _leg(long_fill),
                    'short': _leg(short_fill),
                }
        reason = str(exit_data.get('exit_reason', 'unknown')).lower()
        _warn_if_unrecognised(reason, trade_id)
        # N14 — carry the approximation marker across the bridge. Without it a
        # bridged close that counted an already-flat leg at 0.00 lands in the
        # zebra book reading as exact, and every downstream reader (`pnl_net`,
        # the digest's cohort total, the arming gate's own evidence) treats a
        # figure wrong in a known direction as a measurement.
        return self._store.mark_exited(
            trade_id,
            exit_spot=exit_data.get('exit_spot'),
            exit_debit=exit_data.get('exit_spread'),
            reason=reason,
            exit_legs=legs,
            approximate=bool(exit_data.get('pnl_approximate')))

    def update_trade_fields(self, trade_id: int, **fields):
        return self._store.update_trade_fields(trade_id, **fields)

    def set_trade_status(self, trade_id: int, status: str, **extra):
        return self._store.set_trade_status(trade_id, status, **extra)

    # -- plumbing ------------------------------------------------------------

    def maybe_sync(self, force: bool = False):
        return self._store.maybe_sync(force=force)

    def list_trades(self, *a, **k):
        return self._store.list_trades(*a, **k)

    @property
    def raw(self):
        """The underlying ZebraStore, for callers that need its own vocabulary
        (the vetting layer reads and writes zebra's fields by their real
        names)."""
        return self._store


def get_adapter():
    """The cohort store, adapted — or None when zebra is not usable here.

    None rather than raising: an unavailable fourth book must not stop the
    monitor managing the other three. The caller logs it.
    """
    from zebra.trade_store import get_store
    return ZebraStoreAdapter(get_store())


def cohort_label() -> str:
    return zcfg.COHORT_START
