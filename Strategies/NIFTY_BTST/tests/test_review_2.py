"""One regression test per finding of docs/NEO_REVIEW_2.md, ported from the
reviewer's reproductions with the assertion flipped to the correct
behaviour, plus the four review-1 survivors (NEO-01 corroboration, NEO-12
persisted fake, NEO-22 live poll key, NEO-35 login log) pinned.

Findings whose regression lives elsewhere:
  R2-06/14/15/17 -> tests/test_probe_safety.py (`test_r2_*` below cover the lag)
  R2-13/22       -> below and tests/test_neo_client.py
  R2-24/25       -> below
"""
import json
import logging
from datetime import date, timedelta

import pytest

from conftest import LOT, T, default_premiums, sym
from live_helpers import MON, TUE, WED, SCRIPS, Rig, neo_raw, neo_symbol
from nifty_btst import cli, config as config_mod, live_cli, neo_client as nc, neo_probe
from nifty_btst.broker import (BrokerUnavailable, FakeBroker, NeoBroker, OrderRequest,
                               PositionView, RowError)
from nifty_btst.live_config import build_neo
from nifty_btst.live_engine import ENTRY_OPEN, EXIT_DUE, EXIT_OPEN, MAX_CANCEL_ATTEMPTS, LiveStore
from nifty_btst.pricing import Book
from test_live_cli import _book, _set_execution, wired  # noqa: F401 - fixture
from test_live_engine import CE_MON, CE_NEO, PE_MON, holding, marked, state, trigger_ce
from test_neo_client import CREDS, FakeHTTP, R, login_ok
from test_probe_safety import KITE_ROW, NEO, ProbeRig


def sells(rig):
    return [r for r in rig.broker.sent if r.side == 'S']


def buys(rig):
    return [r for r in rig.broker.sent if r.side == 'B']


# ------------------------------------------------------------------ R2-02
def test_r2_02_required_mode_row_absent_never_adopts_a_sell(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '15:39:58'))                      # stop fires seconds before the loop ends
    assert state(rig)['live']['state'] == EXIT_OPEN
    rig.broker.carry_forward_rows = False           # REST doc: only positions with trades today
    default_premiums(market, '2026-11-03')
    rig.market.set(CE_MON, 170.0, bid=169.8, ask=170.2)
    rig.restart()
    rig.start(T(TUE, '09:00:00'))
    for hms in ('09:15:05', '09:25:05', '09:26:10'):
        rig.go(T(TUE, hms))
    p = state(rig)
    assert p['status'] == 'open' and p['live']['state'] == EXIT_OPEN       # held, UNKNOWN
    assert rig.broker.true_net(CE_NEO) == LOT and len(sells(rig)) == 1
    assert any('nothing positive settles it' in s and 'live-confirm-held' in s for s in rig.sent)
    assert not rig.store.kill()['tripped']
    # the owner checks the app and confirms: the leg is released by positive
    # evidence only when the row appears; here the row comes back
    rig.broker.carry_forward_rows = True
    rig.broker.hold_fills = False
    rig.go(T(TUE, '09:27:00'))                      # the row proves the lot: the dead leg clears
    p = state(rig)
    assert p['live']['state'] in (EXIT_DUE, EXIT_OPEN) or p['status'] == 'closed'
    rig.go(T(TUE, '09:27:03'))
    assert state(rig)['status'] == 'closed' and rig.broker.true_net(CE_NEO) == 0


def test_r2_02_one_empty_read_at_the_open_does_not_book_a_sale(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '15:39:58'))
    rig.broker.carry_forward_rows = False
    default_premiums(market, '2026-11-03')
    rig.market.set(CE_MON, 170.0, bid=169.8, ask=170.2)
    rig.restart()
    rig.start(T(TUE, '09:15:01'))
    rig.service(T(TUE, '09:15:02'))                 # the one read while the row is not yet there
    assert state(rig)['status'] == 'open' and rig.broker.true_net(CE_NEO) == LOT
    rig.broker.carry_forward_rows = True            # it arrives a second later
    rig.broker.hold_fills = False
    rig.go(T(TUE, '09:25:05'))
    rig.go(T(TUE, '09:25:08'))
    assert state(rig)['status'] == 'closed' and rig.broker.true_net(CE_NEO) == 0


