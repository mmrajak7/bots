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


# -- the same rule, the other way round (found 2026-08-31, second review) ----
#
# `base` is not "the local disk" and not "the closed one" -- it is whichever
# side the caller passed first. The monotonic-close rule was written from the
# incident, which arrived with the closed copy as base, and so it only ever
# fired in that direction. Swap the arguments and the identical fault sailed
# through plain version resolution with no note.

def test_a_booked_exit_arriving_as_INCOMING_also_survives():
    """The mirror of the money case. Pre-fix: base won, exit DISCARDED."""
    stale_local_open = _rec(status='entered', version=8)
    drive_closed = _rec(status='exited', version=7, exit_reason='paper:tp')
    winner, note = sc.resolve_merge(stale_local_open, drive_closed, Z)
    assert winner is drive_closed
    assert note and 'not undone by a version counter' in note


def test_the_mirror_holds_at_an_equal_version_too():
    """A tie is the commonest shape: both replicas wrote once since the sync."""
    winner, note = sc.resolve_merge(
        _rec(status='entered', version=7),
        _rec(status='exited', version=7), Z)
    assert winner['status'] == 'exited' and note is not None


def test_the_mirror_speaks_the_bcs_vocabulary_as_well():
    """Three of the four books say open/closed. The rule is stated once."""
    winner, note = sc.resolve_merge(
        _rec(status='open', version=9), _rec(status='closed', version=8),
        sc.BCS_FAMILY_STATUSES)
    assert winner['status'] == 'closed' and note is not None


def test_the_mirror_does_NOT_undo_a_snapshot_restore():
    """`rebuild` sets version = max+1, so a restore is EXACTLY a non-settled
    base at a higher version than the exit it is reversing -- which is the
    mirror's own trigger shape. Without the marker check the mirror would
    re-book the close the restore was run to undo."""
    restored = _rec(status='entered', version=9,
                    restored_from_snapshot_at='2026-08-30T09:00:00')
    drive_closed = _rec(status='exited', version=7, exit_reason='reset')
    winner, note = sc.resolve_merge(restored, drive_closed, Z)
    assert winner is restored and note is None


def test_a_restored_record_can_still_be_closed_for_real_afterwards():
    """The marker is permanent, so it must not freeze the record forever.
    A genuinely later close out-versions it and lands the ordinary way."""
    restored = _rec(status='entered', version=9,
                    restored_from_snapshot_at='2026-08-30T09:00:00')
    real_close = _rec(status='exited', version=11, exit_reason='paper:tp')
    winner, _ = sc.resolve_merge(restored, real_close, Z)
    assert winner is real_close


# -- a freeze is not walked back by a counter either -------------------------

def test_partial_close_is_not_reopened_by_a_higher_version():
    """FROZEN means legs may be live at the broker with NOTHING watching them.
    Reopening it re-arms every auto-exit against an unknown position."""
    frozen = _rec(status='partial_close', version=7)
    stale_open = _rec(status='entered', version=8)
    winner, note = sc.resolve_merge(frozen, stale_open, Z)
    assert winner is frozen
    assert note and 'FROZEN' in note and 'by hand' in note


def test_a_COMPLETED_recovery_still_lands_on_a_frozen_record():
    """The freeze is protected in ONE direction only. A recovery that ran to
    completion on the other replica ends settled, and must propagate."""
    frozen = _rec(status='partial_close', version=7)
    recovered = _rec(status='exited', version=9, exit_reason='recovered')
    winner, _ = sc.resolve_merge(frozen, recovered, Z)
    assert winner is recovered


# -- a cancel is settled, though the CONTRACT table has no word for it -------

def test_a_cancelled_signal_is_not_resurrected_by_a_counter():
    """`cancelled` names no role, so `role_of` returns None and version
    resolution walked it back to `triggered` -- where it re-occupies its
    dedup slot and can re-alert and re-enter."""
    winner, note = sc.resolve_merge(
        _rec(status='triggered', version=8),
        _rec(status='cancelled', version=7), Z)
    assert winner['status'] == 'cancelled' and note is not None


def test_a_cancel_survives_from_the_base_side_too():
    winner, note = sc.resolve_merge(
        _rec(status='cancelled', version=7),
        _rec(status='triggered', version=8), Z)
    assert winner['status'] == 'cancelled' and note is not None


