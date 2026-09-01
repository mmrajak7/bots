"""M14 step 5b - the sweep that actually finishes, or escalates, a frozen close.

The orchestrator. It decides nothing on its own: `classify_frozen` says what
the record is, `recovery_gate` says whether anything may be done, and the close
itself is `_close_spread_inner` - already position-driven, reduce-only,
skip-flat-legs, flip-guarded and unpriced-refusing. What is tested here is the
wiring between those three, which is where an orchestrator can still lose money
even when every part it calls is correct.

Pre-fix every one of these fails trivially, because a frozen record produced
NOTHING: no second alert, no order, no event, ever. That is the defect.

The invariant from the design's §4 is armed on every fixture here
(`reduce_only=True`), so any order that moved a position away from zero would
raise `ReduceOnlyViolation` out of `FakeBroker` rather than being asserted
about afterwards.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest bcs/tests/test_m14_recovery_sweep.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                               # noqa: E402
from bcs.tests.fakes import (FakeBroker, FakeClock, MemoryStore,   # noqa: E402
                             TelegramSpy)
from bcs.tests.test_d2_partial_close_residue import (              # noqa: E402
    B_LONG, B_QTY, B_SHORT, BCS_BOOKS, _LegScript, _complete, _partial)

FROZE = '2026-08-28T10:00:00'
FROZE_TS = datetime.fromisoformat(FROZE).timestamp()
PAST_GRACE = FROZE_TS + 10_000


def _frozen(cause='unfilled', state='frozen', attempts=0, paper=False,
            leg='long'):
    return {
        'id': 1, 'stock': 'TESTCO', 'status': 'partial_close',
        'long_symbol': B_LONG, 'short_symbol': B_SHORT,
        'quantity': B_QTY, 'exchange': 'NFO', 'net_debit': 13.55,
        'spread_width': 50, 'spot_symbol': 'NSE:TESTCO', 'paper': paper,
        'close_failure': {
            'frozen_at': FROZE, 'cause': cause, 'leg': leg,
            'reason': 'SL_SPREAD', 'state': state, 'attempts': attempts,
            'next_attempt_after': None, 'recovery_fills': {}},
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """AUTOUSE, and the Telegram spy is part of it deliberately.

    The sweep alerts on nearly every branch, so a test that forgets the spy
    reaches the real `requests.post` — a unit test making a network call to a
    live bot. Installing it here makes that unreachable rather than remembered.
    """
    FakeClock().install(monkeypatch, sm)
    monkeypatch.setattr(sm, 'trading_enabled', lambda: True)
    monkeypatch.setattr(sm, 'reconcile_after_close', lambda *a, **k: True)
    sm._RECOVERY_NAGGED.clear()
    return TelegramSpy().install(monkeypatch, sm)


@pytest.fixture
def spy(_env):
    return _env


def _run(store, kite, monkeypatch, script=None, orders_allowed=True,
         now=PAST_GRACE, dry_run=False, label='BCS'):
    if script is not None:
        monkeypatch.setattr(sm, 'close_leg', script)
    return sm.recover_frozen_positions(
        kite, [(label, store, orders_allowed)], dry_run,
        nagged=set(), now=now, market_settled=True)


def _broker(positions):
    return FakeBroker(books=BCS_BOOKS, positions=positions, reduce_only=True)


def _events(caplog_text):
    return [l.split('EVENT ', 1)[1].split()[0]
            for l in caplog_text.splitlines() if 'EVENT ' in l]


# ══ the happy path ══════════════════════════════════════════════════════════

def test_a_naked_long_is_sold_and_the_incident_resolves(spy, monkeypatch,
                                                        capsys):
    """S9. Short already flat, long still live: one recovery SELL, booked."""
    store = MemoryStore(trades=[_frozen()])
    kite = _broker([{'tradingsymbol': B_LONG, 'quantity': B_QTY}])
    script = _LegScript(**{B_LONG: [_complete(B_QTY, 40.00)]})

    assert _run(store, kite, monkeypatch, script) == 1

    assert [c['symbol'] for c in script.calls] == [B_LONG]
    assert script.calls[0]['txn'] == 'SELL'
    rec = store.trades[0]
    assert rec['status'] == 'closed'
    assert rec['close_failure']['state'] == 'resolved'
    assert rec['close_failure']['attempts'] == 1
    assert any('FINISHED by recovery' in m for m in spy.sent)


def _tagged(sym, txn, price, qty=B_QTY):
    """An order the SYSTEM placed, in the broker's book, COMPLETE.

    `find_recoverable_fill` only ever adopts an ORDER_TAG fill of ours; an
    untagged one could be anybody's, and adopting it would price a close on a
    trade this system did not make.
    """
    return {'order_id': 'x', 'tradingsymbol': sym, 'transaction_type': txn,
            'quantity': qty, 'price': price, 'tag': 'BCS_MON',
            'status': 'COMPLETE', 'filled_quantity': qty,
            'average_price': price, 'status_message': ''}


def test_a_flat_book_books_with_ZERO_orders(spy, monkeypatch):
    """The self-resolution the grace window exists to allow: a human fixed it
    at the broker, and the sweep simply records what happened."""
    store = MemoryStore(trades=[_frozen()])
    kite = _broker([])
    kite.order_book = [_tagged(B_SHORT, 'BUY', 10.00),
                       _tagged(B_LONG, 'SELL', 40.00)]
    script = _LegScript()

    assert _run(store, kite, monkeypatch, script) == 1

    assert script.calls == [], 'a flat book must produce no order at all'
    assert kite.placed == []
    assert store.trades[0]['close_failure']['state'] == 'resolved'
    assert any('No recovery order was placed' in m for m in spy.sent)


def test_a_flat_book_with_NO_tagged_fill_refuses_and_stays_frozen(spy,
                                                                  monkeypatch):
    """It must not invent a price - and it must not lose the incident either.

    `_refuse_unpriced_close` parks the record at `closing`, which is right on
    the normal path (the crash sweep restores it to `open`) and WRONG here: the
    record came from `partial_close`, and a restart would flip it to `open`,
    dropping it out of `get_frozen_trades()` with a stale `close_failure`
    nobody reads. The frozen position would look healthy again - the exact
    invisibility M14 exists to end, manufactured by M14."""
    store = MemoryStore(trades=[_frozen()])
    kite = _broker([])
    script = _LegScript()

    assert _run(store, kite, monkeypatch, script) == 0
    assert store.trades[0]['status'] == 'partial_close', (
        'a refused recovery left the record at `closing`; the next restart '
        'would recover it to `open` and lose the freeze')
    assert store.trades[0]['close_failure']


def test_the_attempt_is_persisted_BEFORE_the_order(monkeypatch):
    """Stricter than the time stop, which persists after failure. The risk
    bought off is a restart silently resetting the counter and re-firing: a
    crash mid-attempt must cost an attempt, never gain one."""
    store = MemoryStore(trades=[_frozen()])
    kite = _broker([{'tradingsymbol': B_LONG, 'quantity': B_QTY}])
    seen = {}

    def _script(*a, **k):
        seen['attempts_at_order_time'] = \
            store.trades[0]['close_failure']['attempts']
        raise RuntimeError('broker exploded mid-order')

    monkeypatch.setattr(sm, 'close_leg', _script)
    sm.recover_frozen_positions(kite, [('BCS', store, True)], False,
                                nagged=set(), now=PAST_GRACE,
                                market_settled=True)
    assert seen['attempts_at_order_time'] == 1


# ══ what must never happen ══════════════════════════════════════════════════

def test_a_PAPER_record_is_skipped_before_it_is_even_classified(monkeypatch):
    """The sweep is the SECOND route into the order path. Without its own
    guard a paper cohort record would get real orders aimed at legs that do
    not exist at the broker - the mistake the arming order exists to prevent."""
    store = MemoryStore(trades=[_frozen(paper=True)])
    kite = _broker([{'tradingsymbol': B_LONG, 'quantity': B_QTY}])
    script = _LegScript()

    assert _run(store, kite, monkeypatch, script) == 0
    assert script.calls == [] and kite.placed == []
    assert store.trades[0]['status'] == 'partial_close'


def test_fallen_hero_is_alert_only_and_never_ordered_on(spy, monkeypatch):
    """Owner, 2026-08-28: FH is traded by hand. The flag rides on the BOOK,
    so the rule sits where it was decided rather than being inferred from the
    shape of a record."""
    store = MemoryStore(trades=[_frozen()])
    kite = _broker([{'tradingsymbol': B_SHORT, 'quantity': -B_QTY}])
    script = _LegScript()

    assert _run(store, kite, monkeypatch, script, orders_allowed=False) == 0
    assert script.calls == [] and kite.placed == []
    assert any('traded by hand' in m for m in spy.sent)


def test_a_flipped_record_never_orders(spy, monkeypatch):
    """The Feb-2026 amplifier."""
    store = MemoryStore(trades=[_frozen(cause='flipped')])
    kite = _broker([{'tradingsymbol': B_SHORT, 'quantity': -B_QTY}])
    script = _LegScript()

    assert _run(store, kite, monkeypatch, script) == 0
    assert kite.placed == []
    assert store.trades[0]['close_failure']['state'] == 'escalated'


def test_a_rejected_close_gets_zero_retries(spy, monkeypatch):
    """The broker's reason will repeat. One order total would already be one
    too many; the correct count is zero."""
    store = MemoryStore(trades=[_frozen(cause='rejected')])
    kite = _broker([{'tradingsymbol': B_LONG, 'quantity': B_QTY}])
    script = _LegScript()

    assert _run(store, kite, monkeypatch, script) == 0
    assert kite.placed == []
    assert store.trades[0]['close_failure']['state'] == 'escalated'


def test_inside_the_grace_window_nothing_is_placed(monkeypatch):
    store = MemoryStore(trades=[_frozen()])
    kite = _broker([{'tradingsymbol': B_LONG, 'quantity': B_QTY}])
    script = _LegScript()

    assert _run(store, kite, monkeypatch, script, now=FROZE_TS + 60) == 0
    assert script.calls == []
    assert store.trades[0]['close_failure']['state'] == 'frozen'


def test_a_naked_short_does_not_wait(monkeypatch):
    """Zero grace. Waiting buys nothing and can cost a lakh."""
    store = MemoryStore(trades=[_frozen()])
    kite = _broker([{'tradingsymbol': B_SHORT, 'quantity': -B_QTY}])
    script = _LegScript(**{B_SHORT: [_complete(B_QTY, 10.00)]})

    assert _run(store, kite, monkeypatch, script, now=FROZE_TS + 1) == 1
    assert [c['symbol'] for c in script.calls] == [B_SHORT]
    assert script.calls[0]['urgent'] is True


def test_the_kill_switch_stops_the_sweep(monkeypatch):
    monkeypatch.setattr(sm, 'trading_enabled', lambda: False)
    store = MemoryStore(trades=[_frozen()])
    kite = _broker([{'tradingsymbol': B_LONG, 'quantity': B_QTY}])
    script = _LegScript()

    assert _run(store, kite, monkeypatch, script) == 0
    assert script.calls == []


def test_a_spent_incident_never_orders_again(spy, monkeypatch):
    store = MemoryStore(trades=[_frozen(attempts=3)])
    kite = _broker([{'tradingsymbol': B_LONG, 'quantity': B_QTY}])
    script = _LegScript()

    assert _run(store, kite, monkeypatch, script) == 0
    assert script.calls == []
    assert store.trades[0]['close_failure']['state'] == 'escalated'


def test_an_escalated_incident_stays_escalated_across_a_whole_session(
        spy, monkeypatch):
    """No restart, no new day and no further pass re-arms it."""
    store = MemoryStore(trades=[_frozen(state='escalated')])
    kite = _broker([{'tradingsymbol': B_LONG, 'quantity': B_QTY}])
    script = _LegScript()
    for _ in range(20):
        _run(store, kite, monkeypatch, script, now=PAST_GRACE + 86_400)
    assert kite.placed == [] and script.calls == []


# ══ the sweep only ever REDUCES ═════════════════════════════════════════════

def test_the_reduce_only_invariant_is_armed_on_every_order_here():
    """Not an assertion about a run - a statement that the rail is on. If
    `_broker` ever stops arming it, these tests keep passing while checking
    nothing, which is the decorative-guard shape."""
    assert _broker([]).reduce_only is True


def test_the_sweep_never_re_buys_a_hedge(monkeypatch):
    """The design's named rejection. Were the sweep ever to place a BUY to
    "restore the hedge" on a flat leg, FakeBroker raises rather than filling."""
    store = MemoryStore(trades=[_frozen()])
    kite = _broker([{'tradingsymbol': B_LONG, 'quantity': B_QTY}])
    script = _LegScript(**{B_LONG: [_complete(B_QTY, 40.00)]})
    _run(store, kite, monkeypatch, script)
    assert kite.reduce_only_violations == []


# ══ robustness ══════════════════════════════════════════════════════════════

def test_a_broker_that_cannot_be_read_classifies_nothing(monkeypatch):
    """No positions, no classification. Acting on an unread book would be
    ordering against a position whose real size nobody knows."""
    store = MemoryStore(trades=[_frozen()])
    kite = _broker([{'tradingsymbol': B_LONG, 'quantity': B_QTY}])
    kite.positions_raises = Exception('Too many requests')
    script = _LegScript()

    assert _run(store, kite, monkeypatch, script) == 0
    assert script.calls == []


def test_one_bad_record_does_not_stop_the_sweep_for_the_others(monkeypatch):
    """Per-record isolation - the lesson the poll loop learned expensively."""
    bad = _frozen()
    bad['id'] = 9
    del bad['quantity']                        # will raise inside the close
    good = _frozen()
    store = MemoryStore(trades=[bad, good])
    kite = _broker([])
    kite.order_book = [_tagged(B_SHORT, 'BUY', 10.00),
                       _tagged(B_LONG, 'SELL', 40.00)]
    monkeypatch.setattr(sm, 'close_leg', _LegScript())

    sm.recover_frozen_positions(kite, [('BCS', store, True)], False,
                                nagged=set(), now=PAST_GRACE,
                                market_settled=True)
    assert store.trades[1]['close_failure']['state'] == 'resolved', (
        'the malformed record ahead of it stopped the sweep')


def test_an_empty_frozen_book_does_not_even_read_positions(monkeypatch):
    """The sweep runs every poll. It must cost nothing when there is nothing
    to do, or it becomes a rate-limit source of its own."""
    store = MemoryStore(trades=[])
    kite = _broker([])
    kite.positions_raises = AssertionError('positions() must not be called')
    assert _run(store, kite, monkeypatch, _LegScript()) == 0


def test_the_sweep_is_wired_into_the_poll_loop_after_the_per_trade_work():
    """A sweep nobody calls is the `get_frozen_trades` situation all over
    again - a method that existed and was never invoked."""
    import inspect
    src = inspect.getsource(sm.monitor_all)
    assert 'recover_frozen_positions(' in src
    assert src.index('for trade in all_trades:') < \
        src.index('recover_frozen_positions('), (
        'the sweep must run AFTER the per-trade loop; live positions come '
        'first and their stops must not wait on records already stuck')


def test_the_fh_book_is_wired_with_orders_disabled():
    """The one place the alert-only decision is expressed.

    Read as (store, orders_allowed) PAIRS rather than as source strings.
    The first version of this guard asserted the literal text
    `"('FH', fh_store, False)"`, which made it fail the moment the LABEL
    changed spelling (N5 routed those through `STORE_TYPE_LABEL`) while the
    decision it exists to protect had not moved at all — a spelling guard
    wearing a property guard's name, `feedback_a_guard_can_pin_the_wrong_thing`.
    What must never change silently is which book may place orders.
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(sm.monitor_all).lstrip())
    pairs = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign)
                and any(getattr(t, 'id', None) == 'frozen_books'
                        for t in node.targets)):
            continue
        for elt in node.value.elts:
            store = elt.elts[1].id                    # bcs_store, fh_store, ...
            pairs[store] = elt.elts[2].value          # orders_allowed
    assert pairs, 'frozen_books is no longer a literal list of tuples'
    assert pairs['fh_store'] is False, (
        'FALLEN HERO IS ALERT-ONLY (owner, 2026-08-28) — this monitor places '
        'no FH order anywhere, and its frozen records are nagged, never '
        'traded')
    for store in ('bcs_store', 'bps_store', 'zebra_store'):
        assert pairs[store] is True, store


