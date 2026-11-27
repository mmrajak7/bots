"""kotak_neo client + orders + symbols against FakeNeo (no network).

The properties that protect money:
  * a refused order is NEVER re-sent and NEVER triggers a login;
  * the outcome of an order comes from the BOOK by tag, not from the reply;
  * the exit chase never has two sells working and never sells more than held;
  * one bot per contract; an unreadable ownership file places nothing.
"""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kotak_neo import client as C        # noqa: E402
from kotak_neo import orders as O        # noqa: E402
from kotak_neo import session as S       # noqa: E402
from kotak_neo import symbols as Y       # noqa: E402
from kotak_neo.fake import FakeNeo       # noqa: E402

SYM, TOK = 'RELIANCE26DEC1540CE', '144284'
SESS = S.Session(3, '2026-11-27', 'c', 'https://cis.kotaksecurities.com', 'T', 'SID', None,
                 'CK', 'U')


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(S, 'HOME', tmp_path / 'neo')
    return tmp_path / 'neo'


@pytest.fixture
def neo(home):
    f = FakeNeo()
    f.set_quote(SYM, 10.0, 10.2)
    return f


class Refresh:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    def __call__(self, gen, who):
        self.calls.append((gen, who))
        return S.Result(self.outcome, SESS, 'scripted')


def client(neo, bot='zbr', refresh=None):
    return C.NeoClient(bot, http=neo, current_fn=lambda: SESS,
                       refresh_fn=refresh or Refresh(S.STILL_ALIVE))


def buy(c, qty=500, price='10.20', kind='L', trigger='0', side='B'):
    tag = c.new_tag()
    return c.place(C.OrderRequest(SYM, TOK, side, qty, price, tag, kind, trigger)), tag


def places(neo):
    return [x for x in neo.calls if x[1].endswith('/place')]


# ── client: reads ─────────────────────────────────────────────────────────────

def test_the_measured_empty_book_is_empty(neo):
    c = client(neo)
    assert c.order_book() == [] and c.trades() == [] and c.positions() == []


def test_an_unknown_not_ok_read_is_unavailable_never_empty(neo, monkeypatch):
    c = client(neo)
    monkeypatch.setattr(neo, 'get', lambda url, **kw: C_resp(200, {'stat': 'Not_Ok',
                                                                     'stCode': 5000,
                                                                     'errMsg': 'OMS down'}))
    with pytest.raises(C.Unavailable, match='OMS down'):
        c.order_book()


def C_resp(code, body):
    from kotak_neo.fake import _Resp
    return _Resp(code, body)


def test_a_dead_read_asks_the_shared_refresh_once_and_retries_on_adopt(neo):
    r = Refresh(S.ADOPTED)
    c = client(neo, refresh=r)
    neo.dead_reads(1)
    assert c.order_book() == []
    assert r.calls == [(3, 'zbr')]                 # the generation the failed call used


@pytest.mark.parametrize('outcome,exc', [(S.STILL_ALIVE, C.Unavailable),
                                         (S.COOLDOWN, C.Unavailable),
                                         (S.BLOCKED, C.Auth), (S.HOST_REFUSED, C.Auth)])
def test_a_dead_read_that_refresh_cannot_fix(neo, outcome, exc):
    c = client(neo, refresh=Refresh(outcome))
    neo.dead_reads(1)
    with pytest.raises(exc):
        c.order_book()


def test_funds(neo):
    assert client(neo).funds() == 50000.0


# ── client: mutations ──────────────────────────────────────────────────────────

def test_place_returns_the_number_and_the_tag_is_echoed(neo):
    c = client(neo)
    no, tag = buy(c)
    [o] = c.order_book()
    assert o.order_no == no and o.tag == tag and o.bot() == 'zbr' and o.status == 'complete'


def test_a_401_on_place_is_unclear_never_a_login_never_resent(neo):
    r = Refresh(S.LOGGED_IN)
    c = client(neo, refresh=r)
    neo.unclear(placed=False)
    with pytest.raises(C.Unclear):
        buy(c)
    assert r.calls == [] and len(places(neo)) == 1


@pytest.mark.parametrize('kw,msg', [
    (dict(kind='SL-M'), 'SL-M'), (dict(kind='SL', trigger='0'), 'trigger'),
    (dict(qty=0), 'positive'), (dict(side='X'), 'side')])
def test_invalid_orders_are_refused_before_neo(neo, kw, msg):
    c = client(neo)
    with pytest.raises(ValueError, match=msg):
        buy(c, **kw)
    assert places(neo) == []


