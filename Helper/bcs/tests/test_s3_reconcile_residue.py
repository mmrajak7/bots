"""S3 - a leg still live on a trade already BOOKED CLOSED.

`reconcile_after_close` reads the broker's own view after a close reports
success. It is the ICICI-class guard: the code that placed the orders was
never going to catch the bug that placed them wrong, so this reads the broker
instead. What it did with a failure, until 2026-08-29, was send one Telegram.

That is the whole lifecycle. The record is already `closed`/`exited`, so it is
out of the open book, out of `get_frozen_trades()`, and out of every sweep
there is - the same invisible-position shape M14 closed one door over, still
open on this one. Someone swipes the alert away at 15:40 and the position
exists, unwatched, until they happen to look at Kite.

Two defects are covered here, and the second is the worse one:

  1. the residue was never PERSISTED, so nothing could chase it (S3 proper);
  2. `reconcile_after_close` read `short_symbol` and `long_symbol` and NOTHING
     ELSE, so all four Fallen Hero call sites - guarding the only naked short
     this fleet ever holds - found no symbols, collected no residues, and
     logged "both legs flat" for a book they had not looked at. A guard that
     reports CLEAN on a position it never read is worse than no guard: it
     answers the question. [[feedback_a_guard_can_pin_the_wrong_thing]]

The rule the sweep is bound by, asserted repeatedly below: **no order, ever.**
The record is terminal. There is no close lock to take, no stop to re-arm, and
the residue may be a leg the owner is holding on purpose.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest bcs/tests/test_s3_reconcile_residue.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                               # noqa: E402
from bcs.tests.fakes import FakeBroker, FakeClock, MemoryStore, TelegramSpy  # noqa: E402

LONG = 'TESTCO26SEP1340CE'
SHORT = 'TESTCO26SEP1390CE'
QTY = 700

FH_SC = 'FHCO26SEP3000CE'
FH_LC = 'FHCO26SEP3200CE'
FH_SP = 'FHCO26SEP2600PE'
FH_LP = 'FHCO26SEP2550PE'


def _closed(**over):
    t = {'id': 1, 'stock': 'TESTCO', 'status': 'closed',
         'long_symbol': LONG, 'short_symbol': SHORT, 'quantity': QTY,
         'exchange': 'NFO', 'spot_symbol': 'NSE:TESTCO'}
    t.update(over)
    return t


def _fh_closed(**over):
    """A 3-leg Fallen Hero. NONE of its legs is called `short_symbol`."""
    t = {'id': 1, 'stock': 'FHCO', 'status': 'closed',
         'short_call_symbol': FH_SC, 'short_put_symbol': FH_SP,
         'long_put_symbol': FH_LP, 'quantity': 400,
         'exchange': 'NFO', 'spot_symbol': 'NSE:FHCO'}
    t.update(over)
    return t


def _pos(symbol, qty):
    return {'tradingsymbol': symbol, 'quantity': qty}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """AUTOUSE, spy included. Every branch here alerts, and a test that forgot
    the spy would reach the real `requests.post` — a unit test messaging a live
    bot. Installed here so that is unreachable rather than remembered."""
    FakeClock().install(monkeypatch, sm)
    sm._RECOVERY_NAGGED.clear()
    return TelegramSpy().install(monkeypatch, sm)


@pytest.fixture
def spy(_env):
    return _env


def _books(store, label='BCS', orders_allowed=True):
    return [(label, store, orders_allowed)]


# ── 1. detection: the residue is written down ───────────────────────────────

def test_a_residue_found_after_a_close_is_persisted_on_the_record(spy):
    """THE DEFECT. Pre-fix `reconcile_after_close` alerted and returned False,
    and the fact existed nowhere afterwards."""
    trade = _closed()
    store = MemoryStore(trades=[trade])
    kite = FakeBroker(positions=[_pos(SHORT, -QTY)])

    assert sm.reconcile_after_close(kite, trade, 'BCS', store=store) is False

    res = store.trades[0]['reconcile_residue']
    assert res['state'] == 'open'
    assert res['detected_at']
    assert res['legs'] == {SHORT: -QTY, LONG: 0}
    assert SHORT in res['detail']


def test_a_flat_book_writes_nothing(spy):
    trade = _closed()
    store = MemoryStore(trades=[trade])
    kite = FakeBroker(positions=[])

    assert sm.reconcile_after_close(kite, trade, 'BCS', store=store) is True
    assert 'reconcile_residue' not in store.trades[0]


def test_without_a_store_the_alert_still_fires_and_the_log_says_it_is_untracked(
        spy, capsys):
    """`store` is optional so a probe or a test can call this. What must not
    happen is a residue that LOOKS tracked and is not."""
    trade = _closed()
    kite = FakeBroker(positions=[_pos(LONG, QTY)])

    assert sm.reconcile_after_close(kite, trade, 'BCS') is False
    assert len(spy.sent) == 1
    out = capsys.readouterr().out
    assert 'NOT recorded' in out


# ── 2. the FH false clean ───────────────────────────────────────────────────

def test_a_live_fh_naked_short_is_no_longer_reported_flat(spy):
    """Pre-fix: `short_symbol`/`long_symbol` are both absent from an FH record,
    so the loop found nothing, `residues` was empty, and this returned True
    while a NAKED SHORT CALL sat live at the broker."""
    trade = _fh_closed()
    store = MemoryStore(trades=[trade])
    kite = FakeBroker(positions=[_pos(FH_SC, -400)])

    assert sm.reconcile_after_close(kite, trade, 'FH', store=store) is False
    assert FH_SC in store.trades[0]['reconcile_residue']['detail']


def test_every_fh_leg_is_checked_including_the_four_leg_hedge(spy):
    trade = _fh_closed(long_call_symbol=FH_LC)
    store = MemoryStore(trades=[trade])
    kite = FakeBroker(positions=[_pos(FH_LC, 400)])

    assert sm.reconcile_after_close(kite, trade, 'FH', store=store) is False
    assert set(store.trades[0]['reconcile_residue']['legs']) == {
        FH_SC, FH_LC, FH_SP, FH_LP}


def test_a_record_with_no_legs_is_UNVERIFIED_never_clean(spy, capsys):
    """Unknown is not flat. A record this cannot describe must fail here
    rather than pass — the shape that made the FH check decorative."""
    trade = _closed()
    for k in ('long_symbol', 'short_symbol'):
        trade.pop(k)
    store = MemoryStore(trades=[trade])
    kite = FakeBroker(positions=[])

    assert sm.reconcile_after_close(kite, trade, 'BCS', store=store) is False
    assert 'NOT VERIFIED' in spy.sent[0].upper()
    assert 'EVENT reconcile_unknown_shape' in capsys.readouterr().out


def test_an_unreadable_broker_is_not_a_clean_close(spy, capsys):
    kite = FakeBroker(positions=[])
    kite.positions_raises = RuntimeError('boom')
    trade = _closed()
    store = MemoryStore(trades=[trade])

    assert sm.reconcile_after_close(kite, trade, 'BCS', store=store) is False
    assert 'EVENT reconcile_blind' in capsys.readouterr().out


# ── 3. the sweep ────────────────────────────────────────────────────────────

def _seed_residue(store_trade, detail=None, state='open'):
    store_trade['reconcile_residue'] = {
        'detected_at': '2026-08-29T10:00:00', 'state': state,
        'resolved_at': None, 'label': 'BCS',
        'detail': detail or ('%s net -700' % SHORT),
        'legs': {SHORT: -QTY, LONG: 0}, 'last_seen': '2026-08-29T10:00:00'}
    return store_trade


def test_the_sweep_nags_while_the_leg_is_still_live(spy):
    store = MemoryStore(trades=[_seed_residue(_closed())])
    kite = FakeBroker(positions=[_pos(SHORT, -QTY)])

    assert sm.sweep_reconcile_residue(kite, _books(store)) == 1
    assert len(spy.sent) == 1
    assert 'STILL LIVE ON A CLOSED TRADE' in spy.sent[0]
    assert store.trades[0]['reconcile_residue']['state'] == 'open'


def test_the_nag_is_once_a_day_not_once_a_poll(spy):
    """The sweep runs every five seconds. An alert that repeats at that rate
    is one the reader learns to swipe away, which is the same as silencing
    it — the reasoning `_escalate_recovery` already follows."""
    store = MemoryStore(trades=[_seed_residue(_closed())])
    kite = FakeBroker(positions=[_pos(SHORT, -QTY)])

    for _ in range(50):
        sm.sweep_reconcile_residue(kite, _books(store))
    assert len(spy.sent) == 1


def test_the_incident_resolves_ITSELF_when_the_broker_goes_flat(spy):
    """Self-resolution read from the BROKER, never from our own record: the
    human fixing it by hand is the outcome this most wants, and a sweep that
    re-read the freeze-time snapshot could never see it.

    TWO passes, since 2026-08-29: one flat read is not proof — see
    `test_ONE_flat_read_does_not_resolve_the_incident`. This test used to make
    one pass and assert resolution, which ENSHRINED the defect rather than
    catching it."""
    store = MemoryStore(trades=[_seed_residue(_closed())])
    kite = FakeBroker(positions=[])

    sm.sweep_reconcile_residue(kite, _books(store))
    assert sm.sweep_reconcile_residue(kite, _books(store)) == 0
    res = store.trades[0]['reconcile_residue']
    assert res['state'] == 'resolved' and res['resolved_at']
    assert 'FLAT' in spy.sent[0]


def test_a_resolved_incident_is_not_swept_again(spy):
    store = MemoryStore(trades=[_seed_residue(_closed())])
    kite = FakeBroker(positions=[])
    sm.sweep_reconcile_residue(kite, _books(store))   # confirm 1
    sm.sweep_reconcile_residue(kite, _books(store))   # confirm 2 -> resolved
    spy.sent.clear()

    assert sm.sweep_reconcile_residue(kite, _books(store)) == 0
    assert len(spy.sent) == 0


def test_a_failed_write_leaves_the_incident_OPEN(spy):
    """If the record does not say resolved, it is not resolved. Announcing a
    resolution the store refused would end the nag on a live leg."""
    store = MemoryStore(trades=[_seed_residue(_closed())])

    def _boom(*a, **k):
        raise RuntimeError('drive down')
    store.update_trade_fields = _boom
    kite = FakeBroker(positions=[])

    sm.sweep_reconcile_residue(kite, _books(store))
    assert store.trades[0]['reconcile_residue']['state'] == 'open'
    assert len(spy.sent) == 0


def test_a_blind_sweep_reports_the_records_as_STILL_pending(spy, capsys):
    store = MemoryStore(trades=[_seed_residue(_closed())])
    kite = FakeBroker(positions=[])
    kite.positions_raises = RuntimeError('rate limited')

    assert sm.sweep_reconcile_residue(kite, _books(store)) == 1
    assert 'EVENT residue_blind' in capsys.readouterr().out


def test_one_positions_read_for_the_whole_sweep(spy):
    """The F7 rate-limit lesson. Per-leg calls would multiply broker requests
    by the size of the book, on a path that runs every poll."""
    calls = []
    store = MemoryStore(trades=[
        _seed_residue(_closed()),
        _seed_residue(_closed(id=2, stock='OTHERCO'))])
    kite = FakeBroker(positions=[_pos(SHORT, -QTY)])
    real = kite.positions

    def counted():
        calls.append(1)
        return real()
    kite.positions = counted

    sm.sweep_reconcile_residue(kite, _books(store))
    assert len(calls) == 1


def test_an_empty_book_costs_no_broker_call(spy):
    """The normal state is 'no incident'. It must not add an API call per poll
    to the rate budget for a thing that is not happening."""
    calls = []
    store = MemoryStore(trades=[_closed()])
    kite = FakeBroker(positions=[])
    kite.positions = lambda: calls.append(1) or {'net': []}

    assert sm.sweep_reconcile_residue(kite, _books(store)) == 0
    assert calls == []


def test_one_bad_record_does_not_stop_the_sweep_for_the_rest(spy):
    bad = _seed_residue(_closed())
    bad['long_symbol'] = None
    bad['short_symbol'] = None
    good = _seed_residue(_closed(id=2, stock='OTHERCO'))
    store = MemoryStore(trades=[bad, good])
    kite = FakeBroker(positions=[])

    # The legless one cannot be checked and must not be called resolved; the
    # other resolves on its second confirmed flat read.
    assert sm.sweep_reconcile_residue(kite, _books(store)) == 2
    assert sm.sweep_reconcile_residue(kite, _books(store)) == 1
    assert store.trades[0]['reconcile_residue']['state'] == 'open'
    assert store.trades[1]['reconcile_residue']['state'] == 'resolved'
    assert spy.any('no longer declares any option leg')


def test_a_legless_record_nags_daily_not_every_poll(spy):
    """Every branch of this sweep runs at 5-second cadence. An alert or an
    event on the un-checkable one, per poll, buries the incident it reports."""
    bad = _seed_residue(_closed())
    bad['long_symbol'] = bad['short_symbol'] = None
    store = MemoryStore(trades=[bad])
    kite = FakeBroker(positions=[])

    for _ in range(30):
        sm.sweep_reconcile_residue(kite, _books(store))
    assert len(spy.sent) == 1


def test_a_broken_store_method_is_not_reported_as_an_OLD_store(spy, capsys):
    """`except AttributeError` around the call would have called the M14 twin's
    real bug — an AttributeError raised INSIDE `get_frozen_trades` — an absent
    method, and moved on. The two need opposite fixes."""
    class Broken:
        def get_residue_trades(self):
            raise AttributeError("'Broken' object has no attribute '_trades'")

    assert sm.sweep_reconcile_residue(FakeBroker(), _books(Broken())) == 0
    out = capsys.readouterr().out
    assert 'cannot list residues' not in out
    assert 'Could not read residues' in out


def test_a_store_without_the_method_is_NAMED_not_swallowed(spy, capsys):
    class Old:
        pass
    assert sm.sweep_reconcile_residue(FakeBroker(), _books(Old())) == 0
    assert 'cannot list residues' in capsys.readouterr().out


# ── 4. THE INVARIANT: escalate-only ─────────────────────────────────────────

@pytest.mark.parametrize('orders_allowed', [True, False])
@pytest.mark.parametrize('live_qty', [-QTY, QTY, -1])
def test_the_sweep_NEVER_places_an_order(spy, orders_allowed, live_qty):
    """Parametrised over `orders_allowed` deliberately: no value of it
    authorises anything here. The record is CLOSED — there is no close lock to
    take and no stop to re-arm, and the leg may be one the owner is holding."""
    store = MemoryStore(trades=[_seed_residue(_closed())])
    kite = FakeBroker(positions=[_pos(SHORT, live_qty)])

    sm.sweep_reconcile_residue(kite, _books(store, orders_allowed=orders_allowed))
    assert kite.placed == []


def test_the_sweep_never_changes_a_record_status(spy):
    store = MemoryStore(trades=[_seed_residue(_closed())])
    kite = FakeBroker(positions=[_pos(SHORT, -QTY)])

    sm.sweep_reconcile_residue(kite, _books(store))
    assert store.trades[0]['status'] == 'closed'


def test_a_frozen_record_is_not_the_residue_sweeps_business(spy):
    """A `partial_close` record already has a watcher (the M14 sweep) and a nag
    of its own. Naming it here too would double every alert for one position —
    and worse, would put a residue reader on a record the ORDER path owns."""
    frozen = _seed_residue(_closed(status='partial_close'))
    store = MemoryStore(trades=[frozen])
    kite = FakeBroker(positions=[_pos(SHORT, -QTY)])

    assert sm.sweep_reconcile_residue(kite, _books(store)) == 0
    assert len(spy.sent) == 0


# ── 5. clearance by hand ────────────────────────────────────────────────────

def _clear(monkeypatch, store, kite, ref='bcs:1'):
    monkeypatch.setattr(sm, '_frozen_book', lambda n: ('BCS', store))
    return sm.clear_residue(kite, ref)


def test_clearing_while_the_leg_is_LIVE_records_that_it_was(spy, monkeypatch):
    """The only case the machine cannot infer: the leg is deliberate. It
    clears — and the record keeps the difference between 'the residue went
    away' and 'somebody silenced it'."""
    store = MemoryStore(trades=[_seed_residue(_closed())])
    kite = FakeBroker(positions=[_pos(SHORT, -QTY)])

    assert _clear(monkeypatch, store, kite) == 0
    res = store.trades[0]['reconcile_residue']
    assert res['state'] == 'cleared'
    assert res['cleared_while_live']['legs'] == {SHORT: -QTY}
    assert 'CLEARED BY HAND while still live' in spy.sent[0]


