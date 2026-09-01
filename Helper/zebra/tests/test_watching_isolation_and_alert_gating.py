"""Two zebra defects found 2026-08-31, both about a failure going quiet.

1. `check_watching` HAD NO PER-SIGNAL FAULT ISOLATION. `check_entered` was
   given exactly this guard, with the note that the earlier fix "guarded one
   CALL, this guards the CLASS" -- and the copy was never made here. The loop
   indexes directly (`trade['st_value']`, `trade['direction']`), divides by
   `st_value`, and calls a store that can raise `LockTimeout` or `ValueError`
   from `_must_find` when a sibling process removed the row. Any of those
   propagated to `run_cycle`'s PHASE-level catch, so every signal sorted after
   the bad one got no band check, no drift cancel and no entry -- and for a
   persistently bad row, never again.

2. THE ALL-CLEAR WAS NOT GATED ON THE ALARM. `_alert_store_corruption` wrote
   its dedup stamp whatever `_send_telegram` returned, and the dedup is keyed
   on the marker's own timestamp -- once per event EVER. So one network blip
   permanently lost the single most consequential message in the file: a
   quarantine means the book went empty and every open position stopped being
   monitored. `_alert_monitoring_blind` had the same shape with the marker
   written BEFORE the send.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_watching_isolation_and_alert_gating.py -v
"""
import json
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg                                  # noqa: E402
from zebra import monitor as mon                                 # noqa: E402


# ── 1. one poisoned signal must not silence the rest of the watchlist ───────

class _WatchStore:
    """Enough store for `check_watching`; the second row raises on update."""

    def __init__(self, rows, poison_id=None):
        self.rows = rows
        self.poison_id = poison_id
        self.gaps = []

    def get_watching(self):
        return list(self.rows)

    def get_triggered(self):
        return []

    def update_gap(self, trade_id, gap_pct):
        if trade_id == self.poison_id:
            raise RuntimeError('sibling process removed this row')
        self.gaps.append(trade_id)

    def find(self, tid):
        return next((r for r in self.rows if r['id'] == tid), None)


def _row(tid, stock, st_value=100.0, direction='CE'):
    return {'id': tid, 'stock': stock, 'st_value': st_value,
            'direction': direction, 'status': 'watching',
            'timeframe': 'monthly', 'added_at': '2026-08-30T10:00:00'}


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(mon, '_send_telegram', lambda *a, **k: True)


def test_a_poisoned_signal_does_not_stop_the_ones_after_it(monkeypatch):
    """THE DEFECT. Pre-fix the RuntimeError left the loop and every later
    signal went unchecked -- for a persistent bad row, forever."""
    rows = [_row(1, 'AAA'), _row(2, 'BBB'), _row(3, 'CCC')]
    store = _WatchStore(rows, poison_id=2)
    monkeypatch.setattr(mon, 'get_ltp',
                        lambda k, s: {x: 97.0 for x in s})

    mon.check_watching(store, kite=None, dry_run=True)

    assert 3 in store.gaps, (
        'the signal sorted after the poisoned one was never checked')
    assert 2 not in store.gaps


def test_a_zero_st_value_does_not_take_the_cycle_down(monkeypatch):
    """A half-merged or hand-edited row: `(price - 0) / 0`."""
    rows = [_row(1, 'AAA', st_value=0.0), _row(2, 'BBB')]
    store = _WatchStore(rows)
    monkeypatch.setattr(mon, 'get_ltp', lambda k, s: {x: 97.0 for x in s})

    mon.check_watching(store, kite=None, dry_run=True)

    assert 2 in store.gaps, 'a zero st_value stopped the whole watchlist'


def test_the_failure_is_LOGGED_not_swallowed(monkeypatch, caplog):
    """A guard that hides the fault is how a persistent bad row survives."""
    import logging
    caplog.set_level(logging.ERROR)
    store = _WatchStore([_row(1, 'AAA')], poison_id=1)
    monkeypatch.setattr(mon, 'get_ltp', lambda k, s: {x: 97.0 for x in s})

    mon.check_watching(store, kite=None, dry_run=True)

    assert any('WATCHING' in r.getMessage() for r in caplog.records), (
        'the skipped signal was not reported at all')


# ── 2. the all-clear is gated on the alarm having been SENT ────────────────

@pytest.fixture
def marker(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    (tmp_path / 'zebra_store_corrupt.json').write_text(json.dumps({
        'at': '2026-08-31T09:30:58', 'kind': 'quarantine',
        'error': 'the book failed to parse', 'backup': 'zebra.corrupt.1.json'}))
    return tmp_path


def test_a_failed_corruption_alert_is_retried_next_cycle(marker, monkeypatch):
    """THE DEFECT. One blip permanently disarmed the book-went-empty alert."""
    monkeypatch.setattr(mon, '_send_telegram', lambda *a, **k: False)
    assert mon._alert_store_corruption() is False
    assert not (marker / 'zebra_store_corrupt.alerted').exists(), (
        'a failed send was recorded as delivered; this event can never alert '
        'again')

    sent = []
    monkeypatch.setattr(mon, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)
    assert mon._alert_store_corruption() is True
    assert sent, 'the retry did not send'


def test_a_delivered_alert_is_still_deduped(marker, monkeypatch):
    """The negative control: gating on success must not make it re-alert
    every five minutes for the rest of the event's life."""
    sent = []
    monkeypatch.setattr(mon, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)
    assert mon._alert_store_corruption() is True
    assert mon._alert_store_corruption() is False
    assert len(sent) == 1


def test_a_dry_run_still_counts_as_delivered(marker, monkeypatch):
    """`_send_telegram` returns True for a dry run and for a config-muted
    channel. Neither is a failure, and treating them as one would make the
    alert repeat every cycle on a muted box."""
    monkeypatch.setattr(mon, '_send_telegram', lambda *a, **k: True)
    assert mon._alert_store_corruption(dry_run=True) is True
    assert (marker / 'zebra_store_corrupt.alerted').exists()


def test_the_blind_alert_marks_itself_only_after_sending():
    """Same shape, same file: the marker was written BEFORE the send.

    RETIRES WHEN: every alert in this module goes through one send-then-mark
    helper, so the ordering cannot be got wrong per call site.
    """
    import inspect
    src = inspect.getsource(mon._alert_monitoring_blind)
    send_at = src.index('_send_telegram(')
    mark_at = src.index("marker.write_text")
    assert send_at < mark_at, (
        'the blind-alert marker is written before the send again — a failed '
        'send burns the whole day for that cause')
