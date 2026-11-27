"""EIA event calendar -- gates paper MCX option trading around scheduled US EIA releases.

WHY this module exists (spec tasks/SPEC_multi_instrument_2026-10-06.md S3, S9, S12):
  The weekly EIA crude (WPSR, Wed 10:30 ET) and natural-gas storage (NGS, Thu 10:30 ET) reports
  move CRUDEOILM / NATGASMINI premiums by more than the strategy's whole target in seconds. The
  magnet strategy must not enter shortly before a release and must be flat a few minutes before
  it. A wrong calendar is the failure that matters, so every doubtful case FAILS CLOSED.

Design decisions (each traced to a review finding):
  * Rules live in America/New_York and are converted per date (L1): 10:30 ET is 20:00 IST in US
    summer time and 21:00 IST after 2026-11-01. pytz needs .localize(); passing tzinfo=pytz_tz
    to datetime() silently uses LMT offsets, so that form never appears here.
  * Holiday shifts come from events_override.yaml (authoritative). The EIA schedule pages list
    exceptions only, so verification is per standard release date ("anchor"): page parsed + no
    row for that anchor = standard schedule VERIFIED; a row = its date+time must equal the YAML
    (C2). The old scraper compared the page's LATEST row and agreed falsely on shifted weeks --
    no code path here looks at a row that is not mapped to the anchor being checked.
  * A disagreement is ENFORCED, not just alerted: an OPEN exception makes the gate block the
    union of both candidate windows until a later check agrees (RESOLVED) or the user acks it
    with the exact (id, event, date, yaml_time_et) triple (M15).
  * An event-day scrape failure -> UNVERIFIED -> block 19:00 IST -> end of day on the Wed/Thu/Fri
    of that release week (C3, spec S9(b)), because a shift we could not see may land on any of
    those evenings. If the event-day verification never ran at all (cron/Pi failure) the same
    fail-closed window applies: "not verified today" is treated exactly like "could not verify".
  * Gate predicates are STATES (M2): must_flatten() is true for the whole T-5 .. T+30 span, so a
    flock-skipped cron minute at T-5 cannot let a position hold through the release.
  * Audit (M17): event_exceptions.json is non-dated and never contains '_2' (the Pi cleanup cron
    deletes *_2*.json older than 7 days); writes are atomic (tmp + os.replace) because
    option_buy processes read it every minute while the CLI writes it.

Public API for option_buy.py (pure or read-only; see each docstring for the raise contract):
  et_to_ist, scheduled_instances, windows_for, entry_blocked, must_flatten, fail_closed_windows.

CLI (Pi cron):
  python event_calendar.py build  [--date YYYY-MM-DD]     morning: windows file + event-day verify
  python event_calendar.py verify [--date YYYY-MM-DD] [--week]
                                  17:00 Wed/Thu re-check; --week = Sunday preview of next week
                                  (a Sunday-only fetch/parse failure creates no exception).

Known gaps (accepted in spec S12/M14): same-day EIA delays announced only on the day, and shifts
to non-standard weekdays outside Wed/Thu/Fri that are neither in the YAML nor on the page.
"""
import argparse
import datetime
import json
import logging
import logging.handlers
import os
import re
import sys
import time

import pytz
import yaml

log = logging.getLogger('event_calendar')
# Library use (option_buy, tests) must not write files or spam stderr; the CLI adds handlers.
log.addHandler(logging.NullHandler())

IST = pytz.timezone('Asia/Kolkata')
ET = pytz.timezone('America/New_York')
MODULE_DIR = os.path.dirname(os.path.realpath(__file__))

EVENTS_FILE = 'events.yaml'
OVERRIDE_FILE = 'events_override.yaml'
EXCEPTIONS_FILE = 'event_exceptions.json'      # non-dated, no '_2' -> survives the Pi cleanup
WINDOWS_FILE_FMT = 'event_windows_{}.json'      # dated on purpose: a 7-day audit copy is enough
LOG_FILE = os.path.join('logs', 'event_calendar.log')
LOG_RETENTION_DAYS = 30                         # M17: 30-day log of exceptions/blocks

# ---- Frozen gate constants (spec S3/S9). Code, not YAML: fail_closed_windows() must work when
# ---- the YAML is unreadable, and two sources for one number drift.
POST_EVENT_MIN = 30                     # no entry / stay flat until T+30
FLATTEN_LEAD_MIN = 5                    # flatten from T-5
FAIL_CLOSED_BLOCK_FROM = (19, 0)        # IST; earliest standard release is 20:00 IST (EDT)
FAIL_CLOSED_FLATTEN_FROM = (18, 55)     # IST; flat 5 min before the fail-closed block starts
FAIL_CLOSED_WEEKDAYS = (2, 3, 4)        # Wed, Thu, Fri -- holiday shifts land on Thu/Fri
# Processes that the events gate. Hardcoded (not from YAML) for the same reason as above; the
# loader warns if events.yaml names an underlying outside this set.
FAIL_CLOSED_UNDERLYINGS = ('CRUDEOILM', 'NATGASMINI')
# Used if a caller passes an invalid TIME_SL: larger than every frozen TIME_SL (30/45) so an
# input bug widens the block instead of shrinking it.
FALLBACK_TIME_SL_MIN = 60
# Instances are considered for a day if their window can overlap it; covers TIME_SL up to 120.
MAX_PRE_EVENT_MIN = 120

STATUS_OPEN = 'OPEN'
STATUS_UNVERIFIED = 'UNVERIFIED'
STATUS_RESOLVED = 'RESOLVED'
STATUS_ACKED = 'ACKED'
_ACTIVE = (STATUS_OPEN, STATUS_UNVERIFIED, STATUS_ACKED)

