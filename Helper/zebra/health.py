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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from . import config as cfg

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


def _state_path():
    return cfg.LOG_DIR / STATE_FILE


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
    except Exception as e:                      # never let bookkeeping matter
        logger.debug('auth health state not written: %s', e)


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
            logger.debug('credential file %s unreadable: %s', p, e)
    return None


def record_spawn_result(ok: bool) -> int:
    """Track consecutive CLI spawn failures. Returns the current streak.

    The behavioural fallback: if the CLI cannot start, the layer is down
    regardless of what any credential file claims.
    """
    state = _read_state()
    streak = 0 if ok else int(state.get('spawn_failures') or 0) + 1
    state['spawn_failures'] = streak
    state['last_spawn_at'] = datetime.now().isoformat()
    _write_state(state)
    return streak


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
    if state.get('last_warned_on') == today:
        return None

    msg = None
    expiry = credential_expiry(paths)
    if expiry is not None:
        days = (expiry - now).days
        if days <= cfg.AUTH_WARN_DAYS:
            when = 'EXPIRED' if expiry <= now else 'in %d day(s)' % days
            msg = (f"🔑 <b>CLAUDE CLI LOGIN {'EXPIRED' if expiry <= now else 'EXPIRING'}"
                   f"</b>\nThe vetting layer's credential expires {when} "
                   f"({expiry.strftime('%Y-%m-%d %H:%M')}).\n"
                   f"<i>Log in on the Pi: <code>claude</code> then /login. "
                   f"Until then entries and exits fall back to the "
                   f"deterministic rules — trading continues unvetted.</i>")
    elif int(state.get('spawn_failures') or 0) >= 3:
        msg = (f"🔑 <b>CLAUDE CLI NOT STARTING</b>\n"
               f"{state['spawn_failures']} consecutive spawn failures — the "
               f"vetting layer is effectively OFF.\n"
               f"<i>Check auth on the Pi: <code>claude</code> then /login. "
               f"Trading continues on the deterministic rules.</i>")

    if not msg:
        return None
    if send and send(msg, dry_run=dry_run):
        state['last_warned_on'] = today
        _write_state(state)
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
    return {
        'enabled': cfg.VET_ENABLED,
        'credential_expiry': expiry.isoformat() if expiry else None,
        'days_left': (expiry - now).days if expiry else None,
        'spawn_failures': int(state.get('spawn_failures') or 0),
        'last_warned_on': state.get('last_warned_on'),
    }
