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
        'long_ask_entry': 4.0, 'short_bid_entry': 2.0,
        'entry_time': '09:30:00', 'debit': 2.0, 'width': 4.0,
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
    """TIME is a statement about the calendar, not about a price, so it must
    FIRE without a quote -- gated on one, a blind spell at expiry leaves an arm
    open past its own contract.

    Firing is not the same as BOOKING. With no price the trigger latches and
    the booking waits, which is why this asserts `pending` rather than an
    exit; `test_naked_runner_survives_a_blind_time_deadline` covers the rest."""
    sh = _shadow(expiry='2026-09-11')
    ss.poll_one(sh, None, None, None, MID_SESSION, TODAY)
    assert all(a['pending']['reason'] == 'time' for a in sh['arms'].values())
    assert all(a['status'] == 'open' for a in sh['arms'].values())


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


# -- a trigger that cannot be priced is not an exit yet -----------------------

def test_a_tp_on_an_unusable_book_latches_and_books_at_the_next_price():
    """TP fires on SPOT, so it can arrive on a poll where the option book is
    unusable. Booking value=None there records an exit with no P&L, which a
    reader then has to either drop (losing the observation) or count as zero."""
    sh = _shadow()
    dead = _q(5.0, 6.0, reliable=False)
    ss.poll_one(sh, 104.0, dead, dead, MID_SESSION, TODAY)
    a = sh['arms']['naked_long']
    assert a['status'] == 'open' and a['pending']['reason'] == 'tp'

    ss.poll_one(sh, 104.0, _q(7.0, 7.4), _q(3.0, 3.2), MID_SESSION, TODAY)
    assert a['status'] == 'exited'
    assert a['exit']['reason'] == 'tp' and a['exit']['value'] == 7.0
    assert a['exit']['unpriced'] is False


def test_a_latched_trigger_is_not_released_when_the_condition_stops_holding():
    """The trigger DID fire. Re-deciding it on a later print is a different
    rule, and it is the rule that would quietly never book a stop."""
    sh = _shadow()
    dead = _q(5.0, 6.0, reliable=False)
    ss.poll_one(sh, 104.0, dead, dead, MID_SESSION, TODAY)      # TP fires, unpriced
    assert sh['arms']['naked_long']['pending']['reason'] == 'tp'
    ss.poll_one(sh, 90.0, _q(7.0, 7.4), _q(3.0, 3.2), MID_SESSION, TODAY)
    assert sh['arms']['naked_long']['exit']['reason'] == 'tp'   # not 'stop', not open


def test_naked_runner_survives_a_blind_time_deadline():
    """`naked_runner`'s ONLY exit is TIME. One unusable book on the deadline
    poll used to book it at value=None -- losing the arm this whole module
    exists to measure, on exactly the position where it mattered."""
    sh = _shadow(expiry='2026-09-11')                # inside TIME_SL_DAYS
    ss.poll_one(sh, None, None, None, MID_SESSION, TODAY)
    a = sh['arms']['naked_runner']
    assert a['status'] == 'open' and a['pending']['reason'] == 'time'
    ss.poll_one(sh, 100.0, _q(6.0, 6.4), _q(2.0, 2.2), MID_SESSION, TODAY)
    assert a['exit']['reason'] == 'time' and a['exit']['value'] == 6.0


def test_expiry_is_the_backstop_and_records_unpriced_rather_than_hanging():
    """Past its own contract no future poll can price it. Leaving the arm open
    would report it as still being measured when it never can be again."""
    sh = _shadow(expiry='2026-09-10')
    ss.poll_one(sh, None, None, None, MID_SESSION, date(2026, 9, 10))
    a = sh['arms']['naked_runner']
    assert a['status'] == 'exited'
    assert a['exit']['unpriced'] is True and a['exit']['pnl_pct'] is None


def test_unpriced_exits_are_reported_not_silently_dropped(tmp_path, monkeypatch):
    """An unpriceable book correlates with a bad outcome, so discarding these
    rows biases the count in the optimistic direction."""
    monkeypatch.setattr(ss, 'STATE_FILE', tmp_path / 's.json')
    state = {'schema': ss.SCHEMA, 'shadows': {}}
    ss.open_shadows(state, [_trade(expiry='2026-09-10')], '2026-09-01 10:00:00')
    ss.poll_one(state['shadows']['1'], None, None, None,
                MID_SESSION, date(2026, 9, 10))
    ss._save(state)
    sc = ss.scorecard()
    assert sc['arms']['naked_runner'] == []
    assert [r['id'] for r in sc['unpriced']['naked_runner']] == ['1']


# -- the count must not flatter an arm ---------------------------------------

def test_the_exit_book_is_persisted_not_just_the_scalar():
    """An option book cannot be reconstructed after the fact, and it is what
    lets fees be costed per leg instead of scaled off the spread's total."""
    sh = _shadow()
    ss.poll_one(sh, 104.0, _q(7.0, 7.4), _q(3.0, 3.2), MID_SESSION, TODAY)
    legs = sh['arms']['spread_hold']['exit']['legs']
    assert legs['long']['bid'] == 7.0 and legs['short']['ask'] == 3.2