_WEEKDAYS = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
_MONTHS = {m: i + 1 for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'])}

# Indirection so the atomic-write test can make the rename fail without patching os globally.
_replace = os.replace


class EventCalendarError(Exception):
    """Calendar config/state unusable. option_buy must switch to fail_closed_windows()."""


class FetchError(Exception):
    """EIA page could not be downloaded (HTTP error, timeout, network)."""


class ParseError(Exception):
    """EIA page downloaded but its structure is not the one we know how to read."""


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _as_date(d):
    """Accept date, datetime or 'YYYY-MM-DD'; YAML gives date objects for unquoted dates."""
    if isinstance(d, datetime.datetime):
        return d.date()
    if isinstance(d, datetime.date):
        return d
    if isinstance(d, str):
        return datetime.datetime.strptime(d.strip(), '%Y-%m-%d').date()
    raise ValueError('not a date: %r' % (d,))


def _parse_hhmm(s):
    """'10:30' -> (10, 30). Raises ValueError -- callers wrap it as a config error."""
    if not isinstance(s, str):
        raise ValueError('time must be a quoted "HH:MM" string, got %r' % (s,))
    m = re.match(r'^\s*(\d{1,2}):(\d{2})\s*$', s)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError('bad time %r (want "HH:MM")' % (s,))
    return int(m.group(1)), int(m.group(2))


def _fmt_hhmm(t):
    return '%02d:%02d' % (t[0], t[1])


def et_to_ist(date, hh, mm):
    """America/New_York wall time on `date` -> aware Asia/Kolkata datetime.

    is_dst=None makes pytz raise for the non-existent/ambiguous 01:00-03:00 hour on DST switch
    days instead of guessing; EIA never publishes then, so a raise means a config typo.
    Raises ValueError on an invalid date/time (documented; callers treat it as config error).
    """
    d = _as_date(date)
    naive = datetime.datetime(d.year, d.month, d.day, int(hh), int(mm))
    try:
        local = ET.localize(naive, is_dst=None)
    except (pytz.exceptions.AmbiguousTimeError, pytz.exceptions.NonExistentTimeError) as e:
        raise ValueError('ambiguous/non-existent ET time %s: %s' % (naive, e))
    return local.astimezone(IST)


def _as_ist(now):
    """Naive datetimes are IST wall clock (option_buy and the Pi run in IST)."""
    if isinstance(now, str):
        now = datetime.datetime.fromisoformat(now)
    if not isinstance(now, datetime.datetime):
        raise ValueError('now must be a datetime, got %r' % (now,))
    if now.tzinfo is None:
        return IST.localize(now)
    return now.astimezone(IST)


def _ist_day_bounds(day):
    d = _as_date(day)
    start = IST.localize(datetime.datetime(d.year, d.month, d.day))
    # IST has no DST, so +1 day is exactly the next midnight.
    return start, start + datetime.timedelta(days=1)


def _ist_at(day, hm):
    d = _as_date(day)
    return IST.localize(datetime.datetime(d.year, d.month, d.day, hm[0], hm[1]))


def _now_ist():
    return datetime.datetime.now(IST)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _read_yaml(path):
    if not os.path.exists(path):
        raise EventCalendarError('missing config file %s' % path)
    try:
        with open(path, encoding='utf-8') as f:
            data = yaml.safe_load(f)
    except Exception as e:
        raise EventCalendarError('unreadable YAML %s: %s' % (path, e))
    if not isinstance(data, dict):
        raise EventCalendarError('%s: top level must be a mapping' % path)
    return data


def _parse_rules(data, path):
    events = data.get('events')
    if not isinstance(events, list) or not events:
        raise EventCalendarError('%s: "events" must be a non-empty list' % path)
    rules, seen = [], set()
    for i, e in enumerate(events):
        try:
            if not isinstance(e, dict):
                raise ValueError('entry must be a mapping')
            rid = str(e['id']).strip()
            if not rid or rid in seen:
                raise ValueError('empty or duplicate id %r' % rid)
            wd = _WEEKDAYS.get(str(e['weekday']).strip().lower()[:3])
            if wd is None:
                raise ValueError('bad weekday %r' % e['weekday'])
            und = e['underlyings']
            if not isinstance(und, list) or not und or not all(isinstance(u, str) for u in und):
                raise ValueError('underlyings must be a non-empty list of names')
            src = str(e['source']).strip()
            if not src.startswith('http'):
                raise ValueError('source must be a URL')
            rules.append({
                'id': rid, 'name': str(e.get('name', rid)), 'weekday': wd,
                'time_et': _parse_hhmm(e['time_et']), 'underlyings': list(und),
                'source': src, 'page_marker': str(e.get('page_marker', '')).strip(),
            })
            seen.add(rid)
        except (KeyError, ValueError, TypeError) as ex:
            raise EventCalendarError('%s: events[%d] invalid: %s' % (path, i, ex))
    return rules


def _parse_overrides(data, rules, path):
    """Overrides are the authority for shifts; a malformed row must stop the build (fail-closed)
    rather than be skipped, because a skipped row is a release traded through."""
    by_id = {r['id']: r for r in rules}
    rows = data.get('overrides') or []
    if not isinstance(rows, list):
        raise EventCalendarError('%s: "overrides" must be a list' % path)
    out, seen = [], set()
    for i, o in enumerate(rows):
        try:
            if not isinstance(o, dict):
                raise ValueError('entry must be a mapping')
            ev = str(o['event']).strip()
            if ev not in by_id:
                raise ValueError('unknown event %r' % ev)
            orig = _as_date(o['original_date'])
            if orig.weekday() != by_id[ev]['weekday']:
                raise ValueError('original_date %s is not a standard %s release day'
                                 % (orig, ev))
            key = (ev, orig)
            if key in seen:
                raise ValueError('duplicate override for %s %s' % key)
            seen.add(key)
            out.append({'event': ev, 'original_date': orig, 'date': _as_date(o['date']),
                        'time_et': _parse_hhmm(o['time_et']),
                        'reason': str(o.get('reason', '')).strip()})
        except (KeyError, ValueError, TypeError) as ex:
            raise EventCalendarError('%s: overrides[%d] invalid: %s' % (path, i, ex))
    return out


def _parse_acks(data):
    """Malformed acks are dropped with a warning, not fatal: ignoring an ack only keeps the
    wider (safe) window, so it must not take the whole calendar down."""
    acks, warnings = [], []
    raw = data.get('acks') or []
    if not isinstance(raw, list):
        return [], ['acks must be a list -- all acks ignored']
    for i, a in enumerate(raw):
        try:
            acks.append({'id': str(a['id']).strip(), 'event': str(a['event']).strip(),
                         'date': _as_date(a['date']),
                         'yaml_time_et': _fmt_hhmm(_parse_hhmm(a['yaml_time_et']))})
        except Exception as ex:
            warnings.append('ack[%d] malformed, ignored: %s (%r)' % (i, ex, a))
    return acks, warnings


def load_config(base_dir=None):
    """Read events.yaml + events_override.yaml.

    Returns {'rules', 'overrides', 'acks', 'fetch', 'warnings'}.
    Raises EventCalendarError if either file is missing or invalid (caller fails closed).
    """
    base_dir = base_dir or MODULE_DIR
    ev_path = os.path.join(base_dir, EVENTS_FILE)
    ov_path = os.path.join(base_dir, OVERRIDE_FILE)
    ev = _read_yaml(ev_path)
    rules = _parse_rules(ev, ev_path)
    ov = _read_yaml(ov_path)
    overrides = _parse_overrides(ov, rules, ov_path)
    acks, warnings = _parse_acks(ov)
    for r in rules:
        extra = [u for u in r['underlyings'] if u not in FAIL_CLOSED_UNDERLYINGS]
        if extra:
            # fail_closed_windows() cannot protect these when the YAML is unreadable.
            warnings.append('%s gates %s which is not in FAIL_CLOSED_UNDERLYINGS'
                            % (r['id'], extra))
    f = ev.get('fetch') or {}
    try:
        fetch = {'timeout_s': float(f.get('timeout_s', 20)),
                 'retry_wait_s': float(f.get('retry_wait_s', 5)),
                 'user_agent': str(f.get('user_agent', 'ETF_PI-event_calendar/1.0'))}
    except (TypeError, ValueError) as e:
        raise EventCalendarError('%s: bad fetch settings: %s' % (ev_path, e))
    return {'rules': rules, 'overrides': overrides, 'acks': acks, 'fetch': fetch,
            'warnings': warnings}


# ---------------------------------------------------------------------------
# Schedule (pure)
# ---------------------------------------------------------------------------

def scheduled_instances(day, rules, overrides):
    """Event instances whose America/New_York release DATE is `day`.

    Returns [{event, underlyings, T (aware IST), et_date, time_et 'HH:MM',
              source 'rule'|'override', anchor (standard date it belongs to), reason}].
    An override removes the instance on its original_date and adds one on its date.
    Raises EventCalendarError on malformed rule/override dicts (pass load_config() output).
    """
    try:
        d = _as_date(day)
        out = []
        for r in rules:
            mine = [o for o in overrides if o['event'] == r['id']]
            moved_away = {o['original_date'] for o in mine}
            if d.weekday() == r['weekday'] and d not in moved_away:
                out.append(_instance(r, d, r['time_et'], 'rule', d, ''))
            for o in mine:
                if o['date'] == d:
                    out.append(_instance(r, d, o['time_et'], 'override', o['original_date'],
                                         o['reason']))
        out.sort(key=lambda x: (x['T'], x['event']))
        return out
    except EventCalendarError:
        raise
    except Exception as e:
        raise EventCalendarError('scheduled_instances(%s) failed: %s' % (day, e))


def _instance(rule, d, t, source, anchor, reason):
    return {'event': rule['id'], 'underlyings': list(rule['underlyings']),
            'T': et_to_ist(d, t[0], t[1]), 'et_date': d, 'time_et': _fmt_hhmm(t),
            'source': source, 'anchor': anchor, 'reason': reason}


def expected_for_anchor(rule, anchor, overrides):
    """What the YAML says happens to the standard release `anchor`: (date, (h, m), source)."""
    for o in overrides:
        if o['event'] == rule['id'] and o['original_date'] == anchor:
            return o['date'], o['time_et'], 'override'
    return anchor, rule['time_et'], 'rule'


def anchors_in_range(rule, start, end, overrides, rows=None):
    """Standard release dates whose release (per rule, YAML or page) touches [start, end].

    Includes: rule weekdays in range (even if overridden away -- the original day must be
    checked too), overrides moving INTO the range, and page rows moving into the range (so a
    shift the YAML does not know about is still checked)."""
    start, end = _as_date(start), _as_date(end)
    out = set()
    d = start
    while d <= end:
        if d.weekday() == rule['weekday']:
            out.add(d)
        d += datetime.timedelta(days=1)
    for o in overrides:
        if o['event'] == rule['id'] and start <= o['date'] <= end:
            out.add(o['original_date'])
    for row in rows or []:
        if start <= row['alt_date'] <= end:
            out.add(row['anchor'])
    return sorted(out)


def exception_id(event, anchor):
    return '%s@%s' % (event, _as_date(anchor).isoformat())


# ---------------------------------------------------------------------------
# EIA page parsing (pure) + fetch
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r'([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})')
_TIME_RE = re.compile(r'(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\b\.?', re.I)


