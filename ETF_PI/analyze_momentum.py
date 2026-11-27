"""analyze_momentum.py — pre-entry momentum study (2026-09-25)

Question: does the speed/acceleration with which premium arrives in the 5-7% entry zone
predict whether it reaches the signal-TF ST (the magnet)?
Forward (out-of-sample) data from 2026-09-26: option_buy.py MOMENTUM shadow stamps
mom_arm / mom_zone / mom_entry on each arm in logs/confirm_shadow.jsonl, and prints
'MOMENTUM ENTRY' / 'MOMENTUM ZONE' lines. This script rebuilds the same features from the
per-minute price lines, so it covers the in-sample period (08-04→09-25) as well.
Usage: python analyze_momentum.py [YYYY-MM-DD start]   (default 2026-08-04)

Original docstring: pre-entry momentum study: does the speed/acceleration with which premium arrives
in the 5-7% entry zone predict whether it reaches the signal-TF ST (the magnet)?

Source: per-minute LTP SCAN lines in logs/option_buy_<date>.log (Aug 4 -> Sep 25).
Line: 'HH:MM:SS | INFO    |   CE 23250 LTP=150.65 | 10M:17.9%↓ST | 15M:...'
A TF is absent when premium is above that ST.
"""
import re, glob, os, json, sys
from collections import defaultdict

ROOT = r"C:\Users\mail2\Documents\Python\Analysis\ETF_PI"
# ATM line:    '10:05:04 | INFO    |   CE 23250 LTP=...'  (log prefix '| ' + message '  CE')
# Sticky line: '10:05:04 | INFO    |   STICKY CE 23300 LTP=...' (from 2026-09-26)
LINE = re.compile(r"^(\d\d):(\d\d):\d\d \| INFO +\|   (STICKY )?(CE|PE) (\d+) LTP=([\d.]+) \|(.*)$")
TFS = re.compile(r"(\d+[MH]):([\d.]+)%↓ST")
ZONE_LO, ZONE_HI = 5.0, 7.0
HORIZON = 45          # minutes to reach ST
KNIFE = 8.0           # % premium drop from arrival = knife (ADV_SL drift analogue)
EOD = 15 * 60 + 25
START = sys.argv[1] if len(sys.argv) > 1 else "2026-08-04"

def load():
    series = defaultdict(dict)   # (date, side, strike, tf) -> {minute: (ltp, gap)}
    ltp_s = defaultdict(dict)    # (date, side, strike) -> {minute: ltp}
    for f in sorted(glob.glob(os.path.join(ROOT, "logs", "option_buy_20*.log"))):
        date = f[-14:-4]
        if date < START:
            continue
        for ln in open(f, encoding="utf-8", errors="replace"):
            m = LINE.match(ln)
            if not m:
                continue
            hh, mm, sticky, side, strike, ltp, rest = m.groups()
            t = int(hh) * 60 + int(mm)
            ltp = float(ltp)
            ltp_s[(date, side, int(strike))][t] = ltp
            present = dict(TFS.findall(rest))
            for tf in ("10M", "15M"):
                if tf in present:
                    gap = float(present[tf])
                elif sticky:
                    # A STICKY line omits a TF because it is not (or no longer) in the watched
                    # levels — swept after entry, never recorded — NOT because premium crossed
                    # ST. Scoring that as -1 would invent "reached ST" hits.
                    continue
                else:
                    gap = -1.0    # LTP SCAN omits a TF whose ST is not DOWN = premium above ST
                series[(date, side, int(strike), tf)][t] = (ltp, gap)
    return series, ltp_s

def pct(a, b):
    return (a / b - 1) * 100 if b else None

def events(series):
    out = []
    for key, s in series.items():
        ts = sorted(s)
        last_evt = -999
        for i, t in enumerate(ts):
            if t < 9 * 60 + 25 or t > 15 * 60:
                continue
            ltp, gap = s[t]
            prev = s.get(t - 1)
            if not prev or not (ZONE_LO <= gap <= ZONE_HI):
                continue
            pg = prev[1]
            if ZONE_LO <= pg <= ZONE_HI or t - last_evt < 20:
                continue
            origin = "below" if pg > ZONE_HI else "above"   # below = rising toward ST
            def back(k):
                return s.get(t - k)
            b5, b10, b3 = back(5), back(10), back(3)
            if not (b5 and b10):
                continue
            v5 = pct(ltp, b5[0]) / 5          # premium %/min over last 5
            v10 = pct(ltp, b10[0]) / 10
            acc = v5 - pct(b5[0], b10[0]) / 5  # last-5 speed minus prior-5 speed
            g5 = (b5[1] - gap) / 5 if b5[1] >= 0 else None   # gap closing pts/min
            # outcome
            hit = None; knife = False; mfe = 0.0; mae = 0.0
            for k in range(1, HORIZON + 1):
                x = s.get(t + k)
                if t + k > EOD:
                    break
                if not x:
                    continue
                r = pct(x[0], ltp)
                mfe = max(mfe, r); mae = min(mae, r)
                if r <= -KNIFE and hit is None:
                    knife = True
                if x[1] <= 0 and hit is None and not knife:
                    hit = k
            out.append(dict(key=key, date=key[0], side=key[1], tf=key[3], t=t, ltp=ltp, gap=gap,
                            origin=origin, v5=v5, v10=v10, acc=acc, g5=g5,
                            hit=hit, knife=knife, mfe=mfe, mae=mae))
            last_evt = t
    return out

def summarize(ev, label, fn):
    groups = defaultdict(list)
    for e in ev:
        g = fn(e)
        if g is not None:
            groups[g].append(e)
    print(f"\n--- {label} ---")
    print(f"{'bucket':<22}{'n':>5}{'hitST%':>8}{'knife%':>8}{'medMFE':>8}{'medMAE':>8}{'medTTH':>8}")
    for g in sorted(groups):
        L = groups[g]; n = len(L)
        h = [e for e in L if e["hit"]]
        med = lambda a: sorted(a)[len(a) // 2] if a else float("nan")
        print(f"{str(g):<22}{n:>5}{100*len(h)/n:>7.0f}%{100*sum(e['knife'] for e in L)/n:>7.0f}%"
              f"{med([e['mfe'] for e in L]):>8.1f}{med([e['mae'] for e in L]):>8.1f}{med([e['hit'] for e in h]):>8}")

def bucket(x, edges):
    for e in edges:
        if x < e:
            return f"<{e}"
    return f">={edges[-1]}"

if __name__ == "__main__":
    series, ltp_s = load()
    ev = events(series)
    print("events", len(ev), "days", len({e['date'] for e in ev}))
    summarize(ev, "ALL", lambda e: "all")
    summarize(ev, "origin (below = premium rising into zone)", lambda e: e["origin"])
    summarize(ev, "TF", lambda e: e["tf"])
    summarize(ev, "v5 premium %/min", lambda e: bucket(e["v5"], [-1, -0.3, 0, 0.3, 1]))
    summarize(ev, "origin x v5", lambda e: (e["origin"], bucket(e["v5"], [-0.3, 0.3])))
    summarize(ev, "acc (v5 - prior v5)", lambda e: bucket(e["acc"], [-0.5, -0.1, 0.1, 0.5]))
    summarize(ev, "origin=below x acc", lambda e: bucket(e["acc"], [-0.3, 0, 0.3]) if e["origin"] == "below" else None)
    summarize(ev, "month", lambda e: e["date"][:7])