def test_clearing_a_flat_record_notes_no_live_legs(spy, monkeypatch):
    store = MemoryStore(trades=[_seed_residue(_closed())])
    kite = FakeBroker(positions=[])

    assert _clear(monkeypatch, store, kite) == 0
    assert store.trades[0]['reconcile_residue']['cleared_while_live'] is None


def test_a_cleared_record_stops_being_swept(spy, monkeypatch):
    store = MemoryStore(trades=[_seed_residue(_closed())])
    kite = FakeBroker(positions=[_pos(SHORT, -QTY)])
    _clear(monkeypatch, store, kite)
    spy.sent.clear()

    assert sm.sweep_reconcile_residue(kite, _books(store)) == 0
    assert len(spy.sent) == 0


def test_clearing_places_no_order(spy, monkeypatch):
    store = MemoryStore(trades=[_seed_residue(_closed())])
    kite = FakeBroker(positions=[_pos(SHORT, -QTY)])
    _clear(monkeypatch, store, kite)
    assert kite.placed == []


def test_clearing_something_with_no_residue_refuses(spy, monkeypatch):
    store = MemoryStore(trades=[_closed()])
    assert _clear(monkeypatch, store, FakeBroker()) == 1


# ── 6. the wiring — a sweep nobody calls is the defect it fixes ─────────────

