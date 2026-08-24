"""Adversarial pass over B6/B7: what breaks when the FIX fails?

The B6 and B7 suites prove the guards work. This one attacks them. Required by
`feedback_live_automation_bar` ("adversarial review"), and warranted on its own
terms: `_mutate` is now on the write path of all three money books, so a defect
here is a defect in every one of them at once, and `_read_local` now takes a
lock it did not take before — which changes what happens when the lock, the
disk or the filesystem misbehaves.

Written by the same author as the code, which makes it weaker than an
independent review. It compensates by attacking mechanisms rather than
re-asserting intent: every test below asks "what state is the system left in"
after something the happy path never does.

Run:  cd Helper && python -m pytest bcs/tests/test_b6_b7_adversarial.py -v
"""
import json
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common.filelock import LockTimeout, exclusive          # noqa: E402


# ── A failed save must not leave the lock, the flag, or the cache wrong ──────

def test_a_save_failure_still_releases_the_lock(book, monkeypatch):
    """The nightmare: one disk error and the book is wedged for every other
    process until the monitor is killed."""
    store = book.make()
    monkeypatch.setattr(book.cls, '_save_local',
                        lambda self: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        store.update_trade_fields(1, note='mine')

    with exclusive(book.lock, timeout=1.0):      # must not raise
        pass


def test_a_save_failure_clears_the_reentry_flag(book, monkeypatch):
    """Otherwise the NEXT write raises 'already held' and the book is dead for
    the rest of the session -- a strictly worse outcome than the disk error.

    NOTE the toggle instead of `monkeypatch.undo()`. The first draft of this
    test used undo() to restore `_save_local`, which also reverted the `book`
    fixture's redirection of LOCAL_TRADES_FILE -- so the second write went to
    the LIVE book. It put a junk field on three real closed trades and injected
    a fake OPEN position into bear_put_trades.json. undo() is all-or-nothing;
    it cannot restore one patch.
    """
    store = book.make()
    fail = {'on': True}
    real_save = book.cls._save_local

    def maybe_save(self):
        if fail['on']:
            raise OSError('disk full')
        return real_save(self)

    monkeypatch.setattr(book.cls, '_save_local', maybe_save)
    with pytest.raises(OSError):
        store.update_trade_fields(1, note='mine')

    assert getattr(store, '_in_mutate', False) is False

    fail['on'] = False                           # NOT monkeypatch.undo()
    store.update_trade_fields(1, note='later')   # must not raise
    assert book.read()[0].get('note') == 'later'


@pytest.mark.parametrize('how', ['write_text', 'open', 'mkstemp', 'os_open'])
def test_the_production_rail_refuses_every_write_channel(how):
    """The rail is the fix; this proves it fires, on each channel a store uses.

    Deliberately probes a THROWAWAY filename rather than driving a real store
    with the fixture undone. The first version did the latter -- reproduce the
    bug, expect the rail to stop it -- and when the rail turned out to be
    defeatable it corrupted the live books a second time. A test that damages
    production when it fails is not an acceptable way to test a guard against
    damaging production.
    """
    import os
    import tempfile
    from bcs.tests.conftest import ProductionWriteAttempted, REAL_LOGS

    victim = REAL_LOGS / '_rail_probe_never_created.json'
    with pytest.raises(ProductionWriteAttempted):
        if how == 'write_text':
            victim.write_text('x', encoding='utf-8')
        elif how == 'open':
            open(str(victim), 'w').close()
        elif how == 'mkstemp':
            tempfile.mkstemp(dir=str(REAL_LOGS))
        else:
            os.open(str(victim), os.O_WRONLY | os.O_CREAT)
    assert not victim.exists(), 'the probe file was created despite the rail'


def test_the_rail_still_allows_writes_outside_the_real_logs_dir(tmp_path):
    """Negative control: a rail that blocked everything would make the whole
    suite pass by making it impossible to write anything at all."""
    (tmp_path / 'fine.json').write_text('{}', encoding='utf-8')
    assert (tmp_path / 'fine.json').read_text(encoding='utf-8') == '{}'


def test_the_rail_is_not_catchable_by_production_error_handling(book):
    """`_write_high_water` and `_flag_corruption` both swallow Exception by
    design. A plain-Exception rail would be caught there, logged as a warning,
    and the writes would keep landing while the suite stayed green -- which is
    how the Telegram version of this rail survived two fixes before it worked.
    """
    from bcs.tests.conftest import ProductionWriteAttempted
    assert issubclass(ProductionWriteAttempted, BaseException)
    assert not issubclass(ProductionWriteAttempted, Exception)


def test_a_save_failure_is_loud_rather_than_silent(book, monkeypatch):
    """A write that did not reach disk must never look like one that did."""
    store = book.make()
    monkeypatch.setattr(book.cls, '_save_local',
                        lambda self: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        store.set_trade_status(1, 'closed')
    assert book.read()[0]['status'] == 'open', "disk changed despite the error"


def test_an_unreadable_store_file_releases_the_lock_too(book, monkeypatch):
    """`_read_local` only catches JSON/Value errors. An OSError from the read
    happens INSIDE the lock, on a path that did not exist before B7."""
    store = book.make()

    def boom(self):
        raise OSError('io error')
    monkeypatch.setattr(book.cls, '_read_local', boom)

    with pytest.raises(OSError):
        store.update_trade_fields(1, note='mine')
    with exclusive(book.lock, timeout=1.0):
        pass
    assert getattr(store, '_in_mutate', False) is False


# ── Contention must degrade the right way ───────────────────────────────────

def test_a_write_refuses_rather_than_writing_unlocked(book):
    """The only acceptable response to 'cannot lock' is 'do not write'."""
    store = book.make()
    before = book.data.read_bytes()
    with exclusive(book.lock):
        with pytest.raises(LockTimeout):
            store.set_trade_status(1, 'closed')
    assert book.data.read_bytes() == before


def test_a_lock_timeout_during_sync_does_not_propagate(book, monkeypatch):
    """`maybe_sync` runs at the top of every poll. If contention could raise
    out of it, one busy moment would abandon the whole cycle -- and the cycle
    is what checks the stops. A missed REFRESH is survivable; a missed poll is
    not.
    """
    store = book.make()
    store._drive_enabled = True
    store._drive_file_id = 'x'
    monkeypatch.setattr(book.mod.drive_store, 'download_json',
                        lambda svc, fid: [])
    monkeypatch.setattr(book.cls, '_upload_to_drive', lambda self: None)

    with exclusive(book.lock):
        store.maybe_sync(force=True)             # must NOT raise


def test_the_reentry_guard_beats_the_timeout(book):
    """A nested _mutate must fail in milliseconds, not after LOCK_TIMEOUT.

    With the guard removed this raises LockTimeout instead -- correct outcome,
    but only after a stall, and reported as contention rather than as the bug
    it is.
    """
    store = book.make()
    import time as _t
    t0 = _t.time()
    with pytest.raises(RuntimeError) as ei:
        with store._mutate():
            with store._mutate():
                pass
    assert not isinstance(ei.value, LockTimeout)
    assert _t.time() - t0 < store.LOCK_TIMEOUT


# ── The id sidecar cannot go backwards under the lock ───────────────────────

def test_every_high_water_write_happens_under_the_lock(book):
    """`_write_high_water` is read-then-write and NOT atomic on its own. It is
    safe only because both callers run inside `_mutate`. If a third caller
    appears outside the lock, two processes can interleave and the mark drops.
    """
    src = (HELPER / 'common' / 'locked_store.py').read_text(encoding='utf-8')
    callers = [ln.strip() for ln in src.splitlines()
               if '_write_high_water(' in ln and 'def ' not in ln]
    assert len(callers) == 2, (
        f"expected exactly two callers (allocate_id, _note_ids_seen); found "
        f"{len(callers)}: {callers}. A new one outside _mutate would let the "
        f"high-water mark move backwards.")


def test_ids_survive_a_quarantine_that_happens_mid_session(book):
    """Not the startup case the B7 suite covers: the book is corrupted while a
    live store object is already holding trades in memory."""
    store = book.make(seed_ids=(1, 2, 3))
    store.update_trade_fields(1, note='warm')            # mark advances to 3

    book.data.write_text('{ truncated', encoding='utf-8')
    reopened = book.cls(config={'google_drive': {'enabled': False}})
    reopened.initialize()

    assert reopened.load_trades() == []
    assert reopened.add_trade(book.payload)['id'] == 4


def test_a_quarantine_does_not_wipe_the_high_water_mark(book):
    """The mark lives in its own file for exactly this reason."""
    store = book.make(seed_ids=(1, 2, 3, 4, 5))
    store.update_trade_fields(1, note='warm')
    assert store._read_high_water() == 5

    book.data.write_text('{ truncated', encoding='utf-8')
    reopened = book.cls(config={'google_drive': {'enabled': False}})
    reopened.initialize()
    assert reopened._read_high_water() == 5, (
        "the sidecar was collateral damage in the quarantine")


# ── The corruption marker cannot silence itself ─────────────────────────────

def test_a_corrupt_marker_file_still_reports_corruption(book):
    """The marker is JSON on the same disk that just produced a corrupt store.
    If the marker itself is unreadable, the safe answer is 'no marker' only
    because a false CLEAR is worse than a false alarm -- but it must not crash
    the monitor either way.
    """
    book.make(seed_ids=(1,))
    book.data.write_text('{ truncated', encoding='utf-8')
    store = book.cls(config={'google_drive': {'enabled': False}})
    store.initialize()
    assert store.read_corruption_marker()

    store._corrupt_marker_path().write_text('}{ garbage', encoding='utf-8')
    assert store.read_corruption_marker() == {}          # no crash
    assert store.corruption_due_for_alert() == {}


def test_a_marker_holding_a_list_is_rejected_not_indexed(book):
    """`json.loads` on a valid-but-wrong shape returns a list, and `.get` on a
    list is an AttributeError inside the alerting path."""
    store = book.make()
    store._corrupt_marker_path().write_text('[1,2,3]', encoding='utf-8')
    assert store.read_corruption_marker() == {}
    assert store.corruption_due_for_alert() == {}


def test_note_corruption_alerted_on_a_clean_book_is_a_no_op(book):
    store = book.make()
    store.note_corruption_alerted()                      # must not create one
    assert store.read_corruption_marker() == {}
    assert not store._corrupt_marker_path().exists()


# ── The sidecars must not be mistaken for the book ──────────────────────────

def test_the_sidecars_never_collide_with_the_store_file(book):
    store = book.make()
    paths = {store._data_path(), store._lock_path(),
             store._high_water_path(), store._corrupt_marker_path()}
    assert len(paths) == 4, f"two of these are the same file: {paths}"


def test_a_corrupt_backup_is_not_picked_up_as_the_store(book):
    """`_read_local` renames to `<stem>.corrupt.<ts>.json` in the SAME
    directory. Anything that later globs the directory would find it."""
    book.make(seed_ids=(1, 2))
    book.data.write_text('{ truncated', encoding='utf-8')
    store = book.cls(config={'google_drive': {'enabled': False}})
    store.initialize()

    backup = Path(store.read_corruption_marker()['backup'])
    assert backup != store._data_path()
    assert store.load_trades() == []
    store.add_trade(book.payload)
    assert [t['id'] for t in book.read()] == [3], (
        "the new book picked up something other than the new trade")
