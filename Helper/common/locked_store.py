"""B6 — cross-process write serialisation for the money trade stores.

The defect
----------
`bcs`, `fallen_hero` and `bear_put` each keep `self._trades` as an in-memory
cache and persist it with a whole-file rewrite. Every writer is therefore a
read-modify-write against a cache that another process may already have
invalidated:

    monitor  loads  [A,B]            (cache warmed at startup)
    CLI      loads  [A,B]
    CLI      writes [A,B,C]          <- `bcs enter` adds trade C
    monitor  writes [A,B]            <- rewrites its stale cache; C is GONE

No exception, no corrupt file, no log line — a trade simply ceases to exist,
and the one that vanishes is the one just entered. `_save_local`'s tmp+rename
protects against TORN files, which is a different problem entirely.

This is not a narrow window. `initialize()` -> `_sync_from_drive` ->
`_save_local` makes the periodic Drive re-sync a WRITER too, so the live
monitor re-opens the window every poll, all session, on all three books.

Why a mixin and not a fourth copy
---------------------------------
The three stores are near-verbatim copies of each other (584 / 687 / 534
lines) with byte-identical private helpers. Copy-paste is how one store's
missing lock became three. The locking lives here once; each store supplies
its own `_lock_path()` so the books do not serialise against each other.

`zebra/` keeps its own `_mutate` (same design, different schema and a
different merge rule) — deliberately not unified, because merging the PAPER
system's store into the LIVE-money one to save thirty lines is the coupling
`feedback_guard_the_money_system_first` exists to prevent.

The contract for callers
------------------------
* Every mutation goes inside `with self._mutate():`. One unlocked writer
  defeats advisory locking entirely.
* Look up the trade you are mutating INSIDE the block. `_mutate` re-reads disk
  and version-merges on entry, so a dict fetched beforehand may be a detached
  object whose mutations are silently dropped.
* Never call a network API inside the block. The Drive upload is deliberately
  outside it — holding a mutex across an HTTP round-trip would stall the other
  process's entire cycle.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from common import store_contract
from common.filelock import DEFAULT_TIMEOUT, LockTimeout, exclusive

logger = logging.getLogger(__name__)

__all__ = ['LockTimeout', 'LockedStoreMixin']


class LockedStoreMixin:
    """Adds `_mutate()` to a store that has `_read_local`/`_save_local`/
    `_merge_trades`/`_upload_to_drive` and a `self._trades` list cache.

    Subclasses MUST override `_lock_path()`.
    """

    #: Generous on purpose — every critical section here is a disk read, a
    #: dict mutation and a disk write, so a wait this long means the other
    #: holder is wedged, not merely busy. Tests shrink it; production must
    #: not, because a short timeout turns contention into a refused write.
    LOCK_TIMEOUT: float = DEFAULT_TIMEOUT

    #: How long a corruption alert stays suppressed after it fires. The cron
    #: relaunches every 5 minutes and the quarantine path EXITS the monitor,
    #: so without this the marker would Telegram twelve times an hour until
    #: someone looked at it.
    CORRUPT_REALERT_SEC = 3600

    def _lock_path(self) -> Path:
        raise NotImplementedError(
            f"{type(self).__name__} must implement _lock_path(). A store "
            f"without a lock file cannot mutate safely, and defaulting to a "
            f"shared path would serialise unrelated books against each other."
        )

    def _data_path(self) -> Path:
        """The store's JSON file. Both sidecars below are keyed to it."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement _data_path()")

    # -- B7a: an id that has never been used, even after a quarantine -----

    def allocate_id(self) -> int:
        """`max(id) + 1` is an allocator only while the list is COMPLETE.

        It is not, in the one case that matters. `_read_local` quarantines a
        corrupt file and returns `[]`; if Drive is also down `initialize()`
        takes the local branch, so the store legitimately starts empty — and
        the old allocator handed out 1, 2, 3 again.

        That is destructive, not merely confusing. `_merge_trades` is keyed on
        `id` with the higher `version` winning, so once Drive returns, a
        recycled id whose version has outrun the original REPLACES a genuine
        trade. The original is then gone from disk (quarantined) and from Drive
        (overwritten); the only surviving copy is a `.corrupt.*.json` backup
        nobody knows to look in.

        The high-water mark is advisory: missing or unreadable, we fall back to
        the live max and are no worse off than before.
        """
        live = max((t.get('id', 0) for t in self._trades), default=0)
        nid = max(live, self._read_high_water()) + 1
        self._write_high_water(nid)
        return nid

    def _note_ids_seen(self) -> None:
        """Raise the high-water mark to cover every id currently in the book.

        Advancing it only on allocation is not enough, and the gap is not
        hypothetical: every trade that already exists was written before this
        sidecar did, so on the live books the file starts absent. A quarantine
        would then empty a book holding ids 1..N against a mark of 0, and the
        allocator would hand out 1 again — the exact loss this is here to stop,
        on precisely the records that matter most.

        Called from `_mutate` right after the refresh, so the mark tracks every
        id ever SEEN rather than every id this process happened to allocate.
        """
        live = max((t.get('id', 0) for t in self._trades), default=0)
        if live:
            self._write_high_water(live)

    def _high_water_path(self) -> Path:
        # Keyed to the STORE FILE, not to the directory: the sequence belongs
        # to one book. A directory-wide sidecar would make three books share an
        # id space, so opening one would silently skip ids in the others.
        return self._data_path().with_suffix('.nextid.json')

    def _read_high_water(self) -> int:
        try:
            return int(json.loads(self._high_water_path().read_text(
                encoding='utf-8')).get('max_id_ever') or 0)
        except Exception:
            return 0

    def _write_high_water(self, value: int) -> None:
        """Best-effort and never fatal — a trade must not fail to save because
        a bookkeeping sidecar could not be written."""
        try:
            if value <= self._read_high_water():
                return
            self._high_water_path().parent.mkdir(parents=True, exist_ok=True)
            self._high_water_path().write_text(
                json.dumps({'max_id_ever': int(value)}), encoding='utf-8')
        except Exception as e:
            logger.warning("Could not persist the id high-water mark: %s", e)

    # -- B7b: quarantine must be audible ----------------------------------

    def _corrupt_marker_path(self) -> Path:
        return self._data_path().with_suffix('.corrupt-flag.json')

    def _flag_corruption(self, err: str, backup,
                         kind: str = store_contract.MARKER_QUARANTINE) -> None:
        """Leave a marker the MONITOR turns into a Telegram.

        Quarantine is the highest-consequence event this store has, and it was
        log-only. The book goes empty, the cron loop hits "All trades closed...
        exiting", and every open position stops being monitored — the only
        witness one CRITICAL line in a log nobody is tailing. The total failure
        was precisely the case that could not report itself.

        A marker file rather than a direct send: this module deliberately has
        no Telegram dependency, and the cron process exits between cycles, so
        an in-memory flag would not survive to reach the alerting layer.

        `kind` exists because a second, far milder condition was later wired to
        this same marker — a merge conflict, where the book is intact — and the
        alerting layers went on reading out the quarantine story: "failed to
        parse", "restarted EMPTY", "positions UNMONITORED". A CRITICAL alert
        that is false in every clause is worse than no alert, because it trains
        the reader to swipe past the one message that means the stops are dead.
        Defaults to QUARANTINE so that markers written before this change, and
        every caller that does not pass a kind, keep their original meaning.
        """
        try:
            path = self._corrupt_marker_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            # A MILD EVENT MUST NOT ERASE A SEVERE ONE (fixed 2026-08-31).
            #
            # This is a single last-writer-wins slot, so a routine
            # MERGE_CONFLICT overwrote a QUARANTINE that had not been alerted
            # yet -- a cron gap, or Telegram briefly down, is all it takes.
            # The operator then reads "two writers disagreed, the book is
            # intact" about a book that actually went EMPTY, and the marker no
            # longer names the `.corrupt.*.json` backup, which after a
            # quarantine is the only surviving copy of anything Drive has not
            # seen. Overwriting also reset `alerted_at`, so the severe event
            # lost its place in the queue as well as its story.
            #
            # This is the exact INVERSE of the 2026-08-31 false-"EMPTY"
            # incident: there a mild event wore the severe one's words; here
            # the severe event is replaced by the mild one's.
            if kind == store_contract.MARKER_MERGE_CONFLICT:
                try:
                    held = json.loads(path.read_text(encoding='utf-8'))
                except Exception:
                    held = None
                if isinstance(held, dict) and not held.get('alerted_at') and (
                        (held.get('kind')
                         or store_contract.MARKER_QUARANTINE)
                        == store_contract.MARKER_QUARANTINE):
                    # A missing `kind` reads as QUARANTINE, deliberately --
                    # every marker written before the field existed was one.
                    logger.critical(
                        'a merge conflict on %s was NOT recorded: an '
                        'un-alerted QUARANTINE marker from %s is still '
                        'standing and must not be overwritten. Conflict was: '
                        '%s', self._data_path().name, held.get('at'), err)
                    return
            path.write_text(json.dumps({
                'at': datetime.now().isoformat(timespec='seconds'),
                'store': self._data_path().name,
                'kind': kind,
                'error': err,
                'backup': str(backup),
                'alerted_at': None,
            }), encoding='utf-8')
        except Exception as e:
            logger.error("Could not write the corruption marker: %s", e)

    def read_corruption_marker(self) -> dict:
        """The marker, or {} if this book has not been quarantined."""
        try:
            data = json.loads(
                self._corrupt_marker_path().read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def note_corruption_alerted(self) -> None:
        """Stamp the marker so the next cron relaunch does not re-shout.

        The marker is NOT cleared. The condition is unresolved until a human
        reads the `.corrupt.*.json` backup, and clearing it here would make the
        very next cycle look healthy.
        """
        marker = self.read_corruption_marker()
        if not marker:
            return
        marker['alerted_at'] = time.time()
        try:
            self._corrupt_marker_path().write_text(
                json.dumps(marker), encoding='utf-8')
        except Exception as e:
            logger.error("Could not stamp the corruption marker: %s", e)

    def corruption_due_for_alert(self) -> dict:
        """The marker if it should be shouted about now, else {}."""
        marker = self.read_corruption_marker()
        if not marker:
            return {}
        last = marker.get('alerted_at')
        try:
            if last is not None and (time.time() - float(last)) < self.CORRUPT_REALERT_SEC:
                return {}
        except (TypeError, ValueError):
            pass          # unreadable stamp: shout rather than stay quiet
        return marker

    # -- Reads ------------------------------------------------------------

    @contextmanager
    def _read_lock(self) -> Iterator[None]:
        """Serialise a whole-file read against a concurrent save.

        `_save_local` is tmp+os.replace, so a reader never sees a TORN file.
        But on Windows the reader's open handle makes the writer's `os.replace`
        fail outright, and here the writer is the live monitor.

        Re-entrant by inspection, not by luck: `_mutate` already holds the
        lock, and flock on a second fd conflicts with the first even inside one
        process, so taking it again there would deadlock rather than nest.
        """
        if getattr(self, '_in_mutate', False):
            yield
            return
        with exclusive(self._lock_path(), timeout=self.LOCK_TIMEOUT):
            yield

    @contextmanager
    def _mutate(self, drive: bool = True, persist: bool = True) -> Iterator[None]:
        """Lock -> refresh from disk -> caller mutates -> save -> unlock -> Drive.

        The refresh matters as much as the lock. `self._trades` goes stale the
        moment the other process writes, so mutating and saving it would push
        stale data back over fresh. Re-reading inside the lock makes disk the
        truth before the caller touches anything.
        """
        if getattr(self, '_in_mutate', False):
            # flock() on a second fd conflicts with the first even within one
            # process, so nesting self-deadlocks until LockTimeout 30s later.
            # Fail instantly and name the bug instead of stalling the monitor.
            raise RuntimeError(
                f"{type(self).__name__}._mutate() is already held on this "
                f"thread — nesting it deadlocks. Move the inner write out, or "
                f"call the private helper that assumes the lock is held.")

        with exclusive(self._lock_path(), timeout=self.LOCK_TIMEOUT):
            self._in_mutate = True
            try:
                # DISK vs THIS PROCESS'S CACHE -- one replica, two writers on
                # one box. An equal-version difference here is the sibling
                # process's concurrent write that this refresh exists to
                # absorb, not a split brain between machines.
                self._trades = self._merge_trades(self._read_local(),
                                                  self._trades,
                                                  same_replica=True)
                self._note_ids_seen()
                # Rollback point. If the caller raises half-way through a
                # multi-field mutation, the partly-changed trade must not
                # linger in the cache: get_open_trades() would then report a
                # state that was never persisted, and on this codebase that
                # means the monitor acting on a position that does not exist.
                # Disk is untouched either way — the save below is skipped.
                snapshot = copy.deepcopy(self._trades)
                try:
                    yield
                except BaseException:
                    self._trades = snapshot
                    raise
                # A mutation that changed nothing must not rewrite the store.
                # `update_trade_fields` runs for every open position on every
                # poll to carry trailing-SL state, and most polls move nothing;
                # without this each one would rewrite the file and take the
                # cross-process lock on a Pi that also runs the live monitor.
                changed = self._trades != snapshot
                if persist and changed:
                    # THE SAVE ROLLS BACK TOO (fixed 2026-08-31).
                    #
                    # The rollback above covered only exceptions from `yield`.
                    # If `_save_local` itself raised -- disk full, or the
                    # Windows `os.replace` losing to a stray unlocked reader --
                    # the caller got the exception and believed the write had
                    # FAILED, while the cache kept the mutation at version N+1.
                    # The next `_mutate` on any OTHER trade then refreshed
                    # disk(vN) against cache(vN+1), the cache won, and the
                    # "failed" write silently committed minutes later.
                    #
                    # Concretely: `begin_close` raises, the caller escalates to
                    # a human believing no close-lock exists, and the `closing`
                    # status materialises afterwards with nobody driving the
                    # close -- a position stranded until the recovery sweep.
                    #
                    # Cache and disk now fail together, which is the only
                    # state a caller can reason about.
                    try:
                        self._save_local()
                    except BaseException:
                        self._trades = snapshot
                        raise
            finally:
                self._in_mutate = False

        if persist and changed and drive:
            self._upload_to_drive()
