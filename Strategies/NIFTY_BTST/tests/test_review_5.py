"""Regression tests for docs/NEO_REVIEW_5.md (R5-01..R5-06, R5-08, R5-10): the
reviewer's scenarios, rebuilt on the project's fakes, with the CORRECT
behaviour asserted, plus the controls that show the clean path unchanged.
Every assertion that matters reads broker truth (orders sent, `true_net`,
order status), not only alert text."""
from datetime import date, timedelta

import pytest
from conftest import LOT, T, default_premiums, sym
from live_helpers import MON, SCRIPS, TUE, neo_symbol
from nifty_btst.broker import BrokerUnavailable, OrderRequest, PositionView
from nifty_btst.journal import Journal
from nifty_btst.neo_order_probe import OrderProbe, ProbeRefused
from nifty_btst.pricing import Book
from test_live_engine import CE_MON, CE_NEO, PE_MON, holding, marked, state
from test_probe_safety import KITE_ROW, NEO, ProbeRig
from test_review_3 import buys, sells

S2 = sym('2026-11-03', 22850, 'CE')             # a strike that lives past Tuesday
N2 = neo_symbol(S2)
#: the design rule, stated here rather than imported: a refusal never holds
#: a send back for more than a minute (R5-03)
REFUSAL_BACKOFF_MAX_SEC = 60.0
ROW2 = {'tradingsymbol': S2, 'expiry': '2026-11-03', 'strike': 22850.0,
        'instrument_type': 'CE', 'lot_size': LOT}


def app_order(rig, n, side, price='69.00', qty=LOT):
    """The owner trades in the app: an order with no tag of ours, filled now."""
    no = rig.broker.place(OrderRequest(symbol=n, token='t', side=side, qty=qty, price=price, tag=''))
    rig.broker.sent.pop()
    for o in rig.broker.orders:
        if o['nOrdNo'] == no:
            o.update({'ordSt': 'complete', 'fldQty': str(qty), 'avgPrc': price,
                      '_filled_at': rig.clock['now'].isoformat()})
    return no


def intent_times(rig, kind):
    return [ln['t'] for ln in rig.journal.lines() if ln.get('ev') == 'INTENT' and ln['kind'] == kind]


def gaps(times):
    from datetime import datetime
    ts = [datetime.fromisoformat(t) for t in times]
    return [int((b - a).total_seconds()) for a, b in zip(ts, ts[1:])]


def reconcile(rig, when):
    rig.clock['now'] = when
    rig.engine._reconcile(when, startup=False)


