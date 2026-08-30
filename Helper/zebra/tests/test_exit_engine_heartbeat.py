"""H4 — the exit engine can die silently, and the stand-down is one-sided.

`exits_managed_externally` is DECIDED ON only by `zebra/monitor.py`. Since
2026-08-29 the peer reads it too, but only to compute and announce
`common.arming`'s verdict — pinned below — so this process still hands the
cohort's stops away on the strength of its own config and nothing else. Two
roads lead from there to the same silent state:

1. the flag is on and the peer is NOT RUNNING. Nothing books. The log says
   "EXITS EXTERNAL ... measured, not acted on" on every cycle, the other
   alerts keep arriving, and nothing anywhere reports a fault;
2. the kill switch trips. It does not stop the peer, it forces it to DRY RUN
   for the session — alive, polling, alerting, and unable to place a single
   closing order. Alerts continuing is exactly what makes it look healthy.

So the heartbeat records two facts, never one: that the engine polled, and
whether it could BOOK. And "no fresh beat" is split into `missing` (never
started) and `stale` (started, then died), because those need opposite
responses and look identical from here.

Run:  cd Helper && python -m pytest zebra/tests/test_exit_engine_heartbeat.py -v
"""
import json
import sys
import time
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg            # noqa: E402
from zebra import monitor                  # noqa: E402
from zebra.trade_store import ZebraStore   # noqa: E402
from bcs import spread_monitor as sm       # noqa: E402

NOW = 1_756_000_000.0        # a fixed wall clock; nothing here reads the real one


@pytest.fixture
def logs(tmp_path, monkeypatch):
    """One directory both engines resolve their paths through."""
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(sm, 'LOG_DIR', tmp_path)
    return tmp_path


def beat(logs, *, age=0.0, dry_run=False, kill_switch=False,
         cohort_trades=8, cohort_store=True, state='polling', now=NOW):
    """Write a heartbeat as the peer would have written it `age` seconds ago."""
    sm.write_heartbeat(state, dry_run, open_trades=cohort_trades,
                       cohort_trades=cohort_trades, cohort_store=cohort_store,
                       kill_switch=kill_switch, now=now - age)
    return json.loads((logs / sm.HEARTBEAT_NAME).read_text())


def check(n=8, now=NOW, market_open=True):
    return monitor.alert_if_exit_engine_down(n, dry_run=False, now=now,
                                             market_open=market_open)


# ── the premise ─────────────────────────────────────────────────────────────

def test_the_peer_reads_the_stand_down_switch_ONLY_to_report_it():
    """The whole reason this file exists — restated, not weakened.

    The original assertion was that `bcs/spread_monitor.py` never mentions
    `exits_managed_externally` at all, with a note that a change here should be
    deliberate. It was, on 2026-08-29: the monitor now reads all four switches
    to compute `common.arming`'s verdict and announce it. That is the OPPOSITE
    of the coupling this test guards against — the one-sidedness that matters
    is that the peer never DECIDES on the switch, because a stand-down each
    engine resolves for itself is how a position ends up with no engine at all.

    So the property is now scoped rather than absolute: the switch may be read
    inside `_arming_preflight`, which places no order and changes no behaviour,
    and nowhere else.
    """
    import inspect
    src = (HELPER / 'bcs' / 'spread_monitor.py').read_text(encoding='utf-8')
    reporting = set(
        inspect.getsource(sm._arming_preflight).splitlines())
    # Prose mentions it (in backticks); reading it needs the key as a string
    # literal or the zebra constant by name.
    reads = [ln for ln in src.splitlines()
             if ("'exits_managed_externally'" in ln
                 or '"exits_managed_externally"' in ln
                 or 'EXITS_MANAGED_EXTERNALLY' in ln)
             and ln not in reporting]
    assert reads == [], (
        'the peer reads the stand-down switch outside the arming preflight, '
        'i.e. somewhere it could act on it: %r' % [ln.strip() for ln in reads])


def test_the_arming_preflight_places_no_order():
    """The scope above is only safe while the preflight stays inert. It reads
    four switches and speaks; anything else in there would be behaviour keyed
    on a switch this engine does not own."""
    import inspect
    src = inspect.getsource(sm._arming_preflight)
    for forbidden in ('place_limit_order', 'close_spread', 'begin_close',
                      'update_trade_exit', 'kite.'):
        assert forbidden not in src, forbidden


