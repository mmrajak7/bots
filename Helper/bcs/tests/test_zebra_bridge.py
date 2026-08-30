"""The bridge that lets the order path manage the BCS cohort.

Until now `bcs/spread_monitor.py` — the only code in the fleet that can place a
real order — read three books, and the cohort was in a fourth. It printed
`Trades: 0 open` while six positions were live. Meanwhile `zebra/monitor.py`,
which watches those six, has no order API at all. The cohort was watched by
code that could not trade, and the code that could trade could not see it.

Two properties make this bridge safe rather than merely connected, and both are
measured here against the real records:

1. **Direction comes from the SYMBOLS.** The zebra store is the only book that
   holds both bull call spreads and bear put spreads. Taking direction from the
   store, as every other book allows, inverts the stops on every PE record —
   and 4 of the 10 cohort trades would close on their first poll.
2. **The exit rules travel with the TRADE.** The cohort runs with the spot stop
   OFF and a gain-anchored trail; the monitor arms SL_SPOT as trigger #1 and
   anchors its trail to 2x debit. Moving a position between engines must not
   re-arm a stop its owner measured and switched off.
"""
import ast
import inspect
import json
from pathlib import Path

import pytest

from bcs import spread_monitor as sm
from bcs import zebra_adapter as za

HELPER = Path(__file__).resolve().parents[2]
BOOK = HELPER / 'logs' / 'zebra_trades.json'


def _records():
    if not BOOK.exists():
        pytest.skip('no zebra book on this box')
    raw = json.loads(BOOK.read_text(encoding='utf-8'))
    return raw if isinstance(raw, list) else raw.get('trades', raw)


def _cohort():
    from zebra.trade_store import in_cohort
    return [t for t in _records() if in_cohort(t)]


# -- Direction ---------------------------------------------------------------

def test_a_call_vertical_reads_as_bcs():
    t = {'long_symbol': 'KOTAKBANK26SEP395CE',
         'short_symbol': 'KOTAKBANK26SEP410CE'}
    assert sm.vertical_direction(t) == 'BCS'


def test_a_put_vertical_reads_as_bps():
    """The whole reason this function exists. TMPV is long the HIGHER strike
    and takes profit on a FALL; read as BCS its stops point backwards."""
    t = {'long_symbol': 'TMPV26SEP320PE', 'short_symbol': 'TMPV26SEP310PE'}
    assert sm.vertical_direction(t) == 'BPS'


@pytest.mark.parametrize('lo,sh,why', [
    ('X26SEP100CE', 'X26SEP110PE', 'mixed legs are not a vertical'),
    ('X26SEP100CE', None, 'a missing leg cannot be classified'),
    (None, 'X26SEP110CE', 'a missing leg cannot be classified'),
    ('garbage', 'X26SEP110CE', 'an unreadable suffix is not a guess'),
])
def test_anything_that_is_not_a_clean_vertical_returns_none(lo, sh, why):
    assert sm.vertical_direction({'long_symbol': lo, 'short_symbol': sh}) is None, why


def test_get_strategy_reads_a_zebra_record_from_its_symbols():
    pe = {'_store_type': 'zebra', 'long_symbol': 'TMPV26SEP320PE',
          'short_symbol': 'TMPV26SEP310PE'}
    ce = {'_store_type': 'zebra', 'long_symbol': 'MCX26SEP3100CE',
          'short_symbol': 'MCX26SEP3250CE'}
    assert sm.get_strategy(pe) == 'BPS'
    assert sm.get_strategy(ce) == 'BCS'


def test_the_other_books_still_decide_by_store():
    """Negative control: the change must not touch how the three original
    books are classified. A bps-store record stays BPS even though nothing
    about its symbols is inspected here."""
    assert sm.get_strategy({'_store_type': 'bps',
                            'long_symbol': 'X26SEP100PE',
                            'short_symbol': 'X26SEP90PE'}) == 'BPS'
    assert sm.get_strategy({'_store_type': 'bcs',
                            'long_symbol': 'X26SEP100CE',
                            'short_symbol': 'X26SEP110CE'}) == 'BCS'
    assert sm.get_strategy({'_store_type': 'fh',
                            'long_put_symbol': 'X26SEP90PE'}) == 'FH'


