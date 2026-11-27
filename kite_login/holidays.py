"""kite_login.holidays -- refresh BOTS/data/holiday_calendar.json (NSE closures).

Moved here from SNAIL/src/utils/holiday_scraper.py on 2026-11-27 so SNAIL can be
retired; `Helper/common/nse_holidays.py` (every engine's calendar) reads the
file this writes. The parse is the 2026-11-24 fix, unchanged:

  * Zerodha's page carries several tables with DIFFERENT column orders
    (trading: Date|Day|Holiday|Exchanges; settlement / MCX-only / weekend:
    Day|Date|Holiday|...). The date is whichever cell PARSES as a date, and a
    row counts only if its exchanges cell names NSE. Position-based parsing
    dropped every real 2026 closure and Guru Nanak Jayanti was missed.
  * a year is written only when the page has at least MIN_WEEKDAY_HOLIDAYS
    weekday closures for it (fewer = layout changed, that year is NOT
    overwritten); the refresh fails only if neither this year nor next passes,
    so a page that already rolled over to next year still refreshes.
  * existing years not on the page are kept (manual entries survive).
  * written atomically; a failed fetch leaves yesterday's file in place.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .core import write_atomic

logger = logging.getLogger(__name__)

URL = 'https://zerodha.com/marketintel/holiday-calendar/'
TIMEOUT_S = 10
MIN_PAGE_CHARS = 1000
MIN_WEEKDAY_HOLIDAYS = 8      # a real NSE year has ~14-16 weekday closures
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
_WEEKDAYS = {'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'}
DATE_FORMATS = ('%d %b %Y', '%d %B %Y', '%B %d', '%d %B', '%d-%b-%Y', '%d-%m-%Y', '%Y-%m-%d')


def fetch_page() -> str:
    """The page HTML, or ValueError/requests error (caller reports it)."""
    import requests
    r = requests.get(URL, headers={'User-Agent': UA}, timeout=TIMEOUT_S)
    if r.status_code != 200:
        raise ValueError('holiday page HTTP %d' % r.status_code)
    if len(r.text) < MIN_PAGE_CHARS:
        raise ValueError('holiday page too short (%d chars)' % len(r.text))
    return r.text


def parse_date(text: str, default_year: int) -> Optional[date]:
    for fmt in DATE_FORMATS:
        try:
            d = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        return d.replace(year=default_year) if d.year == 1900 else d
    return None


def parse(html: str, current_year: int) -> List[Dict[str, str]]:
    """NSE trading closures on the page: [{'date','name'}], sorted, one per date."""
    from bs4 import BeautifulSoup
    out: Dict[str, Dict[str, str]] = {}
    skipped = 0
    for table in BeautifulSoup(html, 'html.parser').find_all('table'):
        for row in table.find_all('tr'):
            cells = [c.get_text(' ', strip=True) for c in row.find_all(['td', 'th'])]
            if len(cells) < 3:
                continue
            idx, d = None, None
            for i, text in enumerate(cells):
                d = parse_date(re.sub(r'\s*long weekend\s*$', '', text, flags=re.I),
                               current_year)
                if d:
                    idx = i
                    break
            if d is None:
                continue                              # header / unrelated row
            exchanges = cells[-1].lower().split() if len(cells) >= 4 else []
            if 'nse' not in exchanges:
                skipped += 1                          # settlement / MCX only: NSE trades
                continue
            name = next((c for j, c in enumerate(cells[:-1])
                         if j != idx and c.lower() not in _WEEKDAYS), None)
            if not name:
                logger.warning('holiday row without a name: %s', cells)
                continue
            out.setdefault(d.isoformat(), {'date': d.isoformat(), 'name': name})
    logger.info('holidays: parsed %d NSE closures (skipped %d settlement/MCX rows)',
                len(out), skipped)
    return [out[k] for k in sorted(out)]


def refresh(path: Path, force: bool = False, today: Optional[date] = None,
            fetch: Callable[[], str] = fetch_page) -> Tuple[bool, str]:
    """Refresh once a day (force = always). (ok, message). Never raises."""
    today = today or date.today()
    existing: Dict[str, Any] = {}
    if path.exists():
        try:
            with open(str(path), encoding='utf-8') as f:
                existing = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning('holidays: existing %s unreadable (%s) -- rebuilding', path.name, e)
            existing = {}
    if not force and existing.get('_last_updated') == today.isoformat():
        return True, 'already refreshed today'
    try:
        holidays = parse(fetch(), today.year)
    except Exception as e:
        kept = ' (yesterday\'s file kept)' if path.exists() else ''
        return False, 'fetch/parse failed: %s: %s%s' % (type(e).__name__, str(e)[:120], kept)
    by_year: Dict[str, List[Dict[str, str]]] = {}
    for h in holidays:
        by_year.setdefault(h['date'][:4], []).append(h)

    def weekdays(y: str) -> int:
        return sum(date.fromisoformat(h['date']).weekday() < 5 for h in by_year.get(y, []))
    # A year is written only if the page carries a plausible FULL year of it: a thin
    # parse means the layout changed, and must not overwrite a good year. This year
    # OR next year passing is enough, so a page that has already rolled over to next
    # year (December) still refreshes instead of failing every morning.
    good = {y: v for y, v in by_year.items() if weekdays(y) >= MIN_WEEKDAY_HOLIDAYS}
    this_y, next_y = str(today.year), str(today.year + 1)
    if this_y not in good and next_y not in good:
        return False, ('only %d weekday NSE closures parsed for %s and %d for %s (floor %d) -- '
                       'page layout changed? %s NOT overwritten'
                       % (weekdays(this_y), this_y, weekdays(next_y), next_y,
                          MIN_WEEKDAY_HOLIDAYS, path.name))
    data = {k: v for k, v in existing.items() if isinstance(k, str)}
    data.update(good)
    data['_comment'] = 'NSE Trading Holidays - Auto-updated from Zerodha'
    data['_last_updated'] = today.isoformat()
    data['_source'] = URL
    try:
        write_atomic(path, data)
    except OSError as e:
        return False, 'write failed: %s' % e
    total = sum(len(v) for k, v in data.items() if not k.startswith('_'))
    return True, 'saved %d holidays (years refreshed: %s)' % (total, ', '.join(sorted(good)))
