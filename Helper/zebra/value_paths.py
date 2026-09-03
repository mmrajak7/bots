"""Persist the observed value path of every open position, before the logs rot.

WHY THIS EXISTS
---------------
The POLL line is the only record of what the engine actually saw at 14:35 — the
spread's value on the FILL basis, spot, and both legs' book, once per open
position per cycle. On 2026-09-03 a replay over 9,449 of those observations
settled four live questions that no other data could answer:

  * the trail engage level (0.50 -> 0.25; 0.20 would have cost Rs 4,400),
  * the EOD-harvest proposal (REJECTED: overnight holding is +EV, mean +1.3%
    of debit over 59 nights),
  * that COALINDIA #440's round trip was an overnight GAP, not a trail failure,
  * the first cohort-native MAE table (winners max 0.98%, losers min 3.65%).

**And every one of those observations was living in a file scheduled for
deletion.** `common.log_cleanup` gzips a `.log` at 7 days and deletes the
`.log.gz` at 90. The August sessions age out around early December — plausibly
BEFORE the cohort reaches the ~30 closes at which those decisions are due to be
re-derived. `test_the_trail_engage_cliff` tells the next person to "re-run the
replay before changing this"; without this module that instruction expires.

Three sessions were already missing from the laptop copy when the first replay
ran, and two digest days have already been lost for good. The lesson is on the
record; this is the fix.

WHERE IT WRITES, AND WHY THERE
------------------------------
`logs/eod/paths_<date>.json`. That directory is NEVER cleaned and must not be:
`common.log_cleanup` works from an allowlist (`.log` -> gzip, `.log.gz` ->
delete) and skips subdirectories, so nothing here is reachable by it.

One file per session, not one growing file. A day already written is skipped
unless `force=True`, which makes a daily cron idempotent and makes a backfill
safe to re-run — the `deploy_server.sh` incident is what re-running a
"one-time" job costs when it is not.

SIZE, measured on the 2026-08-13..09-03 backfill (16 sessions, 9,477
observations) rather than estimated: **173 bytes per observation** compact.
The August sessions are large (up to 500 KB) because the pre-cohort book held
~20 positions; a cohort-sized session of 4-8 positions runs ~50 KB, so the
forward cost is **roughly 12 MB a year**.

That is an order of magnitude more than the digests (1-9 KB each) and it is
still the right trade: this is the ONLY durable record of what the engine saw,
an option book cannot be reconstructed after the fact, and four live decisions
already rest on it. If it ever needs to shrink, drop `long_bid`/`long_ask`/
`short_bid`/`short_ask` on observations whose `q` is `ok` -- the legs matter
most where the quote was refused.

WHAT IT DOES NOT DO
-------------------
It does not interpret. No replay, no rule, no verdict — those live in whatever
analysis reads this later. This module's only job is to make sure the numbers
still exist when someone asks.
"""

from __future__ import annotations

import glob
import gzip
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config as cfg

logger = logging.getLogger(__name__)

#: One POLL line. Kept deliberately close to the log format rather than made
#: clever: if the log line changes shape this must fail loudly at the next
#: coverage check, not silently match half of it.
POLL_RE = re.compile(
    r'^(?P<ts>[\d\-]+ [\d:,]+).*POLL #(?P<id>\d+) (?P<stock>\S+) '
    r'(?P<dir>CE|PE) '
    r'spot=(?P<spot>[\d.]+) tp=(?P<tp>[\d.]+)(?: \[TP-LATCHED\])? '
    r'sl=(?P<sl>[\d.]+) \| '
    r'value=(?P<val>NA|[\d.]+) \((?P<q>[^)]*)\) debit_sl=(?P<dsl>[\d.]+)'
    r'(?: \| long (?P<lb>[\d.]+|None)/(?P<la>[\d.]+|None)'
    r' short (?P<sb>[\d.]+|None)/(?P<sa>[\d.]+|None))?')

SESSION_GLOB = 'cron_zebra_2026*.log'


def _num(v):
    """A log field as a float, or None. `None` and `NA` are both absent."""
    if v in (None, 'None', 'NA', ''):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _open(path: Path):
    return gzip.open(str(path), 'rt', errors='replace') if path.suffix == '.gz' \
        else open(str(path), errors='replace')


