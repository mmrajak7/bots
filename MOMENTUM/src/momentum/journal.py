"""Append-only journal (the single source of truth) and the process lock.

Build spec §G, §C10; frozen design §7.3 (intent life cycle), E02 (journal before call), E29 (one journal writer).
Every record: {seq, ts, type, ...}; each append is flushed and fsynced before the caller continues, so a crash can
lose at most the record being written (a torn last line is detected and reported, never silently skipped).
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import time
from typing import Any, Dict, Iterator, List, Optional

from . import util

RECORD_TYPES = frozenset({
    "BOOK_OPEN", "PLAN_FINAL", "PLAN_STATUS", "AUDIT_FLAG", "AUDIT_RESULT", "OWNER_TAP", "OWNER_CMD", "OVERLAY", "LEG",
    "VET", "STEP_START", "STEP_END", "INTENT", "SENT", "RESULT", "MODIFY_INTENT", "MODIFY_SENT", "MODIFY_RESULT",
    "CANCEL_INTENT", "CANCEL_SENT", "CANCEL_RESULT", "ORDER_STATUS", "FILL", "ORPHAN", "HOLDINGS_SNAPSHOT",
    "SESSION_USED", "ALERT", "NOTE", "APPROVAL_OPEN", "STAGE",
    "BUY_AMOUNT", "CA_CREDIT", "SELL_SESSION", "GATE",                       # January slice (spec §3, §5)
})


class JournalError(Exception):
    pass


class LockBusy(Exception):
    pass


TAIL_BYTES = 1 << 16            # the last record is found in the file's tail; a longer record falls back to a full read
APPEND_LOCK_WAIT_S = 30         # how long one append waits for another process's append before failing loudly


class Journal:
    def __init__(self, path: Path, dry: bool = False) -> None:
        self.path = path
        self.dry = dry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = self._last_seq()

    def _last_seq(self) -> int:
        recs = self.read()
        return recs[-1]["seq"] if recs else 0

    def read(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out: List[Dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        for i, line in enumerate(lines):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise JournalError(f"{self.path}: line {i + 1} is not valid JSON (torn write?): {e}") from e
            if not isinstance(rec, dict) or "seq" not in rec or "type" not in rec:
                raise JournalError(f"{self.path}: line {i + 1} is not a journal record")
            if out and rec["seq"] != out[-1]["seq"] + 1:
                raise JournalError(f"{self.path}: seq gap at line {i + 1} ({out[-1]['seq']} -> {rec['seq']})")
            out.append(rec)
        return out

    def _tail_seq(self) -> int:
        """seq of the last record ON DISK (another process may have appended since this object was built).
        A torn last line raises: nothing is appended behind a damaged record."""
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return 0
        if size == 0:
            return 0
        with open(self.path, "rb") as f:
            start = max(0, size - TAIL_BYTES)
            f.seek(start)
            chunk = f.read()
        if not chunk.endswith(b"\n"):
            raise JournalError(f"{self.path}: the last line has no line end (torn write?)")
        lines = chunk[:-1].split(b"\n")
        if start > 0 and len(lines) < 2:          # the tail holds only part of one very long record
            recs = self.read()
            return int(recs[-1]["seq"]) if recs else 0
        try:
            return int(json.loads(lines[-1].decode("utf-8"))["seq"])
        except (ValueError, KeyError, TypeError) as e:
            raise JournalError(f"{self.path}: the last line is not a journal record (torn write?): {e}") from e

    def append(self, type_: str, **fields: Any) -> Dict[str, Any]:
        """One record, flushed and fsynced. Safe with more than one process (the 16:00 watch or a hand-run
        kill-cancel beside the order process): the append takes a short file lock and numbers the record from the
        file's real tail, so two writers can never produce the same seq (which would brick every later read)."""
        t = ("DRY_" + type_) if self.dry and not type_.startswith("DRY_") else type_
        base = t[4:] if t.startswith("DRY_") else t
        if base not in RECORD_TYPES:
            raise JournalError(f"unknown record type {type_}")
        for k in fields:
            if k in ("seq", "ts", "type"):
                raise JournalError(f"field {k} is reserved")
        with _append_lock(self.path):
            rec = {"seq": self._tail_seq() + 1, "ts": util.now().isoformat(timespec="seconds"), "type": t}
            rec.update(fields)
            line = json.dumps(rec, sort_keys=True, ensure_ascii=False)
            if "\n" in line:
                raise JournalError("record serialises with a newline")
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._seq = rec["seq"]
        return rec


def repair_torn_tail(path: Path) -> Optional[str]:
    """Owner-run repair (cli `repair-journal`) after a power cut: if the LAST line is not a complete record, move
    that fragment to <journal>.torn.<time> and cut it off. -> the fragment, or None when the journal is whole.
    Safe by the journal-before-call order: an unfinished INTENT or SENT means the broker call was never made; an
    unfinished RESULT leaves the intent SENT, which the reconcile settles by tag. Never touches a complete line."""
    if not path.exists():
        return None
    raw = path.read_bytes()
    if not raw:
        return None
    cut = raw.rfind(b"\n") + 1                   # start of the last (possibly unfinished) line
    tail = raw[cut:]
    if not tail:                                  # ends with a line end: check that the last full line parses
        prev = raw[:-1].rfind(b"\n") + 1
        try:
            rec = json.loads(raw[prev:-1].decode("utf-8"))
            if isinstance(rec, dict) and "seq" in rec and "type" in rec:
                return None
        except ValueError:
            pass
        cut, tail = prev, raw[prev:]
    side = path.with_name(f"{path.name}.torn.{util.now().strftime('%Y%m%dT%H%M%S')}")
    side.write_bytes(tail)
    with open(path, "r+b") as f:
        f.truncate(cut)
        f.flush()
        os.fsync(f.fileno())
    return tail.decode("utf-8", errors="replace")


@contextlib.contextmanager
def _append_lock(journal_path: Path) -> Iterator[None]:
    """Blocking exclusive lock on a side file (never on the journal itself: a Windows byte lock would stop readers)."""
    f = open(journal_path.with_name(journal_path.name + ".lock"), "a+")
    try:
        deadline = time.monotonic() + APPEND_LOCK_WAIT_S
        while True:
            try:
                if os.name == "posix":
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:  # Windows dev PC
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as e:
                if time.monotonic() >= deadline:
                    raise JournalError(f"journal append lock busy for {APPEND_LOCK_WAIT_S} s") from e
                time.sleep(0.05)
        yield
    finally:
        f.close()


@contextlib.contextmanager
def process_lock(lock_dir: Path, name: str) -> Iterator[None]:
    """Exclusive non-blocking lock (flock on the Pi). A second run of a step exits instead of running twice."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    p = lock_dir / f"{name}.lock"
    f = open(p, "a+")
    try:
        if os.name == "posix":
            import fcntl
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise LockBusy(name) from e
        else:  # Windows dev PC
            import msvcrt
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                raise LockBusy(name) from e
        yield
    finally:
        f.close()


def find(recs: List[Dict[str, Any]], type_: str, **match: Any) -> List[Dict[str, Any]]:
    return [r for r in recs if r["type"] == type_ and all(r.get(k) == v for k, v in match.items())]


def last(recs: List[Dict[str, Any]], type_: str, **match: Any) -> Optional[Dict[str, Any]]:
    xs = find(recs, type_, **match)
    return xs[-1] if xs else None
