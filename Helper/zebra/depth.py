"""How many lots the touch could actually absorb — measured, per poll.

WHY THIS EXISTS
---------------
Owner, 2026-08-30: *"how many lots we can enter as we scale ensuring legs
intact"*, then *"let us collect oi and depth in coming weeks and decide"*.

The sizing question has three limits (`zebra/_research/capital_scaling.py`):
the budget, the `capital_per_lot` ladder, and LIQUIDITY. The first two are
arithmetic on numbers we choose. The third is a fact about the market, it is
the only one that decides whether "legs intact" survives scaling, and the book
had never recorded it — 13 cohort records, zero with depth on them.

Entry-side depth is now persisted at entry (`long_ask_qty_entry` /
`short_bid_qty_entry`). This module does the other half, and it is the half
that matters more: **the exit.**

WHY THE EXIT SIDE IS THE HARDER CONSTRAINT
------------------------------------------
An entry that cannot fill costs nothing — `entry_executor` gives up and the
signal is re-evaluated next cycle (`feedback_no_rush_to_enter`). A STOP that
cannot fill is unbounded, and this book has twice paid real money for exits
that went through a book that could not carry them.

Closing a bull call spread SELLS the long (so it needs size on the long's BID)
and BUYS BACK the short (size on the short's ASK) — the mirror of entry. The
binding side is whichever is thinner, exactly as `capital.liquidity_lots`
computes it for entry.

WHAT IS STORED, AND WHY IT IS A SAMPLED HISTOGRAM
-------------------------------------------------
A counter per lot bucket, not a list of readings. The store file is ~1MB and
this runs for every open position; a growing sample list would rewrite the
whole file every cycle.

The histogram is the shape the DECISION needs. "At 3 lots, what fraction of
the time could this position have been closed at the touch?" is one division.
A mean would hide the tail, and the tail is the whole question: a book that
carries 5 lots on 95% of readings and 0 on the other 5% is a book that will
strand a stop.

**SAMPLED every `SAMPLE_SEC`, not counted every poll**, for two reasons:

* the store must stay SILENT on a quiet cycle. `zebra.mfe` is deliberately
  built that way ("once peaks stop advancing the tracking must go completely
  silent") and a per-poll counter would rewrite the file every cycle forever,
  undoing the batching that exists to prevent exactly that;
* the two engines poll at wildly different rates — zebra every 5 minutes, the
  order path every 5 SECONDS — so poll counts are not comparable between them
  and would silently re-weight the moment exits arm. Time sampling makes the
  measurement a property of the market rather than of the cron line.

Evenly-spaced readings make the proportions unbiased, which a census of polls
would also be — it is the COST that differs, not the statistic.
"""
from __future__ import annotations

import time
from typing import Optional

#: Seconds between depth readings on one position. Depth moves on a minute
#: scale, so 15 minutes characterises a book perfectly well while costing ~26
#: store writes a session per position instead of one per poll. Independent of
#: the caller's cadence ON PURPOSE -- see the module docstring.
SAMPLE_SEC = 900

#: Lot counts the histogram answers for. 1 is the floor (can this position be
#: closed AT ALL at the touch?); 5 is `max_lots_hard`; 3 is where the fee-drag
#: curve flattens and is therefore the likeliest ceiling anyone would choose.
BUCKETS = (1, 2, 3, 5)

#: Record field. One dict per position, updated in place.
FIELD = 'exit_depth'


def exit_lots(legs: Optional[dict], lot_size) -> Optional[int]:
    """Lots the touch could absorb ON THE WAY OUT, or None if unknowable.

    The mirror of `capital.liquidity_lots`: closing sells the long (needs the
    long's BID size) and buys back the short (needs the short's ASK size).

    None rather than 0 when depth is absent. 0 is a finding — "the touch is
    empty" — and manufacturing it from a missing field would report measured
    illiquidity that was never measured. Every caller must keep the two
    apart, which is why this returns None and the histogram counts `unknown`
    separately.
    """
    if not legs or not lot_size:
        return None
    try:
        lot_size = int(lot_size)
        if lot_size <= 0:
            return None
        long_qty = (legs.get('long') or {}).get('bid_qty')
        short_qty = (legs.get('short') or {}).get('ask_qty')
    except (AttributeError, TypeError, ValueError):
        return None
    if long_qty is None or short_qty is None:
        return None
    try:
        long_qty, short_qty = int(long_qty), int(short_qty)
    except (TypeError, ValueError):
        return None
    if long_qty < 0 or short_qty < 0:
        return None
    return min(long_qty, short_qty) // lot_size