def test_the_poll_loop_calls_the_residue_sweep():
    """`get_frozen_trades` existed on one adapter and nothing called it for
    months. A method nobody invokes is indistinguishable from one that does
    not exist, which is how eight live positions once answered `Open: 0`."""
    src = Path(sm.__file__).read_text(encoding='utf-8')
    body = src[src.index('def monitor_all('):]
    assert 'sweep_reconcile_residue(kite, frozen_books)' in body


def test_the_digest_treats_a_residue_as_needing_a_human():
    from zebra.engine_log import RECOVERY_NEEDS_HUMAN, recovery_flags
    assert 'reconcile_residue' in RECOVERY_NEEDS_HUMAN
    flags = recovery_flags({'counts': {'reconcile_residue': 2},
                            'needs_human': {'reconcile_residue': 2}})
    assert any('STILL LIVE' in f for f in flags)


def test_the_event_is_emitted_per_decision_not_per_poll(spy, capsys):
    """One live leg reported as thousands of events is the '223 degraded
    events' shape that makes a reader stop reading. The nag emits once a
    day, and the sweep runs every five seconds."""
    store = MemoryStore(trades=[_seed_residue(_closed())])
    kite = FakeBroker(positions=[_pos(SHORT, -QTY)])

    for _ in range(20):
        sm.sweep_reconcile_residue(kite, _books(store))
    assert capsys.readouterr().out.count('EVENT residue_unresolved') == 1


