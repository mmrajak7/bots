"""The ORDER ENGINE's log, read by the accountability record.

`zebra/digest.py` was blind to `bcs/spread_monitor.py` — the process that
places orders. It read `cron_zebra_<day>.log` and the vet transcripts, and
nothing else, so the end-of-day record could not see a failed close, a hard
cutoff miss, a `partial_close` freeze, a flipped or naked position, retry
exhaustion, or the 70 `Too many requests` failures of 2026-08-28. The owner
designed the digest as the place failures get strong action
(`OPUS_WORK_ORDER.md` M14); it was not reading the thing it exists to hold to
account. Same shape as H1, where `journal_report --compare` could not see the
cohort store.

**Why it survived, and why this module exists at all.** The two logs are not
the same format:

    zebra    2026-08-28 09:15:43,844 [INFO] zebra.monitor: === CYCLE START ===
    monitor  [09:15:43]   BCS #457 JINDALSTEL: Spot=1188.00 | ...

`digest._TS` cannot match the second. Bending one regex into something that
half-matches both is how a parser starts dropping lines it appears to read, so
the monitor gets its OWN parser here, and every finding carries the engine
that produced it. The digest says WHO failed, not merely that something did.

Three properties this file is built around:

**A line tally is not an event.** "17 ERROR lines" is what the digest already
had and it is not actionable. The catalogue below names each failure the
monitor can emit, derived by grepping `bcs/spread_monitor.py` for what it
actually writes — not from memory — and `zebra/tests/test_engine_log.py`
greps that file again to prove the names still exist. When the monitor's
wording changes, that test fails loudly rather than the count going quietly to
zero.

**Silence is never clean.** An absent monitor log, a log that parses to
nothing, or a failure-shaped line matching no named event are all REPORTED.
Silently returning nothing is the defect being fixed here, so it must not be
reachable by a second route (`feedback_watchdog_must_not_all_clear`).

**A blip is not a failed close.** Severity is split: ACTION is a leg exposed,
a close that did not happen, an order refused past a cutoff, a record frozen —
a human must do something. DEGRADED is the engine unable to see or price for a
poll and recovering by itself. Rate limits swamp everything by count and would
bury a single failed close if both were tallied together.

**One line may raise more than one event, deliberately.**
`spot fetch failed: Too many requests` is both a rate limit and a position
that went un-priced, and each count must mean exactly what its name says.
Counts therefore do not sum to the line count and the digest says so.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import config as cfg

logger = logging.getLogger(__name__)

ENGINE_MONITOR = 'monitor'
ENGINE_ZEBRA = 'zebra'

#: Needs a human. A leg exposed, a close that did not happen, a frozen record.
ACTION = 'action'
#: The engine could not see or price for a poll, and recovers by itself.
DEGRADED = 'degraded'

#: `bcs/spread_monitor.py:log()` writes `[HH:MM:SS] message`. A message
#: containing a newline (`log(f"\n  *** FLIPPED POSITION ***")`) puts the
#: PREFIX on the empty first physical line and the payload on the next one
#: with NO timestamp — so a parser that keeps only timestamped lines drops
#: exactly the loudest events in the file. Continuations inherit the last
#: timestamp seen.
_MON_TS = re.compile(r'^\[(\d\d:\d\d:\d\d)\] ?(.*)$')

#: One monitor status line per position per status tick.
_POS = re.compile(r'^\s*(BCS|BPS|FH) #(\d+) ([A-Z0-9&_.-]+)[ :](.*)$')

#: A line that looks like a failure. Anything matching this and NO catalogue
#: entry is reported as uncatalogued — the vocabulary cannot drift quietly.
_SMELL = re.compile(
    r'FAIL|Fail|fail|ERROR|CRITICAL|WARNING|REFUS|Refus|ABORT|abort|'
    r'REJECT|Reject|EXCEPTION|Exception|FATAL|MISSING|naked|NAKED|'
    r'[Cc]ould not|[Cc]annot|[Uu]nable to|QUARANTIN|MALFORMED|STALE|Stale')

#: Un-priced stretches at or above this are worth a flag rather than a row.
#: The monitor prints status every 30s, so one missed tick is noise; two
#: minutes is a window in which a book-driven stop could not have fired.
UNWATCHED_FLAG_SEC = 120

#: A gap this long between consecutive monitor log lines means the engine was
#: not polling. Its own cadence is 5s poll / 30s status.
STALL_GAP_SEC = 180


class Event(NamedTuple):
    """One named failure an engine can emit.

    `probe` is a literal that must still appear in `source`. It is what the
    drift test greps for; None means the wording is not ours to pin (a Kite
    exception string) and the pattern has to stand on its own.
    """
    name: str
    severity: str
    pattern: str
    probe: Optional[str]
    note: str
    source: str = 'bcs/spread_monitor.py'


#: THE VOCABULARY. Ordered most-specific first only for readability — every
#: entry is tested against every line, because one line can legitimately be
#: two events (see the module docstring).
#:
#: Derived 2026-08-28 by grepping `bcs/spread_monitor.py` for its `log(...)`
#: calls, NOT from the incident reports. Add to this list rather than widening
#: an existing pattern: a name is what makes a count actionable.
CATALOGUE: Tuple[Event, ...] = (
    # ── ACTION ─────────────────────────────────────────────────────────────
    Event('flipped_position', ACTION, r'FLIPPED POSITION',
          'FLIPPED POSITION',
          'the book is not what the trade says; no orders placed'),
    Event('short_leg_close_failed', ACTION,
          r'SHORT (?:LEG|CALL|PUT) CLOSE FAILED',
          'SHORT LEG CLOSE FAILED',
          'the hedge could not be bought back — the dangerous half of M14'),
    Event('naked_short_averted', ACTION, r'SHORT RESIDUE REMAINS',
          'SHORT RESIDUE REMAINS',
          'long leg deliberately NOT sold; over-hedged and frozen'),
    Event('short_leg_partial_fill', ACTION, r'Short leg partially filled',
          'Short leg partially filled',
          'part of the short is still live at the broker'),
    Event('long_leg_close_failed', ACTION,
          r'LONG (?:LEG|CALL) CLOSE FAILED|Long call close failed|'
          r'Long put sell failed',
          'LONG LEG CLOSE FAILED',
          'bounded (a long option remains) but the close did not complete'),
    Event('naked_long_remains', ACTION, r'naked long remains',
          'naked long remains',
          "M14's SAFE side — max loss is the long's premium — but a leg is "
          'unintentionally open'),
    Event('close_retries_exhausted', ACTION,
          # `attempt(s)` since M14 gave `close_leg` a caller-supplied ceiling:
          # the recovery path passes 1, and "after 1 attempts" reads wrong. The
          # `probe` below still matched the changed wording, so the drift test
          # could not catch this — a reminder that a probe pins the PHRASE and
          # only the pattern pins the MATCH.
          r'FAILED to close \S+ after \d+ attempt',
          'FAILED to close',
          'every retry for one leg is spent'),
    Event('expiry_force_close_failed', ACTION,
          r'EXPIRY FORCE CLOSE FAILED|Expiry force close FAILED',
          'EXPIRY FORCE CLOSE FAILED',
          'the delivery-margin deadline was not met'),
    Event('close_failed', ACTION, r'Close failed',
          'Close failed',
          'a triggered exit did not book'),
    Event('close_exception', ACTION, r'EXCEPTION during close_spread',
          'EXCEPTION during close_spread',
          'the close path raised; the record is frozen at partial_close'),
    Event('close_not_priced', ACTION, r'CLOSE NOT PRICED',
          'CLOSE NOT PRICED',
          'refused to book a price it could not observe'),
    Event('partial_close_freeze_failed', ACTION,
          r'Could not set partial_close status',
          'Could not set partial_close status',
          'the freeze itself failed — the store lock was cleared by hand'),
    Event('order_rejected', ACTION, r'[Oo]rder REJECTED|ORDER REJECTED',
          'ORDER REJECTED',
          'the broker refused an order'),
    Event('order_cutoff_missed', ACTION, r'ORDER CUTOFF:',
          'ORDER CUTOFF',
          'past the hard cutoff — the order was not placed'),
    Event('late_day_guard', ACTION, r'LATE-DAY GUARD',
          'LATE-DAY GUARD',
          'too close to the close to place orders'),
    Event('reconcile_failed', ACTION, r'RECONCILE FAILED',
          'RECONCILE FAILED',
          'the broker-side audit after a close did not agree'),
    Event('malformed_record', ACTION, r'MALFORMED RECORD',
          'MALFORMED RECORD',
          'a position is NOT being monitored'),
    Event('store_quarantined', ACTION, r'STORE QUARANTINED',
          'STORE QUARANTINED',
          'a whole book was taken out of monitoring'),
    Event('fill_recovery_refused', ACTION, r'REFUSING to recover a fill',
          'REFUSING to recover a fill',
          'only untagged fills existed; refused to adopt a stranger\'s'),
    Event('monitor_stopped', ACTION, r'Monitor stopped\. CHECK POSITION',
          'Monitor stopped. CHECK POSITION MANUALLY',
          'the monitor exited with a position needing a look'),
    Event('fatal', ACTION, r'(?:^|\s)FATAL:', 'FATAL:',
          'the engine gave up'),
    Event('heartbeat_write_failed', ACTION, r'heartbeat write failed',
          'heartbeat write failed',
          'zebra cannot tell whether this engine is alive'),
    Event('time_stop_state_unpersisted', ACTION,
          r'could not persist time-stop attempt state',
          'could not persist time-stop attempt state',
          'a failed force-close will not be remembered across a restart'),
    Event('manual_intervention', ACTION,
          r'[Mm]anual intervention needed|Intervene manually|'
          r'manually \(SELL|Manual sell ',
          'manual intervention needed',
          'the engine asked for a human (M14: must self-resolve)'),
    Event('critical', ACTION, r'CRITICAL', 'CRITICAL',
          'anything the engine itself tagged CRITICAL'),

    # ── DEGRADED ───────────────────────────────────────────────────────────
    Event('rate_limited', DEGRADED, r'Too many requests', None,
          "Kite's own words for a 429; a 10s cooldown that further "
          'requests EXTEND'),
    Event('quote_fail', DEGRADED, r'\[QUOTE-FAIL', 'QUOTE-FAIL',
          'no option book that poll — book-driven stops were blind'),
    Event('suspect_book', DEGRADED, r'\[SUSPECT ', 'SUSPECT',
          'a book arrived and the reliability gate rejected it'),
    Event('spot_fetch_failed', DEGRADED, r'spot fetch failed',
          'spot fetch failed',
          'no spot that poll — TP and the spot veto were blind too'),
    Event('value_fetch_failed', DEGRADED, r'value fetch failed',
          'value fetch failed', 'FH position could not be valued'),
    Event('quote_batch_failed', DEGRADED,
          r'QUOTE BATCH FAILED|LTP BATCH FAILED', 'QUOTE BATCH FAILED',
          'a whole batched read failed — every leg in it went un-priced'),
    Event('quote_guard_rejected', DEGRADED, r'QUOTE GUARD:', 'QUOTE GUARD',
          'valuation rejected by the guard'),
    Event('reverify_abort', DEGRADED,
          r'RE-VERIFY ABORT|RE-VERIFY: quote fetch failed', 'RE-VERIFY',
          'a trigger fired and the fresh quote did not confirm it'),
    Event('close_aborted', DEGRADED,
          r'close abort|aborting leg early|NORMAL close abort', 'close abort',
          'a close stood down before placing anything; it will re-arm'),
    Event('expiry_check_failed', DEGRADED, r'expiry-proximity check failed',
          'expiry-proximity check failed',
          'the delivery/expiry proximity check did not run'),
    Event('positions_missing', DEGRADED, r'positions MISSING',
          'positions MISSING',
          'no legs at the broker for a record (expected while in paper)'),
    Event('paper_record_refused', DEGRADED, r'is a PAPER record',
          'is a PAPER record',
          'the live order path declined a paper record (C5 working)'),
    Event('telegram_failed', DEGRADED,
          r'Telegram alert (?:failed|skipped)', 'Telegram alert failed',
          'an alert did not reach the phone'),
    Event('watchlist_ltp_failed', DEGRADED, r'Watchlist: LTP fetch failed',
          'Watchlist: LTP fetch failed',
          'the price-alert watchlist went un-priced for a poll',
          'zerodha/alert_checker.py'),
    Event('order_book_unreadable', DEGRADED,
          r'Order book unreadable|Could not read the order book|'
          r'Could not check pending orders|Order status poll error|'
          r'Cancel failed for|Post-cancel', 'Order book unreadable',
          'the order book could not be read while an order was in flight'),
    Event('loop_error', DEGRADED, r'ERROR (?:in cron loop )?\(\d+/\d+\)',
          'ERROR in cron loop', 'an unhandled exception in the poll loop'),
    Event('close_in_progress_skipped', DEGRADED, r'CLOSE IN PROGRESS',
          'CLOSE IN PROGRESS',
          'a position was skipped because its close was already running'),

    # ── DEGRADED, zebra side ───────────────────────────────────────────────
    # Not the monitor's vocabulary. They are here because the failure record
    # is per-ENGINE, not per-file: the reader wants one table, attributed.
    Event('value_clamped', DEGRADED, r'VALUE BOUND', 'VALUE BOUND',
          'a valuation was outside a vertical\'s mathematical bounds',
          'zebra/monitor.py'),
    Event('quote_rejected', DEGRADED, r'QUOTE REJECT', 'QUOTE REJECT',
          'a book was refused as an estimate rather than clamped to one',
          'zebra/monitor.py'),
    Event('capital_would_refuse', DEGRADED, r'CAPITAL WOULD REFUSE',
          'CAPITAL WOULD REFUSE',
          'entered in paper past a limit that would bind live',
          'zebra/trade_store.py'),
    Event('auth_warning', DEGRADED, r'AUTH WARNING sent', 'AUTH WARNING sent',
          'the auth health check alerted', 'zebra/health.py'),
)

_COMPILED = tuple((e, re.compile(e.pattern)) for e in CATALOGUE)


# ── reading ─────────────────────────────────────────────────────────────────

class LogRead(NamedTuple):
    """What one log file yielded, INCLUDING what it failed to yield."""
    engine: str
    path: Path
    present: bool
    lines: int          # non-blank physical lines
    parsed: int         # lines carrying a timestamp of the expected shape
    rows: List[tuple]   # (HH:MM:SS, message)
    problems: List[str]

    def as_dict(self) -> dict:
        return {'engine': self.engine, 'path': self.path.name,
                'present': self.present, 'lines': self.lines,
                'parsed': self.parsed,
                'first': self.rows[0][0] if self.rows else None,
                'last': self.rows[-1][0] if self.rows else None,
                'problems': list(self.problems)}


# ── M14 · the structured EVENT grammar ──────────────────────────────────────
#
# Everything above catalogues PROSE, matched by regex, because that is what the
# monitor's log lines are. The recovery sweep is new code and emits a
# structured line instead:
#
#     [HH:MM:SS] EVENT recovery_attempt cls=bounded id=1 n=1/3 store=BCS
#
# Parsed rather than pattern-matched, so the digest counts BY NAME and never by
# warning tally. That distinction is the whole point: "223 degraded events"
# tells a reader to stop reading, while "2 recovery attempts, 1 escalation"
# tells them what to do. A new event name needs no new regex and cannot be
# silently absorbed by an existing one.

EVENT_LINE = re.compile(r'\bEVENT\s+([a-z_]+)((?:\s+[a-z_]+=[^\s]+)*)')


def parse_events(rows):
    """`[(name, {k: v}), ...]` for every structured EVENT line in `rows`.

    `rows` is the `(timestamp, text)` shape `read_monitor_log` returns. Values
    stay strings: the digest displays them and any that must be compared are
    compared as the strings they were written as, which cannot silently coerce
    `n=1/3` into something numeric-looking.
    """
    out = []
    for row in rows:
        text = row[1] if isinstance(row, (tuple, list)) and len(row) > 1 else row
        m = EVENT_LINE.search(str(text))
        if not m:
            continue
        kv = dict(pair.split('=', 1) for pair in m.group(2).split() if '=' in pair)
        out.append((m.group(1), kv))
    return out


#: Recovery events that mean a HUMAN must do something. Counted separately so
#: they cannot be averaged away into a total: an exhausted incident is a live
#: position with dead stops, and one of them matters more than fifty waits.
RECOVERY_NEEDS_HUMAN = ('recovery_exhausted', 'unpriced_refusal',
                        'recovery_blind',
                        # S3. A leg still live on a record already booked
                        # CLOSED. Nothing will ever order against it - the
                        # only thing standing between it and invisibility is
                        # somebody reading this line.
                        'reconcile_residue', 'residue_unresolved',
                        'residue_blind', 'reconcile_unknown_shape',
                        # The post-close audit itself failed. Arguably the
                        # most dangerous of the four — a close NOBODY
                        # VERIFIED — and it was the one left out of this
                        # tuple while its own emitter's comment claimed "the
                        # digest counts it by name". It landed in the generic
                        # counts and was never surfaced.
                        'reconcile_blind')


def monitor_log_path(day: str) -> Path:
    """`bcs/spread_monitor.py:4579` names it; keep the two in step."""
    return cfg.LOG_DIR / f"spread_monitor_cron_{day.replace('-', '')}.log"


def read_monitor_log(day: str) -> LogRead:
    """Parse the order engine's log for one day.

    Never raises — the digest must not be able to matter more than an
    inconvenience — but it does not go quiet either. A file that exists and
    yields no rows produces a PROBLEM string, because a format change that
    made this parser silently count zero is precisely the defect this module
    was written to fix.
    """
    p = monitor_log_path(day)
    problems: List[str] = []
    if not p.exists():
        return LogRead(ENGINE_MONITOR, p, False, 0, 0, [],
                       ['no %s — the order engine\'s log is ABSENT, so no '
                        'monitor failure is covered by this digest' % p.name])
    rows: List[tuple] = []
    lines = parsed = 0
    last_ts: Optional[str] = None
    try:
        text = p.read_text(encoding='utf-8', errors='replace')
    except Exception as e:                          # pragma: no cover - IO
        logger.warning('could not read %s: %s', p, e)
        return LogRead(ENGINE_MONITOR, p, True, 0, 0, [],
                       ['could not read %s: %s' % (p.name, e)])
    for line in text.splitlines():
        if not line.strip():
            continue
        lines += 1
        m = _MON_TS.match(line)
        if m:
            parsed += 1
            last_ts = m.group(1)
            body = m.group(2)
            if body.strip():
                rows.append((last_ts, body))
        elif last_ts is not None:
            # A continuation of a multi-line log() call. It inherits the
            # timestamp; dropping it would drop FLIPPED POSITION and the
            # CRITICAL banners, which are written exactly this way.
            rows.append((last_ts, line))
        else:
            rows.append(('00:00:00', line))
    if lines and not parsed:
        problems.append(
            '%s has %d line(s) and NONE carries a [HH:MM:SS] prefix — the '
            'monitor log format changed and zebra/engine_log.py no longer '
            'parses it. Treat every count below as UNKNOWN, not zero.'
            % (p.name, lines))
    elif lines and parsed / float(lines) < 0.5:
        problems.append(
            '%s: only %d of %d line(s) carry a [HH:MM:SS] prefix — the '
            'format may have changed' % (p.name, parsed, lines))
    return LogRead(ENGINE_MONITOR, p, True, lines, parsed, rows, problems)


#: zebra's logger states its own severity, so use it rather than guessing
#: from the prose. The monitor has no level field at all, which is why the
#: smell test exists for that side only.
ZEBRA_LEVELS = ('WARNING', 'ERROR', 'CRITICAL')


def zebra_rows_as_pairs(rows: List[tuple],
                        levels: Tuple[str, ...] = ZEBRA_LEVELS) -> List[tuple]:
    """`digest._read_log` gives (ts, level, logger, msg); this wants (t, msg).

    The zebra timestamp carries the date; the monitor's does not. Everything
    downstream compares times within one day, so trim to HH:MM:SS and keep
    ONE shape rather than teaching each consumer about both.

    Filtered to WARNING and worse. An INFO line saying `st_failed=1` inside
    the scanner's own funnel summary is not a failure event — it is a skip
    count the digest already reports in the funnel — and letting the smell
    test call it one trains the reader to ignore the uncatalogued list, which
    is the one place vocabulary drift shows up.
    """
    return [(t[11:19] if len(t) >= 19 else t, m)
            for t, lvl, _, m in rows if not levels or lvl in levels]


# ── the catalogue applied ───────────────────────────────────────────────────

def catalogue(rows: List[tuple], engine: str) -> Tuple[List[dict], List[dict]]:
    """(named events, uncatalogued failure-shaped lines) for one engine.

    Every catalogue entry is tested against every line: one line can be two
    events, and each count must mean what its name says. See the module
    docstring — this is why the counts do not sum to the line count.
    """
    hits: Dict[str, dict] = {}
    unc: Dict[str, dict] = {}
    for t, msg in rows:
        matched = False
        for ev, rx in _COMPILED:
            if rx.search(msg):
                matched = True
                h = hits.get(ev.name)
                if h is None:
                    hits[ev.name] = {'engine': engine, 'name': ev.name,
                                     'severity': ev.severity, 'count': 1,
                                     'first': t, 'last': t,
                                     'note': ev.note,
                                     'sample': msg.strip()[:120]}
                else:
                    h['count'] += 1
                    h['last'] = t
        if not matched and _SMELL.search(msg):
            key = re.sub(r'\d+', 'N', msg.strip())[:80]
            u = unc.get(key)
            if u is None:
                unc[key] = {'engine': engine, 'count': 1, 'first': t,
                            'sample': msg.strip()[:120]}
            else:
                u['count'] += 1
    order = {ACTION: 0, DEGRADED: 1}
    events = sorted(hits.values(),
                    key=lambda h: (order.get(h['severity'], 2), -h['count']))
    return events, sorted(unc.values(), key=lambda u: -u['count'])


# ── unwatched positions ─────────────────────────────────────────────────────

def _obs_state(rest: str) -> Optional[str]:
    """'priced' / 'unpriced' / None for one monitor status line body.

    The monitor prints the SHORT form of a status line — Spot, TP and SL with
    no `Spread:` — exactly when it has no usable option book, whether the
    quote failed (`[QUOTE-FAIL ...]`) or the reliability gate rejected it
    (`[SUSPECT ...]`). Both mean the same thing for risk: no valuation, so
    debit SL, trail and the intrinsic floor could not have fired.
    """
    if rest.startswith('spot fetch failed') or rest.startswith('value fetch failed'):
        return 'unpriced'
    if 'Spot=' not in rest:
        return None
    return 'priced' if '| Spread:' in rest else 'unpriced'


def _why(rest: str) -> str:
    if 'spot fetch failed' in rest:
        return 'no spot'
    if '[QUOTE-FAIL' in rest:
        return 'quote failed'
    if '[SUSPECT' in rest:
        return 'book rejected'
    return 'no valuation'


def unwatched(rows: List[tuple], day: str) -> List[dict]:
    """Per position, how long it was un-priced today.

    2026-08-28: JINDALSTEL sat at `[QUOTE-FAIL Too many requests]` for minutes
    at a stretch. Nothing reported it, and it is a real risk — a book-driven
    stop could not have fired in that window. A per-line count does not show
    it; the DURATION does.

    A stretch runs from the first un-priced observation to the next priced
    one. A stretch still open at the last observation of that position ends
    there and is marked `open_at_end` — it is not silently extended to the
    close of the session, and it is not dropped either.
    """
    def _dt(t: str) -> datetime:
        return datetime.strptime('%s %s' % (day, t), '%Y-%m-%d %H:%M:%S')

    seq: Dict[str, List[tuple]] = {}
    for t, msg in rows:
        m = _POS.match(msg)
        if not m:
            continue
        rest = m.group(4).strip()
        st = _obs_state(rest)
        if st is None:
            continue
        key = '%s #%s %s' % (m.group(1), m.group(2), m.group(3))
        seq.setdefault(key, []).append((t, st, rest))

    out = []
    for key, obs in seq.items():
        stretches = []
        start = None
        why = ''
        for t, st, rest in obs:
            if st == 'unpriced':
                if start is None:
                    start, why = t, _why(rest)
            elif start is not None:
                stretches.append((start, t, (_dt(t) - _dt(start)).total_seconds(),
                                  why, False))
                start = None
        if start is not None:
            end = obs[-1][0]
            stretches.append((start, end, (_dt(end) - _dt(start)).total_seconds(),
                              why, True))
        if not stretches:
            continue
        longest = max(stretches, key=lambda s: s[2])
        out.append({
            'position': key,
            'stretches': len(stretches),
            'total_sec': int(sum(s[2] for s in stretches)),
            'longest_sec': int(longest[2]),
            'longest_from': longest[0], 'longest_to': longest[1],
            'why': longest[3],
            'open_at_end': any(s[4] for s in stretches),
            'observations': len(obs),
        })
    return sorted(out, key=lambda r: (-r['total_sec'], -r['longest_sec']))


def stalls(rows: List[tuple], day: str) -> List[tuple]:
    """Gaps in the POSITION-STATUS stream during market hours.

    The analogue of `digest._cycles`' missed-cycle detection, for the other
    engine: the monitor prints a status line per open position every 30s, so a
    gap of minutes inside the session is unmonitored time on open positions.

    Measured on the STATUS lines, not on every line in the file, and that is
    the whole design. A day with an empty book is silent for hours — the
    engine has nothing to say — and measuring raw line gaps reported 09:39 to
    15:30 as a 351-minute stall on 2026-07-07, a day with zero open trades.
    A stall must be the absence of something that was DUE. No open position,
    nothing due, no stall.

    Also bounded to 09:15-15:30: outside it the cron restarts every five
    minutes and exits immediately, which is the cron working.
    """
    def _dt(t: str) -> datetime:
        return datetime.strptime('%s %s' % (day, t), '%Y-%m-%d %H:%M:%S')

    open_hm = '%02d:%02d:00' % cfg.MARKET_OPEN
    close_hm = '%02d:%02d:00' % cfg.MARKET_CLOSE
    ticks = sorted({t for t, msg in rows
                    if _POS.match(msg) and _obs_state(_POS.match(msg).group(4).strip())})
    out = []
    for a, b in zip(ticks, ticks[1:]):
        if a < open_hm or b > close_hm:
            continue
        try:
            secs = (_dt(b) - _dt(a)).total_seconds()
        except ValueError:                          # pragma: no cover - guard
            continue
        if secs >= STALL_GAP_SEC:
            out.append((a[:5], b[:5], round(secs / 60.0)))
    return out


def _mode(rows: List[tuple]) -> Optional[str]:
    """DRY RUN or LIVE EXECUTION — whether the engine could trade at all.

    A digest full of clean exits means something different when the engine
    that would have placed the orders was in dry run.
    """
    for _, msg in rows[:60]:
        m = re.match(r'\s*Mode:\s+(\S.*?)\s*$', msg)
        if m:
            return m.group(1)
    return None


# ── the whole picture ───────────────────────────────────────────────────────

def analyse(day: str, zebra_rows: Optional[List[tuple]] = None) -> dict:
    """Both engines' failure record for one day, as data.

    `zebra_rows` is `digest._read_log`'s output; the catalogue is applied to
    it too so that a rate limit is counted wherever it happened and the reader
    can see WHICH engine hit it. The digest's existing `warnings` section is
    untouched and still zebra-only.
    """
    mon = read_monitor_log(day)
    events, unc = catalogue(mon.rows, ENGINE_MONITOR)
    logs = [mon.as_dict()]
    if zebra_rows is not None:
        allz = zebra_rows_as_pairs(zebra_rows, levels=())
        zev, zunc = catalogue(zebra_rows_as_pairs(zebra_rows), ENGINE_ZEBRA)
        events = events + zev
        unc = unc + zunc
        logs.append({'engine': ENGINE_ZEBRA,
                     'path': 'cron_zebra_%s.log' % day.replace('-', ''),
                     'present': bool(allz), 'lines': len(allz),
                     'parsed': len(allz),
                     'first': allz[0][0] if allz else None,
                     'last': allz[-1][0] if allz else None,
                     'problems': []})
    order = {ACTION: 0, DEGRADED: 1}
    events.sort(key=lambda h: (order.get(h['severity'], 2), -h['count']))
    return {
        'logs': logs,
        'mode': _mode(mon.rows),
        'events': events,
        # M14. Structured, not catalogued: counted BY NAME so an escalation
        # cannot be averaged into a degraded total.
        'recovery': recovery_summary(mon.rows),
        'uncatalogued': unc[:8],
        'uncatalogued_total': sum(u['count'] for u in unc),
        'unwatched': unwatched(mon.rows, day),
        'stalls': stalls(mon.rows, day),
        'problems': list(mon.problems),
    }


def recovery_summary(rows):
    """What the frozen-close sweep did today, by name.

    Kept apart from `events` on purpose. Those are PROSE matched by regex and
    are mostly degraded-and-self-recovering; these are decisions the sweep
    made about a position nobody is watching, and one of them can matter more
    than fifty rate limits. Averaging the two together is how the thing that
    needs a human ends up inside a number that says "noisy day".
    """
    parsed = parse_events(rows)
    if not parsed:
        return {}
    counts, per_trade = {}, {}
    for name, kv in parsed:
        counts[name] = counts.get(name, 0) + 1
        who = (kv.get('store'), kv.get('id'))
        if who != (None, None):
            per_trade.setdefault(who, []).append(name)
    return {
        'counts': counts,
        'needs_human': {n: c for n, c in counts.items()
                        if n in RECOVERY_NEEDS_HUMAN},
        'trades': {'%s#%s' % w: v for w, v in sorted(
            per_trade.items(), key=lambda kv: (kv[0][0] or '', kv[0][1] or ''))},
    }


def render_recovery(rec):
    """The FROZEN section. Absent entirely when nothing froze — a heading that
    says "0" every day is a heading people stop reading."""
    if not rec or not rec.get('counts'):
        return []
    out = ['## Frozen closes (M14 recovery)', '']
    out.append('| event | n |')
    out.append('|---|---|')
    for name, n in sorted(rec['counts'].items(), key=lambda kv: (-kv[1], kv[0])):
        mark = ' ⚠' if name in RECOVERY_NEEDS_HUMAN else ''
        out.append('| `%s`%s | %d |' % (name, mark, n))
    if rec.get('trades'):
        out.append('')
        for who, names in rec['trades'].items():
            out.append('- **%s** — %s' % (who, ' → '.join(names)))
    return out


def recovery_flags(rec):
    """Only what a human must act on. A recovery that RESOLVED is good news and
    belongs in the section above, not in the list of things to read first."""
    out = []
    if not rec:
        return out
    for name, n in sorted((rec.get('needs_human') or {}).items()):
        if name == 'recovery_exhausted':
            out.append('%d frozen close(s) EXHAUSTED recovery — a position is '
                       'live at the broker with dead stops and no further '
                       'automatic attempt will be made. Close it by hand.' % n)
        elif name == 'unpriced_refusal':
            out.append('%d frozen close(s) could not be PRICED from our own '
                       'fills and were not booked — book them by hand with '
                       'the real exit price.' % n)
        elif name == 'recovery_blind':
            out.append('%d recovery sweep pass(es) could not read broker '
                       'positions, so nothing was classified.' % n)
        elif name == 'reconcile_residue':
            out.append('%d post-close reconcile(s) found a leg STILL LIVE on '
                       'a trade already booked closed. No order will ever be '
                       'placed against a closed record - close the leg in '
                       'Kite, or clear it with --clear-residue.' % n)
        elif name == 'reconcile_blind':
            out.append('%d close(s) could not be VERIFIED against the broker '
                       'at all — the post-close position read failed. Treat '
                       'those closes as unconfirmed and check Kite.' % n)
        elif name == 'residue_unresolved':
            out.append('%d day(s) on which a post-close residue was still '
                       'live and un-cleared.' % n)
        elif name == 'residue_blind':
            out.append('%d residue sweep pass(es) could not read broker '
                       'positions, so no residue was re-checked.' % n)
        elif name == 'reconcile_unknown_shape':
            out.append('%d close(s) could not be VERIFIED at all: the record '
                       'declared no option legs, so the post-close check had '
                       'nothing to read. Treat as unverified, not clean.' % n)
    if (rec.get('counts') or {}).get('frozen_paper_skipped'):
        # Not a problem — but it IS the line that proves the paper guard ran,
        # and its silence would be indistinguishable from the guard's absence.
        out.append('%d paper record(s) skipped by the recovery sweep (correct '
                   '— paper never reaches the order path).'
                   % rec['counts']['frozen_paper_skipped'])
    return out


def flags(a: dict) -> List[str]:
    """Facts that earn a look, in the digest's own voice: what, never why."""
    out: List[str] = []
    for p in a.get('problems') or []:
        out.append(p)
    # Before the catalogue's own flags: a frozen position with dead stops
    # outranks every degraded-and-recovering count below it.
    out.extend(recovery_flags(a.get('recovery')))
    for ev in a.get('events') or []:
        if ev['severity'] != ACTION:
            continue
        out.append('%s: %d x %s (%s–%s) — %s'
                   % (ev['engine'], ev['count'], ev['name'],
                      ev['first'], ev['last'], ev['note']))
    deg = [e for e in (a.get('events') or []) if e['severity'] == DEGRADED]
    if deg:
        top = sorted(deg, key=lambda e: -e['count'])[:4]
        out.append('%s degraded event(s) across %d class(es): %s'
                   % (sum(e['count'] for e in deg), len(deg),
                      ', '.join('%s %s x%d' % (e['engine'], e['name'],
                                               e['count']) for e in top)))
    for u in a.get('unwatched') or []:
        if u['longest_sec'] >= UNWATCHED_FLAG_SEC:
            out.append('%s un-priced %s in %d stretch(es), longest %s '
                       '(%s–%s, %s) — no book-driven stop could have fired'
                       % (u['position'], _hms(u['total_sec']), u['stretches'],
                          _hms(u['longest_sec']), u['longest_from'],
                          u['longest_to'], u['why']))
    for a0, b0, mins in (a.get('stalls') or []):
        out.append('order engine silent %dm (%s->%s) — no poll ran'
                   % (mins, a0, b0))
    if a.get('uncatalogued'):
        out.append('%d order-engine log line(s) in %d class(es) look like '
                   'failures and match NO named event — name them in '
                   'zebra/engine_log.py CATALOGUE or they stay uncounted: %s'
                   % (a['uncatalogued_total'], len(a['uncatalogued']),
                      a['uncatalogued'][0]['sample'][:60]))
    return out


