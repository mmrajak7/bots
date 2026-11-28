"""Go-live review of kotak_neo by the BTST session (2026-11-28), findings K1,
K5, K8, K9, K10, K12, and the independent review of those fixes (R1-R13).
Most tests fail on the code before the fix; the rest are regression guards
for a property the fix must keep (said in the test).

  K1  orders refused as "session dead" while reads work -> an `orders_auth` alert
      (R5: 30-min window, one generation)
  K5  a WHITELIST IP refusal is never a dead session: no login, the static-IP alert;
      R1: a SESSION-IP refusal (1037, session made from another IP) IS dead -- a
      login from here is its only fix -- and alerts too
  K8  a read's refresh waits at most the client's refresh_wait_s for the lock;
      R3: a confirmed DEAD is cached, R4: hot-path alerts never block the caller
  K9  claim() does not hold the owners lock across the positions read
  K10 net_held() counts this client's product row only; an unknown label is unknown
  K12 an unknown underlying does not delete the good shared scrip cache (R10: one
      re-download a day first)
"""
import json
import sys
import time
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kotak_neo import client as C        # noqa: E402
from kotak_neo import envelope as E      # noqa: E402
from kotak_neo import session as S       # noqa: E402
from kotak_neo import symbols as Y       # noqa: E402
from kotak_neo.fake import FakeNeo, _Resp  # noqa: E402

SYM, TOK = 'NIFTY26DEC26000CE', '45001'
SESS = S.Session(3, '2026-11-27', 'c', 'https://cis.kotaksecurities.com', 'T', 'SID', None,
                 'CK', 'U')
IP_BODY = {'stat': 'Not_Ok', 'stCode': 1037, 'errMsg': 'session ip doesnt match with reqest ip'}
WL_BODY = {'stat': 'Not_Ok', 'stCode': 100008, 'errMsg': 'unauthorized'}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(S, 'HOME', tmp_path / 'neo')
    return tmp_path / 'neo'


@pytest.fixture
def flushed(monkeypatch):
    sent = []
    monkeypatch.setattr(S, '_flush_alerts', lambda: sent.append(1))
    return sent


@pytest.fixture
def neo(home, flushed):
    f = FakeNeo()
    f.set_quote(SYM, 100.0, 100.5)
    return f


class Refresh:
    def __init__(self, outcome=S.STILL_ALIVE):
        self.outcome, self.calls = outcome, []

    def __call__(self, gen, who, **kw):
        self.calls.append((gen, who, kw))
        return S.Result(self.outcome, SESS, 'scripted')


def mk(neo, bot='etf', exclusive=False, product='MIS', refresh=None, **kw):
    return C.NeoClient(bot, http=neo, exclusive=exclusive, product=product,
                       current_fn=lambda: SESS, refresh_fn=refresh or Refresh(), **kw)


def buy(c, qty=65, price='100.50'):
    return c.place(C.OrderRequest(SYM, TOK, 'B', qty, price, c.new_tag()))


def alerts(home):
    p = home / 'alerts.jsonl'
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding='utf-8').splitlines()]


# -- K5: an IP refusal is not a dead session ---------------------------------------------

@pytest.mark.parametrize('body,ip', [
    (IP_BODY, True),
    ({'stat': 'Not_Ok', 'stCode': 100008, 'errMsg': 'unauthorized'}, True),
    ({'stat': 'Not_Ok', 'errMsg': 'Static IP not registered'}, True),
    ({'stat': 'Not_Ok', 'stCode': 1003, 'errMsg': 'Invalid session'}, False),
    ({'stat': 'Ok', 'errMsg': 'whitelist'}, False),
    ({'stat': 'Not_Ok', 'errMsg': 'scrip not in whitelist'}, False),      # R9: no 'ip'
    ({'stat': 'Not_Ok', 'errMsg': 'IP not in whitelist'}, True),
    (None, False)])
