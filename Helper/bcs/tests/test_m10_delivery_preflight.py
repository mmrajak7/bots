"""M10's two remainders: the moneyness FLOOR, and the E-9 preflight.

The 6-session close is sized to clear the delivery ramp (10/25/45/70% of the
long ITM leg's FULL CONTRACT VALUE at EOD of E-4/E-3/E-2/E-1). Two things were
designed with it in 2026-08-29 and not built.

**Moneyness may only ACCELERATE the close.** The intuition runs backwards,
which is why it is a test and not a comment: a far-OTM spread is worth pennies
at E-6 so holding it earns nothing and risks nothing, while the DEEP-ITM one
converging on max value is the one carrying maximum delivery exposure. The
stored session count is a FLOOR.

**The E-9 preflight** is a SEPARATE two-member gate, and the reason it is not
folded into the exit vet is the whole point of it: the vet's safety argument is
that holding is bounded, and past the delivery deadline that premise inverts —
a long ITM put is a give-delivery obligation auctioned at E+3 with a 20% floor
and no ceiling. A gate whose safety argument has inverted must not have a state
that means "wait".

Run:  cd Helper && PURE_PYTHON=1 python -m pytest bcs/tests/test_m10_delivery_preflight.py -v
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                            # noqa: E402
from bcs.tests.fakes import FakeClock, MemoryStore, TelegramSpy  # noqa: E402

#: Tue 2026-09-29 is a real expiry; 2026-09-21 is its 6-session close and no
#: NSE holiday falls in that window, which is pinned elsewhere.
EXPIRY = '2026-09-29'


def _bcs(**over):
    t = {'id': 1, 'stock': 'TESTCO', 'status': 'open', 'expiry': EXPIRY,
         'long_symbol': 'TESTCO26SEP1340CE', 'short_symbol': 'TESTCO26SEP1390CE',
         'spot_symbol': 'NSE:TESTCO', 'net_debit': 13.55, 'spread_width': 50.0,
         'time_policy': 'sessions_before_expiry', 'time_stop_sessions': 6}
    t.update(over)
    return t


def _bps(**over):
    t = _bcs(long_symbol='TESTCO26SEP1390PE',
             short_symbol='TESTCO26SEP1340PE')
    t.update(over)
    return t


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    FakeClock().install(monkeypatch, sm)
    return TelegramSpy().install(monkeypatch, sm)


@pytest.fixture
def spy(_env):
    return _env


# -- the floor ---------------------------------------------------------------

def test_an_ITM_long_leg_buys_one_session_of_head_start():
    """The margin base is the long ITM leg AT ITS STRIKE — full contract
    value, ~Rs 2.82L against a Rs 2L account. When that obligation exists the
    close comes forward."""
    assert sm.delivery_stop_sessions(_bcs(), spot=1400.0) == 7


def test_an_OTM_long_leg_leaves_the_schedule_alone():
    """No obligation, nothing to accelerate. The negative control: without it
    the test above passes just as well when the bonus is unconditional, and
    every close in the book fires a session early for nothing."""
    assert sm.delivery_stop_sessions(_bcs(), spot=1300.0) == 6


def test_a_SHORT_ITM_leg_alone_changes_nothing():
    """A short ITM leg is the counterparty's obligation, not ours, and the
    broker does not net the two. 'Some leg is ITM' is the wrong question and
    would fire this on a spread carrying no delivery exposure at all."""
    # Long 1340 OTM, short 1390 also OTM at 1300; now put spot between them so
    # only the SHORT of a bear put spread is ITM.
    t = _bps()          # long 1390 PE, short 1340 PE
    assert sm.delivery_stop_sessions(t, spot=1395.0) == 6   # both OTM
    assert sm.delivery_stop_sessions(t, spot=1330.0) == 7   # long PE now ITM


@pytest.mark.parametrize('spot', [None])
def test_unknown_moneyness_leaves_the_schedule_alone(spot):
    """Firing every close a session early because the symbols did not parse is
    a real cost paid for no information."""
    assert sm.delivery_stop_sessions(_bcs(), spot=spot) == 6
    assert sm.delivery_stop_sessions(
        _bcs(long_symbol='NOT-AN-OPTION', short_symbol='ALSO-NOT'),
        spot=1400.0) == 6


def test_the_floor_can_only_RAISE_the_count():
    """The invariant, over the whole cross product. A larger session count
    fires the stop EARLIER; a smaller one pushes an ITM position further INTO
    the ramp, which is the single outcome the schedule exists to avoid."""
    for base in range(0, 12):
        for spot in (1300.0, 1400.0, None):
            t = _bcs(time_stop_sessions=base)
            assert sm.delivery_stop_sessions(t, spot) >= base, (base, spot)


def test_the_floor_is_expressed_as_max_not_as_care():
    """Read off the source. The direction is counter-intuitive enough that the
    next person to edit this arithmetic will get it wrong unless the invariant
    is an operation rather than a convention."""
    import inspect
    assert 'max(base' in inspect.getsource(sm.delivery_stop_sessions)


def test_the_time_stop_consults_the_floor():
    """A rule applied in one place and quietly not in its copy is the most
    frequent bug shape in this codebase."""
    import inspect
    assert 'delivery_stop_sessions' in inspect.getsource(sm.time_stop_due)


def test_the_floor_fires_the_close_a_session_earlier():
    """End to end, on real dates. E-7 is 2026-09-18 (Fri); E-6 is 09-21."""
    itm, otm = _bcs(), _bcs()
    on_e7 = date(2026, 9, 18)
    assert sm.time_stop_due(itm, today=on_e7, spot=1400.0) is True
    assert sm.time_stop_due(otm, today=on_e7, spot=1300.0) is False
    # ...and the OTM one still closes on its own schedule.
    assert sm.time_stop_due(otm, today=date(2026, 9, 21), spot=1300.0) is True


# -- the preflight -----------------------------------------------------------

def test_it_says_nothing_before_E9():
    assert sm.delivery_preflight(_bcs(), 1400.0, 40.0,
                                 today=date(2026, 9, 10)) is None


def test_a_long_ITM_PUT_is_CLOSE_NOW_with_no_value_threshold():
    """The only exposure in this book that can exceed the account. A long ITM
    put is a GIVE-delivery obligation against an empty demat: short delivery
    goes to auction at E+3 with a 20% floor and NO ceiling, and Do-Not-Exercise
    was permanently withdrawn in Jan 2023, so there is no opt-out."""
    v = sm.delivery_preflight(_bps(), 1330.0, spread_val=15.0,
                              today=date(2026, 9, 16))
    assert v['verdict'] == sm.CLOSE_NOW
    assert 'give-delivery' in v['why']


def test_a_long_ITM_CALL_waits_until_there_is_nothing_left_to_win():
    """Capped payoff. Holding full contract-value margin for the last few
    percent is the trade this catches; holding for a third of it is not."""
    early = sm.delivery_preflight(_bcs(), 1400.0, spread_val=30.0,
                                  today=date(2026, 9, 16))
    assert early['verdict'] == sm.CLOSE_ON_SCHEDULE
    late = sm.delivery_preflight(_bcs(), 1400.0, spread_val=48.0,
                                 today=date(2026, 9, 16))
    assert late['verdict'] == sm.CLOSE_NOW
    assert '% of max value' in late['why']


def test_an_OTM_long_leg_is_CLOSE_ON_SCHEDULE():
    v = sm.delivery_preflight(_bcs(), 1300.0, spread_val=48.0,
                              today=date(2026, 9, 16))
    assert v['verdict'] == sm.CLOSE_ON_SCHEDULE
    assert 'no delivery obligation' in v['why']


def test_unreadable_moneyness_does_not_invent_a_verdict():
    v = sm.delivery_preflight(
        _bcs(long_symbol='NOT-AN-OPTION', short_symbol='ALSO-NOT'),
        1400.0, 48.0, today=date(2026, 9, 16))
    assert v['verdict'] == sm.CLOSE_ON_SCHEDULE
    assert 'could not be read' in v['why']


def test_it_is_MONOTONIC():
    """The delivery deadline only ever gets nearer. A verdict that could
    soften would let a quiet afternoon undo a decision the calendar made."""
    t = _bps()
    t['delivery_preflight'] = {'verdict': sm.CLOSE_NOW, 'why': 'earlier'}
    # Spot has moved the long put back OTM — irrelevant.
    v = sm.delivery_preflight(t, 1500.0, 5.0, today=date(2026, 9, 16))
    assert v['verdict'] == sm.CLOSE_NOW
    assert v['monotonic'] is True


def test_the_verdict_space_has_exactly_two_members():
    """No DEFER and no HOLD, and this is the assertion that keeps it that way.

    The exit vet's safety argument is that holding is BOUNDED. Past the
    delivery deadline that premise inverts, so a "wait" state here would be a
    state whose justification no longer exists.
    """
    assert sm.DELIVERY_VERDICTS == (sm.CLOSE_NOW, sm.CLOSE_ON_SCHEDULE)
    assert len(sm.DELIVERY_VERDICTS) == 2


def test_the_exit_vet_was_NOT_extended_to_cover_this():
    """Explicitly out of scope, for the reason above. `EXPIRY_FORCE_CLOSE`
    stays out of `VET_KIND`, and so does anything delivery-shaped."""
    from bcs import exit_vet
    assert 'EXPIRY_FORCE_CLOSE' not in exit_vet.VET_KIND
    assert set(exit_vet.VET_KIND) == {'SL_SPOT', 'SL_SPREAD', 'SL_TRAIL', 'TP'}


# -- recording and announcing ------------------------------------------------

def test_a_CLOSE_NOW_is_recorded_and_announced_once(spy):
    trade = _bps()
    store = MemoryStore(trades=[trade])
    v = sm.delivery_preflight(trade, 1330.0, 15.0, today=date(2026, 9, 16))

    assert sm.record_delivery_preflight(store, trade, v, 'BPS') is True
    assert store.trades[0]['delivery_preflight']['verdict'] == sm.CLOSE_NOW
    assert spy.any('DELIVERY PREFLIGHT — CLOSE NOW')
    assert spy.any('ALERT-ONLY')

    before = len(spy.sent)
    assert sm.record_delivery_preflight(store, trade, v, 'BPS') is False
    assert len(spy.sent) == before, 'a one-way transition alerted twice'


def test_CLOSE_ON_SCHEDULE_does_not_telegram(spy):
    """It is the expected answer on nearly every position in the window.
    Alerting it would bury the one verdict that needs a human."""
    trade = _bcs()
    store = MemoryStore(trades=[trade])
    v = sm.delivery_preflight(trade, 1300.0, 20.0, today=date(2026, 9, 16))
    assert sm.record_delivery_preflight(store, trade, v, 'BCS') is False
    assert spy.sent == []


def test_it_places_no_order():
    """Alert-only. The scheduled close is still the only automated close this
    file does on expiry proximity."""
    import inspect
    for fn in (sm.delivery_preflight, sm.record_delivery_preflight,
               sm.delivery_stop_sessions):
        src = inspect.getsource(fn)
        for forbidden in ('place_limit_order', 'close_spread', 'close_leg',
                          'begin_close'):
            assert forbidden not in src, (fn.__name__, forbidden)


def test_it_runs_from_the_monitor():
    """A gate nobody calls is indistinguishable from one that does not
    exist."""
    import inspect
    assert 'record_delivery_preflight' in inspect.getsource(sm.monitor_all)


def test_a_store_that_refuses_the_write_still_alerts(spy):
    """The ALERT matters more than the record: the record is how tomorrow's
    poll avoids repeating itself, and repeating a safety alert is the
    survivable failure."""
    class _Broken(MemoryStore):
        def update_trade_fields(self, *a, **k):
            raise RuntimeError('store is gone')

    trade = _bps()
    v = sm.delivery_preflight(trade, 1330.0, 15.0, today=date(2026, 9, 16))
    assert sm.record_delivery_preflight(_Broken(trades=[trade]), trade, v,
                                        'BPS') is True
    assert spy.any('CLOSE NOW')
