"""The structure shadow measures; it must never act, and never flatter itself.

These tests are mostly about what the module CANNOT do. The happy path is one
test; the rest pin the four ways a shadow quietly turns into either a trading
path or an optimistic one.
"""
import json
from datetime import date, datetime

import pytest

from zebra import structure_shadow as ss


# -- fixtures ----------------------------------------------------------------

def _trade(**kw):
    t = {
        'id': 1, 'status': 'entered', 'cohort': '2026-08-14',
        'stock': 'ACME', 'direction': 'CE',
        'long_symbol': 'ACME26SEP100CE', 'short_symbol': 'ACME26SEP104CE',
        'tp_spot': 104.0, 'expiry': '2026-09-24', 'quantity': 100,
        'entry_spot': 100.0, 'entry_date': '2026-09-01',
        'long_ask_entry': 4.0, 'debit': 2.0, 'width': 4.0,
    }
    t.update(kw)
    return t


def _shadow(**kw):
    state = {'schema': ss.SCHEMA, 'shadows': {}}
    ss.open_shadows(state, [_trade(**kw)], '2026-09-01 10:00:00')
    return state['shadows']['1']


def _q(bid, ask, reliable=True):
    return {'bid': bid, 'ask': ask, 'reliable': reliable,
            'unreliable_reason': '' if reliable else 'wide_book'}


MID_SESSION = datetime(2026, 9, 10, 12, 30, 0)
TODAY = date(2026, 9, 10)


# -- pricing -----------------------------------------------------------------

def test_arms_price_at_the_side_they_would_actually_trade_against():
    """Sell the long at the BID, buy the short back at the ASK. A mid here
    would quote a value nobody can transact at -- the exact defect that made
    every pre-2026-08-12 record optimistic at both ends."""
    lq, sq = _q(5.0, 6.0), _q(2.0, 3.0)
    assert ss.arm_value(ss.ARMS['naked_long'], lq, sq)[0] == 5.0
    assert ss.arm_value(ss.ARMS['spread_hold'], lq, sq)[0] == 5.0 - 3.0


@pytest.mark.parametrize('lq,sq,arm', [
    (_q(5.0, 6.0, reliable=False), _q(2.0, 3.0), 'naked_long'),
    (_q(5.0, 6.0), _q(2.0, 3.0, reliable=False), 'spread_hold'),
    (None, None, 'naked_long'),
])
def test_an_unreliable_book_defers_instead_of_booking(lq, sq, arm):
    v, why = ss.arm_value(ss.ARMS[arm], lq, sq)
    assert v is None and why


def test_a_missing_bid_defers_rather_than_booking_a_total_loss():
    """`_quote_option` returns 0 for both 'no bid' and 'a real bid of zero'.
    Booking the second is right and booking the first invents a -100%."""
    assert ss.arm_value(ss.ARMS['naked_long'], _q(0, 6.0), None)[0] is None


def test_delta1_is_direction_adjusted_or_every_pe_reads_backwards():
    """A PE signal is SHORT the underlying. Marking it at raw spot books a
    profitable fall as a loss and makes `peak` track the worst point."""
    assert ss.delta1_mark('CE', 100.0, 103.0) == 103.0
    assert ss.delta1_mark('PE', 100.0, 97.0) == 103.0     # a fall is a GAIN
    assert ss.delta1_mark('PE', 100.0, 103.0) == 97.0


# -- what must not happen ----------------------------------------------------

def test_a_booked_exit_is_never_repriced():
    """A stop fires once. Re-pricing it on a later, deeper print re-prices the
    counterfactual to something no stop would ever have got."""
    sh = _shadow()
    ss.poll_one(sh, 100.0, _q(1.0, 1.2), _q(0.4, 0.5), MID_SESSION, TODAY)
    first = dict(sh['arms']['naked_long']['exit'])
    assert first['reason'] == 'stop'
    ss.poll_one(sh, 100.0, _q(0.1, 0.2), _q(0.05, 0.1), MID_SESSION, TODAY)
    assert sh['arms']['naked_long']['exit'] == first


def test_value_stops_stand_down_in_the_opening_buffer():
    """Both incidents that cost this book real money were on a session's first
    prints. The shadow inherits the same dark window, or it authorises a rule
    the live engine would not have taken."""
    sh = _shadow()
    at_open = datetime(2026, 9, 10, 9, 20, 0)          # inside the 900s buffer
    ss.poll_one(sh, 100.0, _q(1.0, 1.2), _q(0.4, 0.5), at_open, TODAY)
    assert sh['arms']['naked_long']['status'] == 'open'
    ss.poll_one(sh, 100.0, _q(1.0, 1.2), _q(0.4, 0.5), MID_SESSION, TODAY)
    assert sh['arms']['naked_long']['status'] == 'exited'


def test_spot_triggers_stand_down_in_the_cash_closing_auction(monkeypatch):
    """Spot cannot print between 15:15 and 15:35, so a TP read there is a claim
    about a price that does not exist."""
    monkeypatch.setattr(ss.market_session, 'cash_price_is_frozen',
                        lambda now=None: True)
    sh = _shadow()
    ss.poll_one(sh, 999.0, _q(5.0, 6.0), _q(1.0, 1.2), MID_SESSION, TODAY)
    assert all(a['status'] == 'open' for a in sh['arms'].values())


