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

from typing import List, Optional

from zebra import config as zcfg
from zebra.trade_store import in_cohort

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
    # The N, carried on the record rather than read from zebra's config by the
    # monitor. Same rule as every other field here: the exit policy travels
    # with the TRADE, so the engine holding it does not need to know which
    # book it came from.
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
        """
        return [map_trade(t) for t in self._store.get_entered()
                if in_cohort(t)]

    def get_closing_trades(self) -> List[dict]:
        return [map_trade(t) for t in self._store.load_trades()
                if t.get('status') == 'closing' and in_cohort(t)]

    def find_open_trade(self, stock: str, trade_id: Optional[int] = None):
        for t in self.get_open_trades():
            if trade_id is not None and t['id'] == trade_id:
                return t
            if trade_id is None and t.get('stock') == stock:
                return t
        return None

    def load_trades(self) -> List[dict]:
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
        """
        return self._store.mark_exited(
            trade_id,
            exit_spot=exit_data.get('exit_spot'),
            exit_debit=exit_data.get('exit_value'),
            reason=str(exit_data.get('reason', 'unknown')).lower(),
            exit_legs=exit_data.get('exit_legs'))

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