def test_detection_and_the_nag_do_not_both_count_the_same_incident(spy, capsys):
    """Two names, because one incident found at 10:00 and nagged at 10:00:05
    under a single name reads as two findings in a digest a human uses to
    decide whether to arm."""
    trade = _closed()
    store = MemoryStore(trades=[trade])
    kite = FakeBroker(positions=[_pos(SHORT, -QTY)])

    sm.reconcile_after_close(kite, trade, 'BCS', store=store)
    sm.sweep_reconcile_residue(kite, _books(store))
    out = capsys.readouterr().out
    assert out.count('EVENT reconcile_residue ') == 1
    assert out.count('EVENT residue_unresolved') == 1


def test_an_unchanged_incident_is_not_rewritten_on_every_poll(spy):
    """A disk write per poll per incident buys no new fact. A CHANGE in the
    legs does, and is always written."""
    writes = []
    store = MemoryStore(trades=[_seed_residue(_closed())])
    real = store.update_trade_fields

    def counted(tid, **f):
        writes.append(f)
        return real(tid, **f)
    store.update_trade_fields = counted
    kite = FakeBroker(positions=[_pos(SHORT, -QTY)])

    for _ in range(10):
        sm.sweep_reconcile_residue(kite, _books(store))
    assert writes == []

    kite._positions = [_pos(SHORT, -100)]
    sm.sweep_reconcile_residue(kite, _books(store))
    assert len(writes) == 1
    assert writes[0]['reconcile_residue']['legs'][SHORT] == -100


