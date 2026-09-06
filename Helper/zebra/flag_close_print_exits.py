"""Flag exits that were booked on the CLOSING AUCTION print. It does not re-book.

## What went wrong

`monitor._is_market_open` tested `(h, m) <= MARKET_CLOSE`, so the 15:30 cron
cycle read as open and polled the closing print. Three cohort positions booked
`paper:tp` on it — TMPV #423 at 15:30:28, GMRAIRPORT #455 at 15:30:48 and
ADANIGREEN #471 at 15:31:08, all 2026-08-31. `monitor._exits_executable` stops
it happening again.

## Why this only FLAGS, and the mistake it is a correction of

The first version of this tool re-booked each exit at the last poll before the
close. That was worse than the thing it was fixing. At 15:25 none of the three
had touched its target — TMPV 318.45 against a TP of 309.29, GMRAIRPORT 95.51
against 94.09, ADANIGREEN 1262.90 against 1247.43, all PE, all still above
target. So it stamped a TAKE-PROFIT at a moment when no exit rule had fired:
the original booking at least had a real trigger and a real (if unfillable)
price, while the re-mark had a real price and an invented trigger. It also
flipped TMPV from a Rs 24 win to a Rs 80 loss and moved the whole cohort
headline, on an artefact of where the script chose to mark.

**And the honest counterfactual is not available.** Under the fixed rule the
15:30 touch is never latched, so these three would have carried into 09-01 —
and because the old engine closed them, no path was ever recorded for what
happened next. There is nothing to re-book them AT.

There is a second reason not to trust a pre-close mark, found in review: **the
spot feed is frozen for the last 15 minutes of every session.** Across all 16
captured sessions, spot at 15:15, 15:20 and 15:25 is identical on 125 of 125
observations, against 0 of 122 for the same triple at 12:15-12:25. So a
"15:25 exit_spot" is really the 15:15 print, and the 15:25→15:30 move is 15
minutes of invisible continuous trading plus the closing auction — not the one
5-minute step the first version of this docstring claimed.

So the record keeps the numbers it was booked with, and gains a flag saying
they came from a print this engine could not have transacted at. Anything
scoring fill quality, exit slippage or TP timing must exclude these rows;
anything counting trades may keep them.

SAFE-TO-RERUN: the flag is idempotent and the exit values are never touched. A
row already carrying `exit_closing_print` is left exactly as it is.

RETIRES WHEN: no cohort row has an exit stamped at or after the close — i.e.
this has been run once on every replica and `_exits_executable` has been live
for a full session.

## Running it

    python -m zebra.flag_close_print_exits            # DRY RUN, the default
    python -m zebra.flag_close_print_exits --apply

RUN IT ON ONE REPLICA, THEN LET THE OTHERS SYNC. The store merges by VERSION,
so if two replicas both flag the same row independently they both mint the same
version with different content, which `store_contract.resolve_merge` reports as
a split brain that never converges. Flag on the Pi, wait one cron cycle, verify
elsewhere.
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import config as cfg
from .trade_store import get_store, in_cohort

logger = logging.getLogger(__name__)

FIELD = 'exit_closing_print'


def booked_after_close(trade: dict) -> bool:
    """Was this exit stamped at or after the close?"""
    t = trade.get('exit_time')
    if not t or trade.get('status') != 'exited':
        return False
    try:
        h, m = int(t[:2]), int(t[3:5])
    except (ValueError, IndexError):
        return False
    return (h, m) >= cfg.MARKET_CLOSE


def plan(store) -> list:
    """Cohort rows booked at or after the close and not yet flagged."""
    # `paper:expiry` is EXCLUDED. `_settle_if_expired` deliberately runs above
    # the cascade's close gate and still books on the closing poll, because it
    # fires strictly PAST expiry when the contracts no longer exist — there is
    # no fill to be had at any hour, so a 15:30 stamp on one is not a defect.
    # Flagging it would put a caveat on the one exit that never needed one.
    return [t for t in store.load_trades()
            if in_cohort(t) and booked_after_close(t) and FIELD not in t
            and not str(t.get('exit_reason', '')).endswith('expiry')]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--apply', action='store_true',
                    help='actually write the flag (default: show what would change)')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    store = get_store()
    rows = plan(store)
    if not rows:
        print('\nNo unflagged cohort exit at or after the %02d:%02d close.\n'
              % cfg.MARKET_CLOSE)
        return 0

    print()
    print('EXITS BOOKED ON A PRINT THIS ENGINE COULD NOT HAVE TRANSACTED AT')
    print('The numbers stay as booked. Exclude these rows from any statistic')
    print('about fill quality, exit slippage or TP timing.')
    print()
    hdr = (f"{'id':>4} {'stock':<12}{'exit':<21}{'reason':<16}"
           f"{'exit_debit':>11}{'pnl':>10}")
    print(hdr)
    print('-' * len(hdr))
    for t in rows:
        print(f"{t['id']:>4} {t.get('stock', ''):<12}"
              f"{'%s %s' % (t.get('exit_date'), t.get('exit_time')):<21}"
              f"{t.get('exit_reason', ''):<16}"
              f"{t.get('exit_debit') or 0:>11.2f}{t.get('pnl') or 0:>10,.0f}")

    if not args.apply:
        print('\nDRY RUN. Re-run with --apply to write the flag.\n')
        return 0

    for t in rows:
        # Local-only would be wrong here: this is a claim ABOUT the record that
        # every replica must see, so it takes an ordinary versioned write.
        store.update_trade_fields(t['id'], **{FIELD: {
            'booked_at': '%s %s' % (t.get('exit_date'), t.get('exit_time')),
            'close': '%02d:%02d' % cfg.MARKET_CLOSE,
            'why': 'the 15:30 cron cycle read as open (_is_market_open used '
                   '<= MARKET_CLOSE) and polled the closing auction print; no '
                   'live order fills there',
            'not_rebooked_because': 'under the fixed rule the touch is never '
                                    'latched, so this position would have '
                                    'carried to the next session — and no path '
                                    'was recorded for it, because the old '
                                    'engine closed it here. There is nothing '
                                    'to re-book it AT.',
            'exclude_from': ['fill quality', 'exit slippage', 'TP timing'],
        }})
        logger.info('flagged #%d %s', t['id'], t.get('stock'))

    print(f'\nFlagged {len(rows)} row(s). Values unchanged.\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
