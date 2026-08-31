"""A sibling process writing mid-cycle must not raise a corruption alert.

Drives the REAL `ZebraStore` against real files, deliberately. The repo's own
arming order says a store-bridge fix needs "a regression test that drives the
REAL `ZebraStore`, not the test `MemoryStore`" — and this defect lives entirely
in the interaction between `_mutate`'s disk refresh, the on-disk file, and the
marker written beside it. A fake store has none of those three.

THE SCENARIO, reproduced exactly as it happened on the Pi on 2026-08-31:

  1. The zebra cron loads the book and holds `self._trades` as a cache for the
     18-46 seconds a cycle takes.
  2. A sibling process — one of the Claude vet / review / postmortem CLIs the
     cycle itself spawned, `_mutate`'s docstring names them as by-design —
     writes a verdict to the same record and bumps its version.
  3. The cron's next `_mutate` refreshes from disk and finds its cache and the
     disk at the SAME version with different content.

Pre-fix that produced a CRITICAL log line and a corruption marker, which the
monitor rendered as "the trade file failed to parse and was quarantined... the
store may have restarted EMPTY... exit monitoring is off on every open
position". The book was intact and all seven positions were being polled in
the same log. Eleven such lines and four such alerts in one morning.

Run:  cd Helper && python -m pytest \
        zebra/tests/test_sibling_writer_is_not_a_split_brain.py -v
"""
import json
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common import store_contract as sc                          # noqa: E402
from zebra import config as cfg                                  # noqa: E402
from zebra.trade_store import ZebraStore                          # noqa: E402


SIGNAL = {'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
          'st_value': 100.0, 'st_direction': 'UP', 'signal_price': 96.0,
          'signal_gap_pct': 4.0, 'paper': True, 'notes': 'fixture'}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    s = ZebraStore(config={})
    s._load_local()
    s.add_signal(dict(SIGNAL))
    return s


def marker_path(tmp_path):
    return tmp_path / 'zebra_store_corrupt.json'


def sibling_writes(tmp_path, tid, **fields):
    """A second process edits one record on disk, bumping its version.

    Written through the file rather than through the store object on purpose:
    the whole point is that this edit happens somewhere our cache cannot see.
    """
    path = cfg.LOCAL_FILE
    book = json.loads(path.read_text())
    for t in book:
        if t['id'] == tid:
            t.update(fields)
            t['version'] = t.get('version', 1) + 1
    path.write_text(json.dumps(book, indent=2, default=str))


# -- the defect -------------------------------------------------------------

def test_a_sibling_write_at_a_tied_version_raises_no_marker(store, tmp_path):
    """THE DEFECT. Pre-fix this wrote a corruption marker every cycle.

    RETIRES WHEN: the corruption marker stops being a file the store writes and
    the monitor reads, and becomes an API the two share — at which point this
    asserts on the return value instead of on `zebra_store_corrupt.json`.
    """
    tid = store.load_trades()[0]['id']

    # Our cache advances to version 2 by our own write...
    with store._mutate():
        store.find(tid)['notes'] = 'ours'
    # ...and the sibling independently produces its own version 2 on disk.
    book = json.loads(cfg.LOCAL_FILE.read_text())
    for t in book:
        if t['id'] == tid:
            t['notes'] = 'theirs'
    cfg.LOCAL_FILE.write_text(json.dumps(book, indent=2, default=str))

    with store._mutate():
        pass

    assert not marker_path(tmp_path).exists(), (
        'a sibling writer mid-cycle raised the alarm that means the book was '
        'quarantined and the stops are dead')


def test_the_book_survives_the_tie_intact(store, tmp_path):
    """The alert was false; the data was never in question. Pin that too, so a
    future 'fix' cannot quieten the alert by dropping records."""
    tid = store.load_trades()[0]['id']
    sibling_writes(tmp_path, tid, notes='theirs')
    with store._mutate():
        pass
    book = store.load_trades()
    assert len(book) == 1 and book[0]['id'] == tid


def test_the_sibling_write_is_absorbed_not_discarded(store, tmp_path):
    """A higher version from disk must still win — that is what the refresh is
    FOR, and silencing the tie must not have silenced the refresh."""
    tid = store.load_trades()[0]['id']
    sibling_writes(tmp_path, tid, notes='from the vet')
    with store._mutate():
        pass
    assert store.find(tid)['notes'] == 'from the vet'


# -- the guard must still fire where it means something ---------------------

