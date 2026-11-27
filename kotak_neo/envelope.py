"""Reading a NEO reply: one set of rules for every caller.

Measured on the live API (Pi, 2026-11-27), not the docs:
  * the message key is `errMsg`; the docs say `emsg`; login errors say `message`;
  * a 400 on cancel carried its text in `stat` itself;
  * the empty report is `{"stat":"Not_Ok","stCode":5203,"errMsg":"No Data"}`;
  * the documented dead-session body is stCode 1003 / "Invalid session".
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

MSG_KEYS = ('emsg', 'errMsg', 'message')

#: stCodes and message fragments that mean "this session is dead".
AUTH_STCODES = (1003,)
SESSION_DEAD_TEXT = ('invalid session', 'session expired', 'session ip')


def neo_msg(data: Optional[Dict[str, Any]]) -> str:
    """The message of a NEO body, whichever key carries it ('' if none)."""
    if not isinstance(data, dict):
        return ''
    for k in MSG_KEYS:
        v = data.get(k)
        if v not in (None, ''):
            return str(v)
    stat = str(data.get('stat', '')).strip()
    return stat if stat.lower() not in ('', 'ok', 'not_ok') else ''


def stcode(data: Optional[Dict[str, Any]]) -> Optional[int]:
    try:
        return int(str((data or {}).get('stCode')))
    except (TypeError, ValueError):
        return None


def is_ok(data: Optional[Dict[str, Any]]) -> bool:
    return isinstance(data, dict) and str(data.get('stat', '')).strip().lower() == 'ok'


def is_session_dead(status: int, data: Optional[Dict[str, Any]]) -> bool:
    """HTTP 401/403, the documented stCode, or a dead-session text."""
    if status in (401, 403):
        return True
    if not isinstance(data, dict) or is_ok(data):
        return False
    if stcode(data) in AUTH_STCODES:
        return True
    m = neo_msg(data).lower()
    return any(t in m for t in SESSION_DEAD_TEXT)


_OPAQUE = re.compile(r'[A-Za-z0-9_\-\.=]{24,}')


def scrub(text: Any, limit: int = 300) -> str:
    """A body made safe for a log line: long opaque runs (tokens, sids,
    account ids) are cut. Never log a body without it."""
    return _OPAQUE.sub('<redacted>', str(text))[:limit]
