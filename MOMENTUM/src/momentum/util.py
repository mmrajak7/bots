"""Small shared helpers: IST clock, canonical JSON + SHA-256, durable file writes.

Design refs: build spec §G (canonical JSON for hashes), §C10 (temp + fsync + rename + dir fsync).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")

# Tests replace this to run the engine on a fixed clock.
_clock: Optional[Callable[[], dt.datetime]] = None


def now() -> dt.datetime:
    """Current time in IST (timezone-aware). Overridable for tests via set_clock."""
    return _clock() if _clock is not None else dt.datetime.now(IST)


def set_clock(fn: Optional[Callable[[], dt.datetime]]) -> None:
    global _clock
    _clock = fn


def today() -> dt.date:
    return now().date()


def canonical(obj: Any) -> str:
    """Canonical JSON: sorted keys, no spaces. Floats are refused so a hash never depends on float formatting."""
    def check(o: Any) -> None:
        if isinstance(o, float):
            raise TypeError(f"float in hashed content: {o!r} (use integers / strings)")
        if isinstance(o, dict):
            for k, v in o.items():
                if not isinstance(k, str):
                    raise TypeError(f"non-string key {k!r}")
                check(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                check(v)
    check(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_obj(obj: Any) -> str:
    return sha256_text(canonical(obj))


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(d: Path) -> None:
    if os.name != "posix":          # Windows (dev PC): directory fsync is not available
        return
    fd = os.open(str(d), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(p: Path, text: str) -> None:
    """Write text so a crash leaves either the old or the new file, never a torn one."""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        _fsync_dir(p.parent)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_json_atomic(p: Path, obj: Any) -> None:
    write_atomic(p, json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


def write_once(p: Path, text: str) -> None:
    """Write-once file (decision files, frozen bands): an existing file with different content is an error."""
    if p.exists():
        if p.read_text(encoding="utf-8") == text:
            return
        raise FileExistsError(f"write-once file already exists with different content: {p}")
    write_atomic(p, text)


def parse_hhmm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def at_today(hhmm: str) -> dt.datetime:
    t = parse_hhmm(hhmm)
    d = today()
    return dt.datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=IST)
