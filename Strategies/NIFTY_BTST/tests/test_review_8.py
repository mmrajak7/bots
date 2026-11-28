"""Review 8 (2026-11-28, trader / architect / flow): the BTST fixes.

R8-1 login brake (F2/A2/A3)   R8-2 exit sends failing -> SELL IN APP (T8/A4)
R8-3 no quotes + exit due     R8-4 any fill resets the rejection streak (F7)
R8-5 own settled send is not an orphan (F1/F1b)   R8-6 `prb-` is another bot (A9)
R8-7 unclear-SELL alert names the double-sell risk (F3)

The F1/F1b/F2/F7 cases are the reviewer's reproductions, turned round: each
asserted the bug, these assert it is gone (and the negative controls show the
guard still bites where it must)."""
import json
from datetime import timedelta

import pytest

from conftest import LOT, T
from live_helpers import MON, TUE, Rig
from test_live_engine import CE_MON, CE_NEO, PE_MON, holding, marked, state, trigger_ce

from nifty_btst import neo_client as nc
from nifty_btst.broker import NeoBroker, OrderView, in_scope
from nifty_btst.journal import Journal
from nifty_btst.live_engine import LiveEngine
from nifty_btst.neo_client import LoginBrake, NeoAuthError, NeoReader, NeoTrader
from nifty_btst.live_cli import _brake_path as REAL_BRAKE_PATH   # bound before conftest patches

CREDS = {'consumer_key': 'k', 'mobile_number': '+910000000000', 'ucc': 'U',
         'mpin': '0000', 'totp_secret': 'JBSWY3DPEHPK3PXP'}


def _sells(rig):
    return [r for r in rig.broker.sent if r.side == 'S']


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._b, self.text = code, body, json.dumps(body)

    def json(self):
        return self._b


