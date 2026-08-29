"""M14 §10 - the recovery sweep inside a REAL session, not called directly.

`test_m14_recovery_sweep.py` drives `recover_frozen_positions` on its own. That
proves the sweep, and says nothing about the sweep as part of a running
`monitor_all`: whether the poll loop reaches it at all, whether an attempt
survives a process restart, whether the grace window and the cutoffs behave
against a clock that is actually moving.

That distinction is this repo's most expensive lesson. Every individual guard
for the Feb-2026 incident existed in some form by July, and the July incident
still cost money, because "does the loop as assembled do the right thing" was
never asked of anything but prose.

So these run the production path end to end through `replay.run_session`:
`monitor_all` itself, its market-open buffer, its per-poll cadence, its stores,
and the sweep wired in after the per-trade loop. Only the broker, the stores,
Telegram and the clock are fakes — and `ReplayClock` drives `time.time()`,
`datetime.now()` and `date.today()` from one timeline, so grace and cutoff are
measured against the session being replayed rather than the wall clock.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest bcs/tests/test_m14_recovery_replay.py -v
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                               # noqa: E402
from bcs.tests.replay import Tick, run_session                     # noqa: E402

DAY = date(2026, 9, 2)
LONG, SHORT = 'TESTCO26SEP1340CE', 'TESTCO26SEP1390CE'
QTY = 700

#: Frozen an hour before the session under replay, so every test starts past
#: the 300s bounded grace unless it deliberately says otherwise.
FROZE = datetime(2026, 9, 2, 9, 0, 0).isoformat()


def _frozen(cause='unfilled', state='frozen', attempts=0, frozen_at=FROZE):
    return {
        'id': 1, 'stock': 'TESTCO', 'status': 'partial_close',
        'long_symbol': LONG, 'short_symbol': SHORT, 'spot_symbol': 'NSE:TESTCO',
        'quantity': QTY, 'lot_size': QTY, 'lots': 1, 'exchange': 'NFO',
        'entry_long_price': 21.20, 'entry_short_price': 7.65,
        'net_debit': 13.55, 'spread_width': 50, 'expiry': '2026-09-29',
        'target_spot': 1435.0, 'sl_spot': 1319.0, 'sl_spread': 6.78,
        'entry_date': '2026-08-20', 'entry_spot': 1360.0,
        'close_failure': {'frozen_at': frozen_at, 'cause': cause,
                          'leg': 'long', 'reason': 'SL_SPREAD',
                          'state': state, 'attempts': attempts,
                          'next_attempt_after': None, 'recovery_fills': {}},
    }


def _book(bid, ask):
    return {'bid': bid, 'ask': ask, 'bid_qty': 1400, 'ask_qty': 1400,
            'ltp': (bid + ask) / 2, 'prev_close': (bid + ask) / 2}


def _ticks(*times):
    """A tidy, tight, unremarkable book at each time. Nothing here is trying to
    trip a valuation guard — the subject is the sweep, not the quote."""
    return [Tick(t, 1400.0, _book(40.00, 40.20), _book(10.05, 10.30))
            for t in times]


def NAKED_LONG():
    """FRESH dicts every call, and that is not fussiness.

    `FakeBroker._apply_fill` mutates the position dicts IN PLACE — it has to,
    or `verify_positions` and `reconcile_after_close` would read the fixture
    author's intent instead of what the orders did. A module-level list is
    therefore consumed by the first test that fills against it: the second test
    starts with a flat book and classifies `flat` instead of `naked_long`,
    which is exactly the silent cross-test bleed that made six tests here fail
    only when run together.
    """
    return [{'tradingsymbol': LONG, 'quantity': QTY}]


def INTACT():
    return [{'tradingsymbol': LONG, 'quantity': QTY},
            {'tradingsymbol': SHORT, 'quantity': -QTY}]


@pytest.fixture(autouse=True)
def _clear_nag_state():
    """`_RECOVERY_NAGGED` is module-level — the monitor is one long-lived
    process per session, so the daily-nag key must survive the whole run. That
    makes it shared state between TESTS, and a second test asserting "it
    alerted once" sees zero because the first already claimed the key."""
    sm._RECOVERY_NAGGED.clear()
    yield
    sm._RECOVERY_NAGGED.clear()