# ── N5 · a recovery order must name its book on the journal line ────────────

def test_a_recovery_order_carries_the_BOOK_on_its_journal_line(spy, monkeypatch):
    """`_store_type` is stamped by `_load_all_trades`, and a frozen record
    never goes through it — the sweep reads the stores directly, which is the
    whole point of it. Without the stamp the ONLY orders with no book on their
    journal line would be the recovery orders: the ones placed on a position
    nobody was watching, which is exactly when an incident report needs to
    name it. All four books number from 1.
    """
    ctxs = []
    real_ctx = sm._order_ctx
    monkeypatch.setattr(sm, '_order_ctx',
                        lambda *a, **k: ctxs.append(real_ctx(*a, **k)) or ctxs[-1])
    store = MemoryStore(trades=[_frozen()])
    kite = _broker([{'tradingsymbol': B_LONG, 'quantity': B_QTY}])
    script = _LegScript(**{B_LONG: [_complete(B_QTY, 40.00)]})

    assert _run(store, kite, monkeypatch, script) == 1

    assert ctxs, 'the recovery built no order context at all'
    assert all(c['book'] == 'bcs' for c in ctxs), [c['book'] for c in ctxs]
    assert '_store_type' not in store.trades[0], (
        'the sweep wrote a monitor-internal tag into the STORE record — '
        '`get_frozen_trades` hands out the live dict on every book, so that '
        'persists into the trade file on the next save')


