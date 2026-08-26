"""Append-only record of every order this process INTENDED to place.

What this is for
----------------
`feedback_live_automation_bar` ends with "alert-only first", and automated
ENTRY does not exist, so that line binds only the exit path. The concrete form
agreed in the go-live plan is a `--dry-run` cron for one full session against
the live book, comparing what it WOULD have placed against what the paper
system booked.

That comparison had nothing to read. A dry run announces itself as prose in the
session log —

    [DRY RUN] BUY NHPC26AUG86CE x 5400 @ 0.35 -> DRY_101533412

— which a person can follow and a diff cannot. This module writes the same
event as one JSON object per line, with the context needed to answer *why*
that order: the trade, the leg, the trigger, the book it priced off, spot.

Why it journals LIVE orders too
-------------------------------
An artifact that exists only in dry run makes the two modes incomparable — you
could never check that arming changed nothing but the mode flag. It also
leaves the live path with no order-intent record at all: today a real order
exists in prose and at the broker, and nowhere that survives a crash in a form
you can join on.

Intent BEFORE, result AFTER, two lines sharing an `intent_id`
-------------------------------------------------------------
The intent line is written before the broker call. If the process dies during
`place_order` — network drop, token expiry, a kill — the intent line stands
alone, and **a line with no result is the signal**: an order may exist at the
broker that this system does not know about. That is the exact forensic
question after the Feb-2026 incident, and writing the record after the call
would have destroyed it. Same discipline as
`feedback_journal_the_refusal`: a log-then-apply flow must stamp whether the
apply succeeded, or the log becomes evidence of something that never happened.

Never blocks an order
---------------------
`record_intent` and `record_result` absorb everything. A full disk, a
permissions error or a serialisation bug must not stop a stop-loss from being
placed — this is a witness, not a gate. Failures are reported through the
caller's own `log` and swallowed.

Exactly ONE layer does that, at the boundary the order path calls; `_append`
raises. An earlier version wrapped both, and a mutation run showed the inner
one was indistinguishable from its absence.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

LOG_DIR = Path(__file__).parent.parent / 'logs'

#: Monotonic within a process; combined with the pid it is unique across the
#: concurrent runs flock is supposed to prevent but does not guarantee.
_counter = 0
_lock = threading.Lock()


def journal_path(day: Optional[str] = None) -> Path:
    """One file per day, matching the cron log's date stamp.

    Dated rather than rotated: cron owns the redirect fd for the session log
    and Python cannot rotate that, so the two would drift apart under any
    scheme where only one of them rolls.
    """
    day = day or datetime.now().strftime('%Y%m%d')
    return LOG_DIR / f'order_intents_{day}.jsonl'


def next_intent_id() -> str:
    global _counter
    with _lock:
        _counter += 1
        return f'{os.getpid()}-{_counter}'


def _quiet(log, e) -> None:
    """Report a journal failure through the caller's own logger and continue.

    Never re-raises. `place_limit_order` calls this module on the path to a
    stop-loss; a witness that can abort the thing it witnesses is worse than
    no witness.
    """
    if log:
        try:
            log(f"    WARNING: order journal write failed ({e}) — the order "
                f"itself is unaffected")
        except Exception:
            pass


def _append(record: Dict[str, Any], log=None) -> None:
    """Write one line. RAISES -- the caller is where failures are absorbed.

    This had its own try/except until a mutation run showed the two were
    indistinguishable: removing this one alone changed nothing observable,
    because `record_intent` and `record_result` always caught first. A guard
    nobody can tell from its absence is decorative, and two of them invite
    the reader to assume defence in depth that is not there. One guard, at
    the boundary the order path actually calls.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str, sort_keys=True)
    # One complete line per write, opened in append mode: the kernel keeps
    # concurrent appends of a short line from interleaving. No lock file --
    # taking one here would put a filesystem wait in front of a stop-loss.
    with open(journal_path(), 'a', encoding='utf-8') as f:
        f.write(line + '\n')
        f.flush()


def record_intent(*, symbol: str, txn_type: str, qty: int, price: float,
                  exchange: str, dry_run: bool, context: Optional[dict] = None,
                  log=None) -> str:
    """Write the INTENT line. Returns the `intent_id` to stamp the result with.

    `context` is whatever the caller knows about why: trade id, leg, trigger
    reason, the two legs' book, spot. Free-form on purpose — a fixed schema
    here would have to be revised every time the monitor learns a new reason,
    and a missing field is worth more than a delayed one.
    """
    # The id is minted FIRST and returned no matter what follows. A caller
    # that cannot stamp a result because the journal failed would leave the
    # order path branching on journal state, which is the coupling this whole
    # module is written to avoid.
    intent_id = next_intent_id()
    try:
        _append({
            'kind': 'intent',
            'intent_id': intent_id,
            'ts': datetime.now().isoformat(timespec='seconds'),
            'dry_run': bool(dry_run),
            'exchange': exchange,
            'symbol': symbol,
            'txn_type': txn_type,
            'qty': qty,
            'price': price,
            'context': context or {},
        }, log=log)
    except Exception as e:
        # THE guard. `_append` raises on purpose (see its docstring), so this
        # is the only thing standing between a full disk and an abandoned
        # stop-loss. Do not move it inward.
        _quiet(log, e)
    return intent_id


def record_result(intent_id: str, *, order_id: Optional[str] = None,
                  error: Optional[str] = None, dry_run: bool = False,
                  log=None) -> None:
    """Write the RESULT line for an earlier intent.

    An intent with no result means the process did not survive the broker
    call. That is not a gap in the record — it IS the record.
    """
    try:
        _append({
            'kind': 'result',
            'intent_id': intent_id,
            'ts': datetime.now().isoformat(timespec='seconds'),
            'dry_run': bool(dry_run),
            'order_id': order_id,
            'error': error,
        }, log=log)
    except Exception as e:
        _quiet(log, e)


def read_day(day: Optional[str] = None):
    """Parsed records for one day, in file order. Unparseable lines are
    returned as `{'kind': 'corrupt', ...}` rather than dropped — a line this
    file could not write correctly is itself a finding."""
    p = journal_path(day)
    if not p.exists():
        return []
    out = []
    for n, line in enumerate(p.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except Exception as e:
            out.append({'kind': 'corrupt', 'line_no': n, 'raw': line[:200],
                        'error': str(e)})
    return out


def unresolved(day: Optional[str] = None):
    """Intents with no matching result — orders that may exist at the broker
    while this system believes nothing happened."""
    records = read_day(day)
    done = {r.get('intent_id') for r in records if r.get('kind') == 'result'}
    return [r for r in records
            if r.get('kind') == 'intent' and r.get('intent_id') not in done]
