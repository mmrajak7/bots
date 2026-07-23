"""
FIFTY what-if: trading from Dec-2025 through today (2026-07-23)
================================================================
Question: FIFTY sat out this year (false trade-close logic). What would have
happened if it had traded from Dec'25?

Engine: the validated proxy from _research/filter_stretch/multiyear.py
(monthly ST(10,3) dip-buy — GTT at prior-month ST support, 20% SL,
monthly-low trail ratchet; validated within 1-2% of the bot's real trades).

Config = the LIVE shipped config:
  - NIFTY weekly stretch gate: allow iff stretch <= 2.0 (completed week, iloc[-2])
  - 10 position slots (max_positions: 10)
  - per_trade_amount Rs 5,000 (live test size) — also reported at Rs 5L/trade
Entry window: first touch date in 2025-12-01 .. 2026-07-23.
Open positions marked to market at the last cached close.

Also reported: the same replay under the OLD binary filter (what blocked the
bot in reality) and with no filter, for contrast.

Run from Helper/:  python 20/fifty_dec25_sim.py
"""
import json, os, glob, sys
import pandas as pd, numpy as np

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FS = r'C:/Users/mail2/Documents/Projects/BOTS/FIFTY/_research/filter_stretch'
CACHE = r'C:/Users/mail2/Documents/Projects/BOTS/Helper/playbook/backtest_cache'
WIN_START = pd.Timestamp('2025-12-01')
WIN_END = pd.Timestamp('2026-07-23')
STRETCH_HIGH = 2.0
SLOTS = 10
SIZE_TEST = 5_000
SIZE_REAL = 500_000


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


# ---- NIFTY weekly stretch (completed weeks only) ----
nd = pd.DataFrame(json.load(open(os.path.join(FS, 'nifty_long.json'))))
nd['date'] = pd.to_datetime(nd['date'])
nd = nd.rename(columns={'o': 'Open', 'h': 'High', 'l': 'Low', 'c': 'Close'}).set_index('date').sort_index()
wk = nd.resample('W').agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'}).dropna()
W = supertrend(wk); W['stretch'] = (W['Close']-W['st'])/W['atr']
wk_idx = W.index.values

def nifty_at(d):
    """Completed week strictly before date d."""
    pos = np.searchsorted(wk_idx, np.datetime64(d)) - 1
    if pos < 0: return None
    r = W.iloc[pos]
    return dict(stretch=float(r['stretch']), trend=int(r['trend']))


def load(f):
    d = json.load(open(f))
    df = pd.DataFrame(d); df['date'] = pd.to_datetime(df['date'].str[:10])
    df = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}).set_index('date').sort_index()
    return df[~df.index.duplicated()][['Open', 'High', 'Low', 'Close']]


# ---- generate signal entries Dec'25..today ----
files = [f for f in glob.glob(os.path.join(CACHE, '*.json')) if not os.path.basename(f).startswith('_')]
raw = []
for f in files:
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
    for j in range(12, len(M)-1):
        if mtr[j] != 1: continue
        level = mst[j]
        if level <= 0: continue
        mstart = midx[j+1].to_period('M')
        if mstart.start_time < WIN_START - pd.offsets.MonthBegin(1): continue
        win = df[df.index.to_period('M') == mstart]
        hit = win[win['Low'] <= level]
        if len(hit) == 0: continue
        edate = hit.index[0]
        if not (WIN_START <= edate <= WIN_END): continue
        if hit['Open'].iloc[0] < level:      # gap below support: GTT fills at open
            entry_px = hit['Open'].iloc[0]
        else:
            entry_px = level
        # simulate exits: 20% SL + monthly-low trail
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
        ctx = nifty_at(edate)
        raw.append(dict(sym=sym, entry=str(edate.date()), exit=str(exit_dt.date()),
                        entry_px=round(float(entry_px), 2), exit_px=round(float(exit_px), 2),
                        ret=(exit_px-entry_px)/entry_px*100, status=status,
                        stretch=ctx['stretch'] if ctx else np.nan,
                        ntrend=ctx['trend'] if ctx else 1))

raw.sort(key=lambda t: t['entry'])
print(f"Signal entries Dec'25..Jul'26 (pre-filter): {len(raw)}")

FILTERS = {
    'LIVE stretch<=2.0': lambda t: t['stretch'] <= STRETCH_HIGH,
    'OLD binary (what really happened)': lambda t: t['ntrend'] == 1,
    'NO filter': lambda t: True,
}


def replay(trades, size):
    """10-slot capital replay: slot busy from entry..exit, first-come first-served.
    One open position per symbol (bot behavior)."""
    taken = []
    busy = []  # list of (exit_date, sym)
    for t in trades:
        e = pd.Timestamp(t['entry'])
        busy = [x for x in busy if x[0] >= e]
        if any(sym == t['sym'] for _, sym in busy):
            continue  # already holding this symbol
        if len(busy) < SLOTS:
            taken.append(t)
            busy.append((pd.Timestamp(t['exit']), t['sym']))
    pnl = sum(size*t['ret']/100 for t in taken)
    open_n = sum(1 for t in taken if t['status'] == 'open')
    wins = sum(1 for t in taken if t['ret'] > 0)
    return taken, pnl, wins, open_n


print(f"\n{'Filter':<36} {'Sig':>4} {'Took':>4} {'Win':>4} {'Open':>4} "
      f"{'Avg%':>7} {'P&L@5k':>10} {'P&L@5L':>10}")
results = {}
for name, fn in FILTERS.items():
    sub = [t for t in raw if fn(t)]
    taken, pnl_t, wins, open_n = replay(sub, SIZE_TEST)
    _, pnl_r, _, _ = replay(sub, SIZE_REAL)
    avg = np.mean([t['ret'] for t in taken]) if taken else 0
    print(f"{name:<36} {len(sub):>4} {len(taken):>4} {wins:>4} {open_n:>4} "
          f"{avg:>+6.1f} {pnl_t:>+10.0f} {pnl_r/100000:>+9.1f}L")
    results[name] = taken

# ---- detail: the LIVE-config trade sheet ----
taken = results['LIVE stretch<=2.0']
print(f"\n=== TRADE SHEET — LIVE config (stretch<=2.0, {SLOTS} slots) ===")
print(f"{'Sym':<14} {'Entry':<11} {'Exit':<11} {'St':>5} {'Entry px':>9} {'Exit px':>9} {'Ret%':>7} {'Status':<7}")
for t in taken:
    print(f"{t['sym']:<14} {t['entry']:<11} {t['exit']:<11} {t['stretch']:>5.1f} "
          f"{t['entry_px']:>9.2f} {t['exit_px']:>9.2f} {t['ret']:>+7.1f} {t['status']:<7}")

# monthly buckets
print("\n=== BY ENTRY MONTH (LIVE config) ===")
bym = {}
for t in taken:
    bym.setdefault(t['entry'][:7], []).append(t['ret'])
for m in sorted(bym):
    rs = bym[m]
    print(f"  {m}: {len(rs)} trades, avg {np.mean(rs):+.1f}%, "
          f"wins {sum(1 for r in rs if r>0)}/{len(rs)}")

json.dump({'raw': raw, 'live_taken': taken},
          open(os.path.join(os.path.dirname(__file__), 'fifty_dec25_results.json'), 'w'), indent=1)
print("\nSaved: 20/fifty_dec25_results.json")
