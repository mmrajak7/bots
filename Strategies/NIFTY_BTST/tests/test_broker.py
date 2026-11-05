"""Broker views over string-typed NEO rows; NeoBroker error mapping; the
hostile FakeBroker (lags, resets, missing rows, cancel pending, persistence)."""
from datetime import timedelta

import pytest

from conftest import T
from nifty_btst.broker import (BrokerAuth, BrokerRejected, BrokerUnavailable, BrokerUnclear,
                               FakeBroker, NeoBroker, OrderRequest, OrderView, PositionView)
from nifty_btst.neo_client import NeoAuthError, NeoRejected, NeoUnavailable, NeoUnclear


def test_order_view_parses_strings_and_terminal_states():
    o = OrderView.from_neo({'nOrdNo': '260123000487752', 'ordSt': 'Complete ', 'trdSym': 'X',
                            'trnsTp': 'b', 'qty': '65', 'fldQty': '65', 'avgPrc': '160.65',
                            'prc': '160.70', 'rejRsn': '--', 'GuiOrdId': 'tag', 'prod': 'NRML',
                            'ordDtTm': '26-Oct-2026 10:00:03'})
    assert o.status == 'complete' and o.side == 'B' and o.qty == 65 and o.filled == 65
    assert o.avg_price == 160.65 and o.rej_reason == '' and o.terminal and not o.live
    r = OrderView.from_neo({'nOrdNo': '1', 'ordSt': 'rejected', 'rejRsn': 'RMS:Margin Exceeds',
                            'qty': 'abc', 'fldQty': None})
    assert r.rej_reason.startswith('RMS') and r.qty == 0 and r.filled == 0 and r.tag == ''
    assert OrderView.from_neo({'ordSt': 'open pending'}).live
    assert OrderView.from_neo({'ordSt': 'trigger pending'}).live
    assert OrderView.from_neo({'ordSt': 'cancel pending'}).live
    assert OrderView.from_neo({'ordSt': 'something new'}).live        # unknown = live


def test_position_view_uses_quantities_only_never_the_average():
    p = PositionView.from_neo({'trdSym': 'X', 'prod': 'NRML', 'flBuyQty': '65', 'flSellQty': '0',
                               'cfBuyQty': '130', 'cfSellQty': '65', 'buyAmt': 'garbage'})
    assert p.net_qty == 130
    q = PositionView.from_neo({'sym': 'Y', 'flBuyQty': '0', 'flSellQty': '65'})
    assert q.symbol == 'Y' and q.net_qty == -65


class FakeTrader:
    ready = False

    def __init__(self):
        self.calls = []
        self.fail = {}

    def login(self):
        self.calls.append('login')
        if self.fail.get('login'):
            raise NeoAuthError('bad totp')
        self.ready = True

    def _f(self, name, ok):
        self.calls.append(name)
        f = self.fail.get(name)
        if f:
            raise f
        return ok

    def place(self, **kw):
        return self._f('place', '77')

    def cancel(self, no):
        return self._f('cancel', no)

    def order_book(self):
        return self._f('order_book', [{'nOrdNo': '77', 'ordSt': 'open', 'trdSym': 'S', 'trnsTp': 'S'}])

    def positions(self):
        return self._f('positions', [{'trdSym': 'S', 'prod': 'NRML', 'flBuyQty': '65'},
                                     {'trdSym': 'S', 'prod': 'MIS', 'flBuyQty': '1'}])

    def trades(self):
        return self._f('trades', [{'nOrdNo': '77', 'fldQty': '65'}])

    def limits(self):
        return self._f('limits', {'stat': 'Ok', 'Net': '1,23,456.50'})


def test_neo_broker_maps_errors_and_reads():
    t = FakeTrader()
    b = NeoBroker(t, 'nse_fo', 'NRML', 'Net')
    b.ensure_session()
    assert t.ready
    req = OrderRequest('S', '1', 'B', 65, '1.00', 'tag')
    assert b.place(req) == '77'
    b.cancel('77')
    assert b.order('77').status == 'open' and b.order('78') is None
    assert b.net_long('S', 'NRML') == 65 and b.net_long('S', '') == 66
    assert b.trades()[0].filled == 65
    assert b.available_funds() == 123456.5
    t.fail['place'] = NeoRejected(1004, 'Insufficient balance.')
    with pytest.raises(BrokerRejected) as ei:
        b.place(req)
    assert ei.value.code == '1004'
    t.fail['place'] = NeoUnclear('timed out')
    with pytest.raises(BrokerUnclear):
        b.place(req)
    t.fail['place'] = NeoAuthError('dead')
    with pytest.raises(BrokerAuth):
        b.place(req)
    t.fail['cancel'] = NeoUnclear('x')
    with pytest.raises(BrokerUnclear):
        b.cancel('77')
    t.fail['cancel'] = NeoRejected('1006', 'already')
    with pytest.raises(BrokerRejected):
        b.cancel('77')
    t.fail['order_book'] = NeoUnavailable('down')
    with pytest.raises(BrokerUnavailable):
        b.order_book()
    t.fail['positions'] = NeoAuthError('dead')
    with pytest.raises(BrokerAuth):
        b.positions()
    t.fail['limits'] = None
    t.limits = lambda: {'stat': 'Ok', 'data': {'Net': '99'}}
    assert b.available_funds() == 99.0
    t.limits = lambda: {'stat': 'Ok', 'Other': '1'}
    assert b.available_funds() is None
    t.limits = lambda: {'stat': 'Ok', 'Net': 'n/a'}
    assert b.available_funds() is None
    t2 = FakeTrader()
    t2.fail['login'] = True
    with pytest.raises(BrokerAuth):
        NeoBroker(t2, 'nse_fo', 'NRML', 'Net').ensure_session()


