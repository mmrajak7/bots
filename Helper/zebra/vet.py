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

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from . import config as cfg
from .filelock import exclusive

logger = logging.getLogger(__name__)

# vet states on the trade record
PENDING = 'pending'
ALLOWED = 'allowed'
VETOED = 'vetoed'
UNAVAILABLE = 'unavailable'
TERMINAL = (ALLOWED, VETOED, UNAVAILABLE)


def _now() -> datetime:
    """Naive local clock — used only for durations (deadlines, TTLs).

    Deliberately naive and self-consistent: every timestamp this module stores
    and re-parses uses it, so arithmetic between them is correct regardless of
    host timezone. Never use it to ask WHERE IN THE SESSION we are.
    """
    return datetime.now()


def _now_ist() -> datetime:
    """Exchange wall clock — used only for session-relative questions.

    Separate from _now() because these are different questions: "has 10 minutes
    passed" is timezone-free, "are we in the opening 15 minutes" is not. On a
    UTC-clocked Pi, answering the second with the first silently moves the
    whole session window.
    """
    return datetime.now(cfg.IST)


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


# Detached children we have spawned and not yet reaped, as (proc, kill_after).
# On the cron (the production path) the parent exits within seconds and init
# adopts the child, so zombies cannot exist. In `zebra loop` the parent lives
# all day and an un-waited child sits as a zombie until the loop exits — poll()
# in _reap_children collects them without ever blocking.
_children: list = []


def _reap_children(now: Optional[datetime] = None) -> int:
    """Collect finished children and KILL overdue ones. Returns kills.

    The deadline machinery fails the MARKER open; nothing used to touch the
    PROCESS. A `claude` run that hangs (API outage, network stall) therefore
    lived forever, and the calendar channel re-spawns on its own deadline — so
    a bad afternoon could leave dozens of node processes, a few hundred MB
    each, on a Pi that also runs SNAIL and FIFTY. A paper vetting layer must
    not be able to OOM a box with live-money processes on it, so anything still
    running well past its own deadline is killed.
    """
    now = now or _now()
    killed = 0
    for entry in _children[:]:
        p, kill_after = entry
        try:
            if p.poll() is not None:
                _children.remove(entry)
                continue
            if now < kill_after:
                continue
            p.kill()
            killed += 1
            logger.error('CLI pid=%d overdue past %s — KILLED (a hung agent '
                         'must not accumulate on the Pi)',
                         p.pid, kill_after.strftime('%H:%M:%S'))
            _children.remove(entry)
        except Exception:                       # pragma: no cover - paranoia
            _children.remove(entry)
    return killed


def _spawn_cli(trade_id: int, exit_kind: Optional[str] = None) -> Optional[int]:
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
    template = cfg.EXIT_PROMPT_TEMPLATE if exit_kind else cfg.VET_PROMPT_TEMPLATE
    prompt = template.format(trade_id=trade_id,
                             python=sys.executable,
                             exit_kind=exit_kind or '',
                             vetting_doc=cfg.VETTING_DOC)
    return _spawn_generic(prompt, cfg.VET_MODEL,
                          'vet #%d%s' % (trade_id,
                                         ' ' + exit_kind if exit_kind else ''),
                          channel='exit' if exit_kind else 'entry')


def _allowed_tools(channel: str) -> list:
    """Tool grants for this channel, with the real interpreter path baked in.

    `{python}` must be sys.executable, the same interpreter the prompt tells
    the agent to use: on the Pi that is the CROCODILE venv, and a pattern
    naming a different python would match nothing the agent actually types.
    """
    tools = [t.format(python=sys.executable) for t in cfg.VET_ALLOWED_TOOLS]
    if channel == 'events':
        tools += list(cfg.EVENT_EXTRA_TOOLS)
    return tools


_cli_path: list = []            # one-element memo; [] = not yet resolved


