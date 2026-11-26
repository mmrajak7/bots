"""Regression tests for docs/NEO_REVIEW_6.md (R6-01..R6-05, R6-07, R6-09):
the reviewer's scratch reproductions (scratchpad r6/copy/tests/test_r6.py)
turned round to assert the CORRECT behaviour, plus tests that kill the
mutation survivors M3, M5 and M16. Broker truth (`true_net`, orders sent,
order status) decides, not alert text alone."""
from datetime import timedelta

import pytest
from conftest import LOT, T
from live_helpers import MON, TUE
from nifty_btst.broker import OrderRequest
from test_live_engine import CE_MON, CE_NEO, holding, marked, state
from test_review_3 import sells
from test_review_5 import N2, S2, app_order, reconcile


def _fld_in_lots(rig):
    """NEO reports fldQty in LOTS on the order/trade book (measurement 3);
    the fake keeps the true share count inside, so `true_net` stays honest."""
    orig = rig.broker._public

    def pub(o):
        d = orig(o)
        f = int(d.get('fldQty') or 0)
        if f:
            d['fldQty'] = str(f // LOT)
        return d
    rig.broker._public = pub


def _own_buy(rig, n, when, pid, tag, sym_, expiry, entry_date=MON):
    """This book's own BUY of a lot, filled at `when`, and its record."""
    rig.clock['now'] = when
    if tag:
        rig.journal.intent(when, tag, 'ENTRY', pid, n, 'B', LOT, '100.00')
    no = rig.broker.place(OrderRequest(symbol=n, token='t', side='B', qty=LOT, price='100.00',
                                       tag=tag))
    rig.broker.sent.pop()
    if tag:
        rig.journal.placed(when, tag, no)
    for o in rig.broker.orders:
        if o['nOrdNo'] == no:
            o.update({'ordSt': 'complete', 'fldQty': str(LOT), 'avgPrc': '100.00',
                      '_filled_at': when.isoformat()})
    p = holding(rig, symbol=sym_, expiry=expiry, entry_price=100.0, entry_date=entry_date,
                at_broker=False)
    p['entry_time'] = when.isoformat()
    p['live']['entry_order_no'] = no
    rig.store.save()
    return p, no


# ====================================================================== R6-01
def _quarantined_in_lots(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.positions_lag_sec = 120.0                 # the row still shows the lot
    _fld_in_lots(rig)
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))                           # stop: SELL 1 sent, fills (65 inside)
    rig.go(T(MON, '11:00:01'))                           # reads complete, fldQty 1 -> quarantine
    assert state(rig)['live']['leg'].get('quarantined')
    return rig


def test_r6_01_confirm_held_on_a_quarantined_exit_is_refused_and_no_second_sell(cfg, tmp_path,
                                                                                 market):
    rig = _quarantined_in_lots(cfg, tmp_path, market)
    with pytest.raises(ValueError, match='FILLED'):
        rig.engine.confirm_held(1, T(MON, '11:00:30'))   # the owner: "did not fill, held"
    assert state(rig)['live']['leg'].get('quarantined')
    for i in range(31, 100):
        rig.go(T(MON, '11:00:00') + timedelta(seconds=i))
    assert len(sells(rig)) == 1 and rig.broker.true_net(CE_NEO) == 0     # never short


