"""Four hardenings `zebra/trade_store.py` has and the shared base did not.

Found 2026-08-31. `zebra/trade_store.py` is the most hardened of the four
stores; `common/spread_store.py` + `common/locked_store.py` -- the base under
the two LIVE-MONEY books -- was missing four of its protections. Every one is
the repo's own "copy you did not open" shape, this time with the shared base
as the copy nobody opened.

1. `same_replica`. The refresh inside `_mutate` merges this box's DISK against
   this process's CACHE. An equal-version difference there is a sibling
   process's concurrent write -- exactly what the refresh exists to absorb --
   not a split brain between machines. zebra was given the flag on 2026-08-31
   after the detector's first live session produced 11 CRITICAL lines and 4
   false corruption alerts against a fully intact book. The base never got it.

2. `unversioned_fields`. `update_trade_fields` writes trail state local-only
   and deliberately does NOT bump `version` ("changes every few seconds"). So
   local and Drive hold the same version with different content, which reads
   as a split brain -- a CRITICAL line, a MERGE_CONFLICT marker and an hourly
   Telegram, once per open position per sync, for as long as it is open.

3. The Windows `os.replace` retry. A stray unlocked reader holding the book
   open makes the write fail outright. The BCS and Fallen Hero books are
   hand-captured on Windows, so that reader is a realistic Tuesday afternoon.

4. `_sync_locked` released on EVERY path. `begin_close` sets it;
   `update_trade_exit`'s only release sat after a `raise`, so a contract
   refusal (or a `LockTimeout`) stuck it True and `maybe_sync` returned early
   for the rest of the process -- the long-lived monitor running local-only
   for a whole session, saying so nowhere.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest common/tests/test_spread_store_hardenings.py -v
"""
import importlib
import inspect
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common import store_contract as sc                          # noqa: E402
from common.locked_store import LockedStoreMixin                 # noqa: E402
from common.spread_store import SpreadStoreBase                  # noqa: E402

NO_DRIVE = {'google_drive': {'enabled': False}}
BOOKS = [
    ('bcs.trade_store', 'TradeStore'),
    ('bear_put.trade_store', 'BearPutStore'),
    ('fallen_hero.trade_store', 'FallenHeroStore'),
]
IDS = [b[1] for b in BOOKS]


@pytest.fixture(autouse=True)
def isolate_books(tmp_path, monkeypatch):
    """Every book's paths inside tmp_path, for EVERY test in this file.

    Not optional hygiene. Without it `update_trade_fields` below runs
    `_mutate`, which refreshes against the REAL book on disk and then SAVES --
    so the test's synthetic field landed in `logs/bcs_trades.json`,
    `logs/bear_put_trades.json` and `logs/fallen_hero_trades.json`, and left a
    merge-conflict marker behind. That happened while writing this file, which
    is the fifth instance of `[[feedback_tests_must_not_touch_production]]`
    and the reason `common/tests/conftest.py` now carries the same
    no-production-writes rail `bcs/tests/` has had all along.
    """
    for modname in ('bcs.trade_store', 'bear_put.trade_store',
                    'fallen_hero.trade_store'):
        m = importlib.import_module(modname)
        stem = Path(m.LOCAL_TRADES_FILE).stem
        monkeypatch.setattr(m, 'LOG_DIR', tmp_path)
        monkeypatch.setattr(m, 'LOCAL_TRADES_FILE', tmp_path / (stem + '.json'))
        monkeypatch.setattr(m, 'LOCK_FILE', tmp_path / (stem + '.lock'))


def _store(modname, clsname, monkeypatch=None):
    cls = getattr(importlib.import_module(modname), clsname)
    store = cls(config=dict(NO_DRIVE))
    # Spy on the real alerting call rather than the marker file: what is under
    # test is whether the merge DECIDES to alarm, not where it writes.
    store.flagged = []
    store._flag_corruption = lambda err, backup, **kw: store.flagged.append(err)
    return store


def _rec(**extra):
    r = {'id': 1, 'status': 'open', 'version': 4}
    r.update(extra)
    return r


# -- 1. a sibling process's write is not a split brain -----------------------