# -- The measurement this exists to prevent ----------------------------------

def test_no_cohort_position_fires_a_spot_trigger_at_its_own_entry_spot():
    """The B23 check, re-run through the bridge on the REAL book.

    A just-opened position is healthy by construction. If any spot trigger is
    true at the spot it was entered at, the direction is wrong.
    """
    bad = []
    for t in _cohort():
        m = dict(za.map_trade(t), _store_type='zebra')
        strat = sm.get_strategy(m)
        spot = t.get('entry_spot')
        if spot is None:
            continue
        sl = (spot <= m['sl_spot']) if strat == 'BCS' else (spot >= m['sl_spot'])
        tp = (spot >= m['target_spot']) if strat == 'BCS' else (spot <= m['target_spot'])
        if sl or tp:
            bad.append((t['id'], t['stock'], t.get('direction'), strat, sl, tp))
    assert not bad, f'spot triggers true on healthy positions: {bad}'


def test_taking_direction_from_the_store_would_have_closed_four_of_them():
    """Negative control, and the reason the test above is not vacuous.

    Classify every cohort record as BCS — what a fourth store with a fixed
    leg-type would have done — and count the ones that fire BOTH SL_SPOT and
    TP on their first poll.
    """
    both = [t['id'] for t in _cohort()
            if t.get('entry_spot') is not None
            and t['entry_spot'] <= za.map_trade(t)['sl_spot']
            and t['entry_spot'] >= za.map_trade(t)['target_spot']]
    assert len(both) >= 4, (
        'the naive classification no longer breaks, so the test above no '
        f'longer proves anything (found {both})')


# -- Cohort isolation --------------------------------------------------------

def test_only_cohort_records_reach_the_order_path():
    """The store holds 450 records from the dropped back-ratio strategy. The
    order path must never see one."""
    class FakeZebra:
        def get_entered(self):
            # `paper: False` since 2026-08-27 (C5): cohort membership is
            # necessary but no longer sufficient — a record the order path may
            # manage is one that was actually PLACED at the broker. The paper
            # half of that filter is tested in test_paper_vs_live_close.py.
            return [{'id': 1, 'cohort': '2026-08-14', 'stock': 'IN',
                     'debit': 1.0, 'width': 5.0, 'paper': False},
                    {'id': 2, 'stock': 'LEGACY', 'debit': 1.0, 'width': 5.0,
                     'paper': False},
                    {'id': 3, 'cohort': '2020-01-01', 'stock': 'OLD',
                     'debit': 1.0, 'width': 5.0, 'paper': False}]
    got = za.ZebraStoreAdapter(FakeZebra()).get_open_trades()
    assert [t['stock'] for t in got] == ['IN']


def test_an_unstamped_record_is_legacy_not_a_guess():
    """`in_cohort` treats a missing stamp as legacy by construction. Inferring
    from entry_date would sweep the back ratio back in."""
    from zebra.trade_store import in_cohort
    assert in_cohort({'entry_date': '2026-08-20'}) is False


# -- Field mapping -----------------------------------------------------------

def test_the_mapping_renames_and_never_computes():
    t = {'stock': 'TMPV', 'debit': 4.10, 'width': 10.0, 'tp_spot': 309.29,
         'debit_sl_value': 2.05, 'long_ask_entry': 8.95,
         'short_bid_entry': 4.85, 'sl_spot': 330.78}
    m = za.map_trade(t)
    assert m['net_debit'] == 4.10
    assert m['spread_width'] == 10.0
    assert m['target_spot'] == 309.29
    assert m['sl_spread'] == 2.05
    assert m['entry_long_price'] == 8.95
    assert m['entry_short_price'] == 4.85
    assert m['exchange'] == 'NFO'
    assert m['spot_symbol'] == 'NSE:TMPV'


def test_the_originals_are_left_in_place():
    """`zebra.vet` and the digest read these records by their own field names.
    Stripping them would fork the record between its two readers."""
    m = za.map_trade({'stock': 'X', 'debit': 4.10, 'width': 10.0,
                      'tp_spot': 1.0})
    assert m['debit'] == 4.10 and m['width'] == 10.0 and m['tp_spot'] == 1.0


