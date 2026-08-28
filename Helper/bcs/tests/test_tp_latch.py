"""M13 on the ORDER path — the take-profit latch, driven through `monitor_all`.

The latch is decided in ONE place (`zebra.trade_store.tp_latch`) precisely
because two engines hold these triggers: `zebra/monitor.py` books the cohort in
paper, and this file places the real orders once armed. A rule honoured by one
and not the other is this codebase's single most repeated defect
(`feedback_the_copy_you_did_not_open`, six instances on 2026-08-26 alone), so
the tests below drive the REAL `monitor_all` — the loop, the vet, the abort
cooldown and `close_spread` — and not the helper in isolation.

The abort cooldown is load-bearing in the first replay and not decoration. A
held verdict returns 'ABORT', which parks further TP attempts for
`ABORT_COOLDOWN_SEC` (300s); spot backs off inside that window, so by the time
the engine is allowed to act the live comparison is False. That is the
COFORGE #436 shape reproduced against a loop that polls every 5 seconds instead
of every 5 minutes: the trigger evaporates while the system is unable to act on
it, whatever the cadence.

Run:  cd Helper && python -m pytest bcs/tests/test_tp_latch.py -v
"""
from datetime import date

import pytest

from bcs import spread_monitor as sm
from bcs.tests import replay as replay_mod
from bcs.tests.replay import Tick, run_session

DAY = date(2026, 9, 15)
L, S = 'TESTCO26SEP1340CE', 'TESTCO26SEP1390CE'
QTY = 700
TARGET = 1435.0

#: A cohort BCS as `ZebraStoreAdapter.map_trade` hands it to the monitor.
#: `paper: False` — this one was really placed, so the order path may hold it.
COHORT = {
    'id': 419, 'status': 'open', 'stock': 'TESTCO', 'version': 1,
    'cohort': '2026-08-14', 'structure': 'bcs', 'direction': 'CE',
    'paper': False,
    'long_symbol': L, 'short_symbol': S, 'spot_symbol': 'NSE:TESTCO',
    'exchange': 'NFO', 'quantity': QTY, 'lot_size': QTY, 'lots': 1,
    'entry_long_price': 21.20, 'entry_short_price': 7.65, 'net_debit': 13.55,
    'spread_width': 50, 'target_spot': TARGET, 'sl_spot': 1319.0,
    'sl_spread': 6.78, 'entry_spot': 1360.0, 'expiry': '2026-09-29',
    'spot_sl_enabled': False, 'trail_policy': 'gain_anchored',
    'time_policy': 'sessions_before_expiry', 'time_stop_sessions': 5,
}

LONG_BOOK = {'bid': 100.00, 'bid_qty': 1400, 'ask': 100.20, 'ask_qty': 1400,
             'ltp': 100.10, 'prev_close': 21.0}
SHORT_BOOK = {'bid': 52.00, 'bid_qty': 1400, 'ask': 52.20, 'ask_qty': 1400,
              'ltp': 52.10, 'prev_close': 7.6}


def _pos():
    """A FRESH position list per replay — `TickBroker` mutates it as legs
    fill, and a shared one leaves the next replay starting already flat."""
    return [{'tradingsymbol': S, 'quantity': -QTY},
            {'tradingsymbol': L, 'quantity': QTY}]


class _Gate:
    """The exit vet, answerable per call. `answers` is consumed in order and
    the last value repeats — so `[False]` is "defer once, then allow"."""

    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.calls = []

    def __call__(self, store, trade, kind, quote, spot, dry_run=False):
        self.calls.append((kind, spot))
        return self.answers.pop(0) if len(self.answers) > 1 else (
            self.answers[0] if self.answers else True)


@pytest.fixture
def gate(monkeypatch):
    """Patched where `bcs.exit_vet` reaches it, so the whole real gate wiring
    (kind mapping, dry-run skip, fail-open) is still exercised."""
    import zebra.monitor as zm
    g = _Gate([False, True])
    monkeypatch.setattr(zm, '_exit_cleared', g)
    return g


#: The touch, then the retreat, then the cooldown lapsing on a spot that no
#: longer satisfies the trigger. 11:00:00 fires and is DEFERRED (300s
#: cooldown); by 11:00:30 spot is back under target; the engine is free to act
#: again at 11:05:00, when only a latch can still fire this exit.
TOUCH_THEN_RETREAT = (
    [Tick('11:00:00', 1440.0, LONG_BOOK, SHORT_BOOK, 'THROUGH the target')]
    + [Tick(t, 1400.0, LONG_BOOK, SHORT_BOOK, 'backed off below target')
       for t in ('11:00:30', '11:03:00', '11:05:30', '11:06:00', '11:07:00')]
)

#: Same session, target never touched at all. The negative control.
NEVER_TOUCHED = [Tick(t, 1400.0, LONG_BOOK, SHORT_BOOK, 'below target')
                 for t in ('11:00:00', '11:00:30', '11:03:00', '11:05:30',
                           '11:06:00', '11:07:00')]


