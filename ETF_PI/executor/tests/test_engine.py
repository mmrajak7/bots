"""The executor end to end: real kotak_neo client + orders + chase over FakeNeo (the
hostile fake of NEO's HTTP API), real option_buy._ladder, simulated clock.

Each test names the spec rule it proves (docs/EXECUTOR_SPEC.md). The invariants every
test re-checks after every step (see `Rig.step`):
  * never two ETF sells working on one symbol;
  * the account is never short;
  * ETF never sells another bot's lots.
"""
import datetime as dt
import json
import os
import sys

import pytest

ETF = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ETF)
from Tests import _muzzle  # noqa: E402,F401
import option_buy as ob    # noqa: E402
ob._init_bridge()

from executor import config as C               # noqa: E402
from executor.broker import Broker, import_kotak_neo   # noqa: E402
from executor.engine import Engine             # noqa: E402
from executor.journal import Journal           # noqa: E402
from executor.notify import Notifier           # noqa: E402
from executor.prices import PriceFeed          # noqa: E402
from executor.rules import Quote               # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SYM = 'NIFTY26DEC26000CE'
# kotak_neo exactly where the daemon finds it (config bots_root: the Pi's ETF clone is sparse)
KCN, KON, KS, KY = import_kotak_neo(C.load(local_path=os.path.join(ETF, 'no_such_overlay.yaml'))['bots_root'])


class Rig:
    def __init__(self, tmp, **over):
        self.tmp = tmp
        self.now = dt.datetime(2026, 11, 30, 10, 0, 0, tzinfo=IST)
        # the rig's paper EOD is 15:25 (eod_fn), so orders may go out until the old 15:29:50
        cfg = dict(mode='paper', stop_placing='15:29:50')
        cfg.update(over)
        self.cfg = C.load(local_path=str(tmp / 'none.yaml'), override=cfg)
        self.state = str(tmp / 'state')
        os.makedirs(os.path.join(self.state, 'intents'), exist_ok=True)
        self.broker = Broker(self.cfg, paper=True)
        self.fake = self.broker.fake
        self.sent = []
        self.build()

    def epoch(self):
        return self.now.timestamp()

    def build(self):
        self.feed = PriceFeed(self.cfg, 'none', self.state, rest_quote=lambda k: None,
                              ticker_factory=None, clock=self.epoch)
        self.n = Notifier(sender=lambda m, k: self.sent.append((k, m)), clock=self.epoch)
        self.eng = Engine(self.cfg, self.broker, self.feed, self.n,
                          Journal(os.path.join(self.state, 'journal.jsonl')), ob, self.state,
                          session_fn=self.session, eod_fn=lambda d: dt.time(15, 25),
                          epoch=self.epoch)

    @staticmethod
    def session(d):
        return None if d.weekday() >= 5 else (dt.time(9, 15), dt.time(15, 30))

    def restart(self):
        """A daemon restart: same journal, same broker account, fresh process state."""
        self.build()

    # -- market -----------------------------------------------------------------
    def quote(self, bid, ask=None, ltp=None, sym=SYM, depth=1000):
        ask = ask if ask is not None else round(bid + 0.10, 2)
        self.feed.push(sym, Quote(ltp=ltp if ltp is not None else bid, bid=bid, ask=ask,
                                  bids=[(bid, depth)] if bid else [],
                                  asks=[(ask, depth)] if ask else [], ts=self.epoch(),
                                  source='ws'))
        if bid > 0 and ask > 0:
            self.fake.set_quote(sym, bid, ask)

    def intent(self, key='NIFTY_CE_10minute_20261130_H1', ltp=100.0, st=110.0, t2=0.0,
               age=1, **kw):
        it = {'key': key, 'created_at': (self.now - dt.timedelta(seconds=age)).isoformat(),
              'underlying': 'NIFTY', 'kite_symbol': SYM, 'kite_token': 12345, 'exchange': 'NFO',
              'strike': 26000, 'expiry': '2026-12-01', 'opt_type': 'CE', 'signal_tf': '10minute',
              'signal_ltp': ltp, 'st_value': st, 't2_value': t2, 't2_tf': '15minute' if t2 else '',
              'lot_size': 65, 'tick_size': 0.05, 'entry_bid': ltp - 0.05, 'entry_ask': ltp}
        it.update(kw)
        with open(os.path.join(self.state, 'intents', key + '.json'), 'w') as f:
            json.dump(it, f)
        return key

    # -- time -------------------------------------------------------------------
    def step(self, secs=0.5, n=1, refresh=True):
        for _ in range(n):
            self.now += dt.timedelta(seconds=secs)
            if refresh and SYM in self.feed._q:       # keep the last quote fresh (a live feed)
                q = self.feed._q[SYM]
                q.ts = self.epoch()
            self.eng.step(self.now)
            self.check()

    def check(self):
        live_sells = [o for o in self.fake.orders if o['trnsTp'] == 'S'
                      and o['GuiOrdId'].startswith('etf-')
                      and o['ordSt'] in ('open', 'cancel pending')]
        assert len(live_sells) <= 1, 'two ETF sells working'
        for s in {o['trdSym'] for o in self.fake.orders}:
            assert self.fake.net(s) >= 0, 'account short in %s' % s

    def pos(self, key='NIFTY_CE_10minute_20261130_H1'):
        return self.eng.pos.get(key)

    def etf_orders(self, side=None):
        return [o for o in self.fake.orders if o['GuiOrdId'].startswith('etf-')
                and (side is None or o['trnsTp'] == side)]

    def texts(self):
        return ' | '.join(m for _, m in self.sent)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(KS, 'HOME', tmp_path / 'neo')
    r = Rig(tmp_path)
    r.quote(99.95, 100.0)
    r.step()                                          # first reconcile
    return r


