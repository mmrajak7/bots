"""
Zebra multi-year, regime-tagged, direction-SYMMETRIC backtest harness.

Reconstructs the live zebra signal on cached daily OHLC (2018-2026) for the
F&O universe and simulates watching -> trigger -> entry -> exit, so we can
test rule variants OUT-OF-SAMPLE across many regimes (not just the recent
CE-favourable 2 months).

Anti-lookahead discipline:
  - ST as of day t uses only COMPLETED weekly/monthly candles before t.
  - Confirmation entry enters NEXT bar open after the confirming close.
  - Regime tag uses NIFTY's trailing 200-DMA at entry (no forward info).

Spot R-multiple model (relative comparison of rules):
  target T = ST value at entry (fixed, matches live tp_spot)
  stop   S = variant-defined
  walk daily bars up to HORIZON; first-touch; tie -> stop (pessimistic);
  else mark-to-market at horizon close.  R = reward-per-unit-risk.
"""
from __future__ import annotations
import json, os, glob, csv
from collections import defaultdict, namedtuple
import sys
sys.path.insert(0, r"C:\Users\mail2\Documents\Projects\BOTS\Helper")
from playbook.compute_st import aggregate_to_weekly, aggregate_to_monthly, compute_supertrend

CACHE = r"C:\Users\mail2\Documents\Projects\BOTS\Helper\playbook\backtest_cache"
FNO_CSV = r"C:\Users\mail2\Documents\Projects\BOTS\data\stock_instruments.csv"

# --- live config mirror ---
WATCH_GAP_MAX = 0.05
TRIGGER_GAP_MAX = 0.04
STALE_GAP_MIN = 0.03
FRESHNESS_DAYS = 5
FRESH_TOUCH = 0.01      # within 1% of ST = "touched"
HORIZON = 21           # trading days (~30 cal days, mid of DTE 15-45)
CONFIRM_WIN = 5        # bars to wait for confirmation

def load_universe():
    syms = sorted({r['symbol'] for r in csv.DictReader(open(FNO_CSV))})
    return [s for s in syms if os.path.exists(os.path.join(CACHE, f"{s}.json"))]

def load_daily(sym):
    d = json.load(open(os.path.join(CACHE, f"{sym}.json")))
    out = []
    for c in d:
        dt = c['date'][:10]
        out.append({'date': dt, 'open': c['open'], 'high': c['high'],
                    'low': c['low'], 'close': c['close']})
    return out

def end_date_of(candles_in_period):
    return max(c['date'][:10] for c in candles_in_period)

def st_as_of_series(daily, timeframe):
    """Return list parallel to daily: (st_val, st_dir) of last COMPLETED
    weekly/monthly candle strictly before each daily date."""
    agg = aggregate_to_weekly if timeframe == 'weekly' else aggregate_to_monthly
    periods = agg(daily, exclude_current=False)  # full history
    st = compute_supertrend(periods, 10, 3)
    if not st:
        return [(None, None)] * len(daily)
    # each st entry corresponds to a period; compute that period's end date
    # rebuild period membership to get end dates
    from collections import defaultdict as dd
    buckets = dd(list)
    for c in daily:
        from datetime import datetime
        dt = datetime.fromisoformat(c['date'][:10])
        key = (dt.year, dt.month) if timeframe == 'monthly' else dt.isocalendar()[:2]
        buckets[key].append(c['date'][:10])
    # st entries are in period order; map to sorted keys
    keys_sorted = sorted(buckets.keys())
    # compute_supertrend drops first (period-1) entries; align from the tail
    # periods list == keys_sorted order; st covers periods[period-1:]
    offset = len(periods) - len(st)
    st_end = []  # (end_date, st_val, dir)
    for i, row in enumerate(st):
        key = keys_sorted[offset + i]
        st_end.append((max(buckets[key]), row['supertrend'], row['direction']))
    # assign to daily: for date t, latest st_end with end_date < t
    res = []
    j = -1
    si = 0
    for c in daily:
        t = c['date'][:10]
        while si < len(st_end) and st_end[si][0] < t:
            j = si; si += 1
        if j >= 0:
            res.append((st_end[j][1], st_end[j][2]))
        else:
            res.append((None, None))
    return res

def atr_at(daily, i, n=14):
    if i < n: return None
    trs = []
    for k in range(i-n+1, i+1):
        h, l, pc = daily[k]['high'], daily[k]['low'], daily[k-1]['close']
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs)/n

# --- NIFTY regime ---
def load_nifty():
    d = json.load(open(os.path.join(CACHE, "_nifty50_daily.json")))
    out = [{'date': c['date'][:10], 'close': c['close']} for c in d]
    out.sort(key=lambda x: x['date'])
    return out

NIFTY = load_nifty()
NIFTY_DATES = [x['date'] for x in NIFTY]
def regime_at(date):
    import bisect
    i = bisect.bisect_right(NIFTY_DATES, date) - 1
    if i < 200: return 'unknown'
    sma200 = sum(NIFTY[k]['close'] for k in range(i-199, i+1))/200
    sma50 = sum(NIFTY[k]['close'] for k in range(i-49, i+1))/50
    c = NIFTY[i]['close']
    if c > sma200 and c > sma50: return 'bull'
    if c < sma200 and c < sma50: return 'bear'
    return 'side'

Trade = namedtuple('Trade', 'sym tf dir entry_date R outcome year regime')