def test_r6_01_a_partial_fill_on_a_cancelled_sell_also_refuses(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    rig.broker.orders[0].update({'ordSt': 'cancelled', 'fldQty': '13', 'avgPrc': '69.80',
                                 '_filled_at': T(MON, '11:00:00').isoformat()})
    rig.go(T(MON, '11:00:01'))
    assert state(rig)['live']['leg'].get('quarantined')
    with pytest.raises(ValueError, match='partial fill counts'):
        rig.engine.confirm_held(1, T(MON, '11:00:30'))
    assert state(rig)['live']['leg'].get('quarantined')


def test_r6_01_the_guard_refuses_while_a_cleared_sell_of_the_record_shows_a_fill(cfg, tmp_path,
                                                                                  market):
    """Defence in depth: even if a quarantine is cleared some other way (here
    by hand, as the R5 code did), the guard refuses on that SELL's fill."""
    rig = _quarantined_in_lots(cfg, tmp_path, market)
    lv = state(rig)['live']
    lv.setdefault('leg_history', []).append(dict(lv['leg'], cleared_by_owner='x'))
    lv['leg'] = None
    lv['state'] = 'EXIT_DUE'
    for i in range(31, 100):
        rig.go(T(MON, '11:00:00') + timedelta(seconds=i))
    assert len(sells(rig)) == 1 and rig.broker.true_net(CE_NEO) == 0
    assert any('quarantined and cleared' in s for s in rig.sent)


# ====================================================================== R6-02
def _lot_one_booked_with_no_sale_today(cfg, tmp_path, market):
    """Lot 1 on S2 booked closed by `live-book-external --price` with NO sale
    of it in today's book (sold on an earlier day). Then lot 2, same strike."""
    rig = marked(cfg, tmp_path, market)
    holding(rig, symbol=S2, expiry='2026-11-03', at_broker=False)
    rig.engine.book_external(1, T(MON, '10:30:00'), 150.0)
    assert state(rig)['exit_reason'] == 'closed_externally'
    assert state(rig)['live'].get('sale_unmatched')            # no allowance at all
    p2, _ = _own_buy(rig, N2, T(MON, '11:00:00'), 2, '20261026-2-ENTRY-1', S2, '2026-11-03')
    return rig, p2


def test_r6_02_no_phantom_allowance_the_owner_sale_pulls_our_resting_sell(cfg, tmp_path, market):
    rig, p2 = _lot_one_booked_with_no_sale_today(cfg, tmp_path, market)
    p2['live']['exit_attempts'] = rig.neo.exit_chase_fast_max  # slow mode: ours works 60 s
    rig.store.save()
    rig.broker.hold_fills = True
    rig.market.set(S2, 45.0, bid=44.8, ask=45.2)               # lot 2's stop
    rig.go(T(MON, '11:30:00'))
    assert len(sells(rig)) == 1
    rig.clock['now'] = T(MON, '11:30:05')
    app_order(rig, N2, 'S', price='45.00')                     # the owner sells lot 2 in the app
    rig.service(T(MON, '11:30:06'))
    ours = [o for o in rig.broker.orders if o.get('GuiOrdId', '').startswith('20261026-2-EXIT')]
    assert ours[0]['ordSt'] == 'cancelled'                      # pulled
    rig.broker.hold_fills = False
    for i in range(7, 120):
        rig.go(T(MON, '11:30:00') + timedelta(seconds=i))
    assert len(sells(rig)) == 1 and rig.broker.true_net(N2) == 0    # never short


def test_r6_02_no_phantom_allowance_the_guard_refuses_on_a_lagging_row(cfg, tmp_path, market):
    rig, p2 = _lot_one_booked_with_no_sale_today(cfg, tmp_path, market)
    rig.clock['now'] = T(MON, '11:29:50')
    app_order(rig, N2, 'S', price='46.00')
    rig.broker.positions_lag_sec = 120.0                       # the row has not caught up
    ok, why = rig.engine._sell_allowed(p2, LOT, T(MON, '11:30:00'))
    assert not ok and 'did not send FILLED' in why
    rig.market.set(S2, 45.0, bid=44.8, ask=45.2)
    rig.go(T(MON, '11:30:00'))
    rig.go(T(MON, '11:30:01'))
    assert not sells(rig) and rig.broker.true_net(N2) == 0


def test_r6_02_our_own_quarantined_sell_booked_by_price_is_never_an_allowance(cfg, tmp_path,
                                                                               market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    _fld_in_lots(rig)
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    rig.go(T(MON, '11:00:01'))
    q = state(rig)['live']['leg']['order_no']
    rig.engine.book_external(1, T(MON, '11:02:00'), 69.8)      # the documented way out
    assert state(rig)['status'] == 'closed' and state(rig)['live']['closed_by_orders'] == [q]
    rig.clock['now'] = T(MON, '11:10:00')
    app_order(rig, CE_NEO, 'S', price='60.00')                 # any later foreign sale
    _, unexplained = rig.engine._foreign_sells(CE_NEO, T(MON, '11:10:01'), rig.broker.order_book())
    assert unexplained > 0               # not excused (1: the patched book reports lots)


def test_r6_02_a_sale_that_shows_up_after_the_booking_is_tied_at_reconcile(cfg, tmp_path, market):
    """Control: the owner sold before he booked, but the book showed it late."""
    rig = marked(cfg, tmp_path, market)
    holding(rig, symbol=S2, expiry='2026-11-03')
    rig.clock['now'] = T(MON, '10:29:00')
    rig.broker.visibility_lag_sec = 120.0
    x = app_order(rig, N2, 'S', price='150.00')
    rig.engine.book_external(1, T(MON, '10:30:00'), 150.0)
    assert state(rig)['live'].get('sale_unmatched')
    reconcile(rig, T(MON, '10:32:00'))                         # now visible: tied
    lv = state(rig)['live']
    assert lv['closed_by_orders'] == [x] and not lv.get('sale_unmatched')
    assert not rig.store.kill()['tripped']


# ====================================================================== R6-03
def test_r6_03_a_short_after_a_loud_kill_is_recorded_alerted_and_reminded(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.engine._trip(T(MON, '10:00:00'), 'ORDER QUANTITY UNIT: something', loud=True)
    n = len(rig.sent)
    rig.engine._trip(T(MON, '10:05:00'), 'SHORT: 130 sold today on X against 65', loud=True)
    k = rig.store.kill()
    assert 'SHORT' in k['reason'] and k['latest'].startswith('SHORT')
    assert len(rig.sent) == n + 1 and 'SHORT' in rig.sent[-1]
    rig.engine._trip(T(MON, '10:06:00'), 'SHORT: 130 sold today on X against 65', loud=True)
    assert len(rig.sent) == n + 1                              # the same reason is not repeated
    rig.service(T(MON, '10:07:00'))
    loud = [s for s in rig.sent if 'KILL (LOUD)' in s]
    assert loud and 'KILL (LOUD): SHORT' in loud[-1]           # the reminder leads with the newest


# ====================================================================== R6-04
@pytest.mark.parametrize('field', ['prod', 'ordDtTm'])
def test_r6_04_a_buy_row_with_no_product_or_date_never_nets_a_sale(cfg, tmp_path, market, field):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.clock['now'] = T(MON, '10:30:00')
    no = app_order(rig, CE_NEO, 'B', price='150.00')
    for o in rig.broker.orders:
        if o['nOrdNo'] == no:
            o[field] = ''
    rig.clock['now'] = T(MON, '10:31:00')
    app_order(rig, CE_NEO, 'S', price='151.00')                # he sells our NRML lot
    book = rig.broker.order_book()
    _, unexplained = rig.engine._foreign_sells(CE_NEO, T(MON, '10:32:00'), book)
    assert unexplained == LOT
    assert rig.engine._net_sold_today(CE_NEO, T(MON, '10:32:00'), book) == LOT


def test_r6_04_control_a_dated_nrml_buy_still_nets(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.clock['now'] = T(MON, '10:30:00')
    app_order(rig, CE_NEO, 'B', price='150.00')
    rig.clock['now'] = T(MON, '10:31:00')
    app_order(rig, CE_NEO, 'S', price='151.00')
    _, unexplained = rig.engine._foreign_sells(CE_NEO, T(MON, '10:32:00'), rig.broker.order_book())
    assert unexplained == 0


# ====================================================================== R6-05
def _ours_resting_slow(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    state(rig)['live']['exit_attempts'] = rig.neo.exit_chase_fast_max
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    return rig


def test_r6_05_a_foreign_working_sell_pulls_ours(cfg, tmp_path, market):
    rig = _ours_resting_slow(cfg, tmp_path, market)
    rig.clock['now'] = T(MON, '11:00:02')
    rig.broker.place(OrderRequest(symbol=CE_NEO, token='t', side='S', qty=LOT, price='69.00', tag=''))
    rig.broker.sent.pop()
    rig.service(T(MON, '11:00:03'))
    assert rig.broker.orders[0]['ordSt'] == 'cancelled'
    assert any('is WORKING' in s and 'CANCELLING OURS NOW' in s for s in rig.sent)
    rig.broker.hold_fills = False                              # his SELL fills
    for i in range(4, 90):
        rig.go(T(MON, '11:00:00') + timedelta(seconds=i))
    assert len(sells(rig)) == 1 and rig.broker.true_net(CE_NEO) == 0


def test_r6_05_with_a_slow_cancel_ours_is_pulled_and_never_doubled(cfg, tmp_path, market):
    rig = _ours_resting_slow(cfg, tmp_path, market)
    rig.broker.cancel_pending_sec = 5.0                        # NEO answers `cancel pending`
    rig.clock['now'] = T(MON, '11:00:01')
    app_order(rig, CE_NEO, 'S')
    rig.service(T(MON, '11:00:02'))
    assert rig.broker.orders[0]['ordSt'] == 'cancel pending'
    for i in range(3, 90):
        rig.go(T(MON, '11:00:00') + timedelta(seconds=i))
    assert rig.broker.orders[0]['ordSt'] == 'cancelled'
    assert len(sells(rig)) == 1 and rig.broker.true_net(CE_NEO) == 0


# ====================================================================== R6-07
def test_r6_07_the_refusal_backoff_starts_at_3s_again_the_next_day(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig, expiry='2026-11-03')
    rig.broker.place_faults = [('reject', '429', 'slow')] * 6
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    t0 = T(MON, '11:00:00')
    for i in range(0, 100):
        rig.go(t0 + timedelta(seconds=i))
    assert state(rig)['live']['refusal_step'] >= 5
    rig.engine._on_rejected(state(rig), {'kind': 'EXIT', 'tag': 'x', 'qty': LOT}, T(TUE, '09:25:00'),
                            'stCode 429: slow', placed=False, code='429')
    assert state(rig)['live']['refusal_wait_sec'] == 3.0


# ====================================================================== R6-09 (mutants)
def test_r6_09_m3_a_buy_with_our_tag_never_nets_an_owner_sale(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    p = holding(rig)
    rig.clock['now'] = T(MON, '10:30:00')
    no = rig.broker.place(OrderRequest(symbol=CE_NEO, token='t', side='B', qty=LOT, price='150.00',
                                       tag='20261026-9-ENTRY-1'))       # ours, not in the book
    rig.broker.sent.pop()
    for o in rig.broker.orders:
        if o['nOrdNo'] == no:
            o.update({'ordSt': 'complete', 'fldQty': str(LOT), 'avgPrc': '150.00'})
    rig.clock['now'] = T(MON, '10:31:00')
    app_order(rig, CE_NEO, 'S', price='151.00')
    _, unexplained = rig.engine._foreign_sells(CE_NEO, T(MON, '10:32:00'), rig.broker.order_book())
    assert unexplained == LOT
    ok, _ = rig.engine._sell_allowed(p, LOT, T(MON, '10:32:00'))
    assert not ok


def test_r6_09_m16_our_own_entry_buy_never_hides_a_short(cfg, tmp_path, market):
    """Our entry today (its tag not echoed, bound by number), our stop SELL
    fills, the owner sells too: a real short the SELL sum must catch on the
    FIRST reconcile (the row-shape excuse must not explain it away)."""
    rig = marked(cfg, tmp_path, market)
    _own_buy(rig, N2, T(MON, '10:00:00'), 1, '', S2, '2026-11-03')
    rig.market.set(S2, 45.0, bid=44.8, ask=45.2)
    rig.go(T(MON, '10:30:00'))
    rig.go(T(MON, '10:30:01'))
    assert state(rig)['status'] == 'closed'
    rig.clock['now'] = T(MON, '10:31:00')
    app_order(rig, N2, 'S', price='45.00')
    assert rig.broker.true_net(N2) == -LOT
    reconcile(rig, T(MON, '10:32:00'))
    k = rig.store.kill()
    assert k['tripped'] and k['loud'] and 'SHORT' in k['reason']


def test_r6_09_m5_an_unclaimed_owner_sale_inside_a_fresh_entrys_lag_is_not_adopted(cfg, tmp_path,
                                                                                     market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.positions_lag_sec = 60.0
    _own_buy(rig, N2, T(MON, '11:00:00'), 1, '20261026-1-ENTRY-1', S2, '2026-11-03')
    rig.clock['now'] = T(MON, '11:00:10')
    app_order(rig, N2, 'S', price='101.00')                    # he sells the fresh lot at once
    reconcile(rig, T(MON, '11:00:20'))                         # the row shows neither yet
    assert state(rig)['status'] == 'open'                      # deferred: inside the lag
    reconcile(rig, T(MON, '11:01:30'))                         # after the lag: his sale, booked
    assert state(rig)['exit_reason'] == 'closed_externally'
