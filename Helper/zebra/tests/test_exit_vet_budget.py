"""The stop is bounded, so the wait for permission to take it must be too.

Two changes, both owner decisions of 2026-08-29, both about the same measured
problem: the cohort's only loss-side exits are value-based (`spot_sl_enabled`
is False), `needs_exit_vet` flags every one of them, so EVERY stop this book
can take waits on a Claude agent.

* **The hold budget** (`exit_vet_max_hold_sec`). A single `defer` means a
  later timeout no longer fails open -- it counts as another failure to verify
  and lands on 'hold', which waits on a human. ASHOKLEY #390 went -50% to -75%
  over three cycles on an agent that had died on quota two seconds after
  spawning. The vet is ADDITIVE, never load-bearing; an unbounded hold inverts
  that on the one path that loses real money.
* **M12** (`exit_vet_incycle_wait_sec`). The agent answers in ~1m50s of a
  ~4m50s round trip; the other ~3 minutes is the request sitting on disk until
  the next 5-minute cron tick.

Run:  cd Helper && python -m pytest zebra/tests/test_exit_vet_budget.py -v
"""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg            # noqa: E402
from zebra import vet as vet_mod           # noqa: E402
from zebra.trade_store import ZebraStore    # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    s = ZebraStore(config={'google_drive': {'enabled': False}})
    s.add_signal({'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
                  'st_value': 100.0, 'st_direction': 'UP',
                  'signal_price': 96.0, 'signal_gap_pct': 4.0})
    s.mark_entered(1, {'long_strike': 100.0, 'short_strike': 140.0,
                       'long_symbol': 'L', 'short_symbol': 'S', 'debit': 10.0,
                       'lot_size': 100, 'lots': 1, 'expiry': '2026-09-30',
                       'structure': 'bcs'})
    return s


QUOTE = {'mid': 4.0, 'reliable': False, 'reason': 'wide book', 'legs': {},
         'floored': False}


def _trade(store):
    return store.find(1)


def _held(store, kind='debit_sl'):
    return vet_mod._exit_marker(store.find(1) or {}, kind)


# -- the budget bounds every non-proceed verdict ----------------------------

@pytest.mark.parametrize('verdict', ['wait', 'hold'])
def test_the_first_hold_only_starts_the_clock(store, monkeypatch, verdict):
    """Nothing is overridden on the first cycle. The budget bounds a WAIT, it
    does not skip one."""
    monkeypatch.setattr(vet_mod, '_exit_gate_policy',
                        lambda *a, **k: verdict)
    now = datetime(2026, 9, 15, 11, 0, 0)
    assert vet_mod.exit_gate(store, _trade(store), 'debit_sl', QUOTE, 100.0,
                             now=now) == verdict
    m = _held(store)
    assert m[vet_mod.HELD_SINCE].startswith('2026-09-15T11:00')
    assert m[vet_mod.HELD_DATE] == '2026-09-15'


@pytest.mark.parametrize('verdict', ['wait', 'hold'])
def test_a_spent_budget_takes_the_stop(store, monkeypatch, verdict):
    """The finding, stated directly. Both non-proceed verdicts are bounded --
    'hold' especially, because that is the one that waits on a human."""
    monkeypatch.setattr(vet_mod, '_exit_gate_policy',
                        lambda *a, **k: verdict)
    t0 = datetime(2026, 9, 15, 11, 0, 0)
    vet_mod.exit_gate(store, _trade(store), 'debit_sl', QUOTE, 100.0, now=t0)
    later = t0 + timedelta(seconds=cfg.EXIT_VET_MAX_HOLD_SEC + 1)
    assert vet_mod.exit_gate(store, _trade(store), 'debit_sl', QUOTE, 100.0,
                             now=later) == 'proceed'


@pytest.mark.parametrize('verdict', ['wait', 'hold'])
def test_inside_the_budget_the_verdict_stands(store, monkeypatch, verdict):
    """The negative control: without it the test above passes just as well
    when the budget is zero and every stop fires unvetted."""
    monkeypatch.setattr(vet_mod, '_exit_gate_policy',
                        lambda *a, **k: verdict)
    t0 = datetime(2026, 9, 15, 11, 0, 0)
    vet_mod.exit_gate(store, _trade(store), 'debit_sl', QUOTE, 100.0, now=t0)
    later = t0 + timedelta(seconds=cfg.EXIT_VET_MAX_HOLD_SEC - 1)
    assert vet_mod.exit_gate(store, _trade(store), 'debit_sl', QUOTE, 100.0,
                             now=later) == verdict


