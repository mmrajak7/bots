"""
Does a bear-regime pause actually help FIFTY? Test on Dec'25-Jul'26.
Regime = the Helper Active engine's leading filter:
  PAUSE   instantly when ALL 3 OFF:  NIFTY RSI(14)>50, breadth>40% (% of
          universe stocks above their own 50DMA), NIFTY close>50DMA
  UNPAUSE when 2-of-3 ON for 7 CONSECUTIVE trading days
Applied to the raw Dec'25 signal stream (20/fifty_dec25_results.json) ON TOP
of the live stretch<=2.0 gate. Entries blocked while PAUSED; exits untouched.

Run from Helper/:  python 20/fifty_regime_test.py
"""
import json, os, glob, sys
import pandas as pd, numpy as np

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FS = r'C:/Users/mail2/Documents/Projects/BOTS/FIFTY/_research/filter_stretch'
CACHE = r'C:/Users/mail2/Documents/Projects/BOTS/Helper/playbook/backtest_cache'
HERE = os.path.dirname(os.path.abspath(__file__))

# ---- NIFTY daily ----
nd = pd.DataFrame(json.load(open(os.path.join(FS, 'nifty_long.json'))))
nd['date'] = pd.to_datetime(nd['date'])
nd = nd.set_index('date').sort_index()
close = nd['c']
# RSI(14) Wilder
delta = close.diff()
gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
rsi = 100 - 100/(1 + gain/loss)
sma50 = close.rolling(50).mean()

# ---- breadth: % of universe above own 50DMA per day ----
files = [f for f in glob.glob(os.path.join(CACHE, '*.json')) if not os.path.basename(f).startswith('_')]
START = pd.Timestamp('2025-09-01')
counts = None
above = None
for f in files:
    try:
        d = json.load(open(f))
    except Exception:
        continue
    if len(d) < 100: continue
    df = pd.DataFrame(d)
    df['date'] = pd.to_datetime(df['date'].str[:10])
    s = df.set_index('date')['close'].sort_index()
    s = s[~s.index.duplicated()]
    ma = s.rolling(50).mean()
    ok = (s > ma).astype(float)[s.index >= START]
    if len(ok) == 0: continue
    if counts is None:
        counts = ok*0 + 1; above = ok.copy()
    else:
        above = above.add(ok, fill_value=0)
        counts = counts.add(ok*0 + 1, fill_value=0)
breadth = (above/counts*100).dropna()

# ---- regime timeline ----
days = [d for d in breadth.index if d >= START]
state = 'UNPAUSED'
streak = 0
timeline = {}
for d in days:
    r = rsi.get(d, np.nan); b = breadth.get(d, np.nan)
    c = close.get(d, np.nan); m = sma50.get(d, np.nan)
    sigs = [r > 50, b > 40, c > m]
    n_on = sum(bool(x) for x in sigs)
    if n_on == 0:
        state = 'PAUSED'; streak = 0
    elif state == 'PAUSED':
        if n_on >= 2:
            streak += 1
            if streak >= 7:
                state = 'UNPAUSED'
        else:
            streak = 0
    timeline[d.date().isoformat()] = (state, n_on, round(float(r), 1), round(float(b), 1))

# print transitions
print("=== REGIME TIMELINE (transitions only, from Sep'25) ===")
prev = None
for k, v in timeline.items():
    if v[0] != prev:
        print(f"  {k}: {v[0]}  (signals ON: {v[1]}, RSI {v[2]}, breadth {v[3]}%)")
        prev = v[0]

# ---- apply to trade stream ----
d = json.load(open(os.path.join(HERE, 'fifty_dec25_results.json')))
raw = d['raw']
SLOTS = 10

def regime_on(datestr):
    if datestr in timeline:
        return timeline[datestr][0] == 'UNPAUSED'
    # non-breadth day (holiday edge): use last known
    ks = sorted(k for k in timeline if k <= datestr)
    return timeline[ks[-1]][0] == 'UNPAUSED' if ks else True

def replay(trades, size=5000):
    taken = []; busy = []
    for t in trades:
        e = pd.Timestamp(t['entry'])
        busy = [x for x in busy if x[0] >= e]
        if any(s == t['sym'] for _, s in busy): continue
        if len(busy) < SLOTS:
            taken.append(t); busy.append((pd.Timestamp(t['exit']), t['sym']))
    pnl = sum(size*t['ret']/100 for t in taken)
    wins = sum(1 for t in taken if t['ret'] > 0)
    return taken, pnl, wins

variants = {
    'LIVE stretch<=2.0 (baseline)': lambda t: t['stretch'] <= 2.0,
    'stretch<=2.0 + REGIME pause': lambda t: t['stretch'] <= 2.0 and regime_on(t['entry']),
    'REGIME pause only': lambda t: regime_on(t['entry']),
}
print(f"\n{'Variant':<34} {'Took':>4} {'Win':>4} {'Avg%':>7} {'P&L@5k':>9} {'@5L':>7}")
for name, fn in variants.items():
    sub = [t for t in raw if fn(t)]
    taken, pnl, wins = replay(sub)
    avg = np.mean([t['ret'] for t in taken]) if taken else 0
    print(f"{name:<34} {len(taken):>4} {wins:>4} {avg:>+6.1f} {pnl:>+9.0f} {pnl*100/100000:>+6.1f}L")

# which trades did the regime block / allow?
blocked = [t for t in raw if t['stretch'] <= 2.0 and not regime_on(t['entry'])]
print(f"\nBlocked by regime (of stretch-passing signals): {len(blocked)}")
big = sorted(blocked, key=lambda t: -abs(t['ret']))[:12]
for t in big:
    print(f"  {t['sym']:<14} {t['entry']}  ret {t['ret']:+7.1f}%  ({t['status']})")
