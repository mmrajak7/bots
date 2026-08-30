"""Physical-delivery margin proximity — the live monitor's expiry warning.

Indian single-stock options are physically settled, and the exchange levies a
delivery margin on ITM legs that ramps over the final trading sessions. Until
2026-08-12 the only expiry handling in spread_monitor.py was a force-close ON
EXPIRY DAY: after the whole ramp had been paid, and late enough that a broker
short of margin may square the position off first, at a price nobody chose.

Run:  cd Helper && python -m pytest bcs/tests/test_expiry_margin.py -v
"""
import sys
from datetime import date
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm      # noqa: E402

# Expiry is Tuesday 2026-09-29, and NO holiday falls in the fortnight before
# it. That is why this week and not another: these tests are about weekend
# skipping and the countdown, and they used to sit on the week of 2026-08-27
# — which the daily NSE calendar now says contains a closure (2026-08-26),
# so every expected number here moved by one. A fixture that straddles a
# holiday is testing the calendar, not the arithmetic it means to test.
EXPIRY = '2026-09-29'
BCS_TRADE = {'id': 1, 'stock': 'TESTCO', 'expiry': EXPIRY,
             'spot_symbol': 'NSE:TESTCO',
             'long_symbol': 'TESTCO26AUG100CE',
             'short_symbol': 'TESTCO26AUG140CE'}
FH_TRADE = {'id': 2, 'stock': 'FHCO', 'expiry': EXPIRY,
            'spot_symbol': 'NSE:FHCO',
            'long_put_symbol': 'FHCO26AUG90PE',
            'short_put_symbol': 'FHCO26AUG100PE',
            'short_call_symbol': 'FHCO26AUG150CE'}


class FakeStore:
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


# ── sessions, not calendar days ──────────────────────────────────────────
def test_weekend_does_not_count_as_time_left():
    """THE bug this replaces. On the Friday before expiry week a calendar count
    says 6 days remain; only 4 sessions do, and the delivery ramp has started."""
    friday = date(2026, 9, 25)
    assert (date(2026, 9, 29) - friday).days == 4      # what calendar says
    assert sm.sessions_to_expiry(BCS_TRADE, friday) == 2


def test_sessions_countdown_through_expiry_week():
    for day, expected in ((date(2026, 9, 24), 3),      # Thu
                          (date(2026, 9, 25), 2),      # Fri
                          (date(2026, 9, 28), 1),      # Mon
                          (date(2026, 9, 29), 0)):     # Tue = expiry
        assert sm.sessions_to_expiry(BCS_TRADE, day) == expected, day


def test_expired_and_unparseable_are_handled():
    # PAST expiry, so zero. Moved with the fixture week.
    assert sm.sessions_to_expiry(BCS_TRADE, date(2026, 10, 1)) == 0
    assert sm.sessions_to_expiry({'expiry': 'not-a-date'}, date(2026, 8, 1)) is None
    assert sm.sessions_to_expiry({}, date(2026, 8, 1)) is None


# ── which legs actually carry a delivery obligation ──────────────────────
def test_bcs_itm_legs():
    assert sm.delivery_exposure(BCS_TRADE, 150.0)['itm'] == ['long', 'short']
    assert sm.delivery_exposure(BCS_TRADE, 120.0)['itm'] == ['long']
    assert sm.delivery_exposure(BCS_TRADE, 90.0)['itm'] == []


def test_fallen_hero_legs_are_read_too():
    """FH's naked SHORT CALL is the biggest delivery exposure in the fleet. A
    BCS-shaped implementation finds no legs on this record and reports
    'ITM legs: none' — an actively false all-clear, not a missing feature."""
    exp = sm.delivery_exposure(FH_TRADE, 160.0)        # above the short call
    assert 'short_call' in exp['itm']
    exp = sm.delivery_exposure(FH_TRADE, 95.0)         # below the short put
    assert 'short_put' in exp['itm'] and 'long_put' not in exp['itm']


def test_unreadable_legs_report_unknown_not_none():
    """None means 'could not tell'. An empty list means 'checked, nothing ITM'.
    Collapsing the two would turn a parse failure into an all-clear."""
    assert sm.delivery_exposure({'long_symbol': 'GARBAGE'}, 100.0)['itm'] is None
    assert sm.delivery_exposure(BCS_TRADE, 90.0)['itm'] == []