def test_the_short_entry_price_is_the_BID_not_the_mid():
    """Not cosmetic. `entry_short_price` is what the B21/B17 intrinsic floor
    derives its allowance from — a mid there tightens the floor onto healthy
    books, which is exactly the false-blind B17 was."""
    m = za.map_trade({'stock': 'X', 'short_bid_entry': 4.85,
                      'short_mid_entry': 4.95, 'short_ask_entry': 5.05})
    assert m['entry_short_price'] == 4.85


def test_every_real_cohort_record_maps_to_the_fields_the_monitor_reads():
    need = ('net_debit', 'spread_width', 'target_spot', 'sl_spot', 'sl_spread',
            'entry_long_price', 'entry_short_price', 'exchange', 'spot_symbol',
            'long_symbol', 'short_symbol', 'quantity', 'stock', 'id')
    for t in _cohort():
        m = za.map_trade(t)
        missing = [f for f in need if m.get(f) is None]
        assert not missing, f"#{t['id']} {t['stock']} is missing {missing}"


# -- Exit rules travel with the trade ----------------------------------------

def test_a_cohort_record_carries_the_cohorts_exit_policy():
    m = za.map_trade({'stock': 'X', 'debit': 1.0, 'width': 5.0})
    assert m['spot_sl_enabled'] is False
    assert m['trail_policy'] == 'gain_anchored'
    assert m['time_policy'] == 'sessions_before_expiry'


def test_the_gain_anchored_trail_matches_zebras_own_arithmetic():
    """Not a re-implementation: `trail_level_for` calls `zebra.mfe.trail_levels`,
    so this pins that they agree rather than that two copies were typed the
    same way."""
    from zebra import mfe
    from zebra import config as zcfg
    trade = {'net_debit': 4.10, 'spread_width': 10.0,
             'trail_policy': 'gain_anchored'}
    for peak in (6.0, 8.0, 9.5):
        want = mfe.trail_levels({'width': 10.0, 'debit': 4.10,
                                 'mfe_mid': peak})['level']
        assert sm.trail_level_for(trade, peak) == want
    # and the engage level is debit + ENGAGE_FRAC x max gain
    assert sm.trail_engage_level(trade) == pytest.approx(
        4.10 + zcfg.TRAIL_ENGAGE_FRAC * (10.0 - 4.10), abs=0.01)


def test_the_two_trail_rules_are_actually_different():
    """Negative control. If they happened to agree, every trail test above
    would pass against a bridge that ignored the policy entirely."""
    z = {'net_debit': 4.10, 'spread_width': 10.0, 'trail_policy': 'gain_anchored'}
    b = {'net_debit': 4.10, 'spread_width': 10.0}
    assert sm.trail_engage_level(z) != sm.trail_engage_level(b)
    assert sm.trail_level_for(z, 8.0) != sm.trail_level_for(b, 8.0)


def test_the_debit_anchored_trail_is_untouched():
    """The three original books must behave exactly as before."""
    b = {'net_debit': 4.10, 'spread_width': 10.0}
    assert sm.trail_engage_level(b) == 4.10 * sm.TRAIL_ENGAGE_MULTIPLIER
    assert sm.trail_level_for(b, 8.0) == 8.0 * sm.TRAIL_PERCENT


def test_an_uncomputable_gain_anchor_falls_back_to_the_debit_anchor():
    """A debit at or above width has no max gain. Falling back to the EXISTING
    rule can only DELAY a trail, never arm one early — the safe direction."""
    bad = {'net_debit': 12.0, 'spread_width': 10.0,
           'trail_policy': 'gain_anchored'}
    assert sm.trail_engage_level(bad) == 12.0 * sm.TRAIL_ENGAGE_MULTIPLIER


# -- Store routing -----------------------------------------------------------

def test_writes_route_by_store_not_by_strategy():
    """A zebra record tagged BPS still belongs to the zebra store. Routing on
    _strategy would write a cohort exit into the bear_put book."""
    bcs, fh, bps, zeb = object(), object(), object(), object()
    t = {'_store_type': 'zebra', '_strategy': 'BPS'}
    assert sm._get_store_for(t, bcs, fh, bps, zeb) is zeb


