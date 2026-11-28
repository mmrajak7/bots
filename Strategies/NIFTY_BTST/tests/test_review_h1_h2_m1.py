"""Independent go-live review, 2026-11-28: H1 (an unclear SELL waits its own,
shorter grace), H2 (the product field is normalised, and our own tagged row is
never filtered out), M1 (NEO's trade book is one row per FILL; readers get one
row per ORDER)."""
from datetime import timedelta

import pytest

from conftest import LOT, T
from live_helpers import MON
from nifty_btst.broker import NeoBroker, OrderView, in_scope, per_order
from nifty_btst.live_config import ConfigError
from nifty_btst.live_engine import ENTRY_SENT, EXIT_SENT
from test_live_engine import CE_MON, CE_NEO, holding, marked, state, trigger_ce


def _sells(rig):
    return [r for r in rig.broker.sent if r.side == 'S']


# ------------------------------------------------------------------ H1
def test_an_unclear_sell_that_never_arrived_is_resent_after_the_exit_grace(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)                  # defaults: exit 90 s, entry 600 s
    holding(rig)
    rig.engine.reconcile_ok = True
    rig.broker.place_faults = ['timeout_lost']           # the 401/timeout: nothing reached NEO
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)     # through the stop
    rig.go(T(MON, '11:00:00'))
    assert len(_sells(rig)) == 1
    for i in range(3, 88, 3):                            # inside 90 s: nothing re-sent
        rig.go(T(MON, '11:00:00') + timedelta(seconds=i))
        assert len(_sells(rig)) == 1, i
        assert state(rig)['live']['state'] == EXIT_SENT
    rig.go(T(MON, '11:01:31'))                           # past the exit grace, positions agree
    rig.go(T(MON, '11:01:35'))
    assert len(_sells(rig)) == 2 and _sells(rig)[1].tag.endswith('EXIT-2')
    assert any(ln['ev'] == 'NOT_PLACED' for ln in rig.journal.lines())


