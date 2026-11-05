"""The Stage-0 probe has no path to an order; the Stage-3 probe is bounded,
re-quotes, never proceeds past a non-terminal order, and always reports."""
import ast
import inspect
from datetime import timedelta

import pytest

from conftest import LOT, T, sym
from live_helpers import SCRIPS, neo_symbol
from nifty_btst import neo_probe
from nifty_btst.broker import BrokerUnavailable, BrokerUnclear, FakeBroker
from nifty_btst.journal import Journal
from nifty_btst.neo_client import NeoAuthError, NeoReader, NeoUnavailable
from nifty_btst.neo_order_probe import PROBE_MAX_PREMIUM_RS, OrderProbe, ProbeRefused
from nifty_btst.pricing import Book
from test_neo_symbols import HEADER, row

KITE_ROW = {'tradingsymbol': sym('2026-10-27', 22850, 'CE'), 'expiry': '2026-10-27',
            'strike': 22850.0, 'instrument_type': 'CE', 'lot_size': LOT}
NEO = neo_symbol(KITE_ROW['tradingsymbol'])


# --------------------------------------------------------- Stage 0 probe
def test_probe_module_cannot_place_or_cancel():
    src = inspect.getsource(neo_probe)
    src = src.replace(ast.get_docstring(ast.parse(src)) or '', '')     # code, not prose
    assert 'NeoTrader' not in src and '.place(' not in src and '.cancel(' not in src
    assert '_mutate' not in src and 'PATH_PLACE' not in src and 'PATH_CANCEL' not in src
    assert not hasattr(NeoReader, 'place') and not hasattr(NeoReader, 'cancel')
    assert inspect.signature(neo_probe.run_probe).parameters['reader'].annotation in ('NeoReader', NeoReader)


class FakeReader:
    creds = {'ucc': 'UCC123'}

    def __init__(self, fail=None):
        self.fail = fail or set()
        self.ready = False

    def login(self):
        if 'login' in self.fail:
            raise NeoAuthError('Invalid TOTP')
        self.ready = True
        return {'baseUrl': 'https://cis.example', 'ucc': 'UCC123', 'greetingName': 'Real Name',
                'token': 'T'}

    def scrip_master_paths(self):
        if 'paths' in self.fail:
            raise NeoUnavailable('down')
        return ['https://f/nse_cm.csv'] if 'nofo' in self.fail else ['https://f/nse_cm.csv', 'https://f/nse_fo.csv']

    def fetch_text(self, url):
        from datetime import date
        return (HEADER + row(1, 'NIFTY-WEEKLY-X', 'NIFTY', date(2026, 10, 27), 22850, 'CE')
                + row(2, 'NIFTY-WEEKLY-Y', 'NIFTY', date(2026, 10, 27), 22850, 'PE'))

    def raw_report(self, path, jdata=None):
        if 'reports' in self.fail:
            raise NeoUnavailable('OMS down')
        if 'limits' in path:
            return {'stat': 'Ok', 'stCode': 200, 'Net': '12345', 'data': {'Collateral': '0'}}
        if 'positions' in path:
            return {'stat': 'Not_Ok', 'stCode': 5203, 'emsg': 'No Data', 'data': None}
        return {'stat': 'Ok', 'stCode': 200, 'data': [{'nOrdNo': '1', 'ordSt': 'complete'}]}


def test_run_probe_prints_shapes_mapping_and_redacts_identity():
    out = []
    rc = neo_probe.run_probe(FakeReader(), 'NIFTY', T('2026-10-26', '10:00:00').date(),
                             kite_row=KITE_ROW, out=out.append)
    text = '\n'.join(out)
    assert rc == 0 and '<redacted>' in text and 'NIFTY-WEEKLY-X' in text and 'LOT MATCH' in text
    assert 'limits keys' in text and 'No Data' in text and 'PROBE OK' in text
    assert 'UCC123' not in text and 'Real Name' not in text                    # NEO-35


def test_run_probe_reports_each_failure_without_raising():
    out = []
    assert neo_probe.run_probe(FakeReader({'login'}), 'NIFTY', T('2026-10-26', '10:00:00').date(),
                               out=out.append) == 1
    assert 'LOGIN FAILED' in out[-1]
    out = []
    rc = neo_probe.run_probe(FakeReader({'paths', 'reports'}), 'NIFTY',
                             T('2026-10-26', '10:00:00').date(), out=out.append)
    assert rc == 1 and any('SCRIP MASTER FAILED' in s for s in out) and any('FAILED: OMS down' in s for s in out)
    out = []
    rc = neo_probe.run_probe(FakeReader({'nofo'}), 'NIFTY', T('2026-10-26', '10:00:00').date(),
                             kite_row=dict(KITE_ROW, strike=99.0), out=out.append)
    assert rc == 1 and any('NO nse_fo' in s for s in out)


