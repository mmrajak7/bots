"""One implementation of the three books that are the same book.

WHY THIS EXISTS
---------------
`bcs/trade_store.py`, `bear_put/trade_store.py` and `fallen_hero/trade_store.py`
were ~2,650 lines carrying about 500 lines of real difference -- difflib put
them at 0.71 to 0.77 similar, and 18 of their 29 methods were byte-identical
once comments and the store's own name were stripped. Seven more differed only
in a log string.

That is not a tidiness complaint. Nearly every defect this repository has paid
for twice has one shape: an invariant enforced in one copy and quietly not in
another. These three files produced that shape most reliably, and the evidence
is in their own history -- `reconcile_after_close` read two leg fields of six
because Fallen Hero's copy was never opened; the S3 residue sweep had to be
added three times; `get_entry_residue_trades` was written three times in one
afternoon this very session. [[feedback_copy_pasted_modules_fix_once]]

**`zebra/trade_store.py` is deliberately NOT merged in.** It measured 0.095
similar: a genuinely different state machine (watching -> triggered -> entered
-> exited, with signals that never become positions), and forcing it into this
hierarchy would manufacture exactly the coupling this file exists to remove.

WHAT STAYS IN EACH BOOK
-----------------------
The parts that are actually different, and only those:

  add_trade         each schema has its own required fields and cross-field
                    validations. FH's is 164 lines of them, every one about a
                    credit structure this base knows nothing of.
  _migrate_trades   per-schema backfills.
  list_trades       what a human needs to see differs per structure.
  _bound_exit       the exit bound is a property of the STRUCTURE: a vertical
                    is capped by its width, a reverse jade lizard is not
                    capped the same way at all.

HOW THE PATHS STAY PER-STORE
----------------------------
Every path, the logger and the config loaders resolve AT CALL TIME from the
subclass's own module, and are never captured here. That is not indirection for
its own sake. `_lock_path` and `_data_path` already carried a docstring saying
why: a class attribute would freeze the lock in the real `logs/` directory
while a test redirected the data to `tmp_path` -- "locked, but not against the
writer that matters". Tests monkeypatch `mod.LOCAL_TRADES_FILE` and must keep
working.

Resolved by walking the MRO rather than by `type(self).__module__`, so a test
subclass of a real store still finds the store's paths instead of its own
module's absence of them.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from common import store_contract
from common.locked_store import LockTimeout, LockedStoreMixin


class SpreadStoreBase(LockedStoreMixin):
    """Everything the three option-spread books do identically.

    A subclass supplies, in its own module: `LOCAL_TRADES_FILE`, `LOCK_FILE`,
    `LOG_DIR`, `logger`, `drive_store`, `_load_config` and
    `_resolve_credentials_path`; and on the class, whatever of `add_trade` /
    `_migrate_trades` / `list_trades` / `_bound_exit` it actually needs.

    `drive_store` goes through the module for a second reason beyond symmetry:
    importing `bcs.drive_store` here would close an import cycle, because
    `bcs/__init__.py` imports the store that imports this file.
    """

    #: Set by each subclass to its own `__name__`. Explicit rather than
    #: inferred, so a store that forgets it fails loudly instead of silently
    #: reading this module's (absent) globals.
    _MODULE: Optional[str] = None

    def _mod(self):
        """The module that owns this store's paths, logger and config.

        Walks the MRO so a test subclass of a real store still resolves to the
        store's module. Raising is deliberate: a store whose paths cannot be
        found would otherwise fall through to whatever the base module happens
        to define, and for a book of real positions that is the worst
        available failure.
        """
        for cls in type(self).__mro__:
            name = cls.__dict__.get('_MODULE')
            if name and name in sys.modules:
                return sys.modules[name]
        raise RuntimeError(
            '%s does not declare _MODULE, so its data file, lock and logger '
            'cannot be resolved' % type(self).__name__)

    @property
    def _logger(self):
        return self._mod().logger

    def _bound_exit(self, trade: dict, exit_data: dict) -> dict:
        """Clamp an exit to what this STRUCTURE can be worth. Identity here.

        A vertical is bounded by its width; a reverse jade lizard is not
        bounded the same way at all. Each book with a bound overrides this.
        `bear_put` deliberately has none yet, and its absence is now a visible
        unoverridden hook rather than an invisible missing call.
        """
        return exit_data

    def _migrate_trades(self):
        """Per-schema backfills. No-op unless a book needs one."""
        return None

    def __init__(self, config: Optional[dict] = None):
        self._config = config or self._mod()._load_config()
        self._trades: list = []
        self._drive_service = None
        self._drive_file_id: Optional[str] = None
        self._drive_enabled = False
        self._last_sync_time = 0.0
        self._sync_interval = (
            self._config.get('google_drive', {}).get('sync_interval_sec', 300)
        )
        # Prevents Drive sync from overwriting in-memory state during
        # close operations. Set True by begin_close(), cleared by
        # update_trade_exit() or set_trade_status().
        self._sync_locked = False
        #: Field names this process has written WITHOUT bumping `version`.
        #:
        #: `update_trade_fields` saves local-only and does not bump the
        #: counter -- by design, since the trail state it carries "changes
        #: every few seconds" and versioning it would churn Drive on every
        #: poll. The consequence nobody wired up: local and Drive then hold
        #: the SAME version with DIFFERENT content, which `resolve_merge`
        #: reads as a split brain. That is a CRITICAL log line plus a
        #: MERGE_CONFLICT marker plus an hourly Telegram, once per open
        #: position per sync, for as long as the position is open.
        #:
        #: zebra solved this by handing `resolve_merge` the allowlist its
        #: batched writer already owned. The BCS family has no such list --
        #: `update_trade_fields` takes `**fields`, and three call sites pass
        #: computed keys (`**{kind.field: res}`, `**latch['patch']`, `**gap`)
        #: -- so a hand-maintained copy would drift on the next field added.
        #: Recording what was actually written keeps ONE source.
        #:
        #: Process-local, and that is the honest scope: it names the fields
        #: whose local value is knowingly ahead of Drive. A field written
        #: unversioned by the OTHER replica still reports a conflict, which
        #: is the safe direction -- it announces rather than hides.
        self._unversioned_written: set = set()

    def _lock_path(self) -> Path:
        """Resolved at call time, not import time.

        `_save_local` and `_read_local` read the module global, so a
        test that redirects the store by patching LOCAL_TRADES_FILE
        must move the lock with it. A class attribute would freeze the
        lock in the real logs/ directory while the data went to
        tmp_path -- locked, but not against the writer that matters.
        """
        M = self._mod()
        return M.LOCK_FILE

    def _data_path(self) -> Path:
        """Same call-time resolution as _lock_path, for the same reason."""
        M = self._mod()
        return M.LOCAL_TRADES_FILE

    def initialize(self):
        """Startup: auth Drive, download fresh copy, fall back to local."""
        drive_cfg = self._config.get('google_drive', {})

        if drive_cfg.get('enabled', False):
            self._init_drive(drive_cfg)

        if self._drive_enabled:
            self._sync_from_drive()
        else:
            self._load_local()

        self._migrate_trades()

        open_count = sum(1 for t in self._trades if t.get('status') == 'open')
        self._logger.info(
            "%s initialized: %d trades (%d open), drive=%s",
            type(self).__name__, len(self._trades), open_count,
            'enabled' if self._drive_enabled else 'disabled'
        )

    def _init_drive(self, drive_cfg: dict):
        """Authenticate with Google Drive. Sets _drive_enabled on success."""
        creds_path = self._mod()._resolve_credentials_path(self._config)
        if not creds_path or not creds_path.exists():
            self._logger.warning(
                "Drive credentials not found at %s, running local-only", creds_path
            )
            return

        try:
            self._drive_service = self._mod().drive_store.get_drive_service(creds_path)
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('file_name', 'bcs_trades.json')
            self._drive_file_id = self._mod().drive_store.find_file(
                self._drive_service, folder_id, file_name
            )
            self._drive_enabled = True
            self._logger.info(
                "Drive connected. File ID: %s",
                self._drive_file_id or '(will create on first write)'
            )
        except Exception as e:
            self._logger.warning("Drive init failed: %s. Running local-only.", e)

    def _sync_from_drive(self):
        """Download from Drive, then merge the result under the store lock.

        The download is a NETWORK call and stays OUTSIDE the lock. Holding a
        cross-process mutex across an HTTP round-trip would stall the other
        process's whole cycle -- and this runs every poll on the live monitor.

        Merge strategy (per trade ID, higher version wins):
          - Protects against lost trades when a prior Drive upload failed
          - Picks up new trades added from other machines
          - If merge differs from Drive, re-uploads to Drive
        """
        if not self._drive_file_id:
            self._logger.info("No file on Drive yet, loading local")
            self._load_local()
            return
        try:
            drive_trades = self._mod().drive_store.download_json(
                self._drive_service, self._drive_file_id
            )
            # VALIDATE THE REMOTE SIDE TOO (fixed 2026-08-31).
            #
            # `partition_readable` guarded the LOCAL read and nothing guarded
            # this one -- the input an outside writer can actually poison. One
            # Drive record with a missing `id`, or a non-dict, made
            # `_merge_trades` raise `KeyError`/`TypeError` inside the blanket
            # `except` below, which falls back to `_load_local()`. Every sync,
            # forever: Drive is silently no longer a second copy.
            #
            # Worse, local writes kept calling `_upload_to_drive`, which
            # REPLACES the Drive file wholesale with a book that never absorbed
            # the bad record. If that record was a real trade from another
            # machine it is then gone from Drive with no quarantine, no
            # sidecar and no alert -- only a "Drive download failed" warning
            # that reads as transient.
            #
            # Unreadable remote records are held out of the merge and REPORTED
            # rather than dropped, the same asymmetry the local path uses:
            # never quietly go on with a short book.
            drive_trades, unreadable = store_contract.partition_readable(
                drive_trades, log=self._logger.warning)
            if unreadable:
                detail = '; '.join(str(u.get('why')) for u in unreadable[:4])
                self._logger.critical(
                    '%d unreadable record(s) on Drive were held OUT of the '
                    'merge: %s', len(unreadable), detail)
                self._flag_corruption(
                    '%d unreadable record(s) in the Drive copy of %s (%s). '
                    'They are not in the local book and a local write will '
                    'overwrite them on Drive.'
                    % (len(unreadable), self._data_path().name, detail),
                    backup=None,
                    kind=store_contract.MARKER_MERGE_CONFLICT)
            diverged = False
            with self._mutate(drive=False):
                # _mutate has already refreshed self._trades from disk, so the
                # old `base = self._trades if self._trades else _read_local()`
                # special case is gone: disk is always the base now.
                self._trades = self._merge_trades(self._trades, drive_trades)
                drive_versions = {t['id']: t.get('version', 0) for t in drive_trades}
                merged_versions = {t['id']: t.get('version', 0) for t in self._trades}
                diverged = drive_versions != merged_versions
            self._last_sync_time = time.time()
            if diverged:
                self._logger.info("Merge diverged from Drive, re-uploading")
                self._upload_to_drive()
        except LockTimeout as e:
            # Another process holds the store. The cache stays as it is and the
            # next poll retries -- a missed refresh is not a reason to abandon
            # the poll, which is what raising here would do.
            self._logger.warning("Drive merge skipped, store busy: %s", e)
        except Exception as e:
            self._logger.warning("Drive download failed: %s. Using local file.", e)
            self._load_local()

    def _upload_to_drive(self):
        """Upload current trades to Drive. Logs error on failure (local already saved)."""
        if not self._drive_enabled:
            return

        try:
            drive_cfg = self._config.get('google_drive', {})
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('file_name', 'bcs_trades.json')
            self._drive_file_id = self._mod().drive_store.upload_json(
                self._drive_service, folder_id, file_name,
                self._trades, self._drive_file_id
            )
            self._last_sync_time = time.time()
        except Exception as e:
            self._logger.error("Drive upload failed: %s. Local file is safe.", e)

    def _read_local(self) -> list:
        """Read trades from local JSON file without setting self._trades.

        Corruption-safe: if the file is truncated/corrupt (e.g. crash during
        write), backs it up and returns empty list rather than crashing.
        """
        M = self._mod()
        if not M.LOCAL_TRADES_FILE.exists():
            return []
        # THE QUARANTINE RENAME IS INSIDE THE LOCK (fixed 2026-09-01).
        #
        # The `with self._read_lock()` used to close around the READ alone, so
        # the decode error propagated out of it and the handler ran UNLOCKED.
        # In that gap the live monitor's `_mutate` could take the lock, notice
        # the same corruption, quarantine it, and rewrite a good book from its
        # warm cache -- and then this handler renamed the GOOD file to
        # `.corrupt.<ts>.json`. A healthy book quarantined, this reader
        # continuing with `[]`, and the marker describing the OLD error while
        # naming a backup that is actually the only good copy.
        #
        # `zebra/trade_store.py` never had this hole: its `_read_local` takes
        # no lock of its own precisely because every caller already holds one
        # across the read AND the handler. This is that property, restored to
        # the shared base. `_read_lock` is re-entrant under `_mutate`, so the
        # in-mutate path is unchanged.
        try:
            with self._read_lock():
                try:
                    with open(M.LOCAL_TRADES_FILE, encoding='utf-8') as f:
                        data = json.load(f)
                    if not isinstance(data, list):
                        raise ValueError(
                            f"Expected list, got {type(data).__name__}")
                    return self._quarantine_unreadable(data)
                except (json.JSONDecodeError, ValueError) as e:
                    # File is corrupt — back it up so we can investigate,
                    # start fresh. Still holding the lock, so no other writer
                    # can repair the file between the diagnosis and the rename.
                    backup = M.LOCAL_TRADES_FILE.with_suffix(
                        f'.corrupt.{int(time.time())}.json'
                    )
                    try:
                        M.LOCAL_TRADES_FILE.rename(backup)
                    except OSError:
                        pass  # Can't rename — at least don't crash
                    self._logger.critical(
                        "Trade file CORRUPT (%s). Backed up to %s. "
                        "Starting empty!", e, backup)
                    # B7: a log line is not an alert. The monitor turns this
                    # marker into a Telegram BEFORE it concludes "all trades
                    # closed" and stops watching every open position.
                    self._flag_corruption(str(e), backup)
                    return []
        except LockTimeout:
            # Could not take the lock at all. Returning [] here would look
            # exactly like an empty book to every caller, so raise instead --
            # `_mutate` and `_sync_from_drive` both already treat a
            # LockTimeout as "do not write, retry next poll".
            raise

    def _statuses(self):
        """This book's status vocabulary, for `store_contract`.

        The BCS family says open/closed; zebra says entered/exited. Comparing
        raw strings across the two would compare the wrong things, which is
        why the contract is stated in ROLES.
        """
        return store_contract.BCS_FAMILY_STATUSES

    def _quarantine_unreadable(self, data: list) -> list:
        """Drop records the merge cannot survive, preserving and alerting.

        `_merge_trades` indexes on `t['id']` and compares `version` before any
        caller code runs, on EVERY write path. One record with a missing id or
        a string version therefore made every write on this book raise --
        exits included -- and nothing above quarantined it, because the file
        parsed as JSON perfectly well. See `common.store_contract` for the
        manufacture paths and for why repair is preferred to dropping.

        The quarantined records are written beside the book rather than
        discarded: a record this store cannot read may still be the only
        evidence of a real position.
        """
        good, bad = store_contract.partition_readable(
            data, log=lambda f, *a: self._logger.warning(f, *a))
        if not bad:
            return good
        M = self._mod()
        path = M.LOCAL_TRADES_FILE.with_suffix(
            f'.unreadable.{int(time.time())}.json')
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(bad, f, indent=2, default=str)
        except OSError:
            path = None            # never let preservation break the read
        self._logger.critical(
            "%d unreadable record(s) held out of the book: %s. Preserved at "
            "%s.", len(bad), '; '.join(b['why'] for b in bad), path)
        self._flag_corruption(
            '%d unreadable record(s): %s'
            % (len(bad), '; '.join(b['why'] for b in bad)), path)
        return good

    def _load_local(self):
        """Load trades from local JSON file into cache."""
        self._trades = self._read_local()
        if self._trades:
            self._logger.info("Loaded %d trades from local file", len(self._trades))
        else:
            self._logger.info("No local trades file found, starting empty")

    def _merge_trades(self, base: list, incoming: list,
                      same_replica: bool = False) -> list:
        """Merge two trade lists. Per trade ID, higher version wins.

        This protects against data loss when:
          - A Drive upload failed (base has data incoming doesn't)
          - A new trade was added on another machine (incoming has data base doesn't)
          - A trade was closed locally but Drive still shows it open

        `same_replica=True` means the two sides are this box's DISK and this
        process's own CACHE, not two machines. An equal-version difference
        there is a sibling process's concurrent write -- exactly what the
        refresh exists to absorb -- and not a split brain. zebra was given
        this on 2026-08-31 after the detector's first live session produced 11
        CRITICAL lines and 4 false corruption alerts against a fully intact
        book; the shared base under the two live-money books never got it, so
        the same false alarm was still armed for `bcs_trades.json` the moment a
        second writer touched it.
        """
        by_id: dict = {}
        notes: list = []

        for t in base:
            by_id[t['id']] = t

        for t in incoming:
            tid = t['id']
            if tid not in by_id:
                # New trade from the other side
                by_id[tid] = t
                continue
            # Version alone used to decide this, and a version is a
            # per-replica counter, not a conflict detector -- so a booked exit
            # could be walked back by a replica that had merely bumped a
            # couple of alert flags. See `store_contract.resolve_merge`.
            winner, note = store_contract.resolve_merge(
                by_id[tid], t, self._statuses(),
                same_replica=same_replica,
                unversioned_fields=self._unversioned_written)
            by_id[tid] = winner
            if note:
                notes.append(note)

        for note in notes:
            self._logger.critical('MERGE: %s', note)
        if notes:
            # A log line is not an alert, and these are states no operator can
            # infer from the book itself -- the two replicas simply differ.
            # Flagged as MERGE_CONFLICT, not QUARANTINE: the book is intact and
            # still readable here, so the alert must not claim it went empty
            # and must not make the monitor treat an empty book as suspicious.
            self._flag_corruption(
                '%d merge conflict(s): %s' % (len(notes), ' | '.join(notes)),
                None, kind=store_contract.MARKER_MERGE_CONFLICT)

        merged = sorted(by_id.values(), key=lambda t: t['id'])

        if len(merged) != len(base) or len(merged) != len(incoming):
            self._logger.warning(
                "Trade merge: base=%d + incoming=%d -> merged=%d",
                len(base), len(incoming), len(merged)
            )

        return merged

    def _save_local(self):
        """Write trades to local JSON file (atomic).

        Writes to a temp file first, then does os.replace() which is atomic
        on both Windows (NTFS) and Linux (same filesystem). This prevents
        data corruption if the process is killed mid-write.
        """
        M = self._mod()
        M.LOG_DIR.mkdir(exist_ok=True)
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(M.LOG_DIR), suffix='.tmp', prefix='bcs_trades_'
            )
            with os.fdopen(fd, 'w') as f:
                fd = None  # os.fdopen takes ownership of fd
                json.dump(self._trades, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            # POSIX rename is indifferent to open handles, but on Windows a
            # reader holding the destination open makes this fail OUTRIGHT.
            # Every caller here is under the store lock, so a collision means a
            # stray UNLOCKED reader -- an editor, a backup tool, an ad-hoc
            # script using plain `open()`. Retry briefly rather than lose the
            # write: the BCS and Fallen Hero books are hand-captured on
            # Windows, so that reader is a realistic Tuesday afternoon.
            # `zebra/trade_store.py` has had this since it was written; the
            # shared base under the two live-money books never got the copy.
            for attempt in range(5):
                try:
                    os.replace(tmp_path, str(M.LOCAL_TRADES_FILE))
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
            tmp_path = None  # replaced successfully, don't clean up
        except Exception:
            # Clean up temp file on failure
            if fd is not None:
                os.close(fd)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise

    def load_trades(self) -> list:
        """Return all trades from in-memory cache. No network."""
        return self._trades

    def get_open_trades(self) -> list:
        """Return only open trades from cache."""
        return [t for t in self._trades if t.get('status') == 'open']

    def get_closing_trades(self) -> list:
        """Return trades stuck in 'closing' status (from a crashed close attempt)."""
        return [t for t in self._trades if t.get('status') == 'closing']

    def recover_closing_trade(self, trade_id: int):
        """Reset a 'closing' trade back to 'open' for re-monitoring.

        Used at startup to recover trades left in 'closing' from a previous crash.
        """
        with self._mutate():
            for t in self._trades:
                # The table, not a literal. See `common/store_contract.py`.
                if t['id'] == trade_id and store_contract.allows(
                        store_contract.RECOVER_CLOSING,
                        t.get('status')):
                    t['status'] = 'open'
                    t['version'] = t.get('version', 0) + 1
                    t.pop('close_reason', None)
                    t.pop('close_started', None)
                    self._sync_locked = False
                    self._logger.warning("Trade #%d recovered from 'closing' -> 'open'", trade_id)
                    return True
            return False

    def find_open_trade(self, stock: str, trade_id: Optional[int] = None) -> Optional[dict]:
        """Find an open trade by stock name, optionally filtered by trade ID."""
        for t in self._trades:
            if t['status'] != 'open':
                continue
            if trade_id is not None and t['id'] != trade_id:
                continue
            if t['stock'].upper() == stock.upper():
                return t
        return None

    def next_trade_id(self) -> int:
        """Allocate the next trade ID.

        Delegates to the mixin, which also consults a monotonic
        high-water sidecar — `max(live) + 1` reissues ids 1, 2, 3
        after a quarantine empties the book, and a reissued id is not
        a cosmetic problem: `_merge_trades` resolves by id, so the
        Drive copy of the original trade and the new one become the
        same record and the higher version silently wins.

        Not a pure read: it advances the sidecar. Its only caller is
        `add_trade`, inside the lock, which is where allocation has to
        happen anyway.
        """
        return self.allocate_id()

    def update_trade_exit(self, trade_id: int, exit_data: dict):
        """Mark a trade as closed with exit details. Saves local + Drive.

        THE SYNC LOCK IS RELEASED ON EVERY EXIT PATH (fixed 2026-08-31).
        `begin_close` sets `_sync_locked` to stop a Drive pull overwriting the
        close in flight, and the only release used to sit at the BOTTOM of the
        loop below -- after the contract refusal `raise`, and after anything
        `_mutate` itself can throw (`LockTimeout`). Either one left the flag
        stuck True, which makes `maybe_sync` return early for the rest of the
        process. On the long-lived monitor that is the whole session running
        local-only, with no log line saying so: Drive silently stops being a
        second copy of the money book at exactly the moment a close went wrong.
        """
        try:
            return self._update_trade_exit(trade_id, exit_data)
        finally:
            self._sync_locked = False

    def _update_trade_exit(self, trade_id: int, exit_data: dict):
        with self._mutate():
            found = False
            for t in self._trades:
                if t['id'] == trade_id:
                    # THE CONTRACT, enforced rather than described.
                    # `common/store_contract.py`. Until 2026-08-30 this method
                    # had no status check at all: it would stamp 'closed' onto
                    # a record that was already closed, or onto one FROZEN at
                    # `partial_close` with legs live at the broker. The only
                    # thing between that and a real order was `begin_close`.
                    # The divergence from `ZebraStore` was recorded in a test
                    # named after it and left standing, which is what a
                    # specification nothing consults always becomes.
                    #
                    # RAISES rather than returning: every caller has ALREADY
                    # placed the closing orders, so a silent no-op would leave
                    # the position flat at the broker and open in the book. A
                    # refusal here is a bug upstream and has to be loud.
                    if not store_contract.allows(
                            store_contract.UPDATE_TRADE_EXIT, t['status']):
                        raise ValueError(store_contract.refusal(
                            store_contract.UPDATE_TRADE_EXIT, t['status'],
                            trade_id))
                    exit_data = self._bound_exit(t, exit_data)
                    t['status'] = 'closed'
                    t['exit'] = exit_data
                    # Surfaced at the TOP LEVEL as well as inside `exit`, for
                    # the same reason the FH store does it: every reader of
                    # this book scans trade dicts, and a marker findable only
                    # by opening a nested dict is one most readers will miss.
                    # Absent means exact; the key is never written False.
                    if exit_data.get('pnl_approximate'):
                        t['exit_approximate'] = True
                    t['version'] = t.get('version', 0) + 1
                    found = True
                    break

            self._sync_locked = False  # Always release lock, even if trade not found

            if not found:
                self._logger.error("Trade #%d not found for exit update", trade_id)
                return
            self._logger.info("Trade #%d closed: %s", trade_id, exit_data.get('exit_reason', ''))

    def begin_close(self, trade_id: int, reason: str) -> bool:
        """Acquire close-lock on a trade. Returns True if lock acquired.

        Sets status to 'closing' with version bump. Saves to local + Drive
        immediately so other machines see the lock before placing orders.
        Prevents the 2x close-order bug from concurrent processes.

        Returns False if trade is not 'open' (already closing/closed).
        """
        with self._mutate():
            for t in self._trades:
                if t['id'] == trade_id:
                    # The table, not a literal. See `common/store_contract.py`
                    # for why the specification moved out of the test that held it.
                    if not store_contract.allows(
                            store_contract.BEGIN_CLOSE, t['status']):
                        self._logger.warning(
                            "Trade #%d status is '%s', cannot begin close",
                            trade_id, t['status']
                        )
                        return False
                    t['status'] = 'closing'
                    t['close_reason'] = reason
                    t['close_started'] = datetime.now().isoformat()
                    t['version'] = t.get('version', 0) + 1
                    self._sync_locked = True
                    self._logger.info(
                        "Trade #%d close-lock acquired: %s", trade_id, reason
                    )
                    return True
            self._logger.error("Trade #%d not found for begin_close", trade_id)
            return False

    def begin_recovery(self, trade_id: int, reason: str) -> bool:
        """M14 - take the close-lock on a FROZEN record so recovery can finish it.

        The ONE door out of `partial_close`, and a separate door on purpose.

        **Do not overload `begin_close`.** Its `status == 'open'` check is the
        concurrency lock that stops two processes both closing the same trade -
        the 2x-order shape that cost real money in Feb 2026. Widening it to
        also accept `partial_close` would weaken that guarantee for every
        ordinary close in order to serve a rare one.

        Refuses every other state, and each refusal earns its own reasoning:
        `'open'` was never frozen and belongs to `begin_close`; `closing`
        means an attempt is already in flight, and a second would be the 2x
        order again; `'closed'` is terminal, and ordering on a booked
        record is what S3 exists to forbid.

        Returns False rather than raising - "somebody else got there first" is
        an ordinary answer on a shared store, and the caller branches on it.
        """
        with self._mutate():
            for t in self._trades:
                if t['id'] != trade_id:
                    continue
                # The table, not a literal. `begin_recovery` is the
                # exact inverse of `begin_close`, and stating that as
                # data is the point of it being its own verb.
                if not store_contract.allows(
                        store_contract.BEGIN_RECOVERY, t['status']):
                    self._logger.warning(
                        "Trade #%d status is '%s', cannot begin recovery",
                        trade_id, t['status'])
                    return False
                t['status'] = 'closing'
                t['close_reason'] = reason
                t['close_started'] = datetime.now().isoformat()
                t['version'] = t.get('version', 0) + 1
                self._sync_locked = True
                self._logger.info("Trade #%d recovery-lock acquired: %s",
                            trade_id, reason)
                return True
            self._logger.error("Trade #%d not found for begin_recovery", trade_id)
            return False

    def get_frozen_trades(self) -> list:
        """Records stuck at `partial_close` - live legs, nothing monitoring them.

        This is the state a close lands in when a leg failed AFTER orders went
        out. It drops out of the open book, so before M14 nothing retried it,
        nothing re-alerted, and one Telegram at freeze time was the entire
        lifecycle of a position that may be live at the broker with its stops
        dead. That is the unwatched-position failure that has cost this account
        real money twice.

        Read-only. No caller may treat these as open positions.
        """
        return [t for t in self._trades if t.get('status') == 'partial_close']

    def get_residue_trades(self) -> list:
        """S3 - records BOOKED CLOSED that still show a live leg at the broker.

        `reconcile_after_close` reads the broker's own view after a close
        reports success. When it finds a leg that is not flat, the record is
        already `closed`: it is out of the open book, out of
        `get_frozen_trades()`, and out of every sweep there is. Before this
        method the entire lifecycle of that fact was ONE Telegram — the same
        invisible-position shape M14 exists to end, one door over.

        Deliberately NOT the frozen list. A frozen record has a watcher (the
        recovery sweep) and a nag of its own; a second one for the same
        position would be noise. This names only the records nothing else can
        see, which is why the status filter is part of the query rather than
        left to the caller.

        Read-only, and terminal: no caller may place an order on the strength
        of this list. The record is closed — there is no close lock to take,
        no stop to re-arm, and the residue may be a leg the owner is holding
        on purpose. Escalate, never act.
        """
        return [t for t in self._trades
                if t.get('status') == 'closed'
                and (t.get('reconcile_residue') or {}).get('state') == 'open']

    def get_entry_residue_trades(self) -> list:
        """Records carrying an ENTRY residue — a leg an entry left behind.

        The entry-side twin of `get_residue_trades`, and it exists for the
        same reason: `bcs/entry_executor.py` never unwinds an orphan leg (a
        corrective order through the book that just failed to fill is how a
        Feb-2026 stop became a four-fill loss), so it REPORTS one and stops.
        Until this method existed "reports" meant one Telegram: the orphan was
        in no store, so the frozen sweep, the residue sweep, the startup
        verification and `--list` all missed it, because every one of them
        reads RECORDS.

        NO status filter, and that is the difference from the post-close twin.
        An entry residue can sit on a record in any state: `open` when the
        round completed some spreads and orphaned a leg, or the pre-entry
        state when nothing filled at all and the record never became a
        position. The incident, not the status, is the query.

        Read-only and escalate-only. No caller may place an order on the
        strength of this list.
        """
        return [t for t in self._trades
                if (t.get('entry_residue') or {}).get('state') == 'open']

    def set_trade_status(self, trade_id: int, status: str, **extra_fields):
        """Update trade status and optional extra fields. Saves local + Drive.

        Used for state transitions: open → closing → closed / partial_close.
        """
        # A TERMINAL RECORD IS NOT REOPENED BY A SETTER (added 2026-09-01).
        #
        # This accepted ANY transition, including `closed -> open`, and the
        # merge's monotonic-close rule never sees it: an ordinary versioned
        # local write is exactly what that rule treats as legitimate. So one
        # call resurrects a booked trade with full version legitimacy, and the
        # monitor then manages a position that is already flat at the broker.
        #
        # `partial_close` stays reachable -- it is how the close path freezes a
        # half-done exit, and `store_contract` names it FROZEN rather than
        # terminal. What is refused is leaving a TERMINAL state, which the
        # contract in this same package already says nothing may do: "TERMINAL
        # refuses everything because that is what idempotence IS."
        # THE SYNC LOCK IS RELEASED ON THE RAISE PATH TOO. `update_trade_exit`
        # got a `try/finally` for exactly this a few lines up, and this method
        # -- which the SAME commit taught to raise -- did not. `begin_close` /
        # `begin_recovery` set `_sync_locked`; if a sibling process books the
        # record `closed` in the race window, `_mutate`'s refresh absorbs that
        # write and the refusal below fires. M14's `_return_to_partial_close`
        # catches and only logs, so Drive sync on a live-money book would be
        # silently off for the rest of the monitor session. Releasing on a
        # refusal is safe: the record is terminal, so there is no close in
        # flight left to protect.
        #
        # `extra_fields` is guarded like `update_trade_fields`, minus `status`,
        # which this verb owns. It writes AFTER the version bump, so
        # `set_trade_status(id, 'closing', version=99)` would have clobbered
        # the counter the merge resolves on.
        blocked = {'id', 'version', 'exit'} & set(extra_fields)
        if blocked:
            raise ValueError(
                "set_trade_status may not write %s via extra_fields — those "
                "are the keys the merge resolves on." % sorted(blocked))
        try:
            self._set_trade_status(trade_id, status, **extra_fields)
        finally:
            self._sync_locked = False

    def _set_trade_status(self, trade_id: int, status: str, **extra_fields):
        with self._mutate():
            for t in self._trades:
                if t['id'] == trade_id:
                    if (store_contract.role_of(t.get('status'),
                                               self._statuses())
                            == store_contract.TERMINAL
                            and status != t.get('status')):
                        raise ValueError(
                            "Trade #%d is %s (terminal); set_trade_status may "
                            "not move it to %r. A booked exit is undone by "
                            "`restore_snapshot`, deliberately and visibly, "
                            "never by a field setter."
                            % (trade_id, t.get('status'), status))
                    t['status'] = status
                    t['version'] = t.get('version', 0) + 1
                    for k, v in extra_fields.items():
                        t[k] = v
                    self._logger.info("Trade #%d status -> %s", trade_id, status)
                    return
            self._logger.error("Trade #%d not found for set_trade_status", trade_id)

    def update_trade_fields(self, trade_id: int, **fields):
        """Update arbitrary fields on a trade. Saves local only (no Drive upload).

        Used for lightweight state updates like trailing SL that change every
        few seconds — Drive sync happens on the normal maybe_sync() cycle.
        """
        # THE KEYS THIS MAY NOT WRITE. `zebra/trade_store.py` refuses `status`
        # here and the shared base did not, so the money books had a second,
        # unguarded door onto the state machine -- plus `id` and `version`,
        # either of which silently breaks the merge that keys on them.
        blocked = {'status', 'id', 'version', 'exit'} & set(fields)
        if blocked:
            raise ValueError(
                "update_trade_fields may not write %s — use the verb that "
                "owns that transition (set_trade_status / update_trade_exit). "
                "This method exists for advisory state and does not consult "
                "the store contract." % sorted(blocked))
        with self._mutate(drive=False):
            for t in self._trades:
                if t['id'] == trade_id:
                    for k, v in fields.items():
                        t[k] = v
                    # Declare what was written without a version bump, so the
                    # merge can tell a deliberate local-only write from a
                    # split brain. See `_unversioned_written`.
                    self._unversioned_written.update(fields)
                    return True
            return False

    def maybe_sync(self, force: bool = False):
        """Re-sync from Drive if stale. Called from monitor loop.

        Only hits network if sync_interval_sec has elapsed (default 300s),
        or if force=True. Skipped entirely when _sync_locked is set
        (during close operations) to prevent overwriting in-progress state.
        """
        if not self._drive_enabled:
            return
        if self._sync_locked:
            return

        elapsed = time.time() - self._last_sync_time
        if force or elapsed >= self._sync_interval:
            self._sync_from_drive()