def parse_session(path: Path) -> dict:
    """Every POLL observation in one session log, grouped by trade id.

    Returns `{trade_id: {'stock', 'direction', 'obs': [...]}}`. Observations
    keep the FILL-basis value and the quote quality string, because the two
    together are what separates a real price from the opening-print garbage the
    900s buffer exists to ignore — a replay that drops `quality` reproduces the
    contaminated -12.1%/night answer instead of the honest +1.3%.
    """
    out: dict = {}
    with _open(path) as fh:
        for line in fh:
            m = POLL_RE.search(line)
            if not m:
                continue
            g = m.groupdict()
            rec = out.setdefault(int(g['id']), {
                'stock': g['stock'], 'direction': g['dir'], 'obs': []})
            rec['obs'].append({
                'ts': g['ts'].split(',')[0],
                'spot': _num(g['spot']),
                'tp': _num(g['tp']),
                'sl': _num(g['sl']),
                'val': _num(g['val']),
                'q': g['q'],
                'debit_sl': _num(g['dsl']),
                'long_bid': _num(g['lb']), 'long_ask': _num(g['la']),
                'short_bid': _num(g['sb']), 'short_ask': _num(g['sa']),
            })
    for rec in out.values():
        rec['obs'].sort(key=lambda o: o['ts'])
    return out


def _session_logs() -> dict:
    """`{date: Path}` for every session log on disk, plain or gzipped.

    A plain `.log` WINS over a `.log.gz` of the same date: the cleaner
    compresses in place, so during the changeover both can exist and the plain
    one is the file still being appended to.
    """
    found: dict = {}
    for pat in (SESSION_GLOB, SESSION_GLOB + '.gz'):
        for p in glob.glob(str(cfg.LOG_DIR / pat)):
            path = Path(p)
            m = re.search(r'(\d{8})', path.name)
            if not m:
                continue
            day = datetime.strptime(m.group(1), '%Y%m%d').strftime('%Y-%m-%d')
            if day in found and found[day].suffix != '.gz':
                continue
            found[day] = path
    return found


def out_path(day: str) -> Path:
    return cfg.LOG_DIR / 'eod' / ('paths_%s.json' % day)


def write_day(day: str, force: bool = False) -> Optional[Path]:
    """Extract one session into `logs/eod/paths_<date>.json`.

    Returns the path written, or None when there is nothing to write or the day
    is already captured. Never raises on a bad log: a session that cannot be
    parsed must not stop the others being saved, and losing one day is a far
    smaller failure than a cron that dies and silently stops capturing.
    """
    dest = out_path(day)
    if dest.exists() and not force:
        return None
    src = _session_logs().get(day)
    if src is None:
        return None
    try:
        paths = parse_session(src)
    except Exception as e:                       # broad on purpose, see above
        logger.warning('value paths: could not parse %s: %s', src.name, e)
        return None
    if not paths:
        return None
    obs = sum(len(r['obs']) for r in paths.values())
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'schema': 1,
        'date': day,
        'source': src.name,
        'extracted_at': datetime.now(cfg.IST).replace(tzinfo=None).isoformat(),
        'basis': 'fill',   # value = bid(long) - ask(short); see CLAUDE.md
        'trades': {str(k): v for k, v in sorted(paths.items())},
        'observations': obs,
    }
    tmp = dest.with_suffix('.json.tmp')
    # COMPACT, not pretty-printed. This is machine-read data, and `indent=1`
    # cost 273 bytes/observation against 173 compact -- 37% of the file was
    # whitespace, in a directory that is never cleaned.
    tmp.write_text(json.dumps(payload, separators=(',', ':')), encoding='utf-8')
    os.replace(str(tmp), str(dest))
    logger.info('value paths: %s -> %s (%d trades, %d observations)',
                src.name, dest.name, len(paths), obs)
    return dest


def capture(force: bool = False) -> dict:
    """Save every session log not already captured. The cron entry point.

    Runs over ALL sessions on disk rather than just today's, so a day the cron
    missed is picked up on the next run for as long as its log survives. That
    is the whole margin this module buys: the log lives 90 days, so a capture
    only has to happen once inside that window.
    """
    written, skipped = [], []
    for day in sorted(_session_logs()):
        p = write_day(day, force=force)
        (written if p else skipped).append(day)
    if written:
        logger.info('value paths: captured %d session(s): %s',
                    len(written), ', '.join(written))
    return {'written': written, 'already_had': skipped}


def coverage() -> dict:
    """What is captured, what is still only in a log, and what is lost.

    `at_risk` is the answer that matters: sessions whose observations exist
    ONLY in a log file that `log_cleanup` will eventually delete.
    """
    logs = _session_logs()
    captured = sorted(
        p.name[len('paths_'):-len('.json')]
        for p in (cfg.LOG_DIR / 'eod').glob('paths_*.json'))
    at_risk = sorted(set(logs) - set(captured))
    return {
        'captured': captured,
        'sessions_on_disk': sorted(logs),
        'at_risk': at_risk,
        'observations': sum(
            (json.loads(p.read_text(encoding='utf-8')).get('observations') or 0)
            for p in (cfg.LOG_DIR / 'eod').glob('paths_*.json')),
    }