def test_the_sweep_label_and_the_journal_book_come_from_ONE_table():
    """Two names for the same four books. A second hardcoded map is how the
    sweep's `COHORT` and the journal's `zebra` drift apart."""
    assert sm.LABEL_STORE_TYPE == {v: k for k, v in sm.STORE_TYPE_LABEL.items()}
    assert sm.LABEL_STORE_TYPE['COHORT'] == 'zebra'


def test_an_unknown_label_stamps_NOTHING_rather_than_guessing(spy, monkeypatch):
    """A label the table does not know must leave `book` null. Inventing one
    would put a confident wrong name in the forensic record."""
    trade = _frozen()
    trade.pop('_store_type', None)
    trade.setdefault('_store_type', sm.LABEL_STORE_TYPE.get('NOSUCHBOOK'))
    assert trade['_store_type'] is None
    assert sm._order_ctx(trade, 'SL', 'short', 'BCS')['book'] is None


# ══ a dry run rehearses; it does not resolve ════════════════════════════════
#
# THE DEFECT (found 2026-08-31). `recovery_gate` checks the kill switch and
# never checked `dry_run`, so under the `--dry-run` crontab that has been live
# all through the evidence week a REAL frozen record walked the entire action
# path: it burned and persisted an attempt, called `begin_recovery` (a real
# partial_close -> closing status write), ran the inner close whose dry stub
# reports COMPLETE, and then stamped `resolved` and Telegrammed "frozen close
# FINISHED by recovery" -- for a position whose legs were all still live and
# unbooked at the broker.
#
# The record was then left at `closing`, where the next process start's
# crash-recovery sweep flips it to `open` and hands a HALF-CLOSED position back
# to the per-trade loop to price as a whole spread.