def test_k5_ip_refusal_reading(body, ip):
    assert E.is_ip_refusal(body) is ip


def test_r1_the_two_ip_kinds_are_told_apart():
    assert E.is_session_ip_refusal(IP_BODY) and not E.is_ip_whitelist_refusal(IP_BODY)
    assert E.is_ip_whitelist_refusal(WL_BODY) and not E.is_session_ip_refusal(WL_BODY)


def test_k5_session_ip_text_alone_is_not_a_dead_session():
    assert not E.is_session_dead(200, IP_BODY)
    assert E.is_session_dead(200, {'stat': 'Not_Ok', 'errMsg': 'Invalid session'})


@pytest.mark.parametrize('status', [200, 400, 401])
def test_k5_a_whitelist_refused_read_alerts_and_never_refreshes(neo, home, monkeypatch, status):
    r = Refresh(S.LOGGED_IN)
    c = mk(neo, refresh=r)
    monkeypatch.setattr(neo, 'get', lambda url, **kw: _Resp(status, WL_BODY))
    with pytest.raises(C.Unavailable, match='IP refused'):
        c.order_book()
    assert r.calls == []                               # no login asked for
    assert [a['kind'] for a in alerts(home)] == ['static_ip']


@pytest.mark.parametrize('status', [200, 400, 401])
def test_r1_a_session_ip_read_refreshes_once_and_alerts(neo, home, monkeypatch, status):
    r = Refresh(S.LOGGED_IN)
    c = mk(neo, refresh=r)
    real, n = neo.get, []

    def get(url, **kw):
        n.append(1)
        return _Resp(status, IP_BODY) if len(n) == 1 else real(url, **kw)

    monkeypatch.setattr(neo, 'get', get)
    assert c.order_book() == []                        # the new session from this IP reads
    assert len(r.calls) == 1
    assert [a['kind'] for a in alerts(home)] == ['static_ip']


class _VerifyHTTP:
    def __init__(self, resp):
        self.resp = resp

    def post(self, url, **kw):
        return self.resp


def test_k5_a_whitelist_refused_verify_is_unavailable_and_alerts_without_flushing(home,
                                                                                  flushed):
    cfg = dict(S.DEFAULTS)
    v = S._network_verify(SESS, cfg, http=_VerifyHTTP(_Resp(401, WL_BODY)))
    assert v == S.UNAVAILABLE                          # was DEAD -> a login
    assert [a['kind'] for a in alerts(home)] == ['static_ip']
    assert flushed == []                               # under the lock: _run delivers it


@pytest.mark.parametrize('status', [200, 401])
def test_r1_a_session_ip_verify_says_so_so_a_login_can_fix_it(home, flushed, status):
    v = S._network_verify(SESS, dict(S.DEFAULTS), http=_VerifyHTTP(_Resp(status, IP_BODY)))
    assert v == S.SESSION_IP                           # F1: _decide_locked gates the login
    assert [a['kind'] for a in alerts(home)] == ['static_ip'] and flushed == []


def test_r1_run_delivers_the_alert_the_verify_queued(home, flushed):
    S._ensure_home()                                   # no session, no LOGIN_HOST: no network
    res = S.refresh(0, 'etf', _verify=lambda s: S.UNAVAILABLE)
    assert res.outcome in (S.UNAVAILABLE, S.FAILED, S.HOST_REFUSED, S.ERROR, S.COOLDOWN)
    assert flushed                                     # _run flushed after the lock


def test_k5_an_ip_refused_place_is_alerted_and_not_counted_as_orders_auth(neo, home,
                                                                          monkeypatch):
    c = mk(neo)
    monkeypatch.setattr(neo, 'post', lambda url, **kw: _Resp(401, IP_BODY))
    for _ in range(3):
        with pytest.raises(C.Unclear):
            buy(c)
    kinds = [a['kind'] for a in alerts(home)]
    assert kinds == ['static_ip'] and not (home / 'orders_auth.json').exists()


