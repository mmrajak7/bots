"""M11 — a force-close that did not fill is not a force-close that did.

`arm_time_stop` set `expiry_trades[key] = True` and every later poll
short-circuited on `if key in expiry_trades`. The flag meant "armed", was read
as "handled", and the dict lives in memory — so a close that failed at 15:15
was retried by TOMORROW's process, from scratch, and nothing said so.

That is a day out of the delivery buffer. The close is scheduled six sessions
before expiry precisely to stay clear of a margin ramp levied on the long ITM
leg at its STRIKE (~Rs 2.82L of full contract value against a Rs 2L account,
charged gross per leg), and for a bear put spread the tail past that deadline
is a give-delivery obligation against an empty demat: auction, 20% floor, no
ceiling. Losing a sixth of the buffer silently is the expensive part.

So the state machine has three outcomes now — ARMED, CLOSED, FAILED — the
third is persisted on the record, and the retry happens in the SAME session.

Run:  cd Helper && python -m pytest bcs/tests/test_time_stop_retry.py -v
"""
import sys
from datetime import date, time as dtime
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm      # noqa: E402

TODAY = date(2026, 9, 21)          # a Monday, six sessions before 2026-09-29
STAMP = '2026-09-21'
TRADE = {'id': 7, 'stock': 'TESTCO', 'expiry': '2026-09-29',
         'spot_symbol': 'NSE:TESTCO',
         'long_symbol': 'TESTCO26SEP1000PE',
         'short_symbol': 'TESTCO26SEP900PE',
         'time_policy': 'sessions_before_expiry', 'time_stop_sessions': 6}

AT_1515 = dtime(15, 15)
AT_1524 = dtime(15, 24)
PAST_CUTOFF = dtime(15, 26)        # > HARD_ORDER_CUTOFF_TIME


class FakeStore:
    """Only what this path touches. Records every persisted field."""

    def __init__(self):
        self.writes = []

    def update_trade_fields(self, trade_id, **fields):
        self.writes.append((trade_id, fields))


@pytest.fixture(autouse=True)
def _no_telegram(monkeypatch):
    sent = []
    monkeypatch.setattr(sm, 'send_telegram', lambda msg, **k: sent.append(msg))
    monkeypatch.setattr(sm, 'log', lambda *a, **k: None)
    return sent


def _arm(trade=None, expiry_trades=None, store=None, label='BCS'):
    """Arm once and hand back the state dict the loop would carry."""
    trade = dict(TRADE) if trade is None else trade
    expiry_trades = {} if expiry_trades is None else expiry_trades
    armed = sm.arm_time_stop(trade, ('bcs', 'BCS', trade['id']),
                             expiry_trades, label, today=TODAY, store=store)
    return trade, expiry_trades, armed


# ── the arming semantic that must SURVIVE ────────────────────────────────
def test_an_armed_but_unclosed_trade_does_not_realert_every_cycle(_no_telegram):
    """The flag exists so the alert is not repeated every five minutes. That
    behaviour is correct and the fix must not spend it."""
    trade, expiry_trades, first = _arm()
    assert first is True
    assert len(_no_telegram) == 1
    for _ in range(20):                      # twenty more polls
        assert sm.arm_time_stop(trade, ('bcs', 'BCS', 7), expiry_trades,
                                'BCS', today=TODAY) is False
    assert len(_no_telegram) == 1, "re-armed and re-alerted an armed trade"


def test_arming_still_refuses_when_the_policy_does_not_say_today(_no_telegram):
    early = dict(TRADE, time_stop_sessions=2)      # 6 sessions left, wants 2
    _, expiry_trades, armed = _arm(trade=early)
    assert armed is False and expiry_trades == {} and _no_telegram == []