def test_a_foreign_tag_is_refused(neo):
    c = client(neo)
    with pytest.raises(ValueError, match='must start with'):
        c.place(C.OrderRequest(SYM, TOK, 'B', 500, '10.20', 'btst-1'))


def test_a_definite_rejection_releases_a_new_claim(neo):
    c = client(neo)
    neo.reject(1004, 'Insufficient balance.')
    with pytest.raises(C.Rejected) as ei:
        buy(c)
    assert ei.value.code == '1004' and C.owner_of(SYM) is None


def test_cancel_success_shape_and_unknown_order(neo):
    c = client(neo)
    no, _ = buy(c, price='9.00')                  # rests below the ask
    c.cancel(no)                                  # {result, stat Ok}: a success
    assert c.order_book()[0].status == 'cancelled'
    with pytest.raises(C.Unclear, match='1018'):
        c.cancel('1')


# ── ownership ──────────────────────────────────────────────────────────────────

def test_one_bot_per_contract(neo):
    a, b = client(neo, 'zbr'), client(neo, 'btst')
    buy(a)
    with pytest.raises(C.NotOwner, match='zbr'):
        buy(b)
    assert len(places(neo)) == 1
    a.release(SYM)
    buy(b)
    assert C.owner_of(SYM) == 'btst'


def test_an_unreadable_owner_file_places_nothing(neo, home):
    home.mkdir(parents=True, exist_ok=True)
    (home / 'owners.json').write_text('{broken', encoding='utf-8')
    with pytest.raises(C.Unavailable, match='owners.json'):
        buy(client(neo))
    assert places(neo) == []


# ── resolve ────────────────────────────────────────────────────────────────────

def test_resolve_filled(neo):
    c = client(neo)
    _, tag = buy(c)
    r = O.resolve(c, tag, wait_s=0)
    assert r.state == O.FILLED and r.filled == 500 and r.avg_price == 10.2


def test_resolve_place_accepted_then_rms_rejected(neo):
    """Measured on SL-M: an order number, then `rejected` moments later."""
    c = client(neo)
    neo.rms_reject('RMS:Blocked ... STOP LOSS MARKET ORDER NOT ALLOWED IN OPTIONS')
    no, tag = buy(c)
    assert no                                       # the reply said nothing was wrong
    r = O.resolve(c, tag, wait_s=0)
    assert r.state == O.REJECTED and 'NOT ALLOWED' in r.reason


def test_resolve_waits_through_a_visibility_lag(neo):
    c, clk = client(neo), Clock()
    neo.hide(2)
    _, tag = buy(c)
    r = O.resolve(c, tag, wait_s=5, poll_s=1, sleep=clk.sleep, clock=clk)
    assert r.state == O.FILLED


def test_resolve_absent_only_after_reading_the_book(neo):
    c, clk = client(neo), Clock()
    r = O.resolve(c, 'zbr-never', wait_s=2, poll_s=1, sleep=clk.sleep, clock=clk)
    assert r.state == O.ABSENT and r.terminal


def test_resolve_unknown_when_the_book_cannot_be_read(neo):
    c, clk = client(neo, refresh=Refresh(S.UNAVAILABLE)), Clock()
    neo.dead_reads(99)
    r = O.resolve(c, 'zbr-x', wait_s=2, poll_s=1, sleep=clk.sleep, clock=clk)
    assert r.state == O.UNKNOWN and not r.terminal


def test_an_unclear_place_that_did_go_in_is_found_by_tag(neo):
    c = client(neo)
    neo.unclear(placed=True)
    tag = c.new_tag()
    with pytest.raises(C.Unclear):
        c.place(C.OrderRequest(SYM, TOK, 'B', 500, '10.20', tag))
    assert O.resolve(c, tag, wait_s=0).state == O.FILLED


def test_cancel_of_a_filled_order_settles_from_the_book(neo):
    c = client(neo)
    no, tag = buy(c)
    r = O.cancel_and_confirm(c, no, tag, wait_s=0)
    assert r.state == O.FILLED and r.filled == 500


def test_partial_then_cancel_reports_the_final_filled_quantity(neo):
    c = client(neo)
    neo.partial(200)
    no, tag = buy(c)
    assert O.resolve(c, tag, wait_s=0, until_terminal=False).state == O.PARTIAL
    r = O.cancel_and_confirm(c, no, tag, wait_s=0)
    assert r.state == O.PARTIAL and r.terminal and r.filled == 200


# ── the exit chase ─────────────────────────────────────────────────────────────

