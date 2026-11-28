"""Review fixes P7f (2026-11-28, second round: trader / architect / flow).

Each test pins one fix:

  * A1   a store save is one write of one-record-per-line JSON; the rollback
         copy is exact; the exit's close lock + trigger is ONE write; the
         engine does not re-parse the book after its own save
  * A2   `zebra reset` refuses while a live naked lot is open
  * A4   zebra's cycle alerts when a live naked lot has no polling engine
  * A5   a hand sale made before this exit began is not this exit's
  * E1   one signal never sends more than the chase rounds, rejects included
  * K8   zebra's NEO client waits seconds, not 75 s, for a dead session
  * N3   a SHORT account is alerted red, and never read as flat
  * N4   two sessions from expiry there is no floor, saved trigger bid or not
  * R2   refused exit sells re-alert every repeat interval

Run:  cd Helper && python -m pytest zebra/tests/test_naked_p7f.py -v
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2]))

from test_naked_entry import (_iso, store, parent, Sent, go, naked,   # noqa: E402,F401
                              NOW, QUOTES, _q, LOT)
from test_naked_exit import (SYM, TOK, T0, Clock, neo, engine,       # noqa: E402,F401
                             live_record, client_for)
from kotak_neo import client as C           # noqa: E402
from zebra import config as cfg             # noqa: E402
from zebra import naked_entry as ne         # noqa: E402
from zebra import naked_exit as nx          # noqa: E402
from zebra import naked_rules               # noqa: E402
from zebra import trade_store as ts         # noqa: E402


@pytest.fixture(autouse=True)
def _live(monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'live')


def _fire_tp(store, neo, c, clk, real_bid=30.0):
    r = live_record(store, neo, c)
    neo.set_quote(SYM, real_bid, 40.5)
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    e, sent = engine(store, spot=1041.0, client=c, clock=clk)
    e.poll(None, now=T0)
    assert store.find(r['id'])['status'] == 'closing'
    return r, e, sent


# -- A1: the store's write path --------------------------------------------------

def test_the_book_is_valid_json_with_one_record_per_line(store):
    """Reads the book file the code under test writes.

    RETIRES WHEN: never as a source guard -- it reads a DATA file this code writes, not source;
    it retires with that file format.
    """
    p = parent(store)
    store.update_trade_fields(p['id'], note='x')
    text = cfg.LOCAL_FILE.read_text(encoding='utf-8')
    book = json.loads(text)
    assert [t['id'] for t in book] == [t['id'] for t in store.load_trades()]
    lines = text.strip().splitlines()
    assert lines[0] == '[' and lines[-1] == ']' and len(lines) == len(book) + 2
    assert ts._dump_book([]) == '[]\n' and json.loads(ts._dump_book([])) == []


def test_the_rollback_copy_is_exact_and_detached():
    book = [{'id': 1, 'a': {'b': [1, 2.5, None, 'x']}, 'when': datetime(2026, 12, 1, 9, 15)}]
    snap = ts._snapshot(book)
    assert snap == book and snap is not book and snap[0]['a'] is not book[0]['a']
    book[0]['a']['b'].append(3)
    assert snap != book                             # the change test still sees it


def test_begin_close_writes_the_trigger_fields_in_the_same_write(store):
    p = parent(store)
    v = store.find(p['id'])['version']
    assert store.begin_close(p['id'], 'x', fields={'exit_touch': 40.0}) is True
    t = store.find(p['id'])
    assert t['status'] == 'closing' and t['exit_touch'] == 40.0 and t['version'] == v + 1
    with pytest.raises(ValueError):
        store.begin_close(p['id'], 'x', fields={'status': 'exited'})


def test_the_engine_does_not_reparse_the_book_after_its_own_save(store, monkeypatch):
    p = parent(store)
    e, _ = engine(store)
    e.records()
    calls = []
    monkeypatch.setattr(store, 'reload', lambda: calls.append(1))
    store.update_trade_fields(p['id'], note='own write')
    e.records()
    assert calls == []                              # our own save: the cache has it
    time.sleep(0.02)
    os.utime(cfg.LOCAL_FILE, None)                  # another process wrote it
    st = cfg.LOCAL_FILE.stat().st_mtime
    if st == store.saved_mtime:                     # coarse clock: force a different mtime
        os.utime(cfg.LOCAL_FILE, (st + 1, st + 1))
    e.records()
    assert calls == [1]


# -- A2: reset ----------------------------------------------------------------------

def test_reset_refuses_while_a_live_naked_lot_is_open(store, neo, monkeypatch):
    from zebra import __main__ as cli
    c = client_for(neo)
    r = live_record(store, neo, c)
    monkeypatch.setattr('zebra.trade_store.get_store', lambda *a, **k: store)
    with pytest.raises(SystemExit):
        cli.cmd_reset(type('A', (), {'confirm': True})())
    assert store.find(r['id'])['status'] == 'entered'


# -- A4: zebra-side liveness of the naked engine ------------------------------------

def test_a_live_lot_without_a_polling_engine_is_alerted_and_repeats(store, neo, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_EXIT_ALERT_REPEAT_SEC', 900)
    live_record(store, neo, client_for(neo))
    monkeypatch.setattr(ne, '_beat_problem', lambda m: 'the exit monitor is not polling')
    sent = Sent()
    at = datetime(2026, 12, 2, 11, 0)               # a Wednesday, in session
    clk = [1000.0]
    monkeypatch.setattr('time.time', lambda: clk[0])
    assert ne.engine_liveness_check(store, now=at, send=sent)
    ne.engine_liveness_check(store, now=at, send=sent)
    assert len(sent) == 1 and 'NOT be watched' in sent[0]
    clk[0] += 901
    ne.engine_liveness_check(store, now=at, send=sent)
    assert len(sent) == 2
    # outside the session the beat says 'closed' by design: silent
    assert ne.engine_liveness_check(store, now=datetime(2026, 12, 2, 16, 0), send=sent) == ''
    monkeypatch.setattr(ne, '_beat_problem', lambda m: '')
    assert ne.engine_liveness_check(store, now=at, send=sent) == '' and len(sent) == 2


def test_no_live_lot_means_no_liveness_alert(store, monkeypatch):
    rp = parent(store)                              # a paper parent, no naked record
    assert rp
    monkeypatch.setattr(ne, '_beat_problem', lambda m: 'dead')
    sent = Sent()
    assert ne.engine_liveness_check(store, now=datetime(2026, 12, 2, 11, 0), send=sent) == ''
    assert sent == []


# -- A5: hand-sale baseline -------------------------------------------------------

def test_a_hand_sale_the_bot_cannot_place_stops_it_and_says_check(store, neo, monkeypatch):
    """Re-review F2: there is NO baseline. Any non-bot sale of the contract
    today is counted -- the safe error: the bot stops and says the account
    still READS the lot, so a human checks, never a second sale into a short."""
    c = client_for(neo)
    r = live_record(store, neo, c)
    monkeypatch.setattr(c, 'foreign_filled', lambda sym, side: LOT)
    neo.set_quote(SYM, 40.0, 40.5)
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    clk = Clock()
    e, sent = engine(store, spot=1041.0, client=c, clock=clk)
    for k in range(6):
        clk.t += 5
        e.poll(None, now=T0 + timedelta(seconds=5 * k))
    t = store.find(r['id'])
    assert t['status'] == 'closing' and neo.net(SYM) == LOT   # the bot sold nothing
    msg = [m for m in sent if 'Book it at the true average' in m]
    assert len(msg) == 1 and 'CHECK THE NEO APP' in msg[0] and 'SELL IT' not in msg[0]


def _app_client(neo):
    from test_naked_exit import SESS, Refresh
    return C.NeoClient('app', http=neo, current_fn=lambda: SESS, refresh_fn=Refresh(),
                       exclusive=False)


def test_a_hand_sale_just_before_the_trigger_never_lets_the_bot_go_short(store, neo,
                                                                         monkeypatch):
    """Re-review F2 (reproduced): the owner sells THIS lot in the app just
    before the stop fires while the positions API still lags. The bot must
    not sell too."""
    c = client_for(neo)
    r = live_record(store, neo, c)
    app = _app_client(neo)
    neo.set_quote(SYM, 40.0, 40.5)
    app.place(C.OrderRequest(SYM, TOK, 'S', LOT, '40.00', app.new_tag()))
    assert neo.net(SYM) == 0
    monkeypatch.setattr(c, 'net_held', lambda *a, **k: LOT)  # positions LAG the hand fill
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    clk = Clock()
    e, sent = engine(store, spot=1041.0, client=c, clock=clk)
    for k in range(10):
        clk.t += 5
        e.poll(None, now=T0 + timedelta(seconds=5 * k))
    bot = [o for o in neo.placed('S') if str(o.get('GuiOrdId', '')).startswith('zbr-')]
    assert bot == [] and neo.net(SYM) == 0                     # never short
    assert store.find(r['id'])['status'] == 'closing'


# -- E1: one order budget for every path -------------------------------------------

def test_one_signal_never_sends_more_than_the_chase_rounds(store, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_MAX_ORDERS_PER_SIGNAL', 99)
    monkeypatch.setattr(cfg, 'NAKED_ENTRY_CHASE_ROUNDS', 3)
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'rehearsal')

    class AlwaysRejects:
        n = 0

        def buy(self, symbol, qty, limit, ctx):
            self.n += 1
            return ne.BuyResult('rejected', message='price band', retry=True)
    b = AlwaysRejects()
    out, _ = go(store, parent(store), broker=b)
    assert out.state in ('refused', 'skipped') and b.n == 3


# -- K8: the NEO session wait ---------------------------------------------------------

def test_zebras_neo_client_waits_seconds_for_a_dead_session(monkeypatch):
    from zebra import naked_neo
    seen = {}

    class FakeClientMod:
        @staticmethod
        def NeoClient(bot, **kw):
            seen.update(kw, bot=bot)
            return 'client'
    monkeypatch.setattr(naked_neo, 'neo', lambda: (FakeClientMod, None, None))
    monkeypatch.setattr(cfg, 'NAKED_NEO_REFRESH_WAIT_SEC', 2.0)
    assert naked_neo.default_client() == 'client'
    assert seen['bot'] == naked_neo.BOT and seen['refresh_wait_s'] == 2.0


# -- N3: a short account -------------------------------------------------------------

def test_a_short_account_is_alerted_red_and_never_called_flat(store, neo, monkeypatch):
    c = client_for(neo)
    clk = Clock()
    r, e, sent = _fire_tp(store, neo, c, clk)
    monkeypatch.setattr(c, 'net_held', lambda *a, **k: -LOT)
    for k in range(1, 6):
        clk.t += 11
        e.poll(None, now=T0 + timedelta(seconds=11 * k))
    short = [m for m in sent if 'ACCOUNT IS SHORT' in m]
    assert len(short) == 1 and 'BUY IT BACK' in short[0]
    assert not any('position is flat' in m for m in sent)


# -- N4: the floor two sessions from expiry --------------------------------------------

def test_no_floor_two_sessions_from_expiry_even_without_a_trigger_bid(store, monkeypatch):
    e, _ = engine(store)
    monkeypatch.setattr(naked_rules, 'sessions_left', lambda exp, today: 2)
    assert e._floor({'expiry': '2026-12-03'}, T0) == 0.0
    assert e._floor({'expiry': '2026-12-03', 'exit_touch': 40.0}, T0) == 0.0
    monkeypatch.setattr(naked_rules, 'sessions_left', lambda exp, today: 9)
    assert e._floor({'expiry': '2026-12-30'}, T0) is None            # reliable book only
    assert e._floor({'expiry': '2026-12-30', 'exit_touch': 40.0,
                     'exit_trigger': {'reason': 'stop'}}, T0) == pytest.approx(20.0)


# -- R2: refused sells re-alert ----------------------------------------------------------

def test_refused_exit_sells_realert_every_repeat_interval(store, neo, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_EXIT_ALERT_REPEAT_SEC', 300)
    c = client_for(neo)
    r = live_record(store, neo, c)
    for _ in range(400):
        neo.reject(1004, 'RMS: price out of range')
    neo.set_quote(SYM, 40.0, 40.5)
    QUOTES[SYM] = _q(ask=40.5, bid=40.0)
    clk = Clock()
    e, sent = engine(store, spot=1041.0, client=c, clock=clk)
    for k in range(120):                            # ~22 min of refusals
        clk.t += 11
        e.poll(None, now=T0 + timedelta(seconds=11 * k))
    refused = [m for m in sent if 'REFUSED in a row' in m]
    assert store.find(r['id'])['status'] == 'closing' and neo.net(SYM) == LOT
    assert len(refused) >= 3                        # at 3, then every 300 s -- never just once


# -- the 07:44 login: no test may reach the real NEO session -------------------------

def test_the_suite_cannot_reach_the_real_neo_session():
    """conftest rail: a REAL zebra NEO client built with no fakes fails loudly
    instead of reading -- and refreshing -- the live account's session."""
    from zebra import naked_neo
    c = naked_neo.default_client()
    with pytest.raises(RuntimeError, match='TEST RAIL'):
        c.order_book()


