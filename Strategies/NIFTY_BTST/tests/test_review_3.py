"""Regression tests for docs/NEO_REVIEW_3.md (R3-01..R3-37).

The reviewer's reproductions (which PASSED while showing each defect) are
ported here with the CORRECT behaviour asserted, so each fails again if its
fix is removed. Numbering follows the review; R3-36 is documentation only.
"""
import json
import logging
import threading
import time as _time
from datetime import date, timedelta

import pytest
from conftest import LOT, T, default_premiums, sym
from live_helpers import MON, SCRIPS, TUE, WED, Rig, neo_raw, neo_symbol
from nifty_btst import arming, cli, config as config_mod, journal as jmod, live_cli, neo_client as nc
from nifty_btst.alerts import QUEUE_MAX, AsyncNotifier
from nifty_btst.broker import BrokerUnavailable, NeoBroker, OrderRequest, OrderView, PositionView
from nifty_btst.live_config import build_neo, rule_diffs
from nifty_btst.live_engine import ENTRY_OPEN, EXIT_OPEN, LiveStore
from nifty_btst.neo_order_probe import OrderProbe, ProbeRefused
from nifty_btst.notify import NullNotifier
from test_live_cli import _advancing_clock, _set_execution, wired  # noqa: F401 - fixture
from test_live_engine import CE_MON, CE_NEO, holding, marked, state, trigger_ce
from test_neo_client import CREDS, FakeHTTP, R, login_ok
from test_probe_safety import KITE_ROW, NEO, FakeReader, ProbeRig


def sells(rig):
    return [r for r in rig.broker.sent if r.side == 'S']


def buys(rig):
    return [r for r in rig.broker.sent if r.side == 'B']


def carried_no_row(cfg, tmp_path, market, **kw):
    """A lot bought Monday, truly held at the broker, Tuesday's report has no row."""
    rig = marked(cfg, tmp_path, market, **kw)
    s = sym('2026-11-03', 22850, 'CE')
    holding(rig, symbol=s, expiry='2026-11-03')
    rig.broker.ext_positions_day[neo_symbol(s)] = MON
    rig.broker.carry_forward_rows = False
    default_premiums(market, '2026-11-03')
    rig.market.set(s, 170.0, bid=169.8, ask=170.2)
    rig.restart()
    rig.start(T(TUE, '09:00:00'))
    return rig, s


def _real_record(d, sym_='REALLONG'):
    d.mkdir(parents=True, exist_ok=True)
    s = LiveStore(d)
    s.add_position({'status': 'open', 'side': 'CE', 'symbol': sym_, 'qty': 65, 'lot_size': 65,
                    'expiry': '2026-10-27', 'entry_date': '2026-10-23', 'entry_price': 150.0,
                    'stop_level': 75.0, 'trigger': 150.0, 'exit_defers': 0, 'cls': 'other',
                    'dte_sessions': 1, 'dte_days': 1, 'token': 1, 'strike': 22850.0,
                    'live': {'state': 'FILLED', 'neo_symbol': 'X', 'neo_token': 't', 'tick': 0.05,
                             'leg': None, 'exit_fills': []}})
    s.save(force=True)


def stamped(pos_id, symbol, day, **extra):
    body = {'id': pos_id, 'symbol': symbol, 'date': day}
    body.update(extra)
    return json.dumps(body)


# ---------------------------------------------------------------- R3-01 / R3-18 / R3-29
def test_r3_01_a_terminal_zero_fill_sell_does_not_end_the_owner_confirmed_chase(cfg, tmp_path, market):
    rig, s = carried_no_row(cfg, tmp_path, market)
    n = neo_symbol(s)
    rig.engine.confirm_held(1, T(TUE, '09:10:00'))
    rig.broker.hold_fills = True
    rig.go(T(TUE, '09:15:05'))
    rig.go(T(TUE, '09:25:01'))
    assert len(sells(rig)) == 1
    rig.broker.hold_fills = False
    rig.broker.fill_delay = 1000.0                  # still resting when the 3 s cancel lands
    for i in range(1, 120):
        rig.go(T(TUE, '09:25:01') + timedelta(seconds=i))
    assert len(sells(rig)) >= 2                     # cancelled unfilled: the chase goes on
    assert rig.broker.true_net(n) == LOT and state(rig)['status'] == 'open'
    assert not any('already exists today' in x for x in rig.sent)
    rig.broker.fill_delay = 0.0
    for i in range(120, 200):
        rig.go(T(TUE, '09:25:01') + timedelta(seconds=i))
    assert state(rig)['status'] == 'closed' and rig.broker.true_net(n) == 0
    assert len([o for o in rig.broker.orders if o['ordSt'] == 'complete']) == 1


def test_r3_01b_a_rejected_sell_does_not_end_the_chase_either(cfg, tmp_path, market):
    rig, s = carried_no_row(cfg, tmp_path, market)
    n = neo_symbol(s)
    rig.engine.confirm_held(1, T(TUE, '09:10:00'))
    rig.broker.place_faults = [('placed_rejected', 'RMS: transient')]
    rig.go(T(TUE, '09:25:01'))
    for i in range(1, 60):
        rig.go(T(TUE, '09:25:01') + timedelta(seconds=i))
    assert len(sells(rig)) >= 2 and rig.broker.true_net(n) == 0
    assert state(rig)['status'] == 'closed'


def test_r3_18_holdings_vouches_only_without_a_counting_sell_today(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market, neo_overrides={'holdings_source_enabled': True})
    s = sym('2026-11-03', 22850, 'CE')
    holding(rig, symbol=s, expiry='2026-11-03')
    n = neo_symbol(s)
    rig.broker.ext_positions[n] = 0                   # sold in the app this morning
    rig.broker.carry_forward_rows = False
    rig.broker.holdings_source = {n: LOT}             # Holdings still lists the start-of-day lot
    default_premiums(market, '2026-11-03')
    rig.market.set(s, 170.0, bid=169.8, ask=170.2)
    rig.restart()
    rig.start(T(TUE, '09:00:00'))
    app_sell = OrderView(order_no='APP1', status='complete', symbol=n, side='S', qty=LOT,
                         filled=LOT, avg_price=168.0, price=168.0, rej_reason='', tag='',
                         placed_at='27-Oct-2026 09:05:00', product='NRML')
    real_trades = rig.broker.trades
    rig.broker.trades = lambda: real_trades() + [app_sell]
    rig.go(T(TUE, '09:25:01'))
    rig.go(T(TUE, '09:26:02'))                        # the refusal alert is held a minute
    assert not sells(rig) and rig.broker.true_net(n) == 0
    assert any('holdings lot but a SELL' in x for x in rig.sent)


