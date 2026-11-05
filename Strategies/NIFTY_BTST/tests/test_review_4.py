"""Regression tests for docs/NEO_REVIEW_4.md (R4-01..R4-23) and the review-4
mutation survivors M29/M30/M32/M33/M35: the reviewer's reproductions, ported
with the CORRECT behaviour asserted."""
import json
import logging
import subprocess
import sys
import threading
import time as _time
from datetime import date, timedelta
from pathlib import Path

import pytest
from conftest import LOT, T, default_premiums, sym
from live_helpers import MON, SCRIPS, TUE, neo_symbol
from nifty_btst import alerts, cli, journal as jmod, live_cli, neo_client as nc
from nifty_btst.alerts import AsyncNotifier
from nifty_btst.broker import OrderRequest, PositionView
from nifty_btst.live_engine import LiveStore
from nifty_btst.neo_order_probe import OrderProbe, ProbeRefused
from test_live_cli import _advancing_clock, _set_execution, wired  # noqa: F401 - fixture
from test_live_engine import CE_MON, CE_NEO, holding, marked, state, trigger_ce
from test_neo_client import CREDS, FakeHTTP, R, login_ok
from test_probe_safety import KITE_ROW, NEO, ProbeRig
from test_review_3 import _real_record, buys, carried_no_row, sells, stamped

APP = Path(__file__).resolve().parents[1]


def app_sell(rig, n, price='69.00', fill=True):
    """The owner sells in the app: an order with no tag of ours."""
    no = rig.broker.place(OrderRequest(symbol=n, token='t', side='S', qty=LOT, price=price, tag=''))
    rig.broker.sent.pop()
    for o in rig.broker.orders:
        if o['nOrdNo'] == no and fill:
            o.update({'ordSt': 'complete', 'fldQty': str(LOT), 'avgPrc': price,
                      '_filled_at': rig.clock['now'].isoformat()})
    return no


def _tg(tmp_path):
    p = tmp_path / 'tg.json'
    p.write_text(json.dumps({'bot_token': 'T', 'chat_id': '1'}))
    return p


# ---------------------------------------------------------------- R4-01 / R4-19 / M29
def test_r4_01_flush_waits_for_the_post_in_flight(tmp_path, monkeypatch):
    import requests
    posted = []

    class Resp:
        status_code = 200
        text = 'ok'

    def slow(url, data=None, timeout=None):
        _time.sleep(0.3)                                  # a TLS handshake + POST
        posted.append(data['text'])
        return Resp()
    monkeypatch.setattr(requests, 'post', slow)
    n = AsyncNotifier(_tg(tmp_path), True, 'X')
    n.send('LIVE BOOK CORRUPT')
    _time.sleep(0.05)                                     # the worker has popped it: queue 0, in flight 1
    assert n.pending == 1
    n.flush()
    assert posted == ['X\nLIVE BOOK CORRUPT'] and n.posted == 1 and n.pending == 0