# ── the warning itself ───────────────────────────────────────────────────
def test_warns_one_session_before_the_ramp(_no_telegram):
    store = FakeStore()
    sent = sm.maybe_warn_expiry_proximity(store, dict(BCS_TRADE), 150.0, 'BCS',
                                          today=date(2026, 9, 22))   # 5 left
    assert sent and 'session' in _no_telegram[0]
    assert 'starts in 1 session' in _no_telegram[0]


def test_says_the_ramp_is_active_inside_it(_no_telegram):
    sm.maybe_warn_expiry_proximity(FakeStore(), dict(BCS_TRADE), 150.0, 'BCS',
                                   today=date(2026, 9, 25))          # 2 left
    assert 'ramp ACTIVE' in _no_telegram[0]


def test_quiet_while_expiry_is_far(_no_telegram):
    assert not sm.maybe_warn_expiry_proximity(
        FakeStore(), dict(BCS_TRADE), 150.0, 'BCS', today=date(2026, 8, 10))
    assert _no_telegram == []


def test_expiry_day_is_left_to_the_force_close(_no_telegram):
    """Expiry day already has its own alert and its own automated close. A
    second message there is noise on the one day the user is already watching."""
    assert not sm.maybe_warn_expiry_proximity(
        FakeStore(), dict(BCS_TRADE), 150.0, 'BCS', today=date(2026, 9, 29))
    assert _no_telegram == []


def test_one_warning_per_day_survives_a_monitor_restart(_no_telegram):
    """The cron restarts this process on a 5-minute retry after any crash. An
    unpersisted flag would re-nag on every restart, which trains the user to
    ignore the one alert that costs real money."""
    store = FakeStore()
    t = dict(BCS_TRADE)
    day = date(2026, 9, 25)
    assert sm.maybe_warn_expiry_proximity(store, t, 150.0, 'BCS', today=day)
    assert store.writes == [(1, {'expiry_warn_date': '2026-09-25'})]

    restarted = dict(BCS_TRADE, expiry_warn_date='2026-09-25')
    assert not sm.maybe_warn_expiry_proximity(store, restarted, 150.0, 'BCS',
                                              today=day)
    assert len(_no_telegram) == 1
    # ...but the next session nags again.
    assert sm.maybe_warn_expiry_proximity(store, restarted, 150.0, 'BCS',
                                          today=date(2026, 9, 28))
    assert len(_no_telegram) == 2


def test_a_failed_flag_write_never_swallows_the_warning(_no_telegram):
    """Losing the flag costs a duplicate nag tomorrow. Losing the WARNING costs
    a margin call, so the alert goes out first and the write cannot block it."""
    class Broken(FakeStore):
        def update_trade_fields(self, *a, **k):
            raise RuntimeError('drive down')

    assert sm.maybe_warn_expiry_proximity(Broken(), dict(BCS_TRADE), 150.0,
                                          'BCS', today=date(2026, 9, 25))
    assert len(_no_telegram) == 1


def test_warning_closes_nothing(monkeypatch, _no_telegram):
    """Alert-only, by explicit decision: no new automated close path enters the
    real-order system on expiry proximity."""
    closed = []
    monkeypatch.setattr(sm, 'close_spread',
                        lambda *a, **k: closed.append(a) or 'CLOSED')
    sm.maybe_warn_expiry_proximity(FakeStore(), dict(BCS_TRADE), 150.0, 'BCS',
                                   today=date(2026, 9, 25))
    assert closed == []


def test_warning_is_wired_into_the_cron_startup():
    """The recurring failure in this fleet is code that is written, tested and
    never reached. Assert the cron path actually calls it."""
    src = (HELPER / 'bcs' / 'spread_monitor.py').read_text(encoding='utf-8')
    assert src.count('maybe_warn_expiry_proximity(') >= 3, \
        "warning defined but not called from both monitor paths"


