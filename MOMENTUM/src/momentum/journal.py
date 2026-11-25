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
from typing import Any, Dict, Iterator, List, Optional

from . import util

RECORD_TYPES = frozenset({
    "BOOK_OPEN", "PLAN_FINAL", "PLAN_STATUS", "AUDIT_FLAG", "AUDIT_RESULT", "OWNER_TAP", "OWNER_CMD", "OVERLAY", "LEG",
    "VET", "STEP_START", "STEP_END", "INTENT", "SENT", "RESULT", "MODIFY_INTENT", "MODIFY_SENT", "MODIFY_RESULT",
    "CANCEL_INTENT", "CANCEL_SENT", "CANCEL_RESULT", "ORDER_STATUS", "FILL", "ORPHAN", "HOLDINGS_SNAPSHOT",
    "SESSION_USED", "ALERT", "NOTE", "APPROVAL_OPEN", "STAGE",
})


class JournalError(Exception):
    pass


class LockBusy(Exception):
    pass


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

    def append(self, type_: str, **fields: Any) -> Dict[str, Any]:
        t = ("DRY_" + type_) if self.dry and not type_.startswith("DRY_") else type_
        base = t[4:] if t.startswith("DRY_") else t
        if base not in RECORD_TYPES:
            raise JournalError(f"unknown record type {type_}")
        rec = {"seq": self._seq + 1, "ts": util.now().isoformat(timespec="seconds"), "type": t}
        for k in fields:
            if k in rec:
                raise JournalError(f"field {k} is reserved")
        rec.update(fields)
        line = json.dumps(rec, sort_keys=True, ensure_ascii=False)
        if "\n" in line:
            raise JournalError("record serialises with a newline")
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._seq += 1
        return rec


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
