"""Claude vetting state machine.

The happy path is the easy part. These tests are mostly about the ways the
vetting layer can FAIL, because every one of them must degrade to "the bot
trades exactly as it did before this layer existed" — never to a stalled
signal, never to a double entry, and never to a silently inflated precision
score.

Run:  cd Helper && python -m pytest zebra/tests/test_vet.py -v
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg          # noqa: E402
from zebra import vet                    # noqa: E402
from zebra.trade_store import ZebraStore  # noqa: E402

SIGNAL = {
    'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
    'st_value': 100.0, 'st_direction': 'UP',
    'signal_price': 96.0, 'signal_gap_pct': 4.0,
}
CONTEXT = {'stock': 'TESTCO', 'debit': 14.0, 'debit_to_width_pct': 35.0}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    s = ZebraStore(config={})
    s._load_local()
    s.add_signal(dict(SIGNAL))
    return s


# ── request ──────────────────────────────────────────────────────────────
def test_request_marks_pending_with_a_deadline(store):
    t = vet.request_entry_vet(store, 1, CONTEXT, spawn=False)
    assert vet.is_pending(t)
    assert t['vet']['context'] == CONTEXT      # what the bot saw, no re-quote
    assert vet.is_expired(t) is False


def test_request_refuses_to_revet_a_settled_signal(store):
    vet.request_entry_vet(store, 1, CONTEXT, spawn=False)
    vet.record_verdict(store, 1, vet.ALLOWED)
    with pytest.raises(ValueError, match='already vetted'):
        vet.request_entry_vet(store, 1, CONTEXT, spawn=False)


def test_spawn_failure_does_not_raise(store, monkeypatch):
    """A missing or broken CLI must never stop the bot trading — the deadline
    will fail the signal open instead."""
    monkeypatch.setattr(cfg, 'VET_CLI', '/nonexistent/claude-binary')
    t = vet.request_entry_vet(store, 1, CONTEXT, spawn=True)
    assert vet.is_pending(t)                   # still pending, still trades later


# ── verdict ──────────────────────────────────────────────────────────────
def test_verdict_applies_once(store):
    vet.request_entry_vet(store, 1, CONTEXT, spawn=False)
    assert vet.record_verdict(store, 1, vet.VETOED, decision_id=7) == 'applied'
    t = store.find(1)
    assert t['vet']['state'] == vet.VETOED and t['vet']['decision_id'] == 7


def test_duplicate_verdict_is_discarded(store):
    """A retried CLI call or an overlapping cron must not re-open a settled
    decision."""
    vet.request_entry_vet(store, 1, CONTEXT, spawn=False)
    vet.record_verdict(store, 1, vet.ALLOWED)
    assert 'discarded' in vet.record_verdict(store, 1, vet.VETOED)
    assert store.find(1)['vet']['state'] == vet.ALLOWED      # unchanged


def test_verdict_without_a_pending_request_is_discarded(store):
    assert 'discarded' in vet.record_verdict(store, 1, vet.ALLOWED)


def test_verdict_arriving_after_timeout_is_discarded(store):
    """THE double-entry guard. Once the deadline failed the signal open it is
    already trading unvetted; applying a late verdict would enter it twice."""
    vet.request_entry_vet(store, 1, CONTEXT, spawn=False)
    vet.expire_stale(store, now=datetime.now() + timedelta(hours=2))
    assert 'discarded' in vet.record_verdict(store, 1, vet.VETOED)
    assert store.find(1)['vet']['state'] == vet.UNAVAILABLE


def test_rejects_an_invalid_verdict(store):
    vet.request_entry_vet(store, 1, CONTEXT, spawn=False)
    with pytest.raises(ValueError, match='verdict must be'):
        vet.record_verdict(store, 1, 'maybe')


# ── fail-open ────────────────────────────────────────────────────────────
def test_expiry_fails_open_not_closed(store):
    """A vetting outage must degrade to today's behaviour, not a trading halt."""
    vet.request_entry_vet(store, 1, CONTEXT, spawn=False)
    assert vet.expire_stale(store, now=datetime.now() + timedelta(hours=2)) == [1]
    assert store.find(1)['vet']['state'] == vet.UNAVAILABLE


