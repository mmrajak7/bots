"""Two bots on ONE strike of one account (owner 2026-11-27: "BTST may trade the
same strike in parallel -- we should not interfere").

A NON-exclusive client (ETF_PI) claims nothing, sizes by its OWN tagged fills,
and its chase -- given `held_fn = min(own net, account net)` -- can never sell
another bot's lots, never waits on another bot's working sell, and never
hands an exit to a human when the caller says so (max_* None).
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kotak_neo import client as C        # noqa: E402
from kotak_neo import orders as O        # noqa: E402
from kotak_neo import session as S       # noqa: E402
from kotak_neo.fake import FakeNeo       # noqa: E402

SYM, TOK = 'NIFTY26DEC26000CE', '45001'
SESS = S.Session(3, '2026-11-27', 'c', 'https://cis.kotaksecurities.com', 'T', 'SID', None,
                 'CK', 'U')


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Alive:
    def __call__(self, gen, who):
        return S.Result(S.STILL_ALIVE, SESS, 'scripted')


@pytest.fixture
def neo(tmp_path, monkeypatch):
    monkeypatch.setattr(S, 'HOME', tmp_path / 'neo')
    f = FakeNeo()
    f.set_quote(SYM, 100.0, 100.5)
    return f


def mk(neo, bot, exclusive):
    return C.NeoClient(bot, http=neo, exclusive=exclusive, current_fn=lambda: SESS,
                       refresh_fn=Alive())


def buy(c, qty=65, px='100.50'):
    tag = c.new_tag()
    return c.place(C.OrderRequest(SYM, TOK, 'B', qty, px, tag)), tag


def etf_held(c):
    return lambda: min(c.own_net(SYM), c.net_held(SYM))


def test_non_exclusive_buys_a_strike_another_bot_owns_and_claims_nothing(neo):
    btst, etf = mk(neo, 'btst', True), mk(neo, 'etf', False)
    buy(btst)
    assert C.owner_of(SYM) == 'btst'
    buy(etf)                                          # an exclusive client would raise NotOwner
    assert C.owner_of(SYM) == 'btst'                  # the registry is untouched
    assert neo.net(SYM) == 130


def test_own_net_counts_only_own_tagged_fills(neo):
    btst, etf = mk(neo, 'btst', True), mk(neo, 'etf', False)
    buy(btst, qty=130)
    buy(etf)
    assert etf.own_net(SYM) == 65 and btst.own_net(SYM) == 130
    etf.place(C.OrderRequest(SYM, TOK, 'S', 65, '100.00', etf.new_tag()), reduce_only=True)
    assert etf.own_net(SYM) == 0 and neo.net(SYM) == 130


def test_chase_with_own_cap_never_sells_the_other_bots_lots(neo):
    btst, etf = mk(neo, 'btst', True), mk(neo, 'etf', False)
    buy(btst)
    buy(etf)
    clk = Clock()
    # a stale state that THINKS it holds 130: the own cap must still stop at 65
    st = O.ChaseState(bot='etf', symbol=SYM, token=TOK, tick=0.05, remaining=130)
    p = O.ChasePolicy(reprice_after_s=2, confirm_wait_s=0, max_reprices=None, max_troubles=None)
    for _ in range(20):
        O.chase_exit(etf, st, lambda: neo.quotes[SYM], p, sleep=clk.sleep, clock=clk,
                     held_fn=etf_held(etf))
        clk.t += 2
    assert st.sold == 65 and neo.net(SYM) == 65 and etf.own_net(SYM) == 0
    assert btst.own_net(SYM) == 65                    # BTST's lot is untouched
    assert st.status == 'active' and st.troubles > 0  # alerts, never needs_human (max None)


def test_unclear_resend_ignores_another_bots_working_sell(neo):
    btst, etf = mk(neo, 'btst', True), mk(neo, 'etf', False)
    buy(btst)
    buy(etf)
    # BTST rests a sell far above the market: a live sell on the strike that is NOT ours
    btst.place(C.OrderRequest(SYM, TOK, 'S', 65, '150.00', btst.new_tag()), reduce_only=True)
    clk = Clock()
    st = O.ChaseState(bot='etf', symbol=SYM, token=TOK, tick=0.05, remaining=65)
    p = O.ChasePolicy(reprice_after_s=2, confirm_wait_s=0, unclear_grace_s=10,
                      max_reprices=None, max_troubles=None)
    neo.timeout(placed=False)                         # our first sell never reaches the book
    O.chase_exit(etf, st, lambda: neo.quotes[SYM], p, sleep=clk.sleep, clock=clk,
                 held_fn=etf_held(etf))
    assert st.working_tag and not st.working_no
    clk.t += 11                                       # past the grace
    for _ in range(3):
        O.chase_exit(etf, st, lambda: neo.quotes[SYM], p, sleep=clk.sleep, clock=clk,
                     held_fn=etf_held(etf))
        clk.t += 2
    assert st.sold == 65 and etf.own_net(SYM) == 0
    assert any(o['trnsTp'] == 'S' and o['ordSt'] == 'open' and o['GuiOrdId'].startswith('btst-')
               for o in neo.orders)                   # BTST's order was never touched


def test_exclusive_unclear_resend_still_waits_on_any_live_sell(neo):
    """The default (exclusive) rule is unchanged: any live sell blocks a resend."""
    zbr = mk(neo, 'zbr', True)
    buy(zbr, qty=130)
    zbr.place(C.OrderRequest(SYM, TOK, 'S', 65, '150.00', 'zbr-x'), reduce_only=True)
    clk = Clock()
    st = O.ChaseState(bot='zbr', symbol=SYM, token=TOK, tick=0.05, remaining=65)
    p = O.ChasePolicy(reprice_after_s=2, confirm_wait_s=0, unclear_grace_s=10)
    neo.timeout(placed=False)
    O.chase_exit(zbr, st, lambda: neo.quotes[SYM], p, sleep=clk.sleep, clock=clk)
    clk.t += 11
    O.chase_exit(zbr, st, lambda: neo.quotes[SYM], p, sleep=clk.sleep, clock=clk)
    assert st.sold == 0 and 'live sell' in st.note and len(neo.placed('S')) == 1


def test_price_fn_sets_the_limit_rounded_down_to_the_tick(neo):
    etf = mk(neo, 'etf', False)
    buy(etf)
    neo.set_quote(SYM, 100.0, 100.5)
    clk = Clock()
    st = O.ChaseState(bot='etf', symbol=SYM, token=TOK, tick=0.05, remaining=65)
    p = O.ChasePolicy(reprice_after_s=2, confirm_wait_s=0, max_reprices=None, max_troubles=None)
    neo.set_quote(SYM, 100.0, 100.5)
    # a limit far above the bid rests; check the price the schedule produced
    O.chase_exit(etf, st, lambda: {'bid': 100.0}, p, sleep=clk.sleep, clock=clk,
                 held_fn=etf_held(etf), price_fn=lambda bid, n, tick: bid * 1.0037)
    sell = neo.placed('S')[-1]
    assert sell['prc'] == '100.35'                    # 100.37 rounded DOWN


def test_max_reprices_none_keeps_chasing_a_market_that_never_fills(neo):
    etf = mk(neo, 'etf', False)
    buy(etf)
    clk = Clock()
    st = O.ChaseState(bot='etf', symbol=SYM, token=TOK, tick=0.05, remaining=65)
    p = O.ChasePolicy(reprice_after_s=2, confirm_wait_s=0, max_reprices=None, max_troubles=None)
    for _ in range(60):                               # the limit is always above the bid
        O.chase_exit(etf, st, lambda: {'bid': 100.0}, p, sleep=clk.sleep, clock=clk,
                     held_fn=etf_held(etf), price_fn=lambda bid, n, tick: 200.0)
        clk.t += 3
    assert st.status == 'active' and st.reprices > 12 and st.sold == 0
    assert len([o for o in neo.placed('S') if o['ordSt'] == 'open']) == 1


def _bad_row(tag):
    return {'nOrdNo': '999', 'ordSt': 'complete', 'trdSym': SYM, 'trnsTp': 'B', 'qty': 'x',
            'fldQty': '?', 'avgPrc': '0', 'prc': '0', 'trgPrc': '0', 'prcTp': 'L',
            'rejRsn': '--', 'GuiOrdId': tag, 'ordDtTm': '27-Nov-2026 13:53:11'}


def test_a_bad_row_of_another_bot_does_not_blind_own_reads(neo):
    """Review F11: one unparsable foreign row made the whole book Unavailable."""
    etf = mk(neo, 'etf', False)
    _, tag = buy(etf)
    neo.orders.append(_bad_row('btst-zz'))
    assert etf.own_order_net(SYM) == 65
    assert O.resolve(etf, tag, wait_s=0).state == O.FILLED
    with pytest.raises(C.Unavailable):
        etf.order_book()                              # the strict whole-book read still refuses


def test_a_bad_row_of_this_bot_is_unavailable_never_zero(neo):
    etf = mk(neo, 'etf', False)
    buy(etf)
    neo.orders.append(_bad_row('etf-bad'))
    with pytest.raises(C.Unavailable):
        etf.own_order_net(SYM)


def test_own_order_net_ignores_other_bots_and_counts_partials(neo):
    btst, etf = mk(neo, 'btst', True), mk(neo, 'etf', False)
    buy(btst, qty=130)
    neo.partial(65)
    buy(etf, qty=130)                                 # half fills, half rests
    assert etf.own_order_net(SYM) == 65 and btst.own_order_net(SYM) == 130


def test_n2_a_stray_own_sell_blocks_a_new_sell(neo):
    """Peer review N2: chase never looked for OTHER live sells before placing."""
    etf = mk(neo, 'etf', False)
    buy(etf)
    etf.place(C.OrderRequest(SYM, TOK, 'S', 65, '150.00', etf.new_tag()), reduce_only=True)
    clk = Clock()
    st = O.ChaseState(bot='etf', symbol=SYM, token=TOK, tick=0.05, remaining=65)
    p = O.ChasePolicy(reprice_after_s=2, confirm_wait_s=0, max_reprices=None, max_troubles=None)
    O.chase_exit(etf, st, lambda: neo.quotes[SYM], p, sleep=clk.sleep, clock=clk,
                 held_fn=etf_held(etf))
    assert len(neo.placed('S')) == 1 and 'another live sell' in st.note and neo.net(SYM) == 65


def test_n3_definite_rejects_are_counted_for_the_caller(neo):
    etf = mk(neo, 'etf', False)
    buy(etf)
    clk = Clock()
    st = O.ChaseState(bot='etf', symbol=SYM, token=TOK, tick=0.05, remaining=65)
    p = O.ChasePolicy(reprice_after_s=2, confirm_wait_s=0, reject_backoff_s=5,
                      max_reprices=None, max_troubles=None)
    for _ in range(3):
        neo.reject(1009, 'price band')
        O.chase_exit(etf, st, lambda: neo.quotes[SYM], p, sleep=clk.sleep, clock=clk,
                     held_fn=etf_held(etf))
        clk.t += 6
    assert st.rejects == 3 and 'rejected (3 in a row)' in st.note
    for _ in range(2):                                # place, then settle the fill
        O.chase_exit(etf, st, lambda: neo.quotes[SYM], p, sleep=clk.sleep, clock=clk,
                     held_fn=etf_held(etf))
        clk.t += 1
    assert st.sold == 65 and st.rejects == 0          # a fill resets the counter


def test_n5_auth_inside_the_chase_never_escapes(neo):
    etf = mk(neo, 'etf', False)
    buy(etf)
    clk = Clock()
    st = O.ChaseState(bot='etf', symbol=SYM, token=TOK, tick=0.05, remaining=65)
    p = O.ChasePolicy(reprice_after_s=2, confirm_wait_s=0, max_reprices=None, max_troubles=None)

    def dead():
        raise C.Auth('session latched')
    O.chase_exit(etf, st, lambda: neo.quotes[SYM], p, sleep=clk.sleep, clock=clk, held_fn=dead)
    assert st.troubles == 1 and 'unreadable' in st.note and neo.placed('S') == []


def test_h2_a_cancel_settled_on_a_later_read_still_counts_as_a_reprice(neo):
    """Review H2: with a slow cancel the schedule stayed at step 0 forever."""
    etf = mk(neo, 'etf', False)
    buy(etf)
    clk = Clock()
    st = O.ChaseState(bot='etf', symbol=SYM, token=TOK, tick=0.05, remaining=65)
    p = O.ChasePolicy(reprice_after_s=2, confirm_wait_s=0, max_reprices=None, max_troubles=None)
    seen = []
    neo.slow_cancel(1)
    for _ in range(12):                               # the limit always rests above the bid
        O.chase_exit(etf, st, lambda: {'bid': 100.0}, p, sleep=clk.sleep, clock=clk,
                     held_fn=etf_held(etf), price_fn=lambda b, n, t: (seen.append(n), 200.0 - n)[1])
        clk.t += 1
    assert st.reprices >= 3 and seen[:3] == [0, 1, 2]


def test_m6_positions_are_split_by_product(neo):
    mis = C.NeoClient('etf', http=neo, exclusive=False, product='MIS', current_fn=lambda: SESS,
                      refresh_fn=Alive())
    btst = mk(neo, 'btst', True)
    buy(btst)
    buy(mis)
    rows = {(p.symbol, p.product): p.net_qty for p in mis.positions()}
    assert rows == {(SYM, 'NRML'): 65, (SYM, 'MIS'): 65}