def test_r4_01b_the_last_alert_survives_a_real_process_exit(tmp_path):
    out = tmp_path / 'posted.txt'
    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[3])\n"
        "import requests\n"
        "class R:\n    status_code = 200\n    text = 'ok'\n"
        "def post(url, data=None, timeout=None):\n"
        "    time.sleep(0.3)\n"
        "    open(sys.argv[1], 'a').write(data['text'][:40] + '\\n')\n"
        "    return R()\n"
        "requests.post = post\n"
        "from nifty_btst.alerts import AsyncNotifier\n"
        "n = AsyncNotifier(Path(sys.argv[2]), True, 'X')\n"
        "n.send('LIVE BOOK CORRUPT: positions not watched')\n"
        "n.flush()\n"
        "sys.exit(0)\n")
    r = subprocess.run([sys.executable, '-c', code, str(out), str(_tg(tmp_path)), str(APP)],
                       timeout=30, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert r.returncode == 0, r.stdout
    assert 'LIVE BOOK CORRUPT' in out.read_text()


def test_r4_19_a_dead_worker_posts_synchronously(tmp_path, monkeypatch):
    import requests
    calls = []

    class Resp:
        status_code = 200
        text = 'ok'

    def post(url, data=None, timeout=None):
        calls.append((data['text'], timeout))
        if len(calls) == 1:
            raise SystemExit('thread dies')               # any BaseException kills the worker
        return Resp()
    monkeypatch.setattr(requests, 'post', post)
    n = AsyncNotifier(_tg(tmp_path), True, 'X')
    n.send('first')
    n._worker.join(3)
    assert not n._worker.is_alive()
    assert n.send('KILL TRIPPED') is True
    assert n.sync_posts == 1 and n.posted == 1 and calls[-1] == ('X\nKILL TRIPPED', alerts.SYNC_TIMEOUT_SEC)


def test_r4_19b_a_failed_post_is_retried(tmp_path, monkeypatch):
    import requests
    monkeypatch.setattr(alerts, 'RETRY_WAIT_SEC', 0.01)
    calls = []

    class Resp:
        status_code = 200
        text = 'ok'

    def flaky(url, data=None, timeout=None):
        calls.append(data['text'])
        if len(calls) < 3:
            raise requests.ConnectionError('blip')
        return Resp()
    monkeypatch.setattr(requests, 'post', flaky)
    n = AsyncNotifier(_tg(tmp_path), True, 'X')
    n.send('ENTRY #1 FILLED')
    n.flush()
    assert len(calls) == 3 and n.posted == 1 and n.failed == 0


# ---------------------------------------------------------------- R4-02 / R4-03 / M32
def test_r4_02_a_short_equal_to_our_filled_sell_is_a_loud_kill(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))                        # stop: the engine's SELL rests
    rig.clock['now'] = T(MON, '11:00:01')
    app_sell(rig, CE_NEO)                             # the owner sells in the app as well
    (rig.dir / 'BOOK_EXTERNAL_1.json').write_text(stamped(1, CE_MON, MON, price=68.0))
    rig.broker.hold_fills = False                     # the engine's SELL fills in the same second
    rig.service(T(MON, '11:00:02'))
    rig.service(T(MON, '11:00:03'))
    assert state(rig)['status'] == 'closed' and rig.broker.true_net(CE_NEO) == -LOT
    drop = [s for s in rig.sent if 'already closed' in s or 'resolved by itself' in s]
    assert drop and 'SHORT' in drop[0]
    rig.clock['now'] = T(MON, '11:06:00')
    rig.engine._reconcile(T(MON, '11:06:00'), startup=False)
    k = rig.store.kill()
    assert k['tripped'] and k['loud'] and 'SHORT' in k['reason'] and '130' in k['reason']
    assert not rig.engine.reconcile_ok and not any('Not a short' in s for s in rig.sent)