# --------------------------------------------------------- Stage 3 probe
class ProbeRig:
    def __init__(self, tmp_path, fill_delay=1.0, cls=FakeBroker, start=T('2026-10-26', '10:00:00')):
        self.clock = {'now': start}
        self.fb = cls(lambda: self.clock['now'], fill_delay=fill_delay)
        self.fb.ensure_session()
        self.out = []
        self.quotes = [Book(4.9, 4.8, 5.0, 1000, 1000, '', ltt='2026-10-26T10:00:00')]
        self.dir = tmp_path
        self.probe = OrderProbe(self.fb, Journal(tmp_path), lambda: self.clock['now'], self.sleep,
                                self.out.append)

    def sleep(self, s):
        self.clock['now'] += timedelta(seconds=s)

    def quote(self):
        return self.quotes[-1]

    def run(self, step='all', max_premium=500, row=KITE_ROW):
        return self.probe.run(SCRIPS, row, self.quote, max_premium, step=step)


def test_order_probe_buys_and_sells_one_lot_and_ends_flat(tmp_path):
    r = ProbeRig(tmp_path)
    assert r.run() == 0 and r.probe.held == 0
    assert [q.side for q in r.fb.sent] == ['B', 'B', 'S'] and all(q.qty == LOT for q in r.fb.sent)
    assert r.fb.sent[0].price == '2.40' and r.fb.sent[1].price == '5.00' and r.fb.sent[2].price == '4.80'
    assert r.fb.true_net(NEO) == 0
    assert any('GuiOrdId echo: YES' in s for s in r.out) and 'flat (positions and live orders read)' in r.out[-1]
    j = Journal(tmp_path).lines()
    assert [ln['ev'] for ln in j] == ['INTENT', 'PLACED'] * 3 + ['PROBE_SOLD'] and j[0]['probe'] is True


def test_order_probe_tags_are_unique_across_runs_in_a_day(tmp_path):
    """NEO-33: a second probe must not reuse the first run's tags."""
    r1 = ProbeRig(tmp_path)
    r1.run()
    r2 = ProbeRig(tmp_path)
    r2.run()
    tags = [ln['tag'] for ln in Journal(tmp_path).lines() if ln['ev'] == 'INTENT']
    assert len(tags) == 6 and len(set(tags)) == 6


def test_order_probe_with_nothing_filling_cancels_and_ends_flat(tmp_path):
    r = ProbeRig(tmp_path, fill_delay=10000.0)
    assert r.run() == 0 and [q.side for q in r.fb.sent] == ['B', 'B']
    assert all(o['ordSt'] == 'cancelled' for o in r.fb.orders)


def test_order_probe_stops_when_step_a_is_not_terminal(tmp_path):
    """NEO-19: an unclear cancel used to be walked past, leaving a resting BUY
    while the run said flat."""
    r = ProbeRig(tmp_path, fill_delay=10000.0)
    r.fb.cancel_faults = ['timeout']
    r.fb.cancel_pending_sec = 10000.0
    rc = r.run()
    assert rc == 1 and [q.side for q in r.fb.sent] == ['B']
    assert r.probe.stopped and any('STOPPED' in s for s in r.out) and 'RUN STOPPED' in r.out[-1]


def test_order_probe_flat_check_sees_a_resting_order(tmp_path):
    r = ProbeRig(tmp_path, fill_delay=1.0)
    r.fb.ensure_session()
    from nifty_btst.broker import OrderRequest
    r.fb.hold_fills = True
    r.fb.place(OrderRequest(NEO, 't', 'B', LOT, '1.00', 'owner-manual'))      # resting, not ours
    r.fb.hold_fills = False
    r.fb.exchange_cancelled = []
    rc = r.run()
    assert rc == 1 and 'NOT FLAT' in r.out[-1]


def test_order_probe_reports_a_position_it_could_not_close(tmp_path):
    r = ProbeRig(tmp_path)
    r.fb.place_faults = [None, None] + [('reject', '1004', 'Insufficient balance')] * 12
    rc = r.run()
    assert rc == 1 and r.probe.held == LOT and 'HELD' in r.out[-1]


def test_order_probe_unclear_sell_with_unreadable_book_still_reports_held(tmp_path):
    """NEO-20: ProbeRefused mid-run used to lose the 'still held' line."""
    class DiesOnSell(FakeBroker):
        dead = False

        def place(self, req):
            if req.side == 'S':
                self.dead = True
                raise BrokerUnclear('place: timed out')
            return super().place(req)

        def order_book(self):
            if self.dead:
                raise BrokerUnavailable('down')
            return super().order_book()

        def positions(self):
            if self.dead:
                raise BrokerUnavailable('down')
            return super().positions()
    r = ProbeRig(tmp_path, cls=DiesOnSell)
    with pytest.raises(ProbeRefused):
        r.run()
    assert r.probe.held == LOT and any('HELD' in s for s in r.out[-2:])