def test_expiry_leaves_fresh_requests_alone(store):
    vet.request_entry_vet(store, 1, CONTEXT, spawn=False)
    assert vet.expire_stale(store, now=datetime.now()) == []
    assert vet.is_pending(store.find(1))


def test_expiry_does_not_touch_settled_signals(store):
    vet.request_entry_vet(store, 1, CONTEXT, spawn=False)
    vet.record_verdict(store, 1, vet.ALLOWED)
    assert vet.expire_stale(store, now=datetime.now() + timedelta(days=1)) == []
    assert store.find(1)['vet']['state'] == vet.ALLOWED


def test_malformed_marker_is_treated_as_expired(store):
    """Fail OPEN on a corrupt marker. Treating it as fresh would strand the
    signal pending forever — an 'awaiting approval' state that never resolves
    is how this layer would silently stop the book."""
    with store._mutate():
        store.find(1)['vet'] = {'state': vet.PENDING, 'deadline': 'not-a-date'}
    assert vet.is_expired(store.find(1)) is True
    assert vet.expire_stale(store) == [1]


def test_deadline_is_absolute_so_a_reboot_cannot_strand_a_signal(store):
    """Stored as an absolute timestamp, not elapsed time: a Pi reboot mid-vet
    must still expire on schedule."""
    vet.request_entry_vet(store, 1, CONTEXT, spawn=False)
    deadline = datetime.fromisoformat(store.find(1)['vet']['deadline'])
    reborn = ZebraStore(config={})           # fresh process, empty memory
    reborn._load_local()
    assert vet.is_expired(reborn.find(1), now=deadline + timedelta(seconds=1))


def test_unvetted_signal_is_distinguishable_from_an_allowed_one(store):
    """`unavailable` must never read as `allowed` — that is what keeps an
    outage out of the layer's measured precision."""
    vet.request_entry_vet(store, 1, CONTEXT, spawn=False)
    vet.expire_stale(store, now=datetime.now() + timedelta(hours=2))
    assert store.find(1)['vet']['state'] != vet.ALLOWED


# ── wiring: the sweep must be reached by the entrypoint that RUNS ────────
# Twice before, a subsystem in this fleet looked deployed and executed zero
# times. The fail-open sweep is worthless if nothing calls it.

def test_run_cycle_invokes_the_failopen_sweep(store, monkeypatch):
    from zebra import monitor
    called = []
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    monkeypatch.setattr(monitor.vet_mod, 'expire_stale',
                        lambda s, **kw: called.append(s) or [])
    monkeypatch.setattr(monitor, 'check_watching', lambda *a, **k: None)
    monkeypatch.setattr(monitor, 'check_entered', lambda *a, **k: None)
    monitor.run_cycle(store, kite=None, do_scan=False)
    assert called, "expire_stale was NOT called by run_cycle"


def test_run_cycle_skips_the_sweep_when_the_layer_is_off(store, monkeypatch):
    from zebra import monitor
    called = []
    monkeypatch.setattr(cfg, 'VET_ENABLED', False)
    monkeypatch.setattr(monitor.vet_mod, 'expire_stale',
                        lambda s, **kw: called.append(s) or [])
    monkeypatch.setattr(monitor, 'check_watching', lambda *a, **k: None)
    monkeypatch.setattr(monitor, 'check_entered', lambda *a, **k: None)
    monitor.run_cycle(store, kite=None, do_scan=False)
    assert not called, "sweep ran with the layer disabled"


def test_sweep_failure_does_not_kill_the_cycle(store, monkeypatch):
    """A broken vet layer must not stop position monitoring."""
    from zebra import monitor
    ran = []
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    monkeypatch.setattr(monitor.vet_mod, 'expire_stale',
                        lambda s, **kw: (_ for _ in ()).throw(RuntimeError('boom')))
    monkeypatch.setattr(monitor, 'check_watching', lambda *a, **k: None)
    monkeypatch.setattr(monitor, 'check_entered',
                        lambda *a, **k: ran.append(True))
    monitor.run_cycle(store, kite=None, do_scan=False)
    assert ran, "a vet-layer crash stopped the entered-position monitor"
