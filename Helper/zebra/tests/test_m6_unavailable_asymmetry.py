"""M6 - `UNAVAILABLE` means the opposite thing on the two sides of a trade.

It is stamped when the vet request could not even be MADE: a lock timeout, an
IO error, no agent slot. Until 2026-08-29 `zebra/monitor.py` treated it exactly
like `ALLOWED` on the ENTRY path, which made an entry that was never reviewed
indistinguishable from one that was reviewed and cleared.

The contradiction survived because it was unreachable: `vet.mark_unavailable`
— the only writer that would put it on an entry record — has never had a
caller. A dead branch is where a decided rule goes to be quietly reversed.

**Entries fail CLOSED.** A missed entry costs nothing; an unqualified one costs
capital (`feedback_no_rush_to_enter`).

**Exits fail OPEN, and must keep doing so.** `exit_gate` returns 'proceed' on
UNAVAILABLE on purpose: on that side the bounded outcome is ACTING, and an exit
deadline that depends on an LLM being reachable is how a stop stops working.

Same word, opposite safe direction, because the two sides have opposite
asymmetries. These tests pin BOTH halves, in one file, so nobody "unifies" them.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_m6_unavailable_asymmetry.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zebra import monitor as monitor_mod    # noqa: E402
from zebra import vet as vet_mod            # noqa: E402


# ── the ENTRY side: closed ──────────────────────────────────────────────────

def test_unavailable_is_not_on_the_entry_allowlist():
    """Pinned on the SOURCE, because the branch is currently unreachable —
    `mark_unavailable` has no caller — and a behavioural test of an
    unreachable branch proves nothing about it."""
    src = Path(monitor_mod.__file__).read_text(encoding='utf-8')
    assert 'state not in (vet_mod.ALLOWED, vet_mod.UNAVAILABLE)' not in src, (
        'UNAVAILABLE is back on the entry allowlist — an entry that was never '
        'vetted would proceed as though it had been reviewed and cleared')
    assert 'elif state != vet_mod.ALLOWED:' in src


def test_only_ALLOWED_is_an_entry_state():
    """The allowlist is explicit for a reason: entering used to be the DEFAULT
    for any state without an `elif`, so adding a state to the machine silently
    meant "enter unvetted"."""
    entry_states = {vet_mod.ALLOWED}
    for state in (vet_mod.UNAVAILABLE, vet_mod.VETOED, vet_mod.STARVED,
                  vet_mod.ABANDONED):
        assert state not in entry_states


def test_the_live_dedup_check_agrees_with_the_gate():
    """Two places read the state on the entry path. A rule honoured by one and
    not the other is this codebase's single most repeated defect."""
    src = Path(monitor_mod.__file__).read_text(encoding='utf-8')
    assert 'if state in (vet_mod.ALLOWED, vet_mod.UNAVAILABLE)' not in src
    assert 'if state == vet_mod.ALLOWED' in src


# ── the EXIT side: open, deliberately ───────────────────────────────────────

def test_the_exit_gate_still_PROCEEDS_on_unavailable():
    """The other half, and the reason this file exists rather than a one-line
    edit. Making the exit side match the entry side would mean a dead vet can
    block a stop — the failure the whole exit-vet safety argument is built to
    avoid."""
    src = Path(vet_mod.__file__).read_text(encoding='utf-8')
    gate = src[src.index('def exit_gate('):]
    assert "if state == UNAVAILABLE:" in gate


def test_the_asymmetry_is_written_down_where_it_is_decided():
    """A rule this counter-intuitive that lives only in a test gets 'fixed'."""
    src = Path(monitor_mod.__file__).read_text(encoding='utf-8')
    assert 'ENTRIES FAIL CLOSED' in src
    assert 'EXITS ARE THE OPPOSITE' in src


def test_mark_unavailable_no_longer_warns_against_being_wired_in():
    """It was uncallable-by-warning, not by design. The warning was the real
    open item; the function was always the right shape."""
    doc = vet_mod.mark_unavailable.__doc__ or ''
    assert 'SAFE TO WIRE IN' in doc
    # The removed sentence, not a word that also appears in the HISTORY the
    # docstring now tells. A substring that the correct text legitimately
    # contains is a proxy, not the property — the N11 lesson.
    assert 'Needs a code decision' not in doc, 'the stale warning is still there'
