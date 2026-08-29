"""M2 - a slow entry must not cost the next cycle's exit monitoring.

The arithmetic: one leg can spend `ENTRY_MAX_ATTEMPTS` (2) x (`ORDER_WAIT_SEC`
30 + a 5s re-quote sleep) = 70s; two legs per round, one round per lot, so
~140s for a single one-lot spread — and `check_watching` can enter several
signals in a cycle. Four of them is ~9 minutes against a 5-minute cron whose
`flock -n` SKIPS the next run. Exit monitoring would then not execute for ten
minutes because an ENTRY was slow, which inverts the ordering `run_cycle` was
deliberately given ("EXITS FIRST... exit monitoring is the only phase here
that can lose money by not running").

The budget is checked ONLY BEFORE an entry starts. Interrupting one would
abandon it between the long leg and the short — an ORPHAN LONG, a real
position nobody asked for. Whole entries is the safe granularity: a signal not
started stays 'triggered' and the next cycle picks it up.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_m2_entry_budget.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zebra import monitor as monitor_mod    # noqa: E402


@pytest.fixture(autouse=True)
def _disarmed():
    """Every test starts with no budget armed, and leaves none behind. An
    armed deadline leaking between tests is the module-global equivalent of
    the shared-fixture bug that made six replay tests pass only in isolation.
    """
    monitor_mod.end_entry_phase()
    yield
    monitor_mod.end_entry_phase()


def test_no_budget_armed_means_no_refusal():
    """Outside an entry phase the check must be inert. A budget that refuses
    when nothing armed it would block the CLI's manual entry paths."""
    assert monitor_mod.entry_budget_open() is True


def test_a_fresh_phase_is_open():
    monitor_mod.start_entry_phase(now=1000.0)
    assert monitor_mod.entry_budget_open(now=1000.0) is True


def test_the_budget_closes_when_it_is_spent():
    monitor_mod.start_entry_phase(now=1000.0)
    edge = 1000.0 + monitor_mod.ENTRY_PHASE_BUDGET_SEC
    assert monitor_mod.entry_budget_open(now=edge - 1) is True
    assert monitor_mod.entry_budget_open(now=edge) is False


def test_ending_the_phase_disarms_it():
    """An armed budget leaking past `check_watching` would refuse entries for
    reasons that have nothing to do with the cycle asking."""
    monitor_mod.start_entry_phase(now=1000.0)
    monitor_mod.end_entry_phase()
    assert monitor_mod.entry_budget_open(now=1e12) is True


def test_the_budget_leaves_a_one_lot_entry_its_full_attempts():
    """The number is not arbitrary: it must not cut a single entry short, or
    the cap would be creating the orphan it exists to avoid."""
    one_entry_worst_case = 2 * 2 * (30 + 5)      # legs x attempts x (wait+sleep)
    assert monitor_mod.ENTRY_PHASE_BUDGET_SEC >= one_entry_worst_case


def test_a_spent_budget_refuses_a_NEW_entry_with_a_reason(caplog):
    """And it is logged, not silent: a refusal nobody can see is
    indistinguishable from a signal that never arrived, which this book has
    been bitten by before."""
    monitor_mod.start_entry_phase(now=0.0)
    with caplog.at_level('WARNING'):
        allowed, why = monitor_mod._entries_allowed_or_log(
            {'id': 7, 'stock': 'TESTCO'})
    assert allowed is False
    assert 'budget' in why
    assert 'ENTRY BUDGET SPENT' in caplog.text


def test_the_budget_is_checked_BEFORE_the_arming_switch():
    """Before `ee.entries_allowed`, so an auto-entry-off box still exercises
    it — a guard first observed on the day it starts mattering has never been
    observed at all.

    **But not before PAPER_MODE, and that limit is worth stating.**
    `_entries_allowed_or_log` is reached only through `_auto_enter_bcs`, which
    `check_watching` calls only under `not cfg.PAPER_MODE`. So on today's box
    this code path does not execute at all, and the honest claim is "exercised
    where AUTO-ENTRY is off", not "where paper is on". The budget is for the
    live entry path and cannot be validated by the paper book — which is
    exactly why the arithmetic it bounds is pinned by
    `test_the_budget_leaves_a_one_lot_entry_its_full_attempts` rather than by
    observation.
    """
    src = Path(monitor_mod.__file__).read_text(encoding='utf-8')
    body = src[src.index('def _entries_allowed_or_log('):]
    body = body[:body.index('\ndef ', 1)]
    assert body.index('entry_budget_open()') < body.index('ee.entries_allowed')


def test_the_phase_is_armed_around_check_watching_and_always_disarmed():
    """`finally`, not a trailing call: `check_watching` is wrapped in its own
    `except`, and an exception path that skipped the disarm would leave the
    budget armed for the rest of the process."""
    src = Path(monitor_mod.__file__).read_text(encoding='utf-8')
    body = src[src.index('def run_cycle('):]
    body = body[:body.index('\ndef ', 1)]
    assert 'start_entry_phase()' in body
    tail = body[body.index('check_watching(store, kite, dry_run=dry_run)'):]
    assert 'finally:' in tail and 'end_entry_phase()' in tail


def test_the_budget_never_interrupts_an_entry_in_flight():
    """Pinned on the source. The executor's own loop must not consult it — a
    check inside the round loop could stop between the long leg and the short
    and leave an orphan long, which is worse than the delay it would save."""
    from bcs import entry_executor as ee
    src = Path(ee.__file__).read_text(encoding='utf-8')
    assert 'entry_budget_open' not in src


def test_the_budget_does_not_execute_in_paper_mode():
    """Pinned so the limitation above cannot be forgotten and then relied on.

    `_entries_allowed_or_log` sits behind `_auto_enter_bcs`, which
    `check_watching` calls only when paper mode is OFF. A future reader
    assuming the budget protects the paper cycle would be wrong.
    """
    src = Path(monitor_mod.__file__).read_text(encoding='utf-8')
    body = src[src.index('def check_watching('):]
    call = body.index('_auto_enter_bcs') if '_auto_enter_bcs' in body else None
    assert call is not None or 'not cfg.PAPER_MODE' in src, (
        'the entry path moved — re-derive where the budget actually runs')
