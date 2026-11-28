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
SESSION_DEAD_TEXT = ('invalid session', 'session expired')

#: Refusals because of the caller's IP (Kotak static-IP page), two kinds
#: (review K5 + its review R1, 2026-11-28):
#:   WHITELIST -- 100008 / "static ip" / "ip ... whitelist": this IP may not
#:     trade. A login cannot fix it (and refused logins latch the day): NEVER a
#:     login; reads are unknown; the owner fixes the whitelist.
#:   SESSION IP -- 1037 / "session ip doesnt match": the session was MADE from
#:     another IP (the Pi's public IP changed). A login from this IP is the only
#:     fix, so it stays a dead session -- through the normal brakes -- and the
#:     owner is alerted as well.
#: Both are checked BEFORE `is_session_dead`, whose 401/403 rule claims bodies.
IP_WHITELIST_STCODES = (100008,)
IP_SESSION_STCODES = (1037,)
IP_STCODES = IP_WHITELIST_STCODES + IP_SESSION_STCODES
IP_SESSION_TEXT = ('session ip',)
IP_WHITELIST_TEXT = ('static ip',)
IP_TEXT = IP_SESSION_TEXT + IP_WHITELIST_TEXT + ('whitelist',)


#: 'ip', 'ips', 'ip address', 'ipaddress' as words (review F4)
_IP_WORD = re.compile(r'\bips?\b|\bip[\s_-]?address')


def _refused(data: Optional[Dict[str, Any]]) -> bool:
    return isinstance(data, dict) and not is_ok(data)


def is_session_ip_refusal(data: Optional[Dict[str, Any]]) -> bool:
    """The session was made from another IP: dead, a login from here fixes it."""
    if not _refused(data):
        return False
    code = stcode(data)
    if code in IP_WHITELIST_STCODES:
        return False                    # F4: the never-login code beats any text
    if code in IP_SESSION_STCODES:
        return True
    m = neo_msg(data).lower()
    return any(t in m for t in IP_SESSION_TEXT)


def is_ip_whitelist_refusal(data: Optional[Dict[str, Any]]) -> bool:
    """This IP may not use the API: no login can fix it. A bare 'whitelist'
    counts only next to 'ip' (review R9: a non-IP refusal naming a whitelist
    must not hand the trade to the owner)."""
    if not _refused(data) or is_session_ip_refusal(data):
        return False
    if stcode(data) in IP_WHITELIST_STCODES:
        return True
    m = neo_msg(data).lower()
    return (any(t in m for t in IP_WHITELIST_TEXT)
            or ('whitelist' in m and _IP_WORD.search(m) is not None))


def is_ip_refusal(data: Optional[Dict[str, Any]]) -> bool:
    """Either kind of IP refusal (the owner's static-IP alert fires on both)."""
    return is_session_ip_refusal(data) or is_ip_whitelist_refusal(data)


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
