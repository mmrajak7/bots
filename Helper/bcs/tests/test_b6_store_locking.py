"""B6 — the money stores lose writes, silently, on every poll.

`self._trades` is a cache and every writer rewrites the whole file from it. So
any two writers racing produce a lost update with no exception, no corrupt
file and no log line:

    monitor  loads  [A,B]
    CLI      loads  [A,B]
    CLI      writes [A,B,C]        <- `add_trade` for the position just entered
    monitor  writes [A,B]          <- rewrites its stale cache; C is GONE

The monitor is not an occasional writer. `maybe_sync` -> `_sync_from_drive` ->
`_save_local` fires every poll on all three books, so the window is open all
session.

Everything here runs against ALL THREE stores. B11 and B10 were each written
for `bcs` first and each shipped an untested `fallen_hero` twin that the
mutation run caught later; the three stores are copies of one another, so a
fix applied to one and not the others is the default failure mode, not an
unlikely one.

Run:  cd Helper && python -m pytest bcs/tests/test_b6_store_locking.py -v
"""
import importlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common.filelock import LockTimeout, exclusive          # noqa: E402
from common.locked_store import LockedStoreMixin            # noqa: E402

from bcs.tests.conftest import BOOKS, BOOK_IDS, NO_DRIVE, seed_trades  # noqa: E402

IDS = BOOK_IDS
_seed = seed_trades


# ── The lost update ──────────────────────────────────────────────────────────

def test_a_write_from_another_process_is_not_erased(book):
    """The whole bug, in one assertion."""
    store = book.make(seed_ids=(1, 2))

    # Another process adds trade 3 while this store's cache still says [1,2].
    book.data.write_text(json.dumps(_seed((1, 2, 3))), encoding='utf-8')

    store.update_trade_fields(1, note='mine')

    ids = {t['id'] for t in book.read()}
    assert ids == {1, 2, 3}, (
        f"trade 3 was erased by a stale-cache rewrite (disk now holds {ids})")


def test_negative_control_without_the_refresh_the_trade_is_erased(book, monkeypatch):
    """Proves the test above is testing the refresh and not something else.

    `_read_local` returning the cache is exactly the old code: lock held, but
    nothing re-read, so the write goes out on top of a stale view.
    """
    store = book.make(seed_ids=(1, 2))
    book.data.write_text(json.dumps(_seed((1, 2, 3))), encoding='utf-8')

    monkeypatch.setattr(book.cls, '_read_local', lambda self: self._trades)
    store.update_trade_fields(1, note='mine')

    assert {t['id'] for t in book.read()} == {1, 2}, (
        "the negative control did not reproduce the bug, so the positive test "
        "above proves nothing")


def test_the_local_change_still_lands(book):
    """The refresh must not throw away what the caller came to write."""
    store = book.make(seed_ids=(1, 2))
    book.data.write_text(json.dumps(_seed((1, 2, 3))), encoding='utf-8')

    store.update_trade_fields(1, note='mine')

    got = {t['id']: t for t in book.read()}
    assert got[1].get('note') == 'mine'


def test_a_newer_version_on_disk_wins_the_refresh(book):
    """Another process closed the trade; this one must not resurrect it."""
    store = book.make(seed_ids=(1, 2))
    disk = _seed((1, 2))
    disk[0].update(status='closed', version=9)
    book.data.write_text(json.dumps(disk), encoding='utf-8')

    store.update_trade_fields(2, note='mine')

    got = {t['id']: t for t in book.read()}
    assert got[1]['status'] == 'closed', (
        "a stale cache reverted a close booked by the other process")


def test_add_trade_allocates_its_id_from_disk_not_the_cache(book):
    """Two processes racing `next_trade_id()` hand out the same id, after
    which every lookup by id is ambiguous and the monitor can close the
    wrong position."""
    store = book.make(seed_ids=(1, 2))
    book.data.write_text(json.dumps(_seed((1, 2, 3))), encoding='utf-8')

    added = store.add_trade(book.payload)

    assert added['id'] == 4, (
        f"id {added['id']} was allocated off the stale cache; trade 3 already "
        f"holds it")
    assert {t['id'] for t in book.read()} == {1, 2, 3, 4}


# ── The lock itself ──────────────────────────────────────────────────────────

