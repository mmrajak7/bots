"""The live naked EXIT engine (`zebra/naked_exit.py`, naked live build P4).

Rules come from `naked_rules.step` (tested on its own and against the shadow);
these tests pin what THIS module adds: the clock (TP every poll, STOP/TIME once
per 5-minute bucket), the sale (rehearsal books at the bid; live chases on NEO
with the tag persisted first), resume after a restart, the dry-run refusal,
fees at the NEO rate, and that exits never depend on the entry switch.

Run:  cd Helper && python -m pytest zebra/tests/test_naked_exit.py -v
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2]))

from test_naked_entry import (_iso, store, parent, Sent, go, naked,   # noqa: E402,F401
                              NOW, QUOTES, _q, LOT)
from kotak_neo import client as C           # noqa: E402
from kotak_neo import session as S          # noqa: E402
from kotak_neo.fake import FakeNeo          # noqa: E402
from zebra import config as cfg             # noqa: E402
from zebra import naked_exit as nx          # noqa: E402
from zebra import naked_rules               # noqa: E402
from zebra.trade_store import build_naked_record   # noqa: E402

SYM = 'TESTCO26DEC1000CE'
TOK = '555001'
SESS = S.Session(3, '2026-12-02', 'c', 'https://x', 'T', 'SID', None, 'CK', 'U')
T0 = datetime(2026, 12, 2, 11, 0, 5)


class Refresh:
    def __call__(self, gen, who):
        return S.Result(S.STILL_ALIVE, SESS, 'scripted')


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _left(monkeypatch):
    monkeypatch.setattr(naked_rules, 'sessions_left', lambda exp, today: 15)


@pytest.fixture
def neo(tmp_path, monkeypatch):
    monkeypatch.setattr(S, 'HOME', tmp_path / 'neo')
    return FakeNeo()


def engine(store, spot=1000.0, dry_run=False, client=None, clock=None):
    sent = Sent()
    e = nx.NakedExitEngine(dry_run=dry_run, log=lambda m: None, send=sent, store=store,
                           client=client, spot_fn=lambda kite, stocks: {s: spot for s in stocks},
                           clock=clock)
    return e, sent


def rehearsal_record(store):
    out, _ = go(store, parent(store))
    assert out.state == 'entered'
    return naked(store)[0]


def live_record(store, neo, client):
    p = parent(store)
    # the long really is held at NEO
    neo.set_quote(SYM, 21.0, 21.5)
    client.place(C.OrderRequest(SYM, TOK, 'B', LOT, '21.50', client.new_tag()))
    rec = build_naked_record(p, long_symbol=SYM, long_strike=1000.0, lot_size=LOT,
                             fill_price=21.5, now=NOW, quote=_q(), rehearsal=False)
    rec.update(neo_symbol=SYM, neo_token=TOK, entry_tag='zbr-test')
    return store.add_naked_live(rec)


def client_for(neo):
    return C.NeoClient('zbr', http=neo, current_fn=lambda: SESS, refresh_fn=Refresh())


# ── rehearsal ──────────────────────────────────────────────────────────────

def test_tp_on_spot_books_a_rehearsal_at_the_bid(store):
    r = rehearsal_record(store)
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    e, sent = engine(store, spot=1041.0)
    e.poll(None, now=T0)
    t = store.find(r['id'])
    assert t['status'] == 'exited' and t['exit_reason'] == 'tp'
    assert t['exit_debit'] == 40.0 and t['exit_touch'] == 40.0
    assert t['pnl'] == round((40.0 - 21.5) * LOT, 2)
    assert any('TP' in m for m in sent)


def test_nothing_happens_inside_the_band(store):
    r = rehearsal_record(store)
    QUOTES[SYM] = _q(ask=22.5, bid=22.0)
    e, _ = engine(store, spot=1010.0)
    assert e.poll(None, now=T0) == []
    assert store.find(r['id'])['status'] == 'entered'


def test_stop_needs_two_bucket_polls_not_two_5s_polls(store):
    r = rehearsal_record(store)
    QUOTES[SYM] = _q(ask=10.5, bid=10.0)          # below 50% of 21.5
    e, _ = engine(store, spot=980.0)
    e.poll(None, now=T0)
    e.poll(None, now=T0 + timedelta(seconds=5))    # same bucket: no second count
    e.poll(None, now=T0 + timedelta(seconds=60))
    assert store.find(r['id'])['status'] == 'entered'
    e.poll(None, now=T0 + timedelta(minutes=5))    # next bucket: confirmed
    t = store.find(r['id'])
    assert t['status'] == 'exited' and t['exit_reason'] == 'stop' and t['exit_debit'] == 10.0


def test_a_recovering_poll_resets_the_stop_count(store):
    r = rehearsal_record(store)
    e, _ = engine(store, spot=980.0)
    QUOTES[SYM] = _q(ask=10.5, bid=10.0)
    e.poll(None, now=T0)
    QUOTES[SYM] = _q(ask=15.5, bid=15.0)
    e.poll(None, now=T0 + timedelta(minutes=5))
    QUOTES[SYM] = _q(ask=10.5, bid=10.0)
    e.poll(None, now=T0 + timedelta(minutes=10))
    assert store.find(r['id'])['status'] == 'entered'


def test_unreliable_book_never_counts_toward_a_stop(store):
    r = rehearsal_record(store)
    QUOTES[SYM] = _q(ask=10.5, bid=10.0, reliable=False)
    e, _ = engine(store, spot=980.0)
    for k in range(4):
        e.poll(None, now=T0 + timedelta(minutes=5 * k))
    assert store.find(r['id'])['status'] == 'entered'


def test_no_value_exit_inside_the_opening_buffer(store):
    r = rehearsal_record(store)
    QUOTES[SYM] = _q(ask=10.5, bid=10.0)
    e, _ = engine(store, spot=980.0)
    open_ = datetime(2026, 12, 3, 9, 15, 30)
    e.poll(None, now=open_)
    e.poll(None, now=open_ + timedelta(minutes=5))   # 09:20:30, still in the 15-min buffer
    assert store.find(r['id'])['status'] == 'entered'


def test_time_exit_fires_on_the_bucket(store, monkeypatch):
    r = rehearsal_record(store)
    monkeypatch.setattr(naked_rules, 'sessions_left', lambda exp, today: 6)
    QUOTES[SYM] = _q(ask=25.5, bid=25.0)
    e, _ = engine(store, spot=1010.0)
    e.poll(None, now=T0)
    t = store.find(r['id'])
    assert t['status'] == 'exited' and t['exit_reason'] == 'time'


def test_delivery_window_alerts_once_a_day(store, monkeypatch):
    rehearsal_record(store)
    monkeypatch.setattr(naked_rules, 'sessions_left', lambda exp, today: 3)
    QUOTES[SYM] = _q(ask=25.5, bid=25.0, reliable=False)    # cannot be sold: stays open
    e, sent = engine(store, spot=1010.0)
    e.poll(None, now=T0)
    e.poll(None, now=T0 + timedelta(minutes=5))
    assert sum('delivery' in m for m in sent) == 1


def test_exits_run_even_when_entries_are_switched_off(store, monkeypatch):
    r = rehearsal_record(store)
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'off')
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    e, _ = engine(store, spot=1041.0)
    e.poll(None, now=T0)
    assert store.find(r['id'])['status'] == 'exited'


# ── live (NEO fake) ──────────────────────────────────────────────────────────

def test_live_tp_sells_on_neo_and_books_the_fill_at_the_neo_rate(store, neo, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'live')
    monkeypatch.setattr(cfg, 'NAKED_BROKERAGE_PER_ORDER', 0.0)
    c = client_for(neo)
    r = live_record(store, neo, c)
    neo.set_quote(SYM, 40.0, 40.5)
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    e, sent = engine(store, spot=1041.0, client=c)
    e.poll(None, now=T0)                           # trigger + the sell goes out
    assert store.find(r['id'])['status'] == 'closing'
    e.poll(None, now=T0 + timedelta(seconds=5))    # the fill is read back and booked
    t = store.find(r['id'])
    assert t['status'] == 'exited' and t['exit_reason'] == 'tp' and t['exit_debit'] == 40.0
    assert neo.net(SYM) == 0
    assert t['fees']['brokerage'] == 0.0 and t['fees']['stt'] > 0
    # ONE leg: two orders, priced from the real fills, never a modelled short
    assert t['fees']['orders'] == 2 and t['fees']['approx'] is False
    assert t['fees']['basis'] == 'full'
    assert t['exit_chase']['working_tag'] == '' and t['exit_chase']['fills']


def test_live_chase_tag_is_persisted_before_the_sell(store, neo, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'live')
    c = client_for(neo)
    r = live_record(store, neo, c)
    neo.set_quote(SYM, 40.0, 40.5)
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    seen = []
    real_place = c.place

    def place(req, reduce_only=False):
        seen.append(store.find(r['id'])['exit_chase']['working_tag'])
        return real_place(req, reduce_only=reduce_only)
    monkeypatch.setattr(c, 'place', place)
    e, _ = engine(store, spot=1041.0, client=c)
    e.poll(None, now=T0)
    assert seen and seen[0].startswith('zbr-')


def test_a_restarted_monitor_resumes_the_chase_not_a_second_one(store, neo, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'live')
    c = client_for(neo)
    r = live_record(store, neo, c)
    neo.set_quote(SYM, 30.0, 40.5)                 # real bid 30: a sell at 40 rests
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    clk = Clock()
    e1, _ = engine(store, spot=1041.0, client=c, clock=clk)
    e1.poll(None, now=T0)
    t = store.find(r['id'])
    assert t['status'] == 'closing' and t['exit_chase']['working_tag']
    assert len(neo.placed('S')) == 1
    # "restart": a fresh engine, same store
    e2, _ = engine(store, spot=1041.0, client=c, clock=clk)
    e2.poll(None, now=T0 + timedelta(seconds=5))
    assert len(neo.placed('S')) == 1               # still the one working sell
    neo.set_quote(SYM, 40.0, 40.5)                 # the book crosses: it fills
    lines = e2.poll(None, now=T0 + timedelta(seconds=10))
    lines += e2.poll(None, now=T0 + timedelta(seconds=15))
    assert store.find(r['id'])['status'] == 'exited' and neo.net(SYM) == 0, (
        lines, store.find(r['id'])['exit_chase'])


def test_dry_run_never_sells_a_live_record_and_says_so_once(store, neo, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'live')
    c = client_for(neo)
    r = live_record(store, neo, c)
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    e, sent = engine(store, spot=1041.0, dry_run=True, client=c)
    e.poll(None, now=T0)
    e.poll(None, now=T0 + timedelta(seconds=5))
    assert store.find(r['id'])['status'] == 'entered'
    assert neo.placed('S') == []
    assert sum('dry-run' in m for m in sent) == 1


def test_a_position_sold_by_hand_hands_the_chase_to_a_human(store, neo, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'live')
    monkeypatch.setattr(cfg, 'NAKED_EXIT_MAX_REPRICES', 12)
    c = client_for(neo)
    r = live_record(store, neo, c)
    neo.set_quote(SYM, 40.0, 40.5)
    c.place(C.OrderRequest(SYM, TOK, 'S', LOT, '40.00', c.new_tag()), reduce_only=True)
    assert neo.net(SYM) == 0                       # flat already (sold in the app)
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    e, sent = engine(store, spot=1041.0, client=c)
    for k in range(13):
        e.poll(None, now=T0 + timedelta(seconds=5 * k))
    t = store.find(r['id'])
    assert t['status'] == 'closing' and t['exit_chase']['status'] == 'needs_human'
    assert len(neo.placed('S')) == 1               # only the hand sale; the engine sold nothing
    assert any('STOPPED' in m for m in sent)


def test_live_population_and_active_chase_flags(store, neo, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'live')
    c = client_for(neo)
    e, _ = engine(store, client=c)
    assert e.live_population() is False and e.has_active_chase() is False
    live_record(store, neo, c)
    e._mtime = None
    assert e.live_population() is True and e.open_count() == 1


# ── holiday probe ────────────────────────────────────────────────────────────

class KiteQ:
    def __init__(self, ts=None, boom=False):
        self.ts, self.boom = ts, boom

    def quote(self, keys):
        if self.boom:
            raise RuntimeError('down')
        return {keys[0]: {'last_trade_time': self.ts}}


def test_market_really_open_reads_todays_prints():
    now = datetime(2026, 12, 2, 10, 0)
    assert nx.market_really_open(KiteQ(datetime(2026, 12, 2, 9, 59)), now=now) is True
    assert nx.market_really_open(KiteQ(datetime(2026, 12, 1, 15, 29)), now=now) is False
    assert nx.market_really_open(KiteQ(boom=True), now=now) is None
    assert nx.market_really_open(KiteQ(None), now=now) is None


def test_closing_without_chase_state_starts_the_chase(store, neo, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'live')
    c = client_for(neo)
    r = live_record(store, neo, c)
    store.begin_close(r['id'], 'naked:tp')         # crash after the lock, before the chase
    store.update_trade_fields(r['id'], exit_trigger={'reason': 'tp', 'spot': 1041.0},
                              exit_touch=40.0)
    neo.set_quote(SYM, 40.0, 40.5)
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    e, sent = engine(store, spot=1041.0, client=c)
    e.poll(None, now=T0)
    e.poll(None, now=T0 + timedelta(seconds=5))
    assert store.find(r['id'])['status'] == 'exited' and neo.net(SYM) == 0
    assert any('chase starts now' in m for m in sent)


def test_a_chase_with_no_bid_is_said_out_loud(store, neo, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'live')
    c = client_for(neo)
    r = live_record(store, neo, c)
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    clk = Clock()
    e, sent = engine(store, spot=1041.0, client=c, clock=clk)
    neo.set_quote(SYM, 30.0, 40.5)                 # the sell at 40 rests
    e.poll(None, now=T0)
    # the Kite book goes bad AND its bid is below half the trigger bid (R9 floor)
    QUOTES[SYM] = _q(ask=40.5, bid=15.0, reliable=False)
    for k in range(1, 30):
        clk.t += 5
        e.poll(None, now=T0 + timedelta(seconds=5 * k))
    assert store.find(r['id'])['status'] == 'closing'
    assert sum('NO usable bid' in m for m in sent) == 1



def test_a_wide_book_bid_above_the_floor_still_sells(store, neo, monkeypatch):
    """Review R9: right after a stop collapse the book is often wide; the sale
    takes an unreliable bid at or above half the bid that fired the exit."""
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'live')
    c = client_for(neo)
    r = live_record(store, neo, c)
    QUOTES[SYM] = _q(ask=10.5, bid=10.0)           # a stop: 10 <= 50% of 21.5
    neo.set_quote(SYM, 9.0, 12.0)
    clk = Clock()
    e, _ = engine(store, spot=980.0, client=c, clock=clk)
    e.poll(None, now=T0)
    e.poll(None, now=T0 + timedelta(minutes=5))    # confirmed -> the sell at 10 rests
    QUOTES[SYM] = _q(ask=12.0, bid=9.0, reliable=False)    # wide, but 9 >= 5
    for k in range(1, 6):
        clk.t += 11                                 # past the re-price interval
        e.poll(None, now=T0 + timedelta(minutes=5, seconds=11 * k))
    t = store.find(r['id'])
    assert t['status'] == 'exited' and t['exit_reason'] == 'stop' and neo.net(SYM) == 0


def test_an_order_left_working_overnight_does_not_strand_the_exit(store, neo, monkeypatch):
    """Review R1: a DAY order from yesterday is absent from today's book. The
    account position decides what is left; the chase continues."""
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'live')
    c = client_for(neo)
    r = live_record(store, neo, c)
    neo.set_quote(SYM, 30.0, 40.5)
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    clk = Clock()
    e, sent = engine(store, spot=1041.0, client=c, clock=clk)
    e.poll(None, now=T0)                           # sell at 40 rests
    # overnight: the exchange expired it; simulate today's empty book
    for o in neo.orders:
        if o['trnsTp'] == 'S':
            neo.orders.remove(o)
    clk.t += 86400
    neo.set_quote(SYM, 40.0, 40.5)
    for k in range(1, 4):
        e.poll(None, now=T0 + timedelta(days=1, seconds=5 * k))
    t = store.find(r['id'])
    assert t['status'] == 'exited' and neo.net(SYM) == 0
    assert not any('STOPPED' in m for m in sent)


def test_end_of_session_cancels_a_working_sell(store, neo, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'live')
    c = client_for(neo)
    r = live_record(store, neo, c)
    neo.set_quote(SYM, 30.0, 40.5)
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    e, _ = engine(store, spot=1041.0, client=c)
    e.poll(None, now=T0)
    assert any(o['ordSt'] == 'open' for o in neo.placed('S'))
    e.end_of_session()
    assert not any(o['ordSt'] == 'open' for o in neo.placed('S'))
    assert store.find(r['id'])['exit_chase']['working_tag'] == ''


def test_a_stuck_exit_is_nagged_once_a_day_with_the_booking_command(store, neo, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'live')
    c = client_for(neo)
    r = live_record(store, neo, c)
    store.begin_close(r['id'], 'naked:stop')
    store.update_trade_fields(r['id'], exit_chase={'status': 'needs_human', 'note': 'x',
                                                    'sold': 0, 'remaining': LOT})
    e, sent = engine(store, spot=1000.0, client=c)
    monkeypatch.setattr(e, '_step_closing', lambda kite, t, now: '')
    e.poll(None, now=T0)
    e.poll(None, now=T0 + timedelta(minutes=5))
    nags = [m for m in sent if 'needs a human' in m]
    assert len(nags) == 1 and 'zebra close %s' % r['id'] in nags[0] and '--reason stop' in nags[0]


def test_post_close_extension_steps_only_exits_already_selling(store, monkeypatch):
    r = rehearsal_record(store)
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    e, _ = engine(store, spot=1041.0)              # TP would fire on a normal poll
    e.poll(None, now=T0, closing_only=True)
    assert store.find(r['id'])['status'] == 'entered'


def test_a_record_that_raises_every_poll_is_reported_unwatched(store, monkeypatch):
    rehearsal_record(store)
    e, sent = engine(store, spot=1010.0)
    monkeypatch.setattr(e, '_step_open', lambda *a, **k: (_ for _ in ()).throw(KeyError('x')))
    for k in range(4):
        e.poll(None, now=T0 + timedelta(seconds=5 * k))
    assert sum('NOT being watched' in m for m in sent) == 1


def test_zebra_close_books_a_hand_exit_of_a_stuck_naked_record(store, neo, monkeypatch):
    from zebra import __main__ as cli
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'live')
    c = client_for(neo)
    r = live_record(store, neo, c)
    store.begin_close(r['id'], 'naked:stop')
    monkeypatch.setattr('zebra.trade_store.get_store', lambda *a, **k: store)
    args = type('A', (), {'id': r['id'], 'exit_debit': 9.5, 'exit_spot': None,
                          'reason': 'stop'})()
    cli.cmd_close(args)
    t = store.find(r['id'])
    assert t['status'] == 'exited' and t['exit_debit'] == 9.5 and t['exit_reason'] == 'stop'
