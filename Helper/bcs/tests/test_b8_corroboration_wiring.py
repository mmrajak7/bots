"""B8 — `spot_corroborates` must be wired into `monitor_all`, not just monitor().

The classic shape from `test_cron_wiring.py`'s docstring, one more time: the
guard was written, unit-tested and merged into `monitor()` — the single-trade
mode nobody runs — while `monitor_all()`, the `--cron` entrypoint on the Pi,
never got it.

Why this one outranks defects that fire more often: every OTHER loss-side check
in the cron loop reads the same order book. The reliability gate, the intrinsic
floor, the 3-poll debounce and the blind alert are four checks on ONE source,
so a tidy-but-wrong quote confirms itself three times and trades. Spot is the
only source that cannot be wrong in the same way. Count SOURCES, not checks.

These are wiring tests by design — see that docstring. The behavioural half
arrives with the replay harness (NHPC fixture).

Run:  cd Helper && python -m pytest bcs/tests/test_b8_corroboration_wiring.py -v
"""
import inspect
import re
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm       # noqa: E402


def _cron_source() -> str:
    return inspect.getsource(sm.monitor_all)


# ── The assertions, factored out so the negative control can reuse them ──────

def _call_pos(src: str, name: str):
    """Offset of the first CALL to `name`, or None.

    Deliberately not `src.index(name)`: the surrounding code comments name
    these guards, and the first draft of this test matched a comment instead
    of the call, inverting the ordering check. Match `name(` and require it
    not to be preceded by `#` on its line.
    """
    for m in re.finditer(re.escape(name) + r'\s*\(', src):
        line_start = src.rfind('\n', 0, m.start()) + 1
        if '#' not in src[line_start:m.start()]:
            return m.start()
    return None


def _assert_corroboration_wired(src: str) -> None:
    """Every structural requirement B8 has. Raises AssertionError if unmet."""
    corrob_at = _call_pos(src, 'spot_corroborates')
    blind_at = _call_pos(src, 'track_spread_blindness')

    assert corrob_at is not None, (
        "monitor_all must CALL spot_corroborates — the guard existing in "
        "monitor() and passing its own unit tests is not wiring, and neither "
        "is a comment mentioning it")

    assert 'corrob_state' in src, (
        "spot_corroborates needs per-trade reference state threaded through "
        "the cron loop; a shared or absent dict makes it inert")

    # Ordering is load-bearing, not stylistic: a veto must be visible to the
    # blind tracker, which is what actually Telegrams the operator.
    assert blind_at is not None, "monitor_all no longer calls track_spread_blindness"
    assert corrob_at < blind_at, (
        "the corroboration check must run BEFORE track_spread_blindness so a "
        "veto counts as BLIND and gets alerted; after it, the veto is silent")


def _assert_veto_only(src: str) -> None:
    """It may refuse an exit the book asked for. It may never ask for one."""
    at = _call_pos(src, 'spot_corroborates')
    assert at is not None, "no spot_corroborates call to check"
    window = src[at:at + 600]
    assert 'spread_val = None' in window, (
        "a failed corroboration must set spread_val = None (suppressing "
        "SL_SPREAD, SL_TRAIL and the trail-peak update), not trigger a close")
    assert not re.search(r'\bclose_spread\(|\bclose_leg\(', window), (
        "the corroboration branch must not reach any close path — it is a "
        "VETO, never a TRIGGER")


# ── The tests ────────────────────────────────────────────────────────────────

def test_spot_corroborates_is_wired_into_the_cron_loop():
    _assert_corroboration_wired(_cron_source())


def test_the_veto_can_only_prevent_an_exit_never_cause_one():
    _assert_veto_only(_cron_source())


def test_the_reference_is_cleared_when_a_trade_closes():
    """A reopened id must not inherit the closed trade's reference.

    `spot_corroborates` judges a collapse against its stored reference; a
    stale one from a different position is exactly the false-veto that would
    strand a real exit.
    """
    src = _cron_source()
    assert re.search(r'corrob_state\.pop\(', src), (
        "corrob_state must be popped alongside the other per-trade state when "
        "a trade closes")


def test_corroboration_state_is_per_trade_not_shared():
    """Keyed by close_key like every other per-trade dict."""
    src = _cron_source()
    assert re.search(r'corrob_state\[close_key\]', src), (
        "corrob_state must be indexed by close_key; one shared reference "
        "across positions would compare NHPC's spread against MCX's spot")


# ── Negative controls ────────────────────────────────────────────────────────
# Each proves the assertion above is sensitive to THIS guard, and would not be
# satisfied by some unrelated line that happens to be in the same function.

def test_the_wiring_assertions_fail_when_the_call_is_removed():
    src = _cron_source().replace('spot_corroborates', 'x_removed_')
    with pytest.raises(AssertionError):
        _assert_corroboration_wired(src)


def test_the_wiring_assertions_fail_when_the_state_is_removed():
    src = _cron_source().replace('corrob_state', 'x_removed_')
    with pytest.raises(AssertionError):
        _assert_corroboration_wired(src)


def test_the_ordering_assertion_is_real():
    """Prove the before/after check can actually fail.

    Build a source string where the two names appear in the wrong order and
    assert it is rejected — otherwise a future refactor that moves the guard
    below the blind tracker would pass silently.
    """
    bad = ("corrob_state = {}\n"
           "track_spread_blindness(...)\n"
           "ok, why = spot_corroborates(corrob_state[close_key], spot, v)\n")
    with pytest.raises(AssertionError):
        _assert_corroboration_wired(bad)


def test_the_veto_only_assertion_is_real():
    """A branch that closed instead of vetoing must be rejected."""
    bad = ("ok, why = spot_corroborates(corrob_state[close_key], spot, v)\n"
           "if not ok:\n"
           "    close_spread(kite, trade, 'QUOTE GUARD', dry_run)\n")
    with pytest.raises(AssertionError):
        _assert_veto_only(bad)
