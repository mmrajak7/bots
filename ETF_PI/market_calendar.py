"""Exchange trading calendar: is a session open today, and from when to when (spec §8, §9c).

WHY: on 2026-10-02 (NSE closed for Gandhi Jayanti) the bot ran 375 scans on a frozen tape because
nothing told it the market was shut. The liquidity freshness gate catches a dead tape eventually,
but main() must not run at all on a closed session.

WHY MCX needs more than open/closed: on most NSE holidays MCX closes only its morning session and
trades the evening session from 17:00 (2026-09-14: MCX tape 17:00-23:30 only), on a few it is shut
all day (2026-10-02), and on New Year's Day it trades the morning only. So a day has one of four
types, and session() narrows the underlying's normal hours accordingly.

Data lives in market_holidays.yaml (one list per exchange per year, with the source circular).
A year missing from the YAML is treated as having no holidays (fail-open: the freshness gate is the
backstop), and coverage_problem() returns a message the caller logs as ERROR and sends to Telegram
daily, so a missing year cannot go unnoticed.

Pure functions: no network, no Telegram, no logging side effects. Callers own those.
"""
import datetime
import os

import yaml

HOLIDAYS_FILE = 'market_holidays.yaml'

EXCHANGES = ('NSE', 'MCX')
# Kite option exchange prefix (underlyings.Underlying.exchange) -> holiday calendar.
EXCHANGE_OF = {'NFO': 'NSE', 'MCX': 'MCX'}

OPEN = 'open'
CLOSED = 'closed'
EVENING_ONLY = 'evening_only'
MORNING_ONLY = 'morning_only'
HOLIDAY_TYPES = (CLOSED, EVENING_ONLY, MORNING_ONLY)
# NSE F&O has a single session, so half-day types would be meaningless there; reject them so a
# copy-paste from the MCX block cannot silently open NIFTY at 17:00.
ALLOWED_TYPES = {'NSE': (CLOSED,), 'MCX': HOLIDAY_TYPES}

# MCX morning session ends / evening session starts at 17:00 IST (MCX circular session timings).
MCX_SESSION_SPLIT = datetime.time(17, 0)

# From this month on, a missing NEXT year is reported (the exchanges publish in mid-December).
COVERAGE_REMINDER_MONTH = 12


def _as_date(value, where):
    """Accept a YAML date (PyYAML parses 2026-10-02 to datetime.date) or an ISO string."""
    if isinstance(value, datetime.datetime):
        raise ValueError(f"{where}: date must be a calendar date, not a timestamp: {value!r}")
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise ValueError(f"{where}: bad date {value!r} (expected YYYY-MM-DD)")


def _parse_year(exchange, year, block, path):
    where = f"{path}: {exchange} {year}"
    if not isinstance(block, dict):
        raise ValueError(f"{where}: expected a mapping with 'source' and 'holidays'")
    unknown = set(block) - {'source', 'note', 'holidays'}
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
    rows = block.get('holidays')
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ValueError(f"{where}: 'holidays' must be a list")
    source = block.get('source')
    if source is not None and not (isinstance(source, str) and source.strip()):
        raise ValueError(f"{where}: 'source' must be a non-empty string or null")
    if rows and source is None:
        # Every holiday row must be traceable to an official list for the yearly review.
        raise ValueError(f"{where}: holidays listed without a 'source'")

    days = {}
    for i, row in enumerate(rows):
        rw = f"{where} row {i + 1}"
        if not isinstance(row, dict):
            raise ValueError(f"{rw}: expected {{date, type, name}}, got {row!r}")
        missing = {'date', 'type', 'name'} - set(row)
        if missing:
            raise ValueError(f"{rw}: missing {sorted(missing)}")
        extra = set(row) - {'date', 'type', 'name'}
        if extra:
            raise ValueError(f"{rw}: unknown keys {sorted(extra)}")
        day = _as_date(row['date'], rw)
        if day.year != year:
            raise ValueError(f"{rw}: date {day} is filed under year {year}")
        htype = row['type']
        if htype not in ALLOWED_TYPES[exchange]:
            raise ValueError(f"{rw}: type {htype!r} not allowed for {exchange} "
                             f"(allowed: {', '.join(ALLOWED_TYPES[exchange])})")
        name = row['name']
        if not (isinstance(name, str) and name.strip()):
            raise ValueError(f"{rw}: 'name' must be a non-empty string")
        if day in days:
            raise ValueError(f"{rw}: duplicate date {day}")
        days[day] = (htype, name.strip())
    return {'source': source, 'note': block.get('note'), 'days': days}