def resolve_cli(refresh: bool = False) -> Optional[str]:
    """Absolute path to the Claude CLI, or None if it cannot be found.

    `claude` was invoked by bare name, which works in an interactive shell and
    fails under cron: Debian's cron PATH is `/usr/bin:/bin`, while the CLI
    installs into `~/.local/bin` or an npm prefix. Every spawn would have
    raised FileNotFoundError on the Pi — the layer inert from the first cycle,
    and (before the watchdog fix above) invisibly so.
    """
    if _cli_path and not refresh:
        return _cli_path[0]
    candidates = [cfg.VET_CLI]
    if os.sep not in str(cfg.VET_CLI):
        found = shutil.which(cfg.VET_CLI)
        if found:
            candidates.append(found)
        home = Path.home()
        candidates += [str(home / '.local' / 'bin' / 'claude'),
                       str(home / '.npm-global' / 'bin' / 'claude'),
                       str(home / 'node_modules' / '.bin' / 'claude'),
                       '/usr/local/bin/claude', '/usr/bin/claude']
    for c in candidates:
        try:
            if os.sep in str(c) and os.path.isfile(c) and os.access(c, os.X_OK):
                _cli_path[:] = [c]
                return c
        except OSError:                          # pragma: no cover - paranoia
            continue
    return None


def _spawn_budget_ok(tag: str) -> bool:
    """Is there room on this box to start another agent right now?

    There was no bound of any kind. The caps that exist are PER POSITION and
    PER DAY, which limit how often one trade is looked at — not how many agents
    start at once. `review.run` loops over every entered position, and
    `needs_review` keys on market-wide event types, so a single `rbi_policy` row
    inside the horizon makes it true for ALL of them in the SAME cycle: 24 open
    positions today, 24 detached node processes, each alive up to CHILD_KILL_SEC
    (three cron cycles). Entry and exit channels add more, and one trade can
    hold two exit agents at once.

    `_children` cannot bound this — it is a module global in a process that
    exits in seconds, so in cron it is always empty. The only existing bound is
    coreutils `timeout`, which caps DURATION, not COUNT. So the budget lives on
    the filesystem, where it survives the process.

    This is the rule vet.py already states and did not enforce: a PAPER vetting
    layer must not be able to OOM a box that is running live money.
    """
    path = cfg.LOG_DIR / 'zebra_spawn_budget.json'
    now = time.time()
    window = float(cfg.CHILD_KILL_SEC)
    try:
        cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
        with exclusive(cfg.LOG_DIR / 'zebra_spawn_budget.lock'):
            try:
                recent = json.loads(path.read_text())
            except Exception:
                recent = []
            # Anything older than the child's own kill deadline is gone.
            recent = [t for t in recent
                      if isinstance(t, (int, float)) and now - t < window]
            if len(recent) >= cfg.MAX_CONCURRENT_AGENTS:
                logger.warning(
                    "SPAWN BUDGET: %d agent(s) started in the last %ds, cap is "
                    "%d — refusing to spawn %s. It will fail open on its "
                    "deadline.", len(recent), int(window),
                    cfg.MAX_CONCURRENT_AGENTS, tag)
                return False
            recent.append(now)
            path.write_text(json.dumps(recent))
        return True
    except Exception as e:
        # Fail OPEN on a bookkeeping failure: a broken budget file must not
        # become a silent vetting halt.
        logger.error("Spawn-budget check failed (%s) — allowing %s", e, tag)
        return True