# ── 7. the guard that keeps the leg reader honest ───────────────────────────

def test_legs_of_covers_every_option_leg_field_any_store_declares():
    """DISCOVERY BY SHAPE, not a hardcoded list.

    The FH false clean happened because a two-legged helper was reused on a
    four-legged book and nobody re-derived the field list. A test naming the
    six fields it already knows about would have passed at every moment of
    that bug's life. So this reads the field names out of the STORES and
    fails if `_legs_of` cannot see one — the same correction M14 step 10 had
    to make when a doubles guard checked 2 modules against 5 real doubles and
    scored itself 40% effective.

    `spot_symbol` is excluded on purpose: it is the NSE equity, not an option
    leg, and has no position to be flat.
    """
    import re
    from bcs.spread_monitor import _legs_of

    declared = set()
    for mod in ('bcs/trade_store.py', 'bear_put/trade_store.py',
                'fallen_hero/trade_store.py', 'zebra/trade_store.py',
                'bcs/zebra_adapter.py'):
        text = (HELPER / mod).read_text(encoding='utf-8')
        declared |= set(re.findall(r"'((?:long|short)_[a-z_]*symbol)'", text))
    assert declared, 'the scan found nothing — it has stopped being a guard'

    # A record declaring every one of them at once. Whatever `_legs_of` cannot
    # name here is a leg the post-close audit would silently not check.
    record = {f: 'SYM_%s' % f for f in sorted(declared)}
    seen = {sym for sym, _sign in _legs_of(record)}
    missing = sorted(f for f in declared if 'SYM_%s' % f not in seen)
    assert not missing, (
        '_legs_of does not read %s — a close on a book using those legs would '
        'be reported flat without them ever being looked at' % missing)