def test_both_engines_resolve_the_same_file():
    """One constant, two modules — the most repeated bug shape in this
    codebase. A reader looking at a filename the writer does not write is a
    heartbeat that is permanently `missing`, i.e. a permanent false alarm,
    i.e. an alert nobody reads."""
    assert monitor.HEARTBEAT_NAME == sm.HEARTBEAT_NAME
    assert sm.heartbeat_path() == sm.LOG_DIR / sm.HEARTBEAT_NAME
    # Unpatched roots: both derive logs/ from the same Helper/ directory.
    assert (cfg.PROJECT_ROOT / 'logs').resolve() == sm.LOG_DIR.resolve()


# ── missing vs stale vs dry-run vs healthy ──────────────────────────────────

def test_a_fresh_armed_beat_is_ok_and_says_nothing(logs, telegrams):
    beat(logs)
    assert monitor.read_exit_engine_heartbeat(now=NOW)['state'] == 'ok'
    assert check() is None
    assert telegrams == [], 'a healthy peer produced an alert'


def test_a_stale_beat_alerts(logs, telegrams):
    beat(logs, age=20 * 60)
    assert monitor.read_exit_engine_heartbeat(now=NOW)['state'] == 'stale'
    assert check() == 'stale'
    assert len(telegrams) == 1
    assert 'NO EXIT ENGINE' in telegrams[0]


def test_a_beat_just_inside_the_window_is_still_ok(logs, telegrams):
    """The boundary, so the threshold cannot drift to 'any beat ever'."""
    beat(logs, age=monitor.HEARTBEAT_STALE_SEC - 1)
    assert check() is None
    beat(logs, age=monitor.HEARTBEAT_STALE_SEC + 1)
    assert check() == 'stale'


def test_missing_and_stale_are_different_states(logs, telegrams):
    """Never started vs started-and-died. Same silence, opposite fixes."""
    assert monitor.read_exit_engine_heartbeat(now=NOW)['state'] == 'missing'
    assert check() == 'missing'
    missing_msg = telegrams[-1]

    beat(logs, age=20 * 60)
    assert check() == 'stale'
    stale_msg = telegrams[-1]

    assert missing_msg != stale_msg
    assert 'START IT' in missing_msg
    assert 'min ago' in stale_msg and 'START IT' not in stale_msg


def test_a_running_but_dry_run_peer_is_not_a_healthy_peer(logs, telegrams):
    """The kill-switch road. The process is alive and polling; every close is
    a no-op. A heartbeat that only proved liveness would certify exactly the
    state it exists to catch."""
    beat(logs, dry_run=True, kill_switch=True)
    hb = monitor.read_exit_engine_heartbeat(now=NOW)
    assert hb['state'] == 'dry_run'
    assert check() == 'dry_run'
    assert 'DRY RUN' in telegrams[-1]
    assert 'kill switch' in telegrams[-1]


def test_dry_run_names_the_crontab_when_the_switch_did_not_trip(logs, telegrams):
    beat(logs, dry_run=True, kill_switch=False)
    assert check() == 'dry_run'
    assert '--dry-run' in telegrams[-1]


def test_running_and_booking_is_distinguishable_from_running_and_dry(logs):
    """The single property the whole design turns on."""
    beat(logs, dry_run=False)
    armed = monitor.read_exit_engine_heartbeat(now=NOW)
    beat(logs, dry_run=True)
    dry = monitor.read_exit_engine_heartbeat(now=NOW)
    assert armed['state'] == 'ok' and dry['state'] == 'dry_run'
    assert armed['beat']['dry_run'] is False
    assert dry['beat']['dry_run'] is True


def test_a_peer_with_no_cohort_book_is_not_watching_anything(logs, telegrams):
    """Alive and armed, but its fourth book would not open. Its own alert for
    that can fail to send; this is the second SOURCE, not a second check."""
    beat(logs, cohort_store=False)
    assert monitor.read_exit_engine_heartbeat(now=NOW)['state'] \
        == 'no_cohort_book'
    assert check() == 'no_cohort_book'


def test_a_peer_that_loaded_none_of_them_is_reported(logs, telegrams):
    """`--list answered "Open: 0" with eight positions live` — the two engines
    disagreeing about what the book contains, with both processes healthy."""
    beat(logs, cohort_trades=0)
    assert check(n=8) == 'not_watching'
    assert '0 cohort position(s) loaded' in telegrams[-1]


