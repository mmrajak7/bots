"""Go-live review of kotak_neo by the BTST session (2026-11-28), findings K1,
K5, K8, K9, K10, K12. Each test fails on the code before the fix.

  K1  orders refused as "session dead" while reads work -> an `orders_auth` alert
  K5  an IP refusal is never a dead session: no login, the static-IP alert
  K8  a read's refresh waits at most the client's refresh_wait_s for the lock
  K9  claim() does not hold the owners lock across the positions read
  K10 net_held() counts this client's product row only; an unknown label is unknown
  K12 an unknown underlying does not delete the good shared scrip cache
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
    (None, False)])
def test_k5_ip_refusal_reading(body, ip):
    assert E.is_ip_refusal(body) is ip


def test_k5_session_ip_text_alone_is_not_a_dead_session():
    assert not E.is_session_dead(200, IP_BODY)
    assert E.is_session_dead(200, {'stat': 'Not_Ok', 'errMsg': 'Invalid session'})


@pytest.mark.parametrize('status', [200, 400, 401])
def test_k5_an_ip_refused_read_alerts_and_never_refreshes(neo, home, monkeypatch, status):
    r = Refresh(S.LOGGED_IN)
    c = mk(neo, refresh=r)
    monkeypatch.setattr(neo, 'get', lambda url, **kw: _Resp(status, IP_BODY))
    with pytest.raises(C.Unavailable, match='IP refused'):
        c.order_book()
    assert r.calls == []                               # no login asked for
    assert [a['kind'] for a in alerts(home)] == ['static_ip']


class _VerifyHTTP:
    def __init__(self, resp):
        self.resp = resp

    def post(self, url, **kw):
        return self.resp


def test_k5_an_ip_refused_verify_is_unavailable_and_alerts_without_flushing(home, flushed):
    cfg = dict(S.DEFAULTS)
    v = S._network_verify(SESS, cfg, http=_VerifyHTTP(_Resp(401, IP_BODY)))
    assert v == S.UNAVAILABLE                          # was DEAD -> a login
    assert [a['kind'] for a in alerts(home)] == ['static_ip']
    assert flushed == []                               # under the lock: _run delivers it


def test_k5_an_ip_refused_place_is_alerted_and_not_counted_as_orders_auth(neo, home,
                                                                          monkeypatch):
    c = mk(neo)
    monkeypatch.setattr(neo, 'post', lambda url, **kw: _Resp(401, IP_BODY))
    for _ in range(3):
        with pytest.raises(C.Unclear):
            buy(c)
    kinds = [a['kind'] for a in alerts(home)]
    assert kinds == ['static_ip'] and not (home / 'orders_auth.json').exists()


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
    assert not C.note_orders_auth('etf', 'place', 401, 3, now=1000.0)
    assert not C.note_orders_auth('btst', 'cancel', 401, 3, now=1000.0 + 301)  # first aged out
    assert C.note_orders_auth('btst', 'cancel', 401, 3, now=1000.0 + 302)
    assert not C.note_orders_auth('etf', 'place', 401, 3, now=1000.0 + 600)    # rate-limited
    assert not C.note_orders_auth('etf', 'place', 401, 3, now=1000.0 + 302 + 901)  # alone again
    assert C.note_orders_auth('etf', 'place', 401, 3, now=1000.0 + 302 + 902)
    assert [a['kind'] for a in alerts(home)] == ['orders_auth', 'orders_auth']
    assert 'btst' in alerts(home)[0]['text']


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
    _positions(neo, monkeypatch, {'prod': 'INTRADAY'})
    with pytest.raises(C.Unavailable, match='INTRADAY'):
        mk(neo).net_held(SYM)
    assert mk(neo).net_held(SYM, product=None) == 65


def test_k10_a_row_without_a_label_is_counted(neo, monkeypatch):
    _positions(neo, monkeypatch, {'prod': ''}, {'prod': 'NRML'})
    assert mk(neo).net_held(SYM) == 65                 # MIS client: the unlabelled row only
    assert mk(neo, product=' mis ').net_held(SYM) == 65


# -- K9: no owners lock across the network read ---------------------------------------------

def test_k9_claim_reads_positions_outside_the_owners_lock(neo, monkeypatch):
    btst = mk(neo, 'btst', True, 'NRML')
    real, free = btst.positions, []

    def positions():
        with S.file_lock('owners.lock', 0.0) as got:
            free.append(got)
        return real()

    monkeypatch.setattr(btst, 'positions', positions)
    buy(btst)
    assert free == [True] and C.owner_of(SYM) == 'btst'


def test_k9_a_claim_taken_during_the_read_wins(neo, monkeypatch):
    btst = mk(neo, 'btst', True, 'NRML')
    real = btst.positions

    def positions():
        C.assign(SYM, 'zbr')                           # another bot claimed meanwhile
        return real()

    monkeypatch.setattr(btst, 'positions', positions)
    with pytest.raises(C.NotOwner, match='zbr'):
        buy(btst)
    assert neo.placed() == []


def test_k9_an_existing_own_claim_is_not_new(neo):
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
    with pytest.raises(Y.ScripMasterError, match='cached file itself is fine'):
        Y.load(src, today=date(2026, 11, 30), min_rows=1, underlyings=['NOPE'])
    assert src.n == 1 and list(home.glob('scrip_nse_fo_*.csv'))
    assert len(Y.load(src, today=date(2026, 11, 30), min_rows=1, underlyings=['NIFTY'])) == 5
    assert src.n == 1


def test_k12_a_bad_cache_is_still_replaced(home):
    S._ensure_home()
    (home / 'scrip_nse_fo_20261130.csv').write_text('garbage\n', encoding='utf-8')
    src = Src()
    assert len(Y.load(src, today=date(2026, 11, 30), min_rows=1, underlyings=['NIFTY'])) == 5
    assert src.n == 1