def _spawn_generic(prompt: str, model: str, tag: str,
                   channel: str = 'entry') -> Optional[int]:
    """Fire a detached Claude CLI run. Returns the pid, or None on failure.

    Shared by every agent this package spawns (entry vet, exit vet, position
    review, event calendar) so they cannot drift apart on the details that
    actually matter: detachment, output redirection, cwd, and never raising.
    """
    cli = resolve_cli()
    if cli is None:
        logger.error("CLI %r not found on PATH or in any known install "
                     "location — cannot spawn %s. Vetting is OFF until this is "
                     "fixed; set ZEBRA_VET_CLI to the absolute path.",
                     cfg.VET_CLI, tag)
        _note_spawn(False, channel)
        return None
    if not _spawn_budget_ok(tag):
        # Refusing to spawn is NOT refusing to trade: the caller's deadline
        # lapses and the signal fails open exactly as it does during any other
        # outage. Losing one verdict is survivable; browning out the box that
        # runs the live-money monitor is not.
        return None
    argv = [cli, '-p', prompt, '--model', model,
            '--allowedTools'] + _allowed_tools(channel) + \
           ['--disallowedTools'] + list(cfg.VET_DENIED_TOOLS)
    # Hard wall-clock bound on the CHILD, not just on our bookkeeping. In cron
    # mode — the production path — our process exits within seconds and init
    # adopts the child, so _reap_children can never reach it: without this a
    # hung agent runs until the Pi reboots. `timeout` is coreutils and present
    # on Raspbian; if it is missing we still spawn (fail-open) and rely on the
    # in-process kill, which covers `zebra loop` only.
    if os.name == 'posix':
        timeout_bin = shutil.which('timeout')
        if timeout_bin:
            argv = [timeout_bin, '-k', '30', str(cfg.CHILD_KILL_SEC)] + argv
        else:
            logger.warning('coreutils `timeout` not found — a hung %s agent '
                           'can only be killed in loop mode', tag)
    try:
        _reap_children()
        # Detach so the child survives the cron process exiting. stdout/stderr
        # to a log rather than a pipe: nobody is draining a pipe here, and a
        # full pipe buffer would hang the child mid-decision.
        cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
        # ONE FILE PER DAY, and a banner naming the spawn.
        #
        # This file holds the ONLY copy of an agent's reasoning — the verdict
        # reaches the store, but the working-out that produced it exists
        # nowhere else, and it is the whole point of reviewing whether the
        # layer is any good. It used to be a single unbounded `vet_cli.log`
        # that every spawn appended to, so an entry vet, a position review and
        # a post-mortem firing in one cycle interleaved their output line by
        # line with nothing saying which was which. Unreadable is the same as
        # unrecorded.
        #
        # Dated like the cron log so `find -mtime` can trim it, and so a bad
        # day's reasoning stays findable.
        log_path = cfg.LOG_DIR / f"vet_cli_{_now().strftime('%Y%m%d')}.log"
        out = open(log_path, 'a')
        out.write(f"\n{'=' * 78}\n"
                  f"=== {_now().strftime('%Y-%m-%d %H:%M:%S')}  {tag}  "
                  f"model={model}  channel={channel}\n"
                  f"{'=' * 78}\n")
        out.flush()
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
        _children.append((p, _now() + timedelta(seconds=cfg.CHILD_KILL_SEC)))
        logger.info("CLI spawned pid=%d for %s (model=%s)", p.pid, tag, model)
        _note_spawn(True, channel)
        return p.pid
    except Exception as e:
        # Never propagate: a missing/broken CLI must not stop the bot trading.
        logger.error("CLI spawn FAILED for %s: %s — the caller's deadline "
                     "governs from here", tag, e)
        _note_spawn(False, channel)
        return None


def _note_spawn(ok: bool, channel: str = 'entry') -> None:
    """Feed the auth watchdog. Best-effort: health tracking must never be able
    to break the thing it is watching."""
    try:
        from .health import record_spawn_result
        record_spawn_result(ok, channel)
    except Exception:                            # pragma: no cover - paranoia
        pass


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
        elif is_expired(t):
            # Past the deadline but the sweep has not run yet (it fires at the
            # top of each cycle, so there is up to one monitor_interval_sec of
            # window). VETTING.md promises the agent that a late verdict is
            # void; honour that here rather than leaving it true only by the
            # accident of sweep timing. The signal stays PENDING and the next
            # sweep fails it open exactly as designed.
            outcome = 'discarded (deadline passed)'
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


