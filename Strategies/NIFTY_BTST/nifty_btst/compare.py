"""Pair paper trades with Volrix's backtest trades for the same days.

Volrix updates the strategy daily and offers "Download CSV" on its Trades tab
(downloaded by the owner, by hand). Each paper trade should have a twin there:
same entry date, same side. The per-pair gap in entry price, exit price and
P&L IS the implementation cost; it needs far fewer trades to measure than
the P&L itself does.

The CSV's exact header names were not available when this was written, so
columns are found by name ('entry date', 'instrument', 'entry price',
'exit price', 'pnl'), case- and punctuation-insensitive. A file without them
is refused with the headers it does have, never guessed at.

THE PASS MARK (owner, 2026-11-02) is `gate()`. It is fixed in advance and tests
IMPLEMENTATION only: can we get Volrix's trades at about Volrix's prices? It says
nothing about whether the edge is real, and a losing window does not fail it.
"""
from __future__ import annotations

import csv
import re
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

NEED = {
    'entry_date': ('entrydate', 'entry date'),
    'instrument': ('instrument', 'symbol'),
    'entry_price': ('entryprice', 'entry price'),
    'exit_price': ('exitprice', 'exit price'),
    'pnl': ('pnl', 'p&l', 'profit'),
}
OPTIONAL = {'remarks': ('remarks', 'remark')}

# The pass mark. Do not tune these once the window has started.
GATE_START = date(2026, 10, 30)   # first session the paper trader marked
GATE_PAIRS = 30                   # exactly the first 30 closed pairs, in entry order
GATE_MAX_EXTRA_COST = 2.0         # Rs/share, round trip, mean over those pairs
GATE_BIG_WIN = 10000.0            # Rs on 1 lot: a Volrix win this size must have a twin

# A Volrix row with these remarks has no genuine exit price: the last row of each
# update is the still-open trade marked at the end of the data.
NOT_AN_EXIT = ('end of backtest', 'error fetching')


class CompareError(ValueError):
    pass


def _norm(h: str) -> str:
    return re.sub(r'[^a-z&]', '', h.lower())


def _num(s: str) -> float:
    # A cell may be multi-line ("155.12\n13:00:00"); only the first line is the number.
    s = s.strip().split('\n')[0]
    s = re.sub(r'[^\d.\-]', '', s.replace('−', '-'))
    if s in ('', '-', '.'):
        raise ValueError('not a number')
    return float(s)


def _date(s: str) -> date:
    s = s.strip().split('\n')[0]
    m = re.match(r'(\d{4}-\d{2}-\d{2})', s)
    if m:
        return date.fromisoformat(m.group(1))
    # Volrix writes September as "Sept", which %b rejects: keep only the first 3 letters.
    m = re.match(r'(\d{1,2}) ([A-Za-z]{3})[A-Za-z]* (\d{4})', s)
    if m:
        return datetime.strptime(' '.join(m.groups()), '%d %b %Y').date()
    for fmt in ('%d-%m-%Y', '%d/%m/%Y', '%d-%b-%Y'):
        try:
            return datetime.strptime(s.split(' ')[0], fmt).date()
        except ValueError:
            continue
    raise ValueError('unparseable date %r' % s)


def _side(instrument: str) -> str:
    m = re.search(r'\b(CE|PE)\b', instrument.upper())
    if not m:
        raise ValueError('no CE/PE in %r' % instrument)
    return m.group(1)


def load_volrix(path: Path) -> List[dict]:
    with open(path, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.reader(f))
    if not rows:
        raise CompareError('%s is empty' % path)
    header = [_norm(h) for h in rows[0]]
    idx: Dict[str, int] = {}
    for key, names in NEED.items():
        for n in names:
            if _norm(n) in header:
                idx[key] = header.index(_norm(n))
                break
    missing = [k for k in NEED if k not in idx]
    if missing:
        raise CompareError('Volrix CSV lacks %s; its headers are: %s'
                           % (missing, rows[0]))
    for key, names in OPTIONAL.items():
        for n in names:
            if _norm(n) in header:
                idx[key] = header.index(_norm(n))
                break
    out = []
    for r in rows[1:]:
        if not any(c.strip() for c in r):
            continue
        out.append({
            'entry_date': _date(r[idx['entry_date']]),
            'side': _side(r[idx['instrument']]),
            'instrument': r[idx['instrument']].strip(),
            'entry_price': _num(r[idx['entry_price']]),
            'exit_price': _num(r[idx['exit_price']]) if r[idx['exit_price']].strip() else None,
            'pnl': _num(r[idx['pnl']]),
            'remarks': r[idx['remarks']].strip() if 'remarks' in idx else '',
        })
    return out


