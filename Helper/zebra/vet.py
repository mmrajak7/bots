"""Claude vetting layer — state machine for entry decisions.

Flow
----
    zebra cron: signal triggers
      -> request_entry_vet(): mark the signal PENDING, spawn the Claude CLI
         detached, return immediately (the trading loop never waits)
      -> Claude CLI: reads context, researches, then calls
         `python -m zebra vet decide ...` which lands here
      -> next zebra tick: apply_pending() acts on the verdict

Why decoupled
-------------
The entry path runs inside a flock'd cron cycle that is ALSO monitoring every
open position. A 30-90s inline LLM call would stall that monitoring, and a hang
would hold the lock and block subsequent ticks. Spawning detached costs <=5 min
of latency on a hedged, non-HFT structure and makes a Claude failure a
non-event.

Safety invariants
-----------------
1. Claude NEVER writes the store directly. It calls CLI verbs that go through
   the same locked, schema-validated API as everything else, so the vetting
   layer physically cannot corrupt the trade record.
2. **Fail-open to today's behaviour.** No verdict inside the deadline (crash,
   hang, expired auth, Pi reboot) => the signal enters exactly as it does now,
   tagged `unavailable` and EXCLUDED from scoring. A vetting outage must never
   silently become a trading halt, and must never quietly inflate the layer's
   measured precision.
3. **One verdict, both arms.** The zebra leg and its BCS shadow share a single
   decision, so the July structure A/B stays unconfounded by judgement.
4. **Idempotent.** A verdict arriving after the signal already acted is
   discarded, not applied. The CLI can be retried, the cron can overlap, and
   the Pi can reboot mid-flight without double-entering.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from typing import Optional

from . import config as cfg

logger = logging.getLogger(__name__)

# vet states on the trade record
PENDING = 'pending'
ALLOWED = 'allowed'
VETOED = 'vetoed'
UNAVAILABLE = 'unavailable'
TERMINAL = (ALLOWED, VETOED, UNAVAILABLE)


def _now() -> datetime:
    return datetime.now()


def _parse(ts: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except (TypeError, ValueError):
        return None


# ── state inspection ─────────────────────────────────────────────────────
def vet_state(trade) -> Optional[str]:
    """State of the vet marker, or None if absent — or CORRUPT.

    Hardened on purpose: this is read in check_watching OUTSIDE any
    try/except, so a marker that is a string/number (hand-edit, bad merge)
    raising AttributeError here would kill the ENTIRE watching loop every
    cycle — one bad record becoming a fleet-wide entry halt. Treating corrupt
    as 'never requested' lets the gate overwrite it with a fresh request:
    self-healing, and fail-open like every other malformed-state path.
    """
    if not isinstance(trade, dict):
        return None
    v = trade.get('vet')
    return v.get('state') if isinstance(v, dict) else None


def is_pending(trade: dict) -> bool:
    return vet_state(trade) == PENDING


def is_expired(trade: dict, now: Optional[datetime] = None) -> bool:
    """True once a pending vet has outlived its deadline.

    Deliberately based on a stored absolute deadline rather than elapsed time,
    so a Pi reboot or a clock-skewed cron cannot leave a signal pending
    forever — the classic way an 'await approval' state silently becomes a
    permanent trading halt.
    """
    if not is_pending(trade):
        return False
    deadline = _parse((trade.get('vet') or {}).get('deadline'))
    if deadline is None:
        return True          # malformed marker: treat as expired, fail open
    return (now or _now()) >= deadline


# ── request ──────────────────────────────────────────────────────────────
def request_entry_vet(store, trade_id: int, context: dict,
                      spawn: bool = True) -> dict:
    """Mark a triggered signal PENDING and spawn the vetting CLI.

    `context` is the evidence bundle the analyzer already computed (strikes,
    debit, d/w, OI, spreads, gap, ST) — stored on the marker so the CLI reads
    exactly what the bot saw, with no re-quote drift between the two.
    """
    deadline = _now() + timedelta(seconds=cfg.VET_TIMEOUT_SEC)
    fresh_request = True
    with store._mutate():
        t = store._must_find(trade_id)
        if vet_state(t) in TERMINAL:
            raise ValueError(f"#{trade_id} already vetted ({vet_state(t)})")
        if is_pending(t) and not is_expired(t):
            # An overlapping cron already requested this vet (our caller's
            # cached read predated its write). Re-marking would silently
            # extend the deadline and spawn a SECOND CLI whose late verdict
            # then races the first — keep the original marker and spawn
            # nothing. (An EXPIRED pending is re-marked: its CLI is dead and
            # the sweep hasn't flipped it yet, so a fresh request is correct.)
            fresh_request = False
        else:
            t['vet'] = {
                'state': PENDING,
                'requested_at': _now().isoformat(),
                'deadline': deadline.isoformat(),
                'context': context,
                'decision_id': None,
            }
            t['version'] = t.get('version', 0) + 1
    if fresh_request:
        logger.info("VET REQUESTED #%d %s — deadline %s",
                    trade_id, context.get('stock'), deadline.strftime('%H:%M:%S'))
        if spawn:
            _spawn_cli(trade_id)
    else:
        logger.info("VET already pending for #%d — not re-requesting", trade_id)
    return store.find(trade_id)


# Detached children we have spawned and not yet reaped. On the cron (the
# production path) the parent exits within seconds and init adopts the child,
# so zombies cannot exist. In `zebra loop` the parent lives all day and an
# un-waited child sits as a zombie until the loop exits — poll() in
# _reap_children collects them without ever blocking.
_children: list = []


def _reap_children() -> None:
    for p in _children[:]:
        try:
            if p.poll() is not None:
                _children.remove(p)
        except Exception:                       # pragma: no cover - paranoia
            _children.remove(p)


def _spawn_cli(trade_id: int) -> Optional[int]:
    """Fire the Claude Code CLI detached. Returns the pid, or None on failure.

    Detached and never waited on: the trading loop must not block on an LLM.
    A spawn failure is logged and left to the deadline, which fails open — the
    signal still trades, it just trades unvetted.

    CLI ONLY, never the Anthropic API — the Pi is authenticated once
    interactively and that is the sanctioned path for this fleet.
    """
    # The prompt tells the agent the EXACT interpreter to use: the Pi runs the
    # bot under a venv and has no guaranteed bare `python` on PATH, and the CLI
    # verbs must import the same zebra package the bot runs.
    prompt = cfg.VET_PROMPT_TEMPLATE.format(trade_id=trade_id,
                                            python=sys.executable,
                                            vetting_doc=cfg.VETTING_DOC)
    argv = [cfg.VET_CLI, '-p', prompt, '--model', cfg.VET_MODEL]
    try:
        _reap_children()
        # Detach so the child survives the cron process exiting. stdout/stderr
        # to a log rather than a pipe: nobody is draining a pipe here, and a
        # full pipe buffer would hang the child mid-decision.
        cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
        out = open(cfg.LOG_DIR / 'vet_cli.log', 'a')
        try:
            kwargs = {'stdout': out, 'stderr': subprocess.STDOUT,
                      'stdin': subprocess.DEVNULL,
                      # Pin cwd: `-m zebra` must resolve regardless of where
                      # cron happened to start us.
                      'cwd': str(cfg.PROJECT_ROOT)}
            if os.name == 'posix':
                kwargs['start_new_session'] = True      # survive cron teardown
            p = subprocess.Popen(argv, **kwargs)
        finally:
            out.close()             # child holds its own duplicate of the fd
        _children.append(p)
        logger.info("VET CLI spawned pid=%d for #%d (model=%s)",
                    p.pid, trade_id, cfg.VET_MODEL)
        return p.pid
    except Exception as e:
        # Never propagate: a missing/broken CLI must not stop the bot trading.
        logger.error("VET CLI spawn FAILED for #%d: %s — will fail open at "
                     "the deadline", trade_id, e)
        return None


# ── verdict ──────────────────────────────────────────────────────────────
def record_verdict(store, trade_id: int, verdict: str,
                   decision_id: Optional[int] = None) -> str:
    """Land a verdict on the pending marker. Returns what actually happened.

    Idempotent by design: if the signal already reached a terminal state (a
    retried CLI call, an overlapping cron, or a verdict that arrived after the
    deadline already failed it open), the late verdict is DISCARDED rather than
    applied. Applying it would re-open a settled decision and, in the fail-open
    case, double-enter a signal that is already live.
    """
    if verdict not in (ALLOWED, VETOED):
        raise ValueError(f"verdict must be {ALLOWED!r} or {VETOED!r}")
    outcome = 'applied'
    with store._mutate():
        t = store._must_find(trade_id)
        state = vet_state(t)
        if state in TERMINAL:
            outcome = f'discarded (already {state})'
        elif state != PENDING:
            outcome = 'discarded (no pending vet)'
        else:
            t['vet']['state'] = verdict
            t['vet']['decided_at'] = _now().isoformat()
            t['vet']['decision_id'] = decision_id
            t['version'] = t.get('version', 0) + 1
    if outcome == 'applied':
        logger.info("VET %s #%d (decision #%s)", verdict.upper(), trade_id,
                    decision_id)
    else:
        logger.warning("VET verdict for #%d %s — late or duplicate call",
                       trade_id, outcome)
    return outcome


def expire_stale(store, now: Optional[datetime] = None) -> list:
    """Fail-open sweep: flip timed-out pending vets to `unavailable`.

    Called at the top of every zebra cycle. This is the guard that keeps a
    vetting outage from becoming a trading halt — the signal proceeds exactly
    as it would have before this layer existed, and is tagged so it never
    counts toward the layer's measured precision.
    """
    now = now or _now()
    _reap_children()        # runs every cycle: bounds zombies in `zebra loop`
    expired = []
    for t in list(store.load_trades()):
        if not is_expired(t, now):
            continue
        with store._mutate():
            fresh = store.find(t['id'])
            # Re-check inside the lock: the CLI may have landed a verdict
            # between the scan and the mutation.
            if fresh and is_expired(fresh, now):
                fresh['vet']['state'] = UNAVAILABLE
                fresh['vet']['decided_at'] = now.isoformat()
                fresh['version'] = fresh.get('version', 0) + 1
                expired.append(fresh['id'])
    for tid in expired:
        logger.warning("VET TIMED OUT #%d — failing OPEN (enters unvetted, "
                       "excluded from scoring)", tid)
    return expired
