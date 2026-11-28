"""Review fixes P7e (2026-11-28) to the naked EXIT engine.

Three independent reviews (architect, trader, ops) found the ways a LIVE exit
could stop, orphan an order, or go quiet. Each test below pins one fix:

  * the chase never gives up; troubles alert and RE-alert (H1/A6/F4)
  * a wide book is sold into by exit reason, the TP/TIME floor decays (M3)
  * the walk-down under the bid is capped (M3)
  * dry run mid-exit CANCELS the working sell (A1)
  * `zebra close` refuses while a sell works or the lot is held (A2)
  * a chase carried overnight waits out the opening buffer and resets (M3)
  * a blind open position is alerted (F1); "nothing sold" alerts once (A3/F2)
  * the close lock and chase are local-only writes that survive a Drive
    resync from an older copy (A5 + advisor)
  * the tick comes from the record (NEO scrip row), not config

Run:  cd Helper && python -m pytest zebra/tests/test_naked_exit_p7e.py -v
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2]))

from test_naked_entry import (_iso, store, parent, Sent, go, naked,   # noqa: E402,F401
                              NOW, QUOTES, _q, LOT)
from test_naked_exit import (SYM, TOK, T0, Clock, _left, neo, engine,  # noqa: E402,F401
                             live_record, client_for, rehearsal_record)
from kotak_neo import client as C           # noqa: E402
from zebra import config as cfg             # noqa: E402
from zebra import naked_exit as nx          # noqa: E402


@pytest.fixture(autouse=True)
def _live(monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'live')


def _fire_tp(store, neo, c, clk, real_bid=30.0):
    """TP fires at Kite bid 40; NEO's real bid is lower, so the sell RESTS."""
    r = live_record(store, neo, c)
    neo.set_quote(SYM, real_bid, 40.5)
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    e, sent = engine(store, spot=1041.0, client=c, clock=clk)
    e.poll(None, now=T0)
    assert store.find(r['id'])['status'] == 'closing'
    return r, e, sent


# -- never gives up -----------------------------------------------------------

