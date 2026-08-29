"""Claude exit vetting for the order path.

Why this file exists
--------------------
`zebra/monitor.py` gates every price-driven exit through `_exit_cleared`
before it books anything. `bcs/spread_monitor.py` — the only code in the fleet
that can place a real order — has never had any vetting at all; its five
occurrences of "vet" are the word *veto*, in the LTP and spot-corroboration
guards.

So the exit bridge (`bcs/zebra_adapter.py`) created a hazard on its own: once
the order path can see the cohort, moving exits onto it WITHOUT carrying the
gate across would REMOVE a control that exists today, on the one path that can
lose real money. This module carries it across.

What it deliberately does not do
--------------------------------
**It does not re-implement the gate.** `zebra.vet.exit_gate` holds a great deal
of hard-won sequencing — the marker staleness rules, the ancient-PENDING
discard, the usage-block short-circuit, and the compare-and-swap in
`zebra.vet._set_exit_state` (named here rather than by line number, which
drifts) whose failure mode is "the NHPC direction exactly". A port would
be a second copy to keep correct (`feedback_copy_pasted_modules_fix_once`), so
this calls `zebra.monitor._exit_cleared`, which is the gate AS DEPLOYED,
escalation Telegram included.

**It vets zebra-store records only.** The bcs / bear_put / fallen_hero books
have no vet marker schema, and inventing one for them is not this change.
`store_type != 'zebra'` returns True with no log noise.

**It fails OPEN.** A dead vetting layer must never strand a live stop. Any
exception anywhere below returns True and says so loudly — the same contract
the kill switch has, and the same one `exit_gate` applies internally to a
timed-out agent. The deterministic guards are unchanged and still stand; this
layer is additive, never load-bearing
(`feedback_guards_need_the_inverse_review`).
"""
from __future__ import annotations

from typing import Optional

#: monitor close reason -> the vet's exit kind.
#:
#: EXPIRY_FORCE_CLOSE is deliberately ABSENT, matching zebra's ungated TIME:
#: those exits are calendar-driven rather than quote-driven, so there is no
#: suspect price for an agent to judge, and holding one on a pending verdict
#: would trade a known delivery-margin deadline for an unknown wait.
VET_KIND = {
    'SL_SPOT': 'spot_sl',
    'SL_SPREAD': 'debit_sl',
    'SL_TRAIL': 'trail',
    'TP': 'tp',
}


def vet_kind(reason: str) -> Optional[str]:
    """The vet's name for a monitor close reason, or None if ungated."""
    return VET_KIND.get(str(reason or '').upper())


def as_vet_quote(trade: dict, quote: Optional[dict]) -> dict:
    """`get_spread_value()`'s dict in the shape `zebra.vet` reads.

    Translated, never re-fetched. The gate must judge THE BOOK THE TRIGGER
    FIRED ON: a fresh fetch through zebra's own valuation would give the
    pre-filter a different read of a different moment, and a second book that
    happens to look clean would then skip vetting on a trigger that fired off a
    dirty one — precisely backwards.

    A missing quote maps to `reliable: False`, which makes `needs_exit_vet`
    return True. An exit with no book at all is the most suspicious kind there
    is, so the default must be "look at it", not "wave it through".
    """
    quote = quote or {}
    val = quote.get('spread')
    # ONE fallback, not two. This had a second `(None if quote else
    # 'no_quote')` on the line above until a mutation run showed the two were
    # indistinguishable: an absent dict already arrives here with `val is
    # None`, so the general rule below covers it and the special case never
    # fired. A guard you cannot observe failing is decorative
    # (`feedback_a_second_guard_you_cannot_observe_is_decorative`).
    unreliable = quote.get('unreliable')
    if val is None and not unreliable:
        unreliable = 'no_quote'
    return {
        'mid': val,
        'reliable': val is not None and not unreliable,
        'reason': unreliable,
        'legs': {
            'long': _leg(trade.get('long_symbol'), quote.get('long')),
            'short': _leg(trade.get('short_symbol'), quote.get('short')),
        },
        # Always False here. `get_spread_value` REFUSES a below-floor
        # valuation rather than clamping it (an estimate is not a price), so a
        # floored mid can never reach this function.
        'floored': False,
    }