def test_an_exit_is_never_resent_on_fewer_than_three_valid_reads(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.engine.reconcile_ok = True
    rig.broker.place_faults = ['timeout_lost']
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    rig.go(T(MON, '11:05:00'))                           # long past 90 s, but only 1 read
    assert len(_sells(rig)) == 1


def test_an_unclear_entry_still_waits_the_long_grace(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.place_faults = ['timeout_lost']
    trigger_ce(rig)
    for s in (30, 60, 95, 120, 300, 590):                # far past the EXIT grace
        rig.go(T(MON, '10:00:00') + timedelta(seconds=s))
        assert state(rig)['live']['state'] == ENTRY_SENT and len(rig.broker.sent) == 1, s


def test_the_exit_grace_cannot_be_set_inside_the_positions_lag(cfg, tmp_path, market):
    with pytest.raises(ConfigError, match='exit_unknown_grace_sec'):
        marked(cfg, tmp_path, market, neo_overrides={'exit_unknown_grace_sec': 30})


# ------------------------------------------------------------------ H2
def _row(tag='', prod='NRML'):
    return OrderView(order_no='1', status='complete', symbol=CE_NEO, side='S', qty=LOT,
                     filled=LOT, avg_price=70.0, price=70.0, rej_reason='', tag=tag,
                     placed_at='26-Oct-2026 10:30:00', product=prod)


def test_the_product_is_normalised_where_the_row_is_read():
    r = {'nOrdNo': '1', 'ordSt': 'complete', 'trdSym': CE_NEO, 'trnsTp': 'S', 'qty': '65',
         'fldQty': '65', 'prod': ' nrml '}
    assert OrderView.from_neo(r).product == 'NRML'
    assert in_scope(OrderView.from_neo(r), 'NRML')
    r['prod'] = 'mis'
    assert not in_scope(OrderView.from_neo(r), 'NRML')


def test_our_own_tagged_row_is_never_filtered_whatever_the_product_says():
    for prod in ('MIS', 'Normal', 'CNC', 'NRML', ''):
        assert in_scope(_row(tag='20261026-1-EXIT-1', prod=prod), 'NRML'), prod


def test_negative_control_an_untagged_mis_row_is_still_dropped():
    assert not in_scope(_row(tag='', prod='MIS'), 'NRML')
    assert not in_scope(_row(tag='20261026-1-EXIT', prod='MIS'), 'NRML')   # not our shape


# ------------------------------------------------------------------ M1
def _fill(no, qty, px, tag='20261026-1-ENTRY-1', at='26-Oct-2026 10:00:01'):
    return {'nOrdNo': no, 'ordSt': 'traded', 'trdSym': CE_NEO, 'trnsTp': 'B', 'qty': '0',
            'fldQty': str(qty), 'avgPrc': '%.2f' % px, 'GuiOrdId': tag, 'ordDtTm': at,
            'prod': 'NRML', 'rptTp': 'fill'}


def test_per_fill_rows_sum_into_one_row_per_order():
    rows = [OrderView.from_neo(r) for r in (
        _fill('A', 40, 150.0, at='26-Oct-2026 10:00:02'),
        _fill('B', 65, 70.0, tag='20261026-2-ENTRY-1'),
        _fill('A', 25, 151.0, at='26-Oct-2026 10:00:01'))]
    out = per_order(rows)
    assert [o.order_no for o in out] == ['A', 'B']
    a = out[0]
    assert a.filled == 65 and a.avg_price == pytest.approx((40 * 150 + 25 * 151) / 65, abs=1e-4)
    assert a.placed_at == '26-Oct-2026 10:00:01' and a.tag == '20261026-1-ENTRY-1'
    assert out[1].filled == 65


def test_a_row_without_an_order_number_is_kept_as_it_is():
    r = OrderView.from_neo(_fill('', 10, 1.0))
    assert per_order([r, r]) == [r, r]


class _Trader:
    ready = True

    def trades(self):
        return [_fill('A', 40, 150.0), _fill('A', 25, 151.0)]


def test_neo_broker_trades_returns_the_summed_fill():
    t = NeoBroker(_Trader(), 'nse_fo', 'NRML', 'Net').trades()
    assert len(t) == 1 and t[0].filled == 65            # was 40: the first fill row only


# ------------------------------------------------------------------ L1
def _tg(tmp_path):
    p = tmp_path / 'tg.json'
    p.write_text('{"bot_token": "T", "chat_id": "C"}')
    return p


class _R:
    def __init__(self, code, body=None):
        self.status_code, self.text, self._b = code, 'x', body or {}

    def json(self):
        return self._b


def _notifier(tmp_path, monkeypatch, replies):
    import requests
    from nifty_btst import alerts
    waits, calls = [], []
    monkeypatch.setattr(alerts.time, 'sleep', lambda s: waits.append(s))

    def post(url, data=None, timeout=None):
        calls.append(data['text'])
        r = replies[min(len(calls), len(replies)) - 1]
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(requests, 'post', post)
    n = alerts.AsyncNotifier.__new__(alerts.AsyncNotifier)
    alerts.Notifier.__init__(n, _tg(tmp_path), True, 'X')
    n.posted = n.failed = 0
    return n, waits, calls


def test_a_telegram_429_waits_what_telegram_asks(tmp_path, monkeypatch):
    n, waits, calls = _notifier(tmp_path, monkeypatch, [
        _R(429, {'parameters': {'retry_after': 7}}), _R(200)])
    assert n._post(n._cfg, 'KILL TRIPPED', timeout=1, retries=6)
    assert waits == [7.0] and len(calls) == 2


def test_an_outage_backs_off_longer_than_three_seconds(tmp_path, monkeypatch):
    import requests
    from nifty_btst import alerts
    blip = requests.ConnectionError('down')
    n, waits, calls = _notifier(tmp_path, monkeypatch, [blip, blip, blip, blip, _R(200)])
    assert n._post(n._cfg, 'KILL TRIPPED', timeout=1, retries=alerts.POST_RETRIES)
    assert waits == [1.0, 2.0, 4.0, 8.0]                 # 15 s of outage survived (was 2 s)


def test_a_bad_token_is_not_retried(tmp_path, monkeypatch):
    n, waits, calls = _notifier(tmp_path, monkeypatch, [_R(401)])
    assert not n._post(n._cfg, 'x', timeout=1, retries=6)
    assert len(calls) == 1 and n.failed == 1