def test_r4_02b_short_when_the_fill_lands_during_the_cancel_path(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    rig.clock['now'] = T(MON, '11:00:01')
    app_sell(rig, CE_NEO)
    rig.broker.cancel_faults = [('reject', '1006', 'cannot cancel now')]
    (rig.dir / 'BOOK_EXTERNAL_1.json').write_text(stamped(1, CE_MON, MON, price=68.0))
    rig.service(T(MON, '11:00:02'))
    assert state(rig)['live']['owner_pending']
    rig.broker.hold_fills = False
    rig.service(T(MON, '11:00:03'))
    assert state(rig)['status'] == 'closed' and rig.broker.true_net(CE_NEO) == -LOT
    rig.clock['now'] = T(MON, '11:06:00')
    rig.engine._reconcile(T(MON, '11:06:00'), startup=False)
    assert rig.store.kill()['tripped'] and 'SHORT' in rig.store.kill()['reason']


def test_r4_02c_control_the_row_shape_with_matching_sums_is_still_not_a_short(cfg, tmp_path, market):
    rig, s = carried_no_row(cfg, tmp_path, market)
    n = neo_symbol(s)
    rig.engine.confirm_held(1, T(TUE, '09:10:00'))
    rig.go(T(TUE, '09:25:01'))
    rig.go(T(TUE, '09:25:02'))
    assert state(rig)['status'] == 'closed'
    rig.broker.positions = lambda: [PositionView.from_neo(
        {'trdSym': n, 'prod': 'NRML', 'flBuyQty': '0', 'flSellQty': str(LOT),
         'cfBuyQty': '0', 'cfSellQty': '0'})]
    for t in ('09:30:00', '09:36:00'):
        rig.engine._reconcile(T(TUE, t), startup=False)
    assert not rig.store.kill()['tripped'] and any('Not a short' in x for x in rig.sent)


def test_r4_03_a_foreign_filled_sell_blocks_our_sell_on_the_row_source(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    s = sym('2026-11-03', 22850, 'CE')
    holding(rig, symbol=s, expiry='2026-11-03')
    n = neo_symbol(s)
    default_premiums(market, '2026-11-03')
    rig.market.set(s, 170.0, bid=169.8, ask=170.2)
    rig.broker.positions_lag_sec = 60.0
    rig.restart()
    rig.start(T(TUE, '09:00:00'))
    rig.go(T(TUE, '09:15:05'))
    rig.clock['now'] = T(TUE, '09:24:40')
    no = app_sell(rig, n, price='170.00')             # in the book at once, in the row after 60 s
    rig.go(T(TUE, '09:25:01'))
    rig.go(T(TUE, '09:26:02'))
    assert not sells(rig) and rig.broker.true_net(n) == 0
    assert any(no in x and 'did not send FILLED' in x and 'live-book-external' in x for x in rig.sent)
    for t in ('09:31:00', '09:37:00'):
        rig.clock['now'] = T(TUE, t)
        rig.engine._reconcile(T(TUE, t), startup=False)
    p = state(rig)                                    # his filled SELL on the row: adopted as the exit
    assert p['status'] == 'closed' and p['exit_reason'] == 'closed_externally'
    assert not rig.store.kill()['tripped']            # his sale of our lot is not a short


# ---------------------------------------------------------------- R4-05 / M30 / M33
def test_r4_05a_the_trade_book_quantity_gets_the_unit_check_too(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    o = rig.broker.orders[0]
    o.update({'ordSt': 'complete', 'fldQty': '1', 'avgPrc': '153.80',
              '_filled_at': T(MON, '10:00:00').isoformat()})
    rig.broker._net = lambda lagged=True: {CE_NEO: {'fl': LOT, 'cf': 0}}
    rig.broker.blank_book = True                      # the order book shows nothing; the trade book does
    with pytest.raises(ValueError, match='live-book-external'):
        rig.engine.confirm_held(1, T(MON, '10:01:00'))
    p = state(rig)
    assert p['status'] == 'pending' and p['qty'] == LOT and p['live']['leg'].get('quarantined')
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '10:02:00'))
    assert not sells(rig)
    assert any('live-book-external 1 --price X --yes' in s for s in rig.sent)


def test_r4_05b_a_quarantined_exit_leg_is_watched_and_the_owner_can_clear_it(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    o = rig.broker.orders[0]
    o.update({'ordSt': 'complete', 'fldQty': '1', 'avgPrc': '69.80',
              '_filled_at': T(MON, '11:00:00').isoformat()})
    rig.go(T(MON, '11:00:01'))
    assert rig.store.kill()['loud'] and state(rig)['live']['leg'].get('quarantined')
    rig.market.set(CE_MON, 60.0, bid=59.8, ask=60.2)
    rig.go(T(MON, '11:01:05'))
    assert any('STOP HIT' in s and 'QUARANTINED' in s for s in rig.sent)   # watched, not traded
    assert len(sells(rig)) == 1
    rig.engine.book_external(1, T(MON, '11:05:00'), 68.0)   # the owner's word clears it
    p = state(rig)
    assert p['status'] == 'closed' and p['exit_reason'] == 'closed_externally'
    assert p['live']['leg'] is None and p['live']['leg_history'][-1].get('cleared_by_owner')


def test_m30_confirm_held_books_the_real_fill_from_the_trade_book(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    o = rig.broker.orders[0]
    o.update({'ordSt': 'complete', 'fldQty': str(LOT), 'avgPrc': '153.50',
              '_filled_at': T(MON, '10:00:00').isoformat()})
    rig.broker.blank_book = True                      # only the trade book shows the fill
    rig.engine.confirm_held(1, T(MON, '10:01:00'))
    p = state(rig)
    assert p['status'] == 'open' and p['entry_price'] == 153.5
    assert not p['live'].get('entry_price_estimated')


def test_m33_a_fill_larger_than_sent_is_quarantined(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    o = rig.broker.orders[0]
    o.update({'ordSt': 'complete', 'fldQty': str(2 * LOT), 'avgPrc': '153.80',
              '_filled_at': T(MON, '10:00:00').isoformat()})
    rig.go(T(MON, '10:00:01'))
    p, k = state(rig), rig.store.kill()
    assert p['status'] == 'pending' and k['tripped'] and 'ORDER QUANTITY UNIT' in k['reason']


# ---------------------------------------------------------------- R4-06 / R4-22
def test_r4_06_probe_flags_never_skip_the_live_sell_check(tmp_path):
    r = ProbeRig(tmp_path, fill_delay=0.0)
    j = jmod.Journal(tmp_path)
    r.fb.carry_forward_rows = False
    r.fb.set_external_position(NEO, LOT, day=date(2026, 10, 23))
    j.probe_held(T('2026-10-23', '10:00:00'), NEO, LOT)
    r.fb.hold_fills = True
    r.fb.visibility_lag_sec = 40.0
    r.fb.place_faults = ['timeout_reached']
    r.probe.allow_missing_row = r.probe.owner_confirms_held = True
    with pytest.raises(ProbeRefused):
        r.run(step='sell')
    assert r.fb.orders[0]['ordSt'] == 'open'
    r.clock['now'] += timedelta(seconds=30)
    r2 = OrderProbe(r.fb, j, lambda: r.clock['now'], r.sleep, r.out.append,
                    allow_missing_row=True, owner_confirms_held=True)
    r.fb.hold_fills = False
    r.fb.fill_delay = 1000.0
    assert any(o.live and o.side == 'S' for o in r.fb.order_book())   # the first SELL is visible
    r2.run(SCRIPS, KITE_ROW, r.quote, 500, step='sell')
    assert len([q for q in r.fb.sent if q.side == 'S']) == 1   # the live SELL refuses a second


def test_r4_22_a_second_all_run_refuses_before_buying(tmp_path):
    r = ProbeRig(tmp_path, fill_delay=0.0)
    assert r.run() == 0
    r2 = OrderProbe(r.fb, jmod.Journal(tmp_path), lambda: r.clock['now'], r.sleep, r.out.append)
    with pytest.raises(ProbeRefused, match='already exists today'):
        r2.run(SCRIPS, KITE_ROW, r.quote, 500, step='all')
    assert r.fb.true_net(NEO) == 0 and r2.held == 0
    assert len([q for q in r.fb.sent if q.side == 'B']) == 1


def test_r4_22b_sells_dated_another_day_do_not_count(cfg, tmp_path, market):
    rig, s = carried_no_row(cfg, tmp_path, market)
    n = neo_symbol(s)
    rig.engine.confirm_held(1, T(TUE, '09:10:00'))
    rig.broker.orders.append({'nOrdNo': '997', 'ordSt': 'complete', 'trdSym': n, 'trnsTp': 'S',
                              'qty': str(LOT), 'fldQty': str(LOT), 'avgPrc': '150.00', 'prc': '150.00',
                              'rejRsn': '--', 'GuiOrdId': '', 'ordDtTm': '26-Oct-2026 14:00:00',
                              'prod': 'NRML', '_placed': T(MON, '14:00:00').isoformat()})
    rig.broker.daily_book_reset = False               # a book that is NOT day-scoped
    rig.go(T(TUE, '09:25:01'))
    assert len(sells(rig)) == 1                       # yesterday's SELL is not today's


# ---------------------------------------------------------------- R4-07
def test_r4_07_a_request_before_the_open_is_reported_as_queued(wired, monkeypatch, capsys):  # noqa: F811 - fixture from test_live_cli
    monkeypatch.setattr(live_cli._Lock, 'acquire', lambda self: False)
    _real_record(wired['live'])
    now = live_cli.now_ist()
    opens = now.replace(hour=9, minute=15, second=0) + timedelta(days=1)
    LiveStore(wired['live']).heartbeat(now, state='pre-open', opens_at=opens.isoformat())
    assert cli.main(['live-confirm-held', '1', '--yes']) == 0
    out = capsys.readouterr().out
    assert 'QUEUED' in out and 'applies at 09:15' in out and 'NOT APPLIED' not in out


def test_r4_07b_the_loop_heartbeats_pre_open(wired, monkeypatch):  # noqa: F811 - fixture from test_live_cli
    states = []
    real = LiveStore.heartbeat

    def spy(self, when, **fields):
        states.append(fields.get('state'))
        return real(self, when, **fields)
    monkeypatch.setattr(LiveStore, 'heartbeat', spy)
    _advancing_clock(wired, monkeypatch, T('2026-10-26', '09:00:00'))
    assert cli.main(['live-run', '--once']) == 0
    assert 'pre-open' in states


# ---------------------------------------------------------------- R4-08
def test_r4_08_a_clean_read_resets_the_surplus_count(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    real = rig.broker.positions

    def extra():
        return [PositionView.from_neo({'trdSym': CE_NEO, 'prod': 'NRML', 'flBuyQty': str(LOT),
                                       'flSellQty': '0', 'cfBuyQty': '0', 'cfSellQty': '0'})]
    rig.broker.positions = extra
    rig.clock['now'] = T(MON, '10:00:00')
    rig.engine._reconcile(T(MON, '10:00:00'), startup=False)   # one stale read
    rig.broker.positions = real
    rig.engine._reconcile(T(MON, '10:05:00'), startup=False)
    rig.engine._reconcile(T(MON, '12:00:00'), startup=False)
    assert rig.engine.reconcile_ok
    rig.broker.positions = extra
    rig.engine._reconcile(T(MON, '14:00:00'), startup=False)   # a single stale read again
    assert not rig.store.kill()['tripped']
    rig.engine._reconcile(T(MON, '14:00:03'), startup=False)   # two in a row: confirmed
    assert rig.store.kill()['tripped']


# ---------------------------------------------------------------- R4-09
def test_r4_09_confirm_held_refuses_while_the_buy_is_live(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    with pytest.raises(ValueError, match='working order'):
        rig.engine.confirm_held(1, T(MON, '10:00:30'))
    p = state(rig)
    assert p['status'] == 'pending' and p['live']['leg'] and rig.broker.orders[0]['ordSt'] == 'open'
    assert not rig.store.owner_confirmed_held(1, T(MON, '10:00:30').date())


# ---------------------------------------------------------------- R4-10
def test_r4_10_definite_refusals_are_not_attempts(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.place_faults = [('reject', '429', 'slow down')] * 6
    t0 = T(MON, '10:00:00')
    rig.market.set(CE_MON, 153.5, bid=153.3, ask=153.8)
    for i in range(0, 18, 3):                         # six refusals, more than max_entry_attempts
        rig.go(t0 + timedelta(seconds=i))
    p = state(rig)
    assert p['status'] == 'pending' and p['live']['entry_attempts'] == 0
    assert not any('entry attempts without a fill' in s for s in rig.sent)
    rig.go(t0 + timedelta(seconds=18))                # the gateway relents
    rig.go(t0 + timedelta(seconds=19))
    assert state(rig)['status'] == 'open'


def test_r4_10b_refusals_do_not_push_the_chase_into_slow_mode(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.place_faults = [('reject', '100008', 'unauthorized')] * 10
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    t0 = T(MON, '11:00:00')
    for i in range(0, 30, 3):                         # ten refusals, the whole fast budget
        rig.go(t0 + timedelta(seconds=i))
    assert state(rig)['live']['exit_attempts'] == 0 and state(rig)['status'] == 'open'
    rig.go(t0 + timedelta(seconds=30))                # the IP is fixed: sent at once, not in 60 s
    rig.go(t0 + timedelta(seconds=31))
    assert state(rig)['status'] == 'closed'


# ---------------------------------------------------------------- R4-11
def test_r4_11_a_listed_holiday_with_a_holding_is_not_an_alert_a_minute(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    s = sym('2026-11-03', 22850, 'CE')
    holding(rig, symbol=s, expiry='2026-11-03')
    default_premiums(market, '2026-11-03')
    rig.restart()
    rig.engine.session_expected = False              # the calendar is right: today is closed
    rig.start(T(TUE, '09:00:00'))
    market.stale = set(market.books)
    n0 = len(rig.sent)
    t = T(TUE, '09:15:00')
    for i in range(0, 60 * 60, 60):
        rig.go(t + timedelta(seconds=i))
    assert rig.engine.closed_today == T(TUE, '09:15:00').date()
    assert not [x for x in rig.sent[n0:] if 'nothing has traded today' in x]


# ---------------------------------------------------------------- R4-12 / R4-13
def test_r4_12_kill_or_arm_outrank_exits_only_for_the_abandon_rule(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market, armed=False)
    rig.engine.entries_enabled = False                # exits-only AND unarmed AND kill
    rig.store.kill().update({'tripped': True, 'reason': 'x', 'loud': False})
    trigger_ce(rig, when=T(MON, '10:00:00'))
    assert state(rig)['status'] == 'closed' and state(rig)['never_filled'] and not buys(rig)
    rig.store.kill().update({'tripped': False, 'reason': None})
    rig.store.save()
    rig.restart(armed=True)
    rig.start(T(MON, '14:30:00'))
    rig.market.set(CE_MON, 152.0, bid=151.9, ask=152.1)
    rig.go(T(MON, '14:30:01'))
    assert not buys(rig)                              # the 10:00 trigger is gone


def test_r4_13_one_failed_arm_read_does_not_abandon_the_side(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    answers = [None, date(2099, 1, 1)]
    rig.engine._arm_reader = lambda d: answers.pop(0) if answers else date(2099, 1, 1)
    rig.engine._arm_cache = (None, None)
    trigger_ce(rig)
    assert buys(rig) and state(rig)['status'] == 'pending'   # re-read once: armed, BUY sent
    rig.go(T(MON, '10:00:01'))
    assert state(rig)['status'] == 'open'


# ---------------------------------------------------------------- R4-14 / R4-15 / R4-16
def test_r4_14_offline_commands_never_pretend_a_broker_call(wired, capsys):  # noqa: F811 - fixture from test_live_cli
    _real_record(wired['live'])
    s = LiveStore(wired['live'])
    s.data['positions'][0]['live'].update({
        'state': 'EXIT_OPEN', 'leg': {'kind': 'EXIT', 'tag': 'T1', 'order_no': 'REAL123', 'qty': 65,
                                      'limit': '70.00', 'sent_at': live_cli.now_ist().isoformat()}})
    s.save(force=True)
    assert cli.main(['live-book-external', '1', '--price', '68', '--yes']) == live_cli.EXIT_REFUSED
    out = capsys.readouterr().out
    assert 'NO ENGINE IS RUNNING' in out and 'cancel requested' not in out
    assert (wired['live'] / 'BOOK_EXTERNAL_1.json').exists()        # kept for the loop
    assert LiveStore(wired['live']).data['positions'][0]['live'].get('owner_pending') is None
    s = LiveStore(wired['live'])
    p = s.data['positions'][0]
    p['status'] = 'pending'
    p['live'].update({'state': 'ENTRY_OPEN', 'leg': {
        'kind': 'ENTRY', 'tag': 'T1', 'order_no': 'REAL123', 'qty': 65, 'limit': '153.80',
        'sent_at': live_cli.now_ist().isoformat()}})
    s.save(force=True)
    assert cli.main(['live-confirm-held', '1', '--yes']) == live_cli.EXIT_REFUSED
    assert 'NO ENGINE IS RUNNING' in capsys.readouterr().out
    assert LiveStore(wired['live']).data['positions'][0]['status'] == 'pending'
    assert (wired['live'] / 'CONFIRM_HELD_1').exists()


def test_r4_15_rehearsal_owner_commands_address_the_rehearsal_book(wired, monkeypatch, tmp_path, capsys):  # noqa: F811 - fixture from test_live_cli
    monkeypatch.delenv('BTST_LIVE_STATE_DIR')
    monkeypatch.setattr(live_cli.config_mod, 'APP_DIR', tmp_path)
    _real_record(tmp_path / 'state_rehearsal')
    _set_execution(wired, 'rehearsal')
    assert cli.main(['live-confirm-held', '1', '--yes']) == 0
    assert 'CONFIRMED #1' in capsys.readouterr().out
    assert cli.main(['live-book-external', '1', '--price', '60', '--yes']) == 0
    assert LiveStore(tmp_path / 'state_rehearsal').data['positions'][0]['status'] == 'closed'


def test_r4_16_the_clis_own_tmp_file_is_never_a_request(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    (rig.dir / 'CONFIRM_HELD_1.tmp').write_text(stamped(1, CE_MON, MON))
    rig.service(T(MON, '11:00:00'))
    assert not rig.store.owner_confirmed_held(1, T(MON, '11:00:00').date())
    assert not (rig.dir / 'CONFIRM_HELD_1.tmp.result').exists()


# ---------------------------------------------------------------- R4-17
def test_r4_17_a_bad_price_is_refused_before_anything_is_cancelled(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    with pytest.raises(ValueError, match='finite'):
        rig.engine.book_external(1, T(MON, '11:00:01'), float('nan'))
    assert rig.broker.orders[0]['ordSt'] == 'open' and state(rig)['live'].get('owner_pending') is None


def test_r4_17b_a_fill_during_the_cancel_is_never_booked_as_sold(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    rig.broker.cancel_faults = [('reject', '1006', 'too late')]
    with pytest.raises(ValueError, match='cancel requested'):
        rig.engine.book_external(1, T(MON, '10:00:01'), 150.0)
    rig.broker.hold_fills = False                      # the BUY fills
    rig.service(T(MON, '10:00:02'))
    p = state(rig)
    assert rig.broker.true_net(CE_NEO) == LOT and p['status'] == 'open'
    assert any('FILLED during the cancel' in s and 'NOT applied' in s for s in rig.sent)
    assert not any('read terminal with no fill' in s for s in rig.sent)


# ---------------------------------------------------------------- R4-18
def test_r4_18_the_digest_never_calls_an_abandoned_side_bought(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market, armed=False)
    trigger_ce(rig)
    assert state(rig)['never_filled'] and not buys(rig)
    text = rig.engine.eod_digest(T(MON, '10:00:00').date())
    assert ': BOUGHT' not in text and 'TRIGGERED, NOT BOUGHT' in text
    assert 'entered 0 (filled)' in text and 'never filled today' in text


# ---------------------------------------------------------------- R4-20
@pytest.mark.parametrize('status', [400, 500])
def test_r4_20_a_documented_refusal_is_a_refusal_whatever_the_status(status):
    http = FakeHTTP()
    t = nc.NeoTrader(CREDS, 'https://mis.example', 5, 5, session=http, totp_fn=lambda s: '0')
    login_ok(http)
    t.login()
    http.script[nc.PATH_PLACE] = R(status, {'stat': 'Not_Ok', 'emsg': 'unauthorized', 'stCode': 100008})
    with pytest.raises(nc.NeoRejected):
        t.place(symbol='s', token='1', side='S', qty=65, price='1.00', tag='t',
                segment='nse_fo', product='NRML')


# ---------------------------------------------------------------- R4-21
def test_r4_21_an_unknown_leg_does_not_save_the_book_every_tick(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.visibility_lag_sec = 10000.0
    rig.broker.place_faults = ['timeout_lost']
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    t0 = T(MON, '11:00:00')
    rig.go(t0)
    s0 = rig.store.saves
    for i in range(1, 61):
        rig.service(t0 + timedelta(seconds=i))
    assert rig.store.saves - s0 <= 5


def test_r4_21b_a_known_order_absent_from_the_book_does_not_save_every_tick(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    rig.broker.blank_book = True                      # the book shows nothing all along
    t0 = T(MON, '10:00:00')
    rig.service(t0 + timedelta(seconds=1))
    s0 = rig.store.saves
    for i in range(2, 62):
        rig.service(t0 + timedelta(seconds=i))
    assert rig.store.saves - s0 <= 5


# ---------------------------------------------------------------- R4-04
def test_r4_04_live_ignores_the_paper_overlay_and_a_record_keeps_its_exit_time(wired, monkeypatch):  # noqa: F811 - fixture from test_live_cli
    ov = wired['tmp'] / 'paper.json'
    ov.write_text(json.dumps({'next_day_exit': '10:00:00', 'market_close': '14:00:00'}))
    monkeypatch.setattr(live_cli.config_mod, 'OVERLAY_FILE', ov)
    cfg, _, _ = live_cli._load()
    assert str(cfg.next_day_exit) == '09:25:00' and str(cfg.market_close) != '14:00:00'
    wired['overlay'].write_text(json.dumps({'execution': 'rehearsal', 'next_day_exit': '10:00:00'}))
    cfg2, _, _ = live_cli._load()
    assert str(cfg2.next_day_exit) == '10:00:00'       # live's OWN overlay may move it


def test_r4_04b_an_open_record_keeps_the_exit_time_it_was_filled_under(cfg, tmp_path, market):
    from dataclasses import replace
    drifted = replace(cfg, next_day_exit=T(MON, '10:00:00').time())
    rig = marked(drifted, tmp_path, market)
    s = sym('2026-11-03', 22850, 'CE')
    holding(rig, symbol=s, expiry='2026-11-03')
    state(rig)['exit_rule_time'] = '09:25:00'          # frozen at the fill, before the edit
    rig.store.save(force=True)
    default_premiums(market, '2026-11-03')
    rig.market.set(s, 170.0, bid=169.8, ask=170.2)
    rig.restart()
    rig.start(T(TUE, '09:00:00'))
    rig.go(T(TUE, '09:25:01'))
    assert sells(rig)                                  # 09:25, not the edited 10:00


def test_r4_04c_a_fill_freezes_the_exit_time_on_the_record(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    trigger_ce(rig)
    rig.go(T(MON, '10:00:01'))
    assert state(rig)['status'] == 'open' and state(rig)['exit_rule_time'] == '09:25:00'


# ---------------------------------------------------------------- M35 / R4-23
def test_m35_the_request_is_written_atomically(wired, monkeypatch):  # noqa: F811 - fixture from test_live_cli
    _real_record(wired['live'])

    def boom(self, target):
        raise OSError('power cut before the rename')
    monkeypatch.setattr(Path, 'replace', boom)
    with pytest.raises(OSError):
        cli.main(['live-confirm-held', '1', '--yes'])
    assert not (wired['live'] / 'CONFIRM_HELD_1').exists()        # never a torn request
    assert (wired['live'] / 'CONFIRM_HELD_1.tmp').exists()


def test_r4_23_the_confirmation_text_no_longer_promises_one_sell(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    (rig.dir / 'CONFIRM_HELD_1').write_text(stamped(1, CE_MON, MON, action='confirm_held'))
    rig.service(T(MON, '11:00:00'))
    res = json.loads((rig.dir / 'CONFIRM_HELD_1.result').read_text())
    assert res['applied'] and 'one SELL' not in res['text'] and 'voided' in res['text']
    assert threading.active_count() >= 1 and logging.getLogger(__name__)   # keep the imports honest