def mark_unavailable(store, trade_id: int, why: str,
                     now: Optional[datetime] = None) -> bool:
    """Stamp a signal as failed-open when the REQUEST itself could not be made.

    `expire_stale` covers the request that was made and never answered. This
    covers the one that never got out of the door — a lock timeout or IO error
    inside `request_entry_vet`, which left no `vet` key at all and so produced a
    record indistinguishable from vetting being switched off, and an ENTER alert
    that said nothing about vetting because `_vet_line` returns "" for a missing
    state. Three different "no vetting happened" outcomes with one appearance is
    how a broken layer passes for a working one.

    `failed_open_because` is kept so the forensic record says WHICH kind of
    outage this was.
    """
    now = now or _now()
    with store._mutate():
        t = store.find(trade_id)
        if not t or vet_state(t) in TERMINAL:
            return False
        t['vet'] = dict(t.get('vet') or {},
                        state=UNAVAILABLE,
                        decided_at=now.isoformat(),
                        failed_open_because=str(why)[:300])
        t['version'] = t.get('version', 0) + 1
    return True


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


# ══ EXIT VETTING ═════════════════════════════════════════════════════════
# Exits are the dangerous direction. Both real-money losses in this fleet were
# automated EXITS firing on bad data at market open (ICICI Feb, NHPC Jul), and
# every structure here is hedged with max loss = debit, KNOWN AT ENTRY.
#
# That asymmetry drives the design: HOLDING is bounded risk, EXITING BADLY is
# not. So an exit whose quote cannot be trusted is HELD and escalated to the
# human rather than fired on the hope the print was real.
#
# This sits ON TOP of the deterministic guards (quote reliability, the DEBIT_SL
# confirm debounce, the intrinsic-floor clamp). It never overrides them, and it
# can never TRIGGER an exit they did not already call for — it only delays one.

DEFER = 'defer'

# Every exit kind the monitor can raise, in ONE place. The CLI's argparse
# choices are built from this: they used to be typed out separately, so adding
# the TRAIL exit would have produced a gate that spawns an agent which then
# cannot record a verdict — `--kind trail` would be rejected by argparse, the
# agent would exit having done nothing, and every trail exit would defer to the
# cap and escalate to the human. Silently inert, in the familiar shape.
EXIT_KINDS = ('tp', 'spot_sl', 'debit_sl', 'trail')

# Exit kinds whose trigger is corroborated by REAL TRADES in the underlying, so
# a lying option book cannot manufacture them. Everything else is priced off
# the book and is vetted by default — see _exit_interesting.
SPOT_CORROBORATED_EXITS = frozenset({'tp', 'spot_sl'})


def _exit_marker(trade: dict, kind: str) -> dict:
    """Read the per-kind exit marker, tolerating corruption.

    Same hardening as vet_state(): this is read on the exit path, and an
    AttributeError from a hand-edited record would take down the monitor loop
    for every position, not just this one.
    """
    if not isinstance(trade, dict):
        return {}
    ev = trade.get('exit_vet')
    if not isinstance(ev, dict):
        return {}
    m = ev.get(kind)
    return m if isinstance(m, dict) else {}


def _marker_fresh(marker: dict, now: Optional[datetime] = None) -> bool:
    """Is a TERMINAL exit verdict still about the book in front of us?

    A verdict is a judgement about a specific quote at a specific moment, not a
    standing permission. Without this, an `allow` granted on a clean book on
    Monday sits on the marker forever and waves through a Friday exit priced off
    a garbage book — the exact NHPC shape the layer exists to catch. Same for
    `unavailable`: one ten-minute Claude outage would otherwise disarm vetting
    for that (trade, kind) permanently while the switch still reads ON.

    A stale marker is treated as "never vetted", so the next trigger re-requests
    from scratch — including the defer count, since accumulated distrust belongs
    to the episode that produced it.

    An ESCALATED hold is the exception, and gets a much longer life — see
    `_marker_ttl`.
    """
    decided = _parse(marker.get('decided_at'))
    if decided is None:
        return False
    return (now or _now()) - decided < timedelta(seconds=_marker_ttl(marker))