def test_an_empty_book_on_both_sides_is_not_a_fault(logs, telegrams):
    """Nothing is stood down here, so nothing is unmanaged."""
    beat(logs, cohort_trades=0)
    assert check(n=0) is None
    assert telegrams == []


def test_an_unreadable_beat_is_not_evidence_of_health(logs, telegrams):
    (logs / sm.HEARTBEAT_NAME).write_text('{"ts": ')
    assert monitor.read_exit_engine_heartbeat(now=NOW)['state'] == 'unreadable'
    assert check() == 'unreadable'


def test_a_beat_without_a_timestamp_is_unreadable(logs):
    (logs / sm.HEARTBEAT_NAME).write_text(json.dumps({'state': 'polling'}))
    assert monitor.read_exit_engine_heartbeat(now=NOW)['state'] == 'unreadable'


# ── noise discipline ────────────────────────────────────────────────────────

def test_the_alert_does_not_repeat_every_poll(logs, telegrams):
    """A five-minute cron would otherwise send 75 identical messages a
    session, and an alert that repeats is one the reader learns to skip."""
    beat(logs, age=20 * 60)
    assert check(now=NOW) == 'stale'
    for i in range(1, 6):
        assert check(now=NOW + i * 300) is None
    assert len(telegrams) == 1


def test_it_re_arms_so_a_standing_fault_is_not_forgotten(logs, telegrams):
    beat(logs, age=20 * 60)
    check(now=NOW)
    assert check(now=NOW + monitor.HEARTBEAT_REPEAT_SEC + 1) == 'stale'
    assert len(telegrams) == 2


def test_a_change_of_state_alerts_immediately(logs, telegrams):
    """Dedup is per STATE. `dry_run` degrading into `stale` inside the repeat
    window is new information and must not be swallowed as a repeat."""
    beat(logs, dry_run=True)
    assert check(now=NOW) == 'dry_run'
    beat(logs, age=20 * 60, now=NOW + 60)
    assert check(now=NOW + 60) == 'stale'
    assert len(telegrams) == 2


def test_the_dedup_survives_the_process(logs, telegrams):
    """zebra's cron process exits between cycles, so an in-memory flag would
    re-alert every five minutes. Simulated by clearing nothing but memory:
    the state file is the only thing carrying the fact forward."""
    beat(logs, age=20 * 60)
    assert check(now=NOW) == 'stale'
    assert (logs / monitor.ALERT_STATE_NAME).exists()
    prev = json.loads((logs / monitor.ALERT_STATE_NAME).read_text())
    assert prev['state'] == 'stale'
    assert check(now=NOW + 300) is None


def test_recovery_is_announced_once(logs, telegrams):
    beat(logs, age=20 * 60)
    check(now=NOW)
    beat(logs, now=NOW + 600)
    assert check(now=NOW + 600) == 'recovered'
    assert 'BACK' in telegrams[-1]
    assert check(now=NOW + 900) is None
    assert len(telegrams) == 2


def test_nothing_is_telegrammed_outside_market_hours(logs, telegrams, caplog):
    """The peer exits at 15:30 BY DESIGN and its beat goes stale every single
    evening. Alerting then would cry wolf nightly, which is how a channel
    stops being read. The log line still happens — that is the forensic
    record, and this must hold at 02:00 on a Sunday."""
    import logging
    beat(logs, age=20 * 60)
    with caplog.at_level(logging.ERROR, logger='zebra.monitor'):
        assert check(market_open=False) is None
    assert telegrams == []
    assert any('EXIT ENGINE %s' % 'STALE' in r.getMessage()
               for r in caplog.records)


# ── the writer ──────────────────────────────────────────────────────────────

def test_a_beat_is_written_with_an_empty_book(logs):
    """'Quiet' must not read as 'dead'. A monitor that started and found
    nothing to watch is alive, and the cron line refreshes this every five
    minutes — well inside the staleness window."""
    sm.write_heartbeat('idle', False, open_trades=0, cohort_trades=0,
                       cohort_store=True, now=NOW)
    assert monitor.read_exit_engine_heartbeat(now=NOW)['state'] == 'ok'