@pytest.mark.parametrize('status', [200, 401, 403])
def test_r8_a_whitelist_refused_place_is_rejected_definitely(neo, home, monkeypatch, status):
    c = mk(neo)
    monkeypatch.setattr(neo, 'post', lambda url, **kw: _Resp(status, WL_BODY))
    with pytest.raises(C.Rejected):                    # 401/403 was Unclear before
        buy(c)
    assert not (home / 'orders_auth.json').exists()


def test_r8_a_session_ip_refused_place_stays_unclear(neo, monkeypatch):
    c = mk(neo)
    monkeypatch.setattr(neo, 'post', lambda url, **kw: _Resp(200, IP_BODY))
    with pytest.raises(C.Unclear):
        buy(c)


# -- K8: the refresh wait is the client's ------------------------------------------------

def test_k8_refresh_wait_is_passed_when_set_and_omitted_when_not(neo):
    r1, r2 = Refresh(S.ADOPTED), Refresh(S.ADOPTED)
    neo.dead_reads(1)
    mk(neo, refresh=r1, refresh_wait_s=2).order_book()
    neo.dead_reads(1)
    mk(neo, refresh=r2).order_book()
    assert r1.calls == [(3, 'etf', {'wait_s': 2.0})]
    assert r2.calls == [(3, 'etf', {})]               # old callers' refresh_fn(gen, who) still fit


def test_k8_negative_wait_is_refused():
    with pytest.raises(ValueError, match='refresh_wait_s'):
        C.NeoClient('etf', refresh_wait_s=-1)


def test_k8_a_busy_refresh_lock_costs_the_read_its_wait_not_75_seconds(neo, home):
    c = C.NeoClient('etf', http=neo, exclusive=False, product='MIS', current_fn=lambda: SESS,
                    refresh_fn=S.refresh, refresh_wait_s=0.3)
    neo.dead_reads(1)
    with S.file_lock('refresh.lock', 1.0) as got:     # another bot is refreshing
        assert got
        t = time.time()
        with pytest.raises(C.Unavailable, match='LOCK_TIMEOUT'):
            c.order_book()
        assert time.time() - t < 3.0
    log = (home / 'refresh_log.jsonl').read_text(encoding='utf-8')
    assert 'LOCK_TIMEOUT' in log


# -- K1: orders refused as session-dead alert ---------------------------------------------

def test_k1_two_auth_refused_mutations_alert_once(neo, home, flushed):
    c = mk(neo)
    for _ in range(3):
        neo.unclear(placed=False)
        with pytest.raises(C.Unclear):
            buy(c)
    kinds = [a['kind'] for a in alerts(home)]
    assert kinds == ['orders_auth'] and len(flushed) == 1
    assert 'NEO app' in alerts(home)[0]['text']


def test_k1_one_refusal_is_not_an_alert_and_old_ones_age_out(home, flushed):
    w = C.ORDERS_AUTH_WINDOW_SEC
    assert not C.note_orders_auth('etf', 'place', 401, 3, now=1000.0)
    assert not C.note_orders_auth('btst', 'cancel', 401, 3, now=1000.0 + w + 1)  # first aged out
    assert C.note_orders_auth('btst', 'cancel', 401, 3, now=1000.0 + w + 2)
    assert not C.note_orders_auth('etf', 'place', 401, 3, now=1000.0 + w + 600)  # rate-limited
    t = 1000.0 + w + 2 + C.ORDERS_AUTH_EVERY_SEC
    assert C.note_orders_auth('etf', 'place', 401, 3, now=t)    # 600 and t still in the window
    assert [a['kind'] for a in alerts(home)] == ['orders_auth', 'orders_auth']
    assert 'btst' in alerts(home)[0]['text']


def test_r5_two_entries_ten_minutes_apart_alert(home, flushed):
    assert not C.note_orders_auth('etf', 'place', 401, 3, now=1000.0)
    assert C.note_orders_auth('etf', 'place', 401, 3, now=1600.0)       # was a 5-min window