@pytest.fixture
def books(monkeypatch):
    """Every `MemoryStore` the replay builds, so the COHORT one can be read.

    `run_session` returns the bcs book, and a cohort position lives in the
    fourth store — which it constructs itself, inside the lambda it installs
    for `_open_zebra_store`. The store copies its records on construction (it
    mirrors the real stores deliberately), so the dict handed in is NOT the one
    written to. This keeps the instances instead of guessing.
    """
    made = []
    base = replay_mod.MemoryStore

    class Recording(base):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)

    monkeypatch.setattr(replay_mod, 'MemoryStore', Recording)
    return made


def _record(books, tid):
    for st in books:
        for t in st.trades:
            if t.get('id') == tid:
                return t
    raise AssertionError(
        'trade #%s is in none of the %d books the replay built — the '
        'position was never loaded, so any assertion about it would pass for '
        'the wrong reason' % (tid, len(books)))


def _run(monkeypatch, books, ticks, trade=None, dry_run=False):
    """Returns (kite, the RECORD as the store holds it, telegram spy)."""
    trade = dict(COHORT if trade is None else trade)
    _clock, kite, _bcs_store, spy = run_session(
        monkeypatch, sm, trade, ticks, DAY, _pos(),
        dry_run=dry_run, cohort=[trade])
    return kite, _record(books, trade['id']), spy


# ── The leak, closed ────────────────────────────────────────────────────────

def test_a_touch_seen_once_and_then_retreating_still_closes(monkeypatch, gate, books):
    """COFORGE #436. The target was hit, the verdict came back allowed, and
    the position stayed open because spot had moved on by the time anything
    could act.

    PRE-FIX the second attempt never happens: `spot >= target` is recomputed
    from scratch at 11:05 against 1400 and the exit is simply gone.
    """
    kite, rec, _spy = _run(monkeypatch, books, TOUCH_THEN_RETREAT)

    assert gate.calls, 'the vet was never consulted — the TP never fired'
    assert kite.placed, (
        'the take-profit was lost when spot backed off before the engine '
        'could act — the exact leak M13 exists to close')
    assert rec['status'] == 'closed'
    assert rec['exit_reason'] == 'TP'


def test_the_touch_is_persisted_on_the_record(monkeypatch, gate, books):
    """Not in memory. This process is long-lived, but zebra's cron is not and
    both read the SAME record — the latch has to be a fact about the position,
    not about whoever happens to be running."""
    _kite, rec, _spy = _run(monkeypatch, books, TOUCH_THEN_RETREAT)
    assert rec['tp_touched_at'], 'the touch was never written to the record'
    assert rec['tp_touch_spot'] == 1440.0


def test_the_close_happens_at_the_retreated_spot_not_the_touch(
        monkeypatch, gate, books):
    """The TRIGGER latches; the PRICE does not
    (`feedback_trigger_is_not_the_fill`). The exit is booked against 1400 —
    where the market actually was — and the give-back is RECORDED rather than
    smoothed away."""
    _kite, rec, _spy = _run(monkeypatch, books, TOUCH_THEN_RETREAT)
    assert rec['exit_spot'] == 1400.0
    assert rec['tp_touch_spot_move'] == pytest.approx(-40.0)
    assert rec['tp_touch_gave_back'] is True
    assert rec['tp_touch_to_exit_sec'] >= 300, (
        'the measured lag is smaller than the cooldown that caused it')
    # Both timestamps are taken from THIS module's `datetime.now()`, which the
    # replay clock drives — so the number above is replay minutes, not the
    # fraction of a second the test itself took. A lag measured on a different
    # clock from the one the engine runs on would read ~0 here and would be
    # just as wrong in production the day the box's clocks disagree.


def test_an_untouched_target_closes_nothing(monkeypatch, gate, books):
    """Negative control, and the one that stops the latch becoming "always
    exit". Same session, same book, spot simply never reaches the target."""
    kite, rec, _spy = _run(monkeypatch, books, NEVER_TOUCHED)
    assert kite.placed == [], 'a position with no touch was closed anyway'
    assert rec['status'] == 'open'
    assert 'tp_touched_at' not in rec
    assert gate.calls == [], 'the vet was spent on a trigger that never fired'


# ── The other engine's latch is this engine's trigger ───────────────────────

def test_a_latch_written_by_zebra_is_honoured_here(monkeypatch, gate, books):
    """The handover case. zebra saw the touch at 09:25 and wrote it to the
    record; the order path reads the same file and must act on it even though
    spot never goes near the target in this session.

    This is the assertion that fails if the two engines ever drift apart.
    """
    latched = dict(COHORT, tp_touched_at='2026-09-15T09:25:00',
                   tp_touch_spot=1436.0)
    kite, rec, _spy = _run(monkeypatch, books, NEVER_TOUCHED, trade=latched)
    assert kite.placed, (
        'the order path ignored a touch the other engine had already '
        'recorded — one latch, two engines, or it is not a latch')
    assert rec['exit_reason'] == 'TP'
    assert rec['tp_touched_at'] == '2026-09-15T09:25:00', (
        'the latch was overwritten; the FIRST touch is the one that arms')