def test_a_genuine_drive_side_tie_still_raises_the_marker(store, tmp_path):
    """Negative control: the REPLICA comparison is untouched.

    `_merge_announced` without `same_replica` is the Drive arm of
    `_sync_from_drive`. That one is a real split brain and must stay loud.
    """
    local = store.load_trades()
    other = [dict(local[0], notes='the other replica')]
    store._merge_announced(local, other)
    assert marker_path(tmp_path).exists()


def test_the_marker_is_flagged_as_a_conflict_not_a_quarantine(store, tmp_path):
    """The kind is what stops the monitor reading out the quarantine text.

    RETIRES WHEN: the marker becomes a shared API rather than a JSON file on
    disk, so the kind can be asserted without parsing the file.
    """
    local = store.load_trades()
    store._merge_announced(local, [dict(local[0], notes='other')])
    info = json.loads(marker_path(tmp_path).read_text())
    assert info['kind'] == sc.MARKER_MERGE_CONFLICT


def test_the_marker_names_the_field_under_dispute(store, tmp_path):
    """Diagnosing the first real one meant downloading Drive by hand.

    RETIRES WHEN: the marker becomes a shared API rather than a JSON file on
    disk, so the note can be asserted without parsing the file.
    """
    local = store.load_trades()
    store._merge_announced(local, [dict(local[0], notes='other')])
    info = json.loads(marker_path(tmp_path).read_text())
    assert 'notes' in info['error']


def test_a_reopened_exit_is_announced_even_on_the_refresh(store, tmp_path):
    """`same_replica` suppresses exactly ONE branch.

    A booked exit outrun by a version counter is the case that costs money, and
    it must stay loud on every path — the refresh included.

    On this path `base` is DISK and `incoming` is our cache, so the stale side
    has to be the cache: the exit is booked on disk while this process still
    holds an older `entered` copy whose counter has run ahead. That is the
    shape the guard exists for, and version alone would reopen the trade.
    """
    tid = store.load_trades()[0]['id']
    with store._mutate():
        store.find(tid)['status'] = 'exited'
        store.find(tid)['exit_reason'] = 'paper:tp'

    # A stale cache that has out-counted the booked exit.
    store._trades = [dict(store._trades[0], status='entered', version=99)]

    with store._mutate():
        pass

    assert marker_path(tmp_path).exists(), (
        'a stale copy reopening a closed trade must never be silent')
    assert store.find(tid)['status'] == 'exited', (
        'and the exit must survive — announcing it is not enough')


# -- the batched poll write, against Drive ----------------------------------
#
# FOUND IN PRODUCTION on the Pi after the first fix deployed. The alert text
# was now right and it was still arriving every cycle:
#
#   record #423 is at version 17 on BOTH replicas with DIFFERENT content
#   (differs on: corrob_spot, corrob_t, corrob_value, exit_depth)
#
# `apply_mfe` writes exactly those fields with `drive=False` and NO version
# bump, on purpose. So local disk and Drive sit at an equal version with
# different content for every open position on every poll — the tie condition,
# reached by design rather than by divergence.

def test_the_batched_poll_write_does_not_look_like_a_split_brain(store,
                                                                 tmp_path):
    """THE SECOND DEFECT, reproduced through the real batched write path."""
    tid = store.load_trades()[0]['id']
    drive_copy = [dict(store.load_trades()[0])]      # Drive, before the poll

    store.apply_mfe({tid: {'corrob_spot': 101.5, 'corrob_value': 4.2,
                           'corrob_t': 1234.0, 'exit_depth': {'1': 3}}})

    store._merge_announced(store.load_trades(), drive_copy)

    assert not marker_path(tmp_path).exists(), (
        'the per-poll local-only write raised a split-brain alert once per '
        'open position per cycle')


def test_apply_mfe_still_leaves_the_version_alone(store, tmp_path):
    """Pins the PREMISE of the fix rather than only its effect.

    If `apply_mfe` ever starts bumping the version, the exemption above stops
    being needed and starts being a place a real conflict can hide.
    """
    tid = store.load_trades()[0]['id']
    before = store.find(tid).get('version', 0)
    store.apply_mfe({tid: {'corrob_spot': 1.0}})
    assert store.find(tid).get('version', 0) == before


def test_a_real_divergence_beside_the_poll_fields_still_alerts(store,
                                                              tmp_path):
    """Negative control: the exemption is for ties CONFINED to those fields."""
    tid = store.load_trades()[0]['id']
    drive_copy = [dict(store.load_trades()[0], notes='the other replica')]
    store.apply_mfe({tid: {'corrob_spot': 101.5}})
    store._merge_announced(store.load_trades(), drive_copy)
    assert marker_path(tmp_path).exists()