def _parse_page_date(text):
    m = _DATE_RE.search(text or '')
    if not m:
        raise ParseError('no date in %r' % (text,))
    mon = _MONTHS.get(m.group(1).lower()[:3])
    if mon is None:
        raise ParseError('bad month in %r' % (text,))
    try:
        return datetime.date(int(m.group(3)), mon, int(m.group(2)))
    except ValueError as e:
        raise ParseError('bad date %r: %s' % (text, e))


def _parse_page_time(text):
    t = (text or '').strip().lower()
    if t.startswith('noon'):
        return (12, 0)
    m = _TIME_RE.search(t)
    if not m:
        raise ParseError('no time in %r' % (text,))
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if not (1 <= h <= 12) or mi > 59:
        raise ParseError('bad time %r' % (text,))
    if ap == 'p' and h != 12:
        h += 12
    if ap == 'a' and h == 12:
        h = 0
    return (h, mi)


def _assign_anchors(rows, weekday):
    """Map each exception row to the standard release date it replaces.

    WPSR rows carry 'Data for the week ending' (Fri): anchor = next rule weekday after it.
    NGS rows carry only the alternate date: assign greedily by nearest standard date, closest
    pairs first. Checked against every row on the page at seeding (incl. Dec 2025, where Mon
    12-29 replaces Thu 12-25 because Wed 12-31 is closer to Thu 01-01).
    """
    claimed = {}
    for r in rows:
        if r.get('week_ending') is not None:
            we = r['week_ending']
            a = we + datetime.timedelta(days=((weekday - we.weekday()) % 7) or 7)
            if a in claimed:
                raise ParseError('two rows map to standard date %s' % a)
            r['anchor'] = a
            claimed[a] = r
    pairs = []
    for i, r in enumerate(rows):
        if r.get('anchor') is not None:
            continue
        base = r['alt_date'] - datetime.timedelta(days=(r['alt_date'].weekday() - weekday) % 7)
        for cand in (base, base + datetime.timedelta(days=7)):
            pairs.append((abs((r['alt_date'] - cand).days), i, cand))
    pairs.sort()
    for dist, i, cand in pairs:
        r = rows[i]
        if r.get('anchor') is not None or cand in claimed:
            continue
        r['anchor'] = cand
        claimed[cand] = r
    for r in rows:
        if r.get('anchor') is None:
            raise ParseError('could not map row %r to a standard date' % r['raw'])


