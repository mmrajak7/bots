"""The hold budget must survive the defer episodes it exists to bound.

THE DEFECT THIS PINS (found 2026-08-31). `_apply_hold_budget` stamps
`held_since`/`held_date` into the exit-vet marker; `_request_exit_vet` then
REPLACED that marker wholesale on every re-request after a defer, carrying
`defers` across but not the hold stamps. `_apply_hold_budget` found no stamp
and started the clock again -- so the wait a fired stop could take was
roughly TWICE `exit_vet_max_hold_sec`, and longer with defers ahead of it.

That is the single control making the vet additive rather than load-bearing,
and a clock the waiting party resets is not a bound. ASHOKLEY #390 is the
shape it was shipped for: -50% at the trigger, -75% three cycles later, on an
agent that had died on quota two seconds after spawning.

WHY THE EXISTING BUDGET SUITE COULD NOT SEE IT. Every test in
`test_exit_vet_budget.py` monkeypatches `_exit_gate_policy` with a lambda
returning a fixed verdict -- so the real policy never runs, never calls
`_request_exit_vet`, and the marker is never replaced. The interaction lived
entirely between two functions that were each tested alone. These tests drive
the REAL policy with `spawn=False`.

Run:  cd Helper && python -m pytest \\
        zebra/tests/test_exit_vet_budget_real_policy.py -v
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg            # noqa: E402
from zebra import vet as vet_mod           # noqa: E402
from zebra.trade_store import ZebraStore    # noqa: E402

MIDDAY = datetime(2026, 9, 15, 12, 0, 0)

#: An unreliable book, so `needs_exit_vet` always flags it and the gate is
#: genuinely exercised rather than short-circuited.
QUOTE = {'mid': 4.0, 'reliable': False, 'reason': 'wide book', 'legs': {},
         'floored': False}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    monkeypatch.setattr(vet_mod, 'cli_blocked_until', lambda: None)
    s = ZebraStore(config={'google_drive': {'enabled': False}})
    s.add_signal({'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
                  'st_value': 100.0, 'st_direction': 'UP',
                  'signal_price': 96.0, 'signal_gap_pct': 4.0})
    s.mark_entered(1, {'long_strike': 100.0, 'short_strike': 140.0,
                       'long_symbol': 'L', 'short_symbol': 'S', 'debit': 10.0,
                       'lot_size': 100, 'lots': 1, 'expiry': '2026-09-30',
                       'structure': 'bcs'})
    return s


def _clock(monkeypatch, when):
    monkeypatch.setattr(vet_mod, '_now', lambda: when)


def _gate(store, when):
    """One real gate cycle at `when`. `spawn=False`: no CLI, real policy."""
    return vet_mod.exit_gate(store, store.find(1), 'debit_sl', QUOTE, 95.0,
                             spawn=False, incycle_wait=0, now=when)


def _marker(store):
    return vet_mod._exit_marker(store.find(1) or {}, 'debit_sl')


# -- the clock survives a defer ---------------------------------------------

def test_the_hold_clock_is_not_reset_by_a_defer_re_request(store, monkeypatch):
    """THE DEFECT. Cycle 1 stamps; a defer re-requests; the stamp must stand."""
    _clock(monkeypatch, MIDDAY)
    _gate(store, MIDDAY)
    first = _marker(store).get(vet_mod.HELD_SINCE)
    assert first, 'the first hold must start the clock'

    # Claude answers `defer`: the exit is not cleared and the next cycle
    # raises a fresh request carrying defers=1.
    vet_mod.record_exit_verdict(store, 1, 'debit_sl', 'defer', decision_id=1)

    later = MIDDAY + timedelta(seconds=300)
    _clock(monkeypatch, later)
    _gate(store, later)

    assert _marker(store).get(vet_mod.HELD_SINCE) == first, (
        'the re-request replaced the marker and restarted the hold budget — '
        'the stop can now wait about twice the configured cap')


def test_the_budget_still_expires_on_schedule_across_a_defer(store,
                                                            monkeypatch):
    """The consequence that matters: the stop proceeds at the cap, not 2x it."""
    budget = vet_mod._hold_budget_sec()
    assert budget, 'this test is meaningless with an unbounded budget'

    _clock(monkeypatch, MIDDAY)
    assert _gate(store, MIDDAY) != 'proceed'
    vet_mod.record_exit_verdict(store, 1, 'debit_sl', 'defer', decision_id=1)

    # Re-request partway through the budget...
    mid = MIDDAY + timedelta(seconds=budget // 2)
    _clock(monkeypatch, mid)
    assert _gate(store, mid) != 'proceed'

    # ...and one second past the ORIGINAL deadline the exit must go.
    end = MIDDAY + timedelta(seconds=budget + 1)
    _clock(monkeypatch, end)
    assert _gate(store, end) == 'proceed', (
        'the budget was measured from the re-request, not from when the stop '
        'actually started waiting')


def test_a_fresh_episode_gets_a_fresh_clock(store, monkeypatch):
    """The carry must NOT leak across a resolved episode.

    An `allow` that later goes stale is not a stop that has been waiting, so
    carrying its stamp would spend the budget on time the exit was free and
    fail the very next re-vet open. `defers > 0` is the continuation test.
    """
    _clock(monkeypatch, MIDDAY)
    _gate(store, MIDDAY)
    vet_mod.record_exit_verdict(store, 1, 'debit_sl', 'allow', decision_id=1)
    assert _gate(store, MIDDAY) == 'proceed'

    # Days later the ALLOW is stale, so a new episode opens.
    stale = MIDDAY + timedelta(seconds=cfg.EXIT_VET_TTL_SEC + 1)
    _clock(monkeypatch, stale)
    verdict = _gate(store, stale)
    assert verdict != 'proceed', (
        'a stale allow must be re-vetted, not waved through by a carried '
        'hold stamp')


def test_the_budget_is_still_scoped_to_one_session(store, monkeypatch):
    """A Friday hold must not spend Monday's budget, carry or no carry."""
    _clock(monkeypatch, MIDDAY)
    _gate(store, MIDDAY)
    vet_mod.record_exit_verdict(store, 1, 'debit_sl', 'defer', decision_id=1)

    next_day = MIDDAY + timedelta(days=3)
    _clock(monkeypatch, next_day)
    _gate(store, next_day)
    m = _marker(store)
    assert m.get(vet_mod.HELD_DATE) == next_day.date().isoformat(), (
        'a stamp from a previous session must be re-based, not carried into '
        "today's budget")


def test_the_carry_keeps_the_defer_count(store, monkeypatch):
    """Regression guard: the carry must not disturb what already worked."""
    _clock(monkeypatch, MIDDAY)
    _gate(store, MIDDAY)
    vet_mod.record_exit_verdict(store, 1, 'debit_sl', 'defer', decision_id=1)
    later = MIDDAY + timedelta(seconds=60)
    _clock(monkeypatch, later)
    _gate(store, later)
    assert int(_marker(store).get('defers') or 0) == 1