def test_r5_refusals_of_an_older_generation_do_not_count(home, flushed):
    assert not C.note_orders_auth('etf', 'place', 401, 3, now=1000.0)
    assert not C.note_orders_auth('etf', 'place', 401, 4, now=1010.0)   # a refresh in between
    assert C.note_orders_auth('etf', 'place', 401, 4, now=1020.0)


def test_k1_a_corrupt_counter_file_never_raises(home, flushed):
    S._ensure_home()
    (home / 'orders_auth.json').write_text('{not json', encoding='utf-8')
    assert not C.note_orders_auth('etf', 'place', 401, 3, now=5.0)
    assert C.note_orders_auth('etf', 'place', 401, 3, now=6.0)


# -- K10: product rows ----------------------------------------------------------------------

def test_k10_net_held_counts_only_this_clients_product(neo):
    etf, btst = mk(neo), mk(neo, 'btst', True, 'NRML')
    buy(btst)
    buy(etf)
    assert etf.net_held(SYM) == 65 and btst.net_held(SYM) == 65
    assert etf.net_held(SYM, product=None) == 130
    assert etf.net_held(SYM, product='nrml ') == 65


def test_k10_btst_can_claim_a_strike_etf_holds_in_mis(neo):
    etf, btst = mk(neo), mk(neo, 'btst', True, 'NRML')
    buy(etf)
    buy(btst)                                          # was NotOwner: "account holds 65"
    assert C.owner_of(SYM) == 'btst'


def _positions(neo, monkeypatch, *rows):
    body = {'stat': 'Ok', 'data': [dict({'trdSym': SYM, 'flBuyQty': '65', 'flSellQty': '0',
                                         'cfBuyQty': '0', 'cfSellQty': '0'}, **r) for r in rows]}
    monkeypatch.setattr(neo, 'get', lambda url, **kw: _Resp(200, body))


def test_k10_an_undocumented_product_label_is_unknown_never_zero(neo, monkeypatch):
    _positions(neo, monkeypatch, {'prod': 'XYZ'})
    with pytest.raises(C.Unavailable, match='XYZ'):
        mk(neo).net_held(SYM)
    assert mk(neo).net_held(SYM, product=None) == 65


def test_k10_a_row_without_a_label_is_counted(neo, monkeypatch):
    _positions(neo, monkeypatch, {'prod': ''}, {'prod': 'NRML'})
    assert mk(neo).net_held(SYM) == 65                 # MIS client: the unlabelled row only
    assert mk(neo, product=' mis ').net_held(SYM) == 65


# -- K9: no owners lock across the network read ---------------------------------------------

def test_k9_claim_reads_positions_outside_the_owners_lock(neo, monkeypatch):
    btst = mk(neo, 'btst', True, 'NRML')
    real, free = btst.positions_for, []

    def positions_for(symbol):
        with S.file_lock('owners.lock', 0.0) as got:
            free.append(got)
        return real(symbol)

    monkeypatch.setattr(btst, 'positions_for', positions_for)
    buy(btst)
    assert free == [True] and C.owner_of(SYM) == 'btst'


def test_k9_a_claim_taken_during_the_read_wins(neo, monkeypatch):
    btst = mk(neo, 'btst', True, 'NRML')
    real = btst.positions_for

    def positions_for(symbol):
        C.assign(SYM, 'zbr')                           # another bot claimed meanwhile
        return real(symbol)

    monkeypatch.setattr(btst, 'positions_for', positions_for)
    with pytest.raises(C.NotOwner, match='zbr'):
        buy(btst)
    assert neo.placed() == []


def test_k9_an_existing_own_claim_is_not_new(neo):            # regression guard
    btst = mk(neo, 'btst', True, 'NRML')
    assert btst.claim(SYM) is True
    assert btst.claim(SYM) is False


# -- K12: the shared scrip cache -----------------------------------------------------------

