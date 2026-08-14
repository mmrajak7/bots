"""What the round trip actually costs, stamped on every paper exit.

The two-month paper run has ONE question to answer: does this strategy clear
its own costs? The measured baseline says the median BCS trade is **+0.90%
gross and -0.79% NET** of Zerodha charges, carried entirely by 3 winners out of
33. A cohort scored on `pnl_pct` alone would answer a question nobody asked and
would look fine while losing money.

So `pnl` keeps its meaning (gross, unchanged, comparable to every earlier
record) and `pnl_net` is added beside it. Never one replacing the other: the
old book is priced differently and mixing the two definitions is exactly the
mistake `band_basis` exists to prevent elsewhere.

WHAT IS MODELLED
----------------
Indian equity-option charges, per EXECUTED ORDER and per leg:

  brokerage      flat per order, both sides
  STT            SELL side only, on premium turnover
  exchange txn   both sides, on premium turnover
  SEBI           both sides, on premium turnover
  stamp duty     BUY side only, on premium turnover
  GST            on (brokerage + exchange txn + SEBI)

A 2-leg structure is FOUR executed orders round trip: each leg opens and
closes. A zebra back-ratio buys two of the long strike, which is more quantity
on that leg but still one order.

THE RATES ARE CONFIG, AND THEY ARE ESTIMATES
--------------------------------------------
`cfg.FEE_RATES` carries published Zerodha rates. They change (the Apr 2026 API
round is already in this repo's history), and the only authority is a real
contract note. `fee_model` is stamped on every trade so a rate correction can
be applied retroactively by recomputing from the stored leg prices rather than
guessed at later — the same reason `exit_legs` is persisted at all.

**Verify these against a live contract note before any go-live decision.** They
are good enough to rank trades and to answer "does the median clear costs";
they are not good enough to reconcile a broker statement to the rupee.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from . import config as cfg

logger = logging.getLogger(__name__)

#: Bumped whenever the SHAPE of the calculation changes (not the rates — those
#: live in config and are stamped separately). A stored figure whose model
#: version differs from today's must be recomputed, never compared.
MODEL_VERSION = 1


def _leg_orders(structure: str, debit: float, quantity: int,
                long_price: Optional[float], short_price: Optional[float],
                exit_long: Optional[float], exit_short: Optional[float],
                long_qty_mult: int = 1) -> List[dict]:
    """The four executed orders of a two-leg debit structure.

    Returned as data rather than folded into a total so the caller — and the
    post-mortem two months from now — can see which side carried the cost.
    STT is the big asymmetry: it lands on the sell side only, so a structure
    whose short leg is expensive pays materially more than its mirror.
    """
    q = int(quantity or 0)
    if q <= 0:
        return []
    lq = q * max(1, int(long_qty_mult))
    out = []
    # ENTRY: buy the long, sell the short.
    if long_price is not None:
        out.append({'leg': 'long', 'side': 'BUY', 'price': float(long_price),
                    'qty': lq, 'when': 'entry'})
    if short_price is not None:
        out.append({'leg': 'short', 'side': 'SELL', 'price': float(short_price),
                    'qty': q, 'when': 'entry'})
    # EXIT: the mirror. Short is bought back first in the live playbook, which
    # matters for margin, not for cost.
    if exit_short is not None:
        out.append({'leg': 'short', 'side': 'BUY', 'price': float(exit_short),
                    'qty': q, 'when': 'exit'})
    if exit_long is not None:
        out.append({'leg': 'long', 'side': 'SELL', 'price': float(exit_long),
                    'qty': lq, 'when': 'exit'})
    return out


def estimate(orders: List[dict]) -> dict:
    """Total charges for a list of executed orders, with the breakdown.

    Never raises and never returns a negative total: this runs inside the exit
    path, and a costing error must not be able to stop a position being closed
    or to manufacture a profit.
    """
    r = cfg.FEE_RATES
    out = {'brokerage': 0.0, 'stt': 0.0, 'exchange': 0.0, 'sebi': 0.0,
           'stamp': 0.0, 'gst': 0.0, 'total': 0.0, 'orders': 0,
           'model': MODEL_VERSION, 'rates': dict(r)}
    if not orders:
        return out
    try:
        for o in orders:
            turnover = abs(float(o['price'])) * abs(int(o['qty']))
            sell = str(o.get('side', '')).upper() == 'SELL'
            out['brokerage'] += float(r['brokerage_per_order'])
            out['exchange'] += turnover * float(r['exchange_pct']) / 100.0
            out['sebi'] += turnover * float(r['sebi_pct']) / 100.0
            if sell:
                out['stt'] += turnover * float(r['stt_sell_pct']) / 100.0
            else:
                out['stamp'] += turnover * float(r['stamp_buy_pct']) / 100.0
            out['orders'] += 1
        # GST rides on the SERVICE charges only — never on STT or stamp duty,
        # which are taxes in their own right. Taxing a tax overstates the drag
        # and would make the strategy look worse than it is.
        out['gst'] = (out['brokerage'] + out['exchange'] + out['sebi']) \
            * float(r['gst_pct']) / 100.0
        out['total'] = (out['brokerage'] + out['stt'] + out['exchange']
                        + out['sebi'] + out['stamp'] + out['gst'])
    except Exception as e:                       # pragma: no cover - guard
        logger.warning('fee estimate failed (%s) — reporting zero, NOT '
                       'blocking the exit', e)
        return {'brokerage': 0.0, 'stt': 0.0, 'exchange': 0.0, 'sebi': 0.0,
                'stamp': 0.0, 'gst': 0.0, 'total': 0.0, 'orders': 0,
                'model': MODEL_VERSION, 'error': str(e)[:120]}
    return {k: (round(v, 2) if isinstance(v, float) else v)
            for k, v in out.items()}


def round_trip_for_trade(t: dict, exit_debit: Optional[float]) -> dict:
    """Charges for one zebra/BCS trade, from what the record already stores.

    Falls back to the STRUCTURE price when per-leg exit prices are missing —
    an exit booked at the structure mid has no leg book, and refusing to cost
    it would leave exactly the trades that exited badly with no net figure.
    The fallback is flagged so the post-mortem can tell them apart.
    """
    qty = int(t.get('quantity') or 0)
    # A zebra back-ratio holds TWO longs per short; BCS holds one of each.
    mult = 2 if str(t.get('structure') or 'zebra') == 'zebra' else 1
    legs = t.get('exit_legs') if isinstance(t.get('exit_legs'), dict) else {}
    xl = (legs.get('long') or {}).get('price') if isinstance(legs.get('long'), dict) else None
    xs = (legs.get('short') or {}).get('price') if isinstance(legs.get('short'), dict) else None
    approx = xl is None or xs is None
    entry_long = (t.get('long_ask_entry') if t.get('long_ask_entry') is not None
                  else t.get('long_mid_entry'))
    entry_short = (t.get('short_bid_entry') if t.get('short_bid_entry') is not None
                   else t.get('short_mid_entry'))
    if approx:
        # No leg book — true of 208 of the 215 records closed before exit books
        # were persisted, so this path decides the historical answer, not an
        # edge case.
        #
        # The first cut modelled the exit as ONE order priced at the STRUCTURE
        # value. That under-counted twice over: three orders instead of four,
        # and a sell-side turnover an order of magnitude below the real long
        # leg, which is where STT actually lands. Scale BOTH entry legs by the
        # structure's own decay instead — it preserves the relative leg sizes,
        # which is what the charges are levied on.
        debit0 = float(t.get('debit') or 0)
        k = (float(exit_debit) / debit0) if (debit0 > 0 and exit_debit is not None) else 0.0
        k = max(0.0, min(k, 5.0))        # a vertical cannot 5x; clamp the tail
        xl = float(entry_long or 0) * k
        xs = float(entry_short or 0) * k
    orders = _leg_orders(
        t.get('structure') or 'zebra', float(t.get('debit') or 0), qty,
        entry_long, entry_short, xl, xs, long_qty_mult=mult)
    est = estimate(orders)
    # ORDER COUNT IS KNOWN EVEN WHEN PRICES ARE NOT. A two-leg debit structure
    # is four executed orders round trip whatever the book did, so brokerage
    # and its GST are never in doubt. The first cut skipped an order whenever a
    # leg price was missing, and reported a median of Rs 47 — BELOW the Rs 80
    # fixed brokerage floor, i.e. an impossible number that would have made the
    # cohort's net P&L look better than it can be. Charge the floor.
    expected = 4 if (t.get('long_symbol') and t.get('short_symbol')) else 2
    if est['orders'] < expected:
        r = cfg.FEE_RATES
        missing = expected - est['orders']
        extra = missing * float(r['brokerage_per_order'])
        est['brokerage'] = round(est['brokerage'] + extra, 2)
        est['gst'] = round(est['gst'] + extra * float(r['gst_pct']) / 100.0, 2)
        est['total'] = round(est['total'] + extra
                             + extra * float(r['gst_pct']) / 100.0, 2)
        est['orders'] = expected
        # The premium-based charges (STT above all) are NOT recoverable, so say
        # so instead of quietly reporting a total that reads as complete.
        est['basis'] = 'brokerage_only'
        est['note'] = ('no per-leg prices on this record — STT, exchange, SEBI '
                       'and stamp could not be computed and are MISSING from '
                       'the total, which is therefore a FLOOR, not an estimate')
    else:
        est['basis'] = 'modelled' if approx else 'full'
    est['approx'] = bool(approx)
    return est