def chase_state(qty=500):
    return O.ChaseState(bot='zbr', symbol=SYM, token=TOK, tick=0.05, remaining=qty)


def live_sells(neo):
    return [o for o in neo.placed('S') if o['ordSt'] in ('open', 'trigger pending')]


def run_chase(c, neo, st, clk, quote, steps=40, on_step=None):
    pol = O.ChasePolicy(reprice_after_s=5, confirm_wait_s=0)
    for i in range(steps):
        O.chase_exit(c, st, quote, pol, sleep=clk.sleep, clock=clk)
        assert len(live_sells(neo)) <= 1                    # never two sells working
        if on_step:
            on_step(i)
        clk.t += 5
        if st.status != 'active':
            break
    return st


def test_chase_sells_at_the_bid_when_it_is_there(neo):
    c, clk = client(neo), Clock()
    st = run_chase(c, neo, chase_state(), clk, lambda: neo.quotes[SYM])
    assert st.status == 'done' and st.sold == 500 and st.avg_exit == 10.0


def test_chase_reprices_down_a_falling_book_and_never_oversells(neo):
    c, clk = client(neo), Clock()
    neo.partial(100)
    neo.set_quote(SYM, 10.0, 10.2)
    prices = iter([9.9, 9.7, 9.5, 9.3, 9.1, 8.9, 8.7, 8.5])

    def fall(i):                     # the bid drops away from every resting sell
        try:
            b = next(prices)
            neo.set_quote(SYM, b, b + 0.2)
        except StopIteration:
            pass
    st = run_chase(c, neo, chase_state(), clk, lambda: neo.quotes[SYM], on_step=fall)
    assert st.status == 'done'
    sold = sum(int(o['fldQty']) for o in neo.placed('S'))
    assert sold == 500 == st.sold                              # exactly what was held


def test_a_gap_through_with_no_bid_waits_and_does_not_send(neo):
    c, clk = client(neo), Clock()
    st = chase_state()
    O.chase_exit(c, st, lambda: {'bid': 0, 'ask': 0.5}, O.ChasePolicy(), sleep=clk.sleep,
                 clock=clk)
    assert st.status == 'active' and 'no bid' in st.note and neo.placed('S') == []


def test_an_unclear_sell_that_went_in_is_adopted_not_doubled(neo):
    c, clk = client(neo), Clock()
    neo.set_quote(SYM, 10.0, 10.2)
    neo.unclear(placed=True)
    neo.hide(1)
    st = run_chase(c, neo, chase_state(), clk, lambda: neo.quotes[SYM])
    assert st.status == 'done' and len(neo.placed('S')) == 1 and st.sold == 500


def test_an_unclear_sell_that_never_went_in_is_resent_only_after_the_grace(neo):
    c, clk = client(neo), Clock()
    neo.unclear(placed=False)
    pol = O.ChasePolicy(reprice_after_s=5, confirm_wait_s=0, unclear_grace_s=16)
    st = chase_state()
    O.chase_exit(c, st, lambda: neo.quotes[SYM], pol, sleep=clk.sleep, clock=clk)
    assert neo.placed('S') == [] and st.working_tag
    clk.t += 5
    O.chase_exit(c, st, lambda: neo.quotes[SYM], pol, sleep=clk.sleep, clock=clk)
    assert neo.placed('S') == []                             # still inside the grace
    clk.t += 20
    for _ in range(3):
        O.chase_exit(c, st, lambda: neo.quotes[SYM], pol, sleep=clk.sleep, clock=clk)
    assert st.status == 'done' and len(neo.placed('S')) == 1


def test_chase_gives_up_to_a_human_after_max_rounds(neo):
    """A quote that keeps lying above a collapsed book: every sell rests, is
    cancelled and re-priced, and after max_rounds a human is called."""
    c, clk = client(neo), Clock()
    neo.set_quote(SYM, 1.0, 1.2)                         # the real book has collapsed
    stale = {'bid': 10.0, 'ask': 10.2}                    # the feed has not caught up
    pol = O.ChasePolicy(reprice_after_s=5, confirm_wait_s=0, max_rounds=3)
    st = chase_state()
    for _ in range(20):
        O.chase_exit(c, st, lambda: stale, pol, sleep=clk.sleep, clock=clk)
        assert len(live_sells(neo)) <= 1
        clk.t += 5
        if st.status != 'active':
            break
    assert st.status == 'needs_human' and 'rounds' in st.note
    assert len(neo.placed('S')) == 3 and st.sold == 0 and live_sells(neo) == []


