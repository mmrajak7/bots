"""Part 2: utilisation, and the exposure that is not the debit.

Part 1 (`capital_scaling.py`) answered "which limit binds" and "what does size
cost". This one answers the risk half, and it changes the shape of the answer:
for a PHYSICALLY SETTLED option the debit is the max loss only while the
position is closed on time. The delivery margin is levied on the long ITM leg
AT ITS STRIKE -- full contract value -- and it is one to two orders of
magnitude larger than the debit.

Run:  cd Helper && PURE_PYTHON=1 python -m zebra._research.capital_scaling2
"""
from __future__ import annotations

import io
import json
import statistics as st
from pathlib import Path

HELPER = Path(__file__).resolve().parents[2]
STORE = HELPER / 'logs' / 'zebra_trades.json'
COHORT = '2026-08-14'

#: NSE Clearing's ramp on the long ITM leg, at EOD of E-4/E-3/E-2/E-1.
RAMP = (0.10, 0.25, 0.45, 0.70)


def load():
    rows = json.load(io.open(STORE, encoding='utf-8'))
    return [t for t in rows if t.get('cohort') == COHORT
            and t.get('structure') == 'bcs']


def line(w=78):
    print('-' * w)


def main():
    trades = load()
    caps = [float(t['debit']) * int(t['lot_size']) for t in trades]
    med_cap = st.median(caps)

    # -- 6. utilisation grid -------------------------------------------------
    print('6. UTILISATION: deployed %% of capital, by slots x lots')
    print('   (median position Rs %.0f; capital scales with the ladder)'
          % med_cap)
    line()
    print('%-8s' % 'slots', end='')
    for lots in (1, 2, 3, 4, 5):
        print('%-12s' % ('%d lot' % lots), end='')
    print()
    for slots in (4, 6, 8, 10, 12):
        print('%-8d' % slots, end='')
        for lots in (1, 2, 3, 4, 5):
            # The ladder implies the capital: `capital_per_lot` = 2L.
            capital = 200000.0 * lots
            deployed = med_cap * lots * slots
            print('%-12s' % ('%.0f%%' % (deployed / capital * 100)), end='')
        print()
    print()
    print('  LOTS DO NOT CHANGE UTILISATION. Position size and capital both')
    print('  scale with the ladder, so the ratio is fixed -- the only lever')
    print('  in this table is the number of SLOTS.')
    print()
    print('  At 4 slots the book can never use more than ~20%% of capital.')
    print('  M9 halved that: it was 8 slots (~40%%) until 2026-08-29.')
    print()

    # -- 7. the exposure that is not the debit -------------------------------
    print('7. DELIVERY EXPOSURE IF A CLOSE IS MISSED  (per ONE lot)')
    line()
    print('%-12s %-10s %-12s %-12s %-12s'
          % ('stock', 'debit', 'notional', 'E-4 (10%)', 'E-1 (70%)'))
    rows = []
    for t in trades:
        qty = int(t['lot_size'])
        notional = float(t['long_strike']) * qty
        rows.append((t['stock'], float(t['debit']) * qty, notional))
    for stock, debit, notional in sorted(rows, key=lambda r: -r[2])[:6]:
        print('%-12s %-10s %-12s %-12s %-12s'
              % (stock, 'Rs %.0f' % debit, 'Rs %.1fL' % (notional / 1e5),
                 'Rs %.0f' % (notional * RAMP[0]),
                 'Rs %.1fL' % (notional * RAMP[3] / 1e5)))
    notionals = [n for _s, _d, n in rows]
    print()
    print('  median notional per lot: Rs %.1fL   -> E-4 tranche Rs %.0f'
          % (st.median(notionals) / 1e5, st.median(notionals) * RAMP[0]))
    print('  median DEBIT per lot:    Rs %.0f' % med_cap)
    print('  ratio: the first margin tranche alone is %.0fx the debit.'
          % (st.median(notionals) * RAMP[0] / med_cap))
    print()
    print('  This only bites if the 6-session close FAILS. It is not a reason')
    print('  to stay small -- it is the reason the close, its retry ladder and')
    print('  the E-9 preflight exist, and the reason deployment must leave a')
    print('  cash reserve rather than run to 100%.')
    print()

    # -- 8. what a reserve costs --------------------------------------------
    print('8. WHAT RESERVE COVERS ONE MISSED CLOSE  (Rs 2L capital, 4 slots)')
    line()
    med_notional = st.median(notionals)
    print('%-14s %-14s %-16s %s'
          % ('max_deployed', 'debits at 4x1', 'reserve', 'covers E-4 on'))
    for pct in (100, 80, 60, 50):
        reserve = 200000 * (100 - pct) / 100
        covers = reserve / (med_notional * RAMP[0])
        print('%-14s %-14s %-16s %s'
              % ('%d%%' % pct, 'Rs %.0f' % (med_cap * 4),
                 'Rs %.0f' % reserve,
                 '%.1f lot(s)' % covers))
    print()
    print('  `max_deployed_pct` is 100 today, i.e. NO reserve. The book has')
    print('  never come close to it (4 x Rs %.0f = %.0f%% of 2L), so this is'
          % (med_cap, med_cap * 4 / 2000 ))
    print('  a limit that would only bind after several changes at once --')
    print('  which is exactly when nobody is looking at it.')
    print()

    # -- 9. concentration ----------------------------------------------------
    print('9. CONCENTRATION: one position as a share of capital')
    line()
    print('%-8s %-16s %-16s %s' % ('lots', 'capital', 'largest position',
                                   '% of capital'))
    largest = max(caps)
    for lots in (1, 2, 3, 5):
        capital = 200000.0 * lots
        pos = largest * lots
        print('%-8s %-16s %-16s %.1f%%'
              % (lots, 'Rs %.1fL' % (capital / 1e5), 'Rs %.0f' % pos,
                 pos / capital * 100))
    print()
    print('  Flat, again, because both sides scale with the ladder. The')
    print('  largest cohort position is %.1f%% of capital at every level, well'
          % (largest / 200000 * 100))
    print('  inside the 12.5%% per-trade cap -- so that cap is not protecting')
    print('  anything either.')


if __name__ == '__main__':
    main()
