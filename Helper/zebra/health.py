"""Health of the vetting layer itself — mainly: is the Claude CLI still logged in?

The whole layer fails open, so an expired credential does not stop the bot
trading. It stops the bot *vetting*, silently, while every switch still reads
ON. That is the worst shape a safety system can fail in, and the user cannot
re-authenticate a Pi they do not know has logged out — so this nags on
Telegram before the credential dies, not after.

Two detectors, because credential storage is not a stable contract:
1. Read an expiry timestamp out of the CLI's credential file if one is there.
2. Otherwise fall back to counting consecutive spawn failures — evidence that
   the CLI is not working, whatever the reason.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from . import config as cfg
from common.filelock import exclusive

logger = logging.getLogger(__name__)

# Where the CLI keeps credentials, newest layout first. Checked in order; the
# first that parses wins. An unknown layout is not an error — it just means we
# fall back to the behavioural probe.
_CRED_PATHS = (
    Path.home() / '.claude' / '.credentials.json',
    Path.home() / '.config' / 'claude' / '.credentials.json',
    Path.home() / '.claude' / 'credentials.json',
)
# ONLY the refresh-token expiry, and this distinction is the whole point.
# A real credential file (2026-08-11) carries BOTH:
#     expiresAt              -> the ACCESS token, ~13 hours out, auto-refreshed
#     refreshTokenExpiresAt  -> the SESSION,      ~28 days out, needs a human
# Warning on `expiresAt` would fire every single day and be wrong every single
# day, which trains the user to ignore the one alert that matters. If a layout
# offers only an access-token expiry we deliberately report nothing and let the
# behavioural probe speak instead — silence beats a daily false alarm.
_EXPIRY_KEYS = ('refreshTokenExpiresAt', 'refresh_token_expires_at',
                'sessionExpiresAt')

STATE_FILE = 'auth_health.json'
# How many agents may be spawned without a single one reporting back before we
# call the layer broken. Entry + exit + review + calendar agents all land verbs,
# so on a working Pi this counter rarely passes 1.
SILENT_SPAWN_LIMIT = 5

# What a DEAD channel actually costs, in the owner's terms. Added 2026-08-14:
# the alert named the channel ("events: 27") and stopped there, which only
# means something to a reader who already holds the architecture in their head.
# The one that mattered — the corporate-action interlock reading nothing — was
# invisible in the message and took a code review to surface.
#
# One clause each, and only for channels that are ACTUALLY silent. The old
# trailer asserted the entry/exit consequence unconditionally, so on the
# morning only `events` was dead it announced "Entries are QUEUED" while entry
# vetting was working perfectly — a false statement about the one subsystem the
# reader most needs to trust.
CHANNEL_IMPACT = {
    'entry': ("no signal can be vetted, so entries QUEUE and are DROPPED "
              "after 2 attempts or 1 hour. Nothing enters unvetted — you lose "
              "opportunities, not capital."),
    'exit': ("exits fall back to the deterministic guards (debounce, quote "
             "reliability, intrinsic floor, spot veto). They still fire; they "
             "just fire without a second opinion."),
    'events': ("the event calendar goes stale, and the corporate-action "
               "interlock reads it. On an ex-date your stored stop levels are "
               "breached by ARITHMETIC, on a share that no longer exists."),
    'review': ("no macro/event judgement between triggers. Advisory only — no "
               "stop or target is affected."),
    'postmortem': ("no precedents accumulate for future vetting. Learning "
                   "only — nothing live is affected."),
}


def _state_path():
    return cfg.LOG_DIR / STATE_FILE


@contextmanager
def _locked_state():
    """Read-modify-write the watchdog state under a cross-process lock.

    cron, `zebra loop` and every landing agent all touch this file. Unlocked,
    two processes read the same counter and one write is lost — which delays or
    silently suppresses the threshold. The counters ARE the detector now that
    the credential check no longer masks them, so losing counts loses the
    alarm. Never raises: a locking failure degrades to no bookkeeping, not to a
    broken cycle.
    """
    try:
        cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
        with exclusive(cfg.LOG_DIR / (STATE_FILE + '.lock')):
            state = _read_state()
            yield state
            _write_state(state)
    except Exception as e:
        logger.error('auth health state not updated (%s) — the vetting '
                     'watchdog may be running blind', e)


def _read_state() -> dict:
    try:
        with open(_state_path()) as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    try:
        cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = str(_state_path()) + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, str(_state_path()))
    except Exception as e:
        # Never raises — bookkeeping must not break trading. But it is NOT a
        # debug detail: a full or read-only SD card (the classic Pi failure)
        # freezes these counters, and a frozen counter is a blind watchdog that
        # still reports healthy.
        logger.error('auth health state NOT written (%s) — the vetting '
                     'watchdog is now blind', e)


def _walk_for_expiry(obj) -> Optional[datetime]:
    """Find an expiry anywhere in a nested credential document."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _EXPIRY_KEYS and isinstance(v, (int, float)) and v > 0:
                # Heuristic, and stated as one: values past ~year 2001 in
                # seconds are ~1e9, so anything above 1e11 must be millis.
                secs = float(v) / 1000.0 if float(v) > 1e11 else float(v)
                try:
                    return datetime.fromtimestamp(secs)
                except (OverflowError, OSError, ValueError):
                    return None
            found = _walk_for_expiry(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _walk_for_expiry(v)
            if found:
                return found
    return None


def credential_expiry(paths=None) -> Optional[datetime]:
    """Expiry of the CLI credential, or None if it cannot be determined."""
    for p in (paths or _CRED_PATHS):
        try:
            if not p.exists():
                continue
            with open(p) as f:
                return _walk_for_expiry(json.load(f))
        except Exception as e:
            # The file is THERE and we could not parse it — a layout change or
            # a corrupt write. We fall back to the behavioural probes, which is
            # safe, but the operator should know the expiry warning is gone.
            logger.warning('credential file %s unreadable (%s) — falling back '
                           'to the behavioural probes', p, e)
    return None


def _channels(state: dict) -> dict:
    ch = state.get('channels')
    return ch if isinstance(ch, dict) else {}


def record_spawn_result(ok: bool, channel: str = 'entry') -> int:
    """Track CLI health from the spawn side. Returns spawns-since-landing.

    A successful `Popen` proves only that the BINARY EXISTS. An expired login
    spawns perfectly and then exits immediately with an auth error — which is
    precisely the failure this watchdog is for, and precisely what a
    spawn-success counter cannot see. So a successful spawn does NOT clear the
    alarm; only a landed verb does (see `record_agent_landed`). What we count
    here is "agents launched that never came back", which catches an expired
    login, a broken prompt, and a crashing agent alike.

    Counted PER CHANNEL. A single global counter is cleared by whichever agent
    happens to succeed, so one healthy Sonnet calendar refresh every two hours
    would reset the alarm while every Fable vetting agent died — the counter
    could then never reach its threshold, and the watchdog would be decorative.
    """
    seen = [0]
    with _locked_state() as state:
        ch = _channels(state)
        row = ch.get(channel) if isinstance(ch.get(channel), dict) else {}
        if ok:
            row['spawns_since_landing'] = int(
                row.get('spawns_since_landing') or 0) + 1
        else:
            row['spawn_failures'] = int(row.get('spawn_failures') or 0) + 1
            state['spawn_failures'] = int(state.get('spawn_failures') or 0) + 1
        row['last_spawn_at'] = datetime.now().isoformat()
        ch[channel] = row
        state['channels'] = ch
        state['last_spawn_at'] = row['last_spawn_at']
        seen[0] = int(row.get('spawns_since_landing') or 0)
    return seen[0]


def record_spawn_refused(channel: str = 'entry') -> None:
    """We DECLINED to start an agent. This is not a CLI failure.

    Kept off `spawn_failures` deliberately. That counter drives "CLAUDE CLI NOT
    STARTING — the binary is missing or unrunnable", and a refusal proves the
    opposite: the budget is working and the box is busy. Routing refusals into
    it guaranteed a false alarm on any wide sweep — 24 open positions against a
    deferrable cap of 3 means 21 refusals in one cycle, seven times the
    threshold, blaming a binary that is fine.

    It is the same mistake, one commit later, that the ENTER alert was fixed
    for this morning: "never asked" and "asked and failed" need opposite
    responses, so they must never share a counter.
    """
    with _locked_state() as state:
        ch = _channels(state)
        row = ch.get(channel) if isinstance(ch.get(channel), dict) else {}
        row['refusals'] = int(row.get('refusals') or 0) + 1
        row['last_refused_at'] = datetime.now().isoformat()
        ch[channel] = row
        state['channels'] = ch


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Never raises: a hand-edited or half-written state file must not be able
    to take down the watchdog that is watching for exactly that kind of thing."""
    try:
        return datetime.fromisoformat(ts) if ts else None
    except (TypeError, ValueError):
        return None


def record_cli_blocked(reset_at: Optional[str], reason: str = '') -> None:
    """The CLI is alive and REFUSING — a usage limit, not a failure.

    Recorded because it is otherwise INVISIBLE to every probe in `check`: a
    block suppresses spawns, so `spawns_since_landing` never rises, no spawn
    fails, and the credential is perfectly valid. All three probes report
    healthy while the entire vetting layer is down — the one failure shape this
    module's own docstring says it exists to prevent.

    Pass reset_at=None to clear when the block lifts.
    """
    with _locked_state() as state:
        if reset_at is None:
            state.pop('cli_block', None)
            return
        prev = state.get('cli_block')
        prev = prev if isinstance(prev, dict) else {}
        state['cli_block'] = {
            'reset_at': reset_at,
            'reason': reason,
            'since': prev.get('since') or datetime.now().isoformat(),
            # Preserved across refreshes so one block alerts ONCE, not every
            # cycle for as long as it lasts.
            'alerted_for': prev.get('alerted_for'),
        }


def record_agent_landed(channel: str = 'entry') -> None:
    """A spawned agent completed its job by calling a zebra verb.

    THE proof of life for this layer: the CLI started, authenticated, did the
    work, and wrote back. Called from every verb an agent finishes with, and
    the only thing that clears the alarm — for ITS OWN channel only.
    """
    with _locked_state() as state:
        ch = _channels(state)
        ch[channel] = {'spawns_since_landing': 0, 'spawn_failures': 0,
                       'last_landed_at': datetime.now().isoformat()}
        state['channels'] = ch
        # The global failure counter tracks "the binary would not even start",
        # which is not channel-specific: any agent landing disproves it.
        state['spawn_failures'] = 0
        state['last_landed_at'] = ch[channel]['last_landed_at']


def silent_channels(state: Optional[dict] = None) -> list:
    """Channels that have spawned past the limit without one agent reporting."""
    state = _read_state() if state is None else state
    return sorted(
        name for name, row in _channels(state).items()
        if isinstance(row, dict)
        and int(row.get('spawns_since_landing') or 0) >= SILENT_SPAWN_LIMIT)


def check(send=None, now: Optional[datetime] = None, dry_run: bool = False,
          paths=None) -> Optional[str]:
    """Daily auth check. Returns the message sent, or None.

    Nags once per calendar day so an expiring credential keeps surfacing until
    the user acts, without becoming noise.
    """
    if not cfg.VET_ENABLED:
        return None
    now = now or datetime.now()
    state = _read_state()
    today = now.strftime('%Y-%m-%d')
    # A usage-limit block is an OPERATIONAL event, not a daily credential nag:
    # it starts and ends within hours, and the owner needs to know inside one
    # cycle that nothing is being vetted. So it bypasses the once-a-day gate —
    # but keyed on the block's own reset time, so one block alerts ONCE however
    # many cycles it spans.
    block = state.get('cli_block')
    block = block if isinstance(block, dict) else {}
    block_reset = _parse_iso(block.get('reset_at'))
    block_live = block_reset is not None and now < block_reset
    block_new = block_live and block.get('alerted_for') != block.get('reset_at')
    if state.get('last_warned_on') == today and not block_new:
        return None

    # THREE INDEPENDENT PROBES, deliberately not an if/elif chain.
    #
    # They were chained, and it made the watchdog worse than useless: the
    # credential file on the Pi exists and parses, so `expiry is not None` was
    # always true and the two BEHAVIOURAL probes below it were unreachable. A
    # missing binary, a denied tool permission, a bad model name — every
    # first-deploy failure — would report perfectly healthy for the ~28 days
    # until the credential itself aged out. A safety system that says "fine"
    # while it is dead is the one failure shape this whole layer exists to
    # prevent, so each probe now speaks for itself.
    alerts = []
    if block_live:
        # FOURTH independent probe. The other three are all blind here: spawns
        # are suppressed so nothing goes silent, nothing fails to start, and
        # the credential is fine. Without this the watchdog all-clears through
        # an outage that stops every entry and every exit second opinion.
        alerts.append(
            f"🔑 <b>CLAUDE IS REFUSING — USAGE LIMIT</b>\n"
            f"The agent binary runs and exits immediately"
            f"{': ' + block['reason'] if block.get('reason') else ''}.\n"
            f"Back at <b>{block_reset.strftime('%H:%M')}</b>. Until then:"
            f"\n• <b>entry</b> — {CHANNEL_IMPACT['entry']}"
            f"\n• <b>exit</b> — {CHANNEL_IMPACT['exit']}"
            f"\n<i>Queued entries hold their place to the reset rather than "
            f"burning attempts, so nothing is dropped for want of an agent "
            f"that was never available.</i>")
    expiry = credential_expiry(paths)
    if expiry is not None:
        days = (expiry - now).days
        if days <= cfg.AUTH_WARN_DAYS:
            gone = expiry <= now
            alerts.append(
                f"🔑 <b>CLAUDE CLI LOGIN {'EXPIRED' if gone else 'EXPIRING'}</b>\n"
                f"The vetting layer's credential expires "
                f"{'EXPIRED' if gone else 'in %d day(s)' % days} "
                f"({expiry.strftime('%Y-%m-%d %H:%M')}).")
    if int(state.get('spawn_failures') or 0) >= 3:
        alerts.append(
            f"🔑 <b>CLAUDE CLI NOT STARTING</b>\n"
            f"{state['spawn_failures']} spawn failures — the binary is missing "
            f"or unrunnable (check the PATH cron gives it).")
    silent = silent_channels(state)
    if silent:
        # Spawning fine, never reporting back. This is what an expired login —
        # or a denied tool permission — actually looks like from the outside,
        # and what a spawn-success counter would call healthy.
        detail = ', '.join(
            '%s: %d' % (c, int(_channels(state)[c].get('spawns_since_landing') or 0))
            for c in silent)
        impact = ''.join(
            '\n• <b>%s</b> — %s' % (c, CHANNEL_IMPACT[c])
            for c in silent if c in CHANNEL_IMPACT)
        alerts.append(
            f"🔑 <b>CLAUDE AGENTS NOT REPORTING BACK</b>\n"
            f"Spawned but never completed ({detail}). The CLI starts and exits "
            f"without finishing its work — an auth failure and a tool grant "
            f"that matches nothing look identical from here."
            f"{impact}")

    if not alerts:
        return None
    # Say what to CHECK, not what is wrong. On 2026-08-13 this message asserted
    # an expired login; the login was fine and the real cause was a tool grant
    # that matched nothing. Naming the likeliest cause as the cause sent the
    # owner to re-login for nothing and made the true cause harder to find.
    # The agent's own log is the only place the answer actually exists.
    msg = ('\n\n'.join(alerts) +
           "\n<i>Read the agent's log first — it is the only copy of what it "
           "tried:\n"
           "<code>tail -n 40 logs/vet_cli_$(date +%Y%m%d)_*.log</code>\n"
           "An approval request there = a tool grant that did not match; an "
           "auth error = <code>claude</code> then /login.</i>")
    if send and send(msg, dry_run=dry_run):
        with _locked_state() as s:
            s['last_warned_on'] = today
            if block_live:
                # Stamp the BLOCK as announced, not just the day: the daily
                # gate is what this alert deliberately bypassed, so relying on
                # it would re-send every cycle for the length of the outage.
                b = s.get('cli_block')
                if isinstance(b, dict):
                    b['alerted_for'] = block.get('reset_at')
                    s['cli_block'] = b
        logger.warning('AUTH WARNING sent')
    else:
        # Do NOT mark it warned: an unsent warning is not a warning.
        logger.error('AUTH WARNING could not be sent — will retry next cycle')
    return msg


def status(now: Optional[datetime] = None) -> dict:
    """Everything the dashboard needs, with no side effects."""
    now = now or datetime.now()
    expiry = credential_expiry()
    state = _read_state()
    ch = _channels(state)
    return {
        'enabled': cfg.VET_ENABLED,
        'cli': cfg.VET_CLI,
        'credential_expiry': expiry.isoformat() if expiry else None,
        'days_left': (expiry - now).days if expiry else None,
        'spawn_failures': int(state.get('spawn_failures') or 0),
        'silent_channels': silent_channels(state),
        'spawns_since_landing': {
            name: int(row.get('spawns_since_landing') or 0)
            for name, row in ch.items() if isinstance(row, dict)},
        'last_landed_at': state.get('last_landed_at'),
        'last_warned_on': state.get('last_warned_on'),
    }