def test_troubles_alert_then_realert_after_the_repeat_interval(store, neo, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_EXIT_ALERT_REPEAT_SEC', 300)
    c = client_for(neo)
    clk = Clock()
    r, e, sent = _fire_tp(store, neo, c, clk)
    monkeypatch.setattr(c, 'net_held', lambda *a, **k: (_ for _ in ()).throw(
        C.Unavailable('positions down')))
    monkeypatch.setattr(c, 'order_book', lambda *a, **k: (_ for _ in ()).throw(
        C.Unavailable('book down')))
    for k in range(1, 100):                        # 495 s of a dead NEO
        clk.t += 5
        e.poll(None, now=T0 + timedelta(seconds=5 * k))
    t = store.find(r['id'])
    assert t['exit_chase']['status'] == 'active'    # never handed off
    stuck = [m for m in sent if 'STUCK' in m]
    assert len(stuck) == 2                          # at 12 steps, then once more after 300 s


def test_a_legacy_needs_human_chase_resumes(store, neo):
    c = client_for(neo)
    r = live_record(store, neo, c)
    store.begin_close(r['id'], 'naked:stop')
    store.update_trade_fields(r['id'], exit_trigger={'reason': 'stop', 'at': '2026-12-02 11:00:00'},
                              exit_touch=21.0, exit_chase_day='2026-12-02',
                              exit_chase={'bot': 'zbr', 'symbol': SYM, 'token': TOK,
                                          'tick': 0.05, 'remaining': LOT,
                                          'status': 'needs_human', 'reprices': 12})
    neo.set_quote(SYM, 21.0, 21.5)
    QUOTES[SYM] = _q(ask=21.5, bid=21.0)
    e, _ = engine(store, spot=990.0, client=c)
    e.poll(None, now=T0)
    e.poll(None, now=T0 + timedelta(seconds=5))
    assert store.find(r['id'])['status'] == 'exited' and neo.net(SYM) == 0


# -- what a wide book may be sold into -------------------------------------------

def test_a_tp_will_not_dump_into_a_pulled_book_until_the_floor_decays(store, neo, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_EXIT_FLOOR_DECAY_SEC', 600)
    c = client_for(neo)
    clk = Clock()
    r, e, _ = _fire_tp(store, neo, c, clk, real_bid=30.0)
    QUOTES[SYM] = _q(ask=40.5, bid=30.0, reliable=False)    # 30 < 90% of 40
    for k in range(1, 100):                                  # ~8 min: held
        clk.t += 5
        e.poll(None, now=T0 + timedelta(seconds=5 * k))
    assert store.find(r['id'])['status'] == 'closing' and neo.net(SYM) == LOT
    for k in range(100, 140):                                # past 10 min: STOP floor
        clk.t += 5
        e.poll(None, now=T0 + timedelta(seconds=5 * k))
    t = store.find(r['id'])
    assert t['status'] == 'exited' and neo.net(SYM) == 0 and t['exit_reason'] == 'tp'


def test_a_reliable_book_is_always_sold_at_its_bid(store, neo):
    c = client_for(neo)
    clk = Clock()
    r, e, _ = _fire_tp(store, neo, c, clk, real_bid=30.0)
    QUOTES[SYM] = _q(ask=30.5, bid=30.0, reliable=True)     # moved, but a real book
    for k in range(1, 8):
        clk.t += 11
        e.poll(None, now=T0 + timedelta(seconds=11 * k))
    assert store.find(r['id'])['status'] == 'exited' and neo.net(SYM) == 0


def test_the_walk_down_never_goes_more_than_the_offset_under_the_bid(store, neo, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_EXIT_MAX_OFFSET_TICKS', 5)
    c = client_for(neo)
    clk = Clock()
    r, e, _ = _fire_tp(store, neo, c, clk, real_bid=1.0)    # nothing near fills
    for k in range(1, 80):
        clk.t += 11
        e.poll(None, now=T0 + timedelta(seconds=11 * k))
    prices = [float(o['prc']) for o in neo.placed('S')]
    assert len(prices) > 10 and min(prices) >= 40.0 - 5 * 0.05 - 1e-9
    assert store.find(r['id'])['exit_chase']['reprices'] > 5


# -- no orphaned sell --------------------------------------------------------------

def test_dry_run_mid_exit_cancels_the_working_sell(store, neo):
    c = client_for(neo)
    clk = Clock()
    r, e, sent = _fire_tp(store, neo, c, clk)
    assert any(o['ordSt'] == 'open' for o in neo.placed('S'))
    e.dry_run = True                                   # the kill switch tripped
    e.poll(None, now=T0 + timedelta(seconds=5))
    assert not any(o['ordSt'] == 'open' for o in neo.placed('S'))
    assert store.find(r['id'])['exit_chase']['working_tag'] == ''
    msg = [m for m in sent if 'DRY RUN' in m]
    assert len(msg) == 1 and 'No bot sell is working' in msg[0]


def test_zebra_close_refuses_while_a_sell_works_or_the_lot_is_held(store, neo, monkeypatch):
    from zebra import __main__ as cli
    from zebra import naked_neo
    c = client_for(neo)
    clk = Clock()
    r, e, _ = _fire_tp(store, neo, c, clk)            # a sell rests at NEO
    monkeypatch.setattr('zebra.trade_store.get_store', lambda *a, **k: store)
    monkeypatch.setattr(naked_neo, 'default_client', lambda: c)
    args = type('A', (), {'id': r['id'], 'exit_debit': 39.0, 'exit_spot': None,
                          'reason': 'tp', 'force': False})()
    with pytest.raises(SystemExit):
        cli.cmd_close(args)                             # working sell
    assert 'WORKING' in nx.hand_close_problem(store.find(r['id']), c)
    e.end_of_session()                                  # cancelled; lot still held
    assert 'HOLDS' in nx.hand_close_problem(store.find(r['id']), c)
    with pytest.raises(SystemExit):
        cli.cmd_close(args)
    c.place(C.OrderRequest(SYM, TOK, 'S', LOT, '30.00', c.new_tag()), reduce_only=True)
    assert nx.hand_close_problem(store.find(r['id']), c) == ''
    cli.cmd_close(args)
    assert store.find(r['id'])['status'] == 'exited'


def test_zebra_close_force_books_despite_the_check(store, neo, monkeypatch):
    from zebra import __main__ as cli
    from zebra import naked_neo
    c = client_for(neo)
    r = live_record(store, neo, c)
    store.begin_close(r['id'], 'naked:stop')
    monkeypatch.setattr('zebra.trade_store.get_store', lambda *a, **k: store)
    monkeypatch.setattr(naked_neo, 'default_client', lambda: c)
    args = type('A', (), {'id': r['id'], 'exit_debit': 9.5, 'exit_spot': None,
                          'reason': 'stop', 'force': True})()
    cli.cmd_close(args)
    assert store.find(r['id'])['status'] == 'exited'


# -- overnight ----------------------------------------------------------------------

def test_a_carried_exit_waits_out_the_opening_buffer_then_resets(store, neo):
    c = client_for(neo)
    clk = Clock()
    r, e, _ = _fire_tp(store, neo, c, clk)
    for k in range(1, 40):
        clk.t += 11
        e.poll(None, now=T0 + timedelta(seconds=11 * k))
    assert store.find(r['id'])['exit_chase']['reprices'] > 3
    e.end_of_session()
    placed = len(neo.placed('S'))
    clk.t += 86400
    morning = datetime(2026, 12, 3, 9, 16, 0)
    neo.set_quote(SYM, 40.0, 40.5)
    e.poll(None, now=morning)
    e.poll(None, now=morning + timedelta(minutes=5))
    assert len(neo.placed('S')) == placed              # nothing into the opening prints
    after = datetime(2026, 12, 3, 9, 31, 0)
    e.poll(None, now=after)
    t = store.find(r['id'])
    assert len(neo.placed('S')) == placed + 1
    assert float(neo.placed('S')[-1]['prc']) == 40.0   # re-price count reset: at the bid
    e.poll(None, now=after + timedelta(seconds=5))
    assert store.find(r['id'])['status'] == 'exited'


# -- alerts that say the right thing, once ---------------------------------------

def test_a_blind_open_position_is_alerted_once_then_on_repeat(store, neo, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_BLIND_ALERT_SEC', 300)
    monkeypatch.setattr(cfg, 'NAKED_EXIT_ALERT_REPEAT_SEC', 900)
    c = client_for(neo)
    live_record(store, neo, c)
    QUOTES[SYM] = _q(ask=40.5, bid=15.0, reliable=False)
    clk = Clock()
    e, sent = engine(store, spot=1010.0, clock=clk, client=c)
    for k in range(0, 61):                           # 300 s
        clk.t += 5
        e.poll(None, now=T0 + timedelta(seconds=5 * k))
    assert sum('BLIND' in m for m in sent) == 1
    for k in range(61, 300):                          # +1195 s
        clk.t += 5
        e.poll(None, now=T0 + timedelta(seconds=5 * k))
    assert sum('BLIND' in m for m in sent) == 2
    QUOTES[SYM] = _q(ask=22.5, bid=22.0)
    clk.t += 5
    e.poll(None, now=T0 + timedelta(seconds=1600))
    assert e._blind == {}


def test_nothing_sold_alerts_once_not_every_poll(store, neo):
    c = client_for(neo)
    clk = Clock()
    r, e, sent = _fire_tp(store, neo, c, clk)
    for o in list(neo.orders):                         # yesterday's sell expired ...
        if o['trnsTp'] == 'S':
            neo.orders.remove(o)
    c.place(C.OrderRequest(SYM, TOK, 'S', LOT, '30.00', c.new_tag()), reduce_only=True)
    clk.t += 86400                                      # ... and the lot left overnight
    for k in range(1, 50):
        clk.t += 5
        e.poll(None, now=datetime(2026, 12, 3, 10, 0) + timedelta(seconds=5 * k))
    assert sum('Book it at the true average' in m for m in sent) == 1
    assert store.find(r['id'])['status'] == 'closing'


# -- the store ----------------------------------------------------------------------

def test_local_only_close_lock_survives_a_drive_resync_from_an_older_copy(store, neo,
                                                                          monkeypatch):
    """Advisor (2026-11-28): the close lock and chase are local-only writes. A
    Drive copy taken BEFORE them must lose the merge -- on a periodic resync and
    on a fresh process's initialize()."""
    import copy
    import types
    from zebra.trade_store import ZebraStore
    c = client_for(neo)
    r = live_record(store, neo, c)
    drive_copy = copy.deepcopy(store.load_trades())   # what Drive holds: 'entered'
    clk = Clock()
    neo.set_quote(SYM, 30.0, 40.5)
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    e, _ = engine(store, spot=1041.0, client=c, clock=clk)
    e.poll(None, now=T0)
    assert store.find(r['id'])['status'] == 'closing'
    fake = types.SimpleNamespace(download_json=lambda svc, fid: copy.deepcopy(drive_copy),
                                 upload_json=lambda *a, **k: 'fake')
    monkeypatch.setitem(sys.modules, 'bcs.drive_store', fake)
    for s in (store, ZebraStore()):
        s._drive_enabled, s._drive_file_id, s._drive_service = True, 'fake', None
        s._sync_from_drive()
        t = s.find(r['id'])
        assert t['status'] == 'closing' and t['exit_chase']['working_tag']


def test_the_chase_tick_comes_from_the_record(store, neo):
    c = client_for(neo)
    r = live_record(store, neo, c)
    store.update_trade_fields(r['id'], neo_tick=0.10)
    e, _ = engine(store, client=c)
    st = e._new_chase(store.find(r['id']))
    assert st.tick == 0.10


# -- dead-monitor population and the health command ---------------------------------

def test_a_live_naked_record_mid_exit_still_counts_as_live(store, neo):
    """Review F5: zebra's dead-monitor check must see a `closing` live record."""
    from common import arming
    from zebra import monitor
    c = client_for(neo)
    r = live_record(store, neo, c)
    store.begin_close(r['id'], 'naked:stop')
    assert arming.LIVE_RECORD in monitor._cohort_population(store.load_trades())


def test_naked_check_names_open_chases_and_blocked_entries(store, neo):
    from datetime import date
    from zebra import naked_entry as ne
    c = client_for(neo)
    clk = Clock()
    r, e, _ = _fire_tp(store, neo, c, clk)
    p = parent(store, 'OTHER')
    store.update_trade_fields(p['id'], **{ne.TWIN_FIELD: {'state': 'unclear', 'tag': 'zbr-q',
                                                          'at': '2026-12-02 11:00:00'}})
    lines = '\n'.join(nx.health_lines(store.load_trades(), 'live', date(2026, 12, 2)))
    assert 'open naked: 1' in lines and 'closing | chase active' in lines
    assert 'NEW ENTRIES BLOCKED' in lines and 'naked-resolve %s' % p['id'] in lines
    assert 'live entry gate:' in lines and 'NEO session:' in lines


def test_a_live_naked_record_is_costed_at_neos_stt(store, neo, monkeypatch):
    """NEO's options contract note: STT 0.15% of the sell premium (2026-11-28)."""
    monkeypatch.setattr(cfg, 'NAKED_STT_SELL_PCT', 0.15)
    monkeypatch.setattr(cfg, 'NAKED_BROKERAGE_PER_ORDER', 0.0)
    c = client_for(neo)
    r = live_record(store, neo, c)
    neo.set_quote(SYM, 40.0, 40.5)
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    e, _ = engine(store, spot=1041.0, client=c)
    e.poll(None, now=T0)
    e.poll(None, now=T0 + timedelta(seconds=5))
    t = store.find(r['id'])
    assert t['status'] == 'exited'
    assert t['fees']['stt'] == pytest.approx(round(40.0 * LOT * 0.0015, 2), abs=0.01)


def test_a_rehearsal_blind_spell_is_logged_not_telegrammed(store, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_BLIND_ALERT_SEC', 300)
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'rehearsal')
    rehearsal_record(store)
    QUOTES[SYM] = _q(ask=40.5, bid=15.0, reliable=False)
    clk = Clock()
    e, sent = engine(store, spot=1010.0, clock=clk)
    for k in range(0, 80):
        clk.t += 5
        e.poll(None, now=T0 + timedelta(seconds=5 * k))
    assert not any('BLIND' in m for m in sent)


# -- trader re-review N1: a hand sale never leads to a second bot sell -------------

def _app_client(neo):
    """The owner's NEO app: an order zebra did not tag."""
    from test_naked_exit import SESS, Refresh
    return C.NeoClient('app', http=neo, current_fn=lambda: SESS, refresh_fn=Refresh(),
                       exclusive=False)


def test_a_hand_sale_while_the_chase_runs_never_oversells(store, neo, monkeypatch):
    c = client_for(neo)
    clk = Clock()
    r, e, sent = _fire_tp(store, neo, c, clk, real_bid=30.0)      # bot sell rests at 40
    app = _app_client(neo)
    app.place(C.OrderRequest(SYM, TOK, 'S', LOT, '30.00', app.new_tag()))   # the owner sells
    # the positions API LAGS the fill: it still shows the lot held
    monkeypatch.setattr(c, 'net_held', lambda *a, **k: LOT)
    for k in range(1, 30):
        clk.t += 11
        e.poll(None, now=T0 + timedelta(seconds=11 * k))
    bot_sells = [o for o in neo.placed('S') if str(o.get('GuiOrdId', o.get('tag', ''))
                                                    ).startswith('zbr-')]
    assert neo.net(SYM) == 0                          # never short
    assert not any(o['ordSt'] == 'open' for o in bot_sells)
    t = store.find(r['id'])
    assert t['exit_chase']['remaining'] == 0 and t['status'] == 'closing'
    assert sum('HAND sale' in m for m in sent) == 1
    assert sum('Book it at the true average' in m for m in sent) == 1


def test_an_unreadable_book_sends_nothing(store, neo, monkeypatch):
    c = client_for(neo)
    clk = Clock()
    r, e, _ = _fire_tp(store, neo, c, clk)
    n = len(neo.placed('S'))
    monkeypatch.setattr(c, '_views_where', lambda *a, **k: (_ for _ in ()).throw(
        C.Unavailable('book down')))
    for k in range(1, 10):
        clk.t += 11
        e.poll(None, now=T0 + timedelta(seconds=11 * k))
    assert len(neo.placed('S')) == n


def test_a_vanished_working_sell_is_settled_from_the_account(store, neo, monkeypatch):
    """Architect re-review (c): no all-session loop on an order the book lost."""
    monkeypatch.setattr(cfg, 'NAKED_EXIT_ALERT_STEPS', 3)
    c = client_for(neo)
    clk = Clock()
    r, e, _ = _fire_tp(store, neo, c, clk, real_bid=30.0)
    for o in list(neo.orders):                        # the book loses the working sell
        if o['trnsTp'] == 'S':
            neo.orders.remove(o)
    neo.set_quote(SYM, 40.0, 40.5)
    for k in range(1, 12):
        clk.t += 5
        e.poll(None, now=T0 + timedelta(seconds=5 * k))
    t = store.find(r['id'])
    assert t['status'] == 'exited' and neo.net(SYM) == 0


def test_two_sessions_from_expiry_any_bid_sells(store, neo, monkeypatch):
    from zebra import naked_rules
    c = client_for(neo)
    clk = Clock()
    r, e, _ = _fire_tp(store, neo, c, clk, real_bid=5.0)
    monkeypatch.setattr(naked_rules, 'sessions_left', lambda exp, today: 2)
    assert e._floor(store.find(r['id']), T0) == 0.0


def test_zebra_close_sees_a_working_sell_in_neos_own_book(store, neo):
    """Off the Pi the record's chase state is stale; NEO's book is not."""
    c = client_for(neo)
    clk = Clock()
    r, e, _ = _fire_tp(store, neo, c, clk)
    t = dict(store.find(r['id']))
    t['exit_chase'] = {}                              # what a laptop copy shows
    assert 'NEO shows a sell WORKING' in nx.hand_close_problem(t, c)


def test_the_entry_limit_lands_on_neos_tick_grid(neo):
    from test_naked_neo import broker, ctx, SYM as NSYM, LOT as NLOT
    from kotak_neo import symbols as Y
    from datetime import date
    from zebra import naked_neo as nn
    b = broker(neo)
    b._scrip_map = Y.ScripMap([Y.ScripRow(NSYM, '144284', 'RELIANCE', date(2026, 12, 29),
                                          1540.0, 'CE', NLOT, 0.10, 'OPTSTK')])
    neo.set_quote(NSYM, 10.0, 10.2)
    b.buy(NSYM, NLOT, 10.25, ctx())
    assert float(neo.placed('B')[-1]['prc']) == 10.2