# ====================================================================== R5-04
def test_r5_04_reconcile_reads_the_order_book_once(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    real, calls = rig.broker.order_book, []

    def counted():
        calls.append(1)
        return real()
    rig.broker.order_book = counted
    reconcile(rig, T(MON, '11:00:00'))
    assert len(calls) == 1 and rig.engine.reconcile_ok


def test_r5_04_a_later_read_failure_at_start_never_crashes(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.restart()
    rig.broker.read_faults = [None, None, 'unavailable']   # positions, order book, then a third read
    rig.start(T(MON, '11:00:00'))                          # used to escape: LIVE RUN CRASHED
    assert rig.engine.started and rig.engine.reconcile_ok
    rig.market.set(CE_MON, 150.0, bid=149.8, ask=150.2)
    rig.go(T(MON, '11:00:01'))                             # the loop runs on
    assert state(rig)['status'] == 'open'


def test_r5_04_any_broker_error_inside_reconcile_is_guarded(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    reconcile(rig, T(MON, '11:00:00'))
    assert rig.engine.reconcile_ok                         # control: clean before

    def unreadable(sym_):
        raise BrokerUnavailable('a read after the first two failed')
    rig.engine._looks_like_ours = unreadable               # stands in for any later read
    rig.service(T(MON, '11:05:00'))                        # the 300 s reconcile, inside the tick
    assert not rig.engine.reconcile_ok
    assert any('RECONCILE could not read the broker' in s for s in rig.sent)


# ====================================================================== R5-03
def test_r5_03_ten_refusals_do_not_trip_the_kill(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.place_faults = [('reject', '429', 'slow down')] * 12
    rig.market.set(CE_MON, 153.5, bid=153.3, ask=153.8)
    t0 = T(MON, '10:00:00')
    for i in range(0, 520):
        rig.go(t0 + timedelta(seconds=i))
    k, p = rig.store.kill(), state(rig)
    assert not k['tripped'] and not any('ceiling' in s for s in rig.sent)
    assert len(buys(rig)) == 13 and p['status'] == 'open'          # 12 refused, then bought
    assert rig.journal.sends_today(t0.date(), 'ENTRY') == 1          # refusals are not sends
    assert rig.journal.sends_today(t0.date(), 'ENTRY', include_refused=True) == 13
    assert gaps(intent_times(rig, 'ENTRY')) == [3, 6, 12, 24, 48] + [60] * 7


def test_r5_03_exit_refusals_back_off_to_a_minute_and_never_trip_the_ceiling(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.place_faults = [('reject', '100008', 'unauthorized')] * 5000
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    t0 = T(MON, '11:00:00')
    for i in range(0, 3600, 3):                                      # one hour of refusals
        rig.go(t0 + timedelta(seconds=i))
    g = gaps(intent_times(rig, 'EXIT'))
    assert g[:5] == [3, 6, 12, 24, 48] and max(g) <= REFUSAL_BACKOFF_MAX_SEC
    assert len(sells(rig)) <= 70                                     # was 1,200
    assert not rig.store.kill()['tripped'] and rig.journal.sends_today(t0.date()) == 0
    assert state(rig)['status'] == 'open' and state(rig)['live']['exit_attempts'] == 0
    rig.broker.place_faults = []                                     # the IP is fixed
    relent = t0 + timedelta(seconds=3600)
    for i in range(0, 64, 3):
        rig.go(relent + timedelta(seconds=i))
        if state(rig)['status'] == 'closed':
            break
    assert state(rig)['status'] == 'closed' and rig.broker.true_net(CE_NEO) == 0
    assert (rig.clock['now'] - relent).total_seconds() <= REFUSAL_BACKOFF_MAX_SEC + 3


def test_r5_03_the_backoff_resets_after_a_send_that_is_not_refused(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    refuse = ('reject', '429', 'slow down')
    rig.broker.place_faults = [refuse, refuse, None, refuse]
    rig.broker.hold_fills = True                                     # the placed BUY rests, times out
    rig.market.set(CE_MON, 153.5, bid=153.3, ask=153.8)
    t0 = T(MON, '10:00:00')
    for i in range(0, 10):
        rig.go(t0 + timedelta(seconds=i))
    lv = state(rig)['live']
    assert len(buys(rig)) == 3 and not lv.get('refused_at')           # placed at 9 s: cleared
    for i in range(10, 40):
        rig.go(t0 + timedelta(seconds=i))
    g = gaps(intent_times(rig, 'ENTRY'))
    assert g[:2] == [3, 6]
    assert g[3] == 3                                                 # after the reset: 3 s, not 12 s


def test_r5_03_a_stored_backoff_never_holds_an_exit_beyond_a_minute(cfg, tmp_path, market):
    """Control for the clamp (passes on the old code too: it had no wait)."""
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    now = T(MON, '11:00:00')
    state(rig)['live'].update({'refused_at': (now - timedelta(seconds=61)).isoformat(),
                               'refusal_wait_sec': 1e9, 'refusal_step': 40})
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(now)
    assert len(sells(rig)) == 1
    rig2 = marked(cfg, tmp_path / 'b', market)
    holding(rig2)
    state(rig2)['live'].update({'refused_at': (now + timedelta(days=3)).isoformat(),
                                'refusal_wait_sec': 30})              # a stamp from the future
    rig2.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig2.go(now)
    assert len(sells(rig2)) == 1


def test_r5_03_refused_intents_are_not_counted_by_the_ceilings(tmp_path):
    j = Journal(tmp_path)
    t = T(MON, '10:00:00')
    j.intent(t, '20261026-1-ENTRY-1', 'ENTRY', 1, 'S', 'B', 65, '10.00')
    j.not_placed(t, '20261026-1-ENTRY-1', '429 slow down', refused=True)
    j.intent(t, '20261026-1-ENTRY-2', 'ENTRY', 1, 'S', 'B', 65, '10.00')
    j.not_placed(t, '20261026-1-ENTRY-2', '1009 rejected')             # a real rejection counts
    assert j.sends_today(t.date()) == 1 and j.sends_today(t.date(), include_refused=True) == 2
    assert '20261026-1-ENTRY-1' in j.not_placed_tags()                 # a late arrival still kills


# ====================================================================== R5-08
def test_r5_08_a_refused_send_never_reuses_its_tag(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.place_faults = [('reject', '429', 'slow down'), 'timeout_lost']
    rig.market.set(CE_MON, 153.5, bid=153.3, ask=153.8)
    t0 = T(MON, '10:00:00')
    for i in range(0, 4):
        rig.go(t0 + timedelta(seconds=i))
    tags = [r.tag for r in buys(rig)]
    assert len(tags) == 2 and len(set(tags)) == 2 and tags[0] == '20261026-1-ENTRY-1'
    assert [u['tag'] for u in rig.journal.unresolved()] == [tags[1]]   # the unclear one stays open
    assert state(rig)['live']['entry_attempts'] == 1                  # the refusal is still refunded


# ====================================================================== R5-06
def _resting_sell(cfg, tmp_path, market, slow):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    if slow:
        state(rig)['live']['exit_attempts'] = rig.neo.exit_chase_fast_max
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))                                       # stop: our SELL rests
    assert len(sells(rig)) == 1 and rig.broker.orders[0]['ordSt'] == 'open'
    return rig


@pytest.mark.parametrize('slow', [True, False], ids=['slow_mode', 'fast_mode'])
def test_r5_06_a_resting_sell_is_pulled_when_a_foreign_sell_fills(cfg, tmp_path, market, slow):
    rig = _resting_sell(cfg, tmp_path, market, slow)
    rig.clock['now'] = T(MON, '11:00:01')
    app_order(rig, CE_NEO, 'S')                                      # the owner sells in the app
    rig.service(T(MON, '11:00:02'))
    assert rig.broker.orders[0]['ordSt'] == 'cancelled'               # pulled at once, not at its timer
    assert any('CANCELLING OURS NOW' in s for s in rig.sent)
    rig.broker.hold_fills = False
    for i in range(3, 180):
        rig.go(T(MON, '11:00:00') + timedelta(seconds=i))
    assert len(sells(rig)) == 1 and rig.broker.true_net(CE_NEO) == 0  # never short


def test_r5_06_control_no_foreign_sale_no_early_pull(cfg, tmp_path, market):
    rig = _resting_sell(cfg, tmp_path, market, slow=True)
    for i in range(1, 31):
        rig.service(T(MON, '11:00:00') + timedelta(seconds=i))
    assert rig.broker.orders[0]['ordSt'] == 'open'                    # still working its 60 s
    assert not any('CANCELLING OURS NOW' in s for s in rig.sent)


def test_r5_06_the_slow_mode_alert_says_to_pull_our_sell_first(cfg, tmp_path, market):
    rig = _resting_sell(cfg, tmp_path, market, slow=True)
    for i in range(1, 66):                                           # its 60 s end; the next send
        rig.go(T(MON, '11:00:00') + timedelta(seconds=i))
    assert len(sells(rig)) == 2
    assert any('Consider exiting in the app' in s and 'CANCEL OUR WORKING SELL' in s
               for s in rig.sent)


# ====================================================================== R5-05
def _quarantined_exit(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    rig.broker.orders[0].update({'ordSt': 'complete', 'fldQty': '1', 'avgPrc': '69.80',
                                 '_filled_at': T(MON, '11:00:00').isoformat()})
    # the row still shows the lot: the "1" was a unit misreport, the SELL never filled
    rig.broker._net = lambda lagged=True: {CE_NEO: {'fl': 0, 'cf': LOT}}
    rig.go(T(MON, '11:00:01'))
    assert state(rig)['live']['leg'].get('quarantined')
    return rig


def test_r5_05_not_filled_on_a_quarantined_exit_is_refused_and_changes_nothing(cfg, tmp_path, market):
    rig = _quarantined_exit(cfg, tmp_path, market)
    with pytest.raises(ValueError, match='live-confirm-held 1 --yes'):
        rig.engine.book_external(1, T(MON, '11:01:00'), None)
    assert state(rig)['live']['leg'].get('quarantined') and state(rig)['status'] == 'open'
    for i in range(1, 30):
        rig.go(T(MON, '11:01:00') + timedelta(seconds=i))
    assert len(sells(rig)) == 1                                      # no second SELL went out


def test_r5_05_the_exit_quarantine_alert_names_its_own_commands(cfg, tmp_path, market):
    rig = _quarantined_exit(cfg, tmp_path, market)
    q = [s for s in rig.sent if 'QUARANTINED' in s and 'STOP HIT' not in s]
    assert q and 'live-confirm-held 1 --yes' in q[-1] and '--not-filled' not in q[-1]


def _sell_reads_unfilled(rig):
    """The broker corrects itself: the quarantined SELL reads cancelled, no fill."""
    rig.broker.orders[0].update({'ordSt': 'cancelled', 'fldQty': '0', 'avgPrc': '0.00'})
    rig.broker.orders[0].pop('_filled_at', None)


def test_r5_05_confirm_held_is_refused_while_the_quarantined_sell_shows_a_fill(cfg, tmp_path,
                                                                                market):
    """CORRECTED by R6-01 (this test used to expect a second SELL): the row
    still shows the lot, as inside the positions lag, but the broker reported
    the SELL filled. The owner's word cannot override that: refused, nothing
    changed, no second SELL."""
    rig = _quarantined_exit(cfg, tmp_path, market)
    with pytest.raises(ValueError, match='FILLED'):
        rig.engine.confirm_held(1, T(MON, '11:02:00'))
    assert state(rig)['live']['leg'].get('quarantined')
    assert not rig.store.owner_confirmed_held(1, T(MON, '11:02:00').date())
    for i in range(1, 70):
        rig.go(T(MON, '11:02:00') + timedelta(seconds=i))
    assert len(sells(rig)) == 1


def test_r5_05_confirm_held_clears_once_the_sell_reads_unfilled(cfg, tmp_path, market):
    rig = _quarantined_exit(cfg, tmp_path, market)
    _sell_reads_unfilled(rig)
    rig.engine.confirm_held(1, T(MON, '11:02:00'))
    lv = state(rig)['live']
    assert lv['leg'] is None and lv['leg_history'][-1].get('cleared_by_owner')
    rig.go(T(MON, '11:02:01'))
    assert len(sells(rig)) == 2                                      # the exit resumes, guarded


def test_r5_05_confirm_held_by_request_file_reads_the_sell_first(cfg, tmp_path, market):
    import json
    from test_review_3 import stamped
    rig = _quarantined_exit(cfg, tmp_path, market)
    (rig.dir / 'CONFIRM_HELD_1').write_text(stamped(1, CE_MON, MON, action='confirm_held'))
    rig.service(T(MON, '11:02:00'))
    res = json.loads((rig.dir / 'CONFIRM_HELD_1.result').read_text())
    assert not res['applied'] and 'FILLED' in res['text']
    assert state(rig)['live']['leg'].get('quarantined') and len(sells(rig)) == 1


@pytest.mark.parametrize('row', ['row_lagging', 'row_zero', 'no_row'])
def test_r5_05_wrong_branch_confirm_held_after_a_real_fill_sends_no_second_sell(cfg, tmp_path,
                                                                                  market, row):
    """The SELL really FILLED (the "1" was lots) and the owner picks the wrong
    branch: refused on the broker's own report of the fill, whatever the row
    shows (still the lot inside its lag, 0, or no row), so never short."""
    rig = _quarantined_exit(cfg, tmp_path, market)
    if row == 'row_zero':
        rig.broker._net = lambda lagged=True: {CE_NEO: {'fl': -LOT, 'cf': LOT}}
    elif row == 'no_row':
        rig.broker._net = lambda lagged=True: {}
    with pytest.raises(ValueError, match='FILLED'):
        rig.engine.confirm_held(1, T(MON, '11:02:00'))
    for i in range(1, 70):
        rig.go(T(MON, '11:02:00') + timedelta(seconds=i))
    assert len(sells(rig)) == 1 and state(rig)['status'] == 'open'


def test_r5_05_control_with_a_price_the_quarantine_clears_and_books(cfg, tmp_path, market):
    rig = _quarantined_exit(cfg, tmp_path, market)
    rig.engine.book_external(1, T(MON, '11:05:00'), 68.0)
    p = state(rig)
    assert p['status'] == 'closed' and p['exit_reason'] == 'closed_externally'


def test_r5_05_not_filled_on_a_held_lot_never_cancels_a_working_sell(cfg, tmp_path, market):
    rig = _resting_sell(cfg, tmp_path, market, slow=True)
    with pytest.raises(ValueError, match='sale price is required'):
        rig.engine.book_external(1, T(MON, '11:00:01'), None)
    assert rig.broker.orders[0]['ordSt'] == 'open' and not state(rig)['live'].get('owner_pending')


# ====================================================================== R5-02
def _lot_one_sold_by_owner(cfg, tmp_path, market, how):
    """Lot 1 carried Monday -> Tuesday; the owner sells it in the app at
    09:18 and the record closes, by reconcile's adoption or his command.
    Entries are disarmed on Tuesday so only the scripted lot 2 trades."""
    rig = marked(cfg, tmp_path, market)
    holding(rig, symbol=S2, expiry='2026-11-03')
    default_premiums(market, '2026-11-03')
    rig.market.set(S2, 170.0, bid=169.8, ask=170.2)
    rig.restart(armed=False)
    rig.start(T(TUE, '09:00:00'))
    rig.go(T(TUE, '09:15:05'))
    rig.clock['now'] = T(TUE, '09:18:00')
    x = app_order(rig, N2, 'S', price='170.00')
    if how == 'book_external':
        rig.engine.book_external(1, T(TUE, '09:18:30'), 170.0)
    else:
        reconcile(rig, T(TUE, '09:19:00'))
        assert state(rig)['live']['closed_by_orders'] == [x]
    assert state(rig)['status'] == 'closed' and state(rig)['exit_reason'] == 'closed_externally'
    return rig, x


def _lot_two(rig, when, lag=0.0):
    """The engine's own BUY of the same strike, filled at `when`."""
    rig.clock['now'] = when
    rig.broker.positions_lag_sec = lag
    tag = '20261027-2-ENTRY-1'
    rig.journal.intent(when, tag, 'ENTRY', 2, N2, 'B', LOT, '100.00')
    no = rig.broker.place(OrderRequest(symbol=N2, token='t', side='B', qty=LOT, price='100.00', tag=tag))
    rig.broker.sent.pop()
    rig.journal.placed(when, tag, no)
    for o in rig.broker.orders:
        if o['nOrdNo'] == no:
            o.update({'ordSt': 'complete', 'fldQty': str(LOT), 'avgPrc': '100.00',
                      '_filled_at': when.isoformat()})
    p = holding(rig, symbol=S2, expiry='2026-11-03', entry_price=100.0, entry_date=TUE,
                at_broker=False)
    p['entry_time'] = when.isoformat()
    p['live']['entry_order_no'] = no
    rig.store.save()
    return p


@pytest.mark.parametrize('how', ['adopted', 'book_external'])
def test_r5_02_owner_sale_of_lot_one_does_not_block_lot_two(cfg, tmp_path, market, how):
    rig, _ = _lot_one_sold_by_owner(cfg, tmp_path, market, how)
    _lot_two(rig, T(TUE, '11:00:00'))
    rig.market.set(S2, 45.0, bid=44.8, ask=45.2)                     # lot 2's stop (50)
    rig.go(T(TUE, '11:30:00'))
    rig.go(T(TUE, '11:30:01'))
    assert len(sells(rig)) == 1 and state(rig, 2)['status'] == 'closed'
    assert state(rig, 2)['exit_reason'] == 'stop' and rig.broker.true_net(N2) == 0
    reconcile(rig, T(TUE, '11:40:00'))
    assert not rig.store.kill()['tripped']


def test_r5_02_control_a_second_owner_sale_still_blocks_and_names_the_lot(cfg, tmp_path, market):
    """The allowance covers lot 1, never lot 2: a second owner sale blocks
    our SELL (that block held on the old code too; this test fails there on
    the alert naming the lot), and reconcile books lot 2 at HIS second
    sale's price, not an average with lot 1's sale."""
    rig, x = _lot_one_sold_by_owner(cfg, tmp_path, market, 'book_external')
    p2 = _lot_two(rig, T(TUE, '11:00:00'))
    rig.clock['now'] = T(TUE, '11:20:00')
    y = app_order(rig, N2, 'S', price='60.00')                       # he sells lot 2 in the app too
    ok, why = rig.engine._sell_allowed(p2, LOT, T(TUE, '11:20:01'))
    assert not ok and y in why and 'THIS lot (#2)' in why
    rig.market.set(S2, 45.0, bid=44.8, ask=45.2)
    for i in range(0, 90):
        rig.go(T(TUE, '11:30:00') + timedelta(seconds=i))
    assert not sells(rig) and rig.broker.true_net(N2) == 0            # no SELL, never short
    p2 = state(rig, 2)
    assert p2['exit_reason'] == 'closed_externally' and p2['live']['closed_by_orders'] == [y]
    assert p2['exit_price'] == 60.0 and not rig.store.kill()['tripped']


@pytest.mark.parametrize('how', ['adopted', 'book_external'])
def test_r5_02_inside_the_lag_lot_two_is_not_booked_as_lot_ones_sale(cfg, tmp_path, market, how):
    rig, _ = _lot_one_sold_by_owner(cfg, tmp_path, market, how)
    _lot_two(rig, T(TUE, '11:00:00'), lag=60.0)
    for t in ('11:00:20', '11:00:40'):                               # the row still reads 0
        reconcile(rig, T(TUE, t))
    p = state(rig, 2)
    assert p['status'] == 'open' and p.get('exit_reason') is None
    assert not rig.store.kill()['tripped']
    reconcile(rig, T(TUE, '11:01:30'))                               # after the lag the row agrees
    assert rig.engine.reconcile_ok and state(rig, 2)['status'] == 'open'


def test_r5_02_owner_round_trip_on_the_held_symbol_does_not_block_the_stop(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.clock['now'] = T(MON, '10:30:00')
    app_order(rig, CE_NEO, 'B', price='150.00')                      # he buys one in the app ...
    rig.clock['now'] = T(MON, '10:31:00')
    app_order(rig, CE_NEO, 'S', price='151.00')                      # ... and sells one
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    rig.go(T(MON, '11:00:01'))
    assert len(sells(rig)) == 1 and state(rig)['status'] == 'closed'
    assert rig.broker.true_net(CE_NEO) == 0
    for t in ('11:06:00', '11:12:00'):
        reconcile(rig, T(MON, t))
    assert not rig.store.kill()['tripped']


def test_r5_02_control_an_mis_buy_does_not_excuse_a_sale_of_our_lot(cfg, tmp_path, market):
    """Passes on the old code too: only a BUY in this book's product nets a sale."""
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.clock['now'] = T(MON, '10:30:00')
    no = app_order(rig, CE_NEO, 'B', price='150.00')
    for o in rig.broker.orders:
        if o['nOrdNo'] == no:
            o['prod'] = 'MIS'                                        # an intraday buy, not our NRML
    rig.clock['now'] = T(MON, '10:31:00')
    app_order(rig, CE_NEO, 'S', price='151.00')
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    for i in range(0, 5):
        rig.go(T(MON, '11:00:00') + timedelta(seconds=i))
    assert not sells(rig) and state(rig)['status'] == 'open'


# ====================================================================== R5-01
def _probe(rig, step, row=ROW2):
    rig.broker.fill_delay = 1.0                     # the probe's rest-and-cancel needs a resting order
    out = []

    def sleep(s):
        rig.clock['now'] += timedelta(seconds=s)
    quote = Book(4.9, 4.8, 5.0, 1000, 1000, '', ltt=rig.clock['now'].isoformat())
    probe = OrderProbe(rig.broker, rig.journal, lambda: rig.clock['now'], sleep, out.append)
    rc = probe.run(rig.engine.scrips(), row, lambda: quote, 500, step=step)
    rig.broker.fill_delay = 0.0
    return rc, out


def test_r5_01_a_clean_probe_sale_is_not_a_short(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.clock['now'] = T(MON, '14:00:00')
    assert _probe(rig, 'hold')[0] == 0                               # Stage 3: hold Monday ...
    rig.restart()
    rig.start(T(TUE, '09:00:00'))
    rig.clock['now'] = T(TUE, '09:30:00')
    assert _probe(rig, 'sell')[0] == 0 and rig.broker.true_net(N2) == 0   # ... sell Tuesday, flat
    for t in ('09:40:00', '09:46:00'):                               # then live-run the same day
        reconcile(rig, T(TUE, t))
    assert not rig.store.kill()['tripped'] and rig.engine.reconcile_ok


def test_r5_01_control_an_unexplained_foreign_sale_is_still_a_short(cfg, tmp_path, market):
    """Passes on the old code too: only the PROBE's orders are left out."""
    rig = marked(cfg, tmp_path, market)
    rig.broker.set_external_position(N2, LOT, day=date(2026, 10, 23))
    rig.clock['now'] = T(MON, '10:00:00')
    app_order(rig, N2, 'S', price='170.00')
    reconcile(rig, T(MON, '10:00:00'))
    k = rig.store.kill()
    assert k['tripped'] and k['loud'] and 'SHORT' in k['reason']


def test_r5_01_a_repaired_short_clears_with_live_resume(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    rig.broker.hold_fills = False
    rig.clock['now'] = T(MON, '11:00:00')
    app_order(rig, CE_NEO, 'S')                                      # both fill: SHORT 65
    rig.service(T(MON, '11:00:01'))
    assert state(rig)['status'] == 'closed' and rig.broker.true_net(CE_NEO) == -LOT
    reconcile(rig, T(MON, '11:06:00'))
    assert rig.store.kill()['tripped'] and 'SHORT' in rig.store.kill()['reason']   # real: loud
    rig.clock['now'] = T(MON, '11:10:00')
    app_order(rig, CE_NEO, 'B', price='70.00')                       # the owner buys it back: flat
    assert rig.broker.true_net(CE_NEO) == 0
    rig.engine.resume(T(MON, '11:11:00'))
    for t in ('11:12:00', '11:18:00'):
        reconcile(rig, T(MON, t))
    assert not rig.store.kill()['tripped'] and rig.engine.reconcile_ok


def test_r5_01_an_owner_round_trip_on_an_untracked_symbol_is_not_a_short(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    pe = neo_symbol(PE_MON)
    rig.clock['now'] = T(MON, '10:00:00')
    app_order(rig, pe, 'B', price='100.00')
    app_order(rig, pe, 'S', price='101.00')
    row = {'trdSym': pe, 'prod': 'NRML', 'flBuyQty': str(LOT), 'flSellQty': str(LOT),
           'cfBuyQty': '0', 'cfSellQty': '0'}
    rig.broker.positions = lambda: [PositionView.from_neo(row)]       # a zero-net row for the day
    for t in ('10:05:00', '10:11:00'):
        reconcile(rig, T(MON, t))
    assert not rig.store.kill()['tripped']
    rig.clock['now'] = T(MON, '10:20:00')
    app_order(rig, pe, 'S', price='99.00')                           # control: a real naked sale
    row['flSellQty'] = str(2 * LOT)
    reconcile(rig, T(MON, '10:25:00'))
    k = rig.store.kill()
    assert k['tripped'] and k['loud'] and 'SHORT' in k['reason']


# ====================================================================== R5-10
def _carried_probe_lot(tmp_path):
    r = ProbeRig(tmp_path, fill_delay=0.0)
    j = Journal(tmp_path)
    r.fb.set_external_position(NEO, LOT, day=date(2026, 10, 23))      # the row shows the lot
    j.probe_held(T('2026-10-23', '10:00:00'), NEO, LOT)
    return r, j


def test_r5_10_a_rerun_after_an_unclear_sell_sends_no_second_sell(tmp_path):
    r, j = _carried_probe_lot(tmp_path)
    r.fb.hold_fills = True
    r.fb.visibility_lag_sec = 100.0                                  # not visible for > 30 s
    r.fb.place_faults = ['timeout_reached']
    with pytest.raises(ProbeRefused):
        r.run(step='sell')
    assert len([q for q in r.fb.sent if q.side == 'S']) == 1
    out = []
    r2 = OrderProbe(r.fb, j, lambda: r.clock['now'], r.sleep, out.append)
    assert r2.run(SCRIPS, KITE_ROW, r.quote, 500, step='sell') == 1   # the owner re-runs anyway
    assert len([q for q in r.fb.sent if q.side == 'S']) == 1         # no second SELL
    assert any('UNRESOLVED' in s for s in out) and any('RUN STOPPED' in s for s in out)


def test_r5_10_all_refuses_before_buying_with_an_unresolved_sell(tmp_path):
    r, j = _carried_probe_lot(tmp_path)
    j.intent(r.clock['now'], '20261026-0-EXIT-9', 'EXIT', 0, NEO, 'S', LOT, '4.80', probe=True)
    with pytest.raises(ProbeRefused, match='UNRESOLVED'):
        r.run(step='all')
    assert not r.fb.sent


def test_r5_10_control_yesterdays_unresolved_sell_does_not_strand_a_row_backed_sale(tmp_path):
    r, j = _carried_probe_lot(tmp_path)
    j.intent(T('2026-10-23', '15:00:00'), '20261023-0-EXIT-1', 'EXIT', 0, NEO, 'S', LOT, '4.80',
             probe=True)                                             # never resolved, yesterday
    assert r.run(step='sell') == 0 and r.fb.true_net(NEO) == 0       # the row vouches: sold, flat
    r2, j2 = _carried_probe_lot(tmp_path / 'b')
    r2.fb.carry_forward_rows = False                                 # no row: the flags path
    j2.intent(T('2026-10-23', '15:00:00'), '20261023-0-EXIT-1', 'EXIT', 0, NEO, 'S', LOT, '4.80',
              probe=True)
    r2.probe.allow_missing_row = r2.probe.owner_confirms_held = True
    assert r2.run(step='sell') == 1 and not [q for q in r2.fb.sent if q.side == 'S']
