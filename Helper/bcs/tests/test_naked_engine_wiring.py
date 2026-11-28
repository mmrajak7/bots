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


# ── review fixes P7e (2026-11-28) ─────────────────────────────────────────────

def _beat_file():
    import json
    return json.loads((sm.LOG_DIR / sm.HEARTBEAT_NAME).read_text())


def test_every_way_out_releases_the_naked_engine_in_the_beat(monkeypatch):
    """Review A4/F3. RETIRES WHEN: never as a source guard -- it reads the
    heartbeat DATA file the monitor writes; it retires with that format."""
    eng = FakeEngine()
    _install(monkeypatch, eng)
    run_session(monkeypatch, sm, TRADE, calm(), DAY, POSITIONS)
    beat = _beat_file()
    assert beat['naked_engine'] is False and beat.get('naked_engine_released')
    assert sm._NAKED_ENGINE is None


def test_a_crash_releases_the_beat_and_is_said_out_loud(monkeypatch):
    """RETIRES WHEN: never as a source guard -- reads the heartbeat DATA file."""
    sent = []
    monkeypatch.setattr(sm, 'send_telegram', lambda m, **k: sent.append(m))

    def boom(kite, dry_run):
        sm._NAKED_ENGINE = FakeEngine()
        sm.write_heartbeat('polling', False)
        raise RuntimeError('disk on fire')
    import pytest
    with pytest.raises(RuntimeError):
        sm._releases_naked_engine(boom)(None, False)
    assert sm.monitor_all.__wrapped__                 # the real loop carries the guarantee
    assert _beat_file()['naked_engine'] is False
    assert any('CRASHED' in m for m in sent)


def test_sigterm_becomes_systemexit_so_the_release_runs():
    import pytest
    import signal
    with pytest.raises(SystemExit):
        sm._sigterm_to_exit(signal.SIGTERM, None)


def test_an_armed_naked_mode_keeps_an_empty_monitor_alive(monkeypatch):
    """Review A9/F7: with nothing open but `naked_live` armed, the monitor stays
    up so the next live entry finds an exit engine."""
    from zebra import config as zcfg
    monkeypatch.setattr(zcfg, 'NAKED_LIVE', 'rehearsal')
    eng = FakeEngine(open_n=0)
    _install(monkeypatch, eng)
    closed = dict(TRADE, status='closed')
    run_session(monkeypatch, sm, closed, calm(), DAY, [])
    assert eng.polls


def test_the_naked_poll_reads_the_kill_switch_itself(monkeypatch):
    eng = FakeEngine()
    _install(monkeypatch, eng)
    monkeypatch.setattr(sm, 'trading_enabled', lambda: False)
    run_session(monkeypatch, sm, TRADE, calm(), DAY, POSITIONS)
    assert eng.dry and eng.dry[0] is True          # the FIRST poll already refuses to sell


def test_an_engine_that_failed_to_build_cannot_hide_live_records(monkeypatch):
    _install(monkeypatch, None)
    monkeypatch.setattr(sm, '_live_naked_open_locally', lambda: True)
    seen = []
    real = arming.check

    def spy_check(**kw):
        seen.append(set(kw.get('population') or ()))
        return real(**kw)
    monkeypatch.setattr(arming, 'check', spy_check)
    run_session(monkeypatch, sm, TRADE, calm(), DAY, POSITIONS)
    assert seen and arming.LIVE_RECORD in seen[0]


# ── review fixes P7f (2026-11-28, second round) ───────────────────────────────

def test_a_live_record_filed_mid_session_gets_an_engine(monkeypatch):
    """Review A3: the monitor started with no engine (nothing to own); a live
    naked record then appears (adoption / naked-resolve). It must be watched
    this session, not tomorrow."""
    eng = FakeEngine()
    calls = []

    def opener(dry_run, zebra_store=None):
        calls.append(1)
        return None if len(calls) == 1 else eng
    monkeypatch.setattr(sm, '_open_naked_engine', opener)
    monkeypatch.setattr(sm, '_live_naked_open_locally', lambda: True)
    monkeypatch.setattr(sm, 'NAKED_ENGINE_RECHECK_SEC', 0)
    run_session(monkeypatch, sm, TRADE, calm(), DAY, POSITIONS)
    assert len(calls) >= 2 and eng.polls


