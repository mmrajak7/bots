"""zebra's 5-minute cycle reaches the naked hooks, in the order that matters.

ORDER IS THE CONTRACT: the adoption sweep (P3c) and `/nresume` (P6) must both
run BEFORE `naked_entry.begin_cycle` -- an adopted position has to count
against this cycle's cap, and a cleared kill has to be seen by this cycle's
halt evaluation. Both are skipped under --dry-run. Same source-reading pattern
as `test_m2_entry_budget.py` (the cycle pulls the scanner, Kite and Drive, so
it is pinned structurally, and each hook is tested functionally in its own file).

RETIRES WHEN: run_cycle takes its phases from a list a test can drive.

Run:  cd Helper && python -m pytest zebra/tests/test_naked_cycle_hooks.py -v
"""
import inspect
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import monitor        # noqa: E402


def _body():
    return inspect.getsource(monitor.run_cycle)


def test_both_hooks_run_before_the_halt_is_evaluated():
    src = _body()
    a = src.index('naked_adopt.sweep(')
    t = src.index('naked_tg.poll_resume(')
    b = src.index('naked_entry.begin_cycle(')
    assert a < b and t < b


def test_both_hooks_are_skipped_under_dry_run_and_cannot_raise():
    src = _body()
    for hook in ('naked_adopt.sweep(', 'naked_tg.poll_resume('):
        i = src.index(hook)
        window = src[max(0, i - 400):i]
        assert 'if not dry_run:' in window and 'try:' in window, hook


def test_the_adoption_sweep_does_not_depend_on_the_entry_switch():
    """An order sent in `live` is settled as live after the switch moves."""
    src = _body()
    i = src.index('naked_adopt.sweep(')
    assert "NAKED_LIVE != 'off'" not in src[max(0, i - 400):i]


def test_an_unclear_entry_is_swept_again_at_the_end_of_the_same_cycle():
    """Review L3: a second sweep after the entries, under the same guards."""
    src = _body()
    b = src.index('naked_entry.end_cycle()')
    j = src.index('naked_adopt.sweep(', b)
    window = src[b:j]
    assert 'if not dry_run:' in window and 'try:' in window and 'unresolved_twins' in window