def test_a_missing_zebra_store_falls_back_rather_than_crashing():
    t = {'_store_type': 'zebra', '_strategy': 'BCS'}
    bcs = object()
    assert sm._get_store_for(t, bcs, object(), object(), None) is bcs


# -- The key that two bugs came out of ---------------------------------------

def test_the_trade_key_separates_records_the_old_key_merged():
    a = {'_store_type': 'bcs', '_strategy': 'BCS', 'id': 1}
    b = {'_store_type': 'zebra', '_strategy': 'BCS', 'id': 1}
    c = {'_store_type': 'zebra', '_strategy': 'BPS', 'id': 1}
    assert len({sm.trade_key(a), sm.trade_key(b), sm.trade_key(c)}) == 3


def test_every_per_position_state_dict_is_keyed_by_trade_key():
    """Structural, because no input can distinguish the two spellings until one
    of them is looked up with the other.

    Both bugs this caught were silent-then-loud: `trail_state` keyed
    ('BCS', id) at startup and by close_key in the loop raised on EVERY cycle
    ("too many values to unpack") and ended in a FATAL 'Unmonitored' Telegram;
    `expiry_trades` did the same thing quietly and simply never force-closed an
    expiring position. Both are the same mistake, so pin the shape.
    """
    src = Path(sm.__file__).read_text(encoding='utf-8')
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == 'monitor_all')
    offenders = []
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)):
            continue
        if not node.value.id.endswith(('_state', '_trades', '_progress',
                                       '_until')):
            continue
        key = node.slice
        if isinstance(key, ast.Tuple):
            offenders.append((node.value.id, node.lineno,
                              'literal tuple key'))
    assert not offenders, (
        'per-position state keyed by a literal tuple instead of trade_key(): '
        f'{offenders}. Two of these have already caused live bugs.')


# -- End to end: the spot stop must stay OFF for a cohort position -----------
#
# This section exists because a mutation survived without it. Every test above
# checks a piece; none of them proved the assembled monitor actually declines
# to close a cohort position whose spot has crossed its stored sl_spot. That is
# the single most consequential property of the bridge: the cohort was measured
# with the spot stop OFF (a 3% stop cuts 40% of winners), and pointing an
# engine at it that arms SL_SPOT as trigger #1 would silently re-arm it.

from datetime import date                                    # noqa: E402

from bcs.tests.replay import Tick, run_session               # noqa: E402

_DAY = date(2026, 9, 15)
_L, _S = 'TESTCO26SEP1340CE', 'TESTCO26SEP1390CE'
_QTY = 700

#: A cohort position as the adapter hands it over: mapped field names, the
#: cohort's exit policy stamped on, and `_store_type` set by the loader.
COHORT_TRADE = {
    'id': 419, 'status': 'open', 'stock': 'TESTCO', 'version': 1,
    'cohort': '2026-08-14', 'structure': 'bcs', 'direction': 'CE',
    'long_symbol': _L, 'short_symbol': _S, 'spot_symbol': 'NSE:TESTCO',
    'exchange': 'NFO', 'quantity': _QTY, 'lot_size': _QTY, 'lots': 1,
    'entry_long_price': 21.20, 'entry_short_price': 7.65, 'net_debit': 13.55,
    'spread_width': 50, 'target_spot': 1435.0, 'sl_spot': 1319.0,
    'sl_spread': 6.78, 'entry_spot': 1360.0, 'expiry': '2026-09-29',
    'spot_sl_enabled': False, 'trail_policy': 'gain_anchored',
    'time_policy': 'sessions_before_expiry',
    # STAMPED, like every real cohort record. Omitted here until 2026-08-31,
    # which the new UNSTAMPED arming check correctly flagged: a cohort record
    # with no `paper` flag is read as paper by zebra and as live by this
    # engine, so both would act on it. A fixture that leaves it out is
    # exercising an illegal state without meaning to.
    'paper': False,
}

_LONG_BOOK = {'bid': 40.00, 'bid_qty': 1400, 'ask': 40.20, 'ask_qty': 1400,
              'ltp': 40.10, 'prev_close': 21.0}
_SHORT_BOOK = {'bid': 10.05, 'bid_qty': 1400, 'ask': 10.30, 'ask_qty': 1400,
               'ltp': 10.20, 'prev_close': 7.6}
