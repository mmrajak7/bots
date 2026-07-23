"""
Multi-year validation (2020-2026) of the bear-regime HYBRID for FIFTY.
======================================================================
Variants (all on the proxy dip-buy engine, 20% SL + monthly-low trail):
  A. stretch <= 2.0                      (live shipped config = baseline)
  B. A + regime pause                    (entries blocked while PAUSED)
  C. A + regime pause EXCEPT stretch<=-2 (the hybrid from 20/05)
  D. A + regime pause EXCEPT stretch<=-1.5
Regime: RSI(14)>50 / breadth>40% / close>50DMA; instant PAUSE all-3-OFF,
UNPAUSE 2-of-3 for 7 consecutive trading days. Breadth = % of cache universe
above own 50DMA (computable from ~Dec-2019; validation window 2020-01+).

Replay: 10 slots, one position per symbol. P&L at Rs 5,000/trade (test size);
multiply x100 for Rs 5L sizing. Open trades MTM at cache end (2026-07-23).

Run from Helper/:  python 20/fifty_hybrid_validation.py
"""
import json, os, glob, sys
import pandas as pd, numpy as np

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FS = r'C:/Users/mail2/Documents/Projects/BOTS/FIFTY/_research/filter_stretch'
CACHE = r'C:/Users/mail2/Documents/Projects/BOTS/Helper/playbook/backtest_cache'
HERE = os.path.dirname(os.path.abspath(__file__))
WIN_START = pd.Timestamp('2020-01-01')
WIN_END = pd.Timestamp('2026-07-23')
SLOTS = 10
SIZE = 5_000


def supertrend(df, period=10, mult=3.0):
    df = df.copy(); df['pc'] = df['Close'].shift(1)
    df['tr'] = pd.concat([df['High']-df['Low'], (df['High']-df['pc']).abs(),
                          (df['Low']-df['pc']).abs()], axis=1).max(axis=1)
    atr = df['tr'].ewm(alpha=1/period, adjust=False).mean()
    hl2 = (df['High']+df['Low'])/2
    blb = (hl2-mult*atr).values; bub = (hl2+mult*atr).values; cl = df['Close'].values
    n = len(df); flb = np.zeros(n); fub = np.zeros(n); tr = np.zeros(n); st = np.zeros(n)
    if n: flb[0] = blb[0]; fub[0] = bub[0]; tr[0] = 1; st[0] = flb[0]
    for i in range(1, n):
        flb[i] = max(blb[i], flb[i-1]) if cl[i-1] > flb[i-1] else blb[i]
        fub[i] = min(bub[i], fub[i-1]) if cl[i-1] < fub[i-1] else bub[i]
        tr[i] = 1 if cl[i] > fub[i-1] else (-1 if cl[i] < flb[i-1] else tr[i-1])
        st[i] = flb[i] if tr[i] == 1 else fub[i]
    df['st'] = st; df['trend'] = tr; df['atr'] = atr.values
    return df


# ---------- NIFTY: weekly stretch + daily RSI/50DMA ----------
nd = pd.DataFrame(json.load(open(os.path.join(FS, 'nifty_long.json'))))
nd['date'] = pd.to_datetime(nd['date'])
nd = nd.rename(columns={'o': 'Open', 'h': 'High', 'l': 'Low', 'c': 'Close'}).set_index('date').sort_index()
wk = nd.resample('W').agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'}).dropna()
W = supertrend(wk); W['stretch'] = (W['Close']-W['st'])/W['atr']
wk_idx = W.index.values

def stretch_at(d):
    pos = np.searchsorted(wk_idx, np.datetime64(d)) - 1
    return float(W['stretch'].iloc[pos]) if pos >= 0 else np.nan

close = nd['Close']
delta = close.diff()
gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
rsi = 100 - 100/(1 + gain/loss)
sma50 = close.rolling(50).mean()

# ---------- breadth (daily, whole cache) ----------
files = [f for f in glob.glob(os.path.join(CACHE, '*.json')) if not os.path.basename(f).startswith('_')]
print(f"Universe files: {len(files)}")
B_START = pd.Timestamp('2019-11-01')
above = None; counts = None
frames = []
for f in files:
    try:
        d = json.load(open(f))
    except Exception:
        continue
    if len(d) < 120: continue
    df = pd.DataFrame(d)
    df['date'] = pd.to_datetime(df['date'].str[:10])
    s = df.set_index('date')['close'].sort_index()
    s = s[~s.index.duplicated()]
    ma = s.rolling(50).mean()
    ok = (s > ma).astype(float)
    ok = ok[ok.index >= B_START]
    if len(ok) == 0: continue
    if above is None:
        above = ok.copy(); counts = ok*0 + 1
    else:
        above = above.add(ok, fill_value=0)
        counts = counts.add(ok*0 + 1, fill_value=0)
breadth = (above/counts*100).dropna()
print(f"Breadth series: {breadth.index[0].date()} .. {breadth.index[-1].date()}")

# ---------- regime timeline ----------
timeline = {}
state = 'UNPAUSED'; streak = 0
for d in breadth.index:
    r = rsi.get(d, np.nan); b = breadth.get(d, np.nan)
    c = close.get(d, np.nan); m = sma50.get(d, np.nan)
    n_on = sum(bool(x) for x in [r > 50, b > 40, c > m])
    if n_on == 0:
        state = 'PAUSED'; streak = 0
    elif state == 'PAUSED':
        if n_on >= 2:
            streak += 1
            if streak >= 7: state = 'UNPAUSED'
        else:
            streak = 0
    timeline[d.date().isoformat()] = state
tl_keys = sorted(timeline)