def observe(trade: dict, legs: Optional[dict],
            now: Optional[float] = None) -> dict:
    """This poll's contribution, or `{}` when it is not yet time to sample.

    Returns the PATCH to persist — the same contract `zebra.mfe.compute` has,
    so the caller folds it into the one batched store write per cycle rather
    than taking a write of its own. `{}` on a poll inside the sampling window,
    which is what keeps a quiet cycle silent.

    `now` is epoch seconds, timezone-free on both sides of the comparison, so
    the cross-clock mistake `ist_epoch` exists for cannot arise here.

    Never raises. This is measurement sitting in the exit path, and a
    measurement that can throw is a new way to fail an exit
    (`feedback_guard_the_money_system_first`).
    """
    try:
        now = time.time() if now is None else float(now)
        prev = trade.get(FIELD)
        cur = dict(prev) if isinstance(prev, dict) else {}
        last = cur.get('last_t')
        if last is not None and now - float(last) < SAMPLE_SEC:
            return {}
        cur.setdefault('samples', 0)
        cur.setdefault('unknown', 0)
        for n in BUCKETS:
            cur.setdefault('ge_%d' % n, 0)

        lots = exit_lots(legs, trade.get('lot_size'))
        cur['last_t'] = now
        cur['samples'] = int(cur['samples']) + 1
        if lots is None:
            # A reading with no depth is EVIDENCE, not a gap to skip: a book
            # that stops quoting size is exactly the book a stop cannot leave.
            # Counted separately so `ge_1/measured` never silently improves by
            # dropping the readings it could not take.
            cur['unknown'] = int(cur['unknown']) + 1
        else:
            for n in BUCKETS:
                if lots >= n:
                    cur['ge_%d' % n] = int(cur['ge_%d' % n]) + 1
            # The WORST reading, kept whole. The histogram answers "how
            # often"; this answers "how bad did it get", and a stop only has
            # to meet the worst book once. Sampling can MISS a worse moment
            # between readings, so read it as the best case, not the true
            # minimum.
            worst = cur.get('worst_lots')
            cur['worst_lots'] = lots if worst is None else min(int(worst), lots)
        return {FIELD: cur}
    except Exception:                       # pragma: no cover - never fatal
        return {}


def summary(trade: dict) -> Optional[dict]:
    """This position's depth record as proportions, or None if never measured.

    `measured` is samples MINUS unknown: every rate below is a share of the
    readings that could actually be taken, and `unknown_pct` is reported
    beside them so a position whose book went dark cannot look liquid by
    omission.
    """
    d = trade.get(FIELD)
    if not isinstance(d, dict) or not d.get('samples'):
        return None
    polls = int(d['samples'])
    unknown = int(d.get('unknown') or 0)
    measured = polls - unknown
    out = {'polls': polls, 'unknown': unknown, 'measured': measured,
           'unknown_pct': round(unknown / polls * 100, 1),
           'worst_lots': d.get('worst_lots')}
    for n in BUCKETS:
        c = int(d.get('ge_%d' % n) or 0)
        out['ge_%d' % n] = c
        out['ge_%d_pct' % n] = round(c / measured * 100, 1) if measured else None
    return out


def report(trades: list) -> str:
    """The table this whole exercise exists to produce.

    Per position and in aggregate: what fraction of readings could have closed
    the position at the touch, at each lot count. Aggregated over SAMPLES
    rather than over positions, because a position open for three weeks
    carries more evidence than one open for two days and averaging the
    percentages would weight them equally.
    """
    rows = [(t, summary(t)) for t in trades]
    rows = [(t, s) for t, s in rows if s]
    if not rows:
        return ('No depth measured yet. `zebra.depth.observe` samples every '
                '%d minutes on each open cohort position; the first reading '
                'lands on the next poll after entry.' % (SAMPLE_SEC // 60))

    lines = ['EXIT DEPTH — lots the touch could absorb on the way OUT',
             '(closing SELLS the long at its bid and BUYS BACK the short at '
             'its ask;', ' the thinner side binds)', '']
    head = '%-12s %-6s %-7s %-7s' % ('stock', 'reads', 'dark', 'worst')
    head += ''.join('%-9s' % ('>=%dlot' % n) for n in BUCKETS)
    lines.append(head)
    lines.append('-' * len(head))

    tot = {'polls': 0, 'unknown': 0, 'measured': 0}
    for n in BUCKETS:
        tot['ge_%d' % n] = 0
    worst_all = None
    for t, s in sorted(rows, key=lambda r: -r[1]['polls']):
        line = '%-12s %-6d %-7s %-7s' % (
            str(t.get('stock'))[:12], s['polls'], '%.0f%%' % s['unknown_pct'],
            '—' if s['worst_lots'] is None else s['worst_lots'])
        line += ''.join(
            '%-9s' % ('—' if s['ge_%d_pct' % n] is None
                      else '%.0f%%' % s['ge_%d_pct' % n]) for n in BUCKETS)
        lines.append(line)
        for k in ('polls', 'unknown', 'measured'):
            tot[k] += s[k]
        for n in BUCKETS:
            tot['ge_%d' % n] += s['ge_%d' % n]
        if s['worst_lots'] is not None:
            worst_all = (s['worst_lots'] if worst_all is None
                         else min(worst_all, s['worst_lots']))

    lines.append('-' * len(head))
    m = tot['measured']
    agg = '%-12s %-6d %-7s %-7s' % (
        'ALL', tot['polls'],
        '%.0f%%' % (tot['unknown'] / tot['polls'] * 100) if tot['polls'] else '—',
        '—' if worst_all is None else worst_all)
    agg += ''.join('%-9s' % ('—' if not m else '%.0f%%'
                             % (tot['ge_%d' % n] / m * 100)) for n in BUCKETS)
    lines.append(agg)
    lines.append('')
    lines.append('Read the WORST column, not only the percentages: a stop has '
                 'to meet the worst book once.')
    lines.append('`dark` is readings where the book quoted no size at all — '
                 'those are excluded from the')
    lines.append('percentages, so a position whose book went dark cannot look '
                 'liquid by omission.')
    return '\n'.join(lines)
