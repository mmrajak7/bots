"""The naked exit engine is REACHED by the real `monitor_all` loop (P4 wiring).

`zebra/tests/test_naked_exit.py` proves the engine; this proves the monitor
calls it -- every poll, with the live `dry_run`, that an open naked record
keeps the loop alive when the BCS books are empty, and that a live naked
record enters the arming population. Driven through `bcs.tests.replay`, i.e.
the production loop with only the boundary faked.

Run:  cd Helper && python -m pytest bcs/tests/test_naked_engine_wiring.py -v
"""
import sys
from datetime import date
from pathlib import Path

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                     # noqa: E402
from bcs.tests.replay import Tick, run_session           # noqa: E402
from common import arming                                # noqa: E402

DAY = date(2026, 9, 15)
LONG, SHORT = 'TESTCO26SEP1340CE', 'TESTCO26SEP1390CE'
QTY = 700
TRADE = {
    'id': 1, 'status': 'open', 'stock': 'TESTCO', 'version': 1,
    'long_symbol': LONG, 'short_symbol': SHORT, 'spot_symbol': 'NSE:TESTCO',
    'exchange': 'NFO', 'quantity': QTY, 'lot_size': QTY, 'lots': 1,
    'entry_long_price': 21.20, 'entry_short_price': 7.65, 'net_debit': 13.55,
    'spread_width': 50, 'target_spot': 1435.0, 'sl_spot': 1319.0,
    'sl_spread': 6.78, 'entry_spot': 1360.0, 'expiry': '2026-09-29',
}
POSITIONS = [{'tradingsymbol': SHORT, 'quantity': -QTY},
             {'tradingsymbol': LONG, 'quantity': QTY}]


def book(bid, ask, qty=1400):
    return {'bid': bid, 'bid_qty': qty, 'ask': ask, 'ask_qty': qty,
            'ltp': round((bid + ask) / 2, 2), 'prev_close': bid}


def calm(n=4):
    return [Tick('11:%02d:00' % i, 1400.0, book(40.0, 40.2), book(10.05, 10.3), 'calm')
            for i in range(n)]


class FakeEngine:
    def __init__(self, open_n=1, live=False):
        self.polls, self.dry = [], []
        self.open_n, self.live = open_n, live
        self.dry_run = None

    def poll(self, kite, now=None):
        self.polls.append(kite)
        self.dry.append(self.dry_run)
        return []

    def open_count(self):
        return self.open_n

    def has_active_chase(self):
        return False

    def live_population(self):
        return self.live


def _install(monkeypatch, eng):
    monkeypatch.setattr(sm, '_open_naked_engine', lambda dry_run, zebra_store=None: eng)


def test_the_loop_polls_the_engine_every_iteration(monkeypatch):
    eng = FakeEngine()
    _install(monkeypatch, eng)
    clock, kite, store, spy = run_session(monkeypatch, sm, TRADE, calm(), DAY, POSITIONS)
    assert len(eng.polls) >= 4 and all(k is kite for k in eng.polls)
    assert set(eng.dry) == {False}


def test_dry_run_reaches_the_engine(monkeypatch):
    eng = FakeEngine()
    _install(monkeypatch, eng)
    run_session(monkeypatch, sm, TRADE, calm(), DAY, POSITIONS, dry_run=True)
    assert eng.polls and set(eng.dry) == {True}


def test_an_open_naked_record_keeps_an_otherwise_empty_monitor_alive(monkeypatch):
    eng = FakeEngine(open_n=1)
    _install(monkeypatch, eng)
    closed = dict(TRADE, status='closed')            # nothing open in the BCS books
    run_session(monkeypatch, sm, closed, calm(), DAY, [])
    assert eng.polls, 'the monitor exited "nothing to monitor" with a naked position open'


def test_without_naked_records_the_empty_monitor_still_exits(monkeypatch):
    eng = FakeEngine(open_n=0)
    _install(monkeypatch, eng)
    closed = dict(TRADE, status='closed')
    run_session(monkeypatch, sm, closed, calm(), DAY, [])
    assert eng.polls == []


def test_a_live_naked_record_enters_the_arming_population(monkeypatch):
    eng = FakeEngine(live=True)
    _install(monkeypatch, eng)
    seen = []
    real = arming.check

    def spy_check(**kw):
        seen.append(set(kw.get('population') or ()))
        return real(**kw)
    monkeypatch.setattr(arming, 'check', spy_check)
    run_session(monkeypatch, sm, TRADE, calm(), DAY, POSITIONS)
    assert seen and arming.LIVE_RECORD in seen[0]


def test_the_heartbeat_says_whether_the_naked_engine_runs(monkeypatch, tmp_path):
    """Reads a file written by the code under test.

    RETIRES WHEN: never as a source guard -- it reads a DATA file this code writes, not source;
    it retires with that file format.
    """
    monkeypatch.setattr(sm, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(sm, '_NAKED_ENGINE', FakeEngine())
    sm.write_heartbeat('polling', False)
    import json
    beat = json.loads((tmp_path / sm.HEARTBEAT_NAME).read_text())
    assert beat['naked_engine'] is True
    monkeypatch.setattr(sm, '_NAKED_ENGINE', None)
    sm.write_heartbeat('polling', False)
    assert json.loads((tmp_path / sm.HEARTBEAT_NAME).read_text())['naked_engine'] is False