def test_the_shared_decision_is_the_one_both_engines_call():
    """Pinned on the SOURCE. Two behavioural suites can both pass over two
    copies of the rule that happen to agree today — which is precisely how
    this codebase's most repeated defect gets in."""
    import inspect
    from zebra import monitor as zmon
    from zebra import trade_store as zts
    assert 'tp_latch(' in inspect.getsource(zmon.check_entered)
    assert 'ts.tp_latch(' in inspect.getsource(sm.tp_armed)
    assert callable(zts.tp_latch)


# ── Who may ARM, as opposed to who may honour ───────────────────────────────

def test_a_dry_run_writes_no_latch(monkeypatch, gate, books):
    """Dry run means "monitor everything, change nothing". While this engine
    is in dry run zebra still owns these exits and latches them itself; a dry
    run arming a trigger it is not allowed to pull is a live mutation from a
    rehearsal."""
    kite, rec, _spy = _run(monkeypatch, books, TOUCH_THEN_RETREAT, dry_run=True)
    assert kite.placed == [], 'a dry run placed an order'
    assert 'tp_touched_at' not in rec, (
        'a dry run wrote a latch onto the live cohort record')


def test_a_paper_record_gets_no_latch_from_the_order_path(monkeypatch, gate, books):
    """`close_spread` refuses a paper record outright — no broker ever saw it,
    zebra books it, and zebra arms it. Writing a latch here would have this
    engine arming a position it may not touch."""
    paper = dict(COHORT, id=421, paper=True)
    kite, rec, _spy = _run(monkeypatch, books, TOUCH_THEN_RETREAT, trade=paper)
    assert kite.placed == [], 'an order was placed for a paper record'
    assert 'tp_touched_at' not in rec


def test_the_engine_still_HONOURS_a_latch_on_a_paper_record(monkeypatch, gate, books):
    """The other half, and the one the dry-run evidence week depends on:
    `journal_report --compare` works by letting the monitor walk the whole
    close and journal what an ARMED engine would have done. A monitor that
    refused to see the latch would journal nothing."""
    paper = dict(COHORT, id=422, paper=True,
                 tp_touched_at='2026-09-15T09:25:00', tp_touch_spot=1436.0)
    _kite, _rec, spy = _run(monkeypatch, books, NEVER_TOUCHED, trade=paper)
    assert any('paper' in m.lower() for m in spy.sent), (
        'the latched exit never reached close_spread at all, so the refusal '
        'that proves it was seen never happened')


# ── A bear put spread latches the other way ─────────────────────────────────

PL, PS = 'TESTCO26SEP1390PE', 'TESTCO26SEP1340PE'
PE_TARGET = 1320.0
PE_COHORT = dict(COHORT, id=423, direction='PE', long_symbol=PL,
                 short_symbol=PS, target_spot=PE_TARGET, sl_spot=1400.0,
                 entry_spot=1360.0)

PE_TOUCH_THEN_RETREAT = (
    [Tick('11:00:00', 1315.0, LONG_BOOK, SHORT_BOOK, 'THROUGH the target')]
    + [Tick(t, 1345.0, LONG_BOOK, SHORT_BOOK, 'rallied back above target')
       for t in ('11:00:30', '11:03:00', '11:05:30', '11:06:00', '11:07:00')]
)


def test_a_bear_put_spread_latches_on_a_FALL(monkeypatch, gate, books):
    """TP is `spot <= target` here. Each engine keeps its own direction rule
    and hands the shared decision a boolean — a latch that assumed CE would
    never arm on this book, and its give-back label would be inverted."""
    trade = dict(PE_COHORT)
    _clock, kite, _bcs, _spy = run_session(
        monkeypatch, sm, trade, PE_TOUCH_THEN_RETREAT, DAY,
        [{'tradingsymbol': PS, 'quantity': -QTY},
         {'tradingsymbol': PL, 'quantity': QTY}],
        dry_run=False, cohort=[trade])

    assert kite.placed, 'a bear put spread never converts its take-profit'
    rec = _record(books, trade['id'])
    assert rec['tp_touch_spot'] == 1315.0
    assert rec['tp_touch_spot_move'] == pytest.approx(30.0)
    assert rec['tp_touch_gave_back'] is True, (
        'spot RISING after a PE touch is the give-back — labelling it a gain '
        'reads every bear put exit backwards')


def test_an_armed_position_is_visible_on_the_status_line(monkeypatch, gate,
                                                         books, capsys):
    """An armed take-profit waiting out an abort cooldown must not look like a
    quiet position. Deliberately on the 30-second status line and NOT once per
    5-second poll: sixty identical lines is how a reader learns to skim, which
    is how the OI flag on COCHINSHIP got waved through."""
    _run(monkeypatch, books, TOUCH_THEN_RETREAT)
    out = capsys.readouterr().out
    assert '[TP-LATCHED]' in out
    assert out.count('[TP-LATCHED]') < 30, (
        'the armed state is being printed every poll rather than on the '
        'status line')
