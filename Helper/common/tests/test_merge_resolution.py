"""A version counter is not a clock, and a booked exit is not undone by one.

THE DEFECT THIS PINS (found 2026-08-31). Both stores resolved a merge on
`version` alone, and a version is a per-record counter each replica
increments on its OWN writes. Two silent failures follow:

  1. A TIE with different content. Base wins, and the divergence check that
     decides whether to re-upload compares VERSION MAPS only -- so the two
     replicas disagree permanently with no log line, no alert and no
     re-upload.

  2. A BOOKED EXIT UN-BOOKED. Local `exited` at version 7 against Drive
     `entered` at version 8 -- two alert-flag bumps on the other machine are
     enough to outrun a close. Higher version wins, the exit record is erased,
     and the trade REOPENS as a position nobody entered.

(2) is the one that costs money, and `store_contract.CONTRACT` already had the
answer: TERMINAL refuses every method, "because that is what idempotence IS".
The merge was the one path not asking.

THE CARVE-OUT IS DELIBERATE and keyed on an explicit marker rather than on the
transition. `restore_snapshot` exists precisely to un-book a wrongly-booked
close -- the 2026-08-30 `deploy_server.sh` incident force-closed six live
positions at -100% and the recovery works by out-versioning those exits. A
rule that refused every reopen would break the only tool that has ever had to
repair this book, so `rebuild`'s own `restored_from_snapshot_at` stamp is what
distinguishes an authorised reopen from a stale replica.

Run:  cd Helper && python -m pytest common/tests/test_merge_resolution.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common import store_contract as sc                          # noqa: E402

Z = sc.ZEBRA_STATUSES


def _rec(tid=1, status='entered', version=1, **extra):
    r = {'id': tid, 'status': status, 'version': version}
    r.update(extra)
    return r


# -- the money case ---------------------------------------------------------

def test_a_booked_exit_is_not_reopened_by_a_higher_version():
    """THE DEFECT. Pre-fix the exit was erased and the trade reopened."""
    local_closed = _rec(status='exited', version=7, exit_reason='paper:tp')
    stale_open = _rec(status='entered', version=8)
    winner, note = sc.resolve_merge(local_closed, stale_open, Z)
    assert winner is local_closed
    assert note and 'not undone by a version counter' in note


def test_that_conflict_is_reported_not_swallowed():
    """It cannot be inferred from the book -- the replicas simply differ."""
    winner, note = sc.resolve_merge(
        _rec(status='exited', version=7), _rec(status='entered', version=8), Z)
    assert note is not None


def test_a_closed_record_at_a_lower_incoming_version_is_quiet():
    """Ordinary and uninteresting: no note, no alarm."""
    local_closed = _rec(status='exited', version=9)
    winner, note = sc.resolve_merge(local_closed, _rec(version=3), Z)
    assert winner is local_closed and note is None


# -- the restore carve-out --------------------------------------------------

def test_a_snapshot_restore_MAY_reopen_a_closed_record():
    """The 2026-08-30 recovery. A blanket rule would have broken it."""
    local_closed = _rec(status='exited', version=7, exit_reason='reset')
    restored = _rec(status='entered', version=8,
                    restored_from_snapshot_at='2026-08-30T09:00:00')
    winner, note = sc.resolve_merge(local_closed, restored, Z)
    assert winner is restored
    assert note and 'REOPENED by a snapshot restore' in note


def test_the_restore_carve_out_needs_the_explicit_marker():
    """Intent is named, never inferred from the transition."""
    winner, _ = sc.resolve_merge(
        _rec(status='exited', version=7),
        _rec(status='entered', version=8), Z)
    assert winner['status'] == 'exited'


def test_the_marker_name_matches_what_rebuild_actually_stamps():
    """A carve-out keyed on a field nobody writes is a carve-out that is off.

    RETIRES WHEN: `restore_snapshot.rebuild` stops stamping a marker, i.e. the
    restore path no longer needs to out-rank a close.
    """
    import inspect
    from zebra import restore_snapshot as rs
    assert sc.RESTORE_MARKER in inspect.getsource(rs.rebuild)


def test_the_real_restore_still_beats_a_reset(tmp_path):
    """End to end through the REAL rebuild and the REAL merge.

    The regression that caught this rule out when it was first written too
    broadly: `_merge(restored, live)` must keep the restored record.
    """
    from zebra import restore_snapshot as rs
    from zebra.trade_store import ZebraStore
    snapshot = [_rec(423, 'entered', version=14, stock='TMPV')]
    live = [_rec(423, 'exited', version=15, stock='TMPV',
                 exit_reason='reset_force_close')]
    merged = ZebraStore._merge(rs.rebuild(snapshot, live), live)
    assert merged[0]['status'] == 'entered'


# -- the split brain --------------------------------------------------------

def test_an_equal_version_with_different_content_is_reported():
    """Silent before: the divergence check compares version maps only."""
    a = _rec(version=4, notes='local')
    b = _rec(version=4, notes='remote')
    winner, note = sc.resolve_merge(a, b, Z)
    assert winner is a, 'base still wins; what changed is that it is said'
    assert note and 'DIFFERENT content' in note
    assert 'will not converge on their own' in note, (
        'the reader must be told this does not fix itself')


def test_an_equal_version_with_identical_content_is_silent():
    """The common case must not become noise."""
    a = _rec(version=4)
    b = _rec(version=4)
    winner, note = sc.resolve_merge(a, b, Z)
    assert winner is a and note is None


# -- robustness -------------------------------------------------------------

def test_it_never_raises():
    """A merge that can fail on odd data strands the whole book."""
    for bad in ({}, {'id': 1}, {'id': 1, 'status': None},
                {'id': 1, 'version': 'x'}):
        winner, note = sc.resolve_merge(_rec(), bad, Z)
        assert winner is not None
        winner, note = sc.resolve_merge(bad, _rec(), Z)
        assert winner is not None


def test_an_unknown_status_falls_through_to_version_resolution():
    """`role_of` returns None for a status it cannot speak for; that must not
    be mistaken for TERMINAL in either direction."""
    winner, _ = sc.resolve_merge(
        _rec(status='some_future_state', version=1),
        _rec(status='some_future_state', version=2), Z)
    assert winner['version'] == 2


def test_the_bcs_family_vocabulary_works_too():
    """The three other books say open/closed, not entered/exited."""
    local_closed = {'id': 1, 'status': 'closed', 'version': 7}
    stale_open = {'id': 1, 'status': 'open', 'version': 8}
    winner, note = sc.resolve_merge(local_closed, stale_open,
                                    sc.BCS_FAMILY_STATUSES)
    assert winner is local_closed and note
