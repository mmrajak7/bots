"""IST clock and trading-session arithmetic.

Sessions come from `Helper/common/nse_holidays.py`, the box-wide reader of
`BOTS/data/holiday_calendar.json` (refreshed daily by SNAIL). If that module
cannot be imported, the count degrades to weekdays-only and SAYS so once:
the session count only stamps DTE on a trade for the report, it never decides
an entry or an exit, so a degraded count costs accuracy of a label, not money.

The expiry roll does NOT depend on this module: it is read from the
instrument list (see `selection.pick_expiry`), which knows holiday-shifted
expiries by construction.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta, timezone

from .config import BOTS_ROOT

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

try:
    sys.path.insert(0, str(BOTS_ROOT / 'Helper'))
    from common import nse_holidays as _nh  # type: ignore
except Exception as exc:  # pragma: no cover - depends on the box layout
    _nh = None
    logger.warning('nse_holidays unavailable (%s): session counts are '
                   'WEEKDAYS-ONLY and over-count across a holiday', exc)


def now_ist() -> datetime:
    return datetime.now(IST)


def at(d: date, t) -> datetime:
    return datetime.combine(d, t).replace(tzinfo=IST)


def is_session(d: date) -> bool:
    if _nh is not None:
        return _nh.is_session(d)
    return d.weekday() < 5


def sessions_between(start: date, end: date) -> int:
    """Sessions in (start, end] — Volrix's DTE: today excluded, expiry included."""
    if _nh is not None:
        return _nh.sessions_between(start, end, warn=logger.warning)
    n, cur = 0, start
    while cur < end:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n