def test_the_post_close_audit_uses_that_one_reader():
    """Pinned on the SOURCE, because the behaviour tests above can only prove
    the shapes they happen to build. A second, private field list inside
    `reconcile_after_close` is exactly how the first one drifted."""
    src = Path(sm.__file__).read_text(encoding='utf-8')
    body = src[src.index('def reconcile_after_close('):
               src.index('def _persist_residue(')]
    assert '_legs_of(trade)' in body
    for field in ('short_symbol', 'long_symbol'):
        assert "'%s'" % field not in body, (
            'reconcile_after_close names leg fields itself again — that is the '
            'two-field list that reported an FH naked short as flat')


# ── 8. found by the 2026-08-29 adversarial review ──────────────────────────

def test_ONE_flat_read_does_not_resolve_the_incident(spy):
    """Resolution is TERMINAL and one-way: `get_residue_trades` returns only
    `state == 'open'`, and the only writer of a new incident is
    `reconcile_after_close`, which runs at close time on an already-terminal
    record. So a single successful-but-wrong `positions()` — an empty list in
    the early-session sync window, a degraded response, a missing row — would
    resolve every open incident, send a green "the leftover leg is now FLAT",
    and permanently disarm the guard for the shape that has cost real money
    twice.

    A RAISING positions() was already handled. A LYING one was not.
    """
    store = MemoryStore(trades=[_seed_residue(_closed())])
    kite = FakeBroker(positions=[])

    assert sm.sweep_reconcile_residue(kite, _books(store)) == 1
    assert store.trades[0]['reconcile_residue']['state'] == 'open'
    assert spy.sent == [], 'an all-clear was announced on one read'