def test_r3_29_an_unreadable_sell_voids_the_confirmation(cfg, tmp_path, market):
    rig, s = carried_no_row(cfg, tmp_path, market)
    n = neo_symbol(s)
    rig.engine.confirm_held(1, T(TUE, '09:10:00'))
    rig.broker.visibility_lag_sec = 10000.0
    rig.broker.positions_lag_sec = 10000.0
    rig.broker.place_faults = ['timeout_reached']
    t0 = T(TUE, '09:25:01')
    rig.go(t0)
    for i in range(3, 700, 3):
        rig.go(t0 + timedelta(seconds=i))
    assert len(sells(rig)) == 1 and rig.broker.true_net(n) >= 0
    assert not rig.store.owner_confirmed_held(1, t0.date())
    assert any('VOID' in x for x in rig.sent)


def test_r3_29b_a_counting_sell_by_the_owner_voids_the_confirmation(cfg, tmp_path, market):
    rig, s = carried_no_row(cfg, tmp_path, market)
    n = neo_symbol(s)
    rig.engine.confirm_held(1, T(TUE, '09:10:00'))
    app_sell = OrderView(order_no='APP1', status='complete', symbol=n, side='S', qty=LOT,
                         filled=LOT, avg_price=168.0, price=168.0, rej_reason='', tag='',
                         placed_at='27-Oct-2026 09:12:00', product='NRML')
    real_trades = rig.broker.trades
    rig.broker.trades = lambda: real_trades() + [app_sell]
    rig.go(T(TUE, '09:25:01'))
    assert not sells(rig)
    assert not rig.store.owner_confirmed_held(1, T(TUE, '09:25:01').date())   # voided, journalled
    assert any(a.get('action') == 'void_confirmation' for a in rig.store.data['owner_actions'])


def test_r3_a_the_order_probe_console_is_redacted(tmp_path):
    r = ProbeRig(tmp_path)
    r.probe.redactions = ['UCCX']
    r.probe._say('limits for UCCX: ok')
    assert r.out[-1] == 'limits for <redacted>: ok' and r.probe.report[-1] == r.out[-1]