def _row(tok, und, ts, opt, strike, lot=75, exp_epoch=1483021800):
    return ','.join(str(x) for x in (tok, 'OPTIDX', und, ts, opt, strike * 100, lot,
                                     exp_epoch, 5))


HDR = 'pSymbol,pInstType,pSymbolName,pTrdSymbol,pOptionType,dStrikePrice;,lLotSize,pExpiryDate,dTickSize'
CSV = '\n'.join([HDR] + [_row(1000 + i, 'NIFTY', 'NIFTY26DEC%dCE' % (26000 + i), 'CE', 26000 + i)
                         for i in range(5)])


class Src:
    def __init__(self, text=CSV):
        self.n, self.text = 0, text

    def scrip_master_paths(self):
        return ['https://x/nse_fo.csv']

    def fetch_text(self, url):
        self.n += 1
        return self.text


def test_k12_an_unknown_underlying_keeps_the_good_cache(home):
    src = Src()
    Y.load(src, today=date(2026, 11, 30), min_rows=1)
    with pytest.raises(Y.ScripMasterError, match='no option rows'):
        Y.load(src, today=date(2026, 11, 30), min_rows=1, underlyings=['NOPE'])
    assert src.n == 2                                  # R10: one re-download today
    with pytest.raises(Y.ScripMasterError, match='re-downloaded once today'):
        Y.load(src, today=date(2026, 11, 30), min_rows=1, underlyings=['NOPE'])
    assert src.n == 2 and list(home.glob('scrip_nse_fo_*.csv'))
    assert len(Y.load(src, today=date(2026, 11, 30), min_rows=1, underlyings=['NIFTY'])) == 5
    assert src.n == 2


def test_r10_a_cache_lacking_the_underlying_is_refetched_once(home):
    S._ensure_home()
    other = '\n'.join([HDR] + [_row(2000 + i, 'BANKNIFTY', 'BANKNIFTY26DEC%dCE' % (50000 + i),
                                     'CE', 50000 + i) for i in range(5)])
    (home / 'scrip_nse_fo_20261130.csv').write_text(other, encoding='utf-8')
    src = Src()                                        # today's real file has NIFTY
    assert len(Y.load(src, today=date(2026, 11, 30), min_rows=1, underlyings=['NIFTY'])) == 5
    assert src.n == 1


def test_r10_a_failed_refetch_keeps_the_good_cache(home):
    src = Src()
    Y.load(src, today=date(2026, 11, 30), min_rows=1)
    src.text = 'garbage'
    with pytest.raises(Y.ScripMasterError):
        Y.load(src, today=date(2026, 11, 30), min_rows=1, underlyings=['NOPE'])
    assert len(Y.load(src, today=date(2026, 11, 30), min_rows=1, underlyings=['NIFTY'])) == 5


# -- R3 / R4 -----------------------------------------------------------------------------

def test_r3_a_confirmed_dead_is_not_reverified_inside_the_cache_window():
    calls, sleeps = [], []
    S._dead_cache.clear()
    cfg = dict(S.DEFAULTS)
    verify = lambda s: (calls.append(1), S.DEAD)[1]   # noqa: E731
    assert S._verified(SESS, verify, cfg, sleeps.append) == S.DEAD
    assert S._verified(SESS, verify, cfg, sleeps.append) == S.DEAD
    assert len(calls) == 2 and len(sleeps) == 1         # one confirmed verdict, then cached


def test_r3_alive_clears_the_dead_cache(monkeypatch):
    S._dead_cache[SESS.generation] = time.time() - 1000    # expired
    assert S._verified(SESS, lambda s: S.ALIVE, dict(S.DEFAULTS), lambda x: None) == S.ALIVE
    assert SESS.generation not in S._dead_cache