def test_order_probe_sell_chase_requotes_each_round(tmp_path):
    """NEO-24: every SELL used to go at the quote taken before step A."""
    r = ProbeRig(tmp_path, fill_delay=1.0)
    orig = r.fb.place

    def place(req):
        if req.side == 'S':
            r.fb.hold_fills = True                                   # sells never fill
            r.quotes.append(Book(4.5, 4.5 - 0.1 * len(r.quotes), 4.7, 1000, 1000, '',
                                 ltt='2026-10-26T10:00:00'))
        return orig(req)
    r.fb.place = place
    rc = r.run()
    sells = [q.price for q in r.fb.sent if q.side == 'S']
    assert rc == 1 and len(sells) >= 3 and len(set(sells)) == len(sells)


def test_order_probe_refusals(tmp_path):
    r = ProbeRig(tmp_path)
    with pytest.raises(ProbeRefused, match='code ceiling'):
        r.run(max_premium=PROBE_MAX_PREMIUM_RS + 1)
    r.quotes.append(Book(19.9, 19.8, 20.0, 1000, 1000, ''))
    with pytest.raises(ProbeRefused, match='pick a cheaper'):
        r.run()
    r.quotes.append(Book(4.9, 4.8, 5.0, 1000, 1000, ''))
    with pytest.raises(ProbeRefused, match='no NEO row'):
        r.run(row=dict(KITE_ROW, strike=1.0))
    with pytest.raises(ProbeRefused, match='lot size'):
        r.run(row=dict(KITE_ROW, lot_size=75))
    r.quotes.append(Book(5, 0, 5.2, 0, 1, ''))
    with pytest.raises(ProbeRefused, match='not usable'):
        r.run()
    with pytest.raises(ProbeRefused, match='step'):
        r.run(step='fly')
    assert not r.fb.sent


def test_order_probe_unclear_send_is_searched_by_tag_for_a_while(tmp_path):
    r = ProbeRig(tmp_path)
    r.fb.place_faults = ['timeout_reached']
    r.fb.visibility_lag_sec = 10.0                                   # appears late
    rc = r.run()
    assert rc == 0
    evs = [ln['ev'] for ln in Journal(tmp_path).lines()]
    assert evs[:2] == ['INTENT', 'BOUND']


def test_order_probe_unclear_send_never_found_stops_the_run(tmp_path):
    r = ProbeRig(tmp_path)
    r.fb.place_faults = ['timeout_lost']
    with pytest.raises(ProbeRefused, match='unresolved'):
        r.run()
    assert Journal(tmp_path).unresolved()                            # stays unresolved on purpose


def test_order_probe_hold_then_sell_next_morning(tmp_path):
    """The Stage-3 overnight step that settles NEO-16 and NEO-06."""
    r = ProbeRig(tmp_path)
    assert r.run(step='hold') == 0 and r.probe.held == LOT
    assert any('overnight ON PURPOSE' in s for s in r.out)
    r2 = ProbeRig(tmp_path, start=T('2026-10-27', '09:20:00'))
    r2.fb.orders = r.fb.orders                                       # same exchange, next day
    r2.fb.carry_forward_rows = False                                 # the row is absent
    rc = r2.run(step='sell')
    assert rc == 1 and any('ABSENT' in s for s in r2.out) and not [q for q in r2.fb.sent if q.side == 'S']
    r3 = ProbeRig(tmp_path, start=T('2026-10-27', '09:21:00'))
    r3.fb.orders = r.fb.orders
    r3.fb.carry_forward_rows = False
    r3.probe.allow_missing_row = True                                # without the owner: refused
    assert r3.run(step='sell') == 1 and not r3.fb.sent
    r3.probe.owner_confirms_held = True
    rc = r3.run(step='sell')
    assert rc == 0 and [q.side for q in r3.fb.sent] == ['S'] and r3.fb.true_net(NEO) == 0
    r4 = ProbeRig(tmp_path, start=T('2026-10-28', '09:21:00'))
    r4.fb.set_external_position(NEO, LOT, day=T('2026-10-27', '00:00:00').date())
    rc = r4.run(step='sell')                                          # row PRESENT: sells via the guard
    assert rc == 0 and any('PRESENT' in s for s in r4.out)


def test_run_probe_states_the_funds_value_outright():
    # The real limits envelope runs past the print limit, so `Net` was never visible.
    lines = []
    neo_probe.run_probe(FakeReader(), 'NIFTY', T('2026-10-26', '10:00:00').date(), out=lines.append)
    assert any(line.startswith('limits Net = 12345') for line in lines)
