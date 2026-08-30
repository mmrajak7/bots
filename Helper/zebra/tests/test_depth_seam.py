"""The `analyze()` -> `analyze_bcs()` seam must carry SIZE, not just price.

THE DEFECT THIS PINS (found 2026-08-31, present since depth collection
shipped 2026-08-30). `strikes._atm_quote` projected `_quote_option`'s output
down to {bid, ask, mid, oi}, dropping `bid_qty`/`ask_qty`. `analyze_bcs` then
read `atm_quote.get('ask_qty')` and got None on every production entry, so:

  * `zebra/monitor.py` builds `depth` only `if long_ask_qty is not None`, so
    `capital.plan` NEVER received a depth dict -- the liquidity bound and the
    `liquidity_unknown -> 1 lot` fallback were both unreachable in production;
  * `long_ask_qty_entry` persisted as None on every record, so the entry-side
    half of the lot-scaling evidence could never accumulate. All 13 records of
    cohort 2026-08-14 carry None.

Why the suite could not see it: every other test hands `analyze_bcs` a
hand-built `atm_quote` dict that ALREADY contains the size keys. The bug lived
entirely in the projection between the two functions, which nothing exercised.
These tests therefore drive the REAL `_quote_option` output through the REAL
projection -- never a hand-built dict.

Run:  cd Helper && python -m pytest zebra/tests/test_depth_seam.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zebra import capital                # noqa: E402
from zebra import strikes                # noqa: E402


class _Kite:
    """Kite stub returning a full depth book, the shape `quote()` really has."""

    def __init__(self, bid_qty=4000, ask_qty=3000):
        self.bid_qty, self.ask_qty = bid_qty, ask_qty

    def quote(self, keys):
        key = keys[0]
        return {key: {
            'depth': {'buy': [{'price': 10.0, 'quantity': self.bid_qty}],
                      'sell': [{'price': 10.4, 'quantity': self.ask_qty}]},
            'oi': 50000, 'last_price': 10.2,
            'last_trade_time': None,
        }}


def test_quote_option_really_reports_size():
    """Baseline: the source of the data has always carried it."""
    q = strikes._quote_option(_Kite(), 'TESTCO26AUG1000CE')
    assert q['bid_qty'] == 4000
    assert q['ask_qty'] == 3000


def test_the_atm_projection_carries_size():
    """THE SEAM. Fails pre-fix: both keys were dropped, so both were None."""
    q = strikes._quote_option(_Kite(), 'TESTCO26AUG1000CE')
    atm = strikes._atm_quote(q)
    assert atm['ask_qty'] == 3000, 'ask_qty dropped -> capital.plan loses depth'
    assert atm['bid_qty'] == 4000
    # ...and it still carries everything analyze_bcs priced off before.
    for k in ('bid', 'ask', 'mid', 'oi'):
        assert atm[k] == q[k]


def test_a_projected_quote_yields_a_usable_depth_bound():
    """End to end: projection -> monitor's depth dict -> capital.liquidity_lots.

    This is the arithmetic `capital.plan` was never able to reach, reproduced
    with the monitor's own construction (`zebra/monitor.py` :697 / :1138).
    """
    atm = strikes._atm_quote(strikes._quote_option(_Kite(), 'X'))
    tgt = strikes._quote_option(_Kite(bid_qty=1500, ask_qty=9000), 'Y')

    long_ask_qty = atm.get('ask_qty')
    short_bid_qty = tgt.get('bid_qty')
    assert long_ask_qty is not None, 'the monitor would skip building depth'

    depth = {'long': {'ask_qty': long_ask_qty},
             'short': {'bid_qty': short_bid_qty}}
    # long ask 3000, short bid 1500 -> the short side binds -> 1500//500 = 3
    assert capital.liquidity_lots(depth, 500) == 3


def test_an_unquotable_leg_sizes_to_zero_lots_not_to_unlimited():
    """Fail CLOSED: a quote error zeroes the sizes, which must refuse, not pass.

    `_quote_option`'s error branch returns bid_qty/ask_qty 0. Now that the
    projection carries them, that 0 reaches `liquidity_lots` -- which must
    return 0 lots (refuse), never None (unknown) and never a size.
    """
    class Dead:
        def quote(self, keys):
            raise RuntimeError('Too many requests')

    atm = strikes._atm_quote(strikes._quote_option(Dead(), 'X'))
    assert atm['ask_qty'] == 0
    depth = {'long': {'ask_qty': atm['ask_qty']}, 'short': {'bid_qty': 4000}}
    assert capital.liquidity_lots(depth, 500) == 0


def test_the_projection_is_the_only_place_the_shape_is_narrowed():
    """A future refactor that re-narrows the projection fails here.

    Pins the KEY SET, so dropping a size key to 'simplify' the dict is a test
    failure rather than a silently dead sizing bound.
    """
    q = strikes._quote_option(_Kite(), 'X')
    assert set(strikes._atm_quote(q)) == {
        'bid', 'ask', 'mid', 'oi', 'bid_qty', 'ask_qty'}