def open_one(rig, **kw):
    k = rig.intent(**kw)
    rig.step()
    rig.step()
    p = rig.pos(k)
    assert p['state'] == 'OPEN', (p['state'], p.get('note'))
    return p


# -- happy path ----------------------------------------------------------------------------------
def test_entry_fills_at_the_ask_rebases_and_tp_sells_at_the_bid(rig):
    p = open_one(rig)
    assert p['qty'] == 65 and p['entry_premium'] == pytest.approx(100.0)          # E7
    assert rig.etf_orders('B')[0]['prc'] == '100.00'                             # E3
    rig.quote(109.0, 109.1)
    rig.step()
    assert p['state'] == 'OPEN'
    rig.quote(110.0, 110.1)                                                       # X2
    rig.step(n=3)
    assert p['state'] == 'CLOSED' and p['exit_type'] == 'TP'
    assert p['realised'] == pytest.approx((110.0 - 100.0) * 65)
    assert rig.fake.net(SYM) == 0 and 'SOLD 65' in rig.texts()


def test_journal_rebuild_after_close_is_identical(rig, tmp_path):
    p = open_one(rig)
    rig.quote(110.0, 110.1)
    rig.step(n=3)
    rig.restart()
    q = rig.pos()
    assert q['state'] == 'CLOSED' and q['realised'] == p['realised']


# -- entry gates and refusals (E1/E2/I1) --------------------------------------------------------
def test_stale_intent_is_skipped(rig):
    rig.intent(age=25)
    rig.step()
    assert rig.etf_orders() == [] and 'old' in json.dumps(rig.eng.j.load()[1][-1])


def test_price_ran_above_the_cap_sends_nothing(rig):
    rig.quote(100.35, 100.40)                         # cap = 100.30
    k = rig.intent()
    rig.step()
    assert rig.pos(k)['state'] == 'SKIPPED' and rig.etf_orders() == []


def test_kill_file_blocks_new_entries(rig):
    open(os.path.join(rig.state, 'KILL'), 'w').close()
    k = rig.intent()
    rig.step()
    assert k not in rig.eng.pos and rig.etf_orders() == []


