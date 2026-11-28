"""Telegram restart channel for the live naked entry kill: `/nresume`.

Design: `docs/NAKED_LIVE_DESIGN.md` Q7 -- owner chat id only, the one-time
code from the kill alert, Telegram can only CLEAR a kill, never set one, never
clear a pause, never change a limit. Q7 first placed `/nresume` in the FIFTY
daemon's Telegram poller; that daemon was DISABLED 2026-11-27, and the owner
chose instead: **zebra's own 5-minute cycle reads Telegram once per cycle,
ONLY while a kill is active.** No new process, no new cron line.

    /nresume CODE           -- clears the one killed mode
    /nresume live CODE      -- names the mode (required when BOTH are killed)
    /nresume rehearsal CODE

Backup at the box: `python -m zebra resume CODE --mode live|rehearsal`.

ENTRY POINT: `poll_resume(now, send)`. Call it from the cycle BEFORE
`naked_entry.begin_cycle`, so a cleared kill is seen by the same cycle's
`evaluate`. It NEVER raises -- the cycle that calls it also trades.

CONSTRAINTS (read before changing anything around this token):

  * NO KILL, NO NETWORK. With neither halt file killed, it returns before any
    request, so the common cycle costs one or two small file reads.
  * IT CONSUMES UPDATES. `getUpdates` with an advanced `offset` confirms every
    update below it to Telegram, for the whole bot. That is acceptable ONLY
    because nothing else polls this token: the FIFTY daemon and SNAIL's
    Telegram poller were disabled 2026-11-27, and MOMENTUM uses a different
    token. A revived poller (or a webhook) on the shared token would COMPETE
    with this one -- each would swallow commands meant for the other.
  * Telegram keeps an unconfirmed update for ~24 hours, and this only reads
    while the zebra cycle runs (market hours) AND a kill is active. A
    `/nresume` sent on a Friday evening, over a holiday, or before the kill
    exists is gone by the time anything reads it: resend it, or use the CLI.
  * An UNREADABLE halt file (`naked_halt.HaltUnreadable`, including a state
    file deleted under a journalled kill) is never resumable from Telegram --
    that case is for a human at the box.
  * At most `MAX_ATTEMPTS` resume attempts per cycle. Updates past the cap in
    the same batch are CONSUMED, not queued (the offset moves past them); the
    owner is told once and can resend next cycle. A wrong code is recorded by
    `naked_halt.resume` as `resume_refused`; the reply never contains the code.
  * The bot token is never logged: request exceptions carry the URL, which
    carries the token, so every error string is scrubbed before it is logged.
  * Replies contain no angle brackets: zebra's sender posts with HTML parse
    mode, and a literal '<' makes Telegram reject the whole message.

The offset lives in `cfg.LOG_DIR / naked_tg_offset.json` and is written
(atomically) BEFORE any command is acted on, so an update is handled at most
once; if that write fails, nothing is acted on this cycle. An offset older
than 48 h is discarded (`_load_offset` says why).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from common.filelock import exclusive

from . import config as cfg
from . import naked_halt

logger = logging.getLogger(__name__)

COMMAND = '/nresume'
MAX_ATTEMPTS = 3            # resume attempts handled per cycle
HTTP_TIMEOUT = 5.0          # seconds; getUpdates itself uses timeout=0
FETCH_LIMIT = 50            # updates per getUpdates call
LOCK_TIMEOUT = 5.0
OFFSET_MAX_AGE_HOURS = 48   # see _load_offset
USAGE = ('usage: /nresume CODE, or /nresume live CODE / /nresume rehearsal CODE '
         '(the mode is required when both are killed)')

_CMD_RE = re.compile(r'^/nresume(?:@\S+)?$', re.IGNORECASE)


# -- paths / small io --------------------------------------------------------------

def offset_path() -> Path:
    return cfg.LOG_DIR / 'naked_tg_offset.json'


def _lock_path() -> Path:
    return cfg.LOG_DIR / 'naked_tg.lock'


def _load_offset(now: datetime) -> Optional[int]:
    """The stored offset, or None (read from the oldest pending update).

    An offset older than `OFFSET_MAX_AGE_HOURS` is DROPPED: Telegram holds an
    unconfirmed update for ~24 h, so an old offset dedupes nothing -- and after
    a week with no updates Telegram picks the next update_id at RANDOM, which
    can land BELOW a stale offset and hide every new /nresume for good."""
    p = offset_path()
    try:
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
        v = int(d['offset'])
        saved = datetime.strptime(d['saved_at'], '%Y-%m-%d %H:%M:%S')
    except FileNotFoundError:
        return None
    except Exception as e:
        # A lost offset only re-reads what Telegram still holds (<= 24 h);
        # codes are one-time, so an old /nresume cannot clear a new kill.
        logger.warning('naked tg: offset file %s unreadable (%s) -- reading '
                       'from the oldest pending update', p, type(e).__name__)
        return None
    age_h = (now.replace(tzinfo=None) - saved).total_seconds() / 3600.0
    if age_h > OFFSET_MAX_AGE_HOURS or age_h < -1:
        logger.info('naked tg: offset saved %s is stale -- not used', d['saved_at'])
        return None
    return v


def _save_offset(offset: int, now: datetime) -> None:
    p = offset_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(p) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'offset': int(offset),
                   'saved_at': now.replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S')}, f)
    os.replace(tmp, str(p))


def _tg_config(config_path: Optional[Path]) -> Optional[dict]:
    p = Path(config_path) if config_path is not None else cfg.TELEGRAM_CONFIG
    try:
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
    except Exception as e:
        logger.error('naked tg: telegram config %s unreadable (%s) -- '
                     '/nresume not read this cycle', p, type(e).__name__)
        return None
    if not isinstance(d, dict) or not d.get('bot_token') or not d.get('chat_id'):
        logger.error('naked tg: telegram config %s lacks bot_token/chat_id', p)
        return None
    return {'bot_token': str(d['bot_token']), 'chat_id': str(d['chat_id']).strip()}


def _scrub(text: str, token: str) -> str:
    return str(text).replace(token, '***') if token else str(text)


# -- which modes are resumable ---------------------------------------------------------

def killed_modes() -> List[str]:
    """Modes whose halt file READS as killed. Unreadable files are excluded
    (and logged): Telegram never clears what the code cannot read."""
    out = []
    for m in ('live', 'rehearsal'):
        try:
            if naked_halt.load(m)['killed']:
                out.append(m)
        except naked_halt.HaltUnreadable as e:
            logger.error('naked tg: %s halt file unreadable -- not resumable '
                         'from Telegram, fix at the box (%s)', m, e)
        except Exception as e:
            logger.error('naked tg: %s halt state check failed (%s)', m, e)
    return out


# -- parsing -----------------------------------------------------------------------------

def parse(text: str, killed: List[str]):
    """('ignore', None, None) for anything that is not /nresume;
    ('usage', None, why) for a malformed one; ('resume', mode, code)."""
    parts = (text or '').split()
    if not parts or not _CMD_RE.match(parts[0]):
        return 'ignore', None, None
    args = parts[1:]
    if len(args) == 1:
        if len(killed) == 1:
            return 'resume', killed[0], args[0]
        if not killed:
            return 'usage', None, 'no naked mode is killed (or its file is unreadable)'
        return 'usage', None, 'both live and rehearsal are killed -- name the mode'
    if len(args) == 2 and args[0].lower() in naked_halt.MODES:
        return 'resume', args[0].lower(), args[1]
    return 'usage', None, 'malformed'


# -- the entry point -----------------------------------------------------------------------

def poll_resume(now: datetime, send: Optional[Callable[[str], object]] = None, *,
                http=None, config_path: Optional[Path] = None) -> list:
    """Read Telegram once for `/nresume` while a naked kill is active.

    `send(text)` replies to the owner (default: zebra's Telegram sender).
    `http` is a `requests`-like object with `.get(url, params=, timeout=)`.
    Returns one dict per handled command: ``{'update_id', 'mode', 'result'}``
    with result in cleared | wrong_code | not_killed | unreadable | error |
    usage | rate_limited. Never raises.
    """
    try:
        if not killed_modes():
            return []                                 # the common case: no network
        try:
            with exclusive(_lock_path(), timeout=LOCK_TIMEOUT):
                return _poll_locked(now, send, http, config_path)
        except Exception as e:
            logger.error('naked tg: poll failed (%s)', type(e).__name__)
            return []
    except Exception as e:                            # pragma: no cover - belt and braces
        logger.error('naked tg: unexpected failure (%s)', type(e).__name__)
        return []


def _default_send(text: str) -> object:
    from .monitor import _send_telegram           # lazy: monitor may import us
    return _send_telegram(text)


def _reply(send, text: str) -> None:
    try:
        send(text)
    except Exception as e:
        logger.error('naked tg: reply failed (%s)', type(e).__name__)


def _poll_locked(now, send, http, config_path) -> list:
    killed = killed_modes()                           # re-read under the lock
    if not killed:
        return []
    tg = _tg_config(config_path)
    if tg is None:
        return []
    token, owner = tg['bot_token'], tg['chat_id']
    send = _default_send if send is None else send
    if http is None:
        import requests as http                       # lazy: tests never need it

    offset = _load_offset(now)
    # No `allowed_updates`: Telegram REMEMBERS that setting for the bot, so it
    # would silently filter any later poller on the shared token. Non-message
    # updates are skipped client-side instead.
    params = {'timeout': 0, 'limit': FETCH_LIMIT}
    if offset is not None:
        params['offset'] = offset
    try:
        resp = http.get('https://api.telegram.org/bot%s/getUpdates' % token,
                        params=params, timeout=HTTP_TIMEOUT)
    except Exception as e:
        logger.warning('naked tg: getUpdates failed (%s: %s)', type(e).__name__,
                       _scrub(e, token)[:200])
        return []
    try:
        status = getattr(resp, 'status_code', None)
        body = resp.json()
    except Exception as e:
        logger.warning('naked tg: getUpdates unreadable reply (HTTP %s, %s)',
                       getattr(resp, 'status_code', '?'), type(e).__name__)
        return []
    if status != 200 or not isinstance(body, dict) or not body.get('ok'):
        desc = body.get('description') if isinstance(body, dict) else None
        logger.warning('naked tg: getUpdates refused (HTTP %s: %s)', status,
                       _scrub(desc or '?', token)[:200])
        return []
    updates = [u for u in (body.get('result') or [])
               if isinstance(u, dict) and isinstance(u.get('update_id'), int)
               and (offset is None or u['update_id'] >= offset)]
    if not updates:
        return []
    updates.sort(key=lambda u: u['update_id'])

    # Consume FIRST: if this write fails, act on nothing (a replayed wrong code
    # would otherwise re-refuse and re-reply every cycle).
    try:
        _save_offset(updates[-1]['update_id'] + 1, now)
    except Exception as e:
        logger.error('naked tg: offset not saved (%s) -- no command acted on '
                     'this cycle', type(e).__name__)
        return []

    handled, attempts, capped = [], 0, False
    for u in updates:
        msg = u.get('message')
        if not isinstance(msg, dict):
            continue                                  # edited_message etc.: ignored
        chat = msg.get('chat') if isinstance(msg.get('chat'), dict) else {}
        chat_id = str(chat.get('id', '')).strip()
        sender = msg.get('from') if isinstance(msg.get('from'), dict) else {}
        from_id = str(sender.get('id', '')).strip()
        text = msg.get('text') if isinstance(msg.get('text'), str) else ''
        # The CHAT and the SENDER must both be the owner (review R10): in a
        # group chat the chat id alone would let any member resume.
        if chat_id != owner or from_id != owner:
            if text.strip().lower().startswith(COMMAND):
                logger.warning('naked tg: /nresume from chat %s ignored (not the '
                               'owner)', chat_id or '?')
            else:
                logger.info('naked tg: message from chat %s ignored', chat_id or '?')
            continue
        kind, mode, arg = parse(text, killed)
        if kind == 'ignore':
            continue
        if attempts >= MAX_ATTEMPTS:
            if not capped:
                _reply(send, 'naked /nresume: more than %d attempts this cycle -- '
                             'the rest were dropped; resend in 5 minutes'
                       % MAX_ATTEMPTS)
                capped = True
            handled.append({'update_id': u['update_id'], 'mode': mode,
                            'result': 'rate_limited'})
            continue
        attempts += 1
        if kind == 'usage':
            _reply(send, 'naked /nresume: %s\n%s' % (arg, USAGE))
            handled.append({'update_id': u['update_id'], 'mode': None,
                            'result': 'usage'})
            continue
        handled.append({'update_id': u['update_id'], 'mode': mode,
                        'result': _resume(mode, arg, killed, now, chat_id, send)})
    return handled


def _resume(mode, code, killed, now, chat_id, send) -> str:
    M = mode.upper()
    if mode not in killed:
        # Never call resume() here: it can clear a journalled kill under a
        # MISSING state file, which Telegram must not do.
        _reply(send, 'naked %s: not killed (or its halt file is unreadable) -- '
                     'nothing resumed from Telegram' % M)
        return 'not_killed'
    try:
        ev = naked_halt.resume(mode, code, now, sender='telegram:%s' % chat_id)
    except naked_halt.HaltUnreadable as e:
        logger.error('naked tg: %s halt file unreadable at resume (%s)', mode, e)
        _reply(send, 'naked %s: halt file unreadable -- NOT resumed; fix at the box'
               % M)
        return 'unreadable'
    except ValueError as e:
        if 'wrong' in str(e):
            logger.warning('naked tg: %s resume REFUSED (wrong code) from chat %s',
                           mode, chat_id)
            _reply(send, 'naked %s: wrong restart code -- still KILLED, no new '
                         'entries' % M)
            return 'wrong_code'
        _reply(send, 'naked %s: not killed -- nothing to resume' % M)
        return 'not_killed'
    except Exception as e:
        logger.error('naked tg: %s resume failed (%s)', mode, type(e).__name__)
        _reply(send, 'naked %s: resume FAILED (%s) -- still killed; use the CLI'
               % (M, type(e).__name__))
        return 'error'
    _reply(send, 'naked %s entries RESUMED (kill: %s at %s). The peak resets to '
                 'equity on the next cycle; pauses and limits unchanged.'
           % (M, ev.get('kind'), ev.get('killed_at')))
    return 'cleared'