def _pending_recent(marker: dict, now: Optional[datetime] = None) -> bool:
    """Is an expired PENDING request still ABOUT the episode in front of us?

    A PENDING marker carries no `decided_at`, so `_marker_fresh` cannot judge
    it. This bounds it by its own DEADLINE plus one TTL — deliberately not by
    `requested_at`, which would leave only the gap between the deadline and the
    TTL (5 minutes on the defaults) for a genuine outage to cash out as
    fail-open. Measuring from the deadline guarantees a full TTL — three cron
    cycles — in which a real "Claude did not answer" still returns 'proceed',
    while a fossil from days ago is still discarded.

    Fail-open is the contract for an outage. It is NOT a contract for a request
    that expired in a market that has since closed and reopened.
    """
    deadline = _parse(marker.get('deadline')) or _parse(marker.get('requested_at'))
    if deadline is None:
        return False
    return (now or _now()) - deadline < timedelta(seconds=cfg.EXIT_VET_TTL_SEC)


def _marker_ttl(marker: dict) -> int:
    """Seconds a terminal exit verdict stays authoritative.

    Short (EXIT_VET_TTL_SEC) for ALLOW/UNAVAILABLE: those authorise an exit, and
    an authorisation must not outlive the book it judged.

    Long (EXIT_HOLD_TTL_SEC) once the defer cap is reached and the human has
    been asked. That state authorises nothing — it HOLDS — so it is safe to keep,
    and keeping it is what stops a pathological loop: on a persistently
    untradeable book the short TTL wiped the episode every 15 minutes, and the
    next trigger re-ran the whole agent sequence to arrive at the same
    escalation. ~30 Fable runs a day, per position, to re-learn one fact the
    user was already told. The escalation flag re-arms daily; a genuinely new
    session gets a genuinely fresh look.
    """
    if marker.get('state') == DEFER and \
            int(marker.get('defers') or 0) >= cfg.EXIT_MAX_DEFERS:
        return cfg.EXIT_HOLD_TTL_SEC
    return cfg.EXIT_VET_TTL_SEC


def needs_exit_vet(trade: dict, kind: str, quote: dict,
                   now: Optional[datetime] = None) -> tuple:
    """Cheap deterministic pre-filter: is this exit worth an LLM call?

    Returns (needed, why). Market hours are ~75 cycles/day; a full agent run
    on each would burn ~1M tokens/day to conclude "nothing changed". Python
    decides what is interesting; Claude judges only those.

    An exit is interesting when the QUOTE BEHIND IT might be lying — the NHPC
    signature exactly: a structure that read 0.18 against an intrinsic floor
    far above it, on a book nobody could actually have traded.
    """
    reasons = []
    # Default FALSE, not True: a malformed/empty quote dict is exactly the case
    # that most deserves a second look, and defaulting to "reliable" would let
    # it through unvetted.
    if not quote.get('reliable', False):
        reasons.append('unreliable book (%s)' % (quote.get('reason') or 'n/a'))
    if int(trade.get('debit_blind_cycles') or 0) > 0:
        reasons.append('recent debit-blind cycles')
    # Both real-money incidents happened in the opening minutes, when books are
    # thin and prints are unrepresentative. EXCHANGE clock, never the host's:
    # on a UTC-clocked Pi local time would put the whole session outside this
    # window (or all of it inside), silently changing what gets vetted.
    n = now or _now_ist()
    if (n.hour, n.minute) < (9, 30):
        reasons.append('first 15 minutes of the session')
    # A value trigger acts on a PRICE; a spot trigger is corroborated by real
    # trades in the underlying. The value one is the one that can be faked.
    #
    # Stated as "which kinds are spot-corroborated" rather than "which are
    # value-based" ON PURPOSE. The list used to name `debit_sl` directly, so
    # when the TRAIL exit arrived — also priced entirely off the option book —
    # it would have been silently exempt from vetting unless the book happened
    # to look unreliable, which is precisely the ABB case that looked fine.
    # Inverted, any exit kind added later is vetted by default and has to argue
    # its way out.
    if kind not in SPOT_CORROBORATED_EXITS:
        reasons.append('value-based trigger (priced off the option book)')
    return (bool(reasons), '; '.join(reasons))