def test_mode_off_reads_and_skips_quietly(tmp_path, monkeypatch):
    monkeypatch.setattr(KS, 'HOME', tmp_path / 'neo')
    r = Rig(tmp_path, mode='off')
    r.quote(99.95, 100.0)
    r.step()
    r.intent()
    r.step()
    assert r.etf_orders() == [] and r.sent[-1:] != [] and 'SKIP' not in r.texts()


def test_after_last_entry_time_is_skipped(rig):
    rig.now = rig.now.replace(hour=15, minute=0, second=30)
    rig.intent()
    rig.step()
    assert rig.etf_orders() == []


def test_max_open_and_one_position_per_symbol(rig):
    open_one(rig)
    rig.intent(key='NIFTY_CE_10minute_20261130_H2')
    rig.step()
    assert len(rig.etf_orders('B')) == 1 and 'already active' in json.dumps(rig.eng.j.load()[1][-1])


# -- partial / unfilled / unclear entries (E5/E6, F17, F24) --------------------------------------
def test_unfilled_entry_reprices_three_times_then_gives_up(rig):
    rig.quote(99.95, 100.0)
    k = rig.intent()
    rig.fake.quotes[SYM] = {'bid': 99.95, 'ask': 100.0}

    # the exchange never fills: every buy rests
    orig = rig.fake._match
    rig.fake._match = lambda o: None
    rig.step(n=40)
    rig.fake._match = orig
    p = rig.pos(k)
    assert p['state'] == 'UNFILLED' and len(rig.etf_orders('B')) == 3
    assert all(o['ordSt'] == 'cancelled' for o in rig.etf_orders('B'))


def test_partial_entry_is_the_position(tmp_path, monkeypatch):
    monkeypatch.setattr(KS, 'HOME', tmp_path / 'neo')
    r = Rig(tmp_path, lots=2)
    r.quote(99.95, 100.0)
    r.step()
    r.fake.partial(65)
    k = r.intent()
    r.step(n=10)
    p = r.pos(k)
    assert p['state'] == 'OPEN' and p['qty'] == 65 and len(r.etf_orders('B')) == 1


def test_unclear_entry_that_was_placed_is_resolved_by_tag_never_resent(rig):
    rig.fake.timeout(placed=True)
    k = rig.intent()
    rig.step(n=4)
    p = rig.pos(k)
    assert p['state'] == 'OPEN' and len(rig.etf_orders('B')) == 1
    assert rig.eng.faults == 1                                                     # F22


def test_unclear_entry_never_placed_gives_up_after_grace_with_one_send(rig):
    rig.fake.timeout(placed=False)
    k = rig.intent()
    rig.step(secs=5, n=14)
    p = rig.pos(k)
    assert p['state'] == 'UNFILLED' and rig.etf_orders() == []
    assert len([c for c in rig.fake.calls if c[1].endswith('/place')]) == 1


def test_late_surfacing_entry_fill_is_adopted_and_exited(rig):
    rig.fake.hide(400)                                # placed, filled, invisible for a while
    k = rig.intent()
    rig.step(secs=5, n=14)
    p = rig.pos(k)
    assert p['state'] == 'UNFILLED'
    for o in rig.fake.orders:
        o['_hide'] = 0                                # it surfaces
    rig.step(secs=5, n=6)
    assert p['entry'].get('adopted') and rig.fake.net(SYM) == 0
    assert p['state'] == 'CLOSED' and p['exit_type'] == 'LATE_FILL'


# -- exits (X1-X12) --------------------------------------------------------------------------------
def test_cost_floor_needs_persistence(rig):
    p = open_one(rig)
    p.update(cost_sl_active=True, cost_sl_level=100.10, sl_reason='COST_SL')
    rig.quote(100.05, 100.15)
    rig.step(n=4)                                     # 2 s below
    rig.quote(100.20, 100.30)
    rig.step()
    assert p['state'] == 'OPEN'
    rig.quote(100.05, 100.15)
    rig.step(n=12)                                    # 6 s below
    assert p['state'] == 'CLOSED' and p['exit_type'] == 'COST_SL'