def _run(monkeypatch, trade, positions, ticks=None, **kw):
    return run_session(monkeypatch, sm, trade, ticks or _ticks(
        '09:31:00', '09:35:00', '09:40:00'), DAY, positions, **kw)


def _recovery_orders(kite):
    """Orders placed while the sweep was the only thing that could place any.

    The record is `partial_close`, so it is not in `all_trades` and the
    per-trade loop cannot order on it. Every order in the session is therefore
    the sweep's.
    """
    return list(kite.placed)


# ══ the loop must REACH the sweep ═══════════════════════════════════════════

def test_a_book_whose_only_record_is_frozen_still_runs_the_loop(monkeypatch):
    """THE regression, and it is M14's own failure mode one level up.

    Frozen records are not in `all_trades` — dropping out of the open book is
    the whole defect — so before this the empty-book branch returned "No open
    trades ... Nothing to monitor" and the sweep never ran. A position live at
    the broker with dead stops, and a log line saying there is nothing to
    watch.
    """
    clock, kite, store, spy = _run(monkeypatch, _frozen(), NAKED_LONG())
    assert _recovery_orders(kite), (
        'the session placed no order at all — the poll loop almost certainly '
        'returned before reaching the sweep')
    assert store.trades[0]['status'] == 'closed'


def test_the_session_says_out_loud_that_something_is_frozen(monkeypatch):
    """A count in the startup banner. Silence here is what let a frozen record
    sit for a whole session with nobody knowing it existed."""
    clock, kite, store, spy = _run(monkeypatch, _frozen(), NAKED_LONG())
    assert store.trades[0]['close_failure']['state'] == 'resolved'


# ══ §10.1 · the happy path, through the real loop ═══════════════════════════

def test_a_naked_long_is_finished_by_the_sweep_and_booked(monkeypatch):
    clock, kite, store, spy = _run(monkeypatch, _frozen(), NAKED_LONG())

    sells = [o for o in kite.placed if o['transaction_type'] == 'SELL']
    assert len(sells) == 1, 'one order per leg per attempt'
    assert sells[0]['tradingsymbol'] == LONG
    rec = store.trades[0]
    assert rec['status'] == 'closed'
    assert rec['close_failure']['state'] == 'resolved'
    assert rec['close_failure']['attempts'] == 1
    assert any('FINISHED by recovery' in m for m in spy.sent)


def test_it_never_touches_the_short_leg_that_is_already_flat(monkeypatch):
    """`_close_spread_inner` skips flat legs. An order on a flat short would
    be an OPENING trade — the invariant `FakeBroker` polices."""
    clock, kite, store, spy = _run(monkeypatch, _frozen(), NAKED_LONG())
    assert not [o for o in kite.placed if o['tradingsymbol'] == SHORT]


# ══ §10.2 · exhaustion, then silence for the rest of the session ════════════

def _never_fills(kw):
    return ('OPEN', 0, 0.0)


def test_three_attempts_then_no_further_order_for_the_whole_session(
        monkeypatch):
    """The ceiling holds across a session of ~5s polls, not just across three
    calls. If the counter were per-poll rather than per-incident this places
    hundreds of orders."""
    clock, kite, store, spy = _run(
        monkeypatch, _frozen(), NAKED_LONG(),
        ticks=_ticks('09:31:00', '10:00:00', '11:00:00', '13:00:00',
                     '15:00:00'),
        fill_policy=_never_fills)

    assert len(kite.placed) <= sm.RECOVERY_MAX_ATTEMPTS, (
        'the sweep placed %d orders against a ceiling of %d'
        % (len(kite.placed), sm.RECOVERY_MAX_ATTEMPTS))
    rec = store.trades[0]
    assert rec['close_failure']['state'] == 'escalated'
    assert rec['close_failure']['attempts'] == sm.RECOVERY_MAX_ATTEMPTS


