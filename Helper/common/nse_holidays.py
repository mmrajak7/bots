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
margins on the preceding session — **while a weekday-only count moves the
close LATER.** The two errors compound in the same direction, and the
direction is "still holding when the ramp starts".

WHAT THIS MODULE PROMISES, AND WHAT IT DOES NOT
-----------------------------------------------
It is a STATIC list. It cannot know about a holiday declared mid-year, and it
runs out at the end of its coverage window. Both facts are made VISIBLE rather
than absorbed: `covers()` answers whether a date is inside the window, and the
counters that use this log a warning when they are asked to count past it,
degrading to weekday-only — the old behaviour, out loud.

That is deliberate. A calendar that silently extrapolates is the same shape as
an options chain nobody checked the age of: it still parses, still answers,
still sizes the trade. See `feedback_a_stale_input_still_returns_a_number`.

SOURCING
--------
The 2026 list below was taken on 2026-08-29 from three independent
publications of the NSE calendar, which agree on every date from March
onwards. Two of the three also carry 2026-01-15 (Maharashtra municipal
elections, a special addition); the third omits it. It is included, and the
disagreement is recorded here rather than smoothed over — it is in the past
and cannot affect a forward session count either way.

**Before trusting this past 2026, re-derive it from the NSE circular.** It is
data, not logic, and the failure mode of stale data here is a position held
into the delivery ramp.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

#: Full trading holidays for the equity AND equity-derivatives segments (NSE
#: publishes one list for both). Muhurat sessions are NOT here: 2026-11-08 is
#: a Sunday special session, not a closure of an ordinary trading day, and
#: adding it would make a non-session look like one.
HOLIDAYS_2026 = {
    date(2026, 1, 15),    # Maharashtra municipal elections (see SOURCING)
    date(2026, 1, 26),    # Republic Day
    date(2026, 3, 3),     # Holi
    date(2026, 3, 26),    # Shri Ram Navami
    date(2026, 3, 31),    # Shri Mahavir Jayanti
    date(2026, 4, 3),     # Good Friday
    date(2026, 4, 14),    # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),     # Maharashtra Day
    date(2026, 5, 28),    # Bakri Id
    date(2026, 6, 26),    # Muharram
    date(2026, 9, 14),    # Ganesh Chaturthi
    date(2026, 10, 2),    # Mahatma Gandhi Jayanti
    date(2026, 10, 20),   # Dussehra
    date(2026, 11, 10),   # Diwali - Balipratipada
    date(2026, 11, 24),   # Prakash Gurpurb Sri Guru Nanak Dev
    date(2026, 12, 25),   # Christmas
}

HOLIDAYS = set(HOLIDAYS_2026)

#: The window this list actually describes. Anything outside it is UNKNOWN,
#: and the callers say so instead of assuming "no holidays".
COVERAGE_START = date(2026, 1, 1)
COVERAGE_END = date(2026, 12, 31)


def covers(d: date) -> bool:
    """Is `d` inside the window this calendar can speak for?"""
    return COVERAGE_START <= d <= COVERAGE_END


#: How far ahead a lapse is announced. Two months, because the fix is not a
#: code change: somebody has to find the NSE circular for the next year, and
#: NSE publishes it in December. A warning that first appears on the day the
#: window closes is a warning delivered too late to act on.
COVERAGE_WARN_DAYS = 60


def coverage_status(today: date) -> dict:
    """How much calendar is left, as a decision rather than a fact.

    `{'state', 'days_left', 'detail'}` where state is one of:

      ok        more than COVERAGE_WARN_DAYS of coverage remain
      expiring  inside the warning window; refresh it now
      expired   past COVERAGE_END; every session count is weekday-only

    This exists because the module's own honesty was passive. `sessions_between`
    warns when it is ASKED to count past the window -- correct, and it only
    fires once a position with a next-year expiry already exists, in a cron log,
    on the day it starts mattering. The failure mode of stale data here is a
    position held into the delivery ramp, which is not a thing to learn about
    from a log line. [[feedback_a_stale_input_still_returns_a_number]]

    Deliberately NOT auto-extending. A calendar that extrapolates is the same
    shape as an option chain nobody checked the age of: it still parses, still
    answers, still sizes the trade.
    """
    days = (COVERAGE_END - today).days
    if days < 0:
        return {'state': 'expired', 'days_left': days,
                'detail': 'the NSE holiday calendar ran out on %s. Every '
                          'session count is now WEEKDAYS-ONLY, which '
                          'OVER-estimates the sessions remaining and fires '
                          'every delivery close LATER, into the margin ramp. '
                          'Refresh common/nse_holidays.py from the NSE '
                          'circular.' % COVERAGE_END.isoformat()}
    if days <= COVERAGE_WARN_DAYS:
        return {'state': 'expiring', 'days_left': days,
                'detail': 'the NSE holiday calendar covers only %d more '
                          'day(s) (to %s). Refresh it from the NSE circular '
                          'for the next year before it lapses — past coverage '
                          'the session count silently degrades to '
                          'weekdays-only and every delivery close fires '
                          'LATER.' % (days, COVERAGE_END.isoformat())}
    return {'state': 'ok', 'days_left': days,
            'detail': 'covers to %s (%d days)'
                      % (COVERAGE_END.isoformat(), days)}


def is_holiday(d: date) -> bool:
    """A declared full closure. False for a date outside coverage — ask
    `covers()` first if the difference matters, which on a session count it
    does."""
    return d in HOLIDAYS


def is_session(d: date) -> bool:
    """A day the exchange trades: a weekday that is not a declared holiday."""
    return d.weekday() < 5 and not is_holiday(d)


#: Coverage ends this process has already complained about. The order engine
#: calls `sessions_to_expiry` per open trade per FIVE-SECOND poll, so an
#: un-deduped warning would write ~30,000 five-line notices a session the
#: moment a trade with a next-year expiry exists — burying the log it was
#: written to protect. Keyed by the end date, so a SECOND expiry past coverage
#: still gets its own line; per-process rather than per-day because the
#: monitor is one process per session and zebra's cron re-warns each cycle,
#: which is the right cadence for each.
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
    date when any part of the range falls outside `COVERAGE_END`. The count is still returned — degraded to
    weekday-only for the uncovered part, which is exactly the old behaviour —
    but it is returned NOISILY. A calendar that quietly stops knowing things
    is worse than no calendar, because the number keeps looking authoritative.
    """
    if end <= start:
        return 0
    if not covers(end) and warn is not None and _warn_once(end):
        warn('NSE holiday calendar covers to %s; counting sessions to %s '
             'WEEKDAYS-ONLY past that. A holiday in the uncovered stretch '
             'makes this an OVER-estimate — it will report more sessions '
             'than remain, and the close will fire LATER, into the delivery '
             'margin ramp. Refresh common/nse_holidays.py from the NSE '
             'circular.' % (COVERAGE_END.isoformat(), end.isoformat()))
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
    return sorted(h for h in HOLIDAYS if start <= h <= end)