def gen_signals(sym):
    """Yield entry candidates (pre-variant) for a symbol across both TFs."""
    daily = load_daily(sym)
    if len(daily) < 260: return []
    out = []
    for tf in ('weekly', 'monthly'):
        sts = st_as_of_series(daily, tf)
        open_until = -1  # dedup: index until which a trade is open for this tf+dir
        last_dir = None
        i = 0
        n = len(daily)
        # track watching state per direction implicitly via scanning
        while i < n:
            st_val, st_dir = sts[i]
            if st_val is None or st_val <= 0:
                i += 1; continue
            price = daily[i]['close']
            gap_signed = (price - st_val)/st_val
            gap = abs(gap_signed)
            direction = 'CE' if price < st_val else 'PE'
            # trigger zone: gap in [stale_min, trigger_max]
            if STALE_GAP_MIN <= gap <= TRIGGER_GAP_MAX and i > open_until:
                # freshness: not within 1% of ST in last N days
                fresh = True
                for k in range(max(0, i-FRESHNESS_DAYS), i):
                    pv, pd_ = sts[k]
                    if pv and abs(daily[k]['close']-pv)/pv < FRESH_TOUCH:
                        fresh = False; break
                if fresh:
                    out.append((daily, sts, i, direction, st_val, tf))
                    open_until = i + HORIZON  # block re-entry while position would be open
            i += 1
    return out

def simulate(cand, variant):
    daily, sts, i, direction, st_val, tf = cand
    n = len(daily)
    T = st_val
    # entry
    if variant.get('confirm'):
        ei = None
        for k in range(i, min(i+CONFIRM_WIN, n-1)):
            if k == 0: continue
            if direction == 'CE' and daily[k]['close'] > daily[k-1]['high']:
                ei = k+1; break
            if direction == 'PE' and daily[k]['close'] < daily[k-1]['low']:
                ei = k+1; break
        if ei is None or ei >= n: return None
        E = daily[ei]['open']; start = ei
    else:
        E = daily[i]['close']; start = i
    # stop
    if variant.get('atr_mult'):
        a = atr_at(daily, start-1)
        if a is None: a = E*0.03
        S = E - variant['atr_mult']*a if direction == 'CE' else E + variant['atr_mult']*a
    else:
        S = E*(1-0.03) if direction == 'CE' else E*(1+0.03)
    risk = abs(E - S)
    if risk <= 0: return None
    end = min(start+HORIZON, n-1)
    peak = E; trail = None
    if variant.get('trail_atr'):
        a = atr_at(daily, start-1) or risk
        trail = variant['trail_atr']*a
    for k in range(start, end+1):
        hi, lo, cl = daily[k]['high'], daily[k]['low'], daily[k]['close']
        if direction == 'CE':
            if lo <= S: return _mk(cand, E, -1.0, 'stop')
            if trail is None:
                if hi >= T: return _mk(cand, E, (T-E)/risk, 'target')
            else:
                peak = max(peak, hi)
                if cl < peak-trail and k > start: return _mk(cand, E, (cl-E)/risk, 'trail')
        else:
            if hi >= S: return _mk(cand, E, -1.0, 'stop')
            if trail is None:
                if lo <= T: return _mk(cand, E, (E-T)/risk, 'target')
            else:
                peak = min(peak, lo)
                if cl > peak+trail and k > start: return _mk(cand, E, (E-cl)/risk, 'trail')
    cl = daily[end]['close']
    R = (cl-E)/risk if direction == 'CE' else (E-cl)/risk
    return _mk(cand, E, R, 'mtm')

def _mk(cand, E, R, outcome):
    daily, sts, i, direction, st_val, tf = cand
    ed = daily[i]['date']
    return Trade(None, tf, direction, ed, R, outcome, ed[:4], regime_at(ed))

def run(variant, universe):
    trades = []
    for sym in universe:
        for cand in gen_signals(sym):
            t = simulate(cand, variant)
            if t: trades.append(t._replace(sym=sym))
    return trades

def stats(trades):
    if not trades: return None
    Rs = [t.R for t in trades]
    wins = [r for r in Rs if r > 0]
    neg = -sum(r for r in Rs if r < 0)
    pf = sum(wins)/neg if neg else 99.0
    return dict(n=len(Rs), win=100*len(wins)/len(Rs), totR=sum(Rs),
                exp=sum(Rs)/len(Rs), pf=pf)

def by(trades, key):
    g = defaultdict(list)
    for t in trades: g[getattr(t, key)].append(t)
    return {k: stats(v) for k, v in sorted(g.items())}

if __name__ == '__main__':
    uni = load_universe()
    print(f"Universe: {len(uni)} F&O symbols, cache 2018-2026\n")
    variants = [
        ("baseline (3% stop, proximity)", {}),
        ("confirm entry", {'confirm': True}),
        ("ATR2.5 stop", {'atr_mult': 2.5}),
        ("confirm + ATR2.5", {'confirm': True, 'atr_mult': 2.5}),
    ]
    allres = {}
    for name, v in variants:
        tr = run(v, uni)
        allres[name] = tr
        s = stats(tr)
        print(f"{name:32s} n={s['n']:4d} win={s['win']:4.0f}% totR={s['totR']:+7.0f} exp={s['exp']:+.3f}R PF={s['pf']:.2f}")
    # save for deeper slicing
    out = {name: [t._asdict() for t in tr] for name, tr in allres.items()}
    json.dump(out, open(os.path.join(os.path.dirname(__file__), "bt_results.json"), 'w'))
    print("\nsaved bt_results.json")
