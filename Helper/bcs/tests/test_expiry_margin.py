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

# Expiry is Thursday 2026-08-27.
EXPIRY = '2026-08-27'
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
    friday = date(2026, 8, 21)
    assert (date(2026, 8, 27) - friday).days == 6      # what calendar says
    assert sm.sessions_to_expiry(BCS_TRADE, friday) == 4


def test_sessions_countdown_through_expiry_week():
    for day, expected in ((date(2026, 8, 24), 3),      # Mon
                          (date(2026, 8, 25), 2),      # Tue
                          (date(2026, 8, 26), 1),      # Wed
                          (date(2026, 8, 27), 0)):     # Thu = expiry
        assert sm.sessions_to_expiry(BCS_TRADE, day) == expected, day


def test_expired_and_unparseable_are_handled():
    assert sm.sessions_to_expiry(BCS_TRADE, date(2026, 9, 1)) == 0
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
                                          today=date(2026, 8, 20))   # 5 left
    assert sent and 'session' in _no_telegram[0]
    assert 'starts in 1 session' in _no_telegram[0]


def test_says_the_ramp_is_active_inside_it(_no_telegram):
    sm.maybe_warn_expiry_proximity(FakeStore(), dict(BCS_TRADE), 150.0, 'BCS',
                                   today=date(2026, 8, 25))          # 2 left
    assert 'ramp ACTIVE' in _no_telegram[0]


def test_quiet_while_expiry_is_far(_no_telegram):
    assert not sm.maybe_warn_expiry_proximity(
        FakeStore(), dict(BCS_TRADE), 150.0, 'BCS', today=date(2026, 8, 10))
    assert _no_telegram == []


def test_expiry_day_is_left_to_the_force_close(_no_telegram):
    """Expiry day already has its own alert and its own automated close. A
    second message there is noise on the one day the user is already watching."""
    assert not sm.maybe_warn_expiry_proximity(
        FakeStore(), dict(BCS_TRADE), 150.0, 'BCS', today=date(2026, 8, 27))
    assert _no_telegram == []


def test_one_warning_per_day_survives_a_monitor_restart(_no_telegram):
    """The cron restarts this process on a 5-minute retry after any crash. An
    unpersisted flag would re-nag on every restart, which trains the user to
    ignore the one alert that costs real money."""
    store = FakeStore()
    t = dict(BCS_TRADE)
    day = date(2026, 8, 25)
    assert sm.maybe_warn_expiry_proximity(store, t, 150.0, 'BCS', today=day)
    assert store.writes == [(1, {'expiry_warn_date': '2026-08-25'})]

    restarted = dict(BCS_TRADE, expiry_warn_date='2026-08-25')
    assert not sm.maybe_warn_expiry_proximity(store, restarted, 150.0, 'BCS',
                                              today=day)
    assert len(_no_telegram) == 1
    # ...but the next session nags again.
    assert sm.maybe_warn_expiry_proximity(store, restarted, 150.0, 'BCS',
                                          today=date(2026, 8, 26))
    assert len(_no_telegram) == 2


def test_a_failed_flag_write_never_swallows_the_warning(_no_telegram):
    """Losing the flag costs a duplicate nag tomorrow. Losing the WARNING costs
    a margin call, so the alert goes out first and the write cannot block it."""
    class Broken(FakeStore):
        def update_trade_fields(self, *a, **k):
            raise RuntimeError('drive down')

    assert sm.maybe_warn_expiry_proximity(Broken(), dict(BCS_TRADE), 150.0,
                                          'BCS', today=date(2026, 8, 25))
    assert len(_no_telegram) == 1


def test_warning_closes_nothing(monkeypatch, _no_telegram):
    """Alert-only, by explicit decision: no new automated close path enters the
    real-order system on expiry proximity."""
    closed = []
    monkeypatch.setattr(sm, 'close_spread',
                        lambda *a, **k: closed.append(a) or 'CLOSED')
    sm.maybe_warn_expiry_proximity(FakeStore(), dict(BCS_TRADE), 150.0, 'BCS',
                                   today=date(2026, 8, 25))
    assert closed == []


def test_warning_is_wired_into_the_cron_startup():
    """The recurring failure in this fleet is code that is written, tested and
    never reached. Assert the cron path actually calls it."""
    src = (HELPER / 'bcs' / 'spread_monitor.py').read_text(encoding='utf-8')
    assert src.count('maybe_warn_expiry_proximity(') >= 3, \
        "warning defined but not called from both monitor paths"