# ── resumption after an interruption (2026-08-12) ────────────────────────
# Both settle buffers are computed from a FIXED clock time, so they arm once at
# 09:18 / 09:30 and stay armed all session. That assumes the session runs
# uninterrupted. An exchange halt, a broker outage, or this process crashing
# and being restarted by the 5-minute cron all end with the monitor looking at
# a book as unformed as at 09:15, while is_spread_settled() reports True.
@pytest.fixture(autouse=True)
def _clean_poll_state(monkeypatch):
    """Poll state reset, AND the session clock pinned inside market hours.

    `is_spread_settled` takes an injectable `now` for the resume buffer and
    reads the REAL wall clock for the session buffer -- so with only `now`
    supplied it returns False for every call made before 09:30 IST, whatever
    the test injects. These tests passed all afternoon and failed at 06:33 the
    next morning.

    Third instance of this shape (`test_entry_executor` had two of them a day
    earlier). A suite that only passes during Indian market hours is not a
    suite anybody will trust at 07:00.
    """
    import datetime as _dt

    class _DT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = _dt.datetime(2026, 9, 15, 11, 0, 0)
            return fixed.replace(tzinfo=tz) if tz else fixed
    monkeypatch.setattr(sm, 'datetime', _DT)
    sm.reset_poll_state()
    yield
    sm.reset_poll_state()


def test_the_suite_pins_the_session_clock():
    """The pin above must actually reach the function it disarms.

    Asserted structurally rather than by "it passes right now", which is the
    property that was false: `is_spread_settled` mixes an injected `now` with
    a hidden `now_ist()`, and only the second one decides before 09:30.

    RETIRES WHEN: `is_spread_settled` takes ONE clock, so there is no hidden
    second reading for a fixture to have to pin.
    """
    import inspect
    src = inspect.getsource(sm.is_spread_settled)
    assert 'now_ist()' in src, (
        'is_spread_settled no longer reads the session clock; _clean_poll_state '
        'pins sm.datetime and would now be pinning nothing')
    assert sm.is_spread_settled(now=0.0) is True, (
        'the pinned clock is outside the session buffer, so every value-trigger '
        'test in this file is asserting against a disarmed engine')


def test_a_blackout_rearms_the_spread_buffer(monkeypatch):
    monkeypatch.setattr(sm, 'is_market_settled', lambda: True)
    assert sm.note_poll(True, now=1000.0) is False       # first ever poll
    assert sm.is_spread_settled(now=1005.0) is True

    assert sm.note_poll(True, now=1005.0) is False       # steady polling
    assert sm.note_poll(True, now=2000.0) is True        # ~16 min gap
    assert sm.is_spread_settled(now=2010.0) is False, \
        "value triggers armed on a book that has not re-formed"
    assert sm.is_spread_settled(now=2181.0) is True      # buffer served


def test_normal_polling_never_rearms(monkeypatch):
    monkeypatch.setattr(sm, 'is_market_settled', lambda: True)
    t = 1000.0
    for _ in range(50):                                  # 5s cadence
        assert sm.note_poll(True, now=t) is False
        t += 5
    assert sm.is_spread_settled(now=t) is True


def test_a_failed_poll_does_not_count_as_activity(monkeypatch):
    """The blackout is measured between GOOD polls. Counting a failed one as a
    heartbeat is how an outage looks like a healthy session."""
    monkeypatch.setattr(sm, 'is_market_settled', lambda: True)
    sm.note_poll(True, now=1000.0)
    for t in range(1005, 2000, 5):
        sm.note_poll(False, now=float(t))
    assert sm.note_poll(True, now=2000.0) is True


def test_spot_triggers_are_not_delayed_by_a_resume(monkeypatch):
    """Only the value buffer re-arms. Spot triggers run off real trades and are
    what catches a dead thesis — delaying them suppresses the exit that still
    works."""
    monkeypatch.setattr(sm, 'is_market_settled', lambda: True)
    sm.note_poll(True, now=1000.0)
    sm.note_poll(True, now=2000.0)                       # re-armed
    assert sm.is_spread_settled(now=2010.0) is False
    assert sm.is_market_settled() is True, "a resume delayed the spot triggers"