def regime_on(ds):
    ks_pos = np.searchsorted(tl_keys, ds, side='right') - 1
    return timeline[tl_keys[ks_pos]] == 'UNPAUSED' if ks_pos >= 0 else True

print("\nRegime transitions:")
prev = None
for k in tl_keys:
    if timeline[k] != prev:
        print(f"  {k}: {timeline[k]}")
        prev = timeline[k]

# ---------- generate proxy trades 2020-2026 with dates ----------
def load(f):
    d = json.load(open(f))
    df = pd.DataFrame(d); df['date'] = pd.to_datetime(df['date'].str[:10])
    df = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}).set_index('date').sort_index()
    return df[~df.index.duplicated()][['Open', 'High', 'Low', 'Close']]

raw = []
for fi, f in enumerate(files):
    sym = os.path.splitext(os.path.basename(f))[0]
    try:
        df = load(f)
    except Exception:
        continue
    if len(df) < 300: continue
    mo = df.resample('M').agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'}).dropna()
    if len(mo) < 14: continue
    M = supertrend(mo)
    mst = M['st'].values; mtr = M['trend'].values; midx = M.index
    lows = df['Low'].values; opens = df['Open'].values; didx = df.index
    for j in range(12, len(M)-1):
        if mtr[j] != 1: continue
        level = mst[j]
        if level <= 0: continue
        mstart = midx[j+1].to_period('M')
        win_mask = df.index.to_period('M') == mstart
        win = df[win_mask]
        hit = win[win['Low'] <= level]
        if len(hit) == 0: continue
        edate = hit.index[0]
        if not (WIN_START <= edate <= WIN_END): continue
        entry_px = hit['Open'].iloc[0] if hit['Open'].iloc[0] < level else level
        post = df[df.index > edate]
        sl = entry_px*0.8; cur = None; mlow = None
        exit_px = exit_dt = None
        for dt, row in post.iterrows():
            if row['Low'] <= sl:
                exit_px = min(row['Open'], sl) if row['Open'] < sl else sl
                exit_dt = dt; break
            ym = (dt.year, dt.month)
            if cur is None: cur = ym; mlow = row['Low']
            elif ym != cur: sl = max(sl, mlow); cur = ym; mlow = row['Low']
            else: mlow = min(mlow, row['Low'])
        status = 'closed'
        if exit_px is None:
            status = 'open'
            exit_px = post['Close'].iloc[-1] if len(post) else df['Close'].iloc[-1]
            exit_dt = post.index[-1] if len(post) else df.index[-1]
        raw.append(dict(sym=sym, entry=str(edate.date()), exit=str(exit_dt.date()),
                        ret=(exit_px-entry_px)/entry_px*100, status=status,
                        stretch=stretch_at(edate)))
    if (fi+1) % 200 == 0:
        print(f"  ... {fi+1}/{len(files)} files, {len(raw)} trades")

raw.sort(key=lambda t: t['entry'])
print(f"\nProxy trades 2020-2026 (pre-filter): {len(raw)}")
json.dump(raw, open(os.path.join(HERE, 'fifty_hybrid_raw.json'), 'w'))

# ---------- variants + replay ----------
def replay(trades):
    taken = []; busy = []
    for t in trades:
        e = pd.Timestamp(t['entry'])
        busy = [x for x in busy if x[0] >= e]
        if any(s == t['sym'] for _, s in busy): continue
        if len(busy) < SLOTS:
            taken.append(t); busy.append((pd.Timestamp(t['exit']), t['sym']))
    return taken

VARIANTS = {
    'A: stretch<=2 (live)': lambda t: t['stretch'] <= 2.0,
    'B: A + regime': lambda t: t['stretch'] <= 2.0 and regime_on(t['entry']),
    'C: A + regime|deep-2': lambda t: t['stretch'] <= 2.0 and (regime_on(t['entry']) or t['stretch'] <= -2),
    'D: A + regime|deep-1.5': lambda t: t['stretch'] <= 2.0 and (regime_on(t['entry']) or t['stretch'] <= -1.5),
}

years = sorted(set(t['entry'][:4] for t in raw))
print(f"\n=== YEARLY P&L @Rs {SIZE}/trade, {SLOTS} slots (x100 for 5L sizing) ===")
hdr = f"{'Year':<6}" + "".join(f"{n.split(':')[0]:>12}" for n in VARIANTS)
print(hdr + "   (n trades A/B/C/D)")
takens = {n: replay([t for t in raw if fn(t)]) for n, fn in VARIANTS.items()}
tot = {n: 0.0 for n in VARIANTS}
for y in years:
    row = f"{y:<6}"
    ns = []
    for n in VARIANTS:
        ts = [t for t in takens[n] if t['entry'][:4] == y]
        pnl = sum(SIZE*t['ret']/100 for t in ts)
        tot[n] += pnl
        row += f"{pnl:>+12.0f}"
        ns.append(str(len(ts)))
    print(row + "   " + "/".join(ns))
row = f"{'TOTAL':<6}"
for n in VARIANTS: row += f"{tot[n]:>+12.0f}"
print(row)

print("\n=== SUMMARY ===")
for n in VARIANTS:
    ts = takens[n]
    wins = sum(1 for t in ts if t['ret'] > 0)
    avg = np.mean([t['ret'] for t in ts]) if ts else 0
    mons = sum(1 for t in ts if t['ret'] >= 50)
    print(f"{n:<24} trades {len(ts):>4}  win {wins/len(ts)*100 if ts else 0:>4.0f}%  "
          f"avg {avg:>+6.1f}%  monsters(>=50%) {mons:>3}  "
          f"total @5k {tot[n]:>+9.0f}  @5L {tot[n]*100/100000:>+8.1f}L")