def pair(paper: List[dict], volrix: List[dict], since: Optional[date] = None
         ) -> Tuple[List[Tuple[dict, dict]], List[dict], List[dict]]:
    vk = {(v['entry_date'], v['side']): v for v in volrix}
    pk = {(date.fromisoformat(p['entry_date']), p['side']): p for p in paper}
    start = since or (min(pk)[0] if pk else None)
    if start is None:
        return [], [], []
    vk = {k: v for k, v in vk.items() if k[0] >= start}
    both = sorted(set(vk) & set(pk))
    return ([(pk[k], vk[k]) for k in both],
            [pk[k] for k in sorted(set(pk) - set(vk))],
            [vk[k] for k in sorted(set(vk) - set(pk))])


def render(pairs, paper_only, volrix_only) -> str:
    out = ['PAIRED %d | paper-only %d | volrix-only %d'
           % (len(pairs), len(paper_only), len(volrix_only))]
    gaps = []
    for p, v in pairs:
        ours = p.get('net_ex_brokerage')
        gap = None if ours is None else ours - v['pnl']
        if gap is not None:
            gaps.append(gap)
        out.append('  %s %s  entry %.2f vs %.2f  exit %s vs %s  pnl %s vs %+.0f  gap %s'
                   % (p['entry_date'], p['side'], p['entry_price'], v['entry_price'],
                      p.get('exit_price'), v['exit_price'],
                      '%+.0f' % ours if ours is not None else 'open', v['pnl'],
                      '%+.0f' % gap if gap is not None else '-'))
    if gaps:
        gaps.sort()
        mean = sum(gaps) / len(gaps)
        out.append('GAP (paper - Volrix, Volrix basis) per trade: mean %+.0f  median %+.0f  n %d'
                   % (mean, gaps[len(gaps) // 2], len(gaps)))
    for p in paper_only:
        out.append('  paper-only  %s %s %s (Volrix did not trade it)'
                   % (p['entry_date'], p['side'], p['symbol']))
    for v in volrix_only:
        out.append('  VOLRIX-ONLY %s %s %s pnl %+.0f (we did not trade it)'
                   % (v['entry_date'], v['side'], v['instrument'], v['pnl']))
    return '\n'.join(out)


def _strike(instrument: str) -> Optional[float]:
    m = re.search(r'([\d,]+(?:\.\d+)?)\s*(?:CE|PE)\b', instrument.upper())
    return float(m.group(1).replace(',', '')) if m else None


def _real_exit(v: dict) -> bool:
    remarks = v.get('remarks', '').lower()
    return v['exit_price'] is not None and not any(x in remarks for x in NOT_AN_EXIT)


def gate(paper: List[dict], volrix: List[dict]) -> dict:
    """Score the pass mark: FAIL / PASS / IN PROGRESS, on a population fixed by entry order.

    extra cost per share = (Volrix exit - entry) - (paper exit - entry); positive means
    paper did worse. A pair on a different strike or a different entry time stays in:
    that is an implementation difference, and dropping it would flatter the result.
    """
    pk = {(date.fromisoformat(p['entry_date']), p['side']): p for p in paper
          if date.fromisoformat(p['entry_date']) >= GATE_START}
    vk = {(v['entry_date'], v['side']): v for v in volrix if v['entry_date'] >= GATE_START}
    excluded = sorted(k for k, v in vk.items() if not _real_exit(v))
    keys = sorted(k for k in set(pk) & set(vk)
                  if k not in excluded and pk[k].get('exit_price') is not None)[:GATE_PAIRS]
    costs = [(vk[k]['exit_price'] - vk[k]['entry_price'])
             - (pk[k]['exit_price'] - pk[k]['entry_price']) for k in keys]
    mean = sum(costs) / len(costs) if costs else None
    full = len(keys) >= GATE_PAIRS
    # The window closes with the 30th pair; later Volrix days belong to no test.
    last = keys[-1][0] if full else None
    missed = sorted(k for k, v in vk.items()
                    if k not in pk and k not in excluded and v['pnl'] >= GATE_BIG_WIN
                    and (last is None or k[0] <= last))
    strike_diff = [k for k in keys
                   if pk[k].get('strike') != _strike(vk[k]['instrument'])]
    if missed:
        verdict = 'FAIL'
    elif not full:
        verdict = 'IN PROGRESS'
    elif mean is not None and mean > GATE_MAX_EXTRA_COST:
        verdict = 'FAIL'
    else:
        verdict = 'PASS'
    return {'verdict': verdict, 'pairs': len(keys), 'mean_cost': mean, 'missed': missed,
            'strike_diff': strike_diff, 'excluded': excluded}


def render_gate(g: dict) -> str:
    cost = '-' if g['mean_cost'] is None else '%+.2f' % g['mean_cost']
    out = ['PASS MARK: %s | pairs %d/%d | extra cost Rs %s/share (limit +%.2f) | '
           'big wins missed %d | different strike %d | Volrix rows without a real exit %d'
           % (g['verdict'], g['pairs'], GATE_PAIRS, cost, GATE_MAX_EXTRA_COST,
              len(g['missed']), len(g['strike_diff']), len(g['excluded']))]
    for d, side in g['missed']:
        out.append('  MISSED BIG WIN %s %s' % (d, side))
    out.append('  (tests fills and coverage only; it says nothing about the edge)')
    return '\n'.join(out)
