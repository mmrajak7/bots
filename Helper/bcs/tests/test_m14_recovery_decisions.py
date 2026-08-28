"""M14 step 5, the DECISION layer - what a frozen record is, and whether to act.

Classification and gating only. Nothing under test here places an order, reads
a book or touches a store, which is exactly why it is split out: the reasoning
is pure and can be enumerated, so the orchestrator that can actually lose money
has almost no reasoning left in it.

Two properties carry the whole design and are tested hardest:

**Classification comes from LIVE broker positions, never from the freeze-time
snapshot.** The human may have fixed it in the intervening minutes, and
self-resolution on a now-flat book - booking with zero orders - is the outcome
this mechanism most wants. A classifier reading its own freeze record could
never see it.

**Every gate fails CLOSED, and the polarity is the opposite of the arming
gate's.** For exit REASONS an unrecognised value must not count as a stop; for
PERMISSION TO PLACE ORDERS an unrecognised value must count as no. Same lesson,
opposite direction, and getting it backwards here places orders.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest bcs/tests/test_m14_recovery_decisions.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, time as dtime
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                               # noqa: E402

SHORT, LONG = 'TESTCO26SEP1390CE', 'TESTCO26SEP1340CE'
FROZE = '2026-08-28T10:00:00'
FROZE_TS = datetime.fromisoformat(FROZE).timestamp()


def _t(cause='unfilled', state='frozen', attempts=0, frozen_at=FROZE,
       legs=True, **extra):
    cf = {'cause': cause, 'state': state, 'attempts': attempts,
          'frozen_at': frozen_at, 'leg': 'short', 'reason': 'SL_SPREAD',
          'next_attempt_after': None, 'recovery_fills': {}}
    cf.update(extra.pop('close_failure', {}))
    t = {'id': 1, 'stock': 'TESTCO', 'close_failure': cf}
    if legs:
        t['short_symbol'], t['long_symbol'] = SHORT, LONG
    t.update(extra)
    return t


@pytest.fixture(autouse=True)
def _trading_on(monkeypatch):
    """The kill switch is a separate axis; pin it ON except where tested."""
    monkeypatch.setattr(sm, 'trading_enabled', lambda: True)


# ══ classification, from the LIVE book ══════════════════════════════════════

def test_a_live_short_with_no_long_is_the_unbounded_class():
    c, why = sm.classify_frozen(_t(), {SHORT: -700, LONG: 0})
    assert c == sm.RC_NAKED_SHORT
    assert 'no long' in why


def test_short_and_long_both_live_is_bounded():
    c, _ = sm.classify_frozen(_t(), {SHORT: -700, LONG: 700})
    assert c == sm.RC_BOUNDED


def test_an_over_hedged_book_after_a_partial_buyback_is_bounded():
    """200 short against 700 long. Over-hedged is the bounded side, and the
    close sequence deliberately prefers it to a naked short every time."""
    c, _ = sm.classify_frozen(_t(), {SHORT: -200, LONG: 700})
    assert c == sm.RC_BOUNDED


def test_a_naked_LONG_is_bounded_not_unbounded():
    """Its downside is the premium already paid. Treating it as urgent would
    pay through a garbage book to exit a position that cannot get worse."""
    c, why = sm.classify_frozen(_t(), {SHORT: 0, LONG: 700})
    assert c == sm.RC_BOUNDED
    assert 'premium already paid' in why


def test_an_all_flat_book_is_the_self_resolution_case():
    """THE outcome this mechanism most wants: the human fixed it, so the sweep
    books and resolves with ZERO orders."""
    c, _ = sm.classify_frozen(_t(), {SHORT: 0, LONG: 0})
    assert c == sm.RC_FLAT


def test_the_freeze_time_snapshot_does_not_decide():
    """Same record, different live book, different answer. If this ever fails
    the classifier has started reading the snapshot."""
    frozen = _t(close_failure={'leg': 'short'})
    assert sm.classify_frozen(frozen, {SHORT: -700, LONG: 0})[0] == \
        sm.RC_NAKED_SHORT
    assert sm.classify_frozen(frozen, {SHORT: 0, LONG: 0})[0] == sm.RC_FLAT


def test_a_missing_symbol_reads_as_flat_not_as_unknown():
    """`kite.positions()` omits flat legs entirely; a leg absent from the map
    is flat, and treating absence as unknown would strand every clean case."""
    assert sm.classify_frozen(_t(), {})[0] == sm.RC_FLAT


# ══ the states that must never order ════════════════════════════════════════

def test_a_flipped_freeze_never_orders():
    """The Feb-2026 amplifier: the book is not what the record says, so every
    quantity an order would be sized from is untrusted."""
    c, why = sm.classify_frozen(_t(cause='flipped'), {SHORT: -700, LONG: 0})
    assert c == sm.RC_NO_ORDERS and 'FLIPPED' in why


def test_a_leg_live_the_WRONG_WAY_never_orders_whatever_the_cause_said():
    """Flipped reached by a different door. The freeze cause said `unfilled`,
    but the book says the record is wrong about its own position - and the
    book wins."""
    c, why = sm.classify_frozen(_t(), {SHORT: +700, LONG: 700})
    assert c == sm.RC_NO_ORDERS and 'opposite' in why


def test_a_rejected_close_never_orders():
    """The broker's reason - margin, price band, frozen scrip - will repeat.
    Re-firing into it is the "firing into a broken state" failure."""
    c, why = sm.classify_frozen(_t(cause='rejected'), {SHORT: -700})
    assert c == sm.RC_NO_ORDERS and 'REJECT' in why.upper()


def test_a_legacy_freeze_with_no_record_never_orders():
    """Frozen before M14 shipped. Retrofitting automation onto a freeze nobody
    diagnosed is how helpfulness loses money."""
    t = {'id': 1, 'short_symbol': SHORT, 'long_symbol': LONG}
    c, why = sm.classify_frozen(t, {SHORT: -700})
    assert c == sm.RC_NO_ORDERS and 'legacy' in why


@pytest.mark.parametrize('cause', ['', None, 'probably_fine', 'unfilled '])
def test_an_unrecognised_cause_never_orders(cause):
    c, _ = sm.classify_frozen(_t(cause=cause), {SHORT: -700})
    assert c == sm.RC_NO_ORDERS


@pytest.mark.parametrize('state', ['', None, 'weird', 'FROZEN'])
def test_an_unrecognised_state_never_orders(state):
    """Polarity. For PERMISSION TO ORDER, unknown must mean no."""
    c, _ = sm.classify_frozen(_t(state=state), {SHORT: -700})
    assert c == sm.RC_NO_ORDERS


@pytest.mark.parametrize('state', ['escalated', 'resolved'])
def test_a_terminal_incident_never_orders_again(state):
    c, _ = sm.classify_frozen(_t(state=state), {SHORT: -700})
    assert c == sm.RC_NO_ORDERS


def test_a_record_with_no_legs_never_orders():
    c, why = sm.classify_frozen(_t(legs=False), {SHORT: -700})
    assert c == sm.RC_NO_ORDERS and 'no legs' in why


def test_fallen_hero_legs_are_classified_even_though_nothing_orders_on_them():
    """FH is traded by hand, so no order will follow - but a freeze that
    cannot be DESCRIBED is exactly the invisible position M14 is about, and
    the alert is produced from the classification."""
    t = _t(legs=False, short_call_symbol='SC', long_put_symbol='LP')
    assert sm.classify_frozen(t, {'SC': -400, 'LP': 0})[0] == sm.RC_NAKED_SHORT
    assert sm.classify_frozen(t, {'SC': 0, 'LP': 400})[0] == sm.RC_BOUNDED


# ══ grace: free for bounded, never for a naked short ════════════════════════

def test_a_naked_short_gets_no_grace_at_all():
    """Waiting buys nothing - a human would buy it back, which is what the
    machine will do - and can cost a lakh."""
    assert sm.recovery_grace_sec(sm.RC_NAKED_SHORT) == 0


def test_a_bounded_position_gets_the_full_grace_window():
    assert sm.recovery_grace_sec(sm.RC_BOUNDED) == 300


def test_the_naked_short_backoff_is_shorter_than_the_bounded_one():
    assert (sm.recovery_wait_sec(sm.RC_NAKED_SHORT)
            < sm.recovery_wait_sec(sm.RC_BOUNDED))


# ══ the gate ════════════════════════════════════════════════════════════════

def _gate(trade, rclass, **kw):
    kw.setdefault('now', FROZE_TS + 10_000)
    kw.setdefault('now_time', dtime(11, 0))
    return sm.recovery_gate(trade, rclass, **kw)


def test_a_clean_bounded_record_past_grace_is_allowed():
    ok, why = _gate(_t(), sm.RC_BOUNDED)
    assert ok and 'attempt 1 of 3' in why


def test_inside_the_grace_window_it_waits():
    ok, why = _gate(_t(), sm.RC_BOUNDED, now=FROZE_TS + 100)
    assert not ok and 'grace' in why


def test_a_naked_short_is_allowed_immediately():
    ok, _ = _gate(_t(), sm.RC_NAKED_SHORT, now=FROZE_TS + 1)
    assert ok


def test_spent_attempts_close_the_gate():
    ok, why = _gate(_t(attempts=3), sm.RC_BOUNDED)
    assert not ok and 'attempts spent' in why


def test_attempts_are_per_incident_so_a_new_day_does_not_refill_them():
    """A freeze at 15:10 with attempts spent does NOT get three fresh ones
    tomorrow. Deliberately unlike the time stop, whose attempts reset each
    session - a time stop is a daily deadline, a frozen close is one incident."""
    ok, _ = _gate(_t(attempts=3), sm.RC_BOUNDED, now=FROZE_TS + 86_400 * 3)
    assert not ok


def test_the_backoff_is_respected():
    t = _t(close_failure={'next_attempt_after': FROZE_TS + 20_000})
    ok, why = _gate(t, sm.RC_BOUNDED)
    assert not ok and 'backing off' in why


def test_the_kill_switch_closes_the_gate(monkeypatch):
    """Recovery obeys it like every other order path."""
    monkeypatch.setattr(sm, 'trading_enabled', lambda: False)
    ok, why = _gate(_t(), sm.RC_NAKED_SHORT, now=FROZE_TS + 1)
    assert not ok and 'kill switch' in why


def test_recovery_is_dark_for_the_first_fifteen_minutes():
    """Both real-money incidents were opening prints."""
    ok, why = _gate(_t(), sm.RC_NAKED_SHORT, now=FROZE_TS + 1,
                    market_settled=False)
    assert not ok and 'not settled' in why


def test_a_bounded_class_stops_at_the_normal_cutoff():
    ok, why = _gate(_t(), sm.RC_BOUNDED, now_time=dtime(15, 22))
    assert not ok and 'cutoff' in why


def test_a_naked_short_may_still_act_after_the_normal_cutoff():
    """It gets the hard reduce-only cutoff, not the normal one - carrying a
    naked short overnight is the worse outcome."""
    assert _gate(_t(), sm.RC_NAKED_SHORT, now_time=dtime(15, 22))[0]


def test_nothing_acts_past_the_hard_cutoff():
    for rc in (sm.RC_BOUNDED, sm.RC_NAKED_SHORT):
        assert not _gate(_t(), rc, now_time=dtime(15, 40))[0]


@pytest.mark.parametrize('rclass', [sm.RC_NO_ORDERS, sm.RC_FLAT])
def test_the_no_order_classes_are_refused_by_the_gate_too(rclass):
    """Belt and braces on the money path: the classifier already says so, and
    the gate says it again where the order would be placed."""
    ok, why = _gate(_t(), rclass)
    assert not ok and 'never orders' in why


def test_an_unreadable_frozen_at_cannot_prove_the_grace_window_passed():
    ok, why = _gate(_t(frozen_at='not a date'), sm.RC_BOUNDED)
    assert not ok and 'unreadable' in why


def test_a_terminal_incident_is_refused_by_the_gate_independently():
    """The classifier catches this first; if that check is ever loosened the
    gate must still hold."""
    ok, why = _gate(_t(state='escalated'), sm.RC_BOUNDED)
    assert not ok and 'escalated' in why


def test_every_refusal_says_why():
    """A refusal nobody can read is a position nobody looks at - the failure
    one level up."""
    cases = [(_t(attempts=3), sm.RC_BOUNDED, {}),
             (_t(), sm.RC_BOUNDED, {'now': FROZE_TS + 1}),
             (_t(state='escalated'), sm.RC_BOUNDED, {}),
             (_t(), sm.RC_NO_ORDERS, {}),
             (_t(), sm.RC_BOUNDED, {'now_time': dtime(15, 40)})]
    for trade, rc, kw in cases:
        ok, why = _gate(trade, rc, **kw)
        assert not ok and why and why.strip()


# ══ close_leg's new controls (step 4) ═══════════════════════════════════════

def test_the_attempt_count_cannot_be_widened_past_the_ceiling():
    """A caller must not be able to raise the order ceiling this function
    exists to bound."""
    import inspect
    src = inspect.getsource(sm.close_leg)
    assert 'min(int(attempts), MAX_RETRIES)' in src


def test_a_nonsense_attempt_count_does_not_silently_disable_the_close():
    """0 or a negative would place no order and return None, which reads to
    the caller as "the leg was not tradeable" - a lie about the book."""
    import inspect
    src = inspect.getsource(sm.close_leg)
    assert 'max(\n        1, min(' in src or 'max(1, min(' in src


def test_pay_through_defaults_to_todays_behaviour():
    """Every existing caller must be unchanged by step 4."""
    import inspect
    sig = inspect.signature(sm.close_leg)
    assert sig.parameters['allow_pay_through'].default is True
    assert sig.parameters['attempts'].default is None