def _hms(sec: int) -> str:
    if sec < 60:
        return '%ds' % sec
    return '%dm%02ds' % (sec // 60, sec % 60)


def render(a: dict) -> List[str]:
    """The digest section, as markdown lines. Compact enough to paste."""
    L: List[str] = []
    A = L.append
    # The frozen-close section goes FIRST when there is one: it is about
    # positions nothing is watching, and everything below it is about engines
    # that are, by definition, still running.
    rec = render_recovery(a.get('recovery'))
    if rec:
        L.extend(rec)
        A('')
    A('## Engines')
    for lg in a.get('logs') or []:
        if not lg['present']:
            A('- **%s** `%s` — ABSENT' % (lg['engine'], lg['path']))
            continue
        A('- **%s** `%s` %s–%s, %d line(s)%s'
          % (lg['engine'], lg['path'], lg['first'], lg['last'], lg['lines'],
             (' — mode %s' % a['mode'])
             if lg['engine'] == ENGINE_MONITOR and a.get('mode') else ''))
        for p in lg['problems']:
            A('  - ⚠ %s' % p)
    ev = a.get('events') or []
    if ev:
        A('')
        A('| engine | severity | event | n | first | last |')
        A('|---|---|---|---|---|---|')
        for e in ev:
            A('| %s | %s | `%s` | %d | %s | %s |'
              % (e['engine'], e['severity'], e['name'], e['count'],
                 e['first'], e['last']))
        A('')
        A('_One line can raise more than one event (a quote failure caused by '
          'a rate limit is both), so these do not sum to a line count._')
    else:
        A('')
        A('_no named failure event in either engine log_')
    uw = a.get('unwatched') or []
    if uw:
        A('')
        A('**Un-priced time** (no option book — debit SL / trail / floor blind)')
        A('')
        A('| position | total | stretches | longest | window | why |')
        A('|---|---|---|---|---|---|')
        for u in uw:
            A('| %s | %s | %d | %s | %s–%s%s | %s |'
              % (u['position'], _hms(u['total_sec']), u['stretches'],
                 _hms(u['longest_sec']), u['longest_from'], u['longest_to'],
                 ' (open)' if u['open_at_end'] else '', u['why']))
    if a.get('uncatalogued'):
        A('')
        A('<details><summary>uncatalogued failure-shaped lines</summary>')
        A('')
        for u in a['uncatalogued']:
            A('- `%dx` [%s] %s' % (u['count'], u['engine'], u['sample']))
        A('')
        A('</details>')
    return L
