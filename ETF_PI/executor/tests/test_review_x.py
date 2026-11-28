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


def test_t7_a_probe_rests_its_first_sells_then_exits_through_the_ladder(rig):
    """Review T7: `probe --rest-steps 3` -> the flatten's first 3 sells rest 3% above
    the bid, so cancel -> confirm -> re-place runs 3 times for real; then the normal
    ladder sells. Never two sells working, never short (Rig.check)."""
    p = open_one(rig, key='PROBE_NIFTYX_20261130_100000', probe_rest_steps=3)
    assert p['probe_rest_steps'] == 3
    open(os.path.join(rig.state, 'FLATTEN'), 'w').close()
    rig.step(n=60)                                    # 30 s
    sells = rig.etf_orders('S')
    assert p['state'] == 'CLOSED' and rig.fake.net(p['symbol']) == 0
    assert len(sells) == 4 and [o['ordSt'] for o in sells[:3]] == ['cancelled'] * 3
    assert all(float(o['prc']) > 99.95 for o in sells[:3])
    assert float(sells[3]['prc']) <= 99.95


def test_t7_only_a_probe_key_may_rest_sells(rig):
    p = open_one(rig, probe_rest_steps=3)             # an option_buy signal key
    assert p['probe_rest_steps'] == 0


def test_xe2_a_non_object_intent_is_set_aside_and_exits_keep_working(rig):
    """Review X-E2: a valid-JSON list as an intent raised TypeError on every step --
    never moved, no exits, no heartbeat. Now it goes to done/*.bad, alerted."""
    import json
    p = open_one(rig)
    bad = os.path.join(rig.state, 'intents', 'weird.json')
    with open(bad, 'w') as f:
        json.dump([1, 2, 3], f)
    rig.quote(110.0, 110.1)                           # TP
    rig.step(n=4)
    assert not os.path.exists(bad)
    assert os.path.exists(os.path.join(rig.eng.done_dir, 'weird.json.bad'))
    assert p['state'] == 'CLOSED' and 'bad intent file weird.json' in rig.texts()


def test_xe2_a_stage_bug_never_stops_an_exit(rig, monkeypatch):
    p = open_one(rig)

    def boom(now):
        raise RuntimeError('intent bug')
    monkeypatch.setattr(rig.eng, '_intents', boom)
    rig.quote(110.0, 110.1)
    rig.step(n=4)
    assert p['state'] == 'CLOSED' and 'intents BUG' in rig.texts()


def test_xe1_own_orders_short_but_account_not_short_sends_no_cover(rig, monkeypatch):
    """Review X-E1: the cover was decided from our own order sums alone; a short that
    exists only there (paper, a misread) bought a real, untracked lot."""
    import datetime as dt
    from test_engine import SYM
    p = open_one(rig)
    rig.quote(110.0, 110.1)
    rig.step(n=3)
    assert p['state'] == 'CLOSED'
    rig.broker.place(SYM, '1', 'S', 65, 109.0, rig.broker.new_tag(), reduce_only=True)
    monkeypatch.setattr(rig.broker, 'account_net', lambda sym: 0)   # the account says flat
    for _ in range(4):
        rig.now += dt.timedelta(seconds=16)
        rig.feed._q[SYM].ts = rig.epoch()
        rig.eng.step(rig.now)
    assert not p.get('covered')
    assert not [o for o in rig.etf_orders('B') if o['GuiOrdId'] != p['entry']['tags'][0]]
    assert 'NO cover' in rig.texts()


def test_b4_a_torn_tail_is_cut_so_the_next_row_survives_and_entries_go_on(rig):
    """Review B4 / X-E3: power cut mid-append, restart, more rows, restart again. Before
    the fix the next row glued onto the torn bytes -> corrupt middle line -> no entries,
    and that row lost."""
    open_one(rig)
    path = rig.eng.j.path
    with open(path, 'a') as f:
        f.write('{"ev": "EXIT_ST')                     # power cut mid-append
    rig.restart()
    assert rig.eng.corrupt is None
    rig.quote(110.0, 110.1)                           # TP -> new rows appended
    rig.step(n=3)
    assert rig.pos()['state'] == 'CLOSED'
    rig.restart()
    assert rig.eng.corrupt is None and rig.pos()['state'] == 'CLOSED'
    assert '{"ev": "EXIT_ST' in open(path + '.torn').read()
    rig.intent(key='NIFTY_PE_10minute_20261130_H2')
    rig.step()
    assert 'NIFTY_PE_10minute_20261130_H2' in rig.eng.pos


def test_b4_a_whole_row_missing_only_its_newline_is_kept(tmp_path):
    from executor.journal import Journal
    j = Journal(str(tmp_path / 'j.jsonl'))
    j.append({'ev': 'A', 'key': 'k1', 'pos': {'x': 1}})
    with open(j.path, 'a') as f:
        f.write('{"ev":"B","key":"k2","pos":{"x":2}}')   # the newline was lost
    j.append({'ev': 'C', 'key': 'k3', 'pos': {'x': 3}})
    pos, rows, note = j.load()
    assert note is None and set(pos) == {'k1', 'k2', 'k3'}


# -- Fable review of the 11-28 fixes (F1, F4, F5) ------------------------------------------

def test_f1_an_outside_sell_before_our_open_never_confirms_a_low_row(rig, monkeypatch):
    """BTST / the owner sold 65 of the strike BEFORE ETF bought it. At exit the MIS row
    misreads 0: that earlier sell must not count -- nothing sold, nothing closed."""
    from test_engine import SYM, KCN
    rig.fake._new_order({'pt': 'L', 'ts': SYM, 'tt': 'B', 'qt': 65, 'pr': '100.10', 'ig': '',
                         'pc': 'NRML'})
    rig.fake._new_order({'pt': 'L', 'ts': SYM, 'tt': 'S', 'qt': 65, 'pr': '99.00', 'ig': '',
                         'pc': 'NRML'})                 # an outside round trip, earlier today
    rig.eng.cfg['skip_foreign_strikes'] = False
    p = open_one(rig)
    assert p['outside_base'] == 65
    monkeypatch.setattr(rig.broker.c, 'positions_for',
                        lambda sym: [KCN.PositionView(symbol=SYM, product='MIS', net_qty=0)])
    rig.step(secs=15)
    open(os.path.join(rig.state, 'FLATTEN'), 'w').close()
    rig.step(n=200)
    assert p['state'] == 'EXIT' and 'NOT closing' in rig.texts()


def test_f4_stop_placing_must_leave_the_eod_exit_a_minute(tmp_path):
    import pytest
    from executor import config as C
    none = str(tmp_path / 'none.yaml')
    for bad in ({'eod_time': ''}, {'stop_placing': '15:15:30'}):
        with pytest.raises(C.ConfigError, match='stop_placing'):
            C.load(local_path=none, override=bad)
    assert C.load(local_path=none, override={'eod_time': '', 'stop_placing': '15:29:50'})


def test_f5_a_non_utf8_tail_never_crashes_the_load(tmp_path):
    from executor.journal import Journal
    j = Journal(str(tmp_path / 'j.jsonl'))
    j.append({'ev': 'A', 'key': 'k1', 'pos': {'x': 1}})
    with open(j.path, 'ab') as f:
        f.write(b'\xff\xfe\x80garbage')
    pos, rows, note = j.load()
    assert note is None and set(pos) == {'k1'}
    j.append({'ev': 'B', 'key': 'k2', 'pos': {'x': 2}})
    pos, rows, note = j.load()
    assert note is None and set(pos) == {'k1', 'k2'}