def test_the_beat_carries_the_state_and_the_counts(logs):
    b = beat(logs, state='startup', cohort_trades=3)
    assert b['state'] == 'startup'
    assert b['cohort_trades'] == 3
    assert b['schema'] == sm.HEARTBEAT_SCHEMA
    assert b['pid'] > 0
    assert 'at' in b


def test_the_writer_never_raises_into_the_monitor_loop(logs, monkeypatch):
    """A failure to write the health file must not be able to stop the
    monitoring the health file describes."""
    monkeypatch.setattr(sm, 'LOG_DIR', logs / 'nope' / 'deeper')
    monkeypatch.setattr(sm, 'log', lambda m: None)

    def boom(*a, **k):
        raise OSError('disk full')
    monkeypatch.setattr(Path, 'mkdir', boom)
    assert sm.write_heartbeat('polling', False, now=NOW) is False


def test_a_partial_write_is_never_observed(logs):
    """Written tmp-then-replace: a reader on its own cron must not be able to
    catch a half-written file and conclude the peer is corrupt."""
    beat(logs)
    assert not list(logs.glob('*.tmp'))


def test_the_counts_come_from_the_loaded_book(logs):
    """`_beat_all` is the single derivation of 'how many cohort positions is
    this engine watching', so two beats can never disagree about one book."""
    trades = [{'_store_type': 'zebra'}, {'_store_type': 'zebra'},
              {'_store_type': 'bcs'}]
    sm._beat_all('polling', False, trades, object())
    b = json.loads((logs / sm.HEARTBEAT_NAME).read_text())
    assert b['open_trades'] == 3 and b['cohort_trades'] == 2
    assert b['cohort_store'] is True
    sm._beat_all('polling', False, trades, None)
    b = json.loads((logs / sm.HEARTBEAT_NAME).read_text())
    assert b['cohort_store'] is False


# ── wired into the branch that actually stands down ─────────────────────────