def test_a_dry_run_places_no_order_and_resolves_nothing(spy, monkeypatch):
    """THE DEFECT, on the shape that would have placed a real SELL."""
    store = MemoryStore(trades=[_frozen()])
    kite = _broker([{'tradingsymbol': B_LONG, 'quantity': B_QTY}])
    script = _LegScript(**{B_LONG: [_complete(B_QTY, 40.00)]})

    assert _run(store, kite, monkeypatch, script, dry_run=True) == 0

    assert script.calls == [], 'a dry run reached the order path'
    assert kite.placed == []
    rec = store.trades[0]
    assert rec['status'] == 'partial_close', (
        'a dry run moved the record off partial_close; the next start would '
        'reopen it as a whole spread')
    assert rec['close_failure']['state'] == 'frozen'
    assert rec['close_failure']['attempts'] == 0, 'a dry run spent an attempt'
    assert not any('FINISHED by recovery' in m for m in spy.sent), (
        'a dry run claimed a close it never made')


def test_a_dry_run_does_not_book_a_flat_book_either(spy, monkeypatch):
    """`_finish_flat` had the same hole: the inner close returns True under a
    dry run, so it announced "the exit is booked" having booked nothing."""
    store = MemoryStore(trades=[_frozen()])
    kite = _broker([])
    kite.order_book = [_tagged(B_SHORT, 'BUY', 10.00),
                       _tagged(B_LONG, 'SELL', 40.00)]

    assert _run(store, kite, monkeypatch, _LegScript(), dry_run=True) == 0

    rec = store.trades[0]
    assert rec['status'] == 'partial_close'
    assert rec['close_failure']['state'] == 'frozen'
    assert not any('the exit is booked' in m for m in spy.sent)


def test_a_dry_run_still_says_what_it_SAW(spy, monkeypatch, capsys):
    """The sweep must not go silent -- the whole point of the evidence week is
    a record of what an armed engine would have done."""
    store = MemoryStore(trades=[_frozen()])
    kite = _broker([{'tradingsymbol': B_LONG, 'quantity': B_QTY}])

    _run(store, kite, monkeypatch, _LegScript(), dry_run=True)

    events = _events(capsys.readouterr().out)
    assert 'frozen_seen' in events, 'a dry run stopped classifying'
    assert 'recovery_rehearsed' in events, 'no rehearsal event was journalled'


def test_a_dry_run_still_ESCALATES_a_hand_traded_book(spy, monkeypatch):
    """Fallen Hero is alert-only in every mode. The dry-run guard must stop
    ACTING, not stop warning -- a frozen real position is exactly what a human
    should hear about during the evidence week."""
    store = MemoryStore(trades=[_frozen()])
    kite = _broker([{'tradingsymbol': B_LONG, 'quantity': B_QTY}])

    _run(store, kite, monkeypatch, _LegScript(), orders_allowed=False,
         dry_run=True, label='FH')

    assert spy.sent, 'a hand-traded frozen record went unannounced in dry run'