def parse_schedule_html(html, rule):
    """Parse an EIA schedule page into exception rows for `rule`.

    Returns [{alt_date, time_et (h, m), week_ending|None, anchor, raw}]; [] is a VALID result
    (page parsed, no exceptions listed). Raises ParseError if the page marker, the table header
    or any row cannot be read -- a half-understood page must not verify anything.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError as e:  # pragma: no cover - bs4 is installed on both hosts
        raise ParseError('bs4 unavailable: %s' % e)
    if not html or not isinstance(html, str):
        raise ParseError('empty page')
    soup = BeautifulSoup(html, 'html.parser')
    marker = rule.get('page_marker') or ''
    if marker and marker.lower() not in soup.get_text(' ').lower():
        raise ParseError('page marker %r not found (wrong page or structure changed)' % marker)
    for table in soup.find_all('table'):
        trs = table.find_all('tr')
        hdr_idx, cols = None, None
        for k, tr in enumerate(trs):
            cells = [c.get_text(' ', strip=True).lower() for c in tr.find_all(['th', 'td'])]
            alt = [j for j, c in enumerate(cells) if 'alternate release date' in c]
            tim = [j for j, c in enumerate(cells) if 'release time' in c]
            if alt and tim:
                hdr_idx = k
                cols = {'alt': alt[0], 'time': tim[0],
                        'day': next((j for j, c in enumerate(cells) if 'release day' in c), None),
                        'week': next((j for j, c in enumerate(cells) if 'week ending' in c), None)}
                break
        if hdr_idx is None:
            continue
        rows = []
        for tr in trs[hdr_idx + 1:]:
            cells = [c.get_text(' ', strip=True) for c in tr.find_all(['th', 'td'])]
            if not any(cells):
                continue
            need = max(v for v in cols.values() if v is not None)
            if len(cells) <= need:
                raise ParseError('short row %r' % cells)
            raw = ' | '.join(cells)
            alt_date = _parse_page_date(cells[cols['alt']])
            row = {'alt_date': alt_date, 'time_et': _parse_page_time(cells[cols['time']]),
                   'week_ending': (_parse_page_date(cells[cols['week']])
                                   if cols['week'] is not None else None),
                   'anchor': None, 'raw': raw}
            if cols['day'] is not None:
                # Cross-check the printed weekday: a mismatch means a typo or a layout change,
                # and we cannot tell which column is right.
                day_txt = cells[cols['day']].strip().lower()[:3]
                if _WEEKDAYS.get(day_txt) != alt_date.weekday():
                    raise ParseError('weekday %r does not match %s in row %r'
                                     % (cells[cols['day']], alt_date, raw))
            rows.append(row)
        _assign_anchors(rows, rule['weekday'])
        return rows
    raise ParseError('schedule table header not found (structure changed)')


def scraped_for_anchor(rule, anchor, rows):
    """What the page says about `anchor`: (date, (h, m), raw). No row = standard schedule."""
    for r in rows:
        if r['anchor'] == anchor:
            return r['alt_date'], r['time_et'], r['raw']
    return anchor, rule['time_et'], 'no exception row for %s (standard schedule)' % anchor


def fetch_page(url, timeout_s=20.0, user_agent='ETF_PI-event_calendar/1.0', retry_wait_s=5.0,
               _sleep=time.sleep):
    """GET `url` with a timeout, explicit User-Agent and ONE retry. Raises FetchError."""
    import requests
    last = None
    for attempt in (1, 2):
        try:
            r = requests.get(url, timeout=timeout_s, headers={'User-Agent': user_agent})
            r.raise_for_status()
            if not r.text:
                raise FetchError('empty body')
            return r.text
        except Exception as e:
            last = e
            log.warning('fetch %s attempt %d failed: %s', url, attempt, e)
            if attempt == 1:
                _sleep(retry_wait_s)
    raise FetchError('%s: %s' % (url, last))


# ---------------------------------------------------------------------------
# Exception store (event_exceptions.json)
# ---------------------------------------------------------------------------

def _empty_store():
    return {'version': 1, 'exceptions': {}, 'verifications': {}}


def load_exceptions(base_dir=None):
    """Missing file = no exceptions (first run). Corrupt file raises EventCalendarError: we can
    no longer see which windows are widened, so the caller must fail closed."""
    path = os.path.join(base_dir or MODULE_DIR, EXCEPTIONS_FILE)
    if not os.path.exists(path):
        return _empty_store()
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        raise EventCalendarError('unreadable %s: %s' % (path, e))
    if (not isinstance(data, dict) or not isinstance(data.get('exceptions'), dict)
            or not isinstance(data.get('verifications'), dict)):
        raise EventCalendarError('%s: unexpected structure' % path)
    return data


def atomic_write_json(path, data):
    """tmp + fsync + os.replace: readers see the old or the new file, never a torn one."""
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(data, f, indent=2, sort_keys=True, default=_json_default)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save_exceptions(store, base_dir=None):
    atomic_write_json(os.path.join(base_dir or MODULE_DIR, EXCEPTIONS_FILE), store)


def _json_default(o):
    if isinstance(o, (datetime.datetime, datetime.date)):
        return o.isoformat()
    raise TypeError('not JSON serialisable: %r' % (o,))


def _history(rec, now, text):
    rec.setdefault('history', []).append({'at': now.isoformat(), 'note': text})


def _new_record(rule, anchor, expected, now, status, reason):
    return {'id': exception_id(rule['id'], anchor), 'event': rule['id'],
            'date': anchor.isoformat(),
            'yaml_date': expected[0].isoformat(), 'yaml_time_et': _fmt_hhmm(expected[1]),
            'scraped_date': None, 'scraped_time_et': None, 'raw_scraped': None,
            'status': status, 'detected_at': now.isoformat(), 'resolved_at': None,
            'reason': reason, 'history': []}


def record_verification(store, rule, anchor, expected, scraped, now):
    """Apply one successful page check of `anchor` to the store (pure dict update).

    expected = (date, (h, m)) from the YAML; scraped = (date, (h, m), raw) from the page.
    Returns a list of human-readable transitions (for log + Telegram).
    """
    eid = exception_id(rule['id'], anchor)
    day = now.date().isoformat()
    agree = (expected[0], tuple(expected[1])) == (scraped[0], tuple(scraped[1]))
    store['verifications'][eid] = {'last_parsed_day': day, 'last_parsed_at': now.isoformat(),
                                   'result': 'AGREE' if agree else 'MISMATCH',
                                   'raw_scraped': scraped[2]}
    rec = store['exceptions'].get(eid)
    msgs = []
    if agree:
        if rec and rec['status'] in _ACTIVE:
            prev = rec['status']
            rec['status'] = STATUS_RESOLVED
            rec['resolved_at'] = now.isoformat()
            rec['reason'] = 'later check agrees with YAML (%s %s ET)' % (
                expected[0], _fmt_hhmm(expected[1]))
            _history(rec, now, '%s -> RESOLVED: %s' % (prev, scraped[2]))
            msgs.append('RESOLVED %s (was %s)' % (eid, prev))
        return msgs
    s_date, s_time = scraped[0].isoformat(), _fmt_hhmm(scraped[1])
    if rec is None or rec['status'] == STATUS_RESOLVED:
        old_hist = rec.get('history', []) if rec else []
        why = ('YAML %s %s ET vs EIA %s %s ET'
               % (expected[0], _fmt_hhmm(expected[1]), s_date, s_time))
        rec = _new_record(rule, anchor, expected, now, STATUS_OPEN, why)
        rec['history'] = old_hist
        store['exceptions'][eid] = rec
        msgs.append('OPEN %s: %s' % (eid, rec['reason']))
    else:
        changed = (rec.get('scraped_date'), rec.get('scraped_time_et')) != (s_date, s_time)
        if rec['status'] == STATUS_UNVERIFIED:
            rec['status'] = STATUS_OPEN
            msgs.append('OPEN %s (was UNVERIFIED): YAML %s %s vs EIA %s %s'
                        % (eid, expected[0], _fmt_hhmm(expected[1]), s_date, s_time))
        elif changed:
            msgs.append('%s %s: EIA now says %s %s ET' % (rec['status'], eid, s_date, s_time))
        rec['yaml_date'] = expected[0].isoformat()
        rec['yaml_time_et'] = _fmt_hhmm(expected[1])
        rec['reason'] = 'YAML %s %s ET vs EIA %s %s ET' % (
            expected[0], _fmt_hhmm(expected[1]), s_date, s_time)
    rec['scraped_date'], rec['scraped_time_et'], rec['raw_scraped'] = s_date, s_time, scraped[2]
    _history(rec, now, 'mismatch: %s' % scraped[2])
    return msgs


def record_failure(store, rule, anchor, expected, error, now):
    """Event-day fetch/parse failure for `anchor` -> UNVERIFIED (fail-closed window).

    An OPEN record becomes UNVERIFIED but keeps its scraped candidate, so the union window and
    the fail-closed window both apply. An ACKED record stays ACKED (the user chose the YAML).
    """
    eid = exception_id(rule['id'], anchor)
    rec = store['exceptions'].get(eid)
    msgs = []
    if rec is None or rec['status'] == STATUS_RESOLVED:
        old_hist = rec.get('history', []) if rec else []
        rec = _new_record(rule, anchor, expected, now, STATUS_UNVERIFIED,
                          'event-day verification failed: %s' % error)
        rec['history'] = old_hist
        store['exceptions'][eid] = rec
        msgs.append('UNVERIFIED %s: %s' % (eid, error))
    elif rec['status'] == STATUS_OPEN:
        rec['status'] = STATUS_UNVERIFIED
        rec['reason'] = 'verification failed (%s); last known: %s' % (error, rec['reason'])
        msgs.append('UNVERIFIED %s (was OPEN): %s' % (eid, error))
    _history(rec, now, 'verification failed: %s' % error)
    return msgs


def _ack_check(rec, acks, rules, overrides, day):
    """Return (ack, None) if a valid ack covers `rec` on `day`, else (None, why)."""
    if rec is None or rec.get('status') not in _ACTIVE:
        return None, 'no active exception'
    rule = next((r for r in rules if r['id'] == rec['event']), None)
    if rule is None:
        return None, 'event %s no longer in events.yaml' % rec['event']
    cands = [ack for ack in acks if ack['id'] == rec['id']]
    if not cands:
        return None, 'no ack'
    try:
        anchor = _as_date(rec['date'])
    except ValueError:
        return None, 'record has a bad date'
    for ack in cands:
        if ack['event'] != rec['event'] or ack['date'] != anchor \
                or ack['yaml_time_et'] != rec['yaml_time_et']:
            continue
        # The YAML must still say what the user acked; an edited YAML needs a fresh check.
        exp = expected_for_anchor(rule, anchor, overrides)
        if exp[0].isoformat() != rec['yaml_date'] or _fmt_hhmm(exp[1]) != rec['yaml_time_et']:
            return None, 'YAML changed since the exception was recorded'
        last = max(d for d in (rec.get('yaml_date'), rec.get('scraped_date'), rec['date']) if d)
        if _as_date(day) > _as_date(last):
            return None, 'ack expired (event date %s passed)' % last
        return ack, None
    return None, 'ack fields do not match exception (event/date/yaml_time_et)'


def apply_acks(store, acks, rules, overrides, day, now):
    """Persist ACKED / un-ACKED status. Returns (transitions, warnings).

    Stale or mistyped acks never shrink a window -- they are reported and ignored (M15)."""
    msgs, warnings = [], []
    for ack in acks:
        rec = store['exceptions'].get(ack['id'])
        _, why = _ack_check(rec, [ack], rules, overrides, day)
        if why:
            warnings.append('ack %s ignored: %s' % (ack['id'], why))
    for eid, rec in store['exceptions'].items():
        if rec.get('status') not in _ACTIVE:
            continue
        ack, _ = _ack_check(rec, acks, rules, overrides, day)
        if ack and rec['status'] != STATUS_ACKED:
            rec['status_before_ack'] = rec['status']
            rec['status'] = STATUS_ACKED
            rec['acked_at'] = now.isoformat()
            _history(rec, now, 'ACKED (trust YAML %s %s ET)'
                     % (rec['yaml_date'], rec['yaml_time_et']))
            msgs.append('ACKED %s' % eid)
        elif not ack and rec['status'] == STATUS_ACKED:
            last = max(d for d in (rec.get('yaml_date'), rec.get('scraped_date'), rec['date'])
                       if d)
            if _as_date(day) <= _as_date(last):
                rec['status'] = rec.get('status_before_ack') or STATUS_OPEN
                _history(rec, now, 'ack withdrawn/invalid -> %s' % rec['status'])
                msgs.append('%s %s (ack no longer valid)' % (rec['status'], eid))
    return msgs, warnings


def _effective_status(rec, cfg, day):
    """Status the GATE uses: a currently-valid ack wins; a stored ACKED whose ack is gone
    falls back to the pre-ack status (never trust a stale ACKED)."""
    ack, _ = _ack_check(rec, cfg['acks'], cfg['rules'], cfg['overrides'], day)
    if ack:
        return STATUS_ACKED
    if rec.get('status') == STATUS_ACKED:
        return rec.get('status_before_ack') or STATUS_OPEN
    return rec.get('status')


# ---------------------------------------------------------------------------
# Windows and gate predicates
# ---------------------------------------------------------------------------

def _event_window(event, T, et_date, time_et, source, reason, ids):
    return {'kind': 'event', 'event': event, 'T': T, 'et_date': _as_date(et_date).isoformat(),
            'time_et': time_et, 'source': source, 'post_min': POST_EVENT_MIN,
            'flatten_lead_min': FLATTEN_LEAD_MIN, 'reason': reason,
            'exception_ids': list(ids)}


def _fail_closed_window(day, reason, ids, event=None):
    _, end = _ist_day_bounds(day)
    return {'kind': 'fail_closed', 'event': event, 'T': None,
            'block_from': _ist_at(day, FAIL_CLOSED_BLOCK_FROM),
            'flatten_from': _ist_at(day, FAIL_CLOSED_FLATTEN_FROM),
            'until': end, 'reason': reason, 'exception_ids': list(ids)}


def fail_closed_windows(underlying, day):
    """Windows option_buy uses when windows_for() raises (calendar unusable).

    Event underlyings on Wed/Thu/Fri -> one fail_closed window (block 19:00 IST -> end of day,
    flatten from 18:55); anything else -> []. Never raises: on bad input it returns the
    fail-closed window for event underlyings (wider is the safe direction).
    """
    if underlying not in FAIL_CLOSED_UNDERLYINGS:
        return []
    try:
        d = _as_date(day)
    except Exception:
        d = _now_ist().date()
        return [_fail_closed_window(d, 'FAIL_CLOSED bad day %r' % (day,), [])]
    if d.weekday() not in FAIL_CLOSED_WEEKDAYS:
        return []
    return [_fail_closed_window(d, 'FAIL_CLOSED calendar unavailable', [])]


def _overlaps_day(T, day):
    start, end = _ist_day_bounds(day)
    return (T - datetime.timedelta(minutes=MAX_PRE_EVENT_MIN) < end
            and T + datetime.timedelta(minutes=POST_EVENT_MIN) > start)


def _unverified_days(rec):
    """Wed/Thu/Fri of the anchor's Mon-Sun week, plus the YAML release date itself."""
    anchor = _as_date(rec['date'])
    monday = anchor - datetime.timedelta(days=anchor.weekday())
    days = {monday + datetime.timedelta(days=w) for w in FAIL_CLOSED_WEEKDAYS}
    if rec.get('yaml_date'):
        days.add(_as_date(rec['yaml_date']))
    return days


