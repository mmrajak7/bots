"""Is the EOD digest actually being written? An absence that has to alert.

## Why this file exists

`logs/eod/<date>.json` is the arming-gate evidence record, and since
2026-09-03 the digest cron ALSO captures `logs/eod/paths_<date>.json` — the
5-minute value paths that every replay of this book is measured on.

Nothing fails when the digest does not run. No exception, no red line, no
Telegram: `logs/eod/` simply stays empty, and an empty directory is
indistinguishable from a quiet day. That is not hypothetical — **the digest
cron went uninstalled for 18 days** (found 2026-09-01) and the only reason it
was noticed at all is that somebody went looking for a file. Two of those days
(2026-08-19, 08-20) had already aged out of the retained logs and are gone
permanently.

And it happened AGAIN on 2026-09-07: no `logs/eod/2026-09-07.*`, no
`paths_2026-09-07.json`, and no `cron_digest_*` log at all — the line had not
fired. One session, caught the same way as the first time: by hand.

The cost is on a timer. `common.log_cleanup` gzips a `.log` at 7 days and
deletes the archive at 90, so an un-captured session's raw evidence is
destroyed roughly three months later. A gap found inside 90 days is a
`zebra digest --date` away from being fixed; a gap found after is permanent.

## The shape of the check

Deliberately the same shape as `common.nse_holidays.coverage_status` — a
`{'state', 'detail'}` decision rather than a fact, alerted by the monitor with
per-state cadence. Both answer the same kind of question: is a thing that
silently produces nothing still working?

**Never raises.** A coverage checker that can break the cycle it runs in is a
worse bug than the gap it watches for.

RETIRES WHEN: the digest becomes a step the engine runs itself rather than a
separate crontab line that can go missing.
"""

from __future__ import annotations

import logging
import io
import json
from datetime import date, datetime, time as dtime, timedelta

from common import nse_holidays
from common.market_session import IST

logger = logging.getLogger(__name__)

#: The crontab minute the digest runs at, plus room for it to finish. Before
#: this on a session day, "no digest" is the NORMAL state and must be silent —
#: a checker that cries all morning is one nobody reads by afternoon.
DIGEST_MINUTE = dtime(15, 47)
DIGEST_GRACE_MIN = 13

#: How far back to look. Bounded on purpose: the question is "is it working
#: now", and an unbounded scan would report the pre-installation era forever.
LOOKBACK_SESSIONS = 10


def _deadline_passed(today: date, d: date, now: datetime) -> bool:
    """Should session `d` already have a digest, as of `now`?"""
    if d < today:
        return True
    cutoff = (datetime.combine(d, DIGEST_MINUTE)
              + timedelta(minutes=DIGEST_GRACE_MIN))
    return now.replace(tzinfo=None) >= cutoff


def expected_sessions(today: date, now: datetime,
                      back: int = LOOKBACK_SESSIONS) -> list:
    """Trading sessions that should already hold a digest, newest first.

    Walks backwards through the holiday calendar rather than counting
    weekdays: a real NSE holiday has no session and therefore no digest, and
    counting it as a gap would make this checker cry on exactly the days the
    engine did nothing wrong.
    """
    out, d = [], today
    guard = 0
    while len(out) < back and guard < back * 6:
        guard += 1
        try:
            session = nse_holidays.is_session(d)
        except Exception:                        # pragma: no cover - paranoia
            session = d.weekday() < 5
        if session and _deadline_passed(today, d, now):
            out.append(d)
        d -= timedelta(days=1)
    return out


def _digest_absent_or_partial(out_dir, day: str, have: set) -> bool:
    """A digest built while the session was still open does not count.

    `digest.build` stamps `partial: true` when it runs before MARKET_CLOSE on
    the day it describes, and `write()` persists it anyway — deliberately, it
    is still the best available record at that moment. But it is not the day's
    evidence, and treating it as coverage would hide the missing 15:47 run.
    """
    if '%s.json' % day not in have:
        return True
    try:
        with io.open(str(out_dir / ('%s.json' % day)), encoding='utf-8') as f:
            return bool(json.load(f).get('partial'))
    except Exception:
        # Unreadable is not coverage either.
        return True