def exit_gate(store, trade: dict, kind: str, quote: dict, spot: float,
              spawn: bool = True) -> str:
    """May this exit fire THIS cycle?

    Returns:
      'proceed' — fire it (vetted, not worth vetting, or Claude is down and the
                  deterministic guards stand on their own)
      'wait'    — a verdict is pending; re-evaluate next cycle
      'hold'    — deferred to the cap and escalated; the human decides

    MUST be called BEFORE `set_alert_flag`. That flag is consume-once, and
    burning it on an exit that does not execute strands the exit permanently —
    the monitor's own comments warn about exactly this.
    """
    if not cfg.VET_ENABLED:
        return 'proceed'

    m = _exit_marker(trade, kind)
    state = m.get('state')

    # A terminal verdict authorises THIS episode, not this trade forever. Once
    # it goes stale we forget it entirely — state, defer count and all — so the
    # next trigger is judged on the book actually in front of us.
    if state in (ALLOWED, UNAVAILABLE, DEFER) and not _marker_fresh(m):
        logger.info('EXIT VET marker for #%d %s is stale (%s) — re-evaluating '
                    'this trigger from scratch', trade['id'], kind, state)
        m, state = {}, None

    # PENDING had NO age bound: `_marker_fresh` covers only the three TERMINAL
    # states, and nothing sweeps `exit_vet` at all (`expire_stale` walks the
    # ENTRY marker only). `exit_gate` is reached solely when a trigger fires, so
    # a request whose agent died — and whose trigger then stopped firing —
    # simply sat on disk. Days later the book genuinely collapses, the trigger
    # returns, and the timeout branch below flips that fossil to UNAVAILABLE and
    # returns 'proceed': the exit fires on the strength of a timeout that
    # happened last week, against a book no agent has ever looked at, while the
    # log says "TIMED OUT — proceeding" exactly as it would for a real
    # ten-minute outage. That is a one-shot silent bypass per (trade, kind), on
    # the precise failure shape this channel exists to catch.
    #
    # Forget it entirely so control falls through to `needs_exit_vet` and raises
    # a FRESH request against the quote actually in front of us. Accumulated
    # defers go with it, for the same reason a stale DEFER above loses them:
    # distrust belongs to the episode that earned it.
    if state == PENDING and _exit_expired(m) and not _pending_recent(m):
        logger.warning(
            'EXIT VET request for #%d %s is ANCIENT (requested %s) — '
            'discarding it and re-requesting against the current book',
            trade['id'], kind, m.get('requested_at'))
        m, state = {}, None

    if state == ALLOWED:
        return 'proceed'
    if state == UNAVAILABLE:
        # Claude never answered. The deterministic guards are unchanged and
        # still hold, so this is the same fail-open contract as entry: the
        # layer is additive, never load-bearing.
        return 'proceed'
    if state == DEFER and int(m.get('defers') or 0) >= cfg.EXIT_MAX_DEFERS:
        return 'hold'
    if state == PENDING:
        if not _exit_expired(m):
            return 'wait'
        prior = int(m.get('defers') or 0)
        if prior:
            # Claude LOOKED at this exit and said it could not verify the
            # quote; then the re-check timed out. A timeout is "we don't know",
            # and "we don't know" after an explicit refusal is not consent —
            # fail-open is the contract for *never examined*, not for this.
            # Count the timeout as another failure to verify so the escalation
            # cap arrives on schedule.
            if not _set_exit_state(store, trade['id'], kind, DEFER,
                                   bump_defer=True, expect_state=PENDING):
                return _cas_lost(trade, kind)
            new = prior + 1
            logger.warning('EXIT VET TIMED OUT #%d %s after %d prior defer(s) '
                           '— NOT proceeding; distrust carries over',
                           trade['id'], kind, prior)
            return 'hold' if new >= cfg.EXIT_MAX_DEFERS else 'wait'
        if not _set_exit_state(store, trade['id'], kind, UNAVAILABLE,
                               expect_state=PENDING):
            # The CAS lost, so the marker is NOT pending on disk — a verdict
            # landed between our cached read and this write. Returning
            # 'proceed' here (as this did) fires the exit on the strength of a
            # timeout that did not happen, and if that verdict was `defer` it
            # fires the exit on the very book Claude just said it could not
            # verify. In `zebra loop` the cached read can be a full cycle old,
            # so this is not a microsecond race — it is most of the deadline
            # window, and it is the NHPC direction exactly.
            return _cas_lost(trade, kind)
        logger.warning('EXIT VET TIMED OUT #%d %s — proceeding on the '
                       'deterministic guards alone', trade['id'], kind)
        return 'proceed'

    needed, why = needs_exit_vet(trade, kind, quote)
    if not needed:
        return 'proceed'

    defers = int(m.get('defers') or 0) if state == DEFER else 0
    try:
        _request_exit_vet(store, trade['id'], kind, {
            'reason_flagged': why,
            'kind': kind,
            'spot': spot,
            'mid': quote.get('mid'),
            'reliable': quote.get('reliable'),
            'quote_reason': quote.get('reason'),
            # The actual books. The doc asks the agent to judge depth at touch
            # and spread as a % of mid; without these it was being asked to
            # judge what it could not see. `floored` says the mid is the
            # no-arbitrage floor rather than anything the market quoted — a
            # verdict about whether a price is REAL has to know that.
            'legs': quote.get('legs'),
            'floored': quote.get('floored'),
            'entry_debit': trade.get('debit'),
            'debit_sl_value': trade.get('debit_sl_value'),
            'short_extrinsic_entry': trade.get('short_extrinsic_entry'),
            'long_symbol': trade.get('long_symbol'),
            'short_symbol': trade.get('short_symbol'),
            'expiry': trade.get('expiry'),
        }, defers=defers, spawn=spawn)
    except Exception as e:
        logger.error('EXIT VET request failed #%d %s: %s — proceeding on the '
                     'deterministic guards', trade['id'], kind, e)
        return 'proceed'
    return 'wait'