def test_ordinary_forward_progress_is_still_silent():
    """The guards must not turn every normal transition into an alert."""
    winner, note = sc.resolve_merge(
        _rec(status='watching', version=1),
        _rec(status='triggered', version=2), Z)
    assert winner['status'] == 'triggered' and note is None


# -- two DIFFERENT trades wearing one id (found 2026-08-31) ------------------
#
# Id allocation is per-replica `max(live, sidecar) + 1`, so during a sync gap
# -- five minutes normally, arbitrarily long in Drive-down local-only mode --
# two machines can both mint id 15 for DIFFERENT trades. The owner
# hand-captures BCS trades on Windows while the Pi writes the same books, so
# the topology is supported rather than hypothetical.
#
# Everything else in this file then resolved them as ONE record: version
# picked a winner and the other trade ceased to exist, described (at most) as
# a field conflict. `partition_readable`'s duplicate-id quarantine cannot
# help -- it only looks WITHIN one file.

def _t(tid=15, **extra):
    r = {'id': tid, 'status': 'entered', 'version': 1, 'stock': 'INFY',
         'long_symbol': 'INFY26SEP1500CE', 'short_symbol': 'INFY26SEP1600CE'}
    r.update(extra)
    return r


def test_two_different_stocks_on_one_id_are_reported():
    """THE DEFECT. Pre-fix the higher version simply won and a real trade
    vanished from the book with no alert."""
    winner, note = sc.resolve_merge(
        _t(stock='INFY'), _t(stock='TCS', version=9,
                             long_symbol='TCS26SEP3000CE',
                             short_symbol='TCS26SEP3200CE'), Z)
    assert winner['stock'] == 'INFY', 'base must be kept, not out-versioned'
    assert note and 'DIFFERENT TRADES' in note
    assert 'Re-id one of them' in note, 'the note must say what to do'


def test_the_same_stock_with_different_LEGS_is_a_collision_too():
    """Same underlying, different spread. Version resolution would have picked
    one silently."""
    _, note = sc.resolve_merge(
        _t(), _t(version=9, long_symbol='INFY26SEP1700CE'), Z)
    assert note and 'DIFFERENT TRADES' in note


def test_an_ordinary_version_bump_is_still_silent():
    """The negative control, and the one that matters most: every normal
    update shares an id by design."""
    winner, note = sc.resolve_merge(_t(version=2), _t(version=5), Z)
    assert winner['version'] == 5 and note is None


def test_a_field_ABSENT_on_one_side_is_not_a_collision():
    """"Cannot tell" must not read as "different". An older record missing
    `entry_date` is not a collision with its own newer copy."""
    winner, note = sc.resolve_merge(
        _t(version=2), _t(version=5, entry_date='2026-08-14'), Z)
    assert winner['version'] == 5 and note is None


def test_identity_ignores_fields_that_MOVE():
    """Status, version and fills change over a record's life; comparing them
    would call every ordinary update a different trade."""
    winner, note = sc.resolve_merge(
        _t(version=2, status='entered'),
        _t(version=5, status='exited', exit_debit=12.0), Z)
    assert winner['status'] == 'exited' and note is None


def test_the_fallen_hero_legs_are_checked_too():
    """One function serves all four books, so identity spans both leg
    vocabularies."""
    a = {'id': 3, 'status': 'open', 'version': 1, 'stock': 'X',
         'long_put_symbol': 'X26SEP100PE'}
    b = {'id': 3, 'status': 'open', 'version': 8, 'stock': 'X',
         'long_put_symbol': 'X26SEP200PE'}
    _, note = sc.resolve_merge(a, b, sc.BCS_FAMILY_STATUSES)
    assert note and 'DIFFERENT TRADES' in note


def test_a_collision_is_reported_before_anything_else_resolves_it():
    """The check runs FIRST, so a collision cannot be masked by the settled or
    frozen rules deciding the record on other grounds."""
    _, note = sc.resolve_merge(
        _t(status='exited', version=7, stock='INFY'),
        _t(status='entered', version=9, stock='TCS',
           long_symbol='TCS26SEP3000CE'), Z)
    assert note and 'DIFFERENT TRADES' in note
