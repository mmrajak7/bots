"""One definition of "why did the Kite call fail".

Written 2026-08-27 after the ZEBRA MONITORING BLIND alert told the owner his
access token had probably expired. It had not: it was generated at 08:45:05
that morning. The line immediately above the alert in
`logs/cron_zebra_20260827.log` said what had actually happened —

    14:40:36 [ERROR] playbook.magnet.scanner: LTP fetch failed: Too many requests
    14:40:36 [ERROR] zebra.monitor: MONITORING BLIND: no LTP for ANY of 9 ...

— a **rate limit**. The alert guessed, guessed wrong, and would have sent the
owner to regenerate a perfectly good token while the real cause carried on.

Three failures need three different responses and must never be conflated:

| cause          | what it means                        | what to do            |
|----------------|--------------------------------------|-----------------------|
| ``AUTH``       | the access token is dead/wrong       | re-auth               |
| ``RATE_LIMIT`` | too many requests per second         | back off, cut callers |
| ``NETWORK``    | the link or the OMS is down          | wait, it self-heals   |
| ``UNKNOWN``    | anything else                        | read the message      |

Classification is on the EXCEPTION, not on a substring of its text. The
`kiteconnect` client maps the server's own ``error_type`` field onto an
exception class and carries the HTTP status on ``.code``
(`kiteconnect/connect.py`, ``_request``), so both are available and both are
more reliable than the prose. A 429 is a rate limit whatever its message says;
`TokenException` is an auth failure whatever its message says. The text is
consulted only as a fallback for wrappers that lose the class (a
`RuntimeError` re-raised from a thread, say).

This module lives in `common/` deliberately: `zebra/` and `bcs/` both need it
and they already import each other in both directions, so a shared definition
has to sit below both. The standing lesson in this repo is that a rule fixed in
one place is silently not fixed in its copy — so there is exactly one copy, and
`bcs.spread_monitor._is_auth_error` delegates here rather than keeping its own.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Optional

AUTH = 'auth'
RATE_LIMIT = 'rate_limit'
NETWORK = 'network'
UNKNOWN = 'unknown'

# Kite returns HTTP 429 for its per-second rate limits. The python client
# raises whatever `error_type` the body names — in practice `NetworkException`
# or `DataException` — so the STATUS is the reliable signal, not the class.
_RATE_LIMIT_CODES = frozenset({429})

# Substring fallbacks, lowercased. Only reached when the exception carries no
# usable class or code. Ordered by how specific they are.
_RATE_LIMIT_TEXT = ('too many requests', 'rate limit', 'ratelimit',
                    'too many request')
_AUTH_TEXT = ('token', 'api_key', 'apikey', 'sessionexpired',
              'session expired', 'invalid credentials', 'unauthor')
_NETWORK_TEXT = ('connection', 'timed out', 'timeout', 'temporarily '
                 'unavailable', 'gateway', 'unreachable', 'name resolution',
                 'ssl', 'max retries exceeded')


def _code_of(exc) -> Optional[int]:
    """The HTTP status the client attached, if any."""
    code = getattr(exc, 'code', None)
    if isinstance(code, bool):          # bool is an int; never a status
        return None
    if isinstance(code, int):
        return code
    try:
        return int(str(code))
    except (TypeError, ValueError):
        return None


def classify(exc) -> str:
    """Return AUTH / RATE_LIMIT / NETWORK / UNKNOWN for a Kite call failure.

    Never raises: a classifier that can itself fail is one more thing to guard
    on the path where everything is already going wrong.
    """
    if exc is None:
        return UNKNOWN
    try:
        name = type(exc).__name__
        code = _code_of(exc)
        text = str(exc).lower()

        # 1. Status code first — it is the server's own answer.
        if code in _RATE_LIMIT_CODES:
            return RATE_LIMIT
        # 2. Then the class the client chose from the body's `error_type`.
        #    TokenException is 403 and means exactly one thing.
        if name == 'TokenException':
            return AUTH
        # 3. `Too many requests` can arrive under NetworkException/
        #    DataException with the 429 lost by a wrapper, so the text check
        #    for a rate limit runs BEFORE the class check for a network fault
        #    — otherwise a 429 dressed as NetworkException reads as "the link
        #    is down", which is the same wrong-diagnosis shape this module
        #    exists to stop.
        if any(t in text for t in _RATE_LIMIT_TEXT):
            return RATE_LIMIT
        if any(t in text for t in _AUTH_TEXT):
            return AUTH
        if name in ('NetworkException', 'DataException'):
            return NETWORK
        if any(t in text for t in _NETWORK_TEXT):
            return NETWORK
        return UNKNOWN
    except Exception:                    # pragma: no cover - belt and braces
        return UNKNOWN


def is_rate_limit(exc) -> bool:
    """True when Kite refused the call for exceeding a per-second limit."""
    return classify(exc) == RATE_LIMIT


def is_auth_error(exc) -> bool:
    """True when the access token is the problem. THE definition — see module
    docstring. `bcs.spread_monitor._is_auth_error` delegates here."""
    return classify(exc) == AUTH


def token_file_status(path) -> dict:
    """What the token file on disk actually says.

    Returns ``{'exists', 'generated_at', 'age_hours', 'is_today', 'summary'}``.

    The point of this function is to make "the token has expired" a CHECKED
    claim rather than a guess. On 2026-08-27 the file said
    ``generated_at: 2026-08-27T08:45:05`` — this morning's scheduled
    generation — while the alert asserted expiry anyway.

    Never raises. An unreadable file is itself a reportable fact, not an
    exception to handle at every call site.
    """
    out = {'exists': False, 'generated_at': None, 'age_hours': None,
           'is_today': None, 'summary': 'token file not checked'}
    try:
        if not path or not os.path.exists(str(path)):
            out['summary'] = f'token file MISSING at {path}'
            return out
        out['exists'] = True
        with open(str(path)) as f:
            data = json.load(f)
        raw = data.get('generated_at')
        out['generated_at'] = raw
        if not raw:
            out['summary'] = 'token file has no generated_at'
            return out
        gen = datetime.fromisoformat(str(raw))
        now = datetime.now()
        out['age_hours'] = round((now - gen).total_seconds() / 3600.0, 1)
        out['is_today'] = (gen.date() == now.date())
        out['summary'] = (
            f"token generated {raw} ({out['age_hours']}h ago, "
            f"{'TODAY' if out['is_today'] else 'NOT today — stale'})")
    except Exception as e:
        out['summary'] = f'token file unreadable: {e}'
    return out


def diagnose(exc, token_path=None) -> dict:
    """Cause + a sentence a human can act on, with the token file CHECKED.

    Returns ``{'cause', 'error', 'token', 'headline', 'advice'}``.

    `headline` names what happened. `advice` says what to do about it. Expiry
    is only ever asserted when the file on disk supports it — the whole point
    of this module.
    """
    cause = classify(exc)
    token = token_file_status(token_path) if token_path else None
    err = str(exc) if exc is not None else 'no exception was captured'

    if cause == RATE_LIMIT:
        headline = 'Kite RATE LIMIT (HTTP 429) — not an auth problem'
        advice = ('Too many requests per second. The token is fine. Cut the '
                  'per-cycle request count or back off; do NOT re-auth.')
    elif cause == AUTH:
        headline = 'Kite AUTH failure — the access token was rejected'
        advice = 'Regenerate the access token (SNAIL auth), then restart.'
    elif cause == NETWORK:
        headline = 'Kite NETWORK/OMS failure — neither auth nor rate limit'
        advice = ('The link or the broker backend is unavailable. Usually '
                  'self-heals; re-auth would not help.')
    else:
        headline = 'Kite call failed for an unclassified reason'
        advice = 'Read the error text below before assuming a cause.'

    # The token file gets the last word whenever it contradicts, or confirms,
    # the guess above. A stale file matters even under a rate limit — it is
    # the next failure waiting to happen.
    if token is not None:
        if cause == AUTH and token.get('is_today') is True:
            advice += (' NOTE: the token file was generated TODAY '
                       f"({token.get('generated_at')}), so expiry is NOT the "
                       'obvious explanation — check api_key and user id.')
        elif cause != AUTH and token.get('is_today') is False:
            advice += (' Separately, the token file is NOT from today '
                       f"({token.get('generated_at')}) — refresh it before it "
                       'becomes the next outage.')
    return {'cause': cause, 'error': err, 'token': token,
            'headline': headline, 'advice': advice}