def test_a_held_lock_blocks_the_writer(book):
    """If the lock were not taken this write would sail straight through."""
    store = book.make()
    with exclusive(book.lock):
        with pytest.raises(LockTimeout):
            store.update_trade_fields(1, note='mine')

    assert all('note' not in t for t in book.read()), (
        "a write landed while another holder had the lock")


def test_the_lock_is_released_after_the_block(book):
    store = book.make()
    store.update_trade_fields(1, note='mine')
    with exclusive(book.lock, timeout=1.0):      # must not raise
        pass


def test_each_book_locks_its_own_file(book):
    """One global lock would serialise three unrelated books against each
    other on every poll, on a Pi that is also running the live monitor."""
    store = book.make()
    assert store._lock_path().name == f'{book.stem}.lock'
    assert store._lock_path().parent == book.tmp

    others = [s for m, c, s, p in BOOKS if s != book.stem]
    with exclusive(book.tmp / f'{others[0]}.lock'):
        store.update_trade_fields(1, note='mine')       # must NOT block
    assert book.read()[0].get('note') == 'mine'


def test_the_drive_upload_happens_outside_the_lock(book, monkeypatch):
    """Holding a mutex across an HTTP round-trip stalls the other process's
    entire cycle. The upload must run with the lock already released."""
    seen = {}

    def fake_upload(self):
        try:
            with exclusive(self._lock_path(), timeout=0.2):
                seen['free'] = True
        except LockTimeout:
            seen['free'] = False

    monkeypatch.setattr(book.cls, '_upload_to_drive', fake_upload)
    store = book.make()
    store.set_trade_status(1, 'closed')

    assert seen.get('free') is True, (
        "the Drive upload ran while the store lock was still held")


def test_nesting_fails_fast_instead_of_deadlocking(book):
    """flock on a second fd conflicts even within one process, so a nested
    _mutate would stall the monitor for the whole timeout and then raise
    something that reads like contention rather than like a bug."""
    store = book.make()
    with pytest.raises(RuntimeError) as ei:
        with store._mutate():
            with store._mutate():
                pass
    assert 'nest' in str(ei.value).lower()
    assert not isinstance(ei.value, LockTimeout)


def test_the_reentry_flag_clears_after_a_failed_block(book):
    store = book.make()
    with pytest.raises(ValueError):
        with store._mutate():
            raise ValueError('boom')
    store.update_trade_fields(1, note='mine')            # must not raise
    assert book.read()[0].get('note') == 'mine'


# ── Atomicity of the block ───────────────────────────────────────────────────

def test_a_raise_inside_the_block_leaves_disk_untouched(book):
    store = book.make()
    before = book.data.read_bytes()

    with pytest.raises(ValueError):
        with store._mutate():
            store._trades[0]['status'] = 'closing'
            raise ValueError('half-way')

    assert book.data.read_bytes() == before


def test_a_raise_inside_the_block_rolls_the_cache_back(book):
    """A half-mutated trade left in the cache is worse than the exception:
    get_open_trades() would report a state that was never persisted, and the
    monitor acts on what get_open_trades() says."""
    store = book.make()
    with pytest.raises(ValueError):
        with store._mutate():
            store._trades[0]['status'] = 'closing'
            raise ValueError('half-way')

    assert store._trades[0]['status'] == 'open'


def test_a_no_op_write_does_not_rewrite_the_file(book):
    """`update_trade_fields` runs for every open position every poll to carry
    trailing-SL state, and most polls move nothing."""
    store = book.make()
    store.update_trade_fields(1, note='mine')
    stamp = book.data.stat().st_mtime_ns
    body = book.data.read_bytes()

    store.update_trade_fields(1, note='mine')            # same value again

    assert book.data.read_bytes() == body
    assert book.data.stat().st_mtime_ns == stamp, "the file was rewritten"


def test_a_mutation_that_changes_nothing_neither_saves_nor_uploads(book, monkeypatch):
    """Tested on the block, not through a writer.

    The obvious version of this test — call `set_trade_status(1, 'closed')`
    twice and expect one write — is wrong, and its failure is what sent me
    looking. `set_trade_status` bumps `version` on EVERY call by design,
    because the version is how `_merge_trades` decides a cross-machine
    conflict. So it is never a no-op, and asserting that it is would have
    encoded a bug as a requirement.
    """
    calls = []
    monkeypatch.setattr(book.cls, '_upload_to_drive',
                        lambda self: calls.append(1))
    store = book.make()
    calls.clear()                    # FH's migration backfills downside_risk
    body = book.data.read_bytes()
    stamp = book.data.stat().st_mtime_ns

    with store._mutate():
        pass

    assert calls == [], "an empty mutation uploaded to Drive"
    assert book.data.read_bytes() == body
    assert book.data.stat().st_mtime_ns == stamp, "the file was rewritten"


