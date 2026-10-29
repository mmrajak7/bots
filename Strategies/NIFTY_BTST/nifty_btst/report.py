"""Read-only scorecards over the paper book."""
from __future__ import annotations

from collections import OrderedDict
from typing import Dict, Iterable, List


def _stats(trades: List[dict], field: str) -> Dict[str, float]:
    vals = [t[field] for t in trades]
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v <= 0]
    gl = -sum(losses)
    return {
        'n': len(vals),
        'total': round(sum(vals), 2),
        'avg': round(sum(vals) / len(vals), 2) if vals else 0.0,
        'win_pct': round(100.0 * len(wins) / len(vals), 1) if vals else 0.0,
        'pf': round(sum(wins) / gl, 2) if gl > 0 else float('inf') if wins else 0.0,
    }


def group(trades: Iterable[dict], key: str, field: str = 'net_pnl') -> 'OrderedDict[str, dict]':
    g: Dict[str, List[dict]] = {}
    for t in trades:
        g.setdefault(str(t.get(key)), []).append(t)
    return OrderedDict((k, _stats(v, field)) for k, v in sorted(g.items()))


def render(closed: List[dict], open_: List[dict]) -> str:
    out = []
    if not closed:
        out.append('No closed paper trades yet.')
    else:
        for field, label in (('net_pnl', 'NET (after brokerage)'),
                             ('net_ex_brokerage', 'Volrix basis (charges, no brokerage)')):
            s = _stats(closed, field)
            out.append('%s: n %d  total Rs %+.0f  avg %+.0f  win %.1f%%  PF %s'
                       % (label, s['n'], s['total'], s['avg'], s['win_pct'], s['pf']))
        for key in ('cls', 'side', 'exit_reason', 'dte_sessions'):
            out.append('\nby %s (net):' % key)
            for k, s in group(closed, key).items():
                out.append('  %-18s n %3d  total %+9.0f  avg %+7.0f  win %5.1f%%  PF %s'
                           % (k, s['n'], s['total'], s['avg'], s['win_pct'], s['pf']))
        slips = [t['slip_vs_trigger'] for t in closed + open_ if 'slip_vs_trigger' in t]
        if slips:
            slips.sort()
            out.append('\nentry fill - trigger (Rs/share): median %+.2f  p90 %+.2f  n %d'
                       % (slips[len(slips) // 2], slips[int(0.9 * (len(slips) - 1))],
                          len(slips)))
        flagged = [t for t in closed if t['exit_reason'] in ('next_day_late', 'expired_settled')]
        if flagged:
            out.append('\n!! %d exits were LATE or SETTLED (box missed the 09:25 exit): %s'
                       % (len(flagged), ', '.join('#%d' % t['id'] for t in flagged)))
    if open_:
        out.append('\n' + render_open(open_))
    return '\n'.join(out)


def render_open(open_: List[dict]) -> str:
    if not open_:
        return ''
    lines = ['OPEN:']
    for p in open_:
        lines.append('  #%d %s x%d entry %.2f stop %.2f last %s (%s, entered %s)'
                     % (p['id'], p['symbol'], p['qty'], p['entry_price'],
                        p['stop_level'], p.get('last_ltp'), p['cls'], p['entry_date']))
    return '\n'.join(lines)