def _cas_lost(trade: dict, kind: str) -> str:
    """A timeout write lost its compare-and-set. Wait, never proceed.

    Something else moved the marker off PENDING while we were deciding it had
    timed out — in practice a verdict that landed in the gap. `wait` costs one
    cycle and then acts on the REAL state; `proceed` would act on a state we
    already know is wrong, in the direction that has cost this book money.
    """
    logger.warning('EXIT VET #%d %s: a verdict landed while we were timing it '
                   'out — waiting one cycle to act on the real state',
                   trade['id'], kind)
    return 'wait'


def _exit_expired(marker: dict, now: Optional[datetime] = None) -> bool:
    d = _parse(marker.get('deadline'))
    if d is None:
        return True                      # malformed: fail to the guards
    return (now or _now()) >= d


def _request_exit_vet(store, trade_id: int, kind: str, context: dict,
                      defers: int = 0, spawn: bool = True) -> bool:
    """Mark this (trade, kind) PENDING and spawn the CLI. True if we spawned.

    Carries the same overlap guard as request_entry_vet: `zebra loop` and a
    manual `python -m zebra run` are not serialised against each other (flock
    covers the STORE, not the cycle), so without this both would overwrite the
    marker and spawn a CLI. Two agents answering one request is not redundancy
    — their verdicts race, and two `defer`s from a single re-check drive the
    counter to the escalation cap in one step.
    """
    fresh = True
    with store._mutate():
        t = store._must_find(trade_id)
        ev = t.get('exit_vet')
        if not isinstance(ev, dict):
            ev = {}
        existing = ev.get(kind) if isinstance(ev.get(kind), dict) else {}
        if existing.get('state') == PENDING and not _exit_expired(existing):
            fresh = False          # someone else already asked; keep their marker
        else:
            ev[kind] = {
                'state': PENDING,
                'requested_at': _now().isoformat(),
                'deadline': (_now() + timedelta(
                    seconds=cfg.VET_TIMEOUT_SEC)).isoformat(),
                'defers': defers,
                'context': context,
                'decision_id': None,
            }
            t['exit_vet'] = ev
            t['version'] = t.get('version', 0) + 1
    if not fresh:
        logger.info('EXIT VET already pending for #%d %s — not re-requesting',
                    trade_id, kind)
        return False
    logger.info('EXIT VET REQUESTED #%d %s — %s', trade_id, kind,
                context.get('reason_flagged'))
    if spawn:
        _spawn_cli(trade_id, exit_kind=kind)
    return True