def test_a_real_change_does_reach_drive(book, monkeypatch):
    """Negative control for the test above: the skip must be conditional."""
    calls = []
    monkeypatch.setattr(book.cls, '_upload_to_drive',
                        lambda self: calls.append(1))
    store = book.make()
    calls.clear()
    store.set_trade_status(1, 'closed')
    assert calls == [1]


def test_update_trade_fields_never_uploads(book, monkeypatch):
    """It is the every-poll writer; Drive picks it up on the sync cycle."""
    calls = []
    monkeypatch.setattr(book.cls, '_upload_to_drive',
                        lambda self: calls.append(1))
    store = book.make()
    calls.clear()                    # ignore the migration write on FH
    store.update_trade_fields(1, note='mine')
    assert calls == []


# ── Two real processes ───────────────────────────────────────────────────────

CHILD = textwrap.dedent('''
    import sys, json
    sys.path.insert(0, {helper!r})
    from pathlib import Path
    import {mod} as m
    tmp = Path({tmp!r})
    m.LOG_DIR = tmp
    m.LOCAL_TRADES_FILE = tmp / {data!r}
    m.LOCK_FILE = tmp / {lock!r}
    s = m.{cls}(config={{'google_drive': {{'enabled': False}}}})
    s.initialize()
    for i in range({n}):
        s.update_trade_fields({tid}, tick=i)
''')


def test_two_processes_writing_the_same_book_lose_nothing(book):
    """The scenario the interim rule ('never run a bcs CLI while the monitor
    is up') exists to avoid. It should stop being a rule."""
    store = book.make(seed_ids=(1, 2))
    del store

    modname, clsname = book.mod.__name__, book.cls.__name__
    procs = [
        subprocess.Popen(
            [sys.executable, '-c', CHILD.format(
                helper=str(HELPER), mod=modname, cls=clsname,
                tmp=str(book.tmp), data=book.data.name, lock=book.lock.name,
                n=25, tid=tid)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for tid in (1, 2)
    ]
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode('utf-8', 'replace')[-2000:]

    got = {t['id']: t for t in book.read()}
    assert set(got) == {1, 2}, f"a trade vanished: {sorted(got)}"
    assert got[1].get('tick') == 24, "process A's last write was lost"
    assert got[2].get('tick') == 24, "process B's last write was lost"


# ── The invariant that keeps a fourth copy honest ────────────────────────────

STORE_FILES = ['bcs/trade_store.py', 'fallen_hero/trade_store.py',
               'bear_put/trade_store.py']


@pytest.mark.parametrize('rel', STORE_FILES)
def test_no_store_persists_outside_the_mutate_block(rel):
    """Advisory locking is defeated by one unlocked writer, and this codebase
    grows stores by copy-paste. Every `_save_local()` now lives in the mixin,
    so its absence here is a mechanical check that no writer escaped.
    """
    src = (HELPER / rel).read_text(encoding='utf-8')
    assert 'self._save_local()' not in src, (
        f"{rel} persists outside _mutate(); route it through the mixin")
    assert src.count('self._upload_to_drive()') == 1, (
        f"{rel} should call _upload_to_drive exactly once — in "
        f"_sync_from_drive, deliberately outside the lock. The mixin does the "
        f"rest.")


@pytest.mark.parametrize('modname,clsname,stem,payload', BOOKS, ids=IDS)
def test_every_store_is_locked_and_names_its_own_file(modname, clsname, stem, payload):
    cls = getattr(importlib.import_module(modname), clsname)
    assert issubclass(cls, LockedStoreMixin), f"{clsname} is not locked at all"
    assert '_lock_path' in cls.__dict__, (
        f"{clsname} inherits the mixin's raising _lock_path — it would refuse "
        f"every write")
    assert cls(config=dict(NO_DRIVE))._lock_path().name == f'{stem}.lock'
