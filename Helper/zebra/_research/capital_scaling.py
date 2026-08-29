"""How many lots can this book actually enter, and what does size cost or save?

THE QUESTION (owner, 2026-08-30)
--------------------------------
"capital utilisation and how many lots we can enter as we scale ensuring legs
intact and trade as planned ... capital utilisation and risk management should
go together."

Three separate limits get conflated when this is discussed as one number, and
they bind in a different order at every capital level:

  BUDGET      capital x per-trade fraction, floor-divided by the cost of a lot
  LADDER      `capital_per_lot` -- one extra lot per Rs 2L of capital
  LIQUIDITY   what the touch can absorb without walking the book

The third is the one that decides whether "legs intact" survives scaling, and
it is the one this book has never recorded. See the DEPTH section below.

Run:  cd Helper && PURE_PYTHON=1 python -m zebra._research.capital_scaling
"""
from __future__ import annotations

import io
import json
import statistics as st
from pathlib import Path

HELPER = Path(__file__).resolve().parents[2]
STORE = HELPER / 'logs' / 'zebra_trades.json'
COHORT = '2026-08-14'

#: `zebra/fees.py`'s model, restated for the simulation. Per-order brokerage is
#: the FLAT part -- the part that does not scale -- and everything else is
#: proportional to turnover.
BROKERAGE_PER_ORDER = 20.0
ORDERS_PER_ROUND_TRIP = 4
STT_SELL_PCT = 0.1 / 100
EXCHANGE_PCT = 0.03503 / 100
SEBI_PCT = 0.0001 / 100
STAMP_BUY_PCT = 0.003 / 100
GST_PCT = 18.0 / 100


def load():
    rows = json.load(io.open(STORE, encoding='utf-8'))
    return [t for t in rows if t.get('cohort') == COHORT
            and t.get('structure') == 'bcs']


def per_lot_capital(t) -> float:
    """What one lot of this spread costs. The debit IS the max loss."""
    return float(t['debit']) * int(t['lot_size'])


def costs(t, lots: int) -> dict:
    """Modelled round-trip cost at `lots`, split flat vs proportional.

    The split is the whole point: brokerage is Rs 20 an order however big the
    order is, so it is the part that dilutes with size. Everything else is a
    percentage of turnover and does not care.
    """
    qty = int(t['lot_size']) * lots
    entry_buy = float(t['long_ask_entry']) * qty
    entry_sell = float(t['short_bid_entry']) * qty
    # Exit turnover is unknown ahead of time; use the entry as its own proxy,
    # which is what `zebra/fees.py` falls back to when no exit book exists.
    turnover = (entry_buy + entry_sell) * 2
    sell_turnover = entry_sell + entry_buy      # one sell per leg per round trip

    flat = BROKERAGE_PER_ORDER * ORDERS_PER_ROUND_TRIP
    prop = (sell_turnover * STT_SELL_PCT
            + turnover * EXCHANGE_PCT
            + turnover * SEBI_PCT
            + (entry_buy + entry_sell) * STAMP_BUY_PCT)
    gst = (flat + turnover * EXCHANGE_PCT) * GST_PCT
    total = flat + prop + gst
    debit_value = per_lot_capital(t) * lots
    return {'flat': flat, 'proportional': prop + gst, 'total': total,
            'pct_of_capital': total / debit_value * 100 if debit_value else 0.0}


def crossing_cost(t, lots: int) -> float:
    """What the bid-ask alone costs to open, in rupees.

    `entry_cost` is per share and already fill-basis (ASK long - BID short,
    against the mid). It scales linearly with size AT THE TOUCH -- which is
    exactly the assumption that fails once an order is bigger than the touch,
    and is why the depth section below matters more than this number.
    """
    return float(t.get('entry_cost') or 0.0) * int(t['lot_size']) * lots