def test_prem_sl_fires_on_the_bid_at_once(rig):
    p = open_one(rig)
    p['peak_premium'] = 110
    rig.quote(64.9, 65.2)
    rig.step(n=3)
    assert p['state'] == 'CLOSED' and p['exit_type'] == 'PREM_SL'


def test_time_sl_from_the_minute_sample(rig):
    p = open_one(rig)
    rig.quote(100.5, 100.6)
    rig.now = rig.now.replace(second=10)
    rig.step(secs=60, n=31)
    assert p['state'] == 'CLOSED' and p['exit_type'] == 'TIME_SL'


def test_eod_exit_at_1525(rig):
    p = open_one(rig)
    rig.now = rig.now.replace(hour=15, minute=24, second=58)
    p['entry_time'] = rig.now.isoformat()             # not a TIME_SL case
    rig.quote(100.5, 100.6)
    rig.step(n=8)
    assert p['state'] == 'CLOSED' and p['exit_type'] == 'EOD'


def test_no_price_alerts_then_flattens_from_the_last_bid(rig):
    p = open_one(rig)
    rig.quote(100.5, 100.6)
    rig.step()
    rig.feed._q[SYM].bid = 0                          # the book goes one-sided (F13)
    rig.step(secs=5, n=7, refresh=True)
    assert p['state'] == 'OPEN' and 'no price' in rig.texts()
    rig.step(secs=5, n=19, refresh=True)
    assert p['state'] in ('EXIT', 'CLOSED') and p['exit_type'] == 'NO_PRICE'


def test_exit_chase_reprices_down_a_falling_book_and_never_double_sells(rig):
    p = open_one(rig)
    p['peak_premium'] = 110
    rig.quote(64.9, 65.0)
    orig = rig.fake._match
    rig.fake._match = lambda o: None                  # sells rest...
    rig.step(secs=1, n=10)
    rig.fake._match = orig                            # ...then the market fills
    rig.quote(60.0, 60.1)
    rig.step(secs=1, n=6)
    assert p['state'] == 'CLOSED' and rig.fake.net(SYM) == 0
    assert len(rig.etf_orders('S')) >= 3              # re-priced by cancel + replace (O4)


def test_flatten_file_exits_everything(rig):
    p = open_one(rig)
    open(os.path.join(rig.state, 'FLATTEN'), 'w').close()
    rig.step(n=3)
    assert p['state'] == 'CLOSED' and p['exit_type'] == 'FLATTEN'


# -- shared strike (O6) --------------------------------------------------------------------------
def test_a_strike_another_bot_holds_is_not_entered(tmp_path, monkeypatch):
    """Same-product collision guard (the gate matters only when ETF shares BTST's NRML)."""
    monkeypatch.setattr(KS, 'HOME', tmp_path / 'neo')
    rig = Rig(tmp_path, product='NRML')
    rig.quote(99.95, 100.0)
    rig.step()
    btst = KCN.NeoClient('btst', http=rig.fake, current_fn=lambda: KS.Session(
        1, '', '', 'https://x.kotaksecurities.com', 'T', 'S', None, 'C', 'U'))
    btst.place(KCN.OrderRequest(SYM, '1', 'B', 65, '100.00', btst.new_tag()))
    k = rig.intent()
    rig.step()
    assert k not in rig.eng.pos
    assert rig.etf_orders() == [] and 'not ours' in json.dumps(rig.eng.j.load()[1][-1])


def test_btst_lot_on_the_same_strike_is_never_sold(tmp_path, monkeypatch):
    monkeypatch.setattr(KS, 'HOME', tmp_path / 'neo')
    rig = Rig(tmp_path, skip_foreign_strikes=False)
    rig.quote(99.95, 100.0)
    rig.step()
    btst = KCN.NeoClient('btst', http=rig.fake, current_fn=lambda: KS.Session(
        1, '', '', 'https://x.kotaksecurities.com', 'T', 'S', None, 'C', 'U'))
    btst.place(KCN.OrderRequest(SYM, '1', 'B', 65, '100.00', btst.new_tag()))
    assert rig.fake.net(SYM) == 65
    p = open_one(rig)
    rig.quote(110.0, 110.1)
    rig.step(n=4)
    assert p['state'] == 'CLOSED' and rig.fake.net(SYM) == 65
    assert btst.own_order_net(SYM) == 65