def test_r4_an_alert_from_a_hot_path_does_not_block_the_caller(home, monkeypatch):
    import threading
    monkeypatch.setattr(S, 'ASYNC_ALERTS', True)
    gate, entered, done = threading.Event(), threading.Event(), []

    def slow_flush():
        entered.set()
        gate.wait(5)                                   # Telegram hanging
        done.append(1)

    monkeypatch.setattr(S, '_flush_alerts', slow_flush)
    t = time.time()
    S.flush_alerts_soon()
    assert entered.wait(5)
    S.flush_alerts_soon()                              # coalesced into the running thread
    assert time.time() - t < 0.5
    gate.set()
    for th in list(S._pending_flush):
        th.join(5)
    assert done == [1, 1] and S._pending_flush == []   # one more pass for the 2nd request


def test_k12_a_bad_cache_is_still_replaced(home):            # regression guard
    S._ensure_home()
    (home / 'scrip_nse_fo_20261130.csv').write_text('garbage\n', encoding='utf-8')
    src = Src()
    assert len(Y.load(src, today=date(2026, 11, 30), min_rows=1, underlyings=['NIFTY'])) == 5
    assert src.n == 1


# -- second review of the round-2 fixes (F1-F7) ------------------------------------------

from kotak_neo.tests.test_session import CREDS, Login, fake_creds, publish  # noqa: E402


@pytest.fixture
def pi(home, monkeypatch, flushed):
    """A HOME where logins are allowed (LOGIN_HOST), floors off, cooldown 0."""
    import socket
    S._ensure_home()
    (home / 'LOGIN_HOST').write_text(socket.gethostname(), encoding='utf-8')
    monkeypatch.setattr(S, 'FLOORS', {})
    monkeypatch.setattr(S, 'DEFAULTS', dict(S.DEFAULTS, cooldown_sec=0.0, verify_confirm_sec=0.0))
    S._alive_cache.clear()
    return home


def _refresh(gen, verify, login):
    return S.refresh(gen, 'etf', _verify=verify, _login=login, _creds=fake_creds,
                     _sleep=lambda s: None)


def test_f1_a_session_ip_verdict_allows_one_login_a_day(pi):
    publish(1)
    lg = Login()
    sess_ip = lambda s: S.SESSION_IP                    # noqa: E731  1037 that a login won't cure
    assert _refresh(1, sess_ip, lg).outcome == S.LOGGED_IN
    for g in (2, 2, 2):
        res = _refresh(g, sess_ip, lg)
        assert res.outcome == S.UNAVAILABLE and 'already ran' in res.detail
    assert lg.calls == 1 and S.current().generation == 2   # no churn of the shared session
    S.unblock('owner')                                      # the owner fixed the IP
    assert _refresh(2, sess_ip, lg).outcome == S.LOGGED_IN and lg.calls == 2


def test_f2_a_login_refused_for_the_ip_never_latches(pi):
    publish(1)
    lg = Login('ip', 'ip', 'ip', 'ip', 'ip')
    for _ in range(5):
        st = S.read_state()
        st['next_allowed_at'] = 0.0                        # skip the long cooldown in the test
        S._write_state(st)
        res = _refresh(1, lambda s: S.DEAD, lg)
        assert res.outcome == S.FAILED and res.detail.startswith('ip:')
    st = S.read_state()
    assert not st['latched'] and st['consecutive_refusals'] == 0
    assert 'static_ip' in [a['kind'] for a in alerts(pi)]


def test_f2_the_long_cooldown_follows_an_ip_refused_login(pi):
    publish(1)
    _refresh(1, lambda s: S.DEAD, Login('ip'))
    res = _refresh(1, lambda s: S.DEAD, Login())
    assert res.outcome == S.COOLDOWN and res.retry_after > 600


def test_f2_the_login_reads_an_ip_refusal_as_kind_ip(monkeypatch):
    monkeypatch.setattr(S, 'clock_synced', lambda: True)

    class H:
        def post(self, url, **kw):
            return _Resp(400, WL_BODY)

    with pytest.raises(S.LoginFailed) as ei:
        S._network_login(dict(CREDS), dict(S.DEFAULTS), http=H())
    assert ei.value.kind == 'ip'