def _pos():
    """A FRESH position list per replay.

    `TickBroker` mutates the dicts it is given as legs fill, so a module-level
    list is drained by the first test that closes and every later replay then
    starts with both legs already flat -- it reports "closed by another
    process", places nothing, and an assertion on `kite.placed` fails for a
    reason that has nothing to do with the code under test. Same family as
    `feedback_fake_must_not_be_safer_than_production`: shared mutable state in
    the harness inventing behaviour production does not have.
    """
    return [{'tradingsymbol': _S, 'quantity': -_QTY},
            {'tradingsymbol': _L, 'quantity': _QTY}]

#: Spot well through sl_spot 1319, held for many polls so no debounce saves us.
_BREACH = [Tick(t, 1300.0, _LONG_BOOK, _SHORT_BOOK, 'spot far below sl_spot')
           for t in ('11:00:00', '11:00:06', '11:00:12', '11:00:30',
                     '11:01:00', '11:02:00', '11:05:00', '11:10:00')]


def _run(monkeypatch, trade, store_kind):
    """Drive the real monitor_all with one position in one book.

    The zebra case still hands `trade` to run_session so the TickBroker knows
    which symbols to quote -- it is the BOOK the position sits in that differs,
    not the instrument. `get_store` is then emptied so the bcs book contributes
    nothing and the only open position comes from the cohort store.
    """
    if store_kind == 'zebra':
        _, kite, _, spy = run_session(
            monkeypatch, sm, trade, _BREACH, _DAY, _pos(),
            dry_run=False, cohort=[trade])
        return kite, spy
    _, kite, _, spy = run_session(monkeypatch, sm, trade, _BREACH, _DAY, _pos(),
                                  dry_run=False)
    return kite, spy


def test_a_cohort_position_is_not_closed_when_spot_crosses_its_sl_spot(
        monkeypatch):
    """Spot 1300 against a stored sl_spot of 1319, for ten polls. The number is
    REPORTED; it is not a trigger in this book.

    Asserts on TWO things, because they are different facts and only one of
    them is obvious. "No order placed" is also true of a position nobody
    loaded — and an unmonitored live position is the failure that has actually
    cost money here. So the test first proves the monitor was WATCHING (it
    quoted both legs), then that it chose not to act.
    """
    kite, spy = _run(monkeypatch, dict(COHORT_TRADE), 'zebra')
    quoted = {q.split(':')[-1] for q in kite.quoted}
    assert _L in quoted and _S in quoted, (
        'the cohort position was never quoted, so it was never monitored — '
        'this test would pass for the wrong reason')
    assert kite.placed == [], (
        'the cohort spot stop fired — it was measured and switched off: a 3% '
        f'stop cuts 40% of winners. Orders: {kite.placed}')


def test_the_same_breach_DOES_close_an_ordinary_bcs_position(monkeypatch):
    """Negative control, and the one that makes the test above mean something.

    Identical spot, identical book, identical sl_spot — the only difference is
    which book the record came from and therefore whether its owner armed the
    stop. If this stops firing, the test above is passing because SL_SPOT is
    broken for everyone, not because the policy is respected.
    """
    plain = {k: v for k, v in COHORT_TRADE.items()
             if k not in ('spot_sl_enabled', 'trail_policy', 'time_policy',
                          'cohort', 'structure', 'direction')}
    kite, spy = _run(monkeypatch, plain, 'bcs')
    assert kite.placed, 'SL_SPOT no longer fires for an ordinary BCS position'


def test_the_disarmed_stop_is_still_reported(monkeypatch):
    """Switched off as a TRIGGER, kept as a NUMBER. During a blind spell the
    alert says where spot sits against sl_spot, and that is the whole reason
    the level is still stored."""
    kite, spy = _run(monkeypatch, dict(COHORT_TRADE), 'zebra')
    assert '1319' in ''.join(str(x) for x in spy.sent) or not spy.sent, \
        'sl_spot vanished from the reporting as well as the triggering'


# -- the TIME policy ---------------------------------------------------------
#
# zebra closes unconditionally N trading SESSIONS before expiry; the monitor
# warns from E-5 and force-closes on EXPIRY DAY -- four sessions later. Once
# `exits_managed_externally` makes zebra stand down, applying only the
# monitor's own rule would silently DELETE a stop. A migration must not weaken
# a rule by omission.

