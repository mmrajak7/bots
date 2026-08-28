"""One day of zebra, reduced to something a human can read in a minute.

NOT `report.py`. That sends a P&L summary to a phone. This is the diagnostic
record for the two-month paper run: what the machine did, what it refused to
do, and which facts earn a second look. Different audience, different job.

Design constraints, both deliberate:

**Deterministic. No agent, no tokens, no quota.** The one outage this system
has had was a Claude usage limit, and an EOD agent run competes for the same
session budget as the next morning's entry vets. The thing that would starve is
the layer that gates real entries. Python reads the log; a human reads the
digest.

**Compact enough to PASTE.** The bot runs on the Pi, the analysis happens on
another box, and SSH between them is broken. A digest nobody can move is a
digest nobody reads, so this targets ~60 lines rather than completeness.

**Facts and flags, never diagnoses.** "Vet latency p90 8m against a 2m
baseline" earns a look. "The vetting layer is degraded" is a conclusion a
script has not earned, and a wrong one trains the reader to ignore the right
ones.

Authority is split on purpose: TRADE facts come from the store, which is
structured and correct; PROCESS facts come from the log, which is the only
place they exist.

**BOTH engines, not one.** Until 2026-08-28 this file read `cron_zebra_*.log`
and the vet transcripts and nothing else — so the accountability record was
blind to `bcs/spread_monitor.py`, the process that places orders. It could not
see a failed close, a hard-cutoff miss, a `partial_close` freeze, a flipped or
naked position, or that morning's 70 `Too many requests` failures. The monitor
writes a different line format, so a naive attempt would have yielded nothing
rather than failing loudly. `zebra/engine_log.py` gives it its own parser and
its own named-event vocabulary; every finding here carries the engine that
produced it.

    python -m zebra digest              # today
    python -m zebra digest --date 2026-08-15
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from . import config as cfg
from . import engine_log
# `reason_label` is the report's wording, imported rather than re-derived: the
# Closed table below did its own `paper:` strip and printed a bridged
# `SL_SPREAD` raw — the FIFTH copy of that vocabulary, in the file that owns
# the arming gate. `tp_latch_evidence` is shared for the same reason: two
# readers of a brand-new set of fields is exactly when the second copy starts.
from .report import reason_label, tp_latch_evidence

logger = logging.getLogger(__name__)

_TS = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \[(\w+)\] ([\w.]+): (.*)$')
#: Digits stripped so "TRIGGERED #376" and "#404" collapse to one class.
_NUM = re.compile(r'\d+')


def _read_log(day: str) -> List[tuple]:
    """(time, level, logger, message) for one day, or [] if the file is gone."""
    p = cfg.LOG_DIR / f"cron_zebra_{day.replace('-', '')}.log"
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding='utf-8', errors='replace').splitlines():
            m = _TS.match(line)
            if m:
                out.append((m.group(1), m.group(2), m.group(3), m.group(4)))
    except Exception as e:                       # pragma: no cover - IO guard
        logger.warning('could not read %s: %s', p, e)
    return out


def _cycles(rows: List[tuple]) -> dict:
    """Cycle count, duration and — the one that matters — MISSED cycles.

    cron fires every 5 minutes; a gap materially longer means a cycle did not
    run or did not finish, and during market hours that is unmonitored time on
    open positions. It is invisible in any per-event count, which is why it is
    computed here rather than left to the reader.
    """
    starts = [datetime.strptime(t, '%Y-%m-%d %H:%M:%S')
              for t, _, _, m in rows if m.startswith('=== CYCLE START')]
    durs = []
    for _, _, _, m in rows:
        mm = re.search(r'=== CYCLE END in ([\d.]+)s ===', m)
        if mm:
            durs.append(float(mm.group(1)))
    gaps = []
    for a, b in zip(starts, starts[1:]):
        mins = (b - a).total_seconds() / 60.0
        if mins > (cfg.MONITOR_INTERVAL_SEC / 60.0) * 2:
            gaps.append((a.strftime('%H:%M'), b.strftime('%H:%M'), round(mins)))
    return {'cycles': len(starts),
            'first': starts[0].strftime('%H:%M') if starts else None,
            'last': starts[-1].strftime('%H:%M') if starts else None,
            'median_sec': round(statistics.median(durs), 1) if durs else None,
            'max_sec': round(max(durs), 1) if durs else None,
            'gaps': gaps}


def _funnel(rows: List[tuple]) -> dict:
    """The scan funnel — with `raw` reported PER CYCLE, not summed.

    The scanner runs every cycle over broadly the same Chartink output, so
    totalling it across 62 cycles produced "3204 raw" for what is really ~51
    symbols looked at 62 times. A number that large reads like throughput and
    is actually double-counting. `added` IS cumulative — each one is a distinct
    new signal — so the two are reported on their own terms.
    """
    raws, added = [], 0
    skipped: Counter = Counter()
    for _, _, _, m in rows:
        # `\S+` for the arrow: the scanner writes a unicode '→', but a console
        # or a locale change can turn it into '->' and a single-char match
        # would then silently count nothing at all.
        mm = re.search(r'Scanner: (\d+) raw \S+ (\d+) added \| skipped: (.*)', m)
        if mm:
            raws.append(int(mm.group(1)))
            added += int(mm.group(2))
            for part in mm.group(3).split(','):
                if '=' in part:
                    k, v = part.strip().rsplit('=', 1)
                    try:
                        skipped[k] += int(v)
                    except ValueError:
                        pass
    return {'raw_per_cycle': round(statistics.median(raws)) if raws else 0,
            'scans': len(raws), 'added': added,
            'skipped_per_cycle': {k: round(v / max(1, len(raws)))
                                  for k, v in skipped.items()}}


def _vetting(rows: List[tuple], day: str) -> dict:
    ev: Counter = Counter()
    blocks = []
    for t, lvl, name, m in rows:
        if name != 'zebra.vet':
            continue
        for key, pat in (('requested', 'VET REQUESTED'),
                         ('spawned', 'CLI spawned'),
                         ('retried', 'VET RETRY'),
                         ('queued', 'VET QUEUED'),
                         ('timed_out', 'VET TIMED OUT'),
                         ('starved', 'VET STARVED'),
                         ('blocked', 'VET BLOCKED'),
                         ('abandoned', 'VET ABANDONED'),
                         ('exit_requested', 'EXIT VET REQUESTED'),
                         ('exit_timed_out', 'EXIT VET TIMED OUT')):
            if m.startswith(pat):
                ev[key] += 1
        if m.startswith('CLI BLOCKED'):
            blocks.append(t[11:16])
    # Transcripts: how many agents actually produced reasoning, and how many
    # were refusals. A spawn is not proof of work landing.
    tiny = total = 0
    try:
        for p in cfg.LOG_DIR.glob(f"vet_cli_{day.replace('-', '')}_*.log"):
            total += 1
            if p.stat().st_size < 700:
                tiny += 1
    except Exception:
        pass
    return {'events': dict(ev), 'blocks_at': blocks,
            'transcripts': total, 'transcripts_tiny': tiny}


def _trades(store_rows: List[dict], day: str) -> dict:
    opened = [t for t in store_rows if t.get('entry_date') == day]
    closed = [t for t in store_rows if t.get('exit_date') == day]
    open_now = [t for t in store_rows if t.get('status') == 'entered']
    cancelled = [t for t in store_rows
                 if t.get('status') == 'cancelled'
                 and str(t.get('cancelled_at') or '')[:10] == day]
    reasons: Counter = Counter()
    for t in cancelled:
        r = str(t.get('cancel_reason') or 'unknown')
        reasons[_NUM.sub('N', r).split(':')[0].strip()] += 1
    return {'opened': opened, 'closed': closed, 'open_now': open_now,
            'cancelled': len(cancelled), 'cancel_reasons': dict(reasons)}


def _cohort(store_rows: List[dict], day: Optional[str] = None) -> dict:
    """Everything since `cohort_start` and up to `day` — the only book that counts.

    **`day` bounds the running total, and it has to.** This used to read the
    whole store, which is right for the same-day run (the normal path) and a
    LOOK-AHEAD for any other. Backfilling 08-19's digest today would have
    printed "7 closed, net Rs 30,569" — six of those closed on 08-24, 08-26 and
    08-28, i.e. AFTER the day the file claims to describe. The digest is the
    paper run's dated record; a dated record that quietly contains the future
    is the mistake `feedback_measure_as_of_the_decision_date` is about, and it
    would have arrived the moment a missing session's log was recovered (M18).

    A record whose `exit_date` cannot be parsed cannot be placed in time, so it
    is EXCLUDED and counted in `undated` rather than silently folded into a
    total it may not belong to.
    """
    from .trade_store import in_cohort
    cand = [t for t in store_rows
            if t.get('status') == 'exited' and in_cohort(t)
            and t.get('pnl') is not None]
    ex, undated = [], 0
    for t in cand:
        if day is None:
            ex.append(t)
            continue
        raw = str(t.get('exit_date') or '')[:10]
        try:
            datetime.strptime(raw, '%Y-%m-%d')
        except ValueError:
            undated += 1
            continue
        if raw <= day:            # ISO dates compare correctly as strings
            ex.append(t)
    gross = sum(float(t['pnl']) for t in ex)
    net = sum(float(t.get('pnl_net', t['pnl'])) for t in ex)
    basis: Counter = Counter(
        (t.get('fees') or {}).get('basis', 'unstamped') for t in ex)
    wins = sum(1 for t in ex if float(t.get('pnl_net', t['pnl'])) > 0)
    # ARMING GATE. TP is a SPOT trigger; the stop reasons run through the
    # book-reading machinery -- debit SL, trail, spot veto, intrinsic floor,
    # reliability gate, time SL -- which is the half both real-money losses
    # came from. A cohort of pure TPs is no evidence about it at all.
    #
    # This is an ALLOWLIST (`outcomes.is_stop_exit`), never a `!= 'tp'`. The
    # not-equal test counted every string it did not recognise as a stop, and
    # it recognised almost nothing: `already_flat_tp` -- a TAKE-PROFIT the
    # monitor found already flat at the broker -- cleared the gate, as did all
    # five of the order path's own reason strings. Worse, already-flat is
    # exactly what arming exits against paper positions produces, so the gate
    # would have been cleared by the mistake it exists to prevent.
    #
    # Unrecognised reasons count for NOTHING and are reported separately, so
    # the vocabulary cannot drift quietly a second time.
    from .outcomes import classify, is_stop_exit
    reasons: Counter = Counter()          # canonical kind -> count
    stop_kinds: Counter = Counter()       # only those that COUNT at the gate
    recovered: Counter = Counter()        # already-flat bookings, by kind
    unrecognised: Counter = Counter()     # raw string -> count
    for t in ex:
        c = classify(t.get('exit_reason'))
        key = c['kind'] or 'unrecognised'
        reasons[key] += 1
        if not c['known']:
            unrecognised[c['raw'] or '(empty)'] += 1
        if c['recovered']:
            recovered[key] += 1
        if is_stop_exit(t.get('exit_reason')):
            stop_kinds[key] += 1
    stop_exits = sum(stop_kinds.values())
    # N14 — how much of this total is a figure the producer flagged as
    # inexact. A bridged close that found one leg already flat counts it at
    # 0.00, so the P&L is wrong in a KNOWN direction; folding it silently into
    # the cohort's running net presents it as a measurement. Counted, never
    # excluded: it is still the best number available and dropping it would
    # understate the book instead.
    approx = sum(1 for t in ex if t.get('exit_approximate') is True
                 or (t.get('exit') or {}).get('pnl_approximate') is True)
    return {'closed': len(ex), 'wins': wins, 'undated': undated,
            'approximate': approx,
            'exit_reasons': dict(reasons), 'stop_exits': stop_exits,
            'stop_exit_kinds': dict(stop_kinds),
            'recovered_exits': dict(recovered),
            'unrecognised_exit_reasons': dict(unrecognised),
            'gross': round(gross, 0), 'net': round(net, 0),
            'median_net_pct': (round(statistics.median(
                [float(t['pnl_net_pct']) for t in ex
                 if t.get('pnl_net_pct') is not None]), 2)
                if any(t.get('pnl_net_pct') is not None for t in ex) else None),
            'fee_basis': dict(basis)}


def _warnings(rows: List[tuple]) -> dict:
    c: Counter = Counter()
    for _, lvl, name, m in rows:
        if lvl in ('WARNING', 'ERROR', 'CRITICAL'):
            c[f"{lvl} {name}: {_NUM.sub('N', m)[:70]}"] += 1
    return dict(c.most_common(12))


#: Config switches that make a stop kind UNREACHABLE. A kind listed here with a
#: falsey switch can never produce the cohort evidence the arming gate waits
#: for, so naming it in the gate's message without saying so invites exactly
#: the wait-for-evidence-that-cannot-arrive mistake. Keyed by the canonical
#: `outcomes` kind, valued by the `cfg` attribute that gates it.
_STOP_KIND_SWITCH = {'spot_sl': 'SPOT_SL_ENABLED'}


def _stop_path_desc() -> str:
    """The stop kinds, derived from `outcomes.STOP_KINDS`, disabled ones marked.

    Derived, never spelled out. The old hardcoded literal
    ``"debit_sl / spot_sl / trail / time / expiry"`` had already drifted twice
    over: it is the vocabulary's job to say what the kinds ARE (it gained
    `expiry` without this string noticing), and config's job to say which can
    fire. `spot_sl` has been disabled since the 2026-08-12 measurement
    (`spot_sl_enabled: False` — spot is a VETO, not a trigger), so listing it
    unqualified told the reader to wait for evidence the code cannot generate.
    """
    from .outcomes import STOP_KINDS
    parts = []
    for kind in sorted(STOP_KINDS):
        switch = _STOP_KIND_SWITCH.get(kind)
        if switch and not getattr(cfg, switch, False):
            parts.append(f'{kind} [disabled]')
        else:
            parts.append(kind)
    return ' / '.join(parts)


def _flags(cyc, vet, tr, warn, coh, prev: Optional[dict],
           eng: Optional[dict] = None, tpl: Optional[dict] = None) -> List[str]:
    """Facts that earn a look. Each one names what happened, never why.

    `eng` (the order engine's failure record) and `tpl` (the TP latch's) both
    default to None so that a caller which knows nothing about them — every
    older call site and test — keeps working. Neither is optional in `build`.
    """
    out = []
    for a, b, mins in cyc['gaps']:
        out.append(f"cycle gap {mins}m ({a}->{b}) — positions unmonitored")
    # The ORDER ENGINE's failures come first among the counted ones: a failed
    # close outranks a warning-line tally, and until 2026-08-28 none of them
    # reached this list at all.
    if eng:
        out.extend(engine_log.flags(eng))
    errs = sum(v for k, v in warn.items() if k.startswith('ERROR')
               or k.startswith('CRITICAL'))
    if errs:
        out.append(f"{errs} ERROR/CRITICAL line(s) in the log")
    if vet['events'].get('starved'):
        out.append(f"{vet['events']['starved']} entry vet(s) STARVED — "
                   f"signals dropped for want of a verdict")
    if vet['blocks_at']:
        out.append(f"agent refused on quota at {', '.join(vet['blocks_at'])}")
    if vet['transcripts_tiny']:
        out.append(f"{vet['transcripts_tiny']} of {vet['transcripts']} agent "
                   f"transcript(s) produced almost no output")
    if vet['events'].get('exit_timed_out'):
        out.append(f"{vet['events']['exit_timed_out']} exit(s) fired on the "
                   f"deterministic guards alone")
    # The arming gate, reported every single digest so it cannot be forgotten
    # rather than met. Owner's decision 2026-08-26: do not arm the exit path
    # until the cohort has produced real STOP exits in paper.
    if coh['closed'] and not coh.get('stop_exits'):
        out.append(f"ARMING GATE UNMET: none of the {coh['closed']} cohort "
                   f"exit(s) is a transacted stop. The stop path "
                   f"({_stop_path_desc()}) has NO cohort evidence "
                   f"— do not arm.")
    elif coh.get('stop_exits'):
        kinds = ', '.join(f'{k} x{v}' for k, v in
                          sorted((coh.get('stop_exit_kinds') or {}).items()))
        out.append(f"arming gate: {coh['stop_exits']} cohort stop exit(s) so "
                   f"far ({kinds}) — read their books before arming.")
    # Vocabulary drift is what let a take-profit clear the gate. It must never
    # again be able to happen without a line in this list.
    unrec = coh.get('unrecognised_exit_reasons') or {}
    if unrec:
        out.append(f"{sum(unrec.values())} cohort exit(s) carry an "
                   f"UNRECOGNISED reason ({', '.join('%s x%d' % (k, v) for k, v in sorted(unrec.items()))}) "
                   f"— they count for NOTHING at the arming gate until "
                   f"zebra/outcomes.py names them")
    rec = coh.get('recovered_exits') or {}
    if rec:
        out.append(f"{sum(rec.values())} cohort exit(s) booked ALREADY-FLAT "
                   f"({', '.join('%s x%d' % (k, v) for k, v in sorted(rec.items()))}) "
                   f"— no close machinery ran and the price was recovered from "
                   f"order history, not transacted. They do NOT count as stop "
                   f"evidence. A RUN of these is the signature of exits armed "
                   f"against paper positions.")

    # THE TP LATCH. Both lines are the answer to a question the owner has open,
    # so they are stated when there is data and SILENT when there is none —
    # never "0 expired", which would read as the same-day bound having been
    # tested and found free when it may simply never have armed.
    if tpl and tpl.get('expired'):
        exp = tpl['expired']
        names = ', '.join('#%s %s' % (e['id'], e['stock']) for e in exp[:5])
        more = '' if len(exp) <= 5 else f" +{len(exp) - 5} more"
        out.append(f"{len(exp)} TP latch(es) EXPIRED UNBOOKED ({names}{more}) "
                   f"— a touch reached the end of its session with no close. "
                   f"This is the only price the same-day bound charges; read "
                   f"it against the exits an unbounded latch would have booked "
                   f"days late before deciding the rule is right.")
    m = (tpl or {}).get('measured') or {}
    if m.get('n'):
        w = m['worst_sec']
        line = (f"TP touch→fill over {m['n']} exit(s): median "
                f"{m['median_sec']:.0f}s, worst {m['max_sec']:.0f}s "
                f"(#{w['id']} {w['stock']})")
        if m.get('n_move'):
            line += (f"; spot gave back {m['gave_back']}/{m['n']}, median "
                     f"{m['median_adverse_pct']:+.2f}%, worst "
                     f"{m['max_adverse_pct']:+.2f}%")
        if m.get('giveback_rs_total') is not None:
            line += f"; Rs {m['giveback_rs_total']:,.0f} peak→booked"
        out.append(line + " — this is the M12 evidence: the lag in seconds "
                          "and what price did during it.")

    uncostable = coh['fee_basis'].get('brokerage_only', 0)
    if uncostable:
        out.append(f"{uncostable} cohort exit(s) could not be fully costed "
                   f"(no leg book) — net P&L is a FLOOR for those")
    for t in tr['closed']:
        g, n = t.get('pnl'), t.get('pnl_net')
        if g is not None and n is not None and g > 0 >= n:
            out.append(f"#{t['id']} {t['stock']}: charges turned a gross win "
                       f"into a net loss (Rs{g:.0f} -> Rs{n:.0f})")
        if str(t.get('exit_time') or '')[:5] < '09:30':
            out.append(f"#{t['id']} {t['stock']} exited before 09:30 — the "
                       f"window both real-money losses came from")
    if coh.get('approximate'):
        out.append(f"{coh['approximate']} of {coh['closed']} cohort exit(s) "
                   f"has an APPROXIMATE P&L — a leg was counted at 0.00, so "
                   f"the running net is wrong in a known direction")
    if prev:
        new = set(warn) - set(prev.get('warnings') or {})
        for k in sorted(new)[:4]:
            out.append(f"NEW today: {k}")
    return out


def build(day: Optional[str] = None) -> dict:
    """The whole digest as data. Never raises — a missing digest is an
    inconvenience, and it must not be able to matter more than that."""
    day = day or datetime.now(cfg.IST).strftime('%Y-%m-%d')
    rows = _read_log(day)
    try:
        from .trade_store import ZebraStore
        store = ZebraStore(config={})
        store._load_local()          # LOCAL ONLY: read-only, no Drive, no lock
        store_rows = store.load_trades()
    except Exception as e:
        logger.warning('digest could not read the store: %s', e)
        store_rows = []
    prev = None
    try:
        pp = cfg.LOG_DIR / 'eod' / f"{(datetime.strptime(day, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')}.json"
        if pp.exists():
            prev = json.loads(pp.read_text(encoding='utf-8'))
    except Exception:
        pass
    cyc, fun = _cycles(rows), _funnel(rows)
    vet, tr = _vetting(rows, day), _trades(store_rows, day)
    coh, warn = _cohort(store_rows, day), _warnings(rows)
    try:
        # WHOLE store, not the cohort: the latch fields can only exist on
        # records touched since 2026-08-28, so every one of them is in the
        # cohort already, and filtering would only add a way to lose one.
        tpl = tp_latch_evidence(store_rows, day)
    except Exception as e:                        # pragma: no cover - guard
        logger.warning('TP latch evidence failed: %s', e)
        tpl = {'touch_days': 0, 'armed_records': 0, 'expired': [],
               'measured': {'n': 0, 'rows': []}, 'has_data': False,
               'error': str(e)}
    try:
        eng = engine_log.analyse(day, rows)
    except Exception as e:                        # pragma: no cover - guard
        # Loud, not silent: a broken reader must not read as a clean day.
        logger.warning('engine log analysis failed: %s', e)
        eng = {'logs': [], 'events': [], 'uncatalogued': [],
               'uncatalogued_total': 0, 'unwatched': [], 'stalls': [],
               'mode': None,
               'problems': ['the order-engine log could not be analysed '
                            '(%s) — its failures are NOT covered today' % e]}
    return {'date': day, 'cycles': cyc, 'funnel': fun, 'vetting': vet,
            'trades': {k: (v if not isinstance(v, list) else len(v))
                       for k, v in tr.items()},
            'cohort': coh, 'warnings': warn, 'engines': eng, 'tp_latch': tpl,
            'flags': _flags(cyc, vet, tr, warn, coh, prev, eng, tpl),
            '_detail': tr}


def _render_tp_latch(tpl: dict) -> List[str]:
    """The TP latch section. Two open owner questions, one block.

    Rendered EVERY day, including the days it has nothing — an absent section
    reads as an oversight, and "no touch has been recorded" is itself the
    answer to question 1 for as long as it holds. What it must never do is
    render absence as zero: a bound that has never been tested and a bound
    that has been tested and cost nothing are opposite findings.
    """
    L: List[str] = ['## TP latch']
    if not tpl:
        L.append('_not computed_')
        return L
    if not tpl.get('has_data'):
        L.append("_**no data** — no touch has been recorded on any record "
                 "yet, so neither question this evidence exists for can be "
                 "answered from this book. Not the same as zero: the latch "
                 "shipped 2026-08-28 and every exit booked before that "
                 "carries none of its fields._")
        return L
    m = tpl.get('measured') or {}
    exp = tpl.get('expired') or []
    # `touch_days` counts every distinct day a latch armed on, ever;
    # `armed_records` counts the records still carrying a live stamp. They are
    # different numbers and named as such — a lapsed touch keeps its evidence
    # entry and loses its stamp, so the two diverge the moment one expires.
    L.append(f"**Armed** {tpl['touch_days']} touch-day(s) recorded; "
             f"{tpl['armed_records']} record(s) carry a stamp"
             + (f", {tpl['armed_today']} armed today"
                if tpl.get('armed_today') else ''))
    # Q2 — is M12 worth building? Time first, then what price did during it.
    if not m.get('n'):
        L.append("**Touch→fill** _no data — no exit has booked through a "
                 "latch yet (an exit that fires and books inside one "
                 "observation has no gap to report)_")
    else:
        w = m['worst_sec']
        L.append(f"**Touch→fill** {m['n']} exit(s): median "
                 f"{m['median_sec']:.0f}s, worst {m['max_sec']:.0f}s "
                 f"(#{w['id']} {w['stock']})")
        if not m.get('n_move'):
            L.append("**Spot give-back** _no data — no measured exit carries "
                     "a signed move_")
        else:
            wm = m['worst_move']
            L.append(f"**Spot give-back** {m['gave_back']} of {m['n']} gave "
                     f"back; median {m['median_adverse_pct']:+.2f}%, worst "
                     f"{m['max_adverse_pct']:+.2f}% (#{wm['id']} {wm['stock']}) "
                     f"— positive is AGAINST the position")
        if m.get('giveback_rs_total') is not None:
            wr = m['worst_rs']
            L.append(f"**Value peak→booked** Rs {m['giveback_rs_total']:,.0f} "
                     f"total, worst Rs {m['max_giveback_rs']:,.0f} "
                     f"(#{wr['id']} {wr['stock']}) — structure mid at its peak "
                     f"against the mid it booked at, counted only where the "
                     f"peak came at or after the touch")
        L.append('')
        L.append('| # | stock | exit | touch→fill | spot | gave back | Rs peak→booked |')
        L.append('|---|---|---|---|---|---|---|')
        for r in sorted(m['rows'], key=lambda x: -x['sec']):
            adv = ('—' if r['adverse_pct'] is None
                   else f"{r['adverse_pct']:+.2f}%")
            gb = {True: 'yes', False: 'no'}.get(r['gave_back'], '—')
            rs = ('—' if r['giveback_rs'] is None
                  else f"Rs{r['giveback_rs']:,.0f}")
            L.append(f"| {r['id']} | {r['stock']} | {r['exit_date'] or '—'} | "
                     f"{r['sec']:.0f}s | {adv} | {gb} | {rs} |")
    # Q1 — is same-day the right bound? Every lapse, named.
    L.append('')
    if not exp:
        L.append(f"**Expired unbooked** 0 of {tpl['touch_days']} touch-day(s) "
                 f"— every touch so far booked inside its own session, so the "
                 f"same-day bound has cost nothing yet")
    else:
        L.append(f"**Expired unbooked** {len(exp)} of {tpl['touch_days']} "
                 f"touch-day(s)"
                 + (f", {tpl['expired_today']} today" if tpl.get('expired_today')
                    else '') + ' — the price of the same-day bound:')
        for e in exp:
            L.append(f"- #{e['id']} {e['stock']} ({e['status']}): touched "
                     f"{e['touched_at']} at spot {e['touch_spot']} "
                     f"(tp {e['tp_spot']}), lapsed by {e['noticed_at']}")
    return L


def render(d: dict) -> str:
    """Markdown, dense, built to be pasted into a chat rather than skimmed."""
    L: List[str] = []
    A = L.append
    tr, coh, cyc, vet = d['_detail'], d['cohort'], d['cycles'], d['vetting']
    A(f"# Zebra digest — {d['date']}")
    A('')
    A(f"**Cycles** {cyc['cycles']} ({cyc['first']}–{cyc['last']}), "
      f"median {cyc['median_sec']}s, max {cyc['max_sec']}s"
      + (f", **{len(cyc['gaps'])} gap(s)**" if cyc['gaps'] else ''))
    f = d['funnel']
    A(f"**Scan** ~{f['raw_per_cycle']} raw/cycle over {f['scans']} scans "
      f"→ **{f['added']} new signal(s)**"
      + (f"  (skipped/cycle: {', '.join('%s %d' % (k, v) for k, v in sorted(f['skipped_per_cycle'].items(), key=lambda x: -x[1])[:4])})"
         if f['skipped_per_cycle'] else ''))
    A(f"**Vet** " + (', '.join(f"{k} {v}" for k, v in sorted(vet['events'].items()))
                     or 'nothing requested')
      + f"  |  transcripts {vet['transcripts']}"
      + (f" ({vet['transcripts_tiny']} near-empty)" if vet['transcripts_tiny'] else ''))
    A(f"**Cancelled** {tr['cancelled']}"
      + (f"  ({', '.join('%s %d' % (k, v) for k, v in tr['cancel_reasons'].items())})"
         if tr['cancel_reasons'] else ''))
    A('')
    if not tr['opened']:
        # STATED, never omitted. An absent section reads as an oversight, and
        # "no entries today" is a fact the paper run needs — the whole point of
        # the two months is to accumulate a cohort, so a day that vetted
        # signals and entered none is the thing to notice, not to hide.
        A(f"## Opened\n_none — {vet['events'].get('requested', 0)} vet(s) "
          f"requested, {tr['cancelled']} signal(s) cancelled_\n")
    if tr['opened']:
        A('## Opened')
        A('| # | stock | dir | dte | debit | capital | rate | days | room |')
        A('|---|---|---|---|---|---|---|---|---|')
        for t in tr['opened']:
            a = (((t.get('vet') or {}).get('context') or {}).get('st_attraction')) or {}
            o = a.get('overall') or {}
            # `.get(k, default)` returns None when the KEY EXISTS holding None,
            # and a symbol whose only departure is still running now has
            # exactly that — it would print "None%". Not `or '-'` either: that
            # swallows a real 0.0% rate, which is a fact worth reading.
            rate = o.get('touch_rate_pct')
            rate = '-' if rate is None else '%s%%' % rate
            A(f"| {t['id']} | {t['stock']} | {t.get('direction')} | "
              f"{t.get('dte_at_entry', '—')} | {t.get('debit')} | "
              f"Rs{t.get('capital', 0):,.0f} | {rate} | "
              f"{a.get('median_days_to_touch', '—')} | "
              f"{a.get('sessions_of_room', '—')} |")
        A('')
    if not tr['closed']:
        A('## Closed\n_none_\n')
    if tr['closed']:
        A('## Closed')
        A('| # | stock | reason | gross | net | costing |')
        A('|---|---|---|---|---|---|')
        for t in tr['closed']:
            fee = t.get('fees') or {}
            A(f"| {t['id']} | {t['stock']} | "
              f"{reason_label(t.get('exit_reason'))} | "
              f"Rs{t.get('pnl', 0):,.0f} ({t.get('pnl_pct', 0):+.1f}%) | "
              f"Rs{t.get('pnl_net', t.get('pnl', 0)):,.0f} | "
              f"{fee.get('basis', 'unstamped')} |")
        A('')
    A(f"**Open at close** {len(tr['open_now'])} positions, "
      f"Rs{sum(float(t.get('capital') or 0) for t in tr['open_now']):,.0f} deployed")
    A(f"**Cohort to date** {coh['closed']} closed, {coh['wins']} net-positive, "
      f"gross Rs{coh['gross']:,.0f} → **net Rs{coh['net']:,.0f}**"
      + (f", median {coh['median_net_pct']:+.2f}%" if coh['median_net_pct'] is not None else '')
      + (f"  [{', '.join('%s %d' % (k, v) for k, v in coh['fee_basis'].items())}]"
         if coh['fee_basis'] else '')
      + (f"  ⚠ {coh['approximate']} APPROXIMATE" if coh.get('approximate')
         else ''))
    if coh.get('undated'):
        A(f"_{coh['undated']} exited cohort record(s) carry no usable "
          f"`exit_date` and are excluded from that total — they cannot be "
          f"placed in time._")
    A('')
    L.extend(_render_tp_latch(d.get('tp_latch') or {}))
    A('')
    # The order engine. Rendered from its own module so the vocabulary and its
    # presentation stay in one place — this section exists because the digest
    # was blind to the process that places orders.
    if d.get('engines'):
        L.extend(engine_log.render(d['engines']))
        A('')
    if d['flags']:
        A('## ⚑ Earns a look')
        for x in d['flags']:
            A(f"- {x}")
    else:
        A('## ⚑ Earns a look')
        A('- nothing flagged')
    if d['warnings']:
        A('')
        A('<details><summary>warning classes</summary>')
        A('')
        for k, v in d['warnings'].items():
            A(f"- `{v}×` {k}")
        A('')
        A('</details>')
    return '\n'.join(L)


def write(day: Optional[str] = None) -> Path:
    """Build, render, persist both forms. Returns the markdown path."""
    d = build(day)
    out = cfg.LOG_DIR / 'eod'
    out.mkdir(parents=True, exist_ok=True)
    md = out / f"{d['date']}.md"
    md.write_text(render(d), encoding='utf-8')
    slim = {k: v for k, v in d.items() if k != '_detail'}
    (out / f"{d['date']}.json").write_text(
        json.dumps(slim, indent=1, default=str), encoding='utf-8')
    _merge_flags(out / 'FLAGS.md', d['date'], d['flags'])
    return md


def _merge_flags(path: Path, day: str, flags: List[str]) -> None:
    """The running triage list — one section per day, REPLACED not appended.

    A plain append duplicated the whole day every time the digest was re-run,
    which is guaranteed during the manual first week: the list that exists to
    stop observations dropping would instead fill with copies of them.

    Ticks are preserved across a re-run. The digest may re-state a fact; it
    does not get to un-triage something a human has already dealt with.
    """
    try:
        old = path.read_text(encoding='utf-8') if path.exists() else ''
    except Exception:                            # pragma: no cover - IO guard
        old = ''
    done = {ln.split(']', 1)[1].strip() for ln in old.splitlines()
            if ln.startswith('- [x]')}
    kept, skip = [], False
    for ln in old.splitlines():
        if ln.startswith('## '):
            skip = ln[3:].strip() == day
        if not skip:
            kept.append(ln)
    if flags:
        kept.append(f"\n## {day}")
        for x in flags:
            kept.append(f"- [{'x' if x in done else ' '}] {x}")
    text = '\n'.join(kept).strip()
    try:
        path.write_text((text + '\n') if text else '', encoding='utf-8')
    except Exception as e:                       # pragma: no cover - IO guard
        logger.warning('could not update %s: %s', path, e)