def test_a_resting_sell_is_left_to_work_until_its_reprice_time(neo):
    """5-second polls on a sell that has not filled: no second sell before
    `reprice_after_s`, and the reprice cancels BEFORE it re-sends."""
    c, clk = client(neo), Clock()
    neo.set_quote(SYM, 1.0, 1.2)
    stale = {'bid': 10.0, 'ask': 10.2}
    pol = O.ChasePolicy(reprice_after_s=30, confirm_wait_s=0)
    st = chase_state()
    for _ in range(5):                                   # 5 polls inside 30 s
        O.chase_exit(c, st, lambda: stale, pol, sleep=clk.sleep, clock=clk)
        clk.t += 5
    assert len(neo.placed('S')) == 1 and len(live_sells(neo)) == 1
    clk.t += 30
    O.chase_exit(c, st, lambda: stale, pol, sleep=clk.sleep, clock=clk)
    assert [o['ordSt'] for o in neo.placed('S')] == ['cancelled', 'open']


def test_chase_state_round_trips(neo):
    st = chase_state()
    st.fills.append({'qty': 1})
    assert O.ChaseState.from_dict(json.loads(json.dumps(st.to_dict()))) == st


# ── symbols ────────────────────────────────────────────────────────────────────

HDR = ('pSymbol,pGroup,pExchSeg,pInstType,pSymbolName,pTrdSymbol,pOptionType,dTickSize,'
       'lLotSize,dStrikePrice;,pExpiryDate')


def row(tok, inst, und, ts, opt, strike, lot, exp_epoch=1483021800, tick=5):
    return '%s,XX,nse_fo,%s,%s,%s,%s,%d,%d,%d,%d' % (tok, inst, und, ts, opt, tick, lot,
                                                    int(strike * 100), exp_epoch)


CSV = '\n'.join([HDR,
                 row(144284, 'OPTSTK', 'RELIANCE', 'RELIANCE26DEC1540CE', 'CE', 1540, 500),
                 row(144285, 'OPTSTK', 'RELIANCE', 'RELIANCE26DEC1540PE', 'PE', 1540, 500),
                 row(47930, 'OPTIDX', 'NIFTY', 'NIFTY26D0126450CE', 'CE', 26450, 65,
                     exp_epoch=1482762600),
                 row(1, 'FUTSTK', 'RELIANCE', 'RELIANCE26DECFUT', 'XX', 0, 500)])


def test_stock_and_index_options_map_by_facts():
    m = Y.ScripMap(Y.parse_scrip_master(CSV))
    r = m.find('RELIANCE', date(2026, 12, 29), 1540, 'CE')      # epoch +315511200
    assert r.trading_symbol == 'RELIANCE26DEC1540CE' and r.token == '144284'
    assert r.lot_size == 500 and r.tick_size == 0.05 and r.inst_type == 'OPTSTK'
    assert m.find('NIFTY', date(2026, 12, 26), 26450, 'CE').lot_size == 65
    assert len(m) == 3                                           # the future is not an option


def test_from_kite_cross_checks_the_lot():
    m = Y.ScripMap(Y.parse_scrip_master(CSV))
    k = {'name': 'RELIANCE', 'expiry': '2026-12-29', 'strike': 1540.0,
         'instrument_type': 'PE', 'lot_size': 500}
    assert m.from_kite(k).trading_symbol == 'RELIANCE26DEC1540PE'
    with pytest.raises(Y.ScripMasterError, match='lot size'):
        m.from_kite(dict(k, lot_size=250))


def test_two_rows_for_one_key_is_fatal():
    dup = CSV + '\n' + row(999, 'OPTSTK', 'RELIANCE', 'RELIANCE26DEC1540CE', 'CE', 1540, 500)
    with pytest.raises(Y.ScripMasterError, match='2 NEO rows'):
        Y.ScripMap(Y.parse_scrip_master(dup)).find('RELIANCE', date(2026, 12, 29), 1540, 'CE')


def test_load_downloads_once_a_day_into_the_shared_cache(home):
    class Src:
        n = 0

        def scrip_master_paths(self):
            return ['https://x/nse_cm.csv', 'https://x/nse_fo.csv']

        def fetch_text(self, url):
            Src.n += 1
            assert 'nse_fo' in url
            return CSV
    m1 = Y.load(Src(), today=date(2026, 11, 27))
    m2 = Y.load(Src(), today=date(2026, 11, 27))
    assert Src.n == 1 and len(m1) == len(m2) == 3
