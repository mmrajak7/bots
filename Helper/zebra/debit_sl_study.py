"""Did the 50% debit stop save money, or cause the drain?

The question, unanswered since the fee-drag note was written: `debit_sl` is the
second-largest exit bucket in the book and the largest by loss. We know what it
cost. We do not know what NOT stopping would have cost, so we cannot say
whether the stop is protection or the leak.

Method
------
For every `debit_sl` exit whose expiry has passed, value the structure at its
expiry close and compare that with what the stop actually booked. This is a
deliberate LOOK-AHEAD calculation — that is the point. It is not a signal and
must never become one (`feedback_measure_as_of_the_decision_date`); it is an
outcome comparison, and outcomes are only visible afterwards.

Payoff per share at expiry, by structure — getting this wrong by treating a
zebra as a vertical would invert the answer, and every record reconstructible
today is a zebra:

    CE zebra (2x ITM long, 1x ATM short):  2*max(S-K_L,0) -   max(S-K_S,0)
    PE zebra:                              2*max(K_L-S,0) -   max(K_S-S,0)
    BCS vertical:              clamp(max(S-K_L,0) - max(S-K_S,0), 0, width)

What "held to expiry" is NOT
----------------------------
It is not a free option, and the headline number below is an UPPER BOUND, not
an achievable P&L. Indian stock options are PHYSICALLY SETTLED: carrying an ITM
leg into expiry means delivery, exchange delivery margin ramping over the last
~4 sessions, and STT on exercise charged on full contract value rather than on
premium. Actually banking the expiry value means closing shortly BEFORE expiry
and paying the bid-ask on both legs.

So read the output as: "the stop gave up at most this much." If the stop still
looks good against an upper bound, the conclusion is safe. If it only looks bad
against the upper bound, it is not yet an argument for removing it.

Usage
-----
    python -m zebra.debit_sl_study              # every reconstructible record
    python -m zebra.debit_sl_study --structure bcs
    python -m zebra.debit_sl_study --json out.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HELPER))

TRADES = HELPER / 'logs' / 'zebra_trades.json'
TOKEN = HELPER.parent / 'data' / 'kite_access_token.json'
CACHE = HELPER / 'zebra' / '_expiry_close_cache.json'


# ── payoff ──────────────────────────────────────────────────────────────────

def payoff_per_share(trade: dict, spot: float):
    """Structure value per share at expiry, or None if the shape is unknown.

    Refuses rather than guesses: a record without the strikes it needs is an
    unknown, and averaging an invented number into the answer is how a study
    ends up confirming whatever it was set up to confirm.
    """
    k_l, k_s = trade.get('long_strike'), trade.get('short_strike')
    if k_l is None or k_s is None:
        return None
    direction = (trade.get('direction') or '').upper()
    structure = trade.get('structure')

    if structure == 'bcs':
        width = trade.get('width')
        if width is None:
            width = abs(k_s - k_l)
        if direction == 'CE':
            v = max(spot - k_l, 0.0) - max(spot - k_s, 0.0)
        elif direction == 'PE':
            v = max(k_l - spot, 0.0) - max(k_s - spot, 0.0)
        else:
            return None
        return min(max(v, 0.0), float(width))

    # zebra back ratio: 2 long ITM, 1 short ATM
    if direction == 'CE':
        return 2.0 * max(spot - k_l, 0.0) - max(spot - k_s, 0.0)
    if direction == 'PE':
        return 2.0 * max(k_l - spot, 0.0) - max(k_s - spot, 0.0)
    return None


# ── expiry closes ───────────────────────────────────────────────────────────

def load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_cache(c: dict) -> None:
    CACHE.write_text(json.dumps(c, indent=1, sort_keys=True), encoding='utf-8')


def fetch_expiry_closes(pairs, cache):
    """pairs = {(stock, expiry)}. Returns {key: close}. Cached; Kite only for
    what is missing, because this is re-run every time an expiry passes.

    A FAILED fetch is never cached. The first draft stored None on failure, and
    `todo` skips any key already present — so one transient Kite error would
    have retired that record from the study permanently, and the sample would
    quietly shrink instead of growing as expiries pass. Absent means "ask
    again"; present means "we have a close".
    """
    todo = sorted({p for p in pairs if cache.get(f'{p[0]}|{p[1]}') is None})
    if not todo:
        return cache

    from kiteconnect import KiteConnect
    tok = json.loads(TOKEN.read_text(encoding='utf-8'))
    kite = KiteConnect(api_key=tok['api_key'])
    kite.set_access_token(tok['access_token'])

    tokens = {}
    stocks = sorted({s for s, _ in todo})
    for i in range(0, len(stocks), 100):
        chunk = [f'NSE:{s}' for s in stocks[i:i + 100]]
        try:
            for k, v in kite.ltp(chunk).items():
                tokens[k.split(':', 1)[1]] = v['instrument_token']
        except Exception as e:
            print(f'  ltp batch failed ({e}); falling back one at a time')
            for sym in chunk:
                try:
                    tokens[sym.split(':', 1)[1]] = kite.ltp([sym])[sym]['instrument_token']
                except Exception as e2:
                    print(f'    {sym}: {e2}')

    # A bar for TODAY is still forming. `close` on it is the last traded price
    # so far, not a close, and on expiry day that is the one number the whole
    # study turns on. Refuse it and come back tomorrow rather than settling 20
    # records against a 10 a.m. print.
    today = dt.date.today()

    for stock, expiry in todo:
        key = f'{stock}|{expiry}'
        it = tokens.get(stock)
        if it is None:
            print(f'  {stock}: no instrument token; will retry next run')
            continue
        end = dt.date.fromisoformat(expiry)
        try:
            candles = kite.historical_data(it, end - dt.timedelta(days=10),
                                           end, 'day')
        except Exception as e:
            print(f'  {stock} {expiry}: {e}; will retry next run')
            continue
        # The last session AT OR BEFORE expiry, and strictly BEFORE today.
        # Expiry can fall on a holiday, and taking "the last candle we got"
        # without that check would silently use a post-expiry close on any
        # symbol whose data ran long.
        usable = [c for c in candles
                  if (c['date'].date() if hasattr(c['date'], 'date')
                      else c['date']) <= end
                  and (c['date'].date() if hasattr(c['date'], 'date')
                       else c['date']) < today]
        if not usable:
            print(f'  {stock} {expiry}: no settled daily bar yet; '
                  f'will retry next run')
            continue
        cache[key] = float(usable[-1]['close'])
    save_cache(cache)
    return cache


# ── study ───────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--structure', choices=['bcs', 'zebra', 'all'], default='all')
    ap.add_argument('--asof', default=None, help='YYYY-MM-DD (default: today)')
    ap.add_argument('--json', dest='out', default=None)
    a = ap.parse_args(argv)

    asof = dt.date.fromisoformat(a.asof) if a.asof else dt.date.today()
    book = json.loads(TRADES.read_text(encoding='utf-8'))
    dsl = [t for t in book
           if t.get('status') == 'exited'
           and 'debit_sl' in (t.get('exit_reason') or '')]

    if a.structure == 'bcs':
        dsl = [t for t in dsl if t.get('structure') == 'bcs']
    elif a.structure == 'zebra':
        dsl = [t for t in dsl if t.get('structure') != 'bcs']

    ripe, unripe = [], []
    for t in dsl:
        e = t.get('expiry')
        (ripe if e and dt.date.fromisoformat(e) < asof else unripe).append(t)

    print(f'debit_sl exits: {len(dsl)}   expiry passed: {len(ripe)}   '
          f'not yet: {len(unripe)}')
    if unripe:
        nxt = sorted({t['expiry'] for t in unripe})
        print(f'  NOT counted (expiry in the future): '
              + ', '.join(f'{e} x{sum(1 for t in unripe if t["expiry"] == e)}'
                          for e in nxt))
    if not ripe:
        print('nothing reconstructible yet.')
        return 0

    cache = fetch_expiry_closes({(t['stock'], t['expiry']) for t in ripe},
                                load_cache())

    rows, skipped = [], []
    for t in ripe:
        close = cache.get(f"{t['stock']}|{t['expiry']}")
        if close is None:
            skipped.append((t['stock'], t['expiry'], 'no expiry close'))
            continue
        pay = payoff_per_share(t, close)
        if pay is None:
            skipped.append((t['stock'], t['expiry'], 'unknown structure'))
            continue
        qty = t.get('quantity') or 0
        debit = t.get('debit')
        if not qty or debit is None:
            skipped.append((t['stock'], t['expiry'], 'missing qty/debit'))
            continue
        held = (pay - debit) * qty
        actual = t.get('pnl')
        if actual is None:
            actual = ((t.get('exit_debit') or 0) - debit) * qty
        rows.append({
            'id': t.get('id'), 'stock': t['stock'], 'dir': t.get('direction'),
            'structure': t.get('structure') or 'zebra',
            'expiry': t['expiry'], 'expiry_close': close,
            'entry_spot': t.get('entry_spot'), 'exit_spot': t.get('exit_spot'),
            'debit': debit, 'exit_debit': t.get('exit_debit'),
            'expiry_value': round(pay, 2), 'qty': qty,
            'actual_pnl': round(actual, 0),
            'held_pnl': round(held, 0),
            'stop_saved': round(actual - held, 0),
        })

    if not rows:
        # `ripe` was non-empty but every record was skipped -- a dead token, a
        # symbol that no longer resolves, an expiry whose bar has not settled.
        # Report that as "no answer yet", never as a result: the summary below
        # divides by n, and a study that crashes on the day its sample is
        # incomplete is a study nobody re-runs.
        print()
        print(f'NONE of the {len(ripe)} ripe records could be valued.')
        for s in skipped:
            print('   ', s)
        print()
        print('No answer yet. Re-run once the causes above clear.')
        return 0

    rows.sort(key=lambda r: r['stop_saved'])
    print()
    print(f"{'id':>4} {'stock':<12} {'d':<3} {'expiry':<11} {'close':>9} "
          f"{'debit':>8} {'exp_val':>8} {'actual':>10} {'held':>10} {'saved':>10}")
    print('-' * 100)
    for r in rows:
        print(f"{r['id']:>4} {r['stock']:<12} {r['dir']:<3} {r['expiry']:<11} "
              f"{r['expiry_close']:>9.2f} {r['debit']:>8.2f} "
              f"{r['expiry_value']:>8.2f} {r['actual_pnl']:>10,.0f} "
              f"{r['held_pnl']:>10,.0f} {r['stop_saved']:>10,.0f}")

    n = len(rows)
    saved = sum(r['stop_saved'] for r in rows)
    act = sum(r['actual_pnl'] for r in rows)
    held = sum(r['held_pnl'] for r in rows)
    better = sum(1 for r in rows if r['stop_saved'] > 0)
    print('-' * 100)
    print(f"n={n}   stopped total Rs {act:+,.0f}   held-to-expiry total "
          f"Rs {held:+,.0f}")
    print(f"THE STOP {'SAVED' if saved > 0 else 'COST'} Rs {abs(saved):,.0f} "
          f"in aggregate; it was the better choice on {better}/{n} "
          f"({100.0 * better / n:.0f}%)")
    med = sorted(r['stop_saved'] for r in rows)[n // 2]
    print(f"median per-trade effect of stopping: Rs {med:+,.0f}")
    zero = sum(1 for r in rows if r['expiry_value'] <= 0.005)
    print(f"expired worthless: {zero}/{n}  "
          f"(held-to-expiry would have lost the FULL debit on these)")
    print()
    print('UPPER BOUND, not an achievable P&L: banking the expiry value means '
          'closing just before expiry and paying both legs\' bid-ask, or '
          'taking physical delivery with its margin ramp and exercise STT.')
    if skipped:
        print(f'\nskipped {len(skipped)}:')
        for s in skipped:
            print('   ', s)

    if a.out:
        Path(a.out).write_text(json.dumps(rows, indent=1), encoding='utf-8')
        print(f'\nwrote {a.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