def compute_windows(underlying, day, cfg, store):
    """Pure core of windows_for (config/store already loaded). Raises on malformed input."""
    d = _as_date(day)
    rules = [r for r in cfg['rules'] if underlying in r['underlyings']]
    if not rules:
        return []
    rule_ids = {r['id'] for r in rules}
    windows = []
    for dd in (d - datetime.timedelta(days=1), d, d + datetime.timedelta(days=1)):
        for inst in scheduled_instances(dd, rules, cfg['overrides']):
            if _overlaps_day(inst['T'], d):
                why = '%s %s %s ET (%s%s)' % (inst['event'], inst['et_date'], inst['time_et'],
                                              inst['source'],
                                              ': ' + inst['reason'] if inst['reason'] else '')
                windows.append(_event_window(inst['event'], inst['T'], inst['et_date'],
                                             inst['time_et'], inst['source'], why, []))
    active_ids = set()
    for eid in sorted(store['exceptions']):
        rec = store['exceptions'][eid]
        if rec.get('event') not in rule_ids:
            continue
        st = _effective_status(rec, cfg, d)
        if st in _ACTIVE:
            active_ids.add(eid)
        if st not in (STATUS_OPEN, STATUS_UNVERIFIED):
            continue
        cands = [(rec['yaml_date'], rec['yaml_time_et'], 'yaml')]
        if rec.get('scraped_date') and rec.get('scraped_time_et'):
            cands.append((rec['scraped_date'], rec['scraped_time_et'], 'eia'))
        for c_date, c_time, label in cands:
            hm = _parse_hhmm(c_time)
            T = et_to_ist(c_date, hm[0], hm[1])
            if not _overlaps_day(T, d):
                continue
            hit = next((w for w in windows if w['kind'] == 'event'
                        and w['event'] == rec['event'] and w['T'] == T), None)
            if hit:
                hit['exception_ids'].append(eid)
            else:
                windows.append(_event_window(
                    rec['event'], T, c_date, c_time, 'exception',
                    '%s %s candidate %s %s ET (%s)' % (st, eid, c_date, c_time, label), [eid]))
        if st == STATUS_UNVERIFIED and d in _unverified_days(rec):
            windows.append(_fail_closed_window(d, 'UNVERIFIED %s: %s' % (eid, rec.get('reason')),
                                               [eid], rec['event']))
    # Event day whose verification never ran today (cron/Pi down): same as a failed check.
    for r in rules:
        for anchor in anchors_in_range(r, d, d, cfg['overrides']):
            eid = exception_id(r['id'], anchor)
            v = store['verifications'].get(eid) or {}
            if v.get('last_parsed_day') == d.isoformat() or eid in active_ids:
                continue
            windows.append(_fail_closed_window(
                d, 'NOT_VERIFIED %s: no successful EIA check on %s' % (eid, d), [], r['id']))
    windows.sort(key=lambda w: (w['T'] or w['block_from'], w['kind']))
    return windows