# ------------------------------------------------------------------ R2-03
def test_r2_03_entry_filled_overnight_no_row_is_unknown_not_abandoned(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    rig.broker.hold_fills = False
    rig.broker.order_book()                          # it fills while nobody looks
    assert rig.broker.true_net(CE_NEO) == LOT
    rig.broker.carry_forward_rows = False
    default_premiums(market, '2026-11-03')
    rig.market.set(CE_MON, 170.0, bid=169.8, ask=170.2)
    rig.restart()
    rig.start(T(TUE, '09:00:00'))
    rig.go(T(TUE, '09:15:30'))
    rig.go(T(TUE, '09:25:30'))
    p = state(rig)
    assert p['status'] == 'pending' and p['live']['state'] == ENTRY_OPEN  # never abandoned
    assert any('live-confirm-held' in s for s in rig.sent)
    rig.broker.carry_forward_rows = True             # the row appears: adopted, then exited
    rig.go(T(TUE, '09:26:00'))
    assert state(rig)['status'] == 'open' and state(rig)['entry_date'] == MON
    rig.go(T(TUE, '09:26:03'))
    rig.go(T(TUE, '09:26:06'))
    assert state(rig)['status'] == 'closed' and rig.broker.true_net(CE_NEO) == 0


def test_r2_03_unexplained_long_of_our_family_is_a_loud_kill_probe_lot_exempt(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.set_external_position(neo_symbol(PE_MON), LOT)
    rig.engine._reconcile(T(MON, '11:00:00'), startup=False)
    assert not rig.store.kill()['tripped'] and not rig.engine.reconcile_ok   # R3-05: one read reports
    rig.engine._reconcile(T(MON, '11:00:03'), startup=False)
    k = rig.store.kill()
    assert k['tripped'] and k['loud'] and 'UNEXPLAINED LONG' in k['reason']
    rig2 = marked(cfg, tmp_path / 'b', market)
    rig2.broker.set_external_position(neo_symbol(PE_MON), LOT)
    rig2.journal.probe_held(T(MON, '10:00:00'), neo_symbol(PE_MON), LOT)    # the probe's lot
    rig2.engine._reconcile(T(MON, '11:00:00'), startup=False)
    assert not rig2.store.kill()['tripped'] and any('order probe' in s for s in rig2.sent)


# ------------------------------------------------------------------ R2-01
def test_r2_01_no_blind_sell_on_absence_after_an_unseen_sale(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '15:39:58'))
    rig.broker.hold_fills = False
    rig.broker.order_book()                          # fills after our last read
    assert rig.broker.true_net(CE_NEO) == 0
    rig.broker.carry_forward_rows = False            # flat overnight: no row, as in a held one
    default_premiums(market, '2026-11-03')
    rig.market.set(CE_MON, 170.0, bid=169.8, ask=170.2)
    rig.restart()
    rig.start(T(TUE, '09:00:00'))
    for hms in ('09:15:05', '09:15:08', '09:25:05'):
        rig.go(T(TUE, hms))
    assert len(sells(rig)) == 1 and rig.broker.true_net(CE_NEO) == 0


def test_r2_01_owner_sold_yesterday_in_the_app_no_row_never_goes_short(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.orders.append({'nOrdNo': '999', 'ordSt': 'complete', 'trdSym': CE_NEO, 'trnsTp': 'S',
                              'qty': str(LOT), 'fldQty': str(LOT), 'avgPrc': '171.00', 'prc': '171.00',
                              'rejRsn': '--', 'GuiOrdId': '', 'ordDtTm': '', 'prod': 'NRML',
                              '_placed': T(MON, '15:35:00').isoformat()})
    assert rig.broker.true_net(CE_NEO) == 0
    rig.broker.carry_forward_rows = False
    default_premiums(market, '2026-11-03')
    rig.market.set(CE_MON, 170.0, bid=169.8, ask=170.2)
    rig.restart()
    rig.start(T(TUE, '09:00:00'))
    for i in range(0, 75, 3):
        rig.go(T(TUE, '09:25:01') + timedelta(seconds=i))
    assert not sells(rig) and rig.broker.true_net(CE_NEO) == 0
    assert any('SELL REFUSED' in s for s in rig.sent)
    # the owner's confirmation is positive evidence, but it can never sell on
    # top of a SELL the broker already shows TODAY (one confirmation, one sell)
    rig.broker.orders.append({'nOrdNo': '998', 'ordSt': 'complete', 'trdSym': CE_NEO, 'trnsTp': 'S',
                              'qty': str(LOT), 'fldQty': str(LOT), 'avgPrc': '170.00', 'prc': '170.00',
                              'rejRsn': '--', 'GuiOrdId': '', 'ordDtTm': '', 'prod': 'NRML',
                              '_placed': T(TUE, '09:20:00').isoformat()})
    rig.engine.confirm_held(1, T(TUE, '09:27:00'))
    rig.go(T(TUE, '09:27:01'))
    rig.go(T(TUE, '09:28:02'))
    assert not sells(rig)
    # R4-02/03: the owner's own filled SELL (998) equals what this book held,
    # so it is the owner's sale, NOT a short: our SELL is refused naming it
    # and the way out is book-external; nothing is traded to "repair" it
    assert any('998' in s and 'did not send FILLED' in s for s in rig.sent)
    rig.clock['now'] = T(TUE, '09:31:00')
    rig.engine._reconcile(T(TUE, '09:31:00'), startup=False)
    assert not rig.store.kill()['tripped'] and any('Not a short' in s for s in rig.sent)
    rig.engine.book_external(1, T(TUE, '09:32:00'), 170.0)
    assert state(rig)['status'] == 'closed' and state(rig)['exit_reason'] == 'closed_externally'


# ------------------------------------------------------------------ R2-04
@pytest.mark.parametrize('mode', ['rehearsal', 'paper'])
def test_r2_04_flipping_execution_runs_the_real_book_exits_only(wired, monkeypatch, caplog, mode, tmp_path):  # noqa: F811 - fixture from test_live_cli
    caplog.set_level(logging.INFO)
    monkeypatch.delenv('BTST_LIVE_STATE_DIR')
    monkeypatch.setattr(config_mod, 'APP_DIR', tmp_path)
    s = LiveStore(tmp_path / 'state_live')
    s.add_position({'status': 'open', 'side': 'CE', 'symbol': 'REALLONG', 'token': 1, 'strike': 22850.0,
                    'expiry': '2026-10-27', 'qty': 65, 'lot_size': 65, 'entry_date': '2026-10-23',
                    'entry_price': 150.0, 'stop_level': 75.0, 'trigger': 150.0, 'exit_defers': 0,
                    'cls': 'other', 'dte_sessions': 1, 'dte_days': 1,
                    'live': {'state': 'FILLED', 'neo_symbol': 'X', 'neo_token': 't', 'tick': 0.05,
                             'leg': None, 'exit_fills': []}})
    s.save()
    _set_execution(wired, mode)
    assert cli.main(['live-run', '--once']) == 0
    alerts = [r.message for r in caplog.records if 'ALERT' in r.message]
    assert any('REAL LIVE RECORDS EXIST' in a and 'REALLONG' in a for a in alerts)
    assert any('EXITS-ONLY' in a for a in alerts)
    assert (tmp_path / 'state_live' / 'heartbeat.json').exists()        # the live book ran


def test_r2_04_lots_typo_in_the_live_overlay_still_runs_exits(wired, caplog):  # noqa: F811 - fixture from test_live_cli
    """R4-04: live's rules come from the tracked defaults plus LIVE's own
    overlay; a typo there blocks entries only (exits-only), and a typo in
    the PAPER overlay changes nothing for live."""
    caplog.set_level(logging.INFO)
    overlay = wired['tmp'] / 'paper_overlay.json'
    overlay.write_text(json.dumps({'lots': 2}))
    orig = config_mod.OVERLAY_FILE
    config_mod.OVERLAY_FILE = overlay
    try:
        wired['overlay'].write_text(json.dumps({'execution': 'live', 'lots': 2}))   # LIVE's own typo
        wired['live'].mkdir(parents=True, exist_ok=True)
        s = LiveStore(wired['live'])
        s.add_position({'status': 'open', 'side': 'CE', 'symbol': 'REALLONG', 'qty': 65, 'lot_size': 65,
                        'expiry': '2026-10-27', 'entry_date': '2026-10-23', 'entry_price': 150.0,
                        'stop_level': 75.0, 'trigger': 150.0, 'exit_defers': 0, 'cls': 'other',
                        'dte_sessions': 1, 'dte_days': 1, 'token': 1, 'strike': 22850.0,
                        'live': {'state': 'FILLED', 'neo_symbol': 'X', 'neo_token': 't', 'tick': 0.05,
                                 'leg': None, 'exit_fills': []}})
        s.save()
        assert cli.main(['live-run', '--once']) == 0
        assert wired['fbs']                                               # a broker was built
        alerts = [r.message for r in caplog.records if 'ALERT' in r.message]
        assert any('REALLONG' in a and 'EXITS-ONLY' in a for a in alerts)
        caplog.clear()
        _set_execution(wired, 'live')                                     # the live typo gone
        assert cli.main(['live-run', '--once']) == 0
        alerts = [r.message for r in caplog.records if 'ALERT' in r.message]
        assert not any('EXITS-ONLY' in a for a in alerts)                 # paper's typo: not live's
        assert any('RULES DIFFER FROM PAPER' in r.message and 'lots' in r.message
                   for r in caplog.records)
    finally:
        config_mod.OVERLAY_FILE = orig


def test_r2_04_broken_paper_overlay_does_not_stop_live(wired, caplog):  # noqa: F811 - fixture from test_live_cli
    caplog.set_level(logging.INFO)
    overlay = wired['tmp'] / 'paper_overlay.json'
    overlay.write_text('{{{')
    orig = config_mod.OVERLAY_FILE
    config_mod.OVERLAY_FILE = overlay
    try:
        assert cli.main(['live-run', '--once']) == 0
        assert any('paper config unreadable' in r.message for r in caplog.records)   # information only
    finally:
        config_mod.OVERLAY_FILE = orig


def test_r2_04_unusable_credentials_with_a_record_alerts_naming_it(wired, monkeypatch, caplog):  # noqa: F811 - fixture from test_live_cli
    caplog.set_level(logging.INFO)
    from nifty_btst.neo_client import CredentialsError

    def broken(execution, neo, sdir, clock):
        raise CredentialsError('mpin missing')
    monkeypatch.setattr(live_cli, 'build_broker', broken)
    wired['live'].mkdir(parents=True, exist_ok=True)
    s = LiveStore(wired['live'])
    s.add_position({'status': 'open', 'side': 'CE', 'symbol': 'REALLONG', 'qty': 65, 'lot_size': 65,
                    'expiry': '2026-10-27', 'entry_date': '2026-10-23', 'entry_price': 150.0,
                    'stop_level': 75.0, 'trigger': 150.0, 'exit_defers': 0, 'cls': 'other',
                    'dte_sessions': 1, 'dte_days': 1, 'token': 1, 'strike': 22850.0,
                    'live': {'state': 'FILLED', 'neo_symbol': 'X', 'neo_token': 't', 'tick': 0.05,
                             'leg': None, 'exit_fills': []}})
    s.save()
    _set_execution(wired, 'live')
    assert cli.main(['live-run', '--once']) == 1
    assert any('UNSERVICED' in r.message and 'REALLONG' in r.message for r in caplog.records)


# ------------------------------------------------------------------ R2-05
def test_r2_05_trade_book_outage_does_not_block_a_stop(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)

    def trades():
        raise BrokerUnavailable('trade book: no stat in response / unknown empty shape')
    rig.broker.trades = trades
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    assert len(sells(rig)) == 1


def test_r2_05_empty_shape_is_per_report():
    http = FakeHTTP()
    pol = nc.EnvelopePolicy(empty_per_report={'trade book': (None, 'no trades')})
    r = nc.NeoReader(CREDS, 'https://mis.example', 5, 5, session=http, totp_fn=lambda s: '0',
                     policy=pol)
    login_ok(http)
    r.login()
    http.script[nc.PATH_TRADES] = R(200, {'stat': 'Not_Ok', 'emsg': 'No Trades', 'stCode': 1})
    assert r.trades() == []
    http.script[nc.PATH_POSITIONS] = R(200, {'stat': 'Not_Ok', 'emsg': 'No Trades', 'stCode': 1})
    with pytest.raises(nc.NeoUnavailable):
        r.positions()                                             # positions keep the default shape
    neo = build_neo(neo_raw(empty_report_trade_book={'stcode': 7, 'emsg': 'x'}))
    assert neo.empty_per_report == {'trade book': (7, 'x')}


# ------------------------------------------------------------------ R2-07
def _carry(cfg, tmp_path, market, **kw):
    rig = marked(cfg, tmp_path, market, **kw)
    holding(rig, symbol=sym('2026-11-03', 22850, 'CE'), expiry='2026-11-03')
    return rig


def test_r2_07_holiday_does_not_latch_the_time_exit(cfg, tmp_path, market):
    rig = _carry(cfg, tmp_path, market)
    s = sym('2026-11-03', 22850, 'CE')
    default_premiums(market, '2026-11-03')
    rig.restart()
    rig.engine.session_expected = False
    rig.start(T(TUE, '09:00:00'))
    market.stale = set(market.books)                # a holiday: nothing trades
    rig.go(T(TUE, '09:20:01'))
    assert rig.engine.closed_today == T(TUE, '09:20:01').date()
    rig.go(T(TUE, '09:25:01'))
    assert state(rig)['live'].get('exit_reason') is None and not sells(rig)
    market.stale = set()
    market.set(s, 210.0, bid=209.5, ask=210.5)
    rig.restart()
    rig.start(T(WED, '09:00:00'))
    rig.go(T(WED, '09:15:05'))
    assert not sells(rig)                           # 09:15: the stop watch, not the exit
    rig.go(T(WED, '09:25:01'))
    assert sells(rig) and state(rig)['live']['exit_reason'] == 'next_day_0925'


def test_r2_07_no_kite_token_on_a_holiday_does_not_latch(cfg, tmp_path, market):
    rig = _carry(cfg, tmp_path, market)
    default_premiums(market, '2026-11-03')
    rig.restart()
    rig.engine.market = None
    rig.engine.session_expected = False
    rig.start(T(TUE, '09:00:00'))
    rig.engine.service_orders(T(TUE, '09:30:00'))
    rig.engine.step(T(TUE, '09:30:00'))
    assert state(rig)['live'].get('exit_reason') is None
    rig.engine.session_expected = True              # a calendar session with quotes out
    rig.engine.step(T(TUE, '09:31:00'))
    # R3-10: the calendar alone is not evidence; nothing has been SHOWN trading
    assert state(rig)['live'].get('exit_reason') is None
    assert any('nothing has traded today' in s for s in rig.sent)
    # evidence: the owner's own order in the app, dated today, at the broker
    rig.broker.place(OrderRequest(symbol='OWNERS-OWN', token='9', side='B', qty=LOT,
                                  price='1.00', tag='APP'))
    rig.engine.step(T(TUE, '09:32:00'))
    assert state(rig)['live']['exit_reason'] == 'next_day_late'


# ------------------------------------------------------------------ R2-08
def test_r2_08_reconcile_inside_the_positions_lag_does_not_trip(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.positions_lag_sec = 10.0
    trigger_ce(rig)
    rig.go(T(MON, '10:00:01'))
    assert state(rig)['status'] == 'open'
    for hms in ('10:00:02', '10:00:05'):
        rig.clock['now'] = T(MON, hms)
        rig.engine._reconcile(T(MON, hms), startup=False)
    assert not rig.store.kill()['tripped']
    rig.clock['now'] = T(MON, '10:10:00')
    rig.engine._reconcile(T(MON, '10:10:00'), startup=False)
    assert rig.engine.reconcile_ok and not rig.store.kill()['tripped']
    rig.market.set(PE_MON, 148.0, bid=147.8, ask=148.2)
    rig.go(T(MON, '10:11:00'))
    assert [q for q in buys(rig) if q.symbol != CE_NEO]          # the PE side still trades


# ------------------------------------------------------------------ R2-09
def test_r2_09_cancel_retries_once_a_minute_after_the_limit(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    rig.broker.cancel_faults = ['timeout'] * MAX_CANCEL_ATTEMPTS
    t0 = T(MON, '11:00:01')
    for i in range(0, 120):
        rig.go(t0 + timedelta(seconds=i))
    assert not rig.broker.cancel_faults                          # the cancel path is healthy again
    for i in range(120, 400, 3):
        rig.go(t0 + timedelta(seconds=i))
    assert rig.broker.orders[0]['ordSt'] == 'cancelled' and len(sells(rig)) >= 2
    assert any('retrying once a minute' in s for s in rig.sent)


# ------------------------------------------------------------------ R2-10
def test_r2_10_slow_mode_keeps_a_sell_working_most_of_the_time(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market, neo_overrides={'exit_chase_fast_max': 2})
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    t0 = T(MON, '11:00:00')
    working = 0
    for i in range(0, 600):
        rig.go(t0 + timedelta(seconds=i))
        if i >= 60 and any(o['ordSt'] == 'open' for o in rig.broker.orders):
            working += 1
    assert working / 540.0 > 0.9


# ------------------------------------------------------------------ R2-11
def test_r2_11_service_failure_does_not_starve_step_and_alerts_each_minute(wired, monkeypatch, caplog):  # noqa: F811 - fixture from test_live_cli
    caplog.set_level(logging.INFO)
    from test_live_cli import _advancing_clock
    _advancing_clock(wired, monkeypatch, T('2026-10-26', '15:30:00'))
    steps = {'n': 0}

    def boom(self, now):
        raise OSError('No space left on device')

    def count(self, now):
        steps['n'] += 1
    monkeypatch.setattr(live_cli.LiveEngine, 'service_orders', boom)
    monkeypatch.setattr(live_cli.LiveEngine, 'step', count)
    assert cli.main(['live-run']) == 0
    assert steps['n'] >= 100
    alerts = [r for r in caplog.records if 'LIVE TICK ERRORS' in r.message and 'ALERT' in r.message]
    assert len(alerts) >= 8                                      # about one a minute for 10 min


# ------------------------------------------------------------------ R2-12
def test_r2_12_scrip_download_runs_in_a_worker_and_does_not_block_the_tick(cfg, tmp_path, market):
    import threading
    default_premiums(market, '2026-10-27')
    rig = Rig(cfg, tmp_path, market)
    gate = threading.Event()
    calls = {'n': 0}

    def loader():
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('download timed out')
        gate.wait(5)                                 # a slow download
        return SCRIPS
    rig.engine._scrip_loader = loader
    rig.start(T(MON, '09:00:00'))
    import time as _time
    t0 = _time.monotonic()
    rig.go(T(MON, '09:20:01'))                       # returns at once: the load is in a worker
    assert _time.monotonic() - t0 < 2.0
    assert rig.engine._scrips is None and rig.engine._scrips_thread is not None
    ticks = 0
    for i in range(0, 60, 3):                        # the loop keeps ticking while it loads
        rig.go(T(MON, '09:21:00') + timedelta(seconds=i))
        ticks += 1
    assert ticks == 20 and _time.monotonic() - t0 < 4.0
    gate.set()
    t = rig.engine._scrips_thread
    if t is not None:
        t.join(5)
    rig.go(T(MON, '09:22:05'))
    assert rig.store.day(T(MON, '09:22:05').date())['marks']['CE'].get('neo')


# ------------------------------------------------------------------ R2-13
def test_r2_13_unknown_not_ok_on_a_mutation_is_unclear_documented_codes_reject():
    http = FakeHTTP()
    t = nc.NeoTrader(CREDS, 'https://mis.example', 5, 5, session=http, totp_fn=lambda s: '0')
    login_ok(http)
    t.login()
    http.script[nc.PATH_PLACE] = [R(200, {'stat': 'Not_Ok', 'emsg': 'Gateway timeout to OMS',
                                          'stCode': 5000}),
                                  R(200, {'stat': 'Not_Ok', 'emsg': 'Insufficient balance.',
                                          'stCode': 1004})]
    with pytest.raises(nc.NeoUnclear):
        t.place(symbol='s', token='1', side='S', qty=65, price='1.00', tag='t', segment='nse_fo',
                product='NRML')
    with pytest.raises(nc.NeoRejected):
        t.place(symbol='s', token='1', side='S', qty=65, price='1.00', tag='t', segment='nse_fo',
                product='NRML')


# ------------------------------------------------------------------ R2-06 / R2-15 / R2-17 (probe)
def test_r2_06_probe_chase_never_sends_a_second_sell_when_the_book_lags(tmp_path):
    r = ProbeRig(tmp_path, fill_delay=0.0)
    r.fb.set_external_position(NEO, LOT, day=T('2026-10-23', '00:00:00').date())
    r.fb.visibility_lag_sec = 120.0
    r.fb.positions_lag_sec = 120.0
    r.probe.held = LOT
    row = r.probe._resolve_row(SCRIPS, KITE_ROW)
    r.probe._sell(row, r.quote)
    assert len(r.fb.sent) == 1 and r.probe.stopped
    r.fb.order_book()
    assert r.fb.true_net(NEO) == 0


def test_r2_15_probe_buy_invisible_is_never_called_flat_or_nothing_held(tmp_path):
    r = ProbeRig(tmp_path, fill_delay=10000.0)
    r.fb.visibility_lag_sec = 300.0
    r.fb.cancel_faults = ['timeout'] * 50
    r.fb.hold_fills = True
    rc = r.probe.run(SCRIPS, KITE_ROW, r.quote, 500, step='hold')
    assert rc == 1 and r.probe.stopped and not any('nothing held' in s for s in r.out)
    assert r.probe.held == LOT                       # an invisible BUY counts as possibly held
    r2 = ProbeRig(tmp_path, fill_delay=10000.0)
    r2.fb.hold_fills = True
    orig_place = r2.fb.place

    def place(req):
        if len(r2.fb.sent) == 1:
            r2.fb.visibility_lag_sec = 300.0
            r2.fb.cancel_faults = ['timeout'] * 50
        return orig_place(req)
    r2.fb.place = place
    rc = r2.run()
    assert rc == 1 and 'RUN STOPPED' in r2.out[-1] and 'flat' not in r2.out[-1]


def test_r2_17_probe_skips_b_when_a_filled_and_rechecks_the_ceiling(tmp_path):
    r = ProbeRig(tmp_path, fill_delay=0.0)        # the rest order fills (a crash through 50%)
    r.run()
    assert [q.side for q in r.fb.sent] == ['B', 'S']
    r2 = ProbeRig(tmp_path / 'b', fill_delay=10000.0)
    orig = r2.fb.place

    def place(req):
        no = orig(req)
        r2.quotes.append(Book(49.0, 48.8, 50.0, 1000, 1000, '', ltt='2026-10-26T10:00:00'))
        return no
    r2.fb.place = place
    r2.run(max_premium=500)
    assert len(r2.fb.sent) == 1 and any('NOT buying: premium' in s for s in r2.out)


def test_r2_14_allow_missing_row_needs_journal_and_owner(tmp_path):
    r = ProbeRig(tmp_path)
    r.probe.allow_missing_row = True
    r.probe.owner_confirms_held = True
    rc = r.run(step='sell')                       # no hold ever happened
    assert rc == 1 and not r.fb.sent and any('NO filled probe hold' in s for s in r.out)


# ------------------------------------------------------------------ R2-18
def test_r2_18_identity_keys_never_reach_probe_output_or_alerts(tmp_path):
    from test_probe_safety import FakeReader

    class Rd(FakeReader):
        def raw_report(self, path, jdata=None):
            if 'orders' in path:
                return {'stat': 'Ok', 'stCode': 200, 'data': [{'nOrdNo': '1', 'actId': 'REALUCC',
                                                               'usrId': 'REALUCC',
                                                               'rejRsn': 'RMS: UCC123 margin'}]}
            return super().raw_report(path, jdata)
    out = []
    neo_probe.run_probe(Rd(), 'NIFTY', T(MON, '10:00:00').date(), out=out.append)
    assert not any('REALUCC' in s or 'UCC123' in s for s in out)
    r = ProbeRig(tmp_path)
    r.fb.set_external_position(NEO, LOT)
    r.run(step='sell')
    assert not any('ACCOUNTID' in s for s in r.probe.report)
    from nifty_btst.notify import Notifier
    n = Notifier(tmp_path / 'none.json', False, 'P')
    n.redactions = ['UCC123']
    n.send('RMS:Margin Exceeds for UCC123')
    assert 'UCC123' not in n.sent[-1]


# ------------------------------------------------------------------ R2-19
def test_r2_19_owner_books_an_external_close(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig, symbol=sym('2026-11-03', 22850, 'CE'), expiry='2026-11-03')
    rig.broker.carry_forward_rows = False
    default_premiums(market, '2026-11-03')
    rig.restart()
    rig.start(T(TUE, '09:00:00'))
    rig.go(T(TUE, '09:26:00'))
    assert state(rig)['status'] == 'open' and not sells(rig) and not rig.store.kill()['tripped']
    with pytest.raises(ValueError):
        rig.engine.book_external(1, T(TUE, '09:27:00'), None)      # a price is required
    rig.engine.book_external(1, T(TUE, '09:27:00'), 171.0)
    p = state(rig)
    assert p['status'] == 'closed' and p['exit_reason'] == 'closed_externally' and p['exit_price'] == 171.0
    assert any(ln['ev'] == 'OWNER' and ln['action'] == 'book_external' for ln in rig.journal.lines())


def test_r2_19_owner_requests_reach_the_running_loop_as_files(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    (rig.dir / 'CONFIRM_HELD_1').write_text(json.dumps(
        {'id': 1, 'symbol': CE_MON, 'date': MON, 'action': 'confirm_held'}))      # stamped (R3-02)
    rig.service(T(MON, '11:00:00'))
    assert rig.store.owner_confirmed_held(1, T(MON, '11:00:00').date())
    assert json.loads((rig.dir / 'CONFIRM_HELD_1.result').read_text())['applied'] is True
    (rig.dir / 'BOOK_EXTERNAL_1.json').write_text(json.dumps(
        {'id': 1, 'symbol': CE_MON, 'date': MON, 'price': 160.0}))
    rig.service(T(MON, '11:00:01'))
    assert state(rig)['status'] == 'closed' and state(rig)['exit_price'] == 160.0


# ------------------------------------------------------------------ R2-20
def test_r2_20_mismatch_before_the_open_is_deferred(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    default_premiums(market, '2026-11-03')
    rig.broker.carry_forward_rows = False            # no row at 09:00: unknown, no trip
    rig.restart()
    rig.start(T(TUE, '09:00:00'))
    assert not rig.store.kill()['tripped']
    rig.broker.carry_forward_rows = True
    rig.go(T(TUE, '09:15:05'))
    rig.engine._reconcile(T(TUE, '09:15:06'), startup=False)
    assert rig.engine.reconcile_ok and not rig.store.kill()['tripped']
    # a report that disagrees BEFORE the open is reported, never a verdict
    rig2 = marked(cfg, tmp_path / 'b', market)
    holding(rig2)
    rig2.broker.ext_positions[CE_NEO] = 2 * LOT
    rig2.restart()
    rig2.start(T(TUE, '09:00:00'))
    assert not rig2.store.kill()['tripped'] and not rig2.engine.reconcile_ok
    rig2.clock['now'] = T(TUE, '09:15:06')
    rig2.engine._reconcile(T(TUE, '09:15:06'), startup=False)
    assert not rig2.store.kill()['tripped'] and not rig2.engine.reconcile_ok  # first post-open read
    rig2.clock['now'] = T(TUE, '09:15:09')
    rig2.engine._reconcile(T(TUE, '09:15:09'), startup=False)
    assert rig2.store.kill()['tripped'] and 'EXTRA LOT' in rig2.store.kill()['reason']


# ------------------------------------------------------------------ R2-22
def test_r2_22_junk_rows_and_missing_quantity_fields_raise():
    http = FakeHTTP()
    t = nc.NeoReader(CREDS, 'https://mis.example', 5, 5, session=http, totp_fn=lambda s: '0')
    login_ok(http)
    t.login()
    http.script[nc.PATH_ORDER_BOOK] = R(200, {'stat': 'Ok', 'data': ['gateway busy']})
    with pytest.raises(nc.NeoUnavailable):
        t.order_book()
    with pytest.raises(RowError):
        PositionView.from_neo({'trdSym': 'X', 'prod': 'NRML', 'netQty': '65'})
    with pytest.raises(RowError):
        PositionView.from_neo({'trdSym': 'X', 'flBuyQty': 'abc'})
    with pytest.raises(RowError):
        PositionView.from_neo({'trdSym': 'X', 'flBuyQty': '1'}, qty_unit='lots')   # no lotSz
    assert PositionView.from_neo({'trdSym': 'X', 'flBuyQty': '1', 'lotSz': '65'},
                                 qty_unit='lots').net_qty == 65


# ------------------------------------------------------------------ R2-23
def test_r2_23_rejected_row_with_a_written_off_tag_is_not_a_kill(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.place_faults = [('reject', '1004', 'Insufficient balance')]
    trigger_ce(rig)
    tag = rig.broker.sent[0].tag
    rig.broker.orders.append({'nOrdNo': '555', 'ordSt': 'rejected', 'trdSym': CE_NEO, 'trnsTp': 'B',
                              'qty': str(LOT), 'fldQty': '0', 'avgPrc': '0', 'prc': '153.80',
                              'rejRsn': 'RMS', 'GuiOrdId': tag, 'ordDtTm': '', 'prod': 'NRML',
                              '_placed': T(MON, '10:00:00').isoformat()})
    rig.engine._reconcile(T(MON, '10:01:00'), startup=False)
    assert not rig.store.kill()['tripped']


# ------------------------------------------------------------------ R2-24
def test_r2_24_unknown_status_is_alerted_and_treated_as_live(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    rig.broker.orders[0]['ordSt'] = 'expired'
    rig.go(T(MON, '10:00:01'))
    assert any('UNKNOWN status' in s for s in rig.sent) and len(buys(rig)) == 1


# ------------------------------------------------------------------ R2-25
def test_r2_25_live_store_fsyncs_the_directory(tmp_path, monkeypatch):
    import os
    calls = []
    real_fsync, real_open, real_close = os.fsync, os.open, os.close
    DIR_FD = 999_001
    monkeypatch.setattr(os, 'open', lambda path, *a, **k: DIR_FD if str(path) == str(tmp_path)
                        else real_open(path, *a, **k))
    monkeypatch.setattr(os, 'fsync', lambda fd: calls.append(fd) if fd == DIR_FD else real_fsync(fd))
    monkeypatch.setattr(os, 'close', lambda fd: None if fd == DIR_FD else real_close(fd))
    s = LiveStore(tmp_path)
    s.save()
    assert DIR_FD in calls                           # the DIRECTORY was fsynced, not only the file


# ------------------------------------------------------------------ R2-26
def test_r2_26_order_probe_refuses_while_a_live_run_holds_the_lock(wired, monkeypatch, capsys):  # noqa: F811 - fixture from test_live_cli
    from nifty_btst import arming
    _set_execution(wired, 'live')
    wired['live'].mkdir(parents=True, exist_ok=True)
    (wired['live'] / arming.PROBE_ARM_FILE).write_text('2026-10-26')
    wired['clock']['now'] = T('2026-10-26', '10:00:00')
    monkeypatch.setattr(live_cli._Lock, 'acquire', lambda self: False)
    s = sym('2026-10-27', 22000, 'CE')
    wired['fm'].set(s, 5.0, bid=4.8, ask=5.0)
    assert cli.main(['neo-order-probe', '--symbol', s, '--confirm-real-order']) == live_cli.EXIT_REFUSED
    assert 'live-run holds' in capsys.readouterr().out


# ------------------------------------------------------------------ R2-27
def test_r2_27_paper_commands_do_not_import_the_live_module(monkeypatch):
    import sys
    sys.modules.pop('nifty_btst.live_cli', None)
    import builtins
    real_import = builtins.__import__

    def guard(name, *a, **k):
        if 'live_cli' in name or (a and a[2] and 'live_cli' in a[2]):
            raise SyntaxError('broken live module')
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, '__import__', guard)
    with pytest.raises(SystemExit):                              # argparse: a parser exists
        cli.main(['--help'])
    monkeypatch.setattr(cli, 'cmd_status', lambda args: 0)
    assert cli.main(['status']) == 0                             # the paper command runs


# ---------------------------------------------------- units (measurement 6)
def test_units_lots_reported_where_shares_expected_trips_loud(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.qty_unit = 'lots'                                 # NEO reports 1, we expect 65
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    k = rig.store.kill()
    assert not sells(rig) and k['tripped'] and k['loud'] and 'UNIT MISMATCH' in k['reason']


def test_units_neo_broker_converts_lots_when_configured():
    class Tr:
        ready = True

        def positions(self):
            return [{'trdSym': 'S', 'prod': 'NRML', 'flBuyQty': '1', 'lotSz': '65'}]
    assert NeoBroker(Tr(), 'nse_fo', 'NRML', 'Net', qty_unit='lots').net_long('S', 'NRML') == 65
    assert NeoBroker(Tr(), 'nse_fo', 'NRML', 'Net').net_long('S', 'NRML') == 1


# ---------------------------------------------- review-1 survivors, pinned
def test_neo01_positions_check_after_the_grace_adopts_a_fill(cfg, tmp_path, market):
    """Mutant: `verdict = 'absent'` in `_resolve_unknown` survived. After the
    grace, a position row that shows the lot must ADOPT, never resend."""
    rig = marked(cfg, tmp_path, market, neo_overrides={'unknown_grace_sec': 60})
    rig.broker.visibility_lag_sec = 10000.0
    rig.broker.place_faults = ['timeout_reached']
    trigger_ce(rig)
    rig.go(T(MON, '10:00:30'))
    rig.go(T(MON, '10:01:01'))
    p = state(rig)
    assert p['status'] == 'open' and len(buys(rig)) == 1 and p['live'].get('avg_price_missing')


def test_neo12_real_build_broker_persists_the_fake(tmp_path):
    creds = tmp_path / 'c.json'
    creds.write_text(json.dumps(CREDS))
    neo = build_neo(neo_raw(credentials_file=str(creds)))
    b, r = live_cli.build_broker('rehearsal', neo, tmp_path, lambda: T(MON, '10:00:00'))
    assert isinstance(b, FakeBroker) and b.path == tmp_path / 'fake_broker.json'
    assert not hasattr(r, 'place') and not hasattr(r, 'cancel')
    b.ensure_session()
    b.place(OrderRequest('S', '1', 'B', 65, '1.00', 'tag'))
    b2, _ = live_cli.build_broker('rehearsal', neo, tmp_path, lambda: T(MON, '10:00:00'))
    assert len(b2.orders) == 1
    b3, r3 = live_cli.build_broker('live', neo, tmp_path, lambda: T(MON, '10:00:00'))
    assert isinstance(b3, NeoBroker) and isinstance(r3, nc.NeoTrader) and not r3.ready


def test_neo22_loop_polls_kite_on_the_live_key(wired, monkeypatch):  # noqa: F811 - fixture from test_live_cli
    from test_live_cli import _advancing_clock
    _set_execution(wired, 'rehearsal', poll_seconds=1)           # live: 1 s; paper cfg: 3 s
    _advancing_clock(wired, monkeypatch, T('2026-10-26', '15:39:30'))
    steps = {'n': 0}
    monkeypatch.setattr(live_cli.LiveEngine, 'step', lambda self, now: steps.__setitem__('n', steps['n'] + 1))
    assert cli.main(['live-run']) == 0
    assert steps['n'] >= 28


def test_neo35_login_log_carries_no_identity(caplog):
    caplog.set_level(logging.INFO)
    http = FakeHTTP()
    r = nc.NeoReader(CREDS, 'https://mis.example', 5, 5, session=http, totp_fn=lambda s: '0')
    login_ok(http)
    r.login()
    msgs = [rec.getMessage() for rec in caplog.records if 'login' in rec.getMessage().lower()]
    assert msgs and not any(CREDS['ucc'] in m or 'X' == m[-1] for m in msgs)


# --------------------------------------------- end to end on the real stack
class Exchange:
    """A tiny NEO over FakeHTTP, in the documented shapes."""

    def __init__(self, clock):
        self.clock, self.orders, self.calls, self.seq = clock, [], [], 0
        self.positions = {}

    def post(self, url, **kw):
        return self._h('POST', url, **kw)

    def get(self, url, **kw):
        return self._h('GET', url, **kw)

    def _h(self, m, url, **kw):
        self.calls.append((m, url))
        if url.endswith(nc.LOGIN_PATH):
            return R(200, {'data': {'token': 'V', 'sid': 'S'}})
        if url.endswith(nc.VALIDATE_PATH):
            return R(200, {'data': {'token': 'T', 'sid': 'S', 'baseUrl': 'https://cis.x'}})
        if url.endswith(nc.PATH_PLACE):
            j = json.loads(kw['data']['jData'])
            self.seq += 1
            no = '26%013d' % self.seq
            self.orders.append({'nOrdNo': no, 'ordSt': 'complete', 'trdSym': j['ts'], 'trnsTp': j['tt'],
                                'qty': j['qt'], 'fldQty': j['qt'], 'avgPrc': j['pr'], 'prc': j['pr'],
                                'rejRsn': '--', 'GuiOrdId': j['ig'], 'prod': j['pc'],
                                'ordDtTm': self.clock().strftime('%d-%b-%Y %H:%M:%S')})
            n = self.positions.setdefault(j['ts'], 0)
            self.positions[j['ts']] = n + (int(j['qt']) if j['tt'] == 'B' else -int(j['qt']))
            return R(200, {'nOrdNo': no, 'stat': 'Ok', 'stCode': 200})
        if url.endswith(nc.PATH_ORDER_BOOK):
            return R(200, {'stat': 'Ok', 'stCode': 200, 'data': self.orders})
        if url.endswith(nc.PATH_TRADES):
            return R(200, {'stat': 'Ok', 'stCode': 200, 'data': [o for o in self.orders if int(o['fldQty'])]})
        if url.endswith(nc.PATH_POSITIONS):
            return R(200, {'stat': 'Ok', 'stCode': 200, 'data': [
                {'trdSym': s, 'prod': 'NRML', 'flBuyQty': str(max(q, 0)), 'flSellQty': str(max(-q, 0)),
                 'cfBuyQty': '0', 'cfSellQty': '0', 'lotSz': '65'} for s, q in self.positions.items() if q]})
        if url.endswith(nc.PATH_LIMITS):
            return R(200, {'stat': 'Ok', 'stCode': 200, 'Net': '1000000.00'})
        raise AssertionError(url)


def test_neo23_end_to_end_on_the_real_client_stack(cfg, tmp_path, market):
    default_premiums(market, '2026-10-27')
    rig = Rig(cfg, tmp_path, market)
    ex = Exchange(lambda: rig.clock['now'])
    t = nc.NeoTrader(CREDS, 'https://mis.example', 5, 5, session=ex, totp_fn=lambda s: '0')
    rig.engine.broker = NeoBroker(t, 'nse_fo', 'NRML', 'Net')
    rig.start(T(MON, '09:00:00'))
    rig.go(T(MON, '09:20:01'))
    market.set(CE_MON, 153.5, bid=153.3, ask=153.8)
    rig.go(T(MON, '10:00:00'))
    rig.go(T(MON, '10:00:01'))
    assert state(rig)['status'] == 'open'
    market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    rig.go(T(MON, '11:00:01'))
    p = state(rig)
    assert p['status'] == 'closed' and p['exit_price'] == 69.8 and ex.positions[CE_NEO] == 0
    assert not rig.store.kill()['tripped']
    assert not [c for c in ex.calls if c[1].endswith(nc.PATH_CANCEL)]


def test_stage0_probe_on_the_real_reader_touches_no_mutating_path():
    ex = Exchange(lambda: T(MON, '10:00:00'))
    orig = ex._h

    def h(m, url, **kw):
        if url.endswith(nc.PATH_SCRIP_MASTER):
            return R(200, {'data': {'filesPaths': ['https://f/nse_fo.csv']}})
        if url.endswith('nse_fo.csv'):
            from test_neo_symbols import HEADER, row
            return R(200, None, HEADER + row(1, 'NIFTY-W', 'NIFTY', date(2026, 10, 27), 22850, 'CE'))
        return orig(m, url, **kw)
    ex._h = h
    rd = nc.NeoReader(CREDS, 'https://mis.example', 5, 5, session=ex, totp_fn=lambda s: '0')
    out = []
    assert neo_probe.run_probe(rd, 'NIFTY', T(MON, '10:00:00').date(), out=out.append) == 0
    assert not [c for c in ex.calls if c[1].endswith(nc.PATH_PLACE) or c[1].endswith(nc.PATH_CANCEL)]