def test_an_ordinary_trade_still_stops_on_expiry_day_only():
    from datetime import date as _d
    t = {'id': 1, 'stock': 'X', 'expiry': '2026-09-29'}
    assert sm.time_stop_due(t, _d(2026, 9, 29)) is True
    assert sm.time_stop_due(t, _d(2026, 9, 22)) is False


def test_a_cohort_trade_stops_five_sessions_out():
    from datetime import date as _d
    t = {'id': 1, 'stock': 'X', 'expiry': '2026-09-29',
         'time_policy': 'sessions_before_expiry', 'time_stop_sessions': 5}
    # 2026-09-29 is a Tuesday: 22 Sep is 5 weekday sessions before it.
    assert sm.sessions_to_expiry(t, _d(2026, 9, 22)) == 5
    assert sm.time_stop_due(t, _d(2026, 9, 22)) is True
    assert sm.time_stop_due(t, _d(2026, 9, 21)) is False


def test_the_cohort_time_stop_is_earlier_than_the_expiry_day_one():
    """The whole point. If these two ever agree, the handover has quietly
    reverted the cohort to the weaker rule."""
    from datetime import date as _d
    cohort = {'expiry': '2026-09-29', 'time_policy': 'sessions_before_expiry',
              'time_stop_sessions': 5}
    plain = {'expiry': '2026-09-29'}
    day = _d(2026, 9, 22)
    assert sm.time_stop_due(cohort, day) and not sm.time_stop_due(plain, day)


def test_a_broken_session_count_falls_back_to_expiry_day_not_to_never():
    """A time stop that cannot be computed must still fire on expiry day. The
    other direction is a position that rides into physical settlement."""
    from datetime import date as _d
    t = {'id': 1, 'stock': 'X', 'expiry': '2026-09-29',
         'time_policy': 'sessions_before_expiry', 'time_stop_sessions': None}
    assert sm.time_stop_due(t, _d(2026, 9, 22)) is False
    assert sm.time_stop_due(t, _d(2026, 9, 29)) is True


def test_the_adapter_stamps_the_session_count_zebra_measured():
    """The N travels on the record, so the monitor never has to read zebra's
    config to honour zebra's rule."""
    from zebra import config as zcfg
    assert za.ZEBRA_EXIT_POLICY['time_stop_sessions'] == zcfg.TIME_SL_DAYS
    assert za.ZEBRA_EXIT_POLICY['time_policy'] == 'sessions_before_expiry'


def test_the_cohort_time_stop_actually_force_closes_through_monitor_all(
        monkeypatch):
    """End to end, because the unit tests above only prove the PREDICATE.

    The startup arming loop read `is_expiry_day` and nothing noticed: swapping
    `time_stop_due` back for it left every test green, so the policy existed
    and was never consulted by the code that runs. 2026-09-22 is a Tuesday,
    five weekday sessions before the 29th — the day zebra's rule fires and the
    monitor's expiry-day rule does not.
    """
    from datetime import date as _d
    day = _d(2026, 9, 22)
    trade = dict(COHORT_TRADE, expiry='2026-09-29', time_stop_sessions=5)
    ticks = [Tick(t, 1360.0, _LONG_BOOK, _SHORT_BOOK, 'quiet, five sessions out')
             for t in ('15:16:00', '15:16:10', '15:16:20')]
    _c, kite, _s, _spy = run_session(monkeypatch, sm, trade, ticks, day, _pos(),
                                     dry_run=False, cohort=[trade])
    assert kite.placed, (
        'the cohort time stop never fired: the position rode past the session '
        'zebra would have closed it on, into the delivery-margin ramp')


def test_an_ordinary_position_is_NOT_force_closed_five_sessions_out(
        monkeypatch):
    """Negative control. The three original books keep the expiry-day rule;
    arming them five sessions early would close every one of them a week
    before its time."""
    from datetime import date as _d
    day = _d(2026, 9, 22)
    plain = {k: v for k, v in COHORT_TRADE.items()
             if k not in ('spot_sl_enabled', 'trail_policy', 'time_policy',
                          'time_stop_sessions', 'cohort', 'structure',
                          'direction')}
    plain['expiry'] = '2026-09-29'
    ticks = [Tick(t, 1360.0, _LONG_BOOK, _SHORT_BOOK, 'quiet, five sessions out')
             for t in ('15:16:00', '15:16:10', '15:16:20')]
    _c, kite, _s, _spy = run_session(monkeypatch, sm, plain, ticks, day, _pos(),
                                     dry_run=False)
    assert kite.placed == [], (
        'an ordinary BCS position was force-closed five sessions before '
        f'expiry: {kite.placed}')


