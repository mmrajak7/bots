#!/usr/bin/env python3
"""Price the B6 turbulence tripwire on the ledger before it is allowed to gate anything.

WHY THIS SCRIPT EXISTS
----------------------
`turbulence.BLOCK_ENTRIES` ships False because its thresholds are guesses. Every other
filter in this system — NO_ENTRY_WINDOWS, MIN_T1_DISTANCE, the 30M removal — was
measured on the ledger at SEVERAL thresholds before it was allowed to touch an entry.
This one has not been, and the asymmetry is unforgiving: the ledger's best single day
was +Rs7,605 while its worst was -Rs4,313, so a filter that wrongly closes the desk can
cost more than the worst day ever lost.

It is genuinely open whether event days hurt this strategy at all. The magnet buys cheap
premium below a falling ST line; on high-volatility days premiums are fatter, gaps to ST
are wider, and MIN_T1_DISTANCE stops binding. The honest possible answers include
"sensitize, don't halt" and "event days are the best days". This script is built to be
able to return those answers, not to confirm the tripwire.

    python analyze_turbulence.py --price          # the measurement (needs market data)
    python analyze_turbulence.py --fetch          # cache NIFTY+VIX daily bars via Kite
    python analyze_turbulence.py                  # what the LIVE tripwire has recorded

DATA
----
--fetch caches daily NIFTY (token 256265) and India VIX (264969) bars to
backtest_data/. It needs a FRESH Kite access token; on the Pi SNAIL regenerates one each
morning, so run --fetch there or on a day the desktop token is current. Everything the
tripwire judges is reconstructible from daily bars: VIX close, VIX previous close, NIFTY
open vs previous close (the gap), and the high-low range.
"""
import argparse
import collections
import datetime
import json
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, CURRENT_DIR)

import turbulence as tb  # noqa: E402

NIFTY_TOKEN, VIX_TOKEN = 256265, 264969
_CACHE = os.path.join(CURRENT_DIR, 'backtest_data', '%s_daily_2026.json')

#: Threshold tiers to price. The shipped guesses are the middle row. A filter whose
#: verdict flips wildly across neighbouring tiers is noise, not signal — that is
#: precisely what pricing at several thresholds is for.
TIERS = [
    ('loose',    {'VIX_TURBULENT': 30.0, 'VIX_JUMP_TURBULENT': 30.0, 'GAP_TURBULENT': 2.50}),
    ('shipped',  {'VIX_TURBULENT': 25.0, 'VIX_JUMP_TURBULENT': 20.0, 'GAP_TURBULENT': 1.75}),
    ('tight',    {'VIX_TURBULENT': 20.0, 'VIX_JUMP_TURBULENT': 15.0, 'GAP_TURBULENT': 1.25}),
    ('vtight',   {'VIX_TURBULENT': 18.0, 'VIX_JUMP_TURBULENT': 10.0, 'GAP_TURBULENT': 1.00}),
]


def fetch():
    """Cache daily NIFTY and VIX bars. Requires a fresh Kite token."""
    import option_buy as ob
    k = ob.get_kite()
    if not k:
        print("No Kite client — cannot fetch.")
        return 1
    start, end = datetime.date(2026, 3, 1), datetime.date.today()
    ok = True
    for name, tok in (('NIFTY', NIFTY_TOKEN), ('VIX', VIX_TOKEN)):
        try:
            bars = k.historical_data(tok, start, end, 'day')
            out = [{'date': str(b['date'].date()), 'open': b['open'], 'high': b['high'],
                    'low': b['low'], 'close': b['close']} for b in bars]
            os.makedirs(os.path.dirname(_CACHE % name), exist_ok=True)
            with open(_CACHE % name, 'w') as fh:
                json.dump(out, fh)
            print("%-6s cached %d bars  %s -> %s"
                  % (name, len(out), out[0]['date'], out[-1]['date']))
        except Exception as e:
            ok = False
            print("%-6s FETCH FAILED: %s: %s" % (name, type(e).__name__, e))
            if 'token' in str(e).lower():
                print("       (the Kite access token is stale — it expires daily; "
                       "run this on the Pi where SNAIL refreshes it each morning)")
    return 0 if ok else 1


def load_bars(name):
    try:
        with open(_CACHE % name) as fh:
            return {b['date']: b for b in json.load(fh)}
    except Exception:
        return None


def load_ledger():
    with open(os.path.join(CURRENT_DIR, 'st_opt_positions.json')) as fh:
        positions = json.load(fh)
    by_day = collections.defaultdict(list)
    for k, p in positions.items():
        if p.get('status') != 'closed':
            continue
        try:
            pnl = ((float(p['exit_premium']) - float(p['entry_premium']))
                   * int(p.get('lot_size', 75)))
        except (KeyError, TypeError, ValueError):
            continue
        by_day[p.get('entry_date', '')].append(pnl)
    return by_day


