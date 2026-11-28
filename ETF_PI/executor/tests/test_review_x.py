"""Fixes from the 11-28 reviews (BTST peer Review 8 'X-', ETF bug hunt 'B') that live in
kotak_neo/orders.py, proved end to end through the ETF engine over FakeNeo."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_engine import open_one, rig  # noqa: E402,F401  (the fixture)


def test_xk1_a_sell_rejected_in_the_book_backs_off_and_says_why(rig):
    """Accepted, then RMS-REJECTED in the book: before the fix ~2 sells/s for as long as
    RMS refused (241 in 2 min). Now each refusal waits reject_backoff_s, is counted and
    the alert carries the reason."""
    p = open_one(rig)
    for _ in range(400):
        rig.fake.rms_reject('RMS:MIS orders not allowed')
    rig.quote(80.0, 80.1)                             # PREM_SL fires
    rig.step(n=60)                                    # 30 s
    sells = rig.etf_orders('S')
    limit = 30.0 / rig.cfg['reject_backoff_s'] + 2
    assert 1 <= len(sells) <= limit, len(sells)
    assert p['chase']['rejects'] >= 2
    assert 'rejected in the book' in rig.texts() and 'RMS:MIS orders not allowed' in rig.texts()
    rig.fake._next.clear()                            # RMS relents: the exit completes
    rig.step(secs=1, n=20)
    assert p['state'] == 'CLOSED' and rig.fake.net(p['symbol']) == 0


def test_b6_a_pending_cancel_is_not_resent_every_loop(rig):
    """A slow cancel ('cancel pending' for many reads): before the fix the cancel POST
    went out on every 0.5 s loop (17 in 10 s)."""
    p = open_one(rig)
    orig = rig.fake._match
    rig.fake._match = lambda o: None                  # nothing fills: the chase must re-price
    rig.fake.slow_cancel(reads=40)
    rig.quote(80.0, 80.1)                             # PREM_SL: the first sell rests
    rig.step(n=20)                                    # 10 s
    assert rig.etf_orders('S'), 'no sell went out'
    cancels = [c for c in rig.fake.calls if c[0] == 'POST' and c[1].endswith('/quick/order/cancel')]
    rig.fake._match = orig
    assert 1 <= len(cancels) <= 10.0 / rig.cfg['exit_step_s'] + 1, len(cancels)


def test_b5_a_recovered_feed_prices_off_the_live_bid_again(rig):
    """The feed dies while a sell rests (stale stepping from the last bid), then comes
    back at 100.00: before the fix the limit kept falling 2% a re-price (82.00)."""
    from test_engine import SYM
    from executor.rules import Quote
    p = open_one(rig)

    def feedq(bid):
        rig.feed._q[SYM] = Quote(ltp=bid, bid=bid, ask=bid + 0.1, bids=[(bid, 1000)],
                                 asks=[(bid + 0.1, 1000)], ts=rig.epoch(), source='ws')
    rig.fake.set_quote(SYM, 40.0, 40.1)               # our sells rest: nobody bids near them
    feedq(100.0)
    open(os.path.join(rig.state, 'FLATTEN'), 'w').close()
    rig.step(refresh=False)
    for _ in range(90):                               # 45 s with no price at all
        rig.feed._q.pop(SYM, None)
        rig.step(refresh=False)
    assert p.get('stale_exit')
    for _ in range(20):                               # back: live bid 100.00 for 10 s
        feedq(100.0)
        rig.step(refresh=False)
    last = [o for o in rig.fake.orders if o['trnsTp'] == 'S'][-1]
    assert not p.get('stale_exit')
    tail = float(rig.cfg['exit_tail_pct'])
    assert float(last['prc']) >= 100.0 * (1 - tail / 100.0) - 0.05, last['prc']


def test_xk2_a_book_listing_every_order_twice_never_sells_short(rig):
    """Every order-book row duplicated: before the fix the entry read 130 for a 65 fill
    and the exit sold 130 (account short 65)."""
    from test_engine import KCN
    orig = rig.fake.get

    def get(url, **kw):
        resp = orig(url, **kw)
        if url.endswith('/quick/user/orders'):
            b = resp.json()
            if isinstance(b.get('data'), list):
                resp = type(resp)(200, dict(b, data=b['data'] + [dict(x) for x in b['data']]))
        return resp
    rig.fake.get = get
    p = open_one(rig)
    assert p['qty'] == 65
    rig.quote(80.0, 80.1)                             # PREM_SL inside positions_lag_s
    rig.step(n=10)                                    # Rig.check(): never short
    assert p['state'] == 'CLOSED' and rig.fake.net(p['symbol']) == 0
    assert sum(int(o['qty']) for o in rig.etf_orders('S')) == 65