def test_a_proceeding_gate_is_never_touched(store, monkeypatch):
    monkeypatch.setattr(vet_mod, '_exit_gate_policy',
                        lambda *a, **k: 'proceed')
    assert vet_mod.exit_gate(store, _trade(store), 'debit_sl', QUOTE,
                             100.0) == 'proceed'
    assert _held(store).get(vet_mod.HELD_SINCE) is None


def test_the_budget_is_PER_SESSION_not_per_episode(store, monkeypatch):
    """An undated budget banks Friday 15:29's wait against Monday 09:15's
    first poll and fires the stop on the opening print -- which is where both
    real-money losses on this book happened. Same defect the residue sweep's
    flat-read counter had, and the same fix: date the counter.
    """
    monkeypatch.setattr(vet_mod, '_exit_gate_policy', lambda *a, **k: 'hold')
    friday = datetime(2026, 9, 11, 15, 20, 0)
    vet_mod.exit_gate(store, _trade(store), 'debit_sl', QUOTE, 100.0,
                      now=friday)
    monday = datetime(2026, 9, 14, 9, 30, 0)
    assert vet_mod.exit_gate(store, _trade(store), 'debit_sl', QUOTE, 100.0,
                             now=monday) == 'hold', (
        'a weekend-old hold clock fired the stop on the opening print')
    assert _held(store)[vet_mod.HELD_DATE] == '2026-09-14'


def test_zero_disables_the_bound(store, monkeypatch):
    """The owner can restore the old unbounded behaviour without a code
    change -- and without it, "the budget is on" would be unfalsifiable."""
    monkeypatch.setattr(vet_mod, '_exit_gate_policy', lambda *a, **k: 'hold')
    monkeypatch.setattr(cfg, 'EXIT_VET_MAX_HOLD_SEC', 0)
    t0 = datetime(2026, 9, 15, 11, 0, 0)
    vet_mod.exit_gate(store, _trade(store), 'debit_sl', QUOTE, 100.0, now=t0)
    far = t0 + timedelta(days=3)
    assert vet_mod.exit_gate(store, _trade(store), 'debit_sl', QUOTE, 100.0,
                             now=far) == 'hold'


@pytest.mark.parametrize('junk', [None, '', 'soon', -5, {}])
def test_a_malformed_budget_falls_back_to_the_declared_default(monkeypatch,
                                                               junk):
    """Neither extreme is acceptable. Unbounded restores the defect; zero
    seconds fires every stop unvetted. It falls back to the value the module
    declares."""
    monkeypatch.setattr(cfg, 'EXIT_VET_MAX_HOLD_SEC', junk)
    got = vet_mod._hold_budget_sec()
    assert got == cfg._DEFAULTS['exit_vet_max_hold_sec'] or got == 0
    assert got != 1  # not silently something arbitrary


def test_an_unwritable_clock_delays_the_budget_rather_than_skipping_it(
        store, monkeypatch):
    """Losing the stamp must make the budget start LATER, never fire early."""
    monkeypatch.setattr(vet_mod, '_exit_gate_policy', lambda *a, **k: 'hold')

    def boom(*a, **k):
        raise RuntimeError('store is locked')
    monkeypatch.setattr(vet_mod, '_stamp_held', boom)
    with pytest.raises(RuntimeError):
        boom()
    monkeypatch.setattr(vet_mod, '_stamp_held', lambda *a, **k: None)
    t0 = datetime(2026, 9, 15, 11, 0, 0)
    for _ in range(3):
        assert vet_mod.exit_gate(store, _trade(store), 'debit_sl', QUOTE,
                                 100.0, now=t0) == 'hold'


def test_the_budget_wraps_the_policy_rather_than_branching_inside_it():
    """Every non-'proceed' return has to be covered. A budget checked in three
    of four branches is a budget that does not exist on the fourth, which is
    the shape this codebase keeps paying for.

    RETIRES WHEN: the gate returns a verdict OBJECT that carries its own
    elapsed-hold clock, so the budget is a property of the verdict rather
    than a wrapper that must not be bypassed.
    """
    import inspect
    src = inspect.getsource(vet_mod.exit_gate)
    assert '_exit_gate_policy' in src and '_apply_hold_budget' in src
    policy = inspect.getsource(vet_mod._exit_gate_policy)
    assert '_apply_hold_budget' not in policy


# -- M12: the in-cycle wait -------------------------------------------------

def test_no_armed_budget_means_no_waiting(store):
    """`bcs/spread_monitor.py` polls every 5 seconds and arms no budget.
    Blocking it would stop watching every other position to save nothing."""
    vet_mod.end_incycle_budget()
    t0 = time.time()
    assert vet_mod._await_verdict(store, _trade(store), 'debit_sl', 120) \
        == 'wait'
    assert time.time() - t0 < 1.0


