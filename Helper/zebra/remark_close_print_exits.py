"""Re-mark exits that were booked on the CLOSING AUCTION print.

## What went wrong

`monitor._is_market_open` tested `(h, m) <= MARKET_CLOSE`, so the 15:30 cron
cycle read as open and polled the closing print. Three cohort positions booked
`paper:tp` on it — TMPV #423 at 15:30:28, GMRAIRPORT #455 at 15:30:48 and
ADANIGREEN #471 at 15:31:08, all 2026-08-31. That print moved five names
-3.80%..+2.95% in ONE step while others sat flat, against a median 5-minute
move of 0.062% across all 9,255 other polls in the record. TMPV's book had not
even repriced: long 320 PE bid 8.90 against spot 308.85 is 2.25 BELOW
intrinsic. No live order fills there.

`monitor._exits_executable` stops it happening again. This repairs the rows it
already produced, because the paper book IS the evidence the arming gate is
waiting on, and a third of it came from prices nobody could have traded at.

## What it does

Finds every COHORT exit stamped at or after `MARKET_CLOSE`, looks up the last
poll in `logs/eod/paths_<exit_date>.json` that was both executable
(before the close) and usable (`q == 'ok'`), and re-books the exit there.
Everything downstream — `pnl`, `pnl_pct`, `fees`, `pnl_net`, `pnl_net_pct` —
is recomputed by the store's own `_apply_exit`, so this cannot drift from the
arithmetic the engine uses.

SAFE-TO-RERUN: after a row is re-marked its `exit_time` is before the close, so
it no longer matches and a second run finds nothing. It refuses to act on any
row whose corrected value it cannot READ from the path record, and it never
invents a price.

RETIRES WHEN: no cohort row has an exit stamped at or after the close — i.e.
this has been run once on every replica and `_exits_executable` has been live
for a full session.

## Running it

    python -m zebra.remark_close_print_exits            # DRY RUN, the default
    python -m zebra.remark_close_print_exits --apply

RUN IT ON THE PI TOO. This store syncs through Drive and `_merge` resolves by
VERSION, so a correction applied only on a laptop with `drive=disabled` never
reaches the box — and the box's higher-versioned row would win anyway. The
write bumps `version`, which is what lets the correction travel.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Optional

from . import config as cfg
from .trade_store import get_store, in_cohort

logger = logging.getLogger(__name__)


def _booked_after_close(trade: dict) -> bool:
    """Was this exit stamped at or after the close?"""
    t = trade.get('exit_time')
    if not t or trade.get('status') != 'exited':
        return False
    try:
        h, m = int(t[:2]), int(t[3:5])
    except (ValueError, IndexError):
        return False
    return (h, m) >= cfg.MARKET_CLOSE


def last_executable_poll(trade_id: int, date: str) -> Optional[dict]:
    """The last poll of `date` that a close could actually have been filled on.

    Both conditions, not one. `q == 'ok'` alone would happily return the 15:30
    print, which is the whole defect; before-the-close alone would return a
    garbage book. Returns None rather than a guess when the day is not on disk
    — a path file that was never captured cannot be reconstructed, and inventing
    a price to repair a row about invented prices would be its own joke.
    """
    path = os.path.join(str(cfg.LOG_DIR), 'eod', 'paths_%s.json' % date)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as fh:
            day = json.load(fh)
    except (OSError, ValueError) as e:
        logger.error('cannot read %s: %s', path, e)
        return None
    rec = (day.get('trades') or {}).get(str(trade_id))
    if not rec:
        return None
    usable = [o for o in rec.get('obs', [])
              if o.get('q') == 'ok'
              and o.get('val') is not None
              and o.get('spot') is not None
              and o.get('ts', '')[11:16] < '%02d:%02d' % cfg.MARKET_CLOSE]
    return max(usable, key=lambda o: o['ts']) if usable else None


def plan(store) -> list:
    """(trade, corrected_poll_or_None) for every row booked after the close."""
    out = []
    for t in store.load_trades():
        if in_cohort(t) and _booked_after_close(t):
            out.append((t, last_executable_poll(t['id'], t.get('exit_date'))))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--apply', action='store_true',
                    help='actually re-mark (default: show what would change)')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    store = get_store()
    rows = plan(store)
    if not rows:
        print('\nNothing booked at or after the %02d:%02d close. Clean.\n'
              % cfg.MARKET_CLOSE)
        return 0

    print()
    print('EXITS BOOKED ON A PRINT NOBODY COULD TRADE AT')
    print()
    hdr = (f"{'id':>4} {'stock':<12}{'exit':<20}{'booked':>9}{'->':^4}"
           f"{'executable':>11}  {'at':<10}{'pnl':>10}{'->':^4}{'pnl':>10}")
    print(hdr)
    print('-' * len(hdr))
    blocked = []
    for t, poll in rows:
        stamp = '%s %s' % (t.get('exit_date'), t.get('exit_time'))
        if poll is None:
            blocked.append(t)
            print(f"{t['id']:>4} {t.get('stock',''):<12}{stamp:<20}"
                  f"{t.get('exit_debit') or 0:>9.2f}{'':^4}"
                  f"{'NO PATH DATA — cannot repair, left as booked':<46}")
            continue
        new_pnl = (poll['val'] - float(t['debit'])) * int(t['quantity'])
        print(f"{t['id']:>4} {t.get('stock',''):<12}{stamp:<20}"
              f"{t.get('exit_debit') or 0:>9.2f}{'->':^4}{poll['val']:>11.2f}"
              f"  {poll['ts'][11:19]:<10}{t.get('pnl') or 0:>10,.0f}{'->':^4}"
              f"{new_pnl:>10,.0f}")

    if not args.apply:
        print('\nDRY RUN. Re-run with --apply to write. And run it on the Pi:')
        print('a correction applied only here never reaches the box, because')
        print('the store merges by VERSION through Drive.\n')
        return 0

    applied = 0
    for t, poll in rows:
        if poll is None:
            continue
        # The store's OWN exit arithmetic, not a copy of it. `_apply_exit`
        # recomputes pnl / pnl_pct, re-costs the round trip and re-stamps
        # pnl_net, so a fee-model change cannot leave these three rows behind.
        with store._mutate(drive=True):
            row = store.find(t['id'])
            row['exit_remarked'] = {
                'was': {'exit_time': row.get('exit_time'),
                        'exit_debit': row.get('exit_debit'),
                        'exit_spot': row.get('exit_spot'),
                        'pnl': row.get('pnl')},
                'why': 'booked on the closing auction print; re-marked at the '
                       'last executable poll (see zebra/spot_shadow.py sibling '
                       'note and monitor._exits_executable)',
            }
            store._apply_exit(row, poll['spot'], poll['val'],
                              row.get('exit_reason', 'paper:tp'),
                              exit_legs={
                                  'long': {'bid': poll.get('long_bid'),
                                           'ask': poll.get('long_ask')},
                                  'short': {'bid': poll.get('short_bid'),
                                            'ask': poll.get('short_ask')}})
            # `_apply_exit` stamps `now` — but the exit happened at the poll,
            # not at the moment of the repair, and a record that claims to have
            # closed today would break every date-scoped report.
            row['exit_date'] = poll['ts'][:10]
            row['exit_time'] = poll['ts'][11:19]
        applied += 1

    print(f'\nRe-marked {applied} row(s).')
    if blocked:
        print('LEFT ALONE (no path data on this box): %s'
              % ', '.join('#%d %s' % (t['id'], t.get('stock')) for t in blocked))
        print('Those days may still be on the Pi — run it there.')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