def _leg(symbol, depth: Optional[dict]) -> dict:
    """One leg's top-of-book, in the shape the exit agent is asked to judge.

    `oi` is None, not 0: `get_option_depth` does not carry open interest and a
    zero would read as "no open interest", which is a finding rather than a
    gap. The agent re-quotes live via `zebra quote <ID>` for the numbers it
    needs to judge depth (`feedback_agent_needs_its_own_source`); this context
    is the snapshot the trigger actually fired on.
    """
    depth = depth or {}
    bid, ask = depth.get('bid'), depth.get('ask')
    mid = spread_pct = None
    if bid is not None and ask is not None and (bid or ask):
        mid = round((bid + ask) / 2, 2)
        if mid:
            spread_pct = round((ask - bid) / mid * 100, 1)
    return {'symbol': symbol, 'bid': bid, 'ask': ask, 'mid': mid,
            'oi': None, 'last': depth.get('ltp'),
            'spread_pct': spread_pct}


def exit_cleared(store, trade: dict, reason: str, quote: Optional[dict],
                 spot: float, dry_run: bool = False, log=None) -> bool:
    """May this exit place orders now? False = wait or hold, re-arm and retry.

    Called AFTER the deterministic guards (late-day, re-verify) and BEFORE
    `begin_close`. Both halves of that placement matter:

    * after the guards, so an exit the guards were going to abort anyway never
      spends an agent, and so the agent judges the RE-VERIFIED book;
    * before `begin_close`, for the same reason zebra calls it before
      `set_alert_flag` — that lock is consume-once, and burning it on an exit
      that does not execute strands the exit.

    **`incycle_wait=0`, and it is not an oversight.** M12 lets a CRON-PACED
    caller block in-line for a verdict it just requested, because zebra's next
    look at the marker is five minutes away. This engine's next look is FIVE
    SECONDS away, so the same verdict is picked up 22 polls later for free --
    and blocking the poll loop for two minutes would stop watching every OTHER
    open position, on all four books, to buy nothing. The optimisation is a
    property of the caller's cadence, so the caller states it.

    `dry_run` returns True WITHOUT consulting the gate. Dry run means "monitor
    everything, change nothing", and `exit_gate` is not read-only: it writes
    vet markers to the live store and spawns Claude agents. While the monitor
    is in dry run, zebra is still the engine that actually books these exits
    (see `exits_managed_externally`), so vetting here would also race zebra's
    own gate over one shared marker. The vet arms in the same step the orders
    do.
    """
    say = log or (lambda m: None)
    if trade.get('_store_type') != 'zebra':
        return True
    kind = vet_kind(reason)
    if kind is None:
        return True
    if dry_run:
        say(f"  EXIT VET skipped ({reason}) — dry run; zebra still owns this "
            f"trade's exits")
        return True
    try:
        from zebra.monitor import _exit_cleared as _gate
        raw = getattr(store, 'raw', store)
        ok = _gate(raw, trade, kind, as_vet_quote(trade, quote), spot,
                   dry_run=False, incycle_wait=0)
    except Exception as e:
        # FAIL OPEN, loudly. The deterministic guards already cleared this
        # exit; a broken vetting layer is not a reason to leave a stop unfilled.
        say(f"  EXIT VET ERROR ({reason} #{trade.get('id')}): {e} — "
            f"proceeding on the deterministic guards alone")
        return True
    if not ok:
        say(f"  EXIT VET HELD: {reason} #{trade.get('id')} "
            f"{trade.get('stock')} — no orders this cycle, trigger re-arms")
    return ok