def _set_exit_state(store, trade_id: int, kind: str, state: str,
                    decision_id: Optional[int] = None,
                    bump_defer: bool = False,
                    expect_state: Optional[str] = None) -> bool:
    """Compare-and-set the exit marker. True if the write landed.

    `expect_state` makes this a CAS rather than a blind write, and the check
    happens INSIDE the lock. Checking outside it (as this originally did, via
    the caller) is a TOCTOU: two CLIs that both read PENDING both write, so a
    `defer` and an `allow` become last-writer-wins — and the losing writer is
    silently the one that said "I cannot verify this quote".
    """
    applied = False
    with store._mutate():
        t = store._must_find(trade_id)
        ev = t.get('exit_vet') if isinstance(t.get('exit_vet'), dict) else {}
        m = ev.get(kind) if isinstance(ev.get(kind), dict) else {}
        if expect_state is None or m.get('state') == expect_state:
            m['state'] = state
            m['decided_at'] = _now().isoformat()
            if decision_id is not None:
                m['decision_id'] = decision_id
            if bump_defer:
                m['defers'] = int(m.get('defers') or 0) + 1
            ev[kind] = m
            t['exit_vet'] = ev
            t['version'] = t.get('version', 0) + 1
            applied = True
    return applied


def record_exit_verdict(store, trade_id: int, kind: str, verdict: str,
                        decision_id: Optional[int] = None) -> str:
    """Land an exit verdict. `verdict` is 'allow' or 'defer'.

    There is deliberately NO hard veto for exits. A veto would let the model
    cancel a stop-loss outright; `defer` re-checks with a fresh quote next
    cycle and escalates to the human at cfg.EXIT_MAX_DEFERS. That is the same
    protective power in a shape that cannot silently disarm a stop.
    """
    if verdict not in ('allow', DEFER):
        raise ValueError("exit verdict must be 'allow' or 'defer'")
    t = store.find(trade_id)
    if not t:
        raise ValueError('#%d not found' % trade_id)
    if t.get('status') != 'entered':
        # The position closed while the agent was thinking. Landing a verdict
        # on a settled trade cannot help and would leave a marker that outlives
        # the position it describes.
        logger.warning('EXIT verdict for #%d %s discarded — trade is %s',
                       trade_id, kind, t.get('status'))
        return 'discarded (trade %s)' % t.get('status')
    # The state check that MATTERS happens inside _set_exit_state's lock; this
    # one only buys a precise log line for the common non-racing case.
    applied = _set_exit_state(
        store, trade_id, kind,
        ALLOWED if verdict == 'allow' else DEFER,
        decision_id, bump_defer=(verdict == DEFER), expect_state=PENDING)
    if not applied:
        state = _exit_marker(store.find(trade_id) or {}, kind).get('state')
        logger.warning('EXIT verdict for #%d %s discarded — state is %s '
                       '(late, duplicate, or another agent answered first)',
                       trade_id, kind, state)
        return 'discarded (state %s)' % state
    logger.info('EXIT %s #%d %s (decision #%s)',
                'ALLOWED' if verdict == 'allow' else 'DEFERRED',
                trade_id, kind, decision_id)
    return 'applied'


def exit_defers(trade: dict, kind: str) -> int:
    return int(_exit_marker(trade, kind).get('defers') or 0)


def exit_state(trade: dict, kind: str) -> Optional[str]:
    return _exit_marker(trade, kind).get('state')