# ── ARMED / CLOSED / FAILED, told apart ──────────────────────────────────
def test_a_failed_force_close_retries_in_the_same_session(_no_telegram):
    """THE defect. A close that did not fill used to be indistinguishable from
    one that did, and the only retry was tomorrow's process."""
    trade, expiry_trades, _ = _arm(store=FakeStore())
    st = expiry_trades[('bcs', 'BCS', 7)]
    assert sm.time_stop_attempt_due(st, now=1000.0, now_time=AT_1515) is True

    out = sm.record_time_stop_result(st, False, trade, FakeStore(), 'BCS',
                                     now=1000.0, now_time=AT_1515)
    assert out == 'failed'

    # Not immediately — a retry every five seconds is a spin, not a retry.
    assert sm.time_stop_attempt_due(st, now=1010.0, now_time=AT_1515) is False
    # ...but THIS session, not tomorrow.
    later = 1000.0 + sm.TIME_STOP_RETRY_WAITS[0] + 1
    assert sm.time_stop_attempt_due(st, now=later,
                                    now_time=dtime(15, 17)) is True


def test_the_retry_backoff_shrinks_as_the_cutoff_approaches():
    """Escalating urgency. It cannot escalate through the order pricing —
    EXPIRY_FORCE_CLOSE is already an URGENT_CLOSE_REASON, which is the top of
    that scale — so it escalates through cadence and through who gets told."""
    assert list(sm.TIME_STOP_RETRY_WAITS) == sorted(
        sm.TIME_STOP_RETRY_WAITS, reverse=True)
    st = {'date': STAMP, 'state': 'armed', 'attempts': 0,
          'next_attempt_after': 0.0}
    waits = []
    for _ in range(sm.TIME_STOP_MAX_ATTEMPTS - 1):
        sm.record_time_stop_result(st, False, dict(TRADE), None, 'BCS',
                                   now=0.0, now_time=AT_1515)
        waits.append(st['next_attempt_after'])
    assert waits == sorted(waits, reverse=True) and waits[-1] > 0
    assert waits == [float(w) for w in
                     list(sm.TIME_STOP_RETRY_WAITS)[:len(waits)]]


def test_it_escalates_to_a_human_on_the_final_failure(_no_telegram):
    trade, expiry_trades, _ = _arm(store=FakeStore())
    st = expiry_trades[('bcs', 'BCS', 7)]
    store = FakeStore()
    t = 1000.0
    for _ in range(sm.TIME_STOP_MAX_ATTEMPTS):
        assert sm.time_stop_attempt_due(st, now=t, now_time=AT_1515)
        out = sm.record_time_stop_result(st, False, trade, store, 'BCS',
                                         now=t, now_time=AT_1515)
        t += max(sm.TIME_STOP_RETRY_WAITS) + 1
    assert out == 'escalated'
    assert st['attempts'] == sm.TIME_STOP_MAX_ATTEMPTS
    # No fall-through to tomorrow: the loop stops trying and says so.
    assert sm.time_stop_attempt_due(st, now=t, now_time=AT_1515) is False
    assert '🚨' in _no_telegram[-1] and 'FAILED' in _no_telegram[-1]
    assert 'by hand' in _no_telegram[-1]


def test_past_the_hard_cutoff_the_first_failure_is_already_final(_no_telegram):
    """Past HARD_ORDER_CUTOFF_TIME `close_spread`'s late-day guard refuses to
    place anything, so 'retrying' would only delay the one thing that still
    helps — telling the human while the market is still open."""
    trade, expiry_trades, _ = _arm(store=FakeStore())
    st = expiry_trades[('bcs', 'BCS', 7)]
    out = sm.record_time_stop_result(st, False, trade, FakeStore(), 'BCS',
                                     now=1000.0, now_time=PAST_CUTOFF)
    assert out == 'escalated' and st['attempts'] == 1
    assert sm.time_stop_attempt_due(st, now=9e9, now_time=PAST_CUTOFF) is False
    assert '🚨' in _no_telegram[-1]


def test_a_filled_close_is_terminal_and_writes_nothing(_no_telegram):
    trade, expiry_trades, _ = _arm(store=FakeStore())
    st = expiry_trades[('bcs', 'BCS', 7)]
    store = FakeStore()
    assert sm.record_time_stop_result(st, True, trade, store, 'BCS',
                                      now=1000.0, now_time=AT_1515) == 'closed'
    assert sm.time_stop_attempt_due(st, now=9e9, now_time=AT_1515) is False
    assert store.writes == [], "wrote to a record the close just closed"
    assert len(_no_telegram) == 1, "close_spread already alerts its own success"