def test_two_consecutive_flat_reads_DO_resolve_it(spy):
    """The negative control. Confirmation must not become never-resolving —
    the incident has to be able to end, or the nag is the new failure."""
    store = MemoryStore(trades=[_seed_residue(_closed())])
    kite = FakeBroker(positions=[])

    sm.sweep_reconcile_residue(kite, _books(store))
    assert sm.sweep_reconcile_residue(kite, _books(store)) == 0
    assert store.trades[0]['reconcile_residue']['state'] == 'resolved'
    assert spy.any('FLAT')


def test_a_live_reading_RESETS_the_confirmation(spy):
    """Two flat reads separated by a live one are not two CONSECUTIVE flat
    reads. Without the reset, an intermittent broker view would accumulate
    confirmations across a leg that keeps reappearing."""
    store = MemoryStore(trades=[_seed_residue(_closed())])
    flat = FakeBroker(positions=[])
    live = FakeBroker(positions=[_pos(SHORT, -QTY)])

    sm.sweep_reconcile_residue(flat, _books(store))          # 1 flat
    sm.sweep_reconcile_residue(live, _books(store))          # reset
    sm.sweep_reconcile_residue(flat, _books(store))          # 1 flat again
    assert store.trades[0]['reconcile_residue']['state'] == 'open'


def test_the_confirmation_survives_a_restart(spy):
    """It lives on the RECORD, not in memory: a process restart between the
    two reads must not bank half a confirmation, and must not lose one
    either."""
    store = MemoryStore(trades=[_seed_residue(_closed())])
    kite = FakeBroker(positions=[])
    sm.sweep_reconcile_residue(kite, _books(store))
    assert store.trades[0]['reconcile_residue']['flat_reads'] == 1

    # a "restart": a fresh nag set, same store
    assert sm.sweep_reconcile_residue(kite, _books(store), nagged=set()) == 0
    assert store.trades[0]['reconcile_residue']['state'] == 'resolved'


def test_residue_blind_is_a_daily_event_not_a_per_poll_one(spy, capsys):
    """A needs-human digest event at 12/min is the "one live leg reads as
    thousands of findings" shape this same change forbade for the persist
    path — written correctly there and copied wrong here."""
    store = MemoryStore(trades=[_seed_residue(_closed())])
    kite = FakeBroker(positions=[])
    kite.positions_raises = RuntimeError('rate limited')

    for _ in range(20):
        sm.sweep_reconcile_residue(kite, _books(store))
    assert capsys.readouterr().out.count('EVENT residue_blind') == 1


def test_the_digest_flags_a_close_nobody_verified():
    """`reconcile_blind` is emitted with a comment saying "the digest counts
    it by name", and it was the ONE of the four S3 events left out of
    `RECOVERY_NEEDS_HUMAN` — arguably the most dangerous, since it means the
    post-close audit itself failed."""
    from zebra.engine_log import RECOVERY_NEEDS_HUMAN, recovery_flags

    assert 'reconcile_blind' in RECOVERY_NEEDS_HUMAN
    flags = recovery_flags({'counts': {'reconcile_blind': 1},
                            'needs_human': {'reconcile_blind': 1}})
    assert any('could not be VERIFIED' in f for f in flags)