@pytest.fixture
def store(logs, monkeypatch):
    monkeypatch.setattr(cfg, 'LOCAL_FILE', logs / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', logs / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    s = ZebraStore()
    for name in ('TESTCO', 'OTHERCO'):
        sid = s.add_signal({'stock': name, 'timeframe': 'weekly',
                            'direction': 'CE', 'st_value': 100.0,
                            'st_direction': 'UP', 'signal_price': 96.0,
                            'signal_gap_pct': 4.0})['id']
        s.mark_entered(sid, {'long_strike': 100.0, 'short_strike': 140.0,
                             'long_symbol': 'L', 'short_symbol': 'S',
                             'debit': 10.0, 'lot_size': 100, 'lots': 1,
                             'expiry': '2026-09-30', 'structure': 'bcs'})
        with s._mutate():
            # cohort AND placed. `_exits_external` gained a third condition on
            # 2026-08-27 (C5) -- a PAPER record is withheld from the order path
            # entirely, so there is no peer to stand down for and no heartbeat
            # to check. These two stand in for positions the bridge really
            # holds.
            s.find(sid).update({'cohort': cfg.COHORT_START, 'paper': False})
    return s


def _cycle(store, monkeypatch, market_open=True):
    monkeypatch.setattr(monitor, 'get_ltp',
                        lambda kite, stocks: {s: 150.0 for s in stocks})
    monkeypatch.setattr(monitor, '_structure_quote',
                        lambda kite, t, spot=None: {
                            'mid': 30.0, 'reliable': True, 'reason': None,
                            'legs': {'long': {'symbol': 'L', 'bid': 40.0,
                                              'ask': 40.2},
                                     'short': {'symbol': 'S', 'bid': 10.0,
                                               'ask': 10.2}},
                            'floored': False})
    # Pinned, never read off the wall clock: this suite must pass at 02:00 on
    # a Sunday (`feedback_pin_the_wall_clock_in_tests`).
    monkeypatch.setattr(monitor, '_is_market_open', lambda: market_open)
    monitor.check_entered(store, kite=None, dry_run=False)


def test_the_branch_that_stands_down_is_the_branch_that_checks(
        store, logs, monkeypatch, telegrams):
    monkeypatch.setattr(cfg, 'EXITS_MANAGED_EXTERNALLY', True)
    _cycle(store, monkeypatch)
    assert any('NO EXIT ENGINE' in m for m in telegrams), (
        'zebra stood down from live positions without checking that anything '
        'stood up')


def test_one_alert_for_the_whole_book_not_one_per_position(
        store, logs, monkeypatch, telegrams):
    """A fault common to the entire book must not produce one Telegram per
    row — that is the same 'trained to ignore the marker' failure as the
    per-leg bid-ask warning that fired on 68% of shadows."""
    monkeypatch.setattr(cfg, 'EXITS_MANAGED_EXTERNALLY', True)
    assert len(store.get_entered()) == 2
    _cycle(store, monkeypatch)
    assert len([m for m in telegrams if 'NO EXIT ENGINE' in m]) == 1


def test_a_healthy_peer_produces_no_alert_from_the_branch(
        store, logs, monkeypatch, telegrams):
    """Negative control: without it the test above passes just as well when
    the alert has become unconditional."""
    monkeypatch.setattr(cfg, 'EXITS_MANAGED_EXTERNALLY', True)
    beat(logs, now=time.time())
    _cycle(store, monkeypatch)
    assert not any('NO EXIT ENGINE' in m for m in telegrams)


def test_nothing_is_checked_for_PAPER_records_with_the_switch_off(
        store, logs, monkeypatch, telegrams):
    """With the switch off and PAPER records, zebra books the exits itself.

    There is no peer to be absent and an alert here would be pure noise. This
    is the case the old `test_nothing_is_checked_when_nothing_stood_down`
    meant, but it asserted over the fixture's LIVE records -- see the next
    test for why that was pinning a defect.
    """
    monkeypatch.setattr(cfg, 'EXITS_MANAGED_EXTERNALLY', False)
    with store._mutate():
        for t in store._trades:
            if t.get('status') == 'entered':
                t['paper'] = True
    _cycle(store, monkeypatch)
    assert not any('NO EXIT ENGINE' in m for m in telegrams)


def test_a_LIVE_record_is_checked_even_with_the_switch_off(
        store, logs, monkeypatch, telegrams):
    """THE DEFECT (found 2026-08-31). The old test asserted SILENCE here.

    Its premise -- "with the switch off, zebra books the exits itself" -- is
    false for a record with real legs: `_paper_auto_close` declines those
    regardless of `exits_managed_externally`, because zebra books at the
    structure mid. So the monitor is the ONLY possible engine, and until now
    nothing checked it was there: `alert_if_exit_engine_down` fired solely
    from the stand-down branch.

    That left the arming order's own first live step unguarded. Its step is a
    hand-placed live trade filed while `exits_managed_externally` is still
    false; if the monitor is crash-looping on a dead Kite token it exits in
    `load_kite` BEFORE writing a first beat, so there is no heartbeat at all.
    Every log read OK while nothing held the stops.
    """
    monkeypatch.setattr(cfg, 'EXITS_MANAGED_EXTERNALLY', False)
    # The fixture's records are already `paper: False`, i.e. real legs.
    _cycle(store, monkeypatch)
    msgs = [m for m in telegrams if 'NO EXIT ENGINE' in m]
    assert msgs, 'a live cohort record with no peer engine must alert'
    assert 'CANNOT book' in msgs[0], (
        'the alert must say WHY this engine is not booking — it has not '
        'stood down here, it is unable'
    )
    assert 'exits_managed_externally=true' not in msgs[0], (
        'that reason is false in this state and would send the operator to '
        'the wrong switch'
    )


def test_the_live_record_alert_is_sent_once_per_cycle_not_once_per_row(
        store, logs, monkeypatch, telegrams):
    """Two live positions, one fault, one Telegram."""
    monkeypatch.setattr(cfg, 'EXITS_MANAGED_EXTERNALLY', False)
    _cycle(store, monkeypatch)
    assert len([m for m in telegrams if 'NO EXIT ENGINE' in m]) == 1


def test_a_broken_heartbeat_check_cannot_stop_exit_monitoring(
        store, logs, monkeypatch, telegrams):
    """The check is a health probe bolted onto the exit path. It must never be
    able to take that path down — the failure it reports is less dangerous
    than the failure it would cause."""
    monkeypatch.setattr(cfg, 'EXITS_MANAGED_EXTERNALLY', True)

    def boom(*a, **k):
        raise RuntimeError('probe exploded')
    monkeypatch.setattr(monitor, 'alert_if_exit_engine_down', boom)
    _cycle(store, monkeypatch)          # must not raise
    assert all(t['status'] == 'entered' for t in store.get_entered())