def _fb(fill_delay=2.0, path=None):
    clock = {'now': T('2026-10-26', '10:00:00')}
    fb = FakeBroker(lambda: clock['now'], fill_delay=fill_delay, path=path)
    fb.ensure_session()
    return fb, clock


def test_fake_broker_fills_after_delay_and_reports_positions():
    fb, clock = _fb()
    no = fb.place(OrderRequest('S', '1', 'B', 65, '10.00', 'tag'))
    assert fb.order(no).status == 'open' and fb.positions() == []
    clock['now'] = T('2026-10-26', '10:00:02')
    o = fb.order(no)
    assert o.status == 'complete' and o.avg_price == 10.0 and o.raw['fldQty'] == '65'
    assert fb.net_long('S', 'NRML') == 65 and fb.positions()[0].raw['flBuyQty'] == '65'
    with pytest.raises(BrokerRejected):
        fb.cancel(no)                                     # already complete
    with pytest.raises(BrokerRejected):
        fb.cancel('nope')
    fb.place_faults = [('reject', '1004', 'Insufficient balance')]
    with pytest.raises(BrokerRejected):
        fb.place(OrderRequest('S', '1', 'B', 65, '10.00', 'tag2'))
    fb.read_faults = ['unavailable']
    with pytest.raises(BrokerUnavailable):
        fb.order_book()
    fb.login_fail = True
    with pytest.raises(BrokerAuth):
        fb.ensure_session()


def test_fake_broker_visibility_and_positions_lag():
    fb, clock = _fb(fill_delay=0.0)
    fb.visibility_lag_sec = 30.0
    fb.positions_lag_sec = 10.0
    fb.place_faults = ['timeout_reached']
    with pytest.raises(BrokerUnclear):
        fb.place(OrderRequest('S', '1', 'S', 65, '10.00', 'tag'))
    assert fb.order_book() == [] and fb.positions() == []            # reached, invisible
    assert fb.true_net('S') == -65                                   # but the exchange filled it
    clock['now'] += timedelta(seconds=5)
    assert fb.positions() == []                                      # positions still lag
    clock['now'] += timedelta(seconds=6)
    assert fb.net_long('S', 'NRML') == -65 and fb.order_book() == []  # positions first...
    clock['now'] += timedelta(seconds=21)
    [o] = fb.order_book()                                            # ...then the book
    assert o.status == 'complete'


def test_fake_broker_daily_reset_mass_cancel_and_carry_forward_rows():
    fb, clock = _fb(fill_delay=0.0)
    filled = fb.place(OrderRequest('S', '1', 'B', 65, '10.00', 'fill'))
    assert fb.order(filled).status == 'complete'
    fb.hold_fills = True
    resting = fb.place(OrderRequest('S', '1', 'B', 65, '10.00', 'rest'))
    assert fb.order(resting).status == 'open'
    clock['now'] = T('2026-10-27', '09:16:00')                       # next day
    assert fb.order(resting) is None and fb.order(filled) is None   # gone from the DAY book
    assert [o for o in fb.orders if o['nOrdNo'] == resting][0]['ordSt'] == 'cancelled'
    [p] = fb.positions()
    assert p.raw['cfBuyQty'] == '65' and p.raw['flBuyQty'] == '0' and p.net_qty == 65
    fb.carry_forward_rows = False
    assert fb.positions() == [] and fb.true_net('S') == 65          # no trade today: no row
    fb.daily_book_reset = False
    assert fb.order(filled) is not None


def test_fake_broker_cancel_pending_and_partial():
    fb, clock = _fb(fill_delay=10000.0)
    fb.cancel_pending_sec = 5.0
    no = fb.place(OrderRequest('S', '1', 'S', 130, '10.00', 'tag'))
    fb.cancel_faults = ['partial']
    fb.cancel(no)
    assert fb.order(no).status == 'cancel pending' and fb.order(no).filled == 65
    clock['now'] += timedelta(seconds=5)
    assert fb.order(no).status == 'cancelled'
    fb.blank_book = True
    assert fb.order_book() == [] and fb.order(no) is None


def test_fake_broker_persists_across_processes(tmp_path):
    path = tmp_path / 'fake.json'
    fb, clock = _fb(fill_delay=0.0, path=path)
    no = fb.place(OrderRequest('S', '1', 'B', 65, '10.00', 'tag'))
    fb.set_external_position('X', 65)
    assert fb.order(no).status == 'complete'
    fb2 = FakeBroker(lambda: clock['now'], fill_delay=0.0, path=path)
    assert fb2.order(no).status == 'complete' and fb2.net_long('S', 'NRML') == 65
    assert fb2.net_long('X', 'NRML') == 65
    no2 = fb2.place(OrderRequest('S', '1', 'S', 65, '10.00', 'tag2'))
    assert no2 != no