def _paths_absent_or_stale(day: str, have: set) -> bool:
    """A value-path capture frozen mid-session is not the session.

    `paths_2026-09-03.json` held 100 observations ending 11:15:16 against
    307-623 on its neighbours, because the first write of a day used to win
    permanently. `value_paths.capture_is_stale` compares the capture against
    its source log's mtime; it answers False once the log is gone, which is
    correct — at that point the capture is all there is and nothing can
    improve it.
    """
    if 'paths_%s.json' % day not in have:
        return True
    try:
        from zebra import value_paths
        return bool(value_paths.capture_is_stale(day))
    except Exception:
        return False


def coverage_status(today: date = None, now: datetime = None,
                    out_dir=None) -> dict:
    """`{'state', 'detail', 'missing', 'paths_missing'}`. Never raises.

      ok        every session in the window has a COMPLETE digest
      pending   no session has passed its deadline yet (silent). Rare: it
                needs an empty lookback, i.e. no session in ~60 days. A day
                whose digest is merely not-yet-due returns `ok`.
      missing   exactly one session is absent, partial or stale
      lapsed    two or more — the 18-day shape, back again
      unreadable the directory itself cannot be listed

    On the Pi the first sighting of a gap is the NEXT MORNING, not the same
    evening: cron stops the engine at 15:55 and `run_once` exits once the
    market is closed, so no cycle runs after the 16:00 deadline. That is a
    property of the schedule, not of this module.

    `missing` and `lapsed` are separated because they need different reactions
    and different urgency. One gap is "the cron did not fire today, rebuild it
    and look at the crontab". Two or more means nobody has been watching, and
    the 90-day deletion clock is already running on the earliest one.

    `paths_missing` is reported alongside rather than folded in: the digest and
    the value-path capture ride the SAME crontab line deliberately, so a day
    with one and not the other is a different fault from a day with neither,
    and flattening them would hide it.
    """
    from zebra import config as cfg
    # IST for both, like every date in this fleet: a naive `date.today()` on a
    # UTC box moves the digest deadline across a day boundary and would report
    # a healthy evening as a gap.
    ist_now = datetime.now(IST)
    today = today or ist_now.date()
    now = now or ist_now.replace(tzinfo=None)
    out_dir = out_dir if out_dir is not None else (cfg.LOG_DIR / 'eod')
    try:
        have = {p.name for p in out_dir.iterdir()} if out_dir.exists() else set()
    except Exception as e:
        return {'state': 'unreadable', 'missing': [], 'paths_missing': [],
                'detail': 'cannot list %s: %s' % (out_dir, e)}

    sessions = expected_sessions(today, now)
    if not sessions:
        return {'state': 'pending', 'missing': [], 'paths_missing': [],
                'detail': 'no session has passed its %s digest deadline yet'
                          % DIGEST_MINUTE.strftime('%H:%M')}

    # EXISTENCE IS NOT COVERAGE. The digest crontab runs at 15:00 AND 15:47,
    # and the 15:00 run writes a `partial: true` digest for a session still
    # open. Counting that as present makes this module blind to the exact
    # failure it exists for: the 15:47 line not firing on a day the 15:00 one
    # did. Same for the paths file, which `value_paths` can now tell us is
    # frozen at a mid-session extraction.
    missing = [d.isoformat() for d in sessions
               if _digest_absent_or_partial(out_dir, d.isoformat(), have)]
    paths_missing = [d.isoformat() for d in sessions
                     if _paths_absent_or_stale(d.isoformat(), have)]

    if not missing and not paths_missing:
        return {'state': 'ok', 'missing': [], 'paths_missing': [],
                'detail': 'digest + value paths present for the last %d '
                          'session(s), through %s'
                          % (len(sessions), sessions[0].isoformat())}

    worst = missing or paths_missing
    state = 'missing' if len(worst) == 1 else 'lapsed'
    bits = []
    if missing:
        bits.append('digest MISSING for %s' % ', '.join(missing))
    if paths_missing:
        bits.append('value paths MISSING for %s' % ', '.join(paths_missing))
    bits.append('rebuild with `python -m zebra digest --date <YYYY-MM-DD>` '
                'while the raw log survives — logs are gzipped at 7 days and '
                'DELETED at 90, after which the session is unrecoverable')
    return {'state': state, 'missing': missing, 'paths_missing': paths_missing,
            'detail': '; '.join(bits)}