def load(base_dir=None):
    """Read and validate market_holidays.yaml from `base_dir` (default: this module's directory).

    Returns {exchange: {year: {'source': str|None, 'note': str|None, 'days': {date: (type, name)}}}}.
    Raises ValueError with the file path and offending entry on any problem, including a missing
    or unparsable file, so the caller has one exception type to turn into an ERROR + alert.
    """
    base = base_dir or os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, HOLIDAYS_FILE)
    try:
        with open(path, encoding='utf-8') as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError:
        raise ValueError(f"{path}: holiday calendar not found") from None
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML: {exc}") from None
    except ValueError as exc:
        # PyYAML's own date constructor raises a bare ValueError for an impossible date such as
        # 2026-13-40 (it matches the date pattern, so it never reaches _as_date). Add the path.
        raise ValueError(f"{path}: bad date in YAML: {exc}") from None

    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level must map exchange -> years")
    unknown = set(raw) - set(EXCHANGES)
    if unknown:
        raise ValueError(f"{path}: unknown exchanges {sorted(map(str, unknown))} "
                         f"(expected {', '.join(EXCHANGES)})")
    cal = {}
    for exchange in EXCHANGES:
        years = raw.get(exchange)
        if not isinstance(years, dict) or not years:
            raise ValueError(f"{path}: {exchange} must map year -> holidays (missing or empty)")
        cal[exchange] = {}
        for year, block in years.items():
            # bool is an int subclass; `True:` as a key is a typo, not a year.
            if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2100:
                raise ValueError(f"{path}: {exchange}: bad year key {year!r}")
            cal[exchange][year] = _parse_year(exchange, year, block, path)
    return cal


def _date_of(day):
    # datetime is a date subclass but never equal to a date key; normalise so dict lookups hit.
    return day.date() if isinstance(day, datetime.datetime) else day


def _check_exchange(exchange, cal):
    if exchange not in cal:
        raise ValueError(f"unknown exchange {exchange!r}; known: {', '.join(cal)}")


def day_type(exchange, day, cal):
    """'open' | 'closed' | 'evening_only' | 'morning_only' for `exchange` ('NSE'/'MCX') on `day`.

    Weekends are closed. A year absent from the YAML yields 'open' on weekdays (fail-open, see
    module docstring); coverage_problem() is what flags it.
    """
    _check_exchange(exchange, cal)
    day = _date_of(day)
    if day.weekday() >= 5:
        return CLOSED
    year = cal[exchange].get(day.year)
    if year is None:
        return OPEN
    entry = year['days'].get(day)
    return entry[0] if entry else OPEN


def holiday_name(exchange, day, cal):
    """The listed holiday name for `day`, or None (for log / Telegram text)."""
    _check_exchange(exchange, cal)
    day = _date_of(day)
    year = cal[exchange].get(day.year)
    entry = year['days'].get(day) if year else None
    return entry[1] if entry else None


def exchange_for(underlying):
    """Holiday calendar for an underlyings.Underlying ('NFO' -> 'NSE', 'MCX' -> 'MCX')."""
    try:
        return EXCHANGE_OF[underlying.exchange]
    except KeyError:
        raise ValueError(f"{underlying.name}: no holiday calendar for exchange "
                         f"{underlying.exchange!r}") from None


def session(underlying, day, cal):
    """(open_time, close_time) for `underlying` on `day`, or None when it does not trade.

    Normal hours come from the Underlying record (session_open, session_close(day) - the MCX close
    moves with US DST). evening_only starts at 17:00, morning_only ends at 17:00; max/min keep a
    session that already lies inside the half from being widened.
    """
    day = _date_of(day)
    dtype = day_type(exchange_for(underlying), day, cal)
    if dtype == CLOSED:
        return None
    start, end = underlying.session_open, underlying.session_close(day)
    if dtype == EVENING_ONLY:
        start = max(start, MCX_SESSION_SPLIT)
    elif dtype == MORNING_ONLY:
        end = min(end, MCX_SESSION_SPLIT)
    if start >= end:
        # e.g. an underlying whose normal hours end before 17:00 on an evening_only day.
        return None
    return start, end


def year_covered(exchange, year, cal):
    """True if the YAML lists at least one holiday for `exchange` in `year`.

    An empty list counts as uncovered: no Indian exchange year has zero weekday holidays, so an
    empty list means "not yet published / not yet entered".
    """
    _check_exchange(exchange, cal)
    block = cal[exchange].get(year)
    return bool(block and block['days'])


def coverage_problem(today, cal):
    """Message describing missing calendar years, or None when coverage is fine.

    'MISSING ...'  current year absent for an exchange -> caller logs ERROR + daily Telegram.
    'REMINDER ...' from December 1, next year absent -> caller sends a reminder.
    Both are reported together when both apply.
    """
    today = _date_of(today)
    problems = []
    missing_now = [ex for ex in cal if not year_covered(ex, today.year, cal)]
    if missing_now:
        problems.append(f"MISSING holiday calendar {today.year} for {', '.join(missing_now)}: "
                        f"sessions run as normal weekdays; add the official list to {HOLIDAYS_FILE}")
    if today.month >= COVERAGE_REMINDER_MONTH:
        nxt = today.year + 1
        missing_next = [ex for ex in cal if not year_covered(ex, nxt, cal)]
        if missing_next:
            problems.append(f"REMINDER holiday calendar {nxt} not yet entered for "
                            f"{', '.join(missing_next)}: add the official list to {HOLIDAYS_FILE} "
                            f"before January 1")
    return ' | '.join(problems) if problems else None