def verdict_for_day(day, nifty, vix, tier):
    """Re-evaluate the tripwire for one historical day under a threshold tier."""
    saved = {k: getattr(tb, k) for k in tier}
    try:
        for k, val in tier.items():
            setattr(tb, k, val)
        iso = '%s-%s-%s' % (day[:4], day[4:6], day[6:8])
        n, v = nifty.get(iso), vix.get(iso)
        if not n:
            return None
        # Previous session's close, for both series.
        days = sorted(nifty)
        i = days.index(iso)
        prev = nifty[days[i - 1]]['close'] if i > 0 else None
        vprev = None
        if v:
            vdays = sorted(vix)
            if iso in vix:
                j = vdays.index(iso)
                vprev = vix[vdays[j - 1]]['close'] if j > 0 else None
        return tb.evaluate(vix=(v or {}).get('close'), vix_prev_close=vprev,
                           spot=n['close'], prev_close=prev, day_open=n['open'],
                           day_high=n['high'], day_low=n['low'])
    finally:
        for k, val in saved.items():
            setattr(tb, k, val)


def stats(pnls):
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    return {'n': len(pnls), 'net': sum(pnls),
            'pf': (gross_win / gross_loss) if gross_loss else float('inf'),
            'wr': (100.0 * sum(1 for p in pnls if p > 0) / len(pnls)) if pnls else 0.0}


def price():
    nifty, vix = load_bars('NIFTY'), load_bars('VIX')
    if not nifty:
        print("No cached market data. Run:  python analyze_turbulence.py --fetch")
        print("(needs a fresh Kite token — on the Pi, where SNAIL refreshes it daily)")
        return 1
    if not vix:
        print("NOTE: no VIX bars cached — only the GAP signal can be priced.")
        vix = {}

    by_day = load_ledger()
    days = sorted(by_day)
    print("=" * 78)
    print("PRICING THE TURBULENCE TRIPWIRE — %d trading days, %d trades"
          % (len(days), sum(len(v) for v in by_day.values())))
    print("=" * 78)
    base = stats([p for d in days for p in by_day[d]])
    print("baseline (trade everything): n=%d  net=Rs%.0f  PF=%.2f  WR=%.0f%%"
          % (base['n'], base['net'], base['pf'], base['wr']))
    print()
    print("%-9s %6s %10s %6s %6s  |  %6s %10s %6s %6s" %
          ('tier', 'kept', 'net', 'PF', 'WR', 'skip', 'net', 'PF', 'WR'))
    print("-" * 78)

    covered = None
    for label, tier in TIERS:
        kept, skipped, missing = [], [], 0
        for d in days:
            v = verdict_for_day(d, nifty, vix, tier)
            if v is None:
                missing += 1
                kept.extend(by_day[d])
                continue
            (skipped if v['verdict'] == tb.TURBULENT else kept).extend(by_day[d])
        covered = missing
        k, s = stats(kept), stats(skipped)
        print("%-9s %6d %10.0f %6.2f %5.0f%%  |  %6d %10.0f %6s %5s" %
              (label, k['n'], k['net'], k['pf'], k['wr'], s['n'], s['net'],
               ('%.2f' % s['pf']) if s['n'] else '-',
               ('%.0f%%' % s['wr']) if s['n'] else '-'))

    print("-" * 78)
    print("LEFT columns = the book if turbulent days were skipped.")
    print("RIGHT columns = what those skipped days actually did.")
    print()
    print("READ IT THIS WAY: the tripwire only earns its place if the RIGHT side is")
    print("meaningfully NEGATIVE and the LEFT side beats the baseline. A right side")
    print("that is positive means halting would have thrown away money, and a verdict")
    print("that swings across neighbouring tiers is noise rather than signal.")
    if covered:
        print()
        print("WARNING: %d ledger day(s) had no market bar and were counted as KEPT."
              % covered)
    return 0


def live():
    path = os.path.join(CURRENT_DIR, 'logs', 'turbulence.jsonl')
    if not os.path.exists(path):
        print("No turbulence records yet at %s" % path)
        print("The tripwire writes its first line on the next scan.")
        return 0
    recs, torn = [], 0
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except ValueError:
                torn += 1
    print("=" * 78)
    print("LIVE TRIPWIRE RECORD — %d entries%s"
          % (len(recs), (" (%d torn)" % torn) if torn else ""))
    print("=" * 78)
    by_day = collections.defaultdict(list)
    for r in recs:
        by_day[r.get('day', '?')].append(r)
    for d in sorted(by_day):
        worst = max(by_day[d], key=lambda r: tb._RANK.get(r.get('verdict'), 0))
        blocked = 'BLOCKED' if worst.get('block_enabled') else 'recorded only'
        print("  %s  %-9s  %-13s  %s" %
              (d, worst.get('verdict'), blocked,
               '; '.join(worst.get('reasons', [])) or '-'))
    n_turb = sum(1 for d in by_day
                 if any(r.get('verdict') == tb.TURBULENT for r in by_day[d]))
    print()
    print("days observed: %d | turbulent: %d" % (len(by_day), n_turb))
    print("blocking is %s" % ('ON' if tb.BLOCK_ENTRIES else 'OFF (records only)'))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--price', action='store_true', help='measure on the ledger')
    ap.add_argument('--fetch', action='store_true', help='cache NIFTY+VIX daily bars')
    args = ap.parse_args()
    if args.fetch:
        return fetch()
    if args.price:
        return price()
    return live()


if __name__ == '__main__':
    sys.exit(main())