# ---------------------------------------------------------------- R3-02 / R3-22 / R3-23
def test_r3_02_a_request_from_another_day_or_without_a_stamp_is_refused(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    s = sym('2026-11-03', 22850, 'CE')
    holding(rig, symbol=s, expiry='2026-11-03')
    n = neo_symbol(s)
    (rig.dir / 'CONFIRM_HELD_1').write_text(T(MON, '14:00:00').isoformat())    # the old shape
    rig.broker.ext_positions[n] = 0                   # sold in the app Monday 14:30
    rig.broker.carry_forward_rows = False
    default_premiums(market, '2026-11-03')
    rig.market.set(s, 170.0, bid=169.8, ask=170.2)
    rig.restart()
    rig.start(T(TUE, '09:00:00'))
    rig.go(T(TUE, '09:15:05'))
    assert not rig.store.owner_confirmed_held(1, T(TUE, '09:15:05').date())
    assert (rig.dir / 'CONFIRM_HELD_1.bad').exists() and not (rig.dir / 'CONFIRM_HELD_1').exists()
    assert any('unreadable' in x and 'moved aside' in x for x in rig.sent)
    (rig.dir / 'CONFIRM_HELD_1').write_text(stamped(1, s, MON))              # yesterday's statement
    rig.go(T(TUE, '09:16:05'))
    res = json.loads((rig.dir / 'CONFIRM_HELD_1.result').read_text())
    assert res['applied'] is False and 'stale' in res['text']
    (rig.dir / 'CONFIRM_HELD_1').write_text(stamped(1, 'OTHER', TUE))        # wrong symbol
    rig.go(T(TUE, '09:17:05'))
    res = json.loads((rig.dir / 'CONFIRM_HELD_1.result').read_text())
    assert res['applied'] is False and 'does not match' in res['text']
    rig.go(T(TUE, '09:25:01'))
    assert not sells(rig) and rig.broker.true_net(n) == 0
    assert not rig.store.owner_confirmed_held(1, T(TUE, '09:25:01').date())


def test_r3_02b_cli_writes_a_stamped_file_and_says_truthfully_when_nothing_read_it(
        wired, monkeypatch, capsys):  # noqa: F811 - fixture from test_live_cli
    monkeypatch.setattr(live_cli._Lock, 'acquire', lambda self: False)
    monkeypatch.setattr(live_cli, 'REQUEST_ECHO_WAIT_SEC', 0.6)
    _real_record(wired['live'])
    assert cli.main(['live-confirm-held', '1', '--yes']) == 1
    out = capsys.readouterr().out
    assert 'NOT APPLIED' in out and 'has not read' in out and not out.startswith('APPLIED')
    body = json.loads((wired['live'] / 'CONFIRM_HELD_1').read_text())
    assert body['id'] == 1 and body['symbol'] == 'REALLONG'
    assert body['date'] == live_cli.now_ist().date().isoformat()


def test_r3_22_a_torn_request_file_is_moved_aside_once(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    (rig.dir / 'BOOK_EXTERNAL_1.json').write_text('')      # power cut inside the write
    (rig.dir / 'CONFIRM_HELD_abc').write_text('')
    before = len(rig.sent)
    for i in range(10):
        rig.service(T(MON, '11:00:00') + timedelta(seconds=i))
    assert len(rig.sent) - before == 2
    assert (rig.dir / 'BOOK_EXTERNAL_1.json.bad').exists()
    assert (rig.dir / 'CONFIRM_HELD_abc.bad').exists()
    assert not (rig.dir / 'BOOK_EXTERNAL_1.json').exists() and state(rig)['status'] == 'open'


def test_r3_23_cli_refuses_truthfully_and_echoes_what_it_applied(wired, capsys):  # noqa: F811 - fixture from test_live_cli
    wired['live'].mkdir(parents=True, exist_ok=True)
    assert cli.main(['live-confirm-held', '99', '--yes']) == live_cli.EXIT_REFUSED
    out = capsys.readouterr().out
    assert 'REFUSED' in out and 'no position #99' in out and 'applied' not in out
    assert not (wired['live'] / 'CONFIRM_HELD_99').exists()
    _real_record(wired['live'])
    assert cli.main(['live-confirm-held', '1', '--yes']) == 0
    out = capsys.readouterr().out
    assert 'APPLIED' in out and 'CONFIRMED #1' in out and 'x65' in out
    assert not (wired['live'] / 'CONFIRM_HELD_1').exists()
    assert not (wired['live'] / 'CONFIRM_HELD_1.result').exists()
    # a second confirm on a closed record: the engine's refusal reaches the owner verbatim
    s = LiveStore(wired['live'])
    s.data['positions'][0]['status'] = 'closed'
    s.save(force=True)
    assert cli.main(['live-confirm-held', '1', '--yes']) == live_cli.EXIT_REFUSED
    assert 'NOT APPLIED' in capsys.readouterr().out


# ---------------------------------------------------------------- R3-03 / R3-15
def test_r3_03_book_external_never_writes_off_a_live_order(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    assert state(rig)['live']['state'] == EXIT_OPEN
    rig.broker.ext_positions[CE_NEO] = 0                   # the owner sold in the app
    (rig.dir / 'BOOK_EXTERNAL_1.json').write_text(stamped(1, CE_MON, MON, price=68.0))
    rig.service(T(MON, '11:00:02'))
    res = json.loads((rig.dir / 'BOOK_EXTERNAL_1.json.result').read_text())
    assert res['applied'] is False and 'cancel requested' in res['text']
    assert rig.broker.orders[0]['ordSt'] == 'cancelled'
    rig.service(T(MON, '11:00:03'))                        # read terminal: the request completes
    p = state(rig)
    assert p['status'] == 'closed' and p['exit_reason'] == 'closed_externally'
    assert p['exit_price'] == 68.0 and any('now APPLIED' in x for x in rig.sent)
    rig.broker.hold_fills = False
    for i in range(0, 120, 3):
        rig.go(T(MON, '11:01:00') + timedelta(seconds=i))
    assert rig.broker.true_net(CE_NEO) == 0 and len(sells(rig)) == 1   # never a second SELL


def test_r3_03b_not_filled_with_a_live_buy_cancels_it_first_then_books(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    assert state(rig)['live']['state'] == ENTRY_OPEN
    with pytest.raises(ValueError, match='cancel requested'):
        rig.engine.book_external(1, T(MON, '10:00:01'), None)
    assert state(rig)['status'] == 'pending' and rig.broker.orders[0]['ordSt'] == 'cancelled'
    rig.go(T(MON, '10:00:02'))
    p = state(rig)
    assert p['status'] == 'closed' and p['never_filled'] and len(buys(rig)) == 1


def test_r3_03c_a_fill_read_during_the_write_off_wins(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    rig.broker.hold_fills = False                           # it fills just as the owner acts
    with pytest.raises(ValueError, match='sale price is required'):
        rig.engine.book_external(1, T(MON, '10:00:01'), None)
    p = state(rig)
    assert p['status'] == 'open' and p['qty'] == LOT and len(buys(rig)) == 1   # booked, never written off
    assert rig.broker.orders[0]['ordSt'] == 'complete'


def test_r3_15_non_finite_or_absurd_prices_are_refused_and_the_kill_fails_closed(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    for bad in (float('nan'), float('inf'), -1.0, 0.0, 1e9):
        with pytest.raises(ValueError):
            rig.engine.book_external(1, T(MON, '11:00:00'), bad)
    assert state(rig)['status'] == 'open'
    rig.engine.book_external(1, T(MON, '11:00:00'), 160.0)
    assert state(rig)['status'] == 'closed'
    state(rig)['net_pnl'] = float('nan')                   # a poisoned book
    rig.go(T(MON, '11:01:00'))
    k = rig.store.kill()
    assert k['tripped'] and k['loud'] and 'NON-FINITE' in k['reason']


# ---------------------------------------------------------------- R3-04
def _pending_filled_overnight(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    rig.broker.hold_fills = False
    rig.broker.order_book()                               # filled, overnight, unseen
    rig.broker.carry_forward_rows = False
    default_premiums(market, '2026-11-03')
    rig.market.set(CE_MON, 40.0, bid=39.8, ask=40.2)
    rig.restart()
    rig.start(T(TUE, '09:00:00'))
    rig.go(T(TUE, '09:15:30'))
    assert state(rig)['status'] == 'pending'
    assert any('live-confirm-held 1 --yes' in s for s in rig.sent)
    return rig


def test_r3_04_confirm_held_opens_a_pending_record_whose_entry_filled_overnight(cfg, tmp_path, market):
    rig = _pending_filled_overnight(cfg, tmp_path, market)
    (rig.dir / 'CONFIRM_HELD_1').write_text(stamped(1, CE_MON, TUE))
    rig.go(T(TUE, '09:16:00'))
    res = json.loads((rig.dir / 'CONFIRM_HELD_1.result').read_text())
    assert res['applied'] is True
    p = state(rig)
    assert p['status'] == 'open' and p['entry_price'] == 153.8 and p['entry_date'] == MON
    rig.go(T(TUE, '09:16:03'))
    rig.go(T(TUE, '09:16:06'))
    p = state(rig)
    assert sells(rig) and p['status'] == 'closed' and rig.broker.true_net(CE_NEO) == 0
    assert p['net_pnl'] < 0 and p in rig.store.closed_positions()


def test_r3_04b_book_external_on_a_pending_record_books_a_real_round_trip(cfg, tmp_path, market):
    rig = _pending_filled_overnight(cfg, tmp_path, market)
    rig.engine.book_external(1, T(TUE, '09:40:00'), 39.0)
    p = state(rig)
    assert p['status'] == 'closed' and p['exit_reason'] == 'closed_externally'
    assert not p.get('never_filled') and p['entry_price'] == 153.8
    assert p['live']['entry_price_estimated'] and p['net_pnl'] < -7000
    assert p in rig.store.closed_positions()
    rig.engine._update_kill(T(TUE, '09:40:01'))
    assert rig.store.kill()['equity'] < -7000
    assert any(ln['ev'] == 'BOUND' for ln in rig.journal.lines())


# ---------------------------------------------------------------- R3-05 / R3-20
def test_r3_05_reconcile_inside_the_positions_lag_after_an_exit_does_not_trip(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.positions_lag_sec = 30.0
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    rig.go(T(MON, '11:00:01'))
    assert state(rig)['status'] == 'closed'

    def reconcile(t):
        rig.clock['now'] = T(MON, t)                           # the fake reads the rig clock
        rig.engine._reconcile(T(MON, t), startup=False)
    reconcile('11:00:10')                                      # the row still shows the lot
    reconcile('11:00:20')
    assert not rig.store.kill()['tripped'] and rig.engine.reconcile_ok
    reconcile('11:05:10')                                      # after the lag: the row reads 0
    assert not rig.store.kill()['tripped'] and rig.engine.reconcile_ok
    rig.broker.set_external_position(CE_NEO, 2 * LOT)          # a GENUINE extra lot, later
    reconcile('11:06:00')
    assert not rig.store.kill()['tripped'] and not rig.engine.reconcile_ok
    assert any('READ ONCE' in x for x in rig.sent)
    reconcile('11:06:03')
    assert rig.store.kill()['tripped'] and 'UNEXPLAINED LONG' in rig.store.kill()['reason']


def test_r3_20_a_row_showing_only_todays_sell_of_our_lot_is_not_a_short(cfg, tmp_path, market):
    rig, s = carried_no_row(cfg, tmp_path, market)
    n = neo_symbol(s)
    rig.engine.confirm_held(1, T(TUE, '09:10:00'))
    rig.go(T(TUE, '09:25:01'))
    rig.go(T(TUE, '09:25:02'))
    assert state(rig)['status'] == 'closed'
    rig.broker.positions = lambda: [PositionView.from_neo(
        {'trdSym': n, 'prod': 'NRML', 'flBuyQty': '0', 'flSellQty': str(LOT),
         'cfBuyQty': '0', 'cfSellQty': '0'})]
    for t in ('09:30:00', '09:36:00', '09:42:00'):
        rig.engine._reconcile(T(TUE, t), startup=False)
    assert not rig.store.kill()['tripped'] and rig.engine.reconcile_ok
    assert any('row carries no carried quantity' in x for x in rig.sent)


# ---------------------------------------------------------------- R3-06
class SlowNotifier(NullNotifier):
    """Telegram blackholed: each send burns 10 s on the calling thread."""
    def __init__(self, clock, cost=10.0):
        super().__init__()
        self.clock, self.cost = clock, cost

    def send(self, text):
        self.sent.append(text)
        self.clock['now'] += timedelta(seconds=self.cost)
        return False


def test_r3_06_telegram_posts_from_a_worker_and_never_delays_the_caller(tmp_path, monkeypatch):
    tg = tmp_path / 'tg.json'
    tg.write_text(json.dumps({'bot_token': 'T', 'chat_id': '1'}))
    gate = threading.Event()
    posted = []

    class Resp:
        status_code = 200
        text = 'ok'

    def slow_post(url, data=None, timeout=None):
        gate.wait(10)
        posted.append(data['text'])
        return Resp()
    import requests
    monkeypatch.setattr(requests, 'post', slow_post)
    n = AsyncNotifier(tg, True, 'X')
    total = QUEUE_MAX + 5
    t0 = _time.monotonic()
    for i in range(total):
        n.send('m%d' % i)
    assert _time.monotonic() - t0 < 1.0 and len(n.sent) == total
    assert 4 <= n.dropped <= 5 and n.pending <= QUEUE_MAX
    gate.set()
    deadline = _time.monotonic() + 10
    while n.posted + n.dropped < total and _time.monotonic() < deadline:
        _time.sleep(0.02)
    assert n.posted + n.dropped == total and n.failed == 0
    assert posted[-1] == 'X\nm%d' % (total - 1)               # newest kept, in order
    idx = [int(t.split('m')[1]) for t in posted]
    assert idx == sorted(idx)


def test_r3_06_live_uses_the_worker_notifier_with_its_own_switch(cfg):
    neo = build_neo(neo_raw())
    n = live_cli._notifier(cfg, 'live', neo)
    assert isinstance(n, AsyncNotifier) and n.prefix == neo.telegram_prefix_live
    neo_off = build_neo(neo_raw(telegram_enabled=False))
    assert live_cli._notifier(cfg, 'rehearsal', neo_off).enabled is False


def test_r3_06b_the_sell_goes_out_before_the_exit_due_alert(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    n = SlowNotifier(rig.clock)
    rig.engine.notify = n
    sent_at = []
    orig = rig.broker.place

    def place(req):
        sent_at.append((rig.clock['now'], req.price))
        return orig(req)
    rig.broker.place = place
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    t0 = T(MON, '11:00:00')
    rig.clock['now'] = t0
    market.today = t0.date()
    rig.engine.step(t0)
    assert sent_at and sent_at[0][0] == t0 and sent_at[0][1] == '69.80'
    assert any('EXIT DUE' in s for s in n.sent)


# ---------------------------------------------------------------- R3-07 / R3-19
def test_r3_07_paper_overlay_cannot_silence_live_or_move_its_rules(wired, tmp_path, monkeypatch):  # noqa: F811 - fixture from test_live_cli
    """R3-07 as re-decided by R4-04: live reads the tracked defaults plus its
    OWN overlay, never the paper overlay; a difference from paper is
    reported as information (pairing broken) and blocks nothing."""
    ov = tmp_path / 'paper.json'
    ov.write_text(json.dumps({'telegram_enabled': False, 'stop_frac': 0.7,
                              'next_day_exit': '10:00:00'}))
    (tmp_path / 'tg.json').write_text(json.dumps({'bot_token': 'T', 'chat_id': '1'}))
    monkeypatch.setattr(config_mod, 'OVERLAY_FILE', ov)
    cfg, execution, neo = live_cli._load()
    assert cfg.stop_frac == 0.5 and str(cfg.next_day_exit) == '09:25:00'   # paper's edit: not live's
    n = live_cli._notifier(cfg, 'live', neo)
    assert n.enabled is True and neo.telegram_enabled  # live has its OWN switch
    paper = live_cli._paper_cfg()
    diffs = rule_diffs(cfg, paper)
    assert any(d.startswith('stop_frac=') for d in diffs)
    assert any(d.startswith('next_day_exit=') for d in diffs)
    today = live_cli.now_ist().date()
    wired['live'].mkdir(parents=True, exist_ok=True)
    (wired['live'] / arming.ARM_FILE).write_text(today.isoformat())
    monkeypatch.setattr(arming, 'gate_verdict', lambda neo_, pos: ('PASS', 'gate: PASS (stub)'))
    rep = arming.check(cfg, 'live', neo, wired['live'], today, [], paper_cfg=paper)
    assert rep.rule_drift and 'RULES DIFFER FROM PAPER' in rep.text()
    assert rep.may_run and rep.armed                  # information only: nothing blocked
    assert not hasattr(arming, 'make_arm_reader_with_rules')


def test_r3_19_unknown_neo_keys_are_named(wired):  # noqa: F811 - fixture from test_live_cli
    neo = build_neo(neo_raw(carry_forward_row='absent'))
    assert neo.unknown_keys == ('carry_forward_row',)
    cfg = live_cli._paper_cfg()
    rep = arming.check(cfg, 'rehearsal', neo, wired['live'], live_cli.now_ist().date(), [])
    assert 'carry_forward_row' in rep.text() and 'UNKNOWN' in rep.text()


# ---------------------------------------------------------------- R3-08
def test_r3_08_an_unserviced_loop_retries_the_credentials_each_minute_and_starts_when_fixed(
        wired, monkeypatch, caplog):  # noqa: F811 - fixture from test_live_cli
    caplog.set_level(logging.INFO)
    real = live_cli.build_broker
    calls = {'n': 0}

    def flaky(execution, neo, sdir, clock):
        calls['n'] += 1
        if calls['n'] < 3:
            raise nc.CredentialsError('mpin missing')
        return real(execution, neo, sdir, clock)
    monkeypatch.setattr(live_cli, 'build_broker', flaky)
    _real_record(wired['live'])
    _set_execution(wired, 'live')
    # 15:30 start: the same two retry minutes, then a short loop to the close
    # (a 09:20 start is 22,800 simulated ticks and ~2 minutes of real CPU)
    _advancing_clock(wired, monkeypatch, T('2026-10-26', '15:30:00'))
    assert cli.main(['live-run']) == 0
    alerts = [r for r in caplog.records if 'UNSERVICED' in r.message]
    assert calls['n'] == 3 and len(alerts) == 2
    assert all('NOT being read' in r.message for r in alerts)
    assert len([r for r in caplog.records if 'cleared on re-read' in r.message]) == 2


def test_r3_08b_an_unusable_live_config_is_re_read_each_minute_too(wired, monkeypatch, caplog):  # noqa: F811 - fixture from test_live_cli
    caplog.set_level(logging.INFO)
    real = live_cli._load
    calls = {'n': 0}

    def flaky():
        calls['n'] += 1
        return None if calls['n'] < 3 else real()
    monkeypatch.setattr(live_cli, '_load', flaky)
    _real_record(wired['live'])
    _set_execution(wired, 'live')
    _advancing_clock(wired, monkeypatch, T('2026-10-26', '15:30:00'))   # see test_r3_08
    assert cli.main(['live-run']) == 0
    alerts = [r for r in caplog.records if 'UNSERVICED' in r.message and 'config unusable' in r.message]
    assert calls['n'] == 3 and len(alerts) == 2
    assert any('cleared on re-read' in r.message for r in caplog.records)


# ---------------------------------------------------------------- R3-09
def test_r3_09_wrong_ip_and_throttle_are_definite_refusals_retried_at_the_next_poll(cfg, tmp_path, market):
    http = FakeHTTP()
    t = nc.NeoTrader(CREDS, 'https://mis.example', 5, 5, session=http, totp_fn=lambda s: '0')
    login_ok(http)
    t.login()
    for body, st in (({'stat': 'Not_Ok', 'emsg': 'unauthorized', 'stCode': 100008}, 200),
                     ({'stat': 'Not_Ok', 'emsg': 'rate limited', 'stCode': 429}, 429)):
        http.script[nc.PATH_PLACE] = R(st, body)
        with pytest.raises(nc.NeoRejected):              # NOT placed, definitely
            t.place(symbol='s', token='1', side='S', qty=65, price='1.00', tag='t',
                    segment='nse_fo', product='NRML')
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.place_faults = [('reject', '100008', 'unauthorized'), ('reject', '429', 'slow down')]
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    t0 = T(MON, '11:00:00')
    rig.go(t0)
    assert len(sells(rig)) == 1 and state(rig)['status'] == 'open'
    assert rig.engine._rejection_streak == 0
    for i in range(1, 12):
        rig.go(t0 + timedelta(seconds=i))
    assert len(sells(rig)) >= 3 and state(rig)['status'] == 'closed'
    assert rig.broker.true_net(CE_NEO) == 0
    assert any('REFUSED by the gateway' in s and '100008' in s for s in rig.sent)


# ---------------------------------------------------------------- R3-10
def test_r3_10_a_holiday_the_calendar_misses_does_not_latch_and_the_next_day_sells_at_0925(
        cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    s = sym('2026-11-03', 22850, 'CE')
    holding(rig, symbol=s, expiry='2026-11-03')
    default_premiums(market, '2026-11-03')
    rig.restart()                                     # the calendar says session
    rig.start(T(TUE, '09:00:00'))
    market.stale = set(market.books)                  # nothing trades: an unlisted holiday
    rig.go(T(TUE, '09:20:01'))
    rig.go(T(TUE, '09:25:01'))
    assert state(rig)['live'].get('exit_reason') is None and not sells(rig)
    market.stale = set()
    market.set(s, 210.0, bid=209.5, ask=210.5)
    rig.restart()
    rig.start(T(WED, '09:00:00'))
    rig.go(T(WED, '09:15:05'))
    assert not sells(rig)                             # the stop watch, never the exit
    rig.go(T(WED, '09:25:01'))
    assert sells(rig) and state(rig)['live']['exit_reason'] == 'next_day_0925'


def test_r3_10b_another_polled_book_trading_today_proves_the_day(cfg, tmp_path, market):
    """CE's own book is stale and the mark has no candle; PE's book traded:
    the day is shown trading, so CE's 09:25 exit latches (deferred on its
    dead book) instead of waiting for proof that already exists."""
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    pe = sym('2026-11-03', 22700, 'PE')
    holding(rig, symbol=pe, side='PE', strike=22700.0, expiry='2026-11-03')
    default_premiums(market, '2026-11-03')
    rig.market.set(CE_MON, 170.0, bid=169.8, ask=170.2)
    rig.market.set(pe, 120.0, bid=119.8, ask=120.2)
    rig.market.stale = {CE_MON}
    rig.go(T(TUE, '09:25:01'))
    assert rig.engine._proven_trading_day == TUE
    assert state(rig, 2)['live']['exit_reason'] == 'next_day_0925'
    assert state(rig)['live']['exit_reason'] == 'next_day_0925' and state(rig)['exit_defers'] == 1
    assert not any('nothing has traded today' in s for s in rig.sent)


def test_r3_10c_the_outage_proof_read_is_throttled_to_once_a_minute(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    default_premiums(market, '2026-11-03')
    rig.restart()
    rig.start(T(TUE, '09:00:00'))
    market.fail = True
    calls = {'n': 0}
    real = rig.broker.order_book

    def counted():
        calls['n'] += 1
        return real()
    rig.broker.order_book = counted
    base = calls['n']
    for i in range(0, 180):
        rig.go(T(TUE, '09:26:00') + timedelta(seconds=i))       # 180 one-second ticks, quotes out
    extra = calls['n'] - base
    assert extra <= 12, extra          # the service tick's own reads plus <= 3 proof reads
    assert state(rig)['live'].get('exit_reason') is None
    rig.broker.place(OrderRequest(symbol='OWNERS-OWN', token='9', side='B', qty=LOT,
                                  price='1.00', tag='APP'))
    for i in range(180, 260):
        rig.go(T(TUE, '09:26:00') + timedelta(seconds=i))
    assert state(rig)['live']['exit_reason'] == 'next_day_late'   # proven within the minute


# ---------------------------------------------------------------- R3-11
def test_r3_11_an_order_book_quantity_in_the_wrong_unit_is_never_booked_or_sold(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    o = rig.broker.orders[0]
    o.update({'ordSt': 'complete', 'fldQty': '1', 'avgPrc': '153.80',
              '_filled_at': T(MON, '10:00:00').isoformat()})
    rig.broker._net = lambda lagged=True: {CE_NEO: {'fl': LOT, 'cf': 0}}
    rig.go(T(MON, '10:00:01'))
    p, k = state(rig), rig.store.kill()
    assert p['status'] == 'pending' and p['qty'] == LOT
    assert k['tripped'] and k['loud'] and 'ORDER QUANTITY UNIT' in k['reason']
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    for i in range(5, 120, 5):
        rig.go(T(MON, '10:00:00') + timedelta(seconds=i))
    assert not sells(rig) and len(buys(rig)) == 1 and state(rig)['status'] == 'pending'
    assert any('QUARANTINED' in s for s in rig.sent)


# ---------------------------------------------------------------- R3-12 / R3-34 / R3-35
def test_r3_12_a_garbage_download_is_not_cached(tmp_path):
    class Rd(FakeReader):
        n = 0

        def fetch_text(self, url):
            Rd.n += 1
            return '<html>gateway error</html>\n' if Rd.n == 1 else super().fetch_text(url)
    neo = build_neo(neo_raw())
    rd = Rd()
    rd.login()
    load = live_cli.make_scrip_loader(rd, neo, tmp_path, date(2026, 10, 26), 'NIFTY')
    with pytest.raises(Exception):
        load()
    assert Rd.n == 1 and not list(tmp_path.glob('scrip_master_nse_fo_*.csv'))
    assert len(load()) == 2 and Rd.n == 2             # downloaded again, then cached
    assert list(tmp_path.glob('scrip_master_nse_fo_*.csv'))


def test_r3_34_the_scrip_worker_never_logs_in(tmp_path):
    class NoLogin(FakeReader):
        def login(self):
            raise AssertionError('the worker logged in')
    neo = build_neo(neo_raw())
    with pytest.raises(nc.NeoUnavailable, match='the worker waits'):
        live_cli.make_scrip_loader(NoLogin(), neo, tmp_path, date(2026, 10, 26), 'NIFTY')()


def test_r3_35_a_restart_after_the_open_loads_scrips_in_the_worker(cfg, tmp_path, market):
    default_premiums(market, '2026-10-27')
    rig = Rig(cfg, tmp_path, market)
    rig.start(T(MON, '10:00:00'))
    assert rig.engine._scrips is None and rig.engine._scrips_thread is not None
    rig.engine._scrips_thread.join(5)
    rig.engine._scrips_async_step(T(MON, '10:00:01'))
    assert rig.engine._scrips is not None
    rig2 = Rig(cfg, tmp_path / 'b', market)
    rig2.start(T(MON, '09:00:00'))
    assert rig2.engine._scrips is not None            # before the open a blocking load is free


# ---------------------------------------------------------------- R3-13 / R3-14 / R3-17 / R3-27 / R3-28 / R3-30
def test_r3_13_rerunning_the_sell_step_inside_the_positions_lag_refuses(tmp_path):
    r = ProbeRig(tmp_path, fill_delay=0.0)
    r.fb.set_external_position(NEO, LOT, day=date(2026, 10, 23))
    r.fb.positions_lag_sec = 120.0
    assert r.run(step='sell') == 1
    assert any('do NOT re-run' in x for x in r.out) and r.fb.true_net(NEO) == 0
    r.clock['now'] += timedelta(seconds=30)
    r2 = OrderProbe(r.fb, jmod.Journal(tmp_path), lambda: r.clock['now'], r.sleep, r.out.append)
    r2.run(SCRIPS, KITE_ROW, r.quote, 500, step='sell')
    assert len(r.fb.sent) == 1 and r.fb.true_net(NEO) == 0
    assert any('already exists today' in x for x in r.out)


def test_r3_14_an_unclear_buy_send_stops_the_run_and_reports_held(tmp_path):
    r = ProbeRig(tmp_path, fill_delay=10000.0)
    r.fb.hold_fills = True
    r.fb.visibility_lag_sec = 300.0
    r.fb.place_faults = ['timeout_reached']           # the BUY is AT the exchange
    with pytest.raises(ProbeRefused):
        r.run()
    assert not any('probe complete, flat' in s for s in r.out)
    assert any('RUN STOPPED' in s and 'HELD BY THE BOOK RECORD' in s for s in r.out)
    assert r.probe.held == LOT and r.fb.orders[0]['ordSt'] == 'open'


def test_r3_17_the_probe_reads_holdings_regardless_of_the_engine_flag():
    class Tr:
        ready = True
        calls = 0

        def holdings(self):
            Tr.calls += 1
            return [{'trdSym': 'X', 'quantity': '65'}]
    b = NeoBroker(Tr(), 'nse_fo', 'NRML', 'Net')
    assert b.holdings() is None and Tr.calls == 0
    h = b.holdings(force=True)
    assert h is not None and h[0].symbol == 'X' and h[0].net_qty == 65 and Tr.calls == 1


def test_r3_27_only_a_filled_probe_sell_ends_the_hold(tmp_path):
    j = jmod.Journal(tmp_path)
    j.probe_held(T(MON, '10:00:00'), NEO, LOT)
    j.intent(T(TUE, '09:20:00'), '20261027-0-EXIT-1', 'EXIT', 0, NEO, 'S', LOT, '1.00', probe=True)
    j.placed(T(TUE, '09:20:00'), '20261027-0-EXIT-1', '111')     # later cancelled unfilled
    assert j.probe_hold_open(NEO)
    j.probe_sold(T(TUE, '09:21:00'), NEO, LOT)
    assert not j.probe_hold_open(NEO) and NEO not in j.probe_open_symbols()


def test_r3_28_a_failed_positions_read_is_unknown_not_absent(tmp_path):
    r = ProbeRig(tmp_path)
    r.fb.read_faults = ['unavailable']
    assert r.run(step='sell') == 1
    assert any('carry-forward row UNKNOWN' in s for s in r.out)
    assert not any('row ABSENT' in s for s in r.out) and not r.fb.sent


def test_r3_30_the_probe_envelope_is_redacted():
    from nifty_btst import neo_probe

    class Rd(FakeReader):
        def raw_report(self, path, jdata=None):
            if 'limits' in path:
                return {'stat': 'Ok', 'stCode': 200, 'Net': '1', 'usrId': 'REALUSER', 'data': {}}
            return super().raw_report(path, jdata)
    out = []
    neo_probe.run_probe(Rd(), 'NIFTY', date(2026, 10, 26), out=out.append)
    assert out and not any('REALUSER' in s for s in out)


# ---------------------------------------------------------------- R3-16
def test_r3_16_every_owner_command_addresses_the_live_book_when_live_records_exist(
        wired, monkeypatch, tmp_path, capsys):  # noqa: F811 - fixture from test_live_cli
    monkeypatch.delenv('BTST_LIVE_STATE_DIR')
    monkeypatch.setattr(config_mod, 'APP_DIR', tmp_path)
    _real_record(tmp_path / 'state_live')
    s = LiveStore(tmp_path / 'state_live')
    s.kill().update({'tripped': True, 'reason': 'SHORT at the broker', 'loud': True})
    s.save(force=True)
    _set_execution(wired, 'rehearsal')
    assert cli.main(['live-status']) == 0
    out = capsys.readouterr().out
    assert 'REALLONG' in out and 'SHORT' in out and 'addressing the LIVE book' in out
    assert cli.main(['live-report']) == 0 and 'LIVE book' in capsys.readouterr().out
    assert cli.main(['live-resume', '--yes']) == 0
    out = capsys.readouterr().out
    assert 'APPLIED' in out and 'KILL CLEARED' in out
    assert not LiveStore(tmp_path / 'state_live').kill()['tripped']


# ---------------------------------------------------------------- R3-21
def test_r3_21_a_placeholder_row_of_another_product_is_skipped_but_ours_raises():
    class Tr:
        ready = True
        rows = [{'trdSym': 'GOOD', 'prod': 'NRML', 'flBuyQty': '65', 'cfBuyQty': '0',
                 'flSellQty': '0', 'cfSellQty': '0'},
                {'trdSym': 'OTHER', 'prod': 'CNC', 'flBuyQty': '--', 'cfBuyQty': '--'}]

        def positions(self):
            return self.rows
    tr = Tr()
    b = NeoBroker(tr, 'nse_fo', 'NRML', 'Net')
    row = b.position_row('GOOD', 'NRML')
    assert row is not None and row.net_qty == 65
    tr.rows = [{'trdSym': 'OURS', 'prod': 'NRML', 'flBuyQty': '--', 'cfBuyQty': '--'}]
    with pytest.raises(BrokerUnavailable):
        b.position_row('OURS', 'NRML')


# ---------------------------------------------------------------- R3-24 / R3-25 / R3-26
def test_r3_24_a_trigger_taken_while_blocked_is_abandoned_not_bought_later(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market, armed=False)
    trigger_ce(rig, when=T(MON, '10:00:00'))
    p = state(rig)
    assert p['status'] == 'closed' and p['never_filled'] and not buys(rig)
    assert any('abandoned now, never bought later' in s for s in rig.sent)
    rig.engine._arm_reader = lambda d: date(2099, 1, 1)
    rig.engine._arm_cache = (None, None)
    rig.market.set(CE_MON, 152.0, bid=151.9, ask=152.1)
    for i in range(0, 60, 3):
        rig.go(T(MON, '14:30:00') + timedelta(seconds=i))
    assert not buys(rig)


def test_r3_25_a_confirmed_lot_is_known_to_reconcile(cfg, tmp_path, market):
    rig, s = carried_no_row(cfg, tmp_path, market)
    rig.engine.confirm_held(1, T(TUE, '09:10:00'))
    rig.notifier.sent.clear()
    rig.engine._reconcile(T(TUE, '09:15:05'), startup=False)
    assert rig.engine.reconcile_ok
    assert not any('NO POSITION ROW' in x for x in rig.sent)


def test_r3_26_the_login_alert_counts_a_pending_record(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    rig.engine.session_ok = False
    rig.engine._session_try_at = None
    rig.broker.login_fail = True
    rig.engine.service_orders(T(MON, '10:05:00'))
    assert any('UNPROTECTED' in s and '#1' in s and 'pending' in s for s in rig.sent)
    assert not any('No position.' in s for s in rig.sent)


# ---------------------------------------------------------------- R3-31 / R3-32 / R3-33
class _LockRec:
    log = []
    free = True

    def __init__(self, path):
        self.path = path

    def acquire(self):
        _LockRec.log.append('acquire')
        return _LockRec.free

    def release(self):
        _LockRec.log.append('release')


def test_r3_31_both_probes_hold_a_named_lock_for_the_run(wired, monkeypatch, capsys):  # noqa: F811 - fixture from test_live_cli
    monkeypatch.setattr(live_cli, '_Lock', _LockRec)
    monkeypatch.setattr(live_cli, '_kite_row', lambda cfg, today, symbol, cache: (None, None))
    monkeypatch.setattr(live_cli, '_reader', lambda neo, trader: object())
    monkeypatch.setattr(live_cli, 'pin_ipv4', lambda: None)
    monkeypatch.setattr(live_cli, 'run_probe',
                        lambda *a, **k: (_LockRec.log.append('probe'), 0)[1])
    _LockRec.log, _LockRec.free = [], False
    assert cli.main(['neo-probe']) == live_cli.EXIT_REFUSED       # another run holds it
    assert 'holds' in capsys.readouterr().out and _LockRec.log == ['acquire']
    _LockRec.log, _LockRec.free = [], True
    assert cli.main(['neo-probe']) == 0
    assert _LockRec.log == ['acquire', 'probe', 'release']
    # the order probe: a NAMED lock, released after the run
    _set_execution(wired, 'live')
    wired['live'].mkdir(parents=True, exist_ok=True)
    today = live_cli.now_ist().date()
    (wired['live'] / arming.PROBE_ARM_FILE).write_text(today.isoformat())
    wired['clock']['now'] = T(today.isoformat(), '10:00:00')
    monkeypatch.setattr(live_cli, '_order_probe_locked',
                        lambda *a, **k: (_LockRec.log.append('probe'), 0)[1])
    _LockRec.log, _LockRec.free = [], False
    assert cli.main(['neo-order-probe', '--symbol', 'X', '--confirm-real-order']) == live_cli.EXIT_REFUSED
    assert _LockRec.log == ['acquire']
    _LockRec.log, _LockRec.free = [], True
    assert cli.main(['neo-order-probe', '--symbol', 'X', '--confirm-real-order']) == 0
    assert _LockRec.log == ['acquire', 'probe', 'release']


def test_r3_32_live_run_releases_its_lock_even_when_the_session_raises(wired, monkeypatch):  # noqa: F811 - fixture from test_live_cli
    monkeypatch.setattr(live_cli, '_Lock', _LockRec)

    def boom(*a, **k):
        raise RuntimeError('session crashed')
    monkeypatch.setattr(live_cli, '_live_session', boom)
    wired['clock']['now'] = T('2026-10-26', '10:00:00')
    _LockRec.log, _LockRec.free = [], True
    assert cli.main(['live-run', '--once']) == 1
    assert _LockRec.log == ['acquire', 'release']
    _LockRec.log = []
    assert cli.main(['live-digest']) == 0                    # digest releases too
    assert _LockRec.log == ['acquire', 'release']


def test_r3_33_a_corrupt_live_book_alerts_at_every_tick_while_records_may_exist(wired, caplog):  # noqa: F811 - fixture from test_live_cli
    caplog.set_level(logging.INFO)
    wired['live'].mkdir(parents=True, exist_ok=True)
    (wired['live'] / 'book.json').write_text('{garbage')
    _set_execution(wired, 'live')
    wired['clock']['now'] = T('2026-10-26', '10:00:00')
    for _ in range(3):
        assert cli.main(['live-run', '--once']) == 3
    assert len([r for r in caplog.records if 'LIVE BOOK CORRUPT' in r.message]) == 3


# ---------------------------------------------------------------- R3-37
def _diff(a, b, path=''):
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += _diff(a.get(k), b.get(k), path + '/' + str(k))
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += _diff(x, y, path + '[%d]' % i)
        return out
    return [] if a == b else ['%s: %r -> %r' % (path, a, b)]


def test_r3_37_the_book_is_written_only_when_it_changes(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.market.set(CE_MON, 150.0, bid=149.9, ask=150.1)
    rig.go(T(MON, '11:00:00'))
    rig.go(T(MON, '11:00:03'))
    saves0 = rig.store.saves
    snap = json.loads(json.dumps(rig.store.data, default=str))
    for i in range(6, 60, 3):
        rig.go(T(MON, '11:00:00') + timedelta(seconds=i))
    changes = _diff(snap, json.loads(json.dumps(rig.store.data, default=str)))
    assert rig.store.saves - saves0 <= 1, changes
    assert rig.store.skipped_saves >= 10
    mtime = (rig.dir / 'book.json').stat().st_mtime
    for i in range(60, 90, 3):
        rig.go(T(MON, '11:00:00') + timedelta(seconds=i))
    assert (rig.dir / 'book.json').stat().st_mtime == mtime


# ---------------------------------------------------------------- clean checks
def test_clean_exits_only_never_buys_even_armed_with_pass(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.engine.entries_enabled = False
    trigger_ce(rig)
    for i in range(0, 300, 3):
        rig.go(T(MON, '10:00:01') + timedelta(seconds=i))
    assert not buys(rig) and state(rig)['status'] == 'pending'


def test_clean_confirmation_expires_tonight_and_is_per_id(cfg, tmp_path, market):
    rig, s = carried_no_row(cfg, tmp_path, market)
    rig.engine.confirm_held(1, T(TUE, '09:10:00'))
    assert rig.store.owner_confirmed_held(1, T(TUE, '09:10:00').date())
    assert not rig.store.owner_confirmed_held(1, T(WED, '09:10:00').date())
    assert not rig.store.owner_confirmed_held(2, T(TUE, '09:10:00').date())


def test_clean_missing_row_no_confirmation_alerts_before_the_stop(cfg, tmp_path, market):
    rig, s = carried_no_row(cfg, tmp_path, market)
    assert any('NO POSITION ROW' in x for x in rig.sent)
    rig.market.set(s, 40.0, bid=39.8, ask=40.2)
    rig.go(T(TUE, '09:15:05'))
    assert any('EXIT DUE' in x for x in rig.sent) and not sells(rig)
    rig.go(T(TUE, '09:16:06'))
    assert any('SELL REFUSED' in x and 'live-confirm-held' in x for x in rig.sent)