def test_the_open_buffer_still_wins(monkeypatch):
    """Re-arming must not accidentally UNBLOCK the 09:30 open buffer."""
    monkeypatch.setattr(sm, 'is_market_settled', lambda: True)
    import datetime as _dt

    class _Early(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.combine(_dt.date.today(), sm.MARKET_OPEN)
    monkeypatch.setattr(sm, 'datetime', _Early)
    sm.note_poll(True, now=1000.0)
    assert sm.is_spread_settled(now=1000.0) is False


def test_note_poll_is_wired_into_both_loops():
    src = (HELPER / 'bcs' / 'spread_monitor.py').read_text(encoding='utf-8')
    assert src.count('note_poll(True, now)') == 2, \
        "resumption tracking is defined but not called from both monitor loops"


# ── the playbook's DTE<5 gamma rule (nothing enforced it before) ─────────
def test_gamma_rule_fires_when_little_of_the_max_is_captured():
    t = dict(BCS_TRADE, spread_width=40.0, net_debit=10.0)
    note = sm.gamma_note(t, spread_val=20.0, sessions=3)     # 33% of max
    assert '33% of max' in note and 'gamma' in note


def test_gamma_rule_is_quiet_on_a_position_near_max():
    t = dict(BCS_TRADE, spread_width=40.0, net_debit=10.0)
    assert sm.gamma_note(t, spread_val=36.0, sessions=3) == ''   # 87%


def test_gamma_rule_is_quiet_while_expiry_is_far():
    t = dict(BCS_TRADE, spread_width=40.0, net_debit=10.0)
    assert sm.gamma_note(t, spread_val=20.0, sessions=9) == ''


def test_gamma_rule_says_nothing_without_a_quote():
    """A warning built on a number nobody has is worse than silence."""
    t = dict(BCS_TRADE, spread_width=40.0, net_debit=10.0)
    assert sm.gamma_note(t, spread_val=None, sessions=3) == ''


def test_gamma_rule_survives_a_degenerate_spread():
    assert sm.gamma_note(dict(BCS_TRADE, spread_width=10.0, net_debit=10.0),
                         spread_val=5.0, sessions=3) == ''
    assert sm.gamma_note(dict(BCS_TRADE), spread_val=5.0, sessions=3) == ''


def test_gamma_note_rides_on_the_expiry_warning(_no_telegram):
    """One decision, one notification: it fires in the same window on the same
    position, so a second alert would be pure fatigue."""
    t = dict(BCS_TRADE, spread_width=40.0, net_debit=10.0)
    sm.maybe_warn_expiry_proximity(FakeStore(), t, 150.0, 'BCS',
                                   today=date(2026, 9, 25), spread_val=20.0)
    assert len(_no_telegram) == 1
    assert 'Delivery-margin ramp' in _no_telegram[0]
    assert 'gamma' in _no_telegram[0]


# ── the call-site type bug that killed this warning outright ─────────────
def test_gamma_note_survives_a_quote_dict():
    """get_spread_value returns {'long','short','spread','unreliable'}. Both
    call sites once passed that whole dict, gamma_note did float(dict), and the
    caller's `except Exception` logged 'expiry-proximity check failed' to a file
    nobody reads live — so the delivery-margin warning was dead every day it
    had something to say."""
    t = {'spread_width': 50.0, 'net_debit': 15.0}
    assert sm.gamma_note(t, {'spread': 20.0, 'unreliable': None}, 3) \
        == sm.gamma_note(t, 20.0, 3) != ''


def test_gamma_note_is_silent_on_an_unreliable_book():
    t = {'spread_width': 50.0, 'net_debit': 15.0}
    assert sm.gamma_note(t, {'spread': None, 'unreliable': 'wide'}, 3) == ''


def test_the_call_sites_pass_a_float_not_the_dict():
    """The type guard inside gamma_note is defence in depth, not the fix. If a
    call site regresses, this catches it where the bug actually was."""
    src = (Path(__file__).resolve().parents[1] / 'spread_monitor.py').read_text(
        encoding='utf-8')
    flat = ''.join(src.split())          # whitespace/newline agnostic
    for site in ("spread_val=get_spread_value(kite,trade,spot=spot).get('spread')",
                 "_sv=get_spread_value(kite,t,spot=_spot).get('spread')"):
        assert site in flat, \
            f"expiry-warning call site regressed to passing the quote dict: {site}"