@pytest.mark.parametrize('modname,clsname', BOOKS, ids=IDS)
def test_a_same_replica_tie_is_silent(modname, clsname):
    """THE FALSE ALARM. Disk and cache differing at one version is the
    concurrent write the refresh absorbs, not two machines disagreeing."""
    store = _store(modname, clsname)
    disk = [_rec(trail_peak=11.0)]
    cache = [_rec(trail_peak=12.5)]
    merged = store._merge_trades(disk, cache, same_replica=True)
    assert len(merged) == 1
    # base (disk) still wins, as it always did -- what changes is the silence
    assert store.flagged == [], (
        'a same-replica tie raised a corruption alert')


@pytest.mark.parametrize('modname,clsname', BOOKS, ids=IDS)
def test_a_CROSS_replica_tie_is_still_reported(modname, clsname):
    """The negative control. Silencing the sibling case must not silence the
    real split brain between two machines, which no operator can infer from
    the book itself."""
    store = _store(modname, clsname)
    merged = store._merge_trades([_rec(notes='a')], [_rec(notes='b')],
                                 same_replica=False)
    assert len(merged) == 1
    assert store.flagged, (
        'a genuine cross-replica split brain went unreported')


# -- 2. a deliberate unversioned write is not a conflict ---------------------

@pytest.mark.parametrize('modname,clsname', BOOKS, ids=IDS)
def test_a_trail_write_does_not_raise_a_merge_conflict(modname, clsname):
    """`update_trade_fields` declares what it wrote without a bump, so the
    merge can tell it apart from a divergence."""
    store = _store(modname, clsname)
    store._trades = [_rec()]
    store._unversioned_written.update({'trail_peak', 'trail_sl'})
    store._merge_trades([_rec(trail_peak=12.5)], [_rec(trail_peak=11.0)],
                        same_replica=False)
    assert store.flagged == [], (
        'a field written local-only without a version bump was reported as a '
        'split brain -- once per open position per sync, hourly, forever')


@pytest.mark.parametrize('modname,clsname', BOOKS, ids=IDS)
def test_the_exemption_is_declared_by_the_writer_not_hand_maintained(
        modname, clsname):
    """ONE source. `update_trade_fields` takes `**fields` and three call sites
    pass computed keys, so a hand-written allowlist would drift on the next
    field added."""
    store = _store(modname, clsname)
    store._trades = [_rec()]
    assert store._unversioned_written == set()
    store.update_trade_fields(1, some_new_field_nobody_listed=7)
    assert 'some_new_field_nobody_listed' in store._unversioned_written


@pytest.mark.parametrize('modname,clsname', BOOKS, ids=IDS)
def test_an_UNDECLARED_field_still_conflicts(modname, clsname):
    """The exemption must be narrow: only fields this process actually wrote
    unversioned are excused. Anything else is still a divergence."""
    store = _store(modname, clsname)
    store._merge_trades([_rec(exit_reason='tp')], [_rec(exit_reason='sl')],
                        same_replica=False)
    assert store.flagged


# -- 3 & 4. structural pins on the two remaining hardenings ------------------

def test_the_base_retries_the_windows_rename():
    """A stray unlocked reader must not lose the write.

    RETIRES WHEN: the money books are no longer hand-captured on Windows, so
    an unlocked reader of the data file is not a realistic event.
    """
    src = inspect.getsource(SpreadStoreBase._save_local)
    assert 'PermissionError' in src, (
        'the shared base lost the Windows rename retry that zebra has')


def test_update_trade_exit_releases_the_sync_lock_on_every_path():
    """The release must not sit after the contract `raise`.

    RETIRES WHEN: `_sync_locked` becomes a context manager owned by
    `begin_close`, so no caller can forget to release it.
    """
    src = inspect.getsource(SpreadStoreBase.update_trade_exit)
    assert 'finally' in src and '_sync_locked = False' in src, (
        'a refused or lock-timed-out exit can strand _sync_locked True, '
        'which silently disables Drive sync for the whole session')


def test_the_refresh_declares_itself_same_replica():
    """The one call site that compares disk against this process's cache.

    RETIRES WHEN: `resolve_merge` is given the two sides' identities rather
    than a boolean, so the caller cannot mislabel them.
    """
    src = inspect.getsource(LockedStoreMixin._mutate)
    assert 'same_replica=True' in src, (
        'the in-lock refresh no longer says it is comparing one replica '
        'against itself; sibling writes will alarm again')


# ── 5. the two REFUSALS, which shipped unpinned ────────────────────────────
#
# Called out by review on 2026-09-01: the commit that added them claimed
# "every fix is pinned by tests that were checked by REVERTING the fix", and
# for these two that was FALSE — reverting either guard left the whole suite
# green. A guard nothing asserts is one a later simplification pass deletes.

