"""Event calendar — the facts the mechanical rules cannot see.

Results dates, ex-dividends, budget day, election results, RBI policy. None of
these are in a price series, and all of them can invalidate a structure that
every numeric gate just passed. A Sonnet agent refreshes this file every couple
of hours; the entry checklist and the position-review pre-filter read it.

Design notes
------------
- **Read-mostly and never load-bearing.** A missing, stale or corrupt calendar
  degrades to "no known events" and the bot behaves exactly as it did before
  this file existed. An event feed that can halt trading is a liability, not a
  safety feature.
- **The agent cannot write this file directly.** It builds a candidate JSON and
  installs it through `zebra events replace`, which validates every row and
  rejects the batch atomically. Same discipline as the trade store: agents call
  verbs, verbs own the schema.
- **Whole-file replace, not append.** Events get cancelled and rescheduled, so a
  refresh must be able to REMOVE rows. Append-only would accumulate phantom
  events that quietly veto entries forever.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import date, datetime, timedelta
from typing import Optional

from . import config as cfg
from .filelock import exclusive

logger = logging.getLogger(__name__)

# Only these matter enough to act on. An open vocabulary would let the agent
# invent categories nobody downstream handles.
EVENT_TYPES = ('results', 'ex_dividend', 'bonus', 'split', 'rights',
               'budget', 'election', 'rbi_policy', 'expiry', 'other')
# Events that move the whole market rather than one symbol.
MARKET_TYPES = ('budget', 'election', 'rbi_policy')

# Corporate actions that RE-PRICE the underlying and get the option strikes
# adjusted with it. On the ex-date every spot level this bot has stored —
# entry_spot, tp_spot, sl_spot, the recorded peak — refers to a share that no
# longer exists, and the per-share debit refers to a lot size that has changed
# too. Nothing automated can repair that, so the position is suspended and the
# human is told.
#
# `ex_dividend` is deliberately NOT in this list. An ordinary dividend drops
# the stock for real and the strikes are not adjusted, so a stop firing there
# is a genuine stop on a genuine loss. Suppressing it would be precisely the
# "deferring a real, corroborated exit" error the vetting doc warns about — the
# way a capped loss becomes a maximum loss.
ADJUSTMENT_TYPES = ('bonus', 'split', 'rights')


def _today() -> date:
    return datetime.now(cfg.IST).date()


def _parse_date(v) -> Optional[date]:
    try:
        return datetime.strptime(str(v), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def load() -> dict:
    """Read the calendar. Never raises; a broken file reads as empty."""
    if not cfg.EVENT_FILE.exists():
        return {'refreshed_at': None, 'events': []}
    try:
        with open(cfg.EVENT_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get('events'), list):
            raise ValueError('expected {refreshed_at, events[]}')
        return data
    except Exception as e:
        logger.error('Event calendar unreadable (%s) — treating as empty', e)
        return {'refreshed_at': None, 'events': []}


def is_stale(data: Optional[dict] = None, now: Optional[datetime] = None) -> bool:
    data = load() if data is None else data
    ts = data.get('refreshed_at')
    if not ts:
        return True
    try:
        age = (now or datetime.now()) - datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return True
    return age.total_seconds() > cfg.EVENT_REFRESH_SEC


def _confidence(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 2) if 0.0 <= f <= 1.0 else None


def validate(events) -> list:
    """Return only well-formed rows, logging what was dropped and why.

    Dropping silently would let a malformed refresh empty the calendar while
    reporting success — the failure mode where everything looks fine and no
    event is ever seen again.
    """
    if not isinstance(events, list):
        raise ValueError('events must be a list')
    clean, dropped = [], []
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            dropped.append((i, 'not an object'))
            continue
        d = _parse_date(e.get('date'))
        etype = e.get('type')
        title = str(e.get('title') or '').strip()
        symbol = (e.get('symbol') or '').strip().upper() or None
        if d is None:
            dropped.append((i, 'bad date %r' % e.get('date')))
        elif etype not in EVENT_TYPES:
            dropped.append((i, 'unknown type %r' % etype))
        elif not title:
            dropped.append((i, 'empty title'))
        elif etype not in MARKET_TYPES and not symbol:
            dropped.append((i, 'per-stock event with no symbol'))
        else:
            clean.append({
                'date': d.isoformat(),
                'type': etype,
                'symbol': symbol,
                'title': title[:200],
                # Coerced, not passed through. The doc asks the agent for a
                # 0..1 estimate and the row is meant to be judged on it, so a
                # string or a 7 arriving here would be silently unusable to any
                # future consumer. Unparseable becomes None — "no stated
                # confidence" — rather than a number nobody can trust.
                'confidence': _confidence(e.get('confidence')),
                'source': str(e.get('source') or '')[:200],
            })
    for i, why in dropped:
        logger.warning('Event row %d dropped: %s', i, why)
    if dropped and not clean:
        raise ValueError('every event row was invalid (%d dropped)' % len(dropped))
    return clean


def replace(events, refreshed_at: Optional[str] = None,
            allow_empty: bool = False) -> dict:
    """Atomically install a validated calendar. Returns the stored document."""
    clean = validate(events)
    if not clean and not allow_empty:
        # An empty install wipes the calendar AND refreshes the timestamp, so
        # it reads healthy for two hours while every gate sees no events. That
        # is indistinguishable from a failed research run, so make the caller
        # say it meant it.
        raise ValueError('refusing to install an empty calendar — pass '
                         'allow_empty if the market really has no events')
    doc = {'refreshed_at': refreshed_at or datetime.now().isoformat(),
           'events': sorted(clean, key=lambda e: (e['date'], e['type']))}
    with exclusive(cfg.EVENT_LOCK):
        cfg.EVENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(cfg.EVENT_FILE.parent),
                                   suffix='.tmp', prefix='zev_')
        try:
            with os.fdopen(fd, 'w') as f:
                fd = None
                json.dump(doc, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(5):
                try:
                    os.replace(tmp, str(cfg.EVENT_FILE))
                    break
                except PermissionError:          # Windows: another reader open
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
            tmp = None
        finally:
            if fd is not None:
                os.close(fd)
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    _clear_refresh_pending()        # the agent landed; the next staleness may spawn
    logger.info('Event calendar replaced: %d events', len(clean))
    return doc


# ── queries ──────────────────────────────────────────────────────────────
def upcoming(symbol: Optional[str] = None, within_days: Optional[int] = None,
             data: Optional[dict] = None, today: Optional[date] = None) -> list:
    """Events from today forward that bear on `symbol` (market-wide included)."""
    data = load() if data is None else data
    today = today or _today()
    horizon = cfg.EVENT_HORIZON_DAYS if within_days is None else within_days
    sym = (symbol or '').upper() or None
    out = []
    for e in data.get('events') or []:
        d = _parse_date(e.get('date'))
        if d is None or d < today or (d - today).days > horizon:
            continue
        if e.get('type') in MARKET_TYPES or sym is None or e.get('symbol') == sym:
            out.append(dict(e, days_away=(d - today).days))
    return sorted(out, key=lambda e: e['days_away'])


def adjustment_today(symbol: str, data: Optional[dict] = None,
                     today: Optional[date] = None) -> Optional[dict]:
    """A strike-adjusting corporate action dated TODAY for this symbol.

    The calendar has been collecting these and nothing consumed them. On such a
    day a 1:1 bonus halves the quoted spot while the exchange doubles the lot
    size and halves the strikes — so a stored `sl_spot` from yesterday is
    instantly and catastrophically breached by an event in which nothing
    actually went wrong. Market-wide events are excluded by construction: a
    budget does not re-price one company's shares.
    """
    data = load() if data is None else data
    today = today or _today()
    sym = (symbol or '').upper()
    if not sym:
        return None
    for e in data.get('events') or []:
        if e.get('type') not in ADJUSTMENT_TYPES:
            continue
        if e.get('symbol') != sym:
            continue
        if _parse_date(e.get('date')) == today:
            return dict(e)
    return None


def refresh_pending(now: Optional[datetime] = None) -> bool:
    """Is a calendar agent already in flight?

    Staleness alone is NOT a spawn condition. The file only stops being stale
    when an agent successfully lands `events replace`, which takes minutes — so
    a stale-triggered spawn on a 5-minute cycle launches a fresh agent every
    cycle until one succeeds. If the agent can never succeed (expired auth,
    research failure, a batch that keeps getting rejected) that is one Claude
    process every 5 minutes, ~75/day, forever, with nothing on Telegram.

    Same fix as the exit vet: an in-flight marker with a deadline, so a hung
    agent bounds the damage at one process per deadline instead of one per
    cycle.
    """
    try:
        with open(_pending_path()) as f:
            until = datetime.fromisoformat(json.load(f)['until'])
    except Exception:
        return False
    return (now or datetime.now()) < until


def _pending_path():
    return cfg.EVENT_FILE.with_suffix('.pending.json')


def _mark_refresh_pending() -> None:
    deadline = datetime.now() + timedelta(seconds=cfg.VET_TIMEOUT_SEC)
    try:
        cfg.EVENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_pending_path(), 'w') as f:
            json.dump({'until': deadline.isoformat()}, f)
    except Exception as e:
        # Failing to write the marker means we might spawn again next cycle.
        # Log it loudly rather than pretending: a silent failure here is the
        # spawn storm coming back.
        logger.error('Could not mark event refresh in-flight (%s) — a repeat '
                     'spawn is possible next cycle', e)


def _clear_refresh_pending() -> None:
    try:
        os.unlink(str(_pending_path()))
    except OSError:
        pass


def refresh(symbols, spawn: bool = True) -> bool:
    """Spawn the calendar agent. Returns True if a process was started.

    Detached and fire-and-forget, like every other agent here: a hung research
    call must never hold up a trading cycle.
    """
    if refresh_pending():
        logger.debug('Event refresh already in flight — not re-spawning')
        return False
    if not spawn:
        return False
    from .vet import _spawn_generic
    prompt = cfg.EVENT_PROMPT_TEMPLATE.format(
        vetting_doc=cfg.VETTING_DOC,
        python=_interpreter(),
        symbols=', '.join(sorted(set(symbols))[:40]) or 'none',
    )
    _mark_refresh_pending()
    return _spawn_generic(prompt, cfg.EVENT_MODEL, 'events',
                          channel='events') is not None


def _interpreter() -> str:
    import sys
    return sys.executable
