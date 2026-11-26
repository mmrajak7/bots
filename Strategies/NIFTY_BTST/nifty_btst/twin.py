"""Live (or rehearsal) fills against the PAPER twin, trade by trade.

Owner decision 2026-11-26: the Volrix pass mark is waived as a go-live
condition (`neo.pass_mark_waived`). What replaces it as the implementation
check is this report: paper runs the same rule on the same Kite quotes, so for
every trade both books took, the difference is pure execution:

    extra cost / share = (paper exit - paper entry) - (live exit - live entry)

Positive = live did worse than paper. Split into its entry part (live entry -
paper entry) and exit part (paper exit - live exit), because they have
different fixes (entry: the cap and the chase; exit: the opening settle and
the bid). A pair on a different strike is reported and kept, flagged: it is
still the same decision on the same day.

Read-only, never raises on a bad book: a report must not stop anything.
"""
from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple

Key = Tuple[str, str]          # (entry_date, side)


def load_positions(book: Path) -> List[Dict[str, Any]]:
    try:
        with open(book, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    pos = data.get('positions') if isinstance(data, dict) else None
    return [p for p in pos if isinstance(p, dict)] if isinstance(pos, list) else []


def _closed(positions: List[Dict[str, Any]]) -> Dict[Key, Dict[str, Any]]:
    """Closed, really-filled trades keyed by (entry_date, side). A book takes
    at most one trade per side per day; if a key repeats, the first wins and
    the rest are counted as duplicates by `pair`."""
    out: Dict[Key, Dict[str, Any]] = {}
    for p in positions:
        if not isinstance(p, dict) or p.get('status') != 'closed' or p.get('never_filled'):
            continue
        try:
            float(p['entry_price']), float(p['exit_price'])
        except (KeyError, TypeError, ValueError):
            continue
        out.setdefault((str(p.get('entry_date')), str(p.get('side'))), p)
    return out


def _flag(p: Dict[str, Any]) -> str:
    """Why a row's price is not a real broker fill, or ''."""
    if p.get('entry_price_estimated') or p.get('exit_price_estimated'):
        return 'estimated'
    if p.get('exit_reason') == 'closed_externally' or p.get('closed_externally'):
        return 'external'
    return ''


def pair(live: List[Dict[str, Any]], paper: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Never raises (R6-08): a bad row is skipped, counted in `skipped`."""
    lc, pc = _closed(live), _closed(paper)
    rows, skipped = [], 0
    for key in sorted(set(lc) & set(pc)):
        lv, pp = lc[key], pc[key]
        try:
            le, lx = float(lv['entry_price']), float(lv['exit_price'])
            pe, px = float(pp['entry_price']), float(pp['exit_price'])
            qty = int(float(lv.get('qty') or 0))
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        extra = (px - pe) - (lx - le)
        rows.append({
            'date': key[0], 'side': key[1],
            'same_strike': lv.get('symbol') == pp.get('symbol'),
            'live_symbol': lv.get('symbol'), 'paper_symbol': pp.get('symbol'),
            'live_entry': le, 'paper_entry': pe, 'live_exit': lx, 'paper_exit': px,
            'entry_cost': round(le - pe, 2), 'exit_cost': round(px - lx, 2),
            'extra_cost': round(extra, 2), 'extra_rs': round(extra * qty, 2),
            'live_reason': lv.get('exit_reason'), 'paper_reason': pp.get('exit_reason'),
            'flag': _flag(lv),
        })
    real = [r for r in rows if not r['flag']]            # estimated/owner prices stay out of the means
    same = [r for r in real if r['same_strike']]
    return {
        'pairs': rows, 'skipped': skipped, 'flagged': len(rows) - len(real),
        'mean_extra': round(mean(r['extra_cost'] for r in real), 2) if real else None,
        'mean_extra_same_strike': round(mean(r['extra_cost'] for r in same), 2) if same else None,
        'mean_entry': round(mean(r['entry_cost'] for r in real), 2) if real else None,
        'mean_exit': round(mean(r['exit_cost'] for r in real), 2) if real else None,
        'total_extra_rs': round(sum(r['extra_rs'] for r in real), 2),
        'live_only': sorted(set(lc) - set(pc)),
        'paper_only': sorted(set(pc) - set(lc)),
    }


def render(res: Dict[str, Any], label: str = 'LIVE') -> str:
    lines = ['%s vs PAPER: %d pairs; extra cost per share (+ = %s worse): mean %s '
             '(entry %s, exit %s); same strike only %s; total Rs %s'
             % (label, len(res['pairs']), label.lower(), res['mean_extra'], res['mean_entry'],
                res['mean_exit'], res['mean_extra_same_strike'], res['total_extra_rs'])]
    for r in res['pairs']:
        lines.append('  %s %s %s entry %.2f/%.2f exit %.2f/%.2f (%s/%s) -> entry %+.2f exit %+.2f '
                     'extra %+.2f = Rs %+.0f'
                     % (r['date'], r['side'], r['live_symbol'] if r['same_strike'] else
                        '%s vs paper %s [DIFFERENT STRIKE]' % (r['live_symbol'], r['paper_symbol']),
                        r['live_entry'], r['paper_entry'], r['live_exit'], r['paper_exit'],
                        r['live_reason'], r['paper_reason'], r['entry_cost'], r['exit_cost'],
                        r['extra_cost'], r['extra_rs'])
                     + (' [%s price: not in the means]' % r['flag'] if r['flag'] else ''))
    if res.get('skipped'):
        lines.append('  %d pair(s) skipped: unreadable price or qty' % res['skipped'])
    if res['live_only']:
        lines.append('  %s only (paper did not take it): %s'
                     % (label, ', '.join('%s %s' % k for k in res['live_only'])))
    if res['paper_only']:
        lines.append('  PAPER only (%s missed it): %s'
                     % (label.lower(), ', '.join('%s %s' % k for k in res['paper_only'])))
    return '\n'.join(lines)


def report(live_book: Path, paper_book: Path, label: str = 'LIVE',
           since: Optional[str] = None) -> str:
    try:
        live, paper = load_positions(live_book), load_positions(paper_book)
        if since:
            live = [p for p in live if str(p.get('entry_date')) >= since]
            paper = [p for p in paper if str(p.get('entry_date')) >= since]
        return render(pair(live, paper), label)
    except Exception as exc:                          # noqa: BLE001 - a report never stops anything
        return '%s vs PAPER: twin report unavailable (%s: %s)' % (label, type(exc).__name__, exc)