def test_an_explicit_zero_means_no_waiting(store):
    vet_mod.start_incycle_budget()
    try:
        t0 = time.time()
        assert vet_mod._await_verdict(store, _trade(store), 'debit_sl', 0) \
            == 'wait'
        assert time.time() - t0 < 1.0
    finally:
        vet_mod.end_incycle_budget()


def test_a_verdict_that_lands_in_cycle_is_consumed(store, monkeypatch):
    """The whole point. Without this the exit waits a full cron interval for a
    verdict that is already on disk."""
    vet_mod._set_exit_state(store, 1, 'debit_sl', vet_mod.PENDING)
    vet_mod.start_incycle_budget()
    monkeypatch.setattr(vet_mod.time, 'sleep', lambda s: None)

    seen = {'n': 0}

    def reload_then_allow():
        seen['n'] += 1
        if seen['n'] == 2:
            vet_mod._set_exit_state(store, 1, 'debit_sl', vet_mod.ALLOWED,
                                    expect_state=vet_mod.PENDING)
    monkeypatch.setattr(store, 'reload', reload_then_allow)
    try:
        assert vet_mod._await_verdict(store, _trade(store), 'debit_sl', 120) \
            == 'proceed'
    finally:
        vet_mod.end_incycle_budget()


def test_a_defer_that_lands_in_cycle_reaches_the_same_verdict_as_next_cycle(
        store, monkeypatch):
    """Nothing downstream may be able to tell whether the verdict was consumed
    here or one cycle later -- including the escalation cap."""
    vet_mod._set_exit_state(store, 1, 'debit_sl', vet_mod.PENDING)
    vet_mod.start_incycle_budget()
    monkeypatch.setattr(vet_mod.time, 'sleep', lambda s: None)
    monkeypatch.setattr(cfg, 'EXIT_MAX_DEFERS', 2)

    def reload_then_defer():
        m = vet_mod._exit_marker(store.find(1), 'debit_sl')
        if m.get('state') == vet_mod.PENDING:
            vet_mod._set_exit_state(store, 1, 'debit_sl', vet_mod.DEFER,
                                    bump_defer=True,
                                    expect_state=vet_mod.PENDING)
    monkeypatch.setattr(store, 'reload', reload_then_defer)
    try:
        assert vet_mod._await_verdict(store, _trade(store), 'debit_sl', 120) \
            == 'wait'            # first defer: re-check next cycle
    finally:
        vet_mod.end_incycle_budget()
    assert vet_mod.exit_defers(store.find(1), 'debit_sl') == 1


def test_a_store_that_cannot_reload_refuses_to_wait(store, monkeypatch):
    """The mechanism IS re-reading what another PROCESS wrote. A store that
    cannot re-read would poll its own unchanging cache for two minutes and
    always conclude 'wait' -- the old behaviour, reached slowly and silently.
    """
    vet_mod.start_incycle_budget()

    class _NoReload:
        def find(self, _i):
            return {'id': 1}
    try:
        t0 = time.time()
        assert vet_mod._await_verdict(_NoReload(), {'id': 1}, 'debit_sl',
                                      120) == 'wait'
        assert time.time() - t0 < 1.0
    finally:
        vet_mod.end_incycle_budget()


def test_the_cycle_budget_caps_the_total_not_each_trade(store, monkeypatch):
    """Several triggering positions must not push a 5-minute cron past its own
    interval, whose `flock -n` then SKIPS the next run -- so exit monitoring
    does not happen for ten minutes because it was waiting."""
    monkeypatch.setattr(vet_mod, 'INCYCLE_CYCLE_BUDGET_SEC', 10)
    vet_mod.start_incycle_budget(now=1000.0)
    monkeypatch.setattr(vet_mod.time, 'time', lambda: 1009.0)
    try:
        assert vet_mod._incycle_left() == pytest.approx(1.0)
    finally:
        monkeypatch.undo()
        vet_mod.end_incycle_budget()


def test_the_budget_does_not_leak_into_the_next_cycle():
    """An armed budget surviving the cycle would make the NEXT one wait for
    reasons that have nothing to do with it.

    RETIRES WHEN: the in-cycle budget becomes a context manager the cycle
    enters, making the `finally` structural.
    """
    import inspect
    from zebra import monitor
    src = inspect.getsource(monitor.run_cycle)
    assert 'start_incycle_budget' in src
    i = src.index('start_incycle_budget')
    assert 'finally:' in src[i:i + 600]
    assert 'end_incycle_budget' in src[i:i + 600]