def budget_lots(capital: float, per_trade_pct: float, t) -> int:
    return int((capital * per_trade_pct / 100) // per_lot_capital(t))


def ladder_lots(capital: float, capital_per_lot: float, hard: int) -> int:
    return max(1, min(hard, int(capital // capital_per_lot)))


def line(w=78):
    print('-' * w)


def main():
    trades = load()
    caps = [per_lot_capital(t) for t in trades]
    print('COHORT: %d BCS records, entered %s .. %s'
          % (len(trades), min(t['entry_date'] for t in trades),
             max(t['entry_date'] for t in trades)))
    print('capital per LOT: min %,.0f  median %,.0f  mean %,.0f  max %,.0f'
          .replace(',', '')
          % (min(caps), st.median(caps), st.mean(caps), max(caps)))
    print()

    # -- 1. which limit binds, at each capital level ------------------------
    print('1. WHICH LIMIT BINDS  (per-trade 12.5%, capital_per_lot 2L, hard 5)')
    line()
    print('%-10s %-8s %-8s %-8s %-10s %s'
          % ('capital', 'ladder', 'budget', 'lots', 'deployed', 'binding'))
    med = st.median(caps)
    fake = min(trades, key=lambda t: abs(per_lot_capital(t) - med))
    for capital in (200000, 400000, 600000, 800000, 1000000, 2000000):
        lad = ladder_lots(capital, 200000.0, 5)
        bud = budget_lots(capital, 12.5, fake)
        lots = max(0, min(lad, bud))
        slots = 4
        deployed = per_lot_capital(fake) * lots * slots
        binding = 'ladder' if lad <= bud else 'budget'
        print('%-10s %-8d %-8d %-8d %-10s %s'
              % ('%.1fL' % (capital / 100000), lad, bud, lots,
                 '%.0f%%' % (deployed / capital * 100), binding))
    print()
    print('  Read the two middle columns, not the last one: the LADDER is')
    print('  below the BUDGET at every level, so `capital_per_lot` decides')
    print('  size and the 12.5%% per-trade cap has never bound anything.')
    print()

    # -- 2. what size does to cost ------------------------------------------
    print('2. COST OF A ROUND TRIP, BY SIZE  (median position)')
    line()
    print('%-6s %-12s %-14s %-14s %s'
          % ('lots', 'capital', 'flat (broker)', 'proportional', 'total % of capital'))
    for lots in (1, 2, 3, 5, 8):
        c = costs(fake, lots)
        print('%-6d %-12s %-14s %-14s %.2f%%'
              % (lots, 'Rs %.0f' % (per_lot_capital(fake) * lots),
                 'Rs %.0f' % c['flat'], 'Rs %.0f' % c['proportional'],
                 c['pct_of_capital']))
    print()
    one = costs(fake, 1)['pct_of_capital']
    three = costs(fake, 3)['pct_of_capital']
    print('  Fee drag falls from %.2f%% to %.2f%% between 1 and 3 lots, then'
          % (one, three))
    print('  flattens: only the Rs 80 brokerage dilutes, and it is already a')
    print('  minority of the cost at 1 lot. SIZE HELPS, AND IT HELPS EARLY.')
    print()

    # -- 3. risk, which is the other half -----------------------------------
    print('3. RISK AT SIZE  (debit IS the max loss; %d slots)' % 4)
    line()
    print('%-6s %-14s %-16s %-16s %s'
          % ('lots', 'per position', 'book at 4 slots', '% of Rs 2L', 'worst case'))
    for lots in (1, 2, 3, 5):
        pos = per_lot_capital(fake) * lots
        book = pos * 4
        print('%-6s %-14s %-16s %-16s %s'
              % (lots, 'Rs %.0f' % pos, 'Rs %.0f' % book,
                 '%.0f%%' % (book / 200000 * 100),
                 'Rs %.0f all four to zero' % book))
    print()

    # -- 4. the depth question ----------------------------------------------
    print('4. CAN THE BOOK ABSORB IT?  (the "legs intact" question)')
    line()
    have_depth = [t for t in trades
                  if t.get('long_ask_qty') or t.get('short_bid_qty')]
    print('  records carrying entry DEPTH (ask_qty / bid_qty): %d of %d'
          % (len(have_depth), len(trades)))
    print()
    print('  `capital.liquidity_lots` reads exactly those two fields at plan')
    print('  time and they are NEVER PERSISTED, so how many lots the touch')
    print('  would have absorbed cannot be measured on a single historical')
    print('  entry. That is the binding constraint on scaling and it is the')
    print('  one quantity this book does not keep.')
    print()
    print('  What IS recorded is OI, which is a different thing (open')
    print('  contracts, not resting size at the touch):')
    ois = sorted(min(int(t['long_oi_entry']), int(t['short_oi_entry']))
                 for t in trades)
    lots_if_oi = [min(int(t['long_oi_entry']), int(t['short_oi_entry']))
                  // int(t['lot_size']) for t in trades]
    print('    thinner leg OI: min %d  median %d  max %d'
          % (ois[0], st.median(ois), ois[-1]))
    print('    OI / lot_size:  min %d  median %d  max %d'
          % (min(lots_if_oi), st.median(lots_if_oi), max(lots_if_oi)))
    print('  OI is an upper bound on interest, NOT on touch depth. Do not size')
    print('  from it.')
    print()

    # -- 5. what a partial fill costs at size --------------------------------
    print('5. WHAT SCALING DOES TO A FAILED ROUND')
    line()
    print('  `entry_executor` places N one-lot ROUNDS, long-first, and stops')
    print('  on the first failure. So the exposure to a bad book is not N')
    print('  lots -- it is ONE lot, plus whatever complete spreads already')
    print('  filled. That property is what makes scaling safe at all, and it')
    print('  is why slice size must stay 1.')
    print()
    for lots in (1, 3, 5):
        orphan = per_lot_capital(fake) / float(fake['debit']) * float(
            fake['long_ask_entry'])
        print('    %d lot(s) requested -> worst case is %d complete spread(s) '
              '+ ONE orphan long of Rs %.0f'
              % (lots, lots - 1, orphan))
    print()
    print('  The orphan is capped-risk and is now RECORDED and swept')
    print('  (`entry_residue`, 2026-08-30). Before that it was one Telegram.')


if __name__ == '__main__':
    main()