class _LoginHttp:
    """Answers tradeApiLogin / tradeApiValidate from a script of
    ('ok' | 'mpin' | 'refused' | 'network' | 'ip')."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.login_posts = 0

    def post(self, url, **kw):
        import requests
        if 'tradeApiLogin' in url:
            self.login_posts += 1
            self.cur = self.outcomes.pop(0) if self.outcomes else self.cur
            if self.cur == 'network':
                raise requests.ConnectionError('down')
            if self.cur == 'refused':
                return _Resp(401, {'message': 'Invalid TOTP'})
            if self.cur == 'ip':
                return _Resp(400, {'stat': 'Not_Ok', 'stCode': 100008,
                                   'errMsg': 'request from non-whitelisted static ip'})
            return _Resp(200, {'data': {'token': 't', 'sid': 's'}})
        if 'tradeApiValidate' in url:
            if self.cur == 'mpin':
                return _Resp(200, {'data': None, 'errMsg': 'Invalid MPIN'})
            return _Resp(200, {'data': {'token': 'T', 'sid': 'S', 'baseUrl': 'https://b'}})
        raise AssertionError(url)

    def get(self, url, **kw):
        raise AssertionError('no read in these tests: ' + url)


class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def _reader(http, brake):
    return NeoReader(CREDS, 'https://x', 5, 5, session=http, totp_fn=lambda s: '1', brake=brake)


# ------------------------------------------------------------- R8-1 brake
def test_a_refused_mpin_latches_the_day_and_nothing_more_is_sent(tmp_path):
    clock = _Clock()
    http = _LoginHttp('mpin', 'ok')
    r = _reader(http, LoginBrake(tmp_path / 'b.json', clock=clock, today=lambda: '2026-12-01'))
    with pytest.raises(NeoAuthError, match='MPIN'):
        r.login()
    clock.t += 3600                                       # an hour later: still latched
    with pytest.raises(NeoAuthError, match='LATCHED'):
        r.login()
    assert http.login_posts == 1                          # the 2nd attempt sent nothing


def test_three_refusals_latch_with_a_5_minute_wait_between(tmp_path):
    clock = _Clock()
    http = _LoginHttp('refused', 'refused', 'refused', 'ok')
    r = _reader(http, LoginBrake(tmp_path / 'b.json', clock=clock, today=lambda: '2026-12-01'))
    with pytest.raises(NeoAuthError, match='Invalid TOTP'):
        r.login()
    clock.t += 60                                         # the engine's 60 s retry
    with pytest.raises(NeoAuthError, match='next attempt in 240s'):
        r.login()
    assert http.login_posts == 1
    for _ in range(2):
        clock.t += nc.LOGIN_REFUSAL_BACKOFF_SEC
        with pytest.raises(NeoAuthError):
            r.login()
    assert http.login_posts == 3
    clock.t += 10 * nc.LOGIN_REFUSAL_BACKOFF_SEC
    with pytest.raises(NeoAuthError, match='LATCHED'):
        r.login()
    assert http.login_posts == 3


def test_network_and_ip_failures_never_count_toward_the_latch(tmp_path):
    clock = _Clock()
    http = _LoginHttp('network', 'ip', 'network', 'ip', 'network', 'ok')
    b = LoginBrake(tmp_path / 'b.json', clock=clock, today=lambda: '2026-12-01')
    r = _reader(http, b)
    for _ in range(5):
        with pytest.raises(NeoAuthError):
            r.login()
        clock.t += 60
    assert b.state['refusals'] == 0 and not b.state['latched']
    r.login()
    assert r.ready and http.login_posts == 6


def test_a_success_clears_the_refusal_count(tmp_path):
    clock = _Clock()
    http = _LoginHttp('refused', 'refused', 'ok', 'refused', 'refused', 'ok')
    b = LoginBrake(tmp_path / 'b.json', clock=clock, today=lambda: '2026-12-01')
    r = _reader(http, b)
    for _ in range(6):
        try:
            r.login()
        except NeoAuthError:
            pass
        clock.t += nc.LOGIN_REFUSAL_BACKOFF_SEC
    assert not b.state['latched'] and r.ready


def test_the_latch_survives_a_restart_and_the_owner_unblocks_by_deleting_the_file(tmp_path):
    path = tmp_path / 'neo_login.json'
    clock = _Clock()
    day = lambda: '2026-12-01'                            # noqa: E731
    with pytest.raises(NeoAuthError):
        _reader(_LoginHttp('mpin'), LoginBrake(path, clock=clock, today=day)).login()
    http = _LoginHttp('ok')
    r2 = _reader(http, LoginBrake(path, clock=clock, today=day))     # a new process
    with pytest.raises(NeoAuthError, match='LATCHED.*delete'):
        r2.login()
    assert http.login_posts == 0
    path.unlink()                                         # the owner, after fixing the MPIN
    r2.login()
    assert r2.ready
    assert [e['outcome'] for e in json.loads(path.read_text())['logins']] == ['attempt', 'ok']


def test_a_new_day_starts_unlatched(tmp_path):
    path, clock, day = tmp_path / 'b.json', _Clock(), {'d': '2026-12-01'}
    b = LoginBrake(path, clock=clock, today=lambda: day['d'])
    with pytest.raises(NeoAuthError):
        _reader(_LoginHttp('mpin'), b).login()
    day['d'] = '2026-12-02'
    r = _reader(_LoginHttp('ok'), b)
    r.login()
    assert r.ready


def test_the_attempt_is_written_before_the_wire(tmp_path):
    path = tmp_path / 'b.json'
    seen = {}

    class Spy(_LoginHttp):
        def post(self, url, **kw):
            if 'tradeApiLogin' in url:
                seen['attempts'] = json.loads(path.read_text())['attempts']
            return super().post(url, **kw)

    _reader(Spy('ok'), LoginBrake(path)).login()
    assert seen['attempts'] == 1


def test_no_login_host_means_nothing_is_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(nc, 'login_host_ok', lambda: False)
    http = _LoginHttp('ok')
    with pytest.raises(NeoAuthError, match='may not log in'):
        _reader(http, LoginBrake(tmp_path / 'b.json')).login()
    assert http.login_posts == 0


def test_an_unsynced_clock_means_nothing_is_sent_and_nothing_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(nc, 'clock_synced', lambda: False)
    http = _LoginHttp('ok')
    b = LoginBrake(tmp_path / 'b.json')
    with pytest.raises(NeoAuthError, match='NTP'):
        _reader(http, b).login()
    assert http.login_posts == 0 and b.state['refusals'] == 0
    monkeypatch.setattr(nc, 'clock_synced', lambda: True)
    _reader(http, b).login()
    assert http.login_posts == 1


def test_login_host_reads_the_kotak_neo_file(tmp_path, monkeypatch):
    import socket
    f = tmp_path / 'LOGIN_HOST'
    monkeypatch.setattr(nc, 'LOGIN_HOST_FILE', f)
    assert nc._login_host_ok() is False                   # missing
    f.write_text('not-this-box\n')
    assert nc._login_host_ok() is False
    f.write_text(socket.gethostname() + '\n')
    assert nc._login_host_ok() is True


def test_f2_engine_with_a_refused_mpin_logs_in_once_not_all_day(cfg, tmp_path, market):
    """The reviewer's reproduction: 6 h of 1 s service ticks against a login
    NEO refuses at the MPIN step made >= 300 logins. Now: 1."""
    rig = Rig(cfg, tmp_path, market)
    holding(rig, at_broker=False)
    http = _LoginHttp('mpin')
    trader = NeoTrader(CREDS, 'https://x', 5, 5, session=http,
                       brake=LoginBrake(tmp_path / 'b.json'))
    broker = NeoBroker(trader, 'nse_fo', 'NRML', 'Net')
    eng = LiveEngine(cfg, rig.neo, rig.store, market, rig.notifier, broker, Journal(rig.dir),
                     lambda: None, arm_reader=lambda d: None)
    t0 = T(TUE, '09:15:00')
    for s in range(0, 6 * 3600, 5):
        eng.service_orders(t0 + timedelta(seconds=s))
    assert http.login_posts == 1
    assert any('LATCHED' in m for m in rig.notifier.sent)


def test_build_broker_gives_every_btst_process_one_shared_brake_file(monkeypatch, tmp_path):
    from nifty_btst import live_cli
    monkeypatch.setenv('BTST_LIVE_STATE_DIR', str(tmp_path / 'sl'))
    assert REAL_BRAKE_PATH() == tmp_path / 'sl' / live_cli.LOGIN_BRAKE_FILE
    assert live_cli._brake_path() != REAL_BRAKE_PATH()   # conftest keeps tests off it


def test_no_test_can_reach_the_real_neo(tmp_path):
    """The rail: a reader built WITHOUT a fake session (as zebra's test did,
    making a real login from the Pi on 28-Nov) cannot send anything."""
    r = NeoReader(CREDS, 'https://x', 5, 5, brake=LoginBrake(tmp_path / 'b.json'))
    with pytest.raises(AssertionError, match='REAL NEO'):
        r.login()


# ------------------------------------------------- R8-2 exit sends failing
def test_two_failed_exit_sends_tell_the_owner_to_sell_in_the_app(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig, entry_date='2026-10-23')
    rig.engine.reconcile_ok = True
    rig.broker.place_faults = [('reject', '1009', 'RMS: blocked'), ('reject', '1009', 'RMS: blocked')]
    rig.market.set(CE_MON, 120.0, bid=119.8, ask=120.2)
    rig.go(T(MON, '09:25:01'))
    assert not any('SELL IN THE APP NOW' in m for m in rig.notifier.sent)    # one is not a pattern
    for s in range(4, 20, 3):
        rig.go(T(MON, '09:25:00') + timedelta(seconds=s))
        if len(_sells(rig)) >= 2:
            break
    assert any('NEO did not take our last 2 SELLs' in m and 'SELL IN THE APP NOW' in m
               for m in rig.notifier.sent)


def test_a_placed_sell_resets_the_failure_count(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig, entry_date='2026-10-23')
    rig.engine.reconcile_ok = True
    rig.broker.place_faults = [('reject', '1009', 'RMS: blocked')]
    rig.market.set(CE_MON, 120.0, bid=119.8, ask=120.2)
    rig.go(T(MON, '09:25:01'))
    assert state(rig)['live']['exit_send_failures'] == 1
    for s in range(4, 12):
        rig.go(T(MON, '09:25:00') + timedelta(seconds=s))
    assert state(rig)['status'] == 'closed'
    assert state(rig)['live']['exit_send_failures'] == 0
    assert not any('SELL IN THE APP NOW' in m for m in rig.notifier.sent)


def test_entry_failures_never_raise_the_exit_alarm(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.place_faults = ['timeout_lost', 'timeout_lost']
    trigger_ce(rig)
    assert not state(rig)['live'].get('exit_send_failures')


# --------------------------------------------- R8-3 no quotes + exit due
def test_no_quotes_with_an_exit_due_says_sell_in_the_app_now(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig, entry_date='2026-10-23')
    p = state(rig)
    p['live']['exit_reason'] = 'next_day_0925'
    rig.engine._outage_watch(T(MON, '09:26:00'), 'no Kite token')
    msg = [m for m in rig.notifier.sent if m.lstrip().startswith('NO QUOTES')][-1]
    assert 'CANNOT SELL' in msg and 'SELL IN THE APP NOW' in msg and 'live-book-external 1' in msg


def test_no_quotes_before_the_exit_is_due_is_not_a_sell_order(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.engine._outage_watch(T(MON, '11:00:00'), 'quotes failing (3 polls)')
    msg = [m for m in rig.notifier.sent if m.lstrip().startswith('NO QUOTES')][-1]
    assert 'no exit is due yet' in msg and 'SELL IN THE APP NOW' not in msg


# ------------------------------------------- R8-4 rejection streak (F7)
def test_f7_an_exit_fill_resets_the_rejection_streak(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig, entry_date='2026-10-23')
    rig.engine.reconcile_ok = True
    rig.broker.place_faults = [('reject', '1009', 'RMS: price outside the band')]
    rig.market.set(CE_MON, 120.0, bid=119.8, ask=120.2)
    rig.go(T(MON, '09:25:01'))
    rig.go(T(MON, '09:25:04'))
    rig.go(T(MON, '09:25:05'))
    assert state(rig)['status'] == 'closed'
    rig.broker.place_faults = [('reject', '1009', 'RMS: insufficient margin')]
    rig.market.set(PE_MON, 150.0, bid=149.8, ask=150.2)
    rig.go(T(MON, '14:00:00'))
    assert not rig.store.kill().get('tripped')


def test_negative_control_two_rejections_in_a_row_still_trip(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig, entry_date='2026-10-23')
    rig.engine.reconcile_ok = True
    rig.broker.place_faults = [('reject', '1009', 'RMS: a'), ('reject', '1009', 'RMS: b')]
    rig.market.set(CE_MON, 120.0, bid=119.8, ask=120.2)
    for s in range(1, 12, 3):
        rig.go(T(MON, '09:25:00') + timedelta(seconds=s))
    assert '2 consecutive rejections' in str(rig.store.kill().get('reason'))


# ------------------------------------- R8-5 own settled send (F1 / F1b)
def test_f1_a_trade_settled_unclear_exit_is_journalled_and_no_restart_kill(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.engine.reconcile_ok = True
    rig.broker.place_faults = ['timeout_reached']        # reached NEO, answer lost
    rig.broker.blank_book = True                         # the book glitches: Ok, empty
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)     # through the stop
    rig.go(T(MON, '11:00:00'))
    for i in range(1, 100):
        rig.service(T(MON, '11:00:00') + timedelta(seconds=i))
        if state(rig)['status'] == 'closed':
            break
    assert state(rig)['status'] == 'closed'
    tag = _sells(rig)[0].tag
    assert tag not in [u['tag'] for u in rig.journal.unresolved()]
    rig.broker.blank_book = False
    rig.restart()
    rig.start(T(MON, '11:05:00'))
    assert not rig.store.kill().get('tripped'), rig.store.kill()


def test_f1b_confirm_held_on_an_unclear_buy_then_restart_binds_not_kills(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.place_faults = ['timeout_reached']
    rig.broker.blank_book = True
    trigger_ce(rig)
    rig.engine.confirm_held(1, T(MON, '10:01:00'))
    assert state(rig)['status'] == 'open'
    rig.broker.blank_book = False
    rig.restart()
    rig.start(T(MON, '10:05:00'))
    assert not rig.store.kill().get('tripped'), rig.store.kill()
    tag = state(rig)['live']['leg_history'][-1]['tag']      # journalled at the confirm
    assert tag not in [u['tag'] for u in rig.journal.unresolved()]


def test_negative_control_a_working_order_under_a_settled_tag_still_trips(cfg, tmp_path, market):
    """Bound silently only when the order is FINAL: a WORKING order nobody
    tracks must still trip the loud kill."""
    rig = marked(cfg, tmp_path, market)
    rig.broker.place_faults = ['timeout_reached']
    rig.broker.blank_book = True
    rig.broker.hold_fills = True                         # the BUY rests, unfilled
    trigger_ce(rig)
    rig.engine.confirm_held(1, T(MON, '10:01:00'))
    rig.broker.blank_book = False
    rig.restart()
    rig.start(T(MON, '10:05:00'))
    k = rig.store.kill()
    assert k.get('tripped') and k.get('loud') and 'UNTRACKED' in k['reason'], k


def test_an_orphan_that_is_a_settled_final_order_is_bound_not_killed(cfg, tmp_path, market):
    """The belt behind the journalling: a settled leg whose BOUND line is
    missing (an older journal) and whose FINAL order filled exactly what the
    record booked is bound with a note, never a loud kill."""
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.engine.reconcile_ok = True
    rig.broker.place_faults = ['timeout_reached']
    rig.broker.blank_book = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    for i in range(1, 100):
        rig.service(T(MON, '11:00:00') + timedelta(seconds=i))
        if state(rig)['status'] == 'closed':
            break
    tag = _sells(rig)[0].tag
    lines = [ln for ln in rig.journal.path.read_text().splitlines()
             if not ('"BOUND"' in ln and tag in ln)]
    rig.journal.path.write_text(''.join(ln + chr(10) for ln in lines))
    rig.broker.blank_book = False
    rig.restart()
    rig.start(T(MON, '11:05:00'))
    assert not rig.store.kill().get('tripped'), rig.store.kill()
    assert any('already settled on #1' in m for m in rig.notifier.sent)


def test_negative_control_a_fill_the_record_did_not_book_still_trips(cfg, tmp_path, market):
    """A FINAL order under a settled leg's tag whose fill differs from what
    the record booked is not accounted for: loud kill, not a silent bind."""
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.engine.reconcile_ok = True
    rig.broker.place_faults = ['timeout_reached']
    rig.broker.blank_book = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    for i in range(1, 100):
        rig.service(T(MON, '11:00:00') + timedelta(seconds=i))
        if state(rig)['status'] == 'closed':
            break
    tag = _sells(rig)[0].tag
    # the journal lost the BOUND line, and the broker's row shows MORE filled
    lines = [ln for ln in rig.journal.path.read_text().splitlines()
             if not ('"BOUND"' in ln and tag in ln)]
    rig.journal.path.write_text(''.join(ln + chr(10) for ln in lines))
    for o in rig.broker.orders:
        if o['GuiOrdId'] == tag:
            o.update({'qty': str(2 * LOT), 'fldQty': str(2 * LOT)})
    rig.broker._save()
    rig.broker.blank_book = False
    rig.restart()
    rig.start(T(MON, '11:05:00'))
    k = rig.store.kill()
    assert k.get('tripped') and k.get('loud') and 'UNTRACKED' in k['reason'], k


# --------------------------------------------------- R8-6 prb is a bot
def _row(tag, product='NRML'):
    return OrderView(order_no='1', status='complete', symbol=CE_NEO, side='B', qty=LOT,
                     filled=LOT, avg_price=1.0, price='1', tag=tag, rej_reason='',
                     placed_at='', product=product)


def test_the_kotak_neo_probe_tag_is_another_bot():
    assert not in_scope(_row('prb-20261130-1'), 'NRML')
    assert in_scope(_row(''), 'NRML')                     # a human still counts
    assert in_scope(_row('prbx'), 'NRML')                 # not the bot prefix


# ------------------------------------------- R8-7 unclear SELL alert (F3)
def test_an_unclear_sell_alert_names_the_double_sell_risk(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.engine.reconcile_ok = True
    rig.broker.place_faults = ['timeout_lost']
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    rig.service(T(MON, '11:00:05'))
    assert any('is UNKNOWN' in m and 'CANCEL it there' in m and 'SHORT' in m
               for m in rig.notifier.sent)
