"""NSE trading holidays, and session counting that knows about them.

WHY THIS EXISTS (M4 / M10, 2026-08-29)
--------------------------------------
Both engines counted trading sessions to expiry by skipping weekends and
nothing else, and both said so in a docstring: "holidays are not known to this
repo, so a holiday inside the window makes this an OVER-estimate". That
over-estimate is not neutral — it points the wrong way against real money.

Indian stock options are PHYSICALLY settled, and NSE Clearing ramps a delivery
margin on the long ITM leg over the last four sessions (10% at EOD of E-4, 25%
at E-3, 45% at E-2, 70% at E-1; Risk Management FAQ Q24, restated in F&O
circular NCL/CMPT/73997 of 30 Apr 2026). The margin base is the leg AT ITS
STRIKE — full contract value — and the broker does not net the legs. For this
book that is ~Rs 2.82L demanded against a Rs 2L account.

**A holiday moves each margin tranche EARLIER** — NSE collects a holiday's
margins on the preceding session — **while a weekday-only session count moves
the close LATER.** The two errors compound in the same direction, and the
direction is "still holding when the ramp starts".

WHERE THE DATES COME FROM (rewritten 2026-08-30)
------------------------------------------------
`BOTS/data/holiday_calendar.json`, refreshed daily from Zerodha's published
calendar by `SNAIL/src/utils/holiday_scraper.py`. Read at CALL TIME, cached on
the file's mtime, so a refresh lands without a restart.

**It replaced a hand-written static list that was mostly wrong.** That list
shipped on 2026-08-29 claiming three independent publications of the NSE
calendar agreed on it. Checked against 160 daily FIFTY daemon logs — where a
real holiday leaves a ~20-35 line log (started, found the market closed,
exited) against ~5,700 on a trading day — it scored **2 of 6** evidenced
holidays and carried **6 dates that were full trading days** (2026-03-03,
03-26, 03-31, 04-14, 05-01, 05-28, 06-26). The daily file had 5 of those 6.

The three it MISSED (2026-02-19, 03-19, 04-01) are the half that costs money:
a missed holiday makes the count over-estimate, and the close then fires LATER,
into the ramp.

WHAT THIS MODULE PROMISES, AND WHAT IT DOES NOT
-----------------------------------------------
It knows exactly what the file knows, and it says so out loud when that is
nothing. A missing, unreadable or stale file, or a date in a year the file does
not cover, degrades the count to WEEKDAYS-ONLY — the old behaviour — and warns.
It never extrapolates and it never guesses: a calendar that quietly stops
knowing things is worse than no calendar, because the number keeps looking
authoritative (`feedback_a_stale_input_still_returns_a_number`).

**The file is taken as-is.** Owner decision, 2026-08-30: no hand-patching and
no reconciliation against observed sessions. Two known imperfections are
therefore live and deliberate — 2026-06-23 is an evidenced closure the file
does not list (count over-estimates by one session there), and 2026-08-26 is
listed while `logs/cron_zebra_20260826.log` shows a full 347-poll session
(count under-estimates by one, which fires the close early). A Muhurat special
session appears as a holiday too; those fall on Sundays, which `is_session`
already excludes.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: `common/` -> `Helper/` -> `BOTS/`. Resolved here rather than imported from
#: `zebra.config` (which already knows it) to keep `common/` free of either
#: engine — the same rule `common/arming.py` follows and for the same reason.
BOTS_ROOT = Path(__file__).resolve().parents[2]

#: Written daily by SNAIL's startup workflow and shared by every bot, next to
#: `kite_access_token.json`. The env override exists for tests and for a box
#: that lays the tree out differently.
HOLIDAY_FILE = Path(os.environ.get(
    'NSE_HOLIDAY_FILE', BOTS_ROOT / 'data' / 'holiday_calendar.json'))

#: How old `_last_updated` may get before the file is called stale. The scraper
#: re-runs daily, but a long weekend plus a holiday can legitimately leave it
#: untouched for four days, and an alarm that fires on a normal Tuesday is one
#: the reader stops seeing. Seven days is well inside the window in which a
#: newly-declared mid-year holiday would still be picked up before it mattered.
STALE_DAYS = 7

#: How far ahead the END of coverage is announced. Two months, because the fix
#: is not a code change: Zerodha publishes the next year's calendar in December,
#: and until it does the scraper has nothing to find.
COVERAGE_WARN_DAYS = 60

_YEAR_KEY = re.compile(r'^\d{4}$')

#: Parsed file, cached on identity so the ~30,000 session counts a trading day
#: performs do not re-read and re-parse it. Keyed on (mtime_ns, size) rather
#: than on a TTL: a refresh must land immediately, and a file that has not
#: changed cannot have new dates in it.
_cache: dict = {'key': None, 'holidays': frozenset(), 'years': frozenset(),
                'updated': None, 'error': None}


def _read() -> dict:
    """Load the calendar, cached on the file's identity. Never raises.

    Every failure lands in `_cache['error']` and leaves the holiday set EMPTY,
    which degrades counting to weekdays-only. That is the loud-and-wrong
    direction rather than the quiet-and-wrong one: `coverage_status` reports it
    and both engines alert on it.
    """
    path = HOLIDAY_FILE
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError as e:
        key = (str(path), None, None)
        if _cache['key'] != key:
            _cache.update(key=key, holidays=frozenset(), years=frozenset(),
                          updated=None,
                          error='%s is missing (%s)' % (path, e.__class__.__name__))
        return _cache
    if _cache['key'] == key:
        return _cache

    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError('not an object: %s' % type(raw).__name__)
        holidays, years = set(), set()
        for k, rows in raw.items():
            if not _YEAR_KEY.match(str(k)) or not isinstance(rows, list):
                continue
            years.add(int(k))
            for row in rows:
                d = row.get('date') if isinstance(row, dict) else row
                try:
                    holidays.add(date.fromisoformat(str(d)))
                except (TypeError, ValueError):
                    # ONE bad row must not discard the year. Named, because a
                    # date the parser silently dropped is a holiday the count
                    # does not know about, and that direction holds a position
                    # into the ramp.
                    logger.warning('holiday_calendar.json: unparseable date '
                                   '%r under %r — ignored', d, k)
        updated = None
        try:
            updated = date.fromisoformat(str(raw.get('_last_updated')))
        except (TypeError, ValueError):
            pass
        _cache.update(key=key, holidays=frozenset(holidays),
                      years=frozenset(years), updated=updated,
                      error=None if years else 'no year entries in %s' % path)
    except Exception as e:
        _cache.update(key=key, holidays=frozenset(), years=frozenset(),
                      updated=None,
                      error='%s is unreadable (%s: %s)'
                            % (path, e.__class__.__name__, str(e)[:80]))
    return _cache


def reload() -> dict:
    """Force a re-read on the next call. For tests, and for a caller that has
    just watched the file change under it."""
    _cache['key'] = None
    return _read()


def holidays() -> frozenset:
    """Every declared closure the file knows about."""
    return _read()['holidays']


def covered_years() -> frozenset:
    return _read()['years']


def coverage_end() -> Optional[date]:
    """Last day this calendar can speak for, or None when it knows nothing."""
    years = covered_years()
    return date(max(years), 12, 31) if years else None


def covers(d: date) -> bool:
    """Is `d` inside a year this calendar actually has data for?

    Per YEAR, not a single span: the file is a mapping of year to rows, and a
    year that is absent is absent whether or not it sits between two present
    ones. Asserting a contiguous window would claim knowledge of a gap.
    """
    return d.year in covered_years()


def coverage_status(today: date) -> dict:
    """How much calendar there is, as a decision rather than a fact.

    `{'state', 'days_left', 'detail'}` where state is one of:

      ok         the file is fresh and covers today's year with room to spare
      expiring   inside COVERAGE_WARN_DAYS of the last covered year ending
      expired    past the last covered year
      stale      the file has not been refreshed in STALE_DAYS
      missing    no file at all
      unreadable a file that is not a calendar

    This exists because the module's own honesty was passive. `sessions_between`
    warns when it is ASKED to count past what it knows — correct, and it only
    fires once a position with such an expiry already exists, in a cron log, on
    the day it starts mattering. The failure mode of stale data here is a
    position held into the delivery ramp, which is not a thing to learn about
    from a log line.
    """
    c = _read()
    if c['error'] and not c['holidays']:
        state = 'missing' if 'missing' in c['error'] else 'unreadable'
        return {'state': state, 'days_left': None,
                'detail': '%s. Every session count is WEEKDAYS-ONLY, which '
                          'OVER-estimates the sessions remaining and fires '
                          'every delivery close LATER, into the margin ramp. '
                          'It is written daily by SNAIL\'s startup workflow '
                          '(holiday_scraper.py) — check that it ran.'
                          % c['error']}
    end = coverage_end()
    if end is None:
        return {'state': 'unreadable', 'days_left': None,
                'detail': '%s parsed but names no year. Session counts are '
                          'WEEKDAYS-ONLY.' % HOLIDAY_FILE}
    updated = c['updated']
    if updated is not None and (today - updated).days > STALE_DAYS:
        return {'state': 'stale', 'days_left': (end - today).days,
                'detail': 'the NSE holiday calendar was last refreshed %s '
                          '(%d days ago). It is scraped daily, so this means '
                          'the job has stopped — a holiday declared since then '
                          'is unknown here, and an unknown holiday fires the '
                          'delivery close LATER.'
                          % (updated.isoformat(), (today - updated).days)}
    days = (end - today).days
    if days < 0:
        return {'state': 'expired', 'days_left': days,
                'detail': 'the NSE holiday calendar ran out on %s. Every '
                          'session count is now WEEKDAYS-ONLY, which '
                          'OVER-estimates the sessions remaining and fires '
                          'every delivery close LATER, into the margin ramp.'
                          % end.isoformat()}
    if days <= COVERAGE_WARN_DAYS:
        return {'state': 'expiring', 'days_left': days,
                'detail': 'the NSE holiday calendar covers only %d more '
                          'day(s) (to %s). Zerodha publishes the next year in '
                          'December and the scraper picks it up on its own; if '
                          'this is still showing in January, the job is not '
                          'running.' % (days, end.isoformat())}
    return {'state': 'ok', 'days_left': days,
            'detail': 'covers %s (%d holidays, refreshed %s)'
                      % ('/'.join(str(y) for y in sorted(covered_years())),
                         len(c['holidays']),
                         updated.isoformat() if updated else 'unknown')}


def is_holiday(d: date) -> bool:
    """A declared full closure. False for a date outside coverage — ask
    `covers()` first if the difference matters, which on a session count it
    does."""
    return d in holidays()


def is_session(d: date) -> bool:
    """A day the exchange trades: a weekday that is not a declared holiday."""
    return d.weekday() < 5 and not is_holiday(d)


#: Coverage ends this process has already complained about. The order engine
#: calls `sessions_to_expiry` per open trade per FIVE-SECOND poll, so an
#: un-deduped warning would write ~30,000 five-line notices a session the
#: moment a trade with an uncovered expiry exists — burying the log it was
#: written to protect. Keyed by the end date, so a SECOND uncovered expiry
#: still gets its own line; per-process rather than per-day because the monitor
#: is one process per session and zebra's cron re-warns each cycle, which is
#: the right cadence for each.
_WARNED_PAST_COVERAGE = set()


def _warn_once(end: date) -> bool:
    if end in _WARNED_PAST_COVERAGE:
        return False
    _WARNED_PAST_COVERAGE.add(end)
    return True


def reset_coverage_warnings() -> None:
    """For tests. A module-level set that survives between them makes the
    second test measure the first."""
    _WARNED_PAST_COVERAGE.clear()


def sessions_between(start: date, end: date, *, warn=None) -> int:
    """Trading sessions in `(start, end]` — start exclusive, end inclusive.

    That half-open shape is what both callers already meant by "sessions
    remaining": today does not count, expiry day does.

    `warn` takes one string and is called ONCE PER PROCESS per uncovered end
    date. The count is still returned — degraded to weekday-only for the
    uncovered part, which is exactly the old behaviour — but it is returned
    NOISILY. A calendar that quietly stops knowing things is worse than no
    calendar, because the number keeps looking authoritative.
    """
    if end <= start:
        return 0
    if not covers(end) and warn is not None and _warn_once(end):
        known = coverage_end()
        warn('NSE holiday calendar covers %s; counting sessions to %s '
             'WEEKDAYS-ONLY past that. A holiday in the uncovered stretch '
             'makes this an OVER-estimate — it will report more sessions '
             'than remain, and the close will fire LATER, into the delivery '
             'margin ramp. The calendar is %s.'
             % ('/'.join(str(y) for y in sorted(covered_years())) or 'NOTHING',
                end.isoformat(),
                'refreshed daily into %s by SNAIL' % HOLIDAY_FILE))
    sessions, cur = 0, start
    while cur < end:
        cur += timedelta(days=1)
        if is_session(cur):
            sessions += 1
    return sessions


def next_session(d: date, *, forward: bool = True) -> date:
    """The next (or previous) day the exchange trades, excluding `d` itself."""
    step = timedelta(days=1 if forward else -1)
    cur = d + step
    while not is_session(cur):
        cur += step
    return cur


def holidays_in(start: date, end: date) -> list:
    """Declared closures in `[start, end]`, sorted. For alert text: naming the
    holiday that moved a date is what lets a human check the arithmetic."""
    return sorted(h for h in holidays() if start <= h <= end)
