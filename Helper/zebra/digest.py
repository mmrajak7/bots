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


def _cohort(store_rows: List[dict]) -> dict:
    """Everything since `cohort_start`, which is the only book that counts."""
    from .trade_store import in_cohort
    ex = [t for t in store_rows
          if t.get('status') == 'exited' and in_cohort(t)
          and t.get('pnl') is not None]
    gross = sum(float(t['pnl']) for t in ex)
    net = sum(float(t.get('pnl_net', t['pnl'])) for t in ex)
    basis: Counter = Counter(
        (t.get('fees') or {}).get('basis', 'unstamped') for t in ex)
    wins = sum(1 for t in ex if float(t.get('pnl_net', t['pnl'])) > 0)
    return {'closed': len(ex), 'wins': wins,
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


def _flags(cyc, vet, tr, warn, coh, prev: Optional[dict]) -> List[str]:
    """Facts that earn a look. Each one names what happened, never why."""
    out = []
    for a, b, mins in cyc['gaps']:
        out.append(f"cycle gap {mins}m ({a}->{b}) — positions unmonitored")
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
    coh, warn = _cohort(store_rows), _warnings(rows)
    return {'date': day, 'cycles': cyc, 'funnel': fun, 'vetting': vet,
            'trades': {k: (v if not isinstance(v, list) else len(v))
                       for k, v in tr.items()},
            'cohort': coh, 'warnings': warn,
            'flags': _flags(cyc, vet, tr, warn, coh, prev),
            '_detail': tr}


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
              f"{str(t.get('exit_reason', '')).replace('paper:', '')} | "
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
         if coh['fee_basis'] else ''))
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