def test_an_abort_does_not_consume_an_attempt(_no_telegram):
    """'ABORT' means nothing was placed and the trade is still open. It is not
    a failure to close, so it must not spend one of three retries — but it
    still backs off, or an abort that repeats every poll is a spin."""
    trade, expiry_trades, _ = _arm(store=FakeStore())
    st = expiry_trades[('bcs', 'BCS', 7)]
    assert sm.record_time_stop_result(st, 'ABORT', trade, FakeStore(), 'BCS',
                                      now=1000.0, now_time=AT_1515) == 'armed'
    assert st['attempts'] == 0
    assert sm.time_stop_attempt_due(st, now=1001.0, now_time=AT_1515) is False
    assert sm.time_stop_attempt_due(st, now=2000.0, now_time=AT_1515) is True


def test_no_attempt_before_the_force_close_clock():
    st = {'date': STAMP, 'state': 'armed', 'attempts': 0,
          'next_attempt_after': 0.0}
    assert sm.time_stop_attempt_due(st, now=1000.0,
                                    now_time=dtime(14, 59)) is False
    assert sm.time_stop_attempt_due(st, now=1000.0, now_time=AT_1515) is True


# ── the third outcome must survive a restart ─────────────────────────────
def test_a_failed_close_is_persisted_to_the_record():
    store = FakeStore()
    trade = dict(TRADE)
    st = {'date': STAMP, 'state': 'armed', 'attempts': 0,
          'next_attempt_after': 0.0}
    sm.record_time_stop_result(st, False, trade, store, 'BCS',
                               now=1000.0, now_time=AT_1515)
    assert store.writes == [(7, {'time_stop_attempt': {
        'date': STAMP, 'state': 'failed', 'attempts': 1}})]
    assert trade['time_stop_attempt']['state'] == 'failed'


def test_the_failed_state_survives_a_process_restart(_no_telegram):
    """The cron relaunches this process every five minutes after a crash. An
    in-memory-only flag comes back as 'not yet armed', so the restarted monitor
    re-alerts 'will force-close by 15:15' for a stop that has already failed
    twice — the exact silence this fix exists to end."""
    store = FakeStore()
    trade, expiry_trades, _ = _arm(store=store)
    st = expiry_trades[('bcs', 'BCS', 7)]
    sm.record_time_stop_result(st, False, trade, store, 'BCS',
                               now=1000.0, now_time=AT_1515)
    sm.record_time_stop_result(st, False, trade, store, 'BCS',
                               now=2000.0, now_time=AT_1515)
    persisted = store.writes[-1][1]['time_stop_attempt']

    # --- process dies here; the cron restarts it and reloads the record ---
    _no_telegram.clear()
    reloaded = dict(TRADE, time_stop_attempt=persisted)
    fresh_expiry_trades = {}
    assert sm.arm_time_stop(reloaded, ('bcs', 'BCS', 7), fresh_expiry_trades,
                            'BCS', today=TODAY, store=store) is True
    resumed = fresh_expiry_trades[('bcs', 'BCS', 7)]
    assert resumed['state'] == 'failed' and resumed['attempts'] == 2

    msg = _no_telegram[-1]
    assert 'RESUMED' in msg and 'FAILED' in msg
    assert 'Will force-close by' not in msg, \
        "a stop that already failed twice re-announced itself as a first arming"
    # One retry is still owed, and it is owed TODAY.
    assert sm.time_stop_attempt_due(resumed, now=9e9, now_time=AT_1515) is True


def test_a_restart_after_escalation_does_not_silently_retry(_no_telegram):
    reloaded = dict(TRADE, time_stop_attempt={
        'date': STAMP, 'state': 'escalated', 'attempts': 3})
    expiry_trades = {}
    sm.arm_time_stop(reloaded, ('bcs', 'BCS', 7), expiry_trades, 'BCS',
                     today=TODAY, store=FakeStore())
    st = expiry_trades[('bcs', 'BCS', 7)]
    assert st['state'] == 'escalated'
    assert sm.time_stop_attempt_due(st, now=9e9, now_time=AT_1515) is False
    assert 'by hand' in _no_telegram[-1]