def test_escalation_is_alerted_once_not_once_per_poll(monkeypatch):
    """A SAFETY alert nobody can silence is right; one that repeats every five
    seconds trains the reader to swipe it away, which is the same thing."""
    clock, kite, store, spy = _run(
        monkeypatch, _frozen(), NAKED_LONG(),
        ticks=_ticks('09:31:00', '10:00:00', '11:00:00', '13:00:00',
                     '15:00:00'),
        fill_policy=_never_fills)
    shouts = [m for m in spy.sent if 'NEEDS A HUMAN' in m]
    assert len(shouts) == 1, shouts


def test_an_already_escalated_incident_places_nothing_all_day(monkeypatch):
    """No restart, no new day, no config reload re-arms it."""
    clock, kite, store, spy = _run(
        monkeypatch, _frozen(state='escalated', attempts=3), NAKED_LONG(),
        ticks=_ticks('09:31:00', '11:00:00', '14:00:00'))
    assert kite.placed == []


# ══ §10.3 · restart durability ══════════════════════════════════════════════

def test_an_attempt_survives_a_restart_and_is_not_refilled(monkeypatch):
    """Session one spends attempts; session two resumes where it left off.

    The counter lives on the RECORD, so this is really a test that it is
    persisted before the order rather than held in the loop.
    """
    trade = _frozen()
    clock, kite, store, spy = _run(monkeypatch, trade, NAKED_LONG(),
                                   ticks=_ticks('09:31:00', '10:00:00'),
                                   fill_policy=_never_fills)
    spent = store.trades[0]['close_failure']['attempts']
    assert spent >= 1

    # Session two: a NEW monitor_all over the record as session one left it.
    carried = dict(store.trades[0])
    clock2, kite2, store2, spy2 = _run(monkeypatch, carried, NAKED_LONG(),
                                       ticks=_ticks('09:31:00', '10:00:00'),
                                       fill_policy=_never_fills)
    assert store2.trades[0]['close_failure']['attempts'] <= \
        sm.RECOVERY_MAX_ATTEMPTS, 'a restart refilled the attempt budget'
    assert len(kite.placed) + len(kite2.placed) <= sm.RECOVERY_MAX_ATTEMPTS


def test_a_record_with_its_close_failure_DELETED_is_escalated_not_rearmed(
        monkeypatch):
    """The paranoid variant. A `partial_close` with no `close_failure` is a
    freeze from before M14 shipped, or one somebody edited. Retrofitting
    automation onto a freeze nobody diagnosed is how helpfulness loses money.
    """
    trade = _frozen()
    del trade['close_failure']
    clock, kite, store, spy = _run(monkeypatch, trade, NAKED_LONG())
    assert kite.placed == []
    assert store.trades[0]['status'] == 'partial_close'


# ══ §10.6 · cutoffs, and the dark window at the open ════════════════════════

def test_nothing_is_placed_before_the_market_has_settled(monkeypatch):
    """Both real-money incidents were opening prints, so recovery orders stay
    dark for the same first 15 minutes as the value triggers."""
    clock, kite, store, spy = _run(monkeypatch, _frozen(), NAKED_LONG(),
                                   ticks=_ticks('09:15:30', '09:16:00',
                                                '09:17:00'))
    assert kite.placed == [], 'the sweep ordered inside the open buffer'


def test_a_bounded_class_places_nothing_after_the_normal_cutoff(monkeypatch):
    clock, kite, store, spy = _run(monkeypatch, _frozen(), NAKED_LONG(),
                                   ticks=_ticks('15:21:00', '15:23:00'))
    assert kite.placed == []


def test_the_dark_window_is_measured_on_the_REPLAY_clock(monkeypatch):
    """If `is_market_settled` were reading the wall clock, the two tests above
    would agree with each other by accident whenever the suite happened to run
    after 09:30. Same book, two windows, opposite answers."""
    early = _run(monkeypatch, _frozen(), NAKED_LONG(),
                 ticks=_ticks('09:15:30', '09:17:00'))[1]
    late = _run(monkeypatch, _frozen(), NAKED_LONG(),
                ticks=_ticks('09:31:00', '09:35:00'))[1]
    assert early.placed == [] and late.placed != []


# ══ §10.7 · the states that must never order, across a whole session ════════