def test_f3_a_failed_refetch_does_not_use_up_the_days_attempt(home):
    src = Src()
    Y.load(src, today=date(2026, 11, 30), min_rows=1)

    class Down(Src):
        def fetch_text(self, url):
            raise Y.ScripMasterError('download: ConnectTimeout')

    with pytest.raises(Y.ScripMasterError, match='ConnectTimeout'):
        Y.load(Down(), today=date(2026, 11, 30), min_rows=1, underlyings=['NOPE'])
    with pytest.raises(Y.ScripMasterError, match='no option rows'):
        Y.load(src, today=date(2026, 11, 30), min_rows=1, underlyings=['NOPE'])
    assert src.n == 2                                      # the retry still re-downloaded


def test_f3_one_bots_filter_does_not_use_up_anothers_attempt(home):
    src = Src()
    Y.load(src, today=date(2026, 11, 30), min_rows=1)
    for _ in range(2):
        with pytest.raises(Y.ScripMasterError):
            Y.load(src, today=date(2026, 11, 30), min_rows=1, underlyings=['TYPO'])
    n = src.n
    with pytest.raises(Y.ScripMasterError):
        Y.load(src, today=date(2026, 11, 30), min_rows=1, underlyings=['BANKNIFTY'])
    assert src.n == n + 1


@pytest.mark.parametrize('body,session_ip,whitelist', [
    ({'stat': 'Not_Ok', 'stCode': 100008, 'errMsg': 'Session IP is not whitelisted'}, False, True),
    ({'stat': 'Not_Ok', 'errMsg': 'non-whitelisted IPs are blocked'}, False, True),
    ({'stat': 'Not_Ok', 'errMsg': 'ipaddress not in whitelist'}, False, True),
    ({'stat': 'Not_Ok', 'errMsg': 'IP address not in whitelist'}, False, True),
    ({'stat': 'Not_Ok', 'errMsg': 'tip: whitelist the scrip'}, False, False)])
def test_f4_the_never_login_code_wins_and_ip_words_are_found(body, session_ip, whitelist):
    assert E.is_session_ip_refusal(body) is session_ip
    assert E.is_ip_whitelist_refusal(body) is whitelist


def test_f5_after_a_failed_refresh_reads_fail_fast_on_that_generation(neo):
    r = Refresh(S.COOLDOWN)
    c = mk(neo, refresh=r)
    neo.dead_reads(100)
    with pytest.raises(C.Unavailable, match='COOLDOWN'):
        c.order_book()
    n_http = len(neo.calls)
    for _ in range(5):
        with pytest.raises(C.Unavailable, match='is dead'):
            c.order_book()
    assert len(neo.calls) == n_http and len(r.calls) == 1  # no HTTP, no refresh, no log


def test_f5_a_new_generation_is_read_at_once(neo):
    sess = {'s': SESS}
    c = C.NeoClient('etf', http=neo, exclusive=False, product='MIS',
                    current_fn=lambda: sess['s'], refresh_fn=Refresh(S.COOLDOWN))
    neo.dead_reads(1)
    with pytest.raises(C.Unavailable):
        c.order_book()
    sess['s'] = SESS._replace(generation=4)                 # another process logged in
    assert c.order_book() == []


def test_f5_still_alive_does_not_hold(neo):
    c = mk(neo, refresh=Refresh(S.STILL_ALIVE))
    neo.dead_reads(1)
    with pytest.raises(C.Unavailable):
        c.order_book()
    assert c.order_book() == []                             # a blip: the next read works