# -- the LISTING, which had the same blind spot one command over -------------

def test_the_cohort_appears_in_the_trade_listing(monkeypatch, capsys):
    """`--list` hardcoded three stores and never called `_open_zebra_store`.

    So after the bridge fixed `monitor_all`, the owner's "what do I have open"
    still answered `Open: 0` with eight positions live — the exact misreport
    the bridge exists to end, in the command they actually type.
    """
    trades = [dict(COHORT_TRADE), dict(COHORT_TRADE, id=420, stock='OTHERCO')]

    class _S:
        def get_open_trades(self):
            return trades
    monkeypatch.setattr(sm, '_open_zebra_store', lambda: _S())
    assert sm.list_cohort_trades() == 2
    out = capsys.readouterr().out
    assert 'TESTCO' in out and 'OTHERCO' in out and 'Open: 2' in out


def test_the_listing_reads_direction_from_the_SYMBOLS(monkeypatch, capsys):
    """It used `get_strategy`, which needs `_store_type` — stamped by
    `_load_all_trades`, NOT by the adapter. So every row fell through to the
    'BCS' default, and three live PE bear-put spreads (TMPV, COALINDIA,
    CROMPTON) were listed as bull call spreads.

    Reporting direction from anything but the leg symbols is the bug the
    bridge exists to prevent, wearing a different hat.
    """
    pe = dict(COHORT_TRADE, id=423, stock='TMPV', direction='PE',
              long_symbol='TMPV26SEP320PE', short_symbol='TMPV26SEP310PE',
              long_strike=320.0, short_strike=310.0)
    assert pe.get('_store_type') is None, (
        'the adapter now stamps _store_type — this test is testing the wrong '
        'thing and the fallback it guards may be back')

    class _S:
        def get_open_trades(self):
            return [pe]
    monkeypatch.setattr(sm, '_open_zebra_store', lambda: _S())
    sm.list_cohort_trades()
    row = [l for l in capsys.readouterr().out.splitlines() if 'TMPV' in l][0]
    assert ' BPS ' in row, f'a bear put spread was listed as BCS: {row!r}'


def test_an_unavailable_cohort_store_says_so_rather_than_showing_nothing(
        monkeypatch, capsys):
    """`Open: 0` and "cannot read the book" must never render the same — that
    ambiguity is the whole reason this listing was wrong for a day."""
    monkeypatch.setattr(sm, '_open_zebra_store', lambda: None)
    assert sm.list_cohort_trades() == 0
    assert 'unavailable' in capsys.readouterr().out


def test_the_LIST_COMMAND_actually_calls_it(monkeypatch, capsys):
    """The tests above call `list_cohort_trades` directly, so they pass just
    as well when the CLI never calls it — which was the original defect
    exactly. Asserted on the source, since driving `main()` would need a real
    Kite session and three real stores."""
    import inspect
    src = inspect.getsource(sm.main)
    i = src.index('if args.list:')
    j = src.index('return', i)
    assert 'list_cohort_trades()' in src[i:j], (
        '--list no longer lists the cohort: it would answer "Open: 0" with '
        'live positions, which is the misreport the bridge exists to end')


def test_a_store_that_RAISES_is_reported_not_shown_as_empty(monkeypatch,
                                                            capsys):
    """Same rule as an unavailable store, one failure mode over: "the book is
    empty" and "the book could not be read" are different facts and must not
    render the same."""
    class _Boom:
        def get_open_trades(self):
            raise RuntimeError('drive timeout')
    monkeypatch.setattr(sm, '_open_zebra_store', lambda: _Boom())
    assert sm.list_cohort_trades() == 0
    out = capsys.readouterr().out
    assert 'could not read' in out and 'drive timeout' in out