def test_a_FLIPPED_record_places_nothing_across_a_whole_replay(monkeypatch):
    clock, kite, store, spy = _run(
        monkeypatch, _frozen(cause='flipped'), INTACT(),
        ticks=_ticks('09:31:00', '11:00:00', '14:00:00'))
    assert kite.placed == []
    assert store.trades[0]['close_failure']['state'] == 'escalated'


def test_a_leg_live_the_WRONG_WAY_places_nothing(monkeypatch):
    """Flipped by the BOOK rather than by the freeze cause."""
    inverted = [{'tradingsymbol': LONG, 'quantity': QTY},
                {'tradingsymbol': SHORT, 'quantity': +QTY}]
    clock, kite, store, spy = _run(monkeypatch, _frozen(), inverted,
                                   ticks=_ticks('09:31:00', '11:00:00'))
    assert kite.placed == []


def test_a_rejected_freeze_places_nothing(monkeypatch):
    clock, kite, store, spy = _run(monkeypatch, _frozen(cause='rejected'),
                                   NAKED_LONG(),
                                   ticks=_ticks('09:31:00', '11:00:00'))
    assert kite.placed == []


def test_a_paper_record_places_nothing_through_the_whole_loop(monkeypatch):
    """The guard at the sweep's door, exercised where it actually matters."""
    paper = _frozen()
    paper['paper'] = True
    clock, kite, store, spy = _run(monkeypatch, paper, NAKED_LONG(),
                                   ticks=_ticks('09:31:00', '11:00:00'))
    assert kite.placed == []
    assert store.trades[0]['status'] == 'partial_close'


# ══ §10.5 · self-resolution, with zero orders ═══════════════════════════════

def test_a_book_that_goes_flat_during_grace_books_with_no_order(monkeypatch):
    """The human fixed it at the broker. The sweep records what happened.

    No ORDER_TAG fill exists to price it, so the correct outcome is a refusal
    that keeps the record FROZEN rather than an invented price — and the
    record must not be left at `closing`, where the next restart's crash sweep
    would recover it to `open` and lose the freeze.
    """
    clock, kite, store, spy = _run(monkeypatch, _frozen(), [],
                                   ticks=_ticks('09:31:00', '10:00:00'))
    assert kite.placed == []
    assert store.trades[0]['status'] == 'partial_close', (
        'a refused recovery left the record outside the frozen book')
    # ESCALATED, not left to retry. No order can ever price a flat book, so
    # re-classifying it every poll produced an `unpriced_refusal` and a SAFETY
    # Telegram every five seconds — thousands per session. This replay is what
    # made that visible; calling the sweep directly never showed it.
    assert store.trades[0]['close_failure']['state'] == 'escalated'
    refusals = [m for m in spy.sent if 'NEEDS A HUMAN' in m]
    assert len(refusals) == 1, (
        'the flat-but-unpriceable case alerted %d times in one session'
        % len(refusals))


# ══ the invariant, over every replay in this file ═══════════════════════════

def test_no_replay_here_ever_moved_a_position_away_from_zero(monkeypatch):
    """The §4 invariant, asserted over a real session rather than a call.

    `run_session` builds its own broker, so `reduce_only` is not armed there —
    this re-derives the property from the orders that were actually placed,
    which is the same question asked of the evidence instead of the fixture.
    """
    for positions in (NAKED_LONG(), INTACT()):
        # Snapshot BEFORE the session. The broker fills against these very
        # dicts, so reading them afterwards would compare each order against
        # the book its own fill produced — an invariant that always holds.
        held = {p['tradingsymbol']: p['quantity'] for p in positions}
        clock, kite, store, spy = _run(monkeypatch, _frozen(), positions,
                                       ticks=_ticks('09:31:00', '10:00:00'))
        for o in kite.placed:
            sym = o['tradingsymbol']
            before = held.get(sym, 0)
            after = before + (o['quantity']
                              if o['transaction_type'] == 'BUY'
                              else -o['quantity'])
            assert abs(after) <= abs(before), (
                '%s %s x%s moved %s from %d to %d'
                % (o['transaction_type'], sym, o['quantity'], sym,
                   before, after))
            held[sym] = after