def windows_for(underlying, day, base_dir=None):
    """All gate windows for `underlying` on IST date `day`.

    Combines scheduled instances (rules + overrides), the union candidates of OPEN/UNVERIFIED
    exceptions and fail_closed windows (UNVERIFIED, or event day not verified today).
    Raises EventCalendarError if config/state is unreadable or invalid -- the caller must then
    use fail_closed_windows(). Never returns a partial list.
    """
    base_dir = base_dir or MODULE_DIR
    try:
        cfg = load_config(base_dir)
        store = load_exceptions(base_dir)
        return compute_windows(underlying, day, cfg, store)
    except EventCalendarError:
        raise
    except Exception as e:
        raise EventCalendarError('windows_for(%s, %s) failed: %s' % (underlying, day, e))


def _w_dt(v):
    """Windows may come straight from compute_windows (datetimes) or from the JSON file."""
    if isinstance(v, str):
        return _as_ist(datetime.datetime.fromisoformat(v))
    return _as_ist(v)


def _valid_tsl(time_sl_min):
    try:
        v = float(time_sl_min)
        if v >= 0 and v == v:      # rejects NaN
            return v
    except (TypeError, ValueError):
        pass
    log.warning('invalid time_sl_min %r -> using %d', time_sl_min, FALLBACK_TIME_SL_MIN)
    return float(FALLBACK_TIME_SL_MIN)


def entry_blocked(windows, now, time_sl_min):
    """(blocked, reason, exception_ids) for a NEW entry at `now`.

    event window:       T - time_sl_min <= now < T + 30   (start inclusive, end exclusive)
    fail_closed window: 19:00 IST <= now < end of day
    `now` naive = IST. Never raises: any internal error (incl. windows=None or an unknown
    window kind) returns blocked=True with an EVENT_GATE_ERROR reason.
    """
    try:
        if windows is None:
            raise ValueError('windows is None')
        n = _as_ist(now)
        tsl = _valid_tsl(time_sl_min)
        reasons, ids = [], []
        for w in windows:
            kind = w.get('kind')
            if kind == 'event':
                T = _w_dt(w['T'])
                start = T - datetime.timedelta(minutes=tsl)
                end = T + datetime.timedelta(minutes=w.get('post_min', POST_EVENT_MIN))
                label = 'EVENT_BLOCK %s T=%s IST' % (w.get('event'), T.strftime('%Y-%m-%d %H:%M'))
            elif kind == 'fail_closed':
                start, end = _w_dt(w['block_from']), _w_dt(w['until'])
                label = 'EVENT_FAIL_CLOSED %s' % w.get('reason')
            else:
                raise ValueError('unknown window kind %r' % (kind,))
            if start <= n < end:
                reasons.append('%s (%s-%s)' % (label, start.strftime('%H:%M'),
                                               end.strftime('%H:%M')))
                ids.extend(w.get('exception_ids') or [])
        if reasons:
            return True, '; '.join(reasons), sorted(set(ids))
        return False, '', []
    except Exception as e:
        log.error('entry_blocked failed -> blocking: %s', e)
        return True, 'EVENT_GATE_ERROR %s' % e, []