def test_fees_follow_turnover_so_a_naked_arm_is_not_half_a_spread():
    """Charges scale with turnover, not fill count. A naked arm buys the whole
    ATM premium where the spread pays a net debit about half that, so halving
    the spread's fee to model a 2-fill arm understates it."""
    sh = _shadow()
    ss.poll_one(sh, 104.0, _q(7.0, 7.4), _q(3.0, 3.2), MID_SESSION, TODAY)
    naked = ss.arm_fees(ss.ARMS['naked_long'], sh, sh['arms']['naked_long'])
    spread = ss.arm_fees(ss.ARMS['spread_hold'], sh, sh['arms']['spread_hold'])
    assert naked and spread
    assert naked > spread / 2.0, (naked, spread)


def test_a_reference_arm_is_not_given_an_option_fee_it_would_never_pay():
    sh = _shadow()
    assert ss.arm_fees(ss.ARMS['delta1'], sh, sh['arms']['delta1']) is None


def test_holding_period_is_surfaced_because_the_arms_do_not_hold_alike():
    """`naked_runner` runs to its TIME stop (~28d) against the spread's ~5. A
    per-trade return that takes five times as long is not the same return."""
    sh = _shadow()
    sh['since'] = '2026-09-01 10:00:00'
    assert ss._days_held(sh, '2026-09-15 10:00:00') == 14
    assert ss._days_held(sh, 'rubbish') is None


def test_an_unknown_direction_is_refused_rather_than_shadowed_as_a_put():
    """`_tp_hit` and `delta1_mark` both treat 'not CE' as PE, so an unknown
    direction would be silently measured on the wrong side of the market."""
    state = {'schema': ss.SCHEMA, 'shadows': {}}
    assert ss.open_shadows(state, [_trade(direction='XX')], 'x') == 0


def test_an_uncosted_arm_reports_unknown_not_free():
    """A missing exit book means the fee is UNKNOWN. Returning 0 there makes
    an arm look cheaper than it is, in the net column, permanently."""
    sh = _shadow()
    sh.pop('short_bid_entry')                       # e.g. a pre-schema record
    ss.poll_one(sh, 104.0, _q(7.0, 7.4), _q(3.0, 3.2), MID_SESSION, TODAY)
    assert ss.arm_fees(ss.ARMS['spread_hold'], sh, sh['arms']['spread_hold']) is None
    # and the scorecard must not fold it into net as a zero
    a = sh['arms']['spread_hold']
    gross = (a['exit']['value'] - a['entry_value']) * sh['quantity']
    assert gross != 0


def test_an_unpriced_exit_cannot_be_costed_either():
    """No exit book, no round trip. This is the same hole as the one above,
    reached from the other direction."""
    sh = _shadow(expiry='2026-09-10')
    ss.poll_one(sh, None, None, None, MID_SESSION, date(2026, 9, 10))
    a = sh['arms']['naked_runner']
    assert a['exit']['unpriced'] is True
    assert ss.arm_fees(ss.ARMS['naked_runner'], sh, a) is None


def test_a_spread_arm_is_clamped_to_its_mathematical_bounds():
    """A vertical cannot be worth less than 0 (expiry is always available and
    costs nothing) nor more than its width. Without the floor a wide book books
    a P&L past -100% on a -100%-capped structure -- PIIND #50 read -112.4%."""
    arm = ss.ARMS['spread_hold']
    # long bid 0.55 / short ask 0.60 is an ORDINARY book for a worthless spread
    assert ss.arm_value(arm, _q(0.55, 0.70), _q(0.50, 0.60), 4.0)[0] == 0.0
    assert ss.arm_value(arm, _q(9.0, 9.5), _q(0.1, 0.2), 4.0)[0] == 4.0
    # unknown width still floors at zero -- the bound that costs money
    assert ss.arm_value(arm, _q(0.55, 0.70), _q(0.50, 0.60), None)[0] == 0.0


def test_the_clamp_is_not_applied_to_a_naked_long():
    """A long option has no upper bound. Clamping it to the spread's width
    would truncate exactly the uncapped upside the arm exists to measure."""
    v = ss.arm_value(ss.ARMS['naked_long'], _q(20.0, 20.5), None, 4.0)[0]
    assert v == 20.0


def test_an_arm_with_unresolved_positions_is_reported_CENSORED(tmp_path, monkeypatch):
    """THE ARMS DO NOT CENSOR ALIKE, and that manufactures a win rate.

    An arm with a stop closes on both sides; one without closes on TP (~4 days)
    or TIME (~28), so before the first TIME exits land its closed set is nearly
    all winners. Driving the 23 closed cohort positions through this machinery
    showed naked_long (stop) at 57.9% over 19 against naked_hold (same arm, no
    stop) at 91.7% over 12 -- a 34-point gap that is pure censoring. This book
    has already been fooled by the same shape: 7 wins from 7 closes.
    """
    monkeypatch.setattr(ss, 'STATE_FILE', tmp_path / 's.json')
    state = {'schema': ss.SCHEMA, 'shadows': {}}
    ss.open_shadows(state, [_trade(id=1), _trade(id=2)], '2026-09-01 09:31:00')
    # resolve ONE of the two, leaving the other running
    ss.poll_one(state['shadows']['1'], 104.0, _q(7.0, 7.4), _q(3.0, 3.2),
                MID_SESSION, TODAY)
    ss._save(state)
    sc = ss.scorecard()
    assert sc['still_open']['naked_hold'] == 1
    assert ss.censored(sc, 'naked_hold') is True

    # and once nothing is left running, it stops being censored
    ss.poll_one(state['shadows']['2'], 104.0, _q(7.0, 7.4), _q(3.0, 3.2),
                MID_SESSION, TODAY)
    ss._save(state)
    assert ss.censored(ss.scorecard(), 'naked_hold') is False
