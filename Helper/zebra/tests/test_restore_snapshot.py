"""Restoring an archived store has to WIN the next Drive merge.

2026-08-30: `deploy_server.sh` step 3 (`zebra reset --confirm`, a one-time
hygiene step from the first deployment) was re-run on a live book and
force-closed all six open cohort positions at -100% under
`reset_force_close`, plus cancelled three signals. Paper records, so no money —
but it is the evidence the arming gate is waiting on.

The reset archives first, so the good state exists. Restoring it is not a file
copy: `ZebraStore._merge` resolves by VERSION and higher wins, the reset
incremented every version it touched and pushed them to Drive, so a restored
file carrying the OLD versions loses the next sync and the reset comes back
silently.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_restore_snapshot.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import restore_snapshot as rs      # noqa: E402


def snap_rec(tid, stock, status='entered', version=14, **over):
    r = {'id': tid, 'stock': stock, 'status': status, 'version': version,
         'debit': 10.0, 'lot_size': 100, 'cohort': '2026-08-14'}
    r.update(over)
    return r


def reset_rec(tid, stock, version=15):
    """What the reset left behind."""
    return snap_rec(tid, stock, status='exited', version=version,
                    exit_reason='reset_force_close', pnl_pct=-100.0)


# -- the version rule, which is the whole point -----------------------------

def test_the_restore_outranks_what_it_replaces():
    """THE defect this tool exists for. A plain file copy carries the OLD
    version, `_merge` keeps the higher one, and the reset returns on the next
    Drive sync — silently, because a merge is not an error."""
    snapshot = [snap_rec(423, 'TMPV', version=14)]
    live = [reset_rec(423, 'TMPV', version=15)]
    out = rs.rebuild(snapshot, live)
    assert out[0]['version'] == 16
    assert out[0]['status'] == 'entered'
    assert 'exit_reason' not in out[0]


def test_it_beats_the_merge_for_real():
    """Driven through `ZebraStore._merge` itself rather than asserting on the
    number, because the number only matters through that function."""
    from zebra.trade_store import ZebraStore
    snapshot = [snap_rec(423, 'TMPV', version=14)]
    live = [reset_rec(423, 'TMPV', version=15)]
    restored = rs.rebuild(snapshot, live)
    # base = what we wrote locally, incoming = what Drive still holds.
    merged = ZebraStore._merge(restored, live)
    assert merged[0]['status'] == 'entered', (
        'the reset won the merge — the restore did not out-version it')


def test_a_naive_copy_would_LOSE_that_merge():
    """The negative control. Without it the test above passes just as well
    against a tool that does nothing at all."""
    from zebra.trade_store import ZebraStore
    snapshot = [snap_rec(423, 'TMPV', version=14)]
    live = [reset_rec(423, 'TMPV', version=15)]
    merged = ZebraStore._merge(snapshot, live)      # the naive restore
    assert merged[0]['status'] == 'exited'


def test_it_takes_the_max_of_both_versions():
    """The snapshot can be AHEAD of the live store — a record written after
    the snapshot and then rolled back another way. Bumping only the live
    version would then move backwards."""
    out = rs.rebuild([snap_rec(1, 'X', version=99)],
                     [reset_rec(1, 'X', version=15)])
    assert out[0]['version'] == 100


# -- what it must not do ----------------------------------------------------

def test_a_record_that_exists_only_LIVE_is_left_alone():
    """A restore undoes a known incident; it does not roll back everything
    that happened afterwards. A trade entered after the snapshot must survive."""
    out = rs.rebuild([snap_rec(1, 'OLD')],
                     [reset_rec(1, 'OLD'), snap_rec(2, 'NEWER')])
    assert {r['id'] for r in out} == {1, 2}
    assert next(r for r in out if r['id'] == 2)['stock'] == 'NEWER'


def test_it_stamps_when_it_was_restored():
    """A record that was rolled back must say so. Otherwise the store carries
    a state nothing explains, which is how a later reader concludes the reset
    never happened."""
    out = rs.rebuild([snap_rec(1, 'X')], [reset_rec(1, 'X')])
    assert out[0]['restored_from_snapshot_at']


def test_the_plan_names_every_record_it_would_change():
    """Dry run is the default, so the plan is what the operator decides on."""
    p = rs.plan([snap_rec(1, 'TMPV'), snap_rec(2, 'COALINDIA')],
                [reset_rec(1, 'TMPV'), snap_rec(2, 'COALINDIA')])
    assert len(p['changes']) == 1
    assert p['changes'][0]['stock'] == 'TMPV'
    assert p['changes'][0]['reason'] == 'reset_force_close'
    assert p['unchanged'] == 1


def test_an_identical_store_is_a_no_op():
    p = rs.plan([snap_rec(1, 'X')], [snap_rec(1, 'X')])
    assert p['changes'] == []


# -- the reason the gate was not corrupted ----------------------------------

def test_reset_force_close_is_not_counted_as_a_stop():
    """The arming gate waits on a transacted STOP exit. Six records booked at
    -100% could have read as exactly that. They do not: `reset_force_close` is
    outside `zebra.outcomes`' vocabulary, so it scores as nothing at all.

    That is the allowlist working. Pinned because if somebody ever "helpfully"
    adds this reason to the map, six paper resets would clear the gate that
    guards real money.
    """
    from zebra import outcomes
    c = outcomes.classify('reset_force_close')
    assert c['known'] is False
    assert c['kind'] is None
    assert 'reset_force_close' not in outcomes.STOP_KINDS