def test_yesterdays_counter_does_not_spend_todays_retries(_no_telegram):
    """Attempts do not accumulate across sessions. A stale stamp read as
    current would arrive at today's deadline with zero attempts left."""
    stale = dict(TRADE, time_stop_attempt={
        'date': '2026-09-18', 'state': 'escalated', 'attempts': 3})
    expiry_trades = {}
    sm.arm_time_stop(stale, ('bcs', 'BCS', 7), expiry_trades, 'BCS',
                     today=TODAY, store=FakeStore())
    st = expiry_trades[('bcs', 'BCS', 7)]
    assert st == {'date': STAMP, 'state': 'armed', 'attempts': 0,
                  'next_attempt_after': 0.0}
    assert 'Will force-close by' in _no_telegram[-1]


def test_a_corrupt_counter_reads_pessimistically(_no_telegram):
    """Unrecognised is FAILED, not ARMED: the safe reading of 'something tried
    to close this and I cannot tell how it went' is that it did not fill."""
    for bad in ({'date': STAMP, 'state': 'weird', 'attempts': 'x'},
                {'date': STAMP}):
        expiry_trades = {}
        sm.arm_time_stop(dict(TRADE, time_stop_attempt=bad),
                         ('bcs', 'BCS', 7), expiry_trades, 'BCS', today=TODAY)
        st = expiry_trades[('bcs', 'BCS', 7)]
        assert st['state'] == 'failed' and st['attempts'] == 0


def test_a_failed_persist_never_blocks_the_escalation(_no_telegram):
    """Losing the counter costs a duplicated alert after a restart. Losing the
    ESCALATION costs a delivery obligation, so the write cannot block it."""
    class Broken(FakeStore):
        def update_trade_fields(self, *a, **k):
            raise RuntimeError('drive down')

    st = {'date': STAMP, 'state': 'armed', 'attempts': 0,
          'next_attempt_after': 0.0}
    out = sm.record_time_stop_result(st, False, dict(TRADE), Broken(), 'BCS',
                                     now=1000.0, now_time=PAST_CUTOFF)
    assert out == 'escalated' and '🚨' in _no_telegram[-1]


# ── wiring: the retry has to reach the loop that actually runs ───────────
def test_the_retry_is_wired_into_the_cron_loop():
    """The recurring failure in this fleet is code that is written, tested and
    never reached. `monitor_all` is the --cron entrypoint on the Pi."""
    src = (HELPER / 'bcs' / 'spread_monitor.py').read_text(encoding='utf-8')
    flat = ''.join(src.split())
    assert 'record_time_stop_result(' in flat
    assert 'time_stop_attempt_due(ts_state)' in flat, \
        "the loop still decides retries by membership in expiry_trades"
    assert 'closing_in_progress.pop(close_key,None)' in flat, \
        "a failed force close still holds the in-progress lock for the session"


def test_the_time_stop_still_has_one_due_call_site():
    """`time_stop_due` decides whether a close is owed TODAY and lives behind
    exactly one call site — a mutation reverting two of three sites to
    `is_expiry_day` once survived the whole suite. The retry state rides in
    `expiry_trades`'s VALUE for the same reason: there is nowhere else for a
    second opinion to form."""
    src = (HELPER / 'bcs' / 'spread_monitor.py').read_text(encoding='utf-8')
    assert src.count('time_stop_due(') == 2, \
        "time_stop_due has grown a second caller besides arm_time_stop"


def test_the_force_close_clock_has_one_definition():
    src = (HELPER / 'bcs' / 'spread_monitor.py').read_text(encoding='utf-8')
    flat = ''.join(src.split())
    assert flat.count('>=EXPIRY_FORCE_CLOSE_TIME') == 1, \
        "the force-close clock test was copied instead of shared"