# -- restart (R5) ------------------------------------------------------------------------------
def test_restart_mid_exit_settles_the_working_sell_without_a_second(rig):
    p = open_one(rig)
    p['peak_premium'] = 110
    rig.quote(64.9, 65.0)
    orig = rig.fake._match
    rig.fake._match = lambda o: None
    rig.step()                                        # exit starts, a sell rests
    assert len([o for o in rig.etf_orders('S') if o['ordSt'] == 'open']) == 1
    rig.restart()
    rig.fake._match = orig
    rig.quote(64.9, 65.0)
    rig.step(secs=1, n=8)
    q = rig.pos()
    assert q['state'] == 'CLOSED' and rig.fake.net(SYM) == 0


def test_restart_mid_entry_resolves_by_tag(rig):
    orig = rig.fake._match
    rig.fake._match = lambda o: None
    k = rig.intent()
    rig.step()
    assert rig.pos(k)['state'] == 'ENTRY'
    rig.restart()
    rig.fake._match = orig
    rig.quote(99.95, 100.0)
    rig.step(n=6)
    assert rig.pos(k)['state'] == 'OPEN' and len(rig.etf_orders('B')) == 1


# -- risk (R1/R2) --------------------------------------------------------------------------------
def test_daily_loss_limit_stops_new_entries(rig):
    p = open_one(rig)
    p['peak_premium'] = 110
    rig.quote(50.0, 50.1)
    rig.step(n=4)
    assert p['state'] == 'CLOSED' and p['realised'] <= -3000
    rig.quote(99.95, 100.0)
    rig.intent(key='NIFTY_PE_10minute_20261130_H1')
    rig.step()
    assert 'NIFTY_PE_10minute_20261130_H1' not in rig.eng.pos and 'daily loss' in rig.eng.halt


def test_two_entry_faults_halt_entries(rig):
    rig.fake.reject(1009, 'price band')
    rig.intent()
    rig.step()
    rig.fake.reject(1009, 'price band')
    rig.intent(key='NIFTY_PE_10minute_20261130_H1')
    rig.step()
    assert rig.eng.faults == 2 and rig.eng.halt.startswith('fault streak')