def test_f6_a_flush_that_raises_does_not_silence_later_alerts(monkeypatch):
    monkeypatch.setattr(S, 'ASYNC_ALERTS', True)
    calls = []

    def boom():
        calls.append(1)
        raise KeyboardInterrupt('simulated BaseException in a pass')

    monkeypatch.setattr(S, '_flush_alerts', boom)
    S.flush_alerts_soon()
    for th in list(S._pending_flush):
        th.join(5)
    assert S._pending_flush == []
    monkeypatch.setattr(S, '_flush_alerts', lambda: calls.append(2))
    S.flush_alerts_soon()
    for th in list(S._pending_flush):
        th.join(5)
    assert calls == [1, 2]


def test_f7_other_generations_are_kept_not_erased(home, flushed):
    assert not C.note_orders_auth('etf', 'place', 401, 3, now=1000.0)
    assert not C.note_orders_auth('btst', 'place', 401, 4, now=1001.0)
    assert C.note_orders_auth('zbr', 'cancel', 401, 3, now=1002.0)   # gen 3's first was kept
    st = json.loads((home / 'orders_auth.json').read_text(encoding='utf-8'))
    assert sorted(e['gen'] for e in st['events']) == [3, 3, 4]


# -- zebra review N1/R3 + BTST X-K5: per-symbol reads ---------------------------------------
# One unparsable row of ANOTHER contract (a manual order, another bot) must not
# blind a read about THIS one; a bad row on THIS symbol still fails closed.

OTHER = 'BANKNIFTY26DEC50000PE'


def _book(neo, monkeypatch, orders=(), positions=()):
    ob = {'stat': 'Ok', 'data': list(orders)}
    pos = {'stat': 'Ok', 'data': list(positions)}

    def get(url, **kw):
        return _Resp(200, pos if url.endswith(C.PATH_POSITIONS) else ob)
    monkeypatch.setattr(neo, 'get', get)


def _order(sym, qty='65', fld='0', st='open', side='S', tag='zbr-1'):
    return {'nOrdNo': '9%s' % len(sym), 'ordSt': st, 'trdSym': sym, 'trnsTp': side,
            'qty': qty, 'fldQty': fld, 'avgPrc': '0', 'prc': '10.00', 'GuiOrdId': tag}


def _pos(sym, buy='65'):
    return {'trdSym': sym, 'prod': 'NRML', 'flBuyQty': buy, 'flSellQty': '0',
            'cfBuyQty': '0', 'cfSellQty': '0'}


def test_a_bad_row_of_another_contract_does_not_blind_live_orders(neo, monkeypatch):
    _book(neo, monkeypatch, orders=[_order(SYM), _order(OTHER, qty='--')])
    c = mk(neo, 'zbr', True, 'NRML')
    assert [o.symbol for o in c.live_orders(SYM, 'S')] == [SYM]
    assert [o.symbol for o in c.orders_for(SYM)] == [SYM]
    with pytest.raises(C.Unavailable):
        c.order_book()                                 # the whole-book read stays strict


def test_a_bad_row_on_the_same_contract_still_raises(neo, monkeypatch):
    _book(neo, monkeypatch, orders=[_order(SYM, fld='')])
    with pytest.raises(C.Unavailable):
        mk(neo, 'zbr', True, 'NRML').live_orders(SYM, 'S')


def test_a_bad_position_row_of_another_contract_does_not_blind_net_held(neo, monkeypatch):
    _book(neo, monkeypatch, positions=[_pos(SYM), _pos(OTHER, buy='x')])
    c = mk(neo, 'zbr', True, 'NRML')
    assert c.net_held(SYM) == 65
    assert [p.symbol for p in c.positions_for(SYM)] == [SYM]
    with pytest.raises(C.Unavailable):
        c.positions()
    _book(neo, monkeypatch, positions=[_pos(SYM, buy='x')])
    with pytest.raises(C.Unavailable):
        c.net_held(SYM)


def test_row_symbol_matches_the_views(neo):
    r = _order(SYM)
    assert C.row_symbol(r) == C.OrderView.from_neo(r).symbol
    assert C.row_symbol({'sym': SYM}) == SYM and C.row_symbol({}) == ''