def test_the_time_stop_fires_even_when_nothing_quotes():
    """TIME is a statement about the calendar, not about a price. Gated on a
    quote, a blind spell at expiry leaves an arm open past its own contract."""
    sh = _shadow(expiry='2026-09-11')
    ss.poll_one(sh, None, None, None, MID_SESSION, TODAY)
    assert all(a['exit']['reason'] == 'time' for a in sh['arms'].values())


def test_only_cohort_positions_are_shadowed():
    """Mixing the pre-cohort book in is exactly what made the last naked-long
    answer point both ways at once."""
    state = {'schema': ss.SCHEMA, 'shadows': {}}
    assert ss.open_shadows(state, [_trade(id=9, cohort=None)], 'x') == 0
    assert state['shadows'] == {}


def test_the_module_cannot_place_an_order_or_send_a_telegram():
    """Structural, not a promise in the docstring. A measurement that can reach
    the order path is not a measurement."""
    src = open(ss.__file__, encoding='utf-8').read()
    for forbidden in ('place_order', 'send_telegram', '_send_telegram',
                      'save_trade', 'mark_exited', 'update_trade'):
        assert forbidden not in src, forbidden


def test_poll_never_raises_into_the_cycle(monkeypatch):
    """It runs inside run_cycle. A measurement that can throw is a new way to
    fail the half of the cycle that trades."""
    class Boom:
        def load_trades(self):
            raise RuntimeError('store is down')
    assert ss.poll(Boom(), kite=None) == {}


# -- honesty of the count ----------------------------------------------------

def test_a_shadow_opened_after_entry_is_marked_partial():
    """Its stop may already have fired where nothing was watching, so it is a
    lower bound and must not be counted as an observation.

    SAME DAY IS NOT THE SAME AS OBSERVED FROM ENTRY -- a shadow opened at 14:00
    on a position that entered at 09:30 has missed four and a half hours, and a
    date comparison calls that complete."""
    t = {'entry_date': '2026-09-01', 'entry_time': '09:30:00'}
    # the ordinary case: entry happens after the exit phase, so the shadow
    # legitimately opens on the NEXT cycle
    assert ss.opened_late(t, '2026-09-01 09:35:00') is False
    assert ss.opened_late(t, '2026-09-01 14:00:00') is True
    assert ss.opened_late(t, '2026-09-02 09:35:00') is True
    # unknown or unparseable stamps fail toward PARTIAL, never toward complete
    assert ss.opened_late({'entry_date': '2026-09-01'},
                          '2026-09-01 14:00:00') is False
    assert ss.opened_late(t, 'not-a-timestamp') is True


def test_arms_carry_their_own_fill_count_so_fees_are_not_shared():
    """A naked arm is a 2-fill round trip against the spread's 4. Comparing
    them gross flatters the 4-fill one by a fee it did not avoid."""
    sh = _shadow()
    assert sh['arms']['naked_long']['fills'] == 2
    assert sh['arms']['spread_hold']['fills'] == 4


def test_backfill_refuses_closed_positions(tmp_path, monkeypatch):
    """A closed position's path stops at its own exit -- which is exactly where
    `naked_runner` gets interesting. Seeding one bakes in an unobserved TAIL
    and silently answers the question this module exists to ask."""
    monkeypatch.setattr(ss, 'STATE_FILE', tmp_path / 'shadow.json')
    day = {'trades': {'1': {'obs': [
        {'ts': '2026-09-01 10:00:00', 'spot': 100.0, 'q': 'ok',
         'long_bid': 4.1, 'long_ask': 4.3, 'short_bid': 2.0, 'short_ask': 2.2}]}}}
    (tmp_path / 'paths_2026-09-01.json').write_text(json.dumps(day),
                                                    encoding='utf-8')

    class Store:
        def __init__(self, status):
            self._s = status
        def load_trades(self):
            return [_trade(status=self._s)]

    assert ss.backfill(Store('exited'), paths_dir=tmp_path)['seeded'] == []
    r = ss.backfill(Store('entered'), paths_dir=tmp_path)
    assert [x[0] for x in r['seeded']] == ['1']


def test_backfill_is_safe_to_rerun(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, 'STATE_FILE', tmp_path / 'shadow.json')
    day = {'trades': {'1': {'obs': [
        {'ts': '2026-09-01 10:00:00', 'spot': 100.0, 'q': 'ok',
         'long_bid': 4.1, 'long_ask': 4.3, 'short_bid': 2.0, 'short_ask': 2.2}]}}}
    (tmp_path / 'paths_2026-09-01.json').write_text(json.dumps(day),
                                                    encoding='utf-8')

    class Store:
        def load_trades(self):
            return [_trade()]

    assert len(ss.backfill(Store(), paths_dir=tmp_path)['seeded']) == 1
    assert ss.backfill(Store(), paths_dir=tmp_path)['seeded'] == []


def test_backfilled_legs_inherit_the_engines_own_quality_verdict():
    """The paths store no depth, so a locally re-derived reliability rule would
    fail every leg for want of `bid_qty` -- and a second spelling of a guard is
    how this codebase grows two of them."""
    ok = ss._obs_leg({'q': 'ok', 'long_bid': 1.0, 'long_ask': 1.2}, 'long')
    bad = ss._obs_leg({'q': 'no_two_way_book', 'long_bid': 1.0,
                       'long_ask': 1.2}, 'long')
    assert ok['reliable'] is True and bad['reliable'] is False
    assert ss._obs_leg({'q': 'ok'}, 'short') is None
