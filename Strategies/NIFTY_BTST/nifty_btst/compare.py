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


class CompareError(ValueError):
    pass


def _norm(h: str) -> str:
    return re.sub(r'[^a-z&]', '', h.lower())


def _num(s: str) -> float:
    s = re.sub(r'[^\d.\-]', '', s.replace('−', '-'))
    if s in ('', '-', '.'):
        raise ValueError('not a number')
    return float(s)


def _date(s: str) -> date:
    s = s.strip().split('\n')[0]
    m = re.match(r'(\d{4}-\d{2}-\d{2})', s)
    if m:
        return date.fromisoformat(m.group(1))
    m = re.match(r'(\d{1,2} [A-Za-z]{3} \d{4})', s)
    if m:
        return datetime.strptime(m.group(1), '%d %b %Y').date()
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
