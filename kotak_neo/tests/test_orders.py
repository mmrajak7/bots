"""kotak_neo client + orders + symbols against FakeNeo (no network).

The properties that protect money:
  * a refused order is NEVER re-sent and NEVER triggers a login;
  * the outcome of an order comes from the BOOK by tag, not from the reply;
  * the exit chase never has two sells working, never sells more than the
    ACCOUNT holds, persists its tag before sending, and is single-stepped;
  * one bot per contract; an unreadable ownership file adds no exposure but
    never blocks an exit.
Independent review of the first cut (2026-11-27, F1-F16): each finding has a
test below that fails on the old behaviour.
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
from kotak_neo.fake import FakeNeo, _Resp  # noqa: E402

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


# -- client: reads ---------------------------------------------------------------

def test_the_measured_empty_book_is_empty(neo):
    """Measured at HTTP 200 (BTST's _read refuses non-200 before reading the
    envelope, and the 13:31 probe reached the envelope check)."""
    c = client(neo)
    assert c.order_book() == [] and c.trades() == [] and c.positions() == []


def test_an_unknown_not_ok_read_is_unavailable_never_empty(neo, monkeypatch):
    c = client(neo)
    monkeypatch.setattr(neo, 'get', lambda url, **kw: _Resp(200, {'stat': 'Not_Ok', 'stCode': 5000,
                                                                   'errMsg': 'OMS down'}))
    with pytest.raises(C.Unavailable, match='OMS down'):
        c.order_book()


def test_a_dead_read_asks_the_shared_refresh_once_and_retries_on_adopt(neo):
    r = Refresh(S.ADOPTED)
    c = client(neo, refresh=r)
    neo.dead_reads(1)
    assert c.order_book() == []
    assert r.calls == [(3, 'zbr')]


@pytest.mark.parametrize('outcome,exc', [
    (S.STILL_ALIVE, C.Unavailable), (S.COOLDOWN, C.Unavailable), (S.FAILED, C.Unavailable),
    (S.LOCK_TIMEOUT, C.Unavailable), (S.UNAVAILABLE, C.Unavailable),
    (S.BLOCKED, C.Auth), (S.HOST_REFUSED, C.Auth), (S.ERROR, C.Auth)])
def test_a_dead_read_that_refresh_cannot_fix(neo, outcome, exc):
    c = client(neo, refresh=Refresh(outcome))
    neo.dead_reads(1)
    with pytest.raises(exc):
        c.order_book()


def test_still_dead_after_a_successful_refresh_is_auth(neo):
    c = client(neo, refresh=Refresh(S.LOGGED_IN))
    neo.dead_reads(2)
    with pytest.raises(C.Auth, match='still rejected'):
        c.order_book()


def test_funds_and_unreadable_funds(neo, monkeypatch):
    c = client(neo)
    assert c.funds() == 50000.0
    monkeypatch.setattr(neo, 'post', lambda url, **kw: _Resp(200, {'stat': 'Ok'}))
    with pytest.raises(C.Unavailable):
        c.funds()


@pytest.mark.parametrize('bad', [{'fldQty': 'x'}, {'fldQty': None}, {'qty': ''},
                                 {'fldQty': '600'}])
def test_an_unreadable_order_row_is_unavailable_never_zero(neo, bad):
    """Review F5: `complete` with an unreadable fldQty read as filled 0."""
    c = client(neo)
    buy(c)
    neo.orders[0].update(bad)
    with pytest.raises(C.Unavailable):
        c.order_book()


# -- client: mutations -------------------------------------------------------------

def test_trade_rows_are_fills_not_orders(neo):
    """Measured live: a trade row has qty 0 and the fill in fldQty. Parsing it
    as an order raised 'filled 65 > qty 0' against the real account."""
    c = client(neo)
    _, tag = buy(c)
    [f] = c.trades()
    assert isinstance(f, C.FillView) and f.qty == 500 and f.price == 10.2 and f.tag == tag
    assert f.bot() == 'zbr' and f.raw['qty'] == 0


def test_a_fill_row_without_a_price_or_quantity_is_unavailable(neo, monkeypatch):
    c = client(neo)
    bad = {'stat': 'Ok', 'data': [{'nOrdNo': '1', 'fldQty': 0, 'qty': 0, 'avgPrc': '1'}]}
    monkeypatch.setattr(neo, 'get', lambda url, **kw: _Resp(200, bad))
    with pytest.raises(C.Unavailable, match='trade book'):
        c.trades()


def test_place_returns_the_number_and_the_tag_is_echoed(neo):
    c = client(neo)
    no, tag = buy(c)
    [o] = c.order_book()
    assert o.order_no == no and o.tag == tag and o.bot() == 'zbr' and o.status == 'complete'


def test_tags_differ_across_processes_in_the_same_second(neo, monkeypatch):
    """Review F3: cron and monitor (same bot) collided in one second."""
    a, b = client(neo), client(neo)
    monkeypatch.setattr(C.os, 'getpid', lambda: 1111)
    ta = a.new_tag()
    monkeypatch.setattr(C.os, 'getpid', lambda: 2222)
    tb = b.new_tag()
    assert ta != tb and ta.startswith('zbr-') and len(ta) <= 21


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
    no, _ = buy(c, price='9.00')
    c.cancel(no)
    assert c.order_book()[0].status == 'cancelled'
    with pytest.raises(C.Unclear, match='1018'):
        c.cancel('1')


# -- ownership -----------------------------------------------------------------------

def test_one_bot_per_contract_and_no_claim_on_an_unowned_position(neo):
    a, b = client(neo, 'zbr'), client(neo, 'btst')
    buy(a)
    with pytest.raises(C.NotOwner, match='zbr'):
        buy(b)
    assert len(places(neo)) == 1
    a.release(SYM)                                  # zbr lets go, but the lots are still there
    with pytest.raises(C.NotOwner, match='no owner'):
        buy(b)
    assert len(places(neo)) == 1


def test_release_if_flat_only_when_flat(neo):
    c = client(neo)
    buy(c)
    assert c.release_if_flat(SYM) is False and C.owner_of(SYM) == 'zbr'
    c.place(C.OrderRequest(SYM, TOK, 'S', 500, '10.00', c.new_tag()), reduce_only=True)
    assert c.release_if_flat(SYM) is True and C.owner_of(SYM) is None


def test_an_unreadable_owner_file_blocks_a_buy_but_never_an_exit(neo, home):
    c = client(neo)
    buy(c)
    (home / 'owners.json').write_text('{broken', encoding='utf-8')
    with pytest.raises(C.Unavailable, match='owners.json'):
        buy(c)
    c.place(C.OrderRequest(SYM, TOK, 'S', 500, '10.00', c.new_tag()), reduce_only=True)
    assert neo.net(SYM) == 0


def test_reduce_only_is_for_sells(neo):
    c = client(neo)
    with pytest.raises(ValueError, match='reduce_only'):
        c.place(C.OrderRequest(SYM, TOK, 'B', 500, '10.20', c.new_tag()), reduce_only=True)


# -- resolve -------------------------------------------------------------------------

def test_resolve_filled(neo):
    c = client(neo)
    _, tag = buy(c)
    r = O.resolve(c, tag, wait_s=0, expect_qty=500)
    assert r.state == O.FILLED and r.filled == 500 and r.avg_price == 10.2


def test_resolve_place_accepted_then_rms_rejected(neo):
    c = client(neo)
    neo.rms_reject('RMS:Blocked ... STOP LOSS MARKET ORDER NOT ALLOWED IN OPTIONS')
    no, tag = buy(c)
    assert no
    r = O.resolve(c, tag, wait_s=0)
    assert r.state == O.REJECTED and 'NOT ALLOWED' in r.reason


def test_resolve_waits_through_a_visibility_lag(neo):
    c, clk = client(neo), Clock()
    neo.hide(2)
    _, tag = buy(c)
    assert O.resolve(c, tag, wait_s=5, poll_s=1, sleep=clk.sleep, clock=clk).state == O.FILLED


def test_absent_is_not_terminal(neo):
    """Review F8: an entry that trusted ABSENT.terminal would buy twice."""
    c, clk = client(neo), Clock()
    r = O.resolve(c, 'zbr-never', wait_s=2, poll_s=1, sleep=clk.sleep, clock=clk)
    assert r.state == O.ABSENT and not r.terminal


def test_resolve_unknown_when_the_book_cannot_be_read(neo):
    c, clk = client(neo, refresh=Refresh(S.UNAVAILABLE)), Clock()
    neo.dead_reads(99)
    r = O.resolve(c, 'zbr-x', wait_s=2, poll_s=1, sleep=clk.sleep, clock=clk)
    assert r.state == O.UNKNOWN and not r.terminal


def test_one_tag_on_two_sides_is_unknown_not_a_sum(neo):
    """Review F3: a 500 buy and a 250 sell under one tag read as FILLED 750."""
    c = client(neo)
    tag = c.new_tag()
    c.place(C.OrderRequest(SYM, TOK, 'B', 500, '10.20', tag))
    c.place(C.OrderRequest(SYM, TOK, 'S', 250, '10.00', tag), reduce_only=True)
    assert O.resolve(c, tag, wait_s=0).state == O.UNKNOWN


def test_book_quantity_not_what_was_sent_is_unknown(neo):
    """Review F11: shares vs lots must never be guessed."""
    c = client(neo)
    _, tag = buy(c)
    assert O.resolve(c, tag, wait_s=0, expect_qty=1).state == O.UNKNOWN


def test_complete_with_a_short_fill_is_unknown(neo):
    c = client(neo)
    _, tag = buy(c)
    neo.orders[0]['fldQty'] = '200'
    assert O.resolve(c, tag, wait_s=0).state == O.UNKNOWN


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


# -- the exit chase ------------------------------------------------------------------

def hold(c, neo, qty=500):
    """Put `qty` on the account (a filled buy), as an entry would."""
    buy(c, qty=qty)
    assert neo.net(SYM) == qty


def chase_state(qty=500):
    return O.ChaseState(bot='zbr', symbol=SYM, token=TOK, tick=0.05, remaining=qty)


def live_sells(neo):
    return [o for o in neo.placed('S') if o['ordSt'] in ('open', 'trigger pending',
                                                         'cancel pending')]


POL = dict(reprice_after_s=5, confirm_wait_s=0)


def run_chase(c, neo, st, clk, quote, steps=60, on_step=None, **pol):
    p = O.ChasePolicy(**dict(POL, **pol))
    for i in range(steps):
        O.chase_exit(c, st, quote, p, sleep=clk.sleep, clock=clk)
        assert len(live_sells(neo)) <= 1                   # never two sells working
        assert neo.net(SYM) >= 0                           # never short
        if on_step:
            on_step(i)
        clk.t += 5
        if st.status != 'active':
            break
    return st


def test_chase_sells_at_the_bid_and_releases_the_contract(neo):
    c, clk = client(neo), Clock()
    hold(c, neo)
    st = run_chase(c, neo, chase_state(), clk, lambda: neo.quotes[SYM])
    assert st.status == 'done' and st.sold == 500 and st.avg_exit == 10.0
    assert neo.net(SYM) == 0 and C.owner_of(SYM) is None


def test_chase_reprices_down_a_falling_book_and_never_oversells(neo):
    c, clk = client(neo), Clock()
    hold(c, neo)
    neo.partial(100)
    prices = iter([9.9, 9.7, 9.5, 9.3, 9.1, 8.9, 8.7, 8.5])

    def fall(i):
        b = next(prices, None)
        if b is not None:
            neo.set_quote(SYM, b, b + 0.2)
    st = run_chase(c, neo, chase_state(), clk, lambda: neo.quotes[SYM], on_step=fall)
    assert st.status == 'done' and st.sold == 500 and neo.net(SYM) == 0


def test_chase_never_sells_more_than_the_account_holds(neo):
    """Review F4: state said 500, the account held 250 -> it went short 250."""
    c, clk = client(neo), Clock()
    hold(c, neo, qty=250)
    st = run_chase(c, neo, chase_state(500), clk, lambda: neo.quotes[SYM], max_troubles=3)
    assert neo.net(SYM) == 0 and st.sold == 250
    assert st.status == 'needs_human' and 'holds 0' in st.note


def test_unreadable_positions_send_nothing(neo):
    c, clk = client(neo, refresh=Refresh(S.UNAVAILABLE)), Clock()
    hold(c, neo)
    neo.dead_reads(99)
    st = chase_state()
    O.chase_exit(c, st, lambda: neo.quotes[SYM], O.ChasePolicy(**POL), sleep=clk.sleep, clock=clk)
    assert neo.placed('S') == [] and 'positions unreadable' in st.note and st.status == 'active'


def test_one_bad_read_does_not_strand_the_exit(neo):
    """Review F6: one failed read used to hand a resting stop to a human."""
    c, clk = client(neo, refresh=Refresh(S.UNAVAILABLE)), Clock()
    hold(c, neo)
    neo.set_quote(SYM, 1.0, 1.2)
    stale = {'bid': 10.0}
    p = O.ChasePolicy(reprice_after_s=60, confirm_wait_s=0)
    st = chase_state()
    O.chase_exit(c, st, lambda: stale, p, sleep=clk.sleep, clock=clk)       # sell rests
    neo.dead_reads(1)
    clk.t += 5
    O.chase_exit(c, st, lambda: stale, p, sleep=clk.sleep, clock=clk)       # one bad read
    assert st.status == 'active' and st.troubles == 1
    neo.set_quote(SYM, 10.0, 10.2)                                          # the book recovers
    clk.t += 5
    for _ in range(3):
        O.chase_exit(c, st, lambda: neo.quotes[SYM], p, sleep=clk.sleep, clock=clk)
    assert st.status == 'done' and st.troubles == 0 and neo.net(SYM) == 0


def test_the_tag_is_persisted_before_the_order_is_sent(neo):
    """Review F2: a crash after place lost the tag and the next run sold again."""
    c, clk = client(neo), Clock()
    hold(c, neo)
    seen = []

    def persist(st):
        if st.working_tag:
            seen.append((st.working_tag, [o['GuiOrdId'] for o in neo.placed('S')]))
    O.chase_exit(c, chase_state(), lambda: neo.quotes[SYM], O.ChasePolicy(**POL),
                 persist=persist, sleep=clk.sleep, clock=clk)
    tag, sent_then = seen[0]
    assert tag not in sent_then                      # saved while the order did not exist yet


def test_a_restarted_process_settles_the_saved_tag_and_does_not_resell(neo):
    c, clk = client(neo), Clock()
    hold(c, neo)
    neo.set_quote(SYM, 1.0, 1.2)
    saved = {}
    st = chase_state()
    O.chase_exit(c, st, lambda: {'bid': 10.0}, O.ChasePolicy(**POL),
                 persist=lambda s: saved.update(s.to_dict()), sleep=clk.sleep, clock=clk)
    st2 = O.ChaseState.from_dict(dict(saved))        # the process died; a new one loads
    neo.set_quote(SYM, 10.0, 10.2)                   # the resting sell now fills
    clk.t += 1
    O.chase_exit(c, st2, lambda: neo.quotes[SYM], O.ChasePolicy(**POL), sleep=clk.sleep, clock=clk)
    O.chase_exit(c, st2, lambda: neo.quotes[SYM], O.ChasePolicy(**POL), sleep=clk.sleep, clock=clk)
    assert len(neo.placed('S')) == 1 and neo.net(SYM) == 0 and st2.status == 'done'


def test_a_second_process_cannot_step_the_same_chase(neo):
    c, clk = client(neo), Clock()
    hold(c, neo)
    st = chase_state()
    with S.file_lock('chase_zbr_%s.lock' % SYM, 1.0) as got:
        assert got
        O.chase_exit(c, st, lambda: neo.quotes[SYM], O.ChasePolicy(**POL), sleep=clk.sleep,
                     clock=clk)
    assert neo.placed('S') == [] and 'another process' in st.note


def test_a_gap_through_with_no_bid_waits_and_does_not_send(neo):
    c, clk = client(neo), Clock()
    hold(c, neo)
    st = chase_state()
    O.chase_exit(c, st, lambda: {'bid': 0, 'ask': 0.5}, O.ChasePolicy(), sleep=clk.sleep,
                 clock=clk)
    assert st.status == 'active' and 'no bid' in st.note and neo.placed('S') == []


@pytest.mark.parametrize('hidden', [1, 8])
def test_an_unclear_sell_that_went_in_is_adopted_not_doubled(neo, hidden):
    """Review F1: hide(8) outlived the grace and the old chase sold twice."""
    c, clk = client(neo), Clock()
    hold(c, neo)
    neo.set_quote(SYM, 1.0, 1.2)                     # the unclear sell rests first
    neo.unclear(placed=True)
    neo.hide(hidden)
    st = chase_state()
    p = O.ChasePolicy(reprice_after_s=600, confirm_wait_s=0)     # default grace
    for i in range(30):
        O.chase_exit(c, st, lambda: {'bid': 10.0}, p, sleep=clk.sleep, clock=clk)
        assert len(neo.placed('S')) == 1             # never a second sell
        if i == 12:
            neo.set_quote(SYM, 10.0, 10.2)           # then it fills
        clk.t += 5
        if st.status != 'active':
            break
    assert st.status == 'done' and neo.net(SYM) == 0


def test_an_unclear_sell_that_never_went_in_is_resent_after_the_grace_only(neo):
    c, clk = client(neo), Clock()
    hold(c, neo)
    neo.unclear(placed=False)
    p = O.ChasePolicy(reprice_after_s=5, confirm_wait_s=0, unclear_grace_s=16)
    st = chase_state()
    O.chase_exit(c, st, lambda: neo.quotes[SYM], p, sleep=clk.sleep, clock=clk)
    assert neo.placed('S') == [] and st.working_tag
    clk.t += 5
    O.chase_exit(c, st, lambda: neo.quotes[SYM], p, sleep=clk.sleep, clock=clk)
    assert neo.placed('S') == []
    clk.t += 20
    for _ in range(3):
        O.chase_exit(c, st, lambda: neo.quotes[SYM], p, sleep=clk.sleep, clock=clk)
    assert st.status == 'done' and len(neo.placed('S')) == 1 and neo.net(SYM) == 0


def test_a_rejected_sell_backs_off_and_does_not_walk_the_price(neo):
    """Review F13: each reject re-sent every poll a tick lower."""
    c, clk = client(neo), Clock()
    hold(c, neo)
    neo.reject(1009, 'RMS check')
    p = O.ChasePolicy(reprice_after_s=5, confirm_wait_s=0, reject_backoff_s=15)
    st = chase_state()
    O.chase_exit(c, st, lambda: neo.quotes[SYM], p, sleep=clk.sleep, clock=clk)
    assert 'rejected' in st.note and st.reprices == 0
    clk.t += 5
    O.chase_exit(c, st, lambda: neo.quotes[SYM], p, sleep=clk.sleep, clock=clk)
    assert len(places(neo)) == 2                     # buy + the rejected sell only
    clk.t += 15
    O.chase_exit(c, st, lambda: neo.quotes[SYM], p, sleep=clk.sleep, clock=clk)
    sells = neo.placed('S')
    assert sells and sells[-1]['prc'] == '10.00'     # same price: a reject is not a re-price


def test_a_slow_cancel_is_settled_on_a_later_poll_not_resold(neo):
    c, clk = client(neo), Clock()
    hold(c, neo)
    neo.set_quote(SYM, 1.0, 1.2)
    neo.slow_cancel(3)
    stale = {'bid': 10.0}
    p = O.ChasePolicy(reprice_after_s=5, confirm_wait_s=0)
    st = chase_state()
    for _ in range(8):
        O.chase_exit(c, st, lambda: stale, p, sleep=clk.sleep, clock=clk)
        assert len(live_sells(neo)) <= 1
        clk.t += 5
    assert neo.net(SYM) == 500                       # nothing filled, nothing doubled


def test_the_working_sell_is_left_to_work_until_its_reprice_time(neo):
    c, clk = client(neo), Clock()
    hold(c, neo)
    neo.set_quote(SYM, 1.0, 1.2)
    stale = {'bid': 10.0}
    p = O.ChasePolicy(reprice_after_s=30, confirm_wait_s=0)
    st = chase_state()
    for _ in range(5):
        O.chase_exit(c, st, lambda: stale, p, sleep=clk.sleep, clock=clk)
        clk.t += 5
    assert len(neo.placed('S')) == 1 and len(live_sells(neo)) == 1
    clk.t += 30
    O.chase_exit(c, st, lambda: stale, p, sleep=clk.sleep, clock=clk)
    O.chase_exit(c, st, lambda: stale, p, sleep=clk.sleep, clock=clk)
    assert [o['ordSt'] for o in neo.placed('S')] == ['cancelled', 'open']


def test_chase_gives_up_to_a_human_after_max_reprices(neo):
    c, clk = client(neo), Clock()
    hold(c, neo)
    neo.set_quote(SYM, 1.0, 1.2)
    stale = {'bid': 10.0}
    p = O.ChasePolicy(reprice_after_s=5, confirm_wait_s=0, max_reprices=3)
    st = chase_state()
    for _ in range(30):
        O.chase_exit(c, st, lambda: stale, p, sleep=clk.sleep, clock=clk)
        assert len(live_sells(neo)) <= 1
        clk.t += 5
        if st.status != 'active':
            break
    assert st.status == 'needs_human' and 're-prices' in st.note
    assert len(neo.placed('S')) == 3 and live_sells(neo) == [] and neo.net(SYM) == 500


def test_chase_state_round_trips(neo):
    st = chase_state()
    st.fills.append({'qty': 1})
    assert O.ChaseState.from_dict(json.loads(json.dumps(st.to_dict()))) == st


# -- symbols -------------------------------------------------------------------------

HDR = ('pSymbol,pGroup,pExchSeg,pInstType,pSymbolName,pTrdSymbol,pOptionType,dTickSize,'
       'lLotSize,dStrikePrice;,pExpiryDate')


def row(tok, inst, und, ts, opt, strike, lot, exp_epoch=1483021800, tick=5):
    return '%s,XX,nse_fo,%s,%s,%s,%s,%d,%d,%d,%d' % (tok, inst, und, ts, opt, tick, lot,
                                                    int(round(strike * 100)), exp_epoch)


CSV = '\n'.join([HDR,
                 row(144284, 'OPTSTK', 'RELIANCE', 'RELIANCE26DEC1540CE', 'CE', 1540, 500),
                 row(144285, 'OPTSTK', 'RELIANCE', 'RELIANCE26DEC1540PE', 'PE', 1540, 500),
                 row(47930, 'OPTIDX', 'NIFTY', 'NIFTY26D0126450CE', 'CE', 26450, 65,
                     exp_epoch=1480608000),                       # Tue 2026-12-01 15:30 IST
                 row(55001, 'OPTSTK', 'NHPC', 'NHPC26DEC82.5CE', 'CE', 82.5, 6400),
                 row(1, 'FUTSTK', 'RELIANCE', 'RELIANCE26DECFUT', 'XX', 0, 500)])


def test_stock_and_index_options_map_by_facts():
    m = Y.ScripMap(Y.parse_scrip_master(CSV))
    r = m.find('RELIANCE', date(2026, 12, 29), 1540, 'CE')
    assert r.trading_symbol == 'RELIANCE26DEC1540CE' and r.token == '144284'
    assert r.lot_size == 500 and r.tick_size == 0.05 and r.inst_type == 'OPTSTK'
    assert m.find('NIFTY', date(2026, 12, 1), 26450, 'CE').lot_size == 65
    assert m.find('NHPC', date(2026, 12, 29), 82.5, 'CE').token == '55001'   # half strikes
    assert len(m) == 4


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


class Src:
    def __init__(self, text=CSV):
        self.n, self.text = 0, text

    def scrip_master_paths(self):
        return ['https://x/nse_cm.csv', 'https://x/nse_fo.csv']

    def fetch_text(self, url):
        self.n += 1
        assert 'nse_fo' in url
        return self.text


def test_load_downloads_once_a_day_into_the_shared_cache(home):
    src = Src()
    m1 = Y.load(src, today=date(2026, 11, 27), min_rows=1)
    m2 = Y.load(src, today=date(2026, 11, 27), min_rows=1, underlyings=['NHPC'])
    assert src.n == 1 and len(m1) == 4 and len(m2) == 1
    assert not list(home.glob('*.tmp'))


def test_a_truncated_download_is_never_cached(home):
    with pytest.raises(Y.ScripMasterError, match='truncated'):
        Y.load(Src(CSV), today=date(2026, 11, 27), min_rows=1000)
    assert not list(home.glob('scrip_nse_fo_*.csv'))


# -- static IP: alert and hand the trade to the owner (owner, 2026-11-27) -------------

@pytest.mark.parametrize('code,msg,exc', [
    (100008, 'unauthorized', C.Rejected),
    (1037, 'session ip doesnt match with reqest ip', C.Unclear)])
def test_an_ip_refusal_alerts_once_per_window(neo, monkeypatch, code, msg, exc):
    sent = []
    monkeypatch.setattr(S, '_flush_alerts', lambda: sent.append(1))
    c = client(neo)
    for _ in range(2):
        neo.reject(code, msg) if code == 100008 else None
        if code == 1037:
            monkeypatch.setattr(neo, 'post', lambda url, **kw: _Resp(
                400, {'stat': 'Not_Ok', 'stCode': 1037, 'errMsg': msg}))
        with pytest.raises(exc):
            buy(c)
    alerts = (S.HOME / 'alerts.jsonl').read_text(encoding='utf-8').splitlines()
    assert len(alerts) == 1 and 'static_ip' in alerts[0] and 'MANUALLY' in alerts[0]
    assert len(sent) == 1


def test_the_morning_ip_check(home, monkeypatch):
    monkeypatch.setattr(S, '_flush_alerts', lambda: None)
    S._ensure_home()
    assert S.check_public_ip(lambda: '1.2.3.4') is None            # off: no expected IP
    (S.HOME / 'config.json').write_text('{"expected_public_ipv4": "1.2.3.4"}', encoding='utf-8')
    assert S.check_public_ip(lambda: '1.2.3.4') is None
    assert S.check_public_ip(lambda: None) is None                  # cannot read: no alarm
    assert 'changed' in S.check_public_ip(lambda: '5.6.7.8')
    assert 'static_ip' in (S.HOME / 'alerts.jsonl').read_text(encoding='utf-8')