def must_flatten(windows, now):
    """(flatten, reason, exception_ids): a STATE, true for every minute in the span.

    event window:       T - 5 <= now < T + 30
    fail_closed window: 18:55 IST <= now < end of day
    Never raises: an internal error returns flatten=True (EVENT_GATE_ERROR) -- holding through
    an unknown window is the outcome this module exists to prevent.
    """
    try:
        if windows is None:
            raise ValueError('windows is None')
        n = _as_ist(now)
        reasons, ids = [], []
        for w in windows:
            kind = w.get('kind')
            if kind == 'event':
                T = _w_dt(w['T'])
                start = T - datetime.timedelta(minutes=w.get('flatten_lead_min', FLATTEN_LEAD_MIN))
                end = T + datetime.timedelta(minutes=w.get('post_min', POST_EVENT_MIN))
                label = 'EVENT_FLAT %s T=%s IST' % (w.get('event'), T.strftime('%Y-%m-%d %H:%M'))
            elif kind == 'fail_closed':
                start, end = _w_dt(w['flatten_from']), _w_dt(w['until'])
                label = 'EVENT_FLAT fail-closed %s' % w.get('reason')
            else:
                raise ValueError('unknown window kind %r' % (kind,))
            if start <= n < end:
                reasons.append('%s (%s-%s)' % (label, start.strftime('%H:%M'),
                                               end.strftime('%H:%M')))
                ids.extend(w.get('exception_ids') or [])
        if reasons:
            return True, '; '.join(reasons), sorted(set(ids))
        return False, '', []
    except Exception as e:
        log.error('must_flatten failed -> flattening: %s', e)
        return True, 'EVENT_GATE_ERROR %s' % e, []


# ---------------------------------------------------------------------------
# Verification run (I/O at the edges, injectable)
# ---------------------------------------------------------------------------

def _default_send(msg):
    import common  # lazy: common.py holds secrets and is absent in some test environments
    common.sendMsgTelegram(msg, 'I')


def _telegram(send, msg):
    """Telegram must never break the calendar run; failures are logged only."""
    try:
        (send or _default_send)(msg)
    except Exception as e:
        log.error('Telegram send failed: %s', e)


def _fetch_rows(rule, cfg, fetch, cache):
    """Fetch + parse one rule's page once per run. Returns (rows, None) or (None, error_str)."""
    url = rule['source']
    if url not in cache:
        try:
            html = (fetch or (lambda u: fetch_page(u, cfg['fetch']['timeout_s'],
                                                   cfg['fetch']['user_agent'],
                                                   cfg['fetch']['retry_wait_s'])))(url)
            cache[url] = ('ok', html)
        except Exception as e:
            cache[url] = ('err', 'fetch failed: %s' % e)
    status, payload = cache[url]
    if status == 'err':
        return None, payload
    try:
        return parse_schedule_html(payload, rule), None
    except ParseError as e:
        return None, 'parse failed: %s' % e
    except Exception as e:
        return None, 'parse crashed: %s' % e


def verify_range(cfg, store, start, end, now, fetch=None, create_unverified=True):
    """Verify every anchor of every rule touching [start, end]. Mutates `store`.

    create_unverified=False is the Sunday preview: a failure is reported but creates no
    exception (the event-day check follows). Returns (transitions, failures, checked).
    """
    msgs, failures, checked, cache = [], [], [], {}
    for rule in cfg['rules']:
        rows, err = _fetch_rows(rule, cfg, fetch, cache)
        anchors = anchors_in_range(rule, start, end, cfg['overrides'], rows)
        for anchor in anchors:
            exp = expected_for_anchor(rule, anchor, cfg['overrides'])
            if err:
                eid = exception_id(rule['id'], anchor)
                failures.append('%s: %s' % (eid, err))
                v = store['verifications'].get(eid) or {}
                if (create_unverified and v.get('last_parsed_day') == now.date().isoformat()
                        and v.get('result') == 'AGREE'):
                    # Review-4 R4-2: a transient fetch failure at the 17:00 recheck must not undo
                    # a successful same-day verification (it would fail-close this evening AND
                    # the rest of the week). The morning AGREE stands; the failure is reported.
                    msgs.append('RECHECK FAILED %s (%s) - this morning\'s AGREE stands' % (eid, err))
                    continue
                if create_unverified:
                    msgs += record_failure(store, rule, anchor, exp, err, now)
                continue
            scr = scraped_for_anchor(rule, anchor, rows)
            msgs += record_verification(store, rule, anchor, exp, scr, now)
            checked.append('%s: YAML %s %s / EIA %s %s %s' % (
                exception_id(rule['id'], anchor), exp[0], _fmt_hhmm(exp[1]), scr[0],
                _fmt_hhmm(scr[1]),
                'OK' if (exp[0], exp[1]) == (scr[0], tuple(scr[1])) else 'MISMATCH'))
    for m in msgs:
        log.warning('exception: %s', m)
    for f in failures:
        log.error('verify failure: %s', f)
    return msgs, failures, checked


def _is_event_day(cfg, day):
    return any(anchors_in_range(r, day, day, cfg['overrides']) for r in cfg['rules'])


def _active_lines(store, cfg, day):
    out = []
    for eid in sorted(store['exceptions']):
        rec = store['exceptions'][eid]
        st = _effective_status(rec, cfg, day)
        if st not in _ACTIVE:
            continue
        line = '%s %s: %s' % (st, eid, rec.get('reason'))
        if st in (STATUS_OPEN, STATUS_UNVERIFIED):
            line += ('\n  to trust YAML add to acks: {id: "%s", event: %s, date: %s, '
                     'yaml_time_et: "%s"}' % (eid, rec['event'], rec['date'],
                                              rec['yaml_time_et']))
        out.append(line)
    return out


def _fmt_window(w):
    if w['kind'] == 'event':
        T = w['T']
        return ('%s T %s IST (%s ET, %s) flat %s-%s%s' % (
            w['event'], T.strftime('%H:%M'), w['time_et'], w['source'],
            (T - datetime.timedelta(minutes=w['flatten_lead_min'])).strftime('%H:%M'),
            (T + datetime.timedelta(minutes=w['post_min'])).strftime('%H:%M'),
            ' [%s]' % ','.join(w['exception_ids']) if w['exception_ids'] else ''))
    return 'FAIL-CLOSED %s-EOD: %s' % (w['block_from'].strftime('%H:%M'), w['reason'])


def _serialise_windows(windows):
    return json.loads(json.dumps(windows, default=_json_default))


def _write_fail_closed_file(base_dir, day, now, error):
    data = {'date': day.isoformat(), 'built_at': now.isoformat(), 'fail_closed': True,
            'error': str(error),
            'windows': {u: _serialise_windows(fail_closed_windows(u, day))
                        for u in FAIL_CLOSED_UNDERLYINGS}}
    try:
        atomic_write_json(os.path.join(base_dir, WINDOWS_FILE_FMT.format(day.isoformat())), data)
    except Exception as e:
        log.error('could not write fail-closed windows file: %s', e)