@pytest.mark.parametrize('modname,clsname', BOOKS, ids=IDS)
def test_a_closed_trade_cannot_be_reopened_by_the_setter(modname, clsname):
    """`closed -> open` in one call. The merge's monotonic-close rule never
    sees it: an ordinary versioned local write is exactly what that rule
    treats as legitimate."""
    store = _store(modname, clsname)
    store._trades = [{'id': 1, 'status': 'closed', 'version': 3}]
    with pytest.raises(ValueError) as e:
        store.set_trade_status(1, 'open')
    assert 'terminal' in str(e.value)
    assert store._trades[0]['status'] == 'closed'


@pytest.mark.parametrize('modname,clsname', BOOKS, ids=IDS)
def test_the_freeze_and_the_close_lock_still_work(modname, clsname):
    """The negative control. `partial_close` is FROZEN, not terminal, and the
    close path depends on reaching it — a guard that blocked the freeze would
    break every half-done close."""
    store = _store(modname, clsname)
    store._trades = [{'id': 1, 'status': 'open', 'version': 1}]
    store.set_trade_status(1, 'closing')
    assert store._trades[0]['status'] == 'closing'
    store.set_trade_status(1, 'partial_close', close_failed_leg='long')
    assert store._trades[0]['status'] == 'partial_close'
    assert store._trades[0]['close_failed_leg'] == 'long'


@pytest.mark.parametrize('modname,clsname', BOOKS, ids=IDS)
def test_reclosing_a_closed_trade_with_the_same_status_is_allowed(
        modname, clsname):
    """Idempotence: `closed -> closed` moves nothing and must not raise."""
    store = _store(modname, clsname)
    store._trades = [{'id': 1, 'status': 'closed', 'version': 3}]
    store.set_trade_status(1, 'closed')


@pytest.mark.parametrize('modname,clsname', BOOKS, ids=IDS)
def test_update_trade_fields_refuses_the_merge_keys(modname, clsname):
    """`status` was zebra-only; `id`/`version`/`exit` silently break the merge
    that keys on them."""
    store = _store(modname, clsname)
    store._trades = [{'id': 1, 'status': 'open', 'version': 1}]
    for bad in ({'status': 'closed'}, {'id': 9}, {'version': 99},
                {'exit': {}}):
        with pytest.raises(ValueError):
            store.update_trade_fields(1, **bad)
    assert store._trades[0] == {'id': 1, 'status': 'open', 'version': 1}


@pytest.mark.parametrize('modname,clsname', BOOKS, ids=IDS)
def test_update_trade_fields_still_writes_advisory_state(modname, clsname):
    """The negative control: this method carries the trail and the nag
    markers on every poll and must keep working."""
    store = _store(modname, clsname)
    store._trades = [{'id': 1, 'status': 'open', 'version': 1}]
    assert store.update_trade_fields(1, trail_active=True, trail_peak=12.5)
    assert store._trades[0]['trail_peak'] == 12.5


@pytest.mark.parametrize('modname,clsname', BOOKS, ids=IDS)
def test_set_trade_status_extra_fields_cannot_clobber_the_version(
        modname, clsname):
    """`extra_fields` writes AFTER the version bump, so it was an unguarded
    sibling of the door `update_trade_fields` just closed."""
    store = _store(modname, clsname)
    store._trades = [{'id': 1, 'status': 'open', 'version': 1}]
    with pytest.raises(ValueError):
        store.set_trade_status(1, 'closing', version=99)
    assert store._trades[0]['version'] == 1


@pytest.mark.parametrize('modname,clsname', BOOKS, ids=IDS)
def test_a_refused_status_change_releases_the_sync_lock(modname, clsname):
    """`begin_close` sets `_sync_locked`; if a sibling books the record closed
    in the race window this refusal fires, and leaving the flag set turns Drive
    sync off for the rest of a long-lived monitor session. `update_trade_exit`
    got a `try/finally` for exactly this; this method did not."""
    store = _store(modname, clsname)
    store._trades = [{'id': 1, 'status': 'closed', 'version': 3}]
    store._sync_locked = True
    with pytest.raises(ValueError):
        store.set_trade_status(1, 'open')
    assert store._sync_locked is False, (
        'Drive sync is now off for the rest of this process')