def test_a_second_monitor_runs_without_the_engine_and_writes_no_beat(monkeypatch, tmp_path):
    """Review A6: only one process on the box may hold the naked engine; a
    second monitor must not cancel the first one's sell or flip its beat."""
    from common import filelock
    from zebra import config as zcfg
    monkeypatch.setattr(sm, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(zcfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(zcfg, 'NAKED_LIVE', 'live')
    monkeypatch.setattr(sm, '_BEAT_SUPPRESSED', False)
    monkeypatch.setattr(sm, '_NAKED_LOCK_ALERTED', False)
    sent = []
    monkeypatch.setattr(sm, 'send_telegram', lambda m, **k: sent.append(m))
    monkeypatch.setattr(filelock, 'try_hold', lambda path: False)     # the FIRST monitor has it
    assert sm._open_naked_engine(False, None) is None
    assert sm._BEAT_SUPPRESSED is True and len(sent) == 1
    assert sm.write_heartbeat('polling', True) is False
    assert not (tmp_path / sm.HEARTBEAT_NAME).exists()
    sm._open_naked_engine(False, None)
    assert len(sent) == 1                                              # said once per process


def test_the_engine_lock_is_exclusive_across_processes_and_idempotent_within(tmp_path):
    import os
    from common import filelock
    p = tmp_path / 'naked_exit_engine.lock'
    assert filelock.try_hold(p) and filelock.try_hold(p)
    fd = os.open(str(p), os.O_RDWR)                   # another holder
    try:
        assert not filelock._try_acquire(fd)
    finally:
        os.close(fd)
    filelock.release_held(p)
    fd = os.open(str(p), os.O_RDWR)
    try:
        assert filelock._try_acquire(fd)
        filelock._release(fd)
    finally:
        os.close(fd)


def test_an_engine_without_the_neo_package_does_not_claim_to_be_armed(monkeypatch, tmp_path):
    """Review A3: the beat's `naked_engine` is what the live entry gate trusts.

    RETIRES WHEN: never as a source guard -- it reads a DATA file this code writes, not source;
    it retires with that file format.
    """
    import json
    monkeypatch.setattr(sm, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(sm, '_BEAT_SUPPRESSED', False)
    eng = FakeEngine()
    eng.neo_error = 'No module named kotak_neo'
    monkeypatch.setattr(sm, '_NAKED_ENGINE', eng)
    sm.write_heartbeat('polling', False)
    assert json.loads((tmp_path / sm.HEARTBEAT_NAME).read_text())['naked_engine'] is False


def test_a_monitor_that_lost_the_lock_takes_the_engine_when_it_frees(monkeypatch, tmp_path):
    """Re-review F4: a hand-run monitor started first holds the lock; the cron
    monitor must take the engine (and resume its beat) once that one exits."""
    from common import filelock
    from zebra import config as zcfg
    monkeypatch.setattr(sm, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(zcfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(zcfg, 'NAKED_LIVE', 'live')
    monkeypatch.setattr(sm, '_BEAT_SUPPRESSED', False)
    monkeypatch.setattr(sm, '_NAKED_LOCK_ALERTED', False)
    monkeypatch.setattr(sm, 'send_telegram', lambda m, **k: None)
    from zebra import naked_exit
    monkeypatch.setattr(naked_exit.NakedExitEngine, 'open_count', lambda self: 0)  # no book
    free = [False]
    monkeypatch.setattr(filelock, 'try_hold', lambda path: free[0])
    monkeypatch.setattr(sm, '_live_naked_open_locally', lambda: False)
    assert sm._open_naked_engine(False, None) is None and sm._BEAT_SUPPRESSED
    free[0] = True                                    # the hand-run monitor exited
    eng = sm._open_naked_engine(False, None)
    assert eng is not None and sm._BEAT_SUPPRESSED is False
    filelock.release_held(sm._naked_engine_lock_path())