def test_a_429_is_not_a_fault(rig, monkeypatch):
    real, calls = rig.broker.place, []

    def place(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise KCN.Rejected('429', 'rate limited (HTTP 429)')   # what the client raises on 429
        return real(*a, **k)
    monkeypatch.setattr(rig.broker, 'place', place)
    k = rig.intent()
    rig.step(n=14)                                    # 5 s back-off (review M3), then the buy
    assert rig.eng.faults == 0 and rig.pos(k)['state'] == 'OPEN' and len(calls) == 2


# -- carry over a day (F16 / R6) ------------------------------------------------------------------
def test_unsold_lot_is_carried_and_sold_next_morning(rig):
    p = open_one(rig)
    rig.now = rig.now.replace(hour=15, minute=29, second=0)
    p['entry_time'] = rig.now.isoformat()
    orig = rig.fake._match
    rig.fake._match = lambda o: None
    rig.quote(100.5, 100.6)
    rig.step(secs=2, n=40)                            # past 15:29:50 and the close
    assert p['state'] == 'EXIT' and rig.fake.net(SYM) == 65
    assert not [o for o in rig.etf_orders('S') if o['ordSt'] == 'open']   # nothing left working
    assert 'NOT SOLD' in rig.texts()
    # next session: NEO's order book is a DAY book
    rig.fake.orders[:] = [o for o in rig.fake.orders if o['ordSt'] == 'complete' and False]
    rig.fake._match = orig
    held_overnight = 65

    def net(s):
        return held_overnight + sum(int(o['fldQty']) * (1 if o['trnsTp'] == 'B' else -1)
                                    for o in rig.fake.orders if o['trdSym'] == s)
    rig.fake.net = net
    rig.broker.account_net = lambda s: net(s)         # the MIS row shows the carried lot
    rig.now = dt.datetime(2026, 12, 1, 9, 14, 0, tzinfo=IST)
    rig.quote(101.0, 101.1)
    rig.step(secs=1, n=40)
    assert p['carry'] and p['carried_qty'] == 65
    rig.step(secs=1, n=40)
    assert p['state'] == 'CLOSED' and net(SYM) == 0
    first_sell = min(o['ordDtTm'] for o in rig.etf_orders('S'))
    assert first_sell                                  # sold only after 09:15:05


# -- journal integrity (F23) ----------------------------------------------------------------------
def test_torn_last_line_is_ignored_and_corrupt_middle_blocks_entries(rig):
    open_one(rig)
    path = rig.eng.j.path
    with open(path, 'a') as f:
        f.write('{"ev": "EXIT_ST')                     # power cut mid-append
    rig.restart()
    assert rig.eng.corrupt is None and rig.pos()['state'] == 'OPEN'
    lines = open(path).read().split('\n')
    lines.insert(2, 'garbage')
    open(path, 'w').write('\n'.join(lines))
    rig.restart()
    assert rig.eng.corrupt and rig.pos()['state'] == 'OPEN'
    rig.intent(key='NIFTY_PE_10minute_20261130_H1')
    rig.step()
    assert 'NIFTY_PE_10minute_20261130_H1' not in rig.eng.pos


def test_c1_a_carried_lot_with_no_positions_row_is_never_sold(rig):
    """Review C1: the broker squared the MIS lot off overnight; the journal still says 65.
    Selling it would be a naked short. No row -> nothing sold, closed EXTERNAL, alerted."""
    p = open_one(rig)
    rig.now = rig.now.replace(hour=15, minute=29, second=0)
    p['entry_time'] = rig.now.isoformat()
    orig = rig.fake._match
    rig.fake._match = lambda o: None
    rig.quote(100.5, 100.6)
    rig.step(secs=2, n=40)
    assert p['state'] == 'EXIT'
    rig.fake.orders[:] = []                           # new day: empty DAY book, lot squared off
    rig.fake._match = orig
    rig.now = dt.datetime(2026, 12, 1, 9, 14, 0, tzinfo=IST)
    rig.quote(101.0, 101.1)
    rig.step(secs=1, n=150)
    assert rig.etf_orders('S') == [] and p['state'] == 'CLOSED'
    assert p['exit_type'].endswith('+EXTERNAL') and p.get('realised_estimated')
    assert 'NO MIS row' in rig.texts()


def test_h1_one_empty_book_read_never_abandons_an_open_lot(rig):
    p = open_one(rig)
    real = rig.broker.own_orders
    rig.broker.own_orders = lambda: []                # one spurious 5203 "no data"
    rig.eng._last_reconcile = 0
    rig.step()
    rig.broker.own_orders = real
    rig.eng._last_reconcile = 0
    rig.step()
    assert p['state'] == 'OPEN' and p['qty'] == 65


def test_h3_no_price_during_an_exit_steps_down_from_the_last_bid(rig):
    p = open_one(rig)
    rig.quote(100.5, 100.6)
    rig.step()
    orig = rig.fake._match
    rig.fake._match = lambda o: None
    open(os.path.join(rig.state, 'FLATTEN'), 'w').close()
    rig.step()
    assert p['state'] == 'EXIT'
    rig.feed._q[SYM].bid = 0                          # quotes die mid-exit
    rig.step(secs=2, n=25, refresh=True)
    assert p.get('stale_exit') and 'stepping down' in rig.texts()
    prices = [float(o['prc']) for o in rig.etf_orders('S')]
    assert prices[-1] < prices[0]                     # cumulative step-down, not the frozen bid


def test_h5_after_the_close_a_stuck_exit_is_not_stepped_all_night(rig):
    p = open_one(rig)
    rig.now = rig.now.replace(hour=15, minute=29, second=0)
    p['entry_time'] = rig.now.isoformat()
    rig.fake._match = lambda o: None
    rig.quote(100.5, 100.6)
    rig.step(secs=2, n=40)
    calls, rows = len(rig.fake.calls), len(open(rig.eng.j.path).read().splitlines())
    rig.step(secs=30, n=120, refresh=False)           # an hour after the close
    assert len(rig.fake.calls) == calls               # no NEO reads
    assert len(open(rig.eng.j.path).read().splitlines()) - rows < 5


# -- traceability review (2026-11-28) -------------------------------------------------------------
@pytest.mark.parametrize('kw,why', [({'signal_tf': '15minute'}, 'not a NIFTY 10M intent'),
                                    ({'underlying': 'BANKNIFTY'}, 'not a NIFTY 10M intent')])
def test_only_nifty_10m_reaches_orders(rig, kw, why):
    rig.intent(**kw)
    rig.step()
    assert rig.etf_orders() == [] and why in json.dumps(rig.eng.j.load()[1][-1])


@pytest.mark.parametrize('hhmm', [(10, 30, 5), (11, 29, 50), (14, 15, 5), (14, 44, 50)])
def test_no_entry_inside_a_blocked_window(rig, hhmm):
    rig.now = rig.now.replace(hour=hhmm[0], minute=hhmm[1], second=hhmm[2])
    rig.intent()
    rig.step()
    assert rig.etf_orders() == [] and 'blocked entry window' in json.dumps(rig.eng.j.load()[1][-1])


def test_last_entry_1450_allows_the_whole_minute_then_stops(rig):
    rig.now = rig.now.replace(hour=14, minute=50, second=30)
    k = rig.intent()
    rig.step(n=2)
    assert rig.pos(k)['state'] == 'OPEN'
    rig.now = rig.now.replace(hour=14, minute=51, second=5)
    rig.intent(key='NIFTY_PE_10minute_20261130_H2')
    rig.step()
    assert 'NIFTY_PE_10minute_20261130_H2' not in rig.eng.pos


def test_live_eod_1515_exits_before_the_broker(rig):
    rig.eng.eod_fn = lambda d: dt.time(15, 15)        # what __main__ wires: min(15:25, eod_time)
    p = open_one(rig)
    rig.now = rig.now.replace(hour=15, minute=14, second=58)
    p['entry_time'] = rig.now.isoformat()
    rig.quote(100.5, 100.6)
    rig.step(n=6)
    assert p['state'] == 'CLOSED' and p['exit_type'] == 'EOD'


def test_journal_records_rule_quote_slippage_and_charges(rig):
    p = open_one(rig)
    rows = rig.eng.j.load()[1]
    send = [r for r in rows if r.get('ev') == 'ENTRY_SEND'][-1]
    assert send['quote']['ask'] == 100.0 and send['signal_age_s'] is not None
    assert [r for r in rows if r.get('ev') == 'ENTRY_PLACED'][-1]['place_ms'] >= 0
    assert p['entry_slip'] == pytest.approx(0.0)
    rig.quote(110.0, 110.1)
    rig.step(n=3)
    rows = rig.eng.j.load()[1]
    start = [r for r in rows if r.get('ev') == 'EXIT_START'][-1]
    assert start['pos']['exit_detail']['rule'] == 'TP' and start['pos']['exit_detail']['how'] == 'bid_touch'
    assert start['quote']['bid'] == 110.0
    c = rig.pos()
    assert c['charges_est'] > 0 and c['net_est'] == pytest.approx(c['realised'] - c['charges_est'])
    assert c['exit_slip_vs_level'] == pytest.approx(0.0)
    assert 'place buy' in rig.eng.neo_stats


def test_q7_short_after_the_last_close_is_still_watched(rig):
    p = open_one(rig)
    rig.quote(110.0, 110.1)
    rig.step(n=3)
    assert p['state'] == 'CLOSED' and rig.eng._active() == []
    # a stray own sell fills AFTER the close (e.g. an unclear sell surfacing late)
    rig.broker.place(SYM, '1', 'S', 65, 109.0, rig.broker.new_tag(), reduce_only=True)
    assert rig.fake.net(SYM) == -65
    for _ in range(4):                                # no check(): the account IS short here
        rig.now += dt.timedelta(seconds=16)
        rig.feed._q[SYM].ts = rig.epoch()
        rig.eng.step(rig.now)
    assert p.get('covered') and rig.fake.net(SYM) == 0


# -- review R7 (2026-11-28): an undocumented product label is UNREADABLE, not "no row" --

@pytest.mark.parametrize('prod,want', [('MIS', 65), (' mis ', 65), ('', 65), ('NRML', None)])
def test_r7_account_net_reads_our_product_row(rig, monkeypatch, prod, want):
    row = KCN.PositionView(symbol='NIFTYX', product=prod, net_qty=65)
    monkeypatch.setattr(rig.broker.c, 'positions_for', lambda symbol: [row])
    assert rig.broker.account_net('NIFTYX') == want


def test_r7_an_unknown_product_label_raises_so_every_caller_fails_closed(rig, monkeypatch):
    row = KCN.PositionView(symbol='NIFTYX', product='XYZ', net_qty=65)
    monkeypatch.setattr(rig.broker.c, 'positions_for', lambda symbol: [row])
    with pytest.raises(KCN.Unavailable, match='unknown label'):
        rig.broker.account_net('NIFTYX')


# -- review B1 (2026-11-28): a low positions row alone never ends management of a lot --

def test_b1_a_lagging_positions_row_never_closes_a_lot_we_still_hold(rig, monkeypatch):
    """The MIS row reads net 0 (lag / a quantity read never measured) while our own book
    says we bought 65 and nothing sold outside. Nothing is sold (never a naked short),
    the lot is NOT closed, the owner is told; when the row agrees again it sells."""
    p = open_one(rig)
    real = rig.broker.c.positions_for
    monkeypatch.setattr(rig.broker.c, 'positions_for',
                        lambda symbol: [KCN.PositionView(symbol=SYM, product='MIS', net_qty=0)])
    rig.step(secs=15)                                 # past positions_lag_s
    open(os.path.join(rig.state, 'FLATTEN'), 'w').close()
    rig.step(n=200)                                   # 100 s: far past the old 30 s close
    assert p['state'] == 'EXIT' and rig.etf_orders('S') == []
    assert rig.fake.net(SYM) == 65
    assert 'NOT closing' in rig.texts()
    monkeypatch.setattr(rig.broker.c, 'positions_for', real)
    rig.step(n=10)
    assert p['state'] == 'CLOSED' and not p['exit_type'].endswith('+EXTERNAL')
    assert rig.fake.net(SYM) == 0


def test_b1_a_sell_in_the_app_is_confirmed_by_the_book_and_closed_external(rig):
    """The owner exits by hand in the NEO app (an untagged MIS sell). The row AND the
    book agree: ETF sells nothing and closes the lot as EXTERNAL."""
    p = open_one(rig)
    rig.step(secs=15)
    rig.fake._new_order({'pt': 'L', 'ts': SYM, 'tt': 'S', 'qt': 65, 'pr': '99.00',
                         'ig': '', 'pc': p.get('product') or rig.cfg['product']})
    assert rig.fake.net(SYM) == 0
    open(os.path.join(rig.state, 'FLATTEN'), 'w').close()
    rig.step(n=100)
    assert rig.etf_orders('S') == [] and p['state'] == 'CLOSED'
    assert p['exit_type'].endswith('+EXTERNAL')
    assert 'NOT closing' not in rig.texts()
