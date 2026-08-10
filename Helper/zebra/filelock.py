"""Cross-process exclusive file lock (POSIX + Windows).

Why this exists
---------------
The trade store is about to have TWO writers: the zebra cron (every 5 min) and
the Claude vetting/review cron (every 5 min), with the Drive re-sync effectively
a third. Unprotected read-modify-write on a JSON file loses data SILENTLY:

    zebra reads  [A,B]
    claude reads [A,B]
    zebra writes [A,B,C]        <- adds trade C
    claude writes[A,B,D]        <- built on the stale read; C is GONE

No exception, no corrupt file, no log line. A trade simply ceases to exist.
`_save_local`'s atomic tmp+rename protects against TORN files, not against this
— it makes each individual write all-or-nothing, which is a different problem.

Equally important is read-check-write atomicity. `set_alert_flag` is
consume-once ("has this exit already fired?"). Two processes racing it can both
observe the flag unset and both act — which is precisely the class of bug that
put 4x BUY orders on the ICICI short leg in Feb.

Implementation notes
--------------------
Uses OS advisory locks (fcntl on POSIX, msvcrt on Windows) rather than a
lockfile-with-PID scheme, because the OS releases these automatically when the
fd closes or the process dies. A crashed cron therefore cannot strand the lock —
which matters on an unattended Pi where nobody is watching to clear it.

Advisory means every writer must cooperate. One unlocked writer defeats the
whole scheme, so all mutation must route through ZebraStore's locked path.
"""

from __future__ import annotations

import errno
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

try:                                        # POSIX (the Pi — what matters)
    import fcntl
    _HAVE_FCNTL = True
except ImportError:                         # Windows (dev + tests)
    fcntl = None
    _HAVE_FCNTL = False

try:
    import msvcrt
    _HAVE_MSVCRT = True
except ImportError:
    msvcrt = None
    _HAVE_MSVCRT = False


DEFAULT_TIMEOUT = 30.0      # generous: critical sections are milliseconds
_POLL_SEC = 0.05


class LockTimeout(RuntimeError):
    """Could not acquire the lock in time — treat as 'do not write'."""


def _try_acquire(fd: int) -> bool:
    """One non-blocking attempt. True if we now hold the exclusive lock."""
    if _HAVE_FCNTL:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
    if _HAVE_MSVCRT:
        try:
            # Locks one byte at the current offset; enough for mutual exclusion
            # since every participant locks the same byte of the same file.
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    # No locking primitive at all: refuse to pretend. Callers rely on this for
    # correctness, so silently degrading to "no lock" would be the worst option.
    raise RuntimeError("No file-locking primitive available on this platform")


def _release(fd: int) -> None:
    try:
        if _HAVE_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif _HAVE_MSVCRT:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError as e:                    # pragma: no cover - best effort
        logger.warning("lock release failed (fd closes anyway): %s", e)


@contextmanager
def exclusive(lock_path: Path, timeout: float = DEFAULT_TIMEOUT) -> Iterator[None]:
    """Hold an exclusive cross-process lock on `lock_path` for the block.

    Raises LockTimeout if it cannot be acquired within `timeout`. The caller
    must treat that as "do not write" — never as "write anyway".

    Keep the block SHORT: load -> mutate -> save. Never hold this across a Kite
    call, a Drive upload, or an LLM run; a slow network call inside the lock
    would stall the other cron's entire cycle.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Opened O_RDWR (not O_TRUNC): the file is a pure lock token, never read.
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while True:
            if _try_acquire(fd):
                acquired = True
                break
            if time.monotonic() >= deadline:
                raise LockTimeout(
                    f"could not lock {lock_path} within {timeout:g}s — "
                    f"another process is holding it"
                )
            time.sleep(_POLL_SEC)
        yield
    finally:
        if acquired:
            _release(fd)
        os.close(fd)
