"""Four more store-layer defects, found 2026-08-31 in the second review pass.

1. **A FAILED SAVE COMMITTED ANYWAY.** `_mutate`'s rollback covered only
   exceptions from the caller's block. If `_save_local` itself raised -- disk
   full, or a Windows `os.replace` losing to a stray unlocked reader -- the
   caller got the exception and believed the write had FAILED, while the cache
   kept the mutation at version N+1. The next `_mutate` on any OTHER trade then
   refreshed disk(vN) against cache(vN+1), the cache won, and the "failed"
   write silently committed. Concretely: `begin_close` raises, a human is
   escalated to believing no close-lock exists, and `closing` materialises
   minutes later with nobody driving the close.

2. **THE ID HIGH-WATER MARK IGNORED IDS IT MERELY SAW.** `ZebraStore` does not
   inherit `LockedStoreMixin` and never got `_note_ids_seen`, so its mark
   advanced only on ids it ALLOCATED. The sidecar is created at runtime and is
   not in git, so a fresh box that syncs the book down and then quarantines
   before its first allocation reissues id 1 -- and `_merge` keys on id, so
   two distinct trades become one and the higher version erases the other.

3. **A MILD MARKER ERASED A SEVERE ONE.** The corruption marker is a single
   last-writer-wins slot, so a routine MERGE_CONFLICT overwrote an un-alerted
   QUARANTINE -- replacing "the book went empty" with "two writers disagreed"
   and dropping the `.corrupt.*.json` path, which after a quarantine is the
   only surviving copy. The exact INVERSE of the 2026-08-31 false-"EMPTY"
   incident, where a mild event wore the severe one's words.

4. **THE DRIVE COPY WAS NEVER VALIDATED.** `partition_readable` guarded the
   local read; the remote one -- the input an outside writer can actually
   poison -- got nothing. One record with a missing `id` made the merge raise
   into the blanket handler, which falls back to local EVERY sync, while local
   writes kept replacing the Drive file wholesale with a book that never
   absorbed it.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest common/tests/test_store_hardenings_round2.py -v
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common import store_contract as sc                          # noqa: E402

NO_DRIVE = {'google_drive': {'enabled': False}}


@pytest.fixture
def bcs_store(tmp_path, monkeypatch):
    m = importlib.import_module('bcs.trade_store')
    monkeypatch.setattr(m, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(m, 'LOCAL_TRADES_FILE', tmp_path / 'bcs_trades.json')
    monkeypatch.setattr(m, 'LOCK_FILE', tmp_path / 'bcs_trades.lock')
    return m.TradeStore(config=dict(NO_DRIVE))


@pytest.fixture
def zebra_store(tmp_path, monkeypatch):
    from zebra import config as zcfg
    from zebra import trade_store as zts
    monkeypatch.setattr(zcfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(zcfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(zcfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    return zts.ZebraStore(config={})


# ── 1. a failed save must not commit later ─────────────────────────────────

def test_a_failed_save_rolls_the_cache_back_bcs(bcs_store, monkeypatch):
    """THE DEFECT. Pre-fix the mutation stayed in the cache at v+1 and the
    next unrelated write persisted it."""
    bcs_store._trades = [{'id': 1, 'status': 'open', 'version': 1}]
    before = json.dumps(bcs_store._trades, sort_keys=True)

    monkeypatch.setattr(type(bcs_store), '_save_local',
                        lambda self: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        with bcs_store._mutate(drive=False):
            bcs_store._trades[0]['status'] = 'closing'
            bcs_store._trades[0]['version'] = 2

    assert json.dumps(bcs_store._trades, sort_keys=True) == before, (
        'a write the caller was told FAILED is still live in the cache and '
        'will commit on the next unrelated mutation')


def test_a_failed_save_rolls_the_cache_back_zebra(zebra_store, monkeypatch):
    zebra_store._trades = [{'id': 1, 'status': 'entered', 'version': 1}]
    before = json.dumps(zebra_store._trades, sort_keys=True)

    monkeypatch.setattr(type(zebra_store), '_save_local',
                        lambda self: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        with zebra_store._mutate(drive=False):
            zebra_store._trades[0]['status'] = 'exited'

    assert json.dumps(zebra_store._trades, sort_keys=True) == before


def test_a_SUCCESSFUL_save_still_commits(bcs_store):
    """The negative control: the rollback must not undo a good write."""
    bcs_store._trades = [{'id': 1, 'status': 'open', 'version': 1}]
    with bcs_store._mutate(drive=False):
        bcs_store._trades[0]['status'] = 'closing'
    assert bcs_store._trades[0]['status'] == 'closing'


# ── 2. the id mark covers ids merely seen ──────────────────────────────────

def test_zebra_raises_the_id_mark_on_a_book_it_only_read(zebra_store):
    """A fresh box with no sidecar syncs ids 1..7 down from Drive. Pre-fix the
    mark stayed 0, so a quarantine before the first allocation reissued 1."""
    zebra_store._trades = [{'id': i, 'status': 'entered', 'version': 1}
                           for i in range(1, 8)]
    assert zebra_store._read_high_water() == 0
    with zebra_store._mutate(drive=False):
        zebra_store._trades[0]['note'] = 'touched'
    assert zebra_store._read_high_water() == 7, (
        'the mark ignored ids this process did not allocate; a quarantine '
        'would reissue them')


def test_the_reissue_cannot_happen_after_the_mark_is_set(zebra_store):
    """The consequence, stated directly: after a quarantine empties the book
    the allocator must not hand out 1 again."""
    zebra_store._trades = [{'id': i, 'status': 'entered', 'version': 1}
                           for i in range(1, 8)]
    with zebra_store._mutate(drive=False):
        zebra_store._trades[0]['note'] = 'touched'
    zebra_store._trades = []                      # the quarantine
    with zebra_store._mutate(drive=False):
        assert zebra_store._next_id() == 8


# ── 3. a merge conflict must not erase an un-alerted quarantine ────────────

def test_a_merge_conflict_does_not_overwrite_a_pending_quarantine(bcs_store):
    """The mild event must not replace the severe one.

    RETIRES WHEN: the corruption marker is exposed through a store
    accessor instead of being read off disk here, so these tests stop
    depending on its on-disk shape.
    """
    bcs_store._flag_corruption('the book failed to parse',
                               backup='bcs_trades.corrupt.1.json',
                               kind=sc.MARKER_QUARANTINE)
    bcs_store._flag_corruption('two writers disagreed', backup=None,
                               kind=sc.MARKER_MERGE_CONFLICT)

    held = json.loads(bcs_store._corrupt_marker_path().read_text('utf-8'))
    assert held['kind'] == sc.MARKER_QUARANTINE, (
        'the mild event replaced the severe one')
    assert 'corrupt.1.json' in str(held['backup']), (
        'the only surviving copy of the book is no longer named')


def test_a_marker_with_NO_kind_is_treated_as_a_quarantine(bcs_store):
    """Every marker written before the field existed was a quarantine, and
    the alerting layers already read a missing kind that way.

    RETIRES WHEN: the corruption marker is exposed through a store
    accessor instead of being read off disk here, so these tests stop
    depending on its on-disk shape.
    """
    p = bcs_store._corrupt_marker_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({'at': '2026-08-31T09:30:58',
                             'error': 'old-style marker'}), encoding='utf-8')
    bcs_store._flag_corruption('two writers disagreed', backup=None,
                               kind=sc.MARKER_MERGE_CONFLICT)
    assert 'old-style marker' in p.read_text('utf-8')


def test_an_ALERTED_quarantine_may_be_replaced(bcs_store):
    """The operator has already been told, so the slot is free again --
    otherwise one quarantine would suppress every later conflict for good.

    RETIRES WHEN: the corruption marker is exposed through a store
    accessor instead of being read off disk here, so these tests stop
    depending on its on-disk shape.
    """
    bcs_store._flag_corruption('parsed nothing', backup='b.json',
                               kind=sc.MARKER_QUARANTINE)
    p = bcs_store._corrupt_marker_path()
    d = json.loads(p.read_text('utf-8'))
    d['alerted_at'] = '2026-08-31T09:31:00'
    p.write_text(json.dumps(d), encoding='utf-8')

    bcs_store._flag_corruption('two writers disagreed', backup=None,
                               kind=sc.MARKER_MERGE_CONFLICT)
    assert json.loads(p.read_text('utf-8'))['kind'] == sc.MARKER_MERGE_CONFLICT


def test_a_quarantine_may_always_overwrite_a_conflict(bcs_store):
    """Severity only ever goes up without being read.

    RETIRES WHEN: the corruption marker is exposed through a store
    accessor instead of being read off disk here, so these tests stop
    depending on its on-disk shape.
    """
    bcs_store._flag_corruption('two writers disagreed', backup=None,
                               kind=sc.MARKER_MERGE_CONFLICT)
    bcs_store._flag_corruption('the book failed to parse', backup='b.json',
                               kind=sc.MARKER_QUARANTINE)
    assert json.loads(
        bcs_store._corrupt_marker_path().read_text('utf-8')
    )['kind'] == sc.MARKER_QUARANTINE


# ── 4. the Drive copy is validated like the local one ──────────────────────

def test_an_unreadable_drive_record_is_held_out_not_fatal(bcs_store,
                                                          monkeypatch):
    """Pre-fix `_merge_trades` raised on `t['id']` and the whole sync fell
    back to local, every time, forever."""
    bcs_store._trades = [{'id': 1, 'status': 'open', 'version': 1}]
    bcs_store._drive_file_id = 'x'
    bcs_store._drive_service = object()
    flagged = []
    bcs_store._flag_corruption = lambda err, backup, **kw: flagged.append(err)

    mod = bcs_store._mod()
    monkeypatch.setattr(mod.drive_store, 'download_json',
                        lambda *a, **k: [{'id': 2, 'status': 'open',
                                          'version': 1},
                                         {'no_id': True}])
    monkeypatch.setattr(type(bcs_store), '_upload_to_drive', lambda self: None)

    bcs_store._sync_from_drive()

    ids = sorted(t['id'] for t in bcs_store._trades)
    assert ids == [1, 2], (
        'the good remote record did not land; the sync fell back to local')
    assert flagged, 'the unreadable remote record was dropped silently'
    assert 'unreadable' in flagged[0]


def test_a_clean_drive_copy_raises_nothing(bcs_store, monkeypatch):
    """Negative control: validation must not alarm on a healthy book."""
    bcs_store._trades = [{'id': 1, 'status': 'open', 'version': 1}]
    bcs_store._drive_file_id = 'x'
    bcs_store._drive_service = object()
    flagged = []
    bcs_store._flag_corruption = lambda err, backup, **kw: flagged.append(err)

    mod = bcs_store._mod()
    monkeypatch.setattr(mod.drive_store, 'download_json',
                        lambda *a, **k: [{'id': 1, 'status': 'open',
                                          'version': 1}])
    monkeypatch.setattr(type(bcs_store), '_upload_to_drive', lambda self: None)

    bcs_store._sync_from_drive()
    assert flagged == []