def cmd_build(day, base_dir, now, send=None, fetch=None):
    """Morning build: verify if event day, apply acks, write event_windows_<day>.json, Telegram.
    Returns process exit code (0 ok, 1 fail-closed)."""
    try:
        cfg = load_config(base_dir)
        store = load_exceptions(base_dir)
    except EventCalendarError as e:
        log.error('build %s: calendar unusable -> fail-closed: %s', day, e)
        _write_fail_closed_file(base_dir, day, now, e)
        _telegram(send, '[EVENTS] %s ERROR calendar unusable -> FAIL-CLOSED 19:00-EOD on '
                        'Wed/Thu/Fri for %s\n%s' % (day, ', '.join(FAIL_CLOSED_UNDERLYINGS), e))
        return 1
    msgs, failures, checked = [], [], []
    if _is_event_day(cfg, day):
        msgs, failures, checked = verify_range(cfg, store, day, day, now, fetch, True)
    else:
        log.info('build %s: not an event day, no verification', day)
    a_msgs, warnings = apply_acks(store, cfg['acks'], cfg['rules'], cfg['overrides'], day, now)
    msgs += a_msgs
    warnings = cfg['warnings'] + warnings
    for w in warnings:
        log.warning(w)
    try:
        save_exceptions(store, base_dir)
        und = sorted({u for r in cfg['rules'] for u in r['underlyings']})
        wins = {u: compute_windows(u, day, cfg, store) for u in und}
        atomic_write_json(os.path.join(base_dir, WINDOWS_FILE_FMT.format(day.isoformat())),
                          {'date': day.isoformat(), 'built_at': now.isoformat(),
                           'fail_closed': False, 'windows': _serialise_windows(wins),
                           'transitions': msgs, 'failures': failures, 'warnings': warnings})
    except Exception as e:
        log.error('build %s: write failed -> fail-closed: %s', day, e)
        _write_fail_closed_file(base_dir, day, now, e)
        _telegram(send, '[EVENTS] %s ERROR build failed -> FAIL-CLOSED: %s' % (day, e))
        return 1
    for u, ws in wins.items():
        for w in ws:
            log.info('window %s %s: %s', day, u, _fmt_window(w))
    active = _active_lines(store, cfg, day)
    if any(wins.values()) or active or msgs or failures or warnings:
        lines = ['[EVENTS] %s %s' % (day, day.strftime('%a'))]
        for u, ws in wins.items():
            for w in ws:
                lines.append('%s: %s' % (u, _fmt_window(w)))
        lines += ['verify ' + c for c in checked]
        lines += ['FAILED ' + f for f in failures]
        lines += ['change ' + m for m in msgs]
        lines += ['exception ' + a for a in active]
        lines += ['warning ' + w for w in warnings]
        _telegram(send, '\n'.join(lines))
    return 0


def cmd_verify(day, base_dir, now, week=False, send=None, fetch=None):
    """17:00 event-day re-check, or (--week) Sunday preview of day+1 .. day+7."""
    try:
        cfg = load_config(base_dir)
        store = load_exceptions(base_dir)
    except EventCalendarError as e:
        log.error('verify %s: calendar unusable: %s', day, e)
        _telegram(send, '[EVENTS] %s ERROR verify: calendar unusable: %s' % (day, e))
        return 1
    if week:
        start, end = day + datetime.timedelta(days=1), day + datetime.timedelta(days=7)
        msgs, failures, checked = verify_range(cfg, store, start, end, now, fetch, False)
    elif _is_event_day(cfg, day):
        start = end = day
        msgs, failures, checked = verify_range(cfg, store, day, day, now, fetch, True)
    else:
        log.info('verify %s: not an event day, nothing to check', day)
        return 0
    a_msgs, warnings = apply_acks(store, cfg['acks'], cfg['rules'], cfg['overrides'], day, now)
    msgs += a_msgs
    try:
        save_exceptions(store, base_dir)
    except Exception as e:
        log.error('verify %s: could not save exceptions: %s', day, e)
        _telegram(send, '[EVENTS] %s ERROR verify could not save exceptions: %s' % (day, e))
        return 1
    lines = ['[EVENTS] verify %s%s' % (day, ' week %s..%s' % (start, end) if week else '')]
    if week:
        d = start
        while d <= end:
            for inst in scheduled_instances(d, cfg['rules'], cfg['overrides']):
                lines.append('%s %s %s ET = %s IST %s' % (
                    inst['event'], d.strftime('%a %m-%d'), inst['time_et'],
                    inst['T'].strftime('%a %H:%M'), '/'.join(inst['underlyings'])))
            d += datetime.timedelta(days=1)
    lines += ['verify ' + c for c in checked]
    lines += [('FAILED (preview only, no exception) ' if week else 'FAILED ') + f
              for f in failures]
    lines += ['change ' + m for m in msgs]
    lines += ['exception ' + a for a in _active_lines(store, cfg, day)]
    lines += ['warning ' + w for w in cfg['warnings'] + warnings]
    _telegram(send, '\n'.join(lines))
    return 1 if failures and not week else 0


def setup_cli_logging(base_dir):
    """File logging only for the CLI; importing the module never opens a log file."""
    os.makedirs(os.path.join(base_dir, 'logs'), exist_ok=True)
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter('%(asctime)s | %(levelname)-7s | %(message)s', '%Y-%m-%d %H:%M:%S')
    fh = logging.handlers.TimedRotatingFileHandler(
        os.path.join(base_dir, LOG_FILE), when='midnight', backupCount=LOG_RETENTION_DAYS,
        encoding='utf-8')
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(ch)


def main(argv=None, base_dir=None, now=None, send=None, fetch=None):
    """CLI entry. Tests call this with base_dir/now/send/fetch injected (no file logging)."""
    p = argparse.ArgumentParser(description='EIA event calendar (build / verify)')
    p.add_argument('cmd', choices=['build', 'verify'])
    p.add_argument('--date', help='YYYY-MM-DD (IST date; default today)')
    p.add_argument('--week', action='store_true', help='verify: preview next 7 days')
    p.add_argument('--base-dir', default=None)
    a = p.parse_args(argv)
    base_dir = base_dir or a.base_dir or MODULE_DIR
    now = _as_ist(now) if now is not None else _now_ist()
    try:
        day = _as_date(a.date) if a.date else now.date()
    except ValueError as e:
        p.error('bad --date: %s' % e)
    log.info('event_calendar %s day=%s week=%s base=%s', a.cmd, day, a.week, base_dir)
    if a.cmd == 'build':
        return cmd_build(day, base_dir, now, send, fetch)
    return cmd_verify(day, base_dir, now, a.week, send, fetch)


if __name__ == '__main__':
    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument('--base-dir', default=None)
    _known, _ = _pre.parse_known_args()
    setup_cli_logging(_known.base_dir or MODULE_DIR)
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as _e:
        log.exception('event_calendar crashed: %s', _e)
        _telegram(None, '[EVENTS] ERROR event_calendar crashed: %s' % _e)
        sys.exit(1)
