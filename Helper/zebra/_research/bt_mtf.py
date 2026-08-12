"""Iteration 2: multi-timeframe variants + robustness comparison.

Compares:
  S  = single-TF alignment (validated iter-1): take signal only when that-TF
       ST direction agrees with trade direction (CE & ST-UP / PE & ST-DOWN).
  M1 = monthly-trend gate on weekly-entry, magnet direction.
  M2 = monthly-trend defines direction; weekly pullback is entry timing.

Direction label convention (matches live): CE if price < ST, else PE.
"""
import sys, json
from collections import defaultdict
sys.path.insert(0, r"C:\Users\mail2\Documents\Projects\BOTS\Helper")
sys.path.insert(0, r"C:\Users\mail2\Documents\Projects\BOTS\Helper\zebra\_research")
import bt_engine as E

CACHE = E.CACHE
WATCH = E.WATCH_GAP_MAX; TRIG = E.TRIGGER_GAP_MAX; STALE = E.STALE_GAP_MIN
FRESH_DAYS = E.FRESHNESS_DAYS; FRESH_TOUCH = E.FRESH_TOUCH; HORIZON = E.HORIZON

def build_candidates(sym):
    """Weekly-entry gap-band signals, each tagged with weekly+monthly ST dir."""
    daily = E.load_daily(sym)
    if len(daily) < 320: return []
    wk = E.st_as_of_series(daily, 'weekly')
    mo = E.st_as_of_series(daily, 'monthly')
    out = []
    open_until = -1
    n = len(daily)
    for i in range(n):
        wst, wdir = wk[i]
        mst, mdir = mo[i]
        if not wst or wst <= 0 or not mdir:
            continue
        price = daily[i]['close']
        gap = abs(price - wst) / wst
        if not (STALE <= gap <= TRIG) or i <= open_until:
            continue
        # freshness on weekly ST
        fresh = True
        for k in range(max(0, i-FRESH_DAYS), i):
            pv = wk[k][0]
            if pv and abs(daily[k]['close']-pv)/pv < FRESH_TOUCH:
                fresh = False; break
        if not fresh:
            continue
        direction = 'CE' if price < wst else 'PE'
        out.append(dict(daily=daily, wk=wk, i=i, direction=direction,
                        wst=wst, wdir=wdir, mdir=mdir))
        open_until = i + HORIZON
    return out

def sim(c, atr_mult=2.5):
    daily, i, direction, T = c['daily'], c['i'], c['direction'], c['wst']
    n = len(daily); E.atr_at  # noqa
    Eprice = daily[i]['close']; start = i
    a = E.atr_at(daily, start-1) or Eprice*0.03
    S = Eprice - atr_mult*a if direction == 'CE' else Eprice + atr_mult*a
    risk = abs(Eprice - S)
    if risk <= 0: return None
    end = min(start+HORIZON, n-1)
    for k in range(start, end+1):
        hi, lo, cl = daily[k]['high'], daily[k]['low'], daily[k]['close']
        if direction == 'CE':
            if lo <= S: return _r(c, -1.0)
            if hi >= T: return _r(c, (T-Eprice)/risk)
        else:
            if hi >= S: return _r(c, -1.0)
            if lo <= T: return _r(c, (Eprice-T)/risk)
    cl = daily[end]['close']
    return _r(c, (cl-Eprice)/risk if direction=='CE' else (Eprice-cl)/risk)

def _r(c, R):
    ed = c['daily'][c['i']]['date']
    return E.Trade(None, 'weekly', c['direction'], ed, R, '', ed[:4], E.regime_at(ed))

def stats(ts):
    Rs=[t.R for t in ts]
    if not Rs: return None
    w=[x for x in Rs if x>0]; neg=-sum(x for x in Rs if x<0)
    yrs=defaultdict(list)
    for t in ts: yrs[t.year].append(t.R)
    pos=sum(1 for y in yrs.values() if sum(y)>0); worst=min((sum(y) for y in yrs.values()),default=0)
    return dict(n=len(Rs),win=100*len(w)/len(Rs),totR=sum(Rs),exp=sum(Rs)/len(Rs),
                pf=(sum(w)/neg if neg else 99),posy=pos,ny=len(yrs),worst=worst)

def line(name, ts):
    s=stats(ts)
    if not s: print(f"{name:30s} no trades"); return
    print(f"{name:30s} n={s['n']:4d} win={s['win']:3.0f}% totR={s['totR']:+6.0f} "
          f"exp={s['exp']:+.3f} PF={s['pf']:.2f} +yrs {s['posy']}/{s['ny']} worst {s['worst']:+.0f}")

if __name__ == '__main__':
    uni = E.load_universe()
    allc = []
    for sym in uni:
        allc.extend(build_candidates(sym))
    print(f"weekly gap-band candidates: {len(allc)}\n")

    def filt(mode):
        out=[]
        for c in allc:
            d, wdir, mdir = c['direction'], c['wdir'], c['mdir']
            if mode == 'none':
                pass
            elif mode == 'S':   # single-TF: weekly dir agrees
                if d=='CE' and wdir!='UP': continue
                if d=='PE' and wdir!='DOWN': continue
            elif mode == 'M1':  # monthly trend agrees with magnet direction
                if d=='CE' and mdir!='UP': continue
                if d=='PE' and mdir!='DOWN': continue
            elif mode == 'M1b': # both weekly AND monthly agree
                if d=='CE' and not (wdir=='UP' and mdir=='UP'): continue
                if d=='PE' and not (wdir=='DOWN' and mdir=='DOWN'): continue
            r=sim(c)
            if r: out.append(r)
        return out

    for mode,label in [('none','none (pure magnet)'),('S','S: weekly-aligned'),
                       ('M1','M1: monthly-trend gate'),('M1b','M1b: weekly+monthly both')]:
        ts=filt(mode)
        line(label, ts)
        # walk-forward
        h1=[t for t in ts if t.year<='2021']; h2=[t for t in ts if t.year>='2022']
        line(f"   {label[:6]} 2018-21", h1)
        line(f"   {label[:6]} 2022-26", h2)
        print()