# -- re-review F1: a zero read after unrelated troubles is never terminal ----------------

def test_troubles_then_one_zero_read_never_settles_the_exit(store, neo, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_EXIT_ZERO_HELD_SEC', 600)
    c = client_for(neo)
    clk = Clock()
    r, e, sent = _fire_tp(store, neo, c, clk)
    e.end_of_session()                                 # no sell working
    reads = iter([C.Unavailable('down')] * 11 + [0] * 3)

    def held(*a, **k):
        v = next(reads, LOT)
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(c, 'net_held', held)
    for k in range(1, 20):
        clk.t += 11
        e.poll(None, now=T0 + timedelta(seconds=11 * k))
    t = store.find(r['id'])
    assert t['status'] in ('closing', 'exited') and not t.get('exit_needs_human')
    assert not any('READS 0' in m or 'position is flat' in m for m in sent)


# -- re-review F3: a short account at the step that completes is never booked ------------

def test_a_short_account_on_the_completing_step_is_not_booked(store, neo, monkeypatch):
    c = client_for(neo)
    clk = Clock()
    r, e, sent = _fire_tp(store, neo, c, clk, real_bid=40.0)  # the bot sell fills
    monkeypatch.setattr(c, 'net_held', lambda *a, **k: -LOT)  # ...and a hand sale too
    for k in range(1, 6):
        clk.t += 11
        e.poll(None, now=T0 + timedelta(seconds=11 * k))
    t = store.find(r['id'])
    assert t['status'] == 'closing' and t.get('exit_needs_human')
    assert any('SHORT' in m and 'BUY IT BACK' in m for m in sent)
    assert not any(m.startswith('\U0001F7E2') for m in sent)   # no green booking


# -- re-review F5: the token survives an UNCLEAR stamp ---------------------------------

def test_an_unclear_entry_keeps_the_neo_token_for_adoption(store, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'rehearsal')

    class Unclear:
        def buy(self, symbol, qty, limit, ctx):
            return ne.BuyResult('unclear', message='timeout', tag='zbr-1', neo_symbol=SYM,
                                neo_token=TOK, neo_tick=0.05)
    p = parent(store)
    out, _ = go(store, p, broker=Unclear())
    tw = store.find(p['id'])[ne.TWIN_FIELD]
    assert out.state == 'unclear' and tw['neo_token'] == TOK and tw['neo_tick'] == 0.05


# -- re-review F6: the late floor anchor needs a RELIABLE bid ------------------------------

def test_a_late_floor_anchor_ignores_a_stub_bid(store, neo):
    c = client_for(neo)
    r = live_record(store, neo, c)
    store.begin_close(r['id'], 'naked:stop')         # crash before the trigger was written
    QUOTES[SYM] = _q(ask=5.0, bid=0.05, reliable=False)
    e, _ = engine(store, spot=990.0, client=c)
    e.poll(None, now=T0)
    assert store.find(r['id']).get('exit_touch') in (None, 0, 0.0)
    QUOTES[SYM] = _q(ask=21.5, bid=21.0, reliable=True)
    e.poll(None, now=T0 + timedelta(seconds=5))
    assert store.find(r['id']).get('exit_touch') == 21.0


# -- re-review F7: a NaN never makes a no-op write ---------------------------------------

def test_a_nan_field_does_not_turn_every_no_op_into_a_rewrite():
    book = [{'id': 1, 'iv': float('nan')}]
    snap = ts._snapshot(book)
    assert not ts._changed(book, snap, ts._pickled(book))
    book[0]['iv'] = 0.2
    assert ts._changed(book, snap, ts._pickled(snap))


# -- re-review F11: hand-resolve without a NEO symbol needs --force ---------------------

def test_hand_resolve_without_a_neo_symbol_needs_force(store, monkeypatch):
    from zebra import naked_adopt as na
    p = parent(store)
    store.update_trade_fields(p['id'], **{ne.TWIN_FIELD: {
        'state': 'unclear', 'mode': 'live', 'at': NOW.strftime('%Y-%m-%d %H:%M:%S'),
        'qty': LOT, 'tag': 'zbr-x'}})
    with pytest.raises(ValueError, match='no NEO symbol'):
        na.resolve_by_hand(store, p['id'], filled=21.6)
    na.resolve_by_hand(store, p['id'], filled=21.6, force=True)
    assert naked(store)[0]['quantity'] == LOT
