"""Joiner-trait step 1: DESCRIPTIVE profile only (JOINER_TRAIT_DESIGN.md).  Prints NO R and NO P&L.
Daily bars only (full_raw + surv_raw).  The one use of row files is a COUNT of K1 PRE-JOIN trades by distance to join.
    python -I joiner_profile.py
"""
from __future__ import annotations

import bisect
import gzip
import json
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

SRS = Path(__file__).resolve().parent  # Pi portability (was the PC path)
sys.path.insert(0, str(SRS))
import de_kill as K  # noqa: E402

V, F = K.V, K.F
OFFSETS = (24, 18, 12, 9, 6, 3, 2, 1)
START = "2020-01"


def madd(m: str, k: int) -> str:
    y, mo = int(m[:4]), int(m[5:7]) - 1 + k
    return f"{y + mo // 12:04d}-{mo % 12 + 1:02d}"


def mdiff(a: str, b: str) -> int:
    return (int(a[:4]) - int(b[:4])) * 12 + int(a[5:7]) - int(b[5:7])


def load_daily(s: str):
    f = V.data_file(s)
    if f is None:
        return None
    with gzip.open(f, "rt") as g:
        x = json.load(g)["daily"]
    rows = sorted({r[0]: r for r in x}.values(), key=lambda r: r[0])
    return [dict(d=r[0], o=r[1], h=r[2], l=r[3], c=r[4], v=r[5]) for r in rows]


def q(vals, p):
    v = sorted(vals)
    if not v:
        return None
    i = p * (len(v) - 1)
    lo = int(i)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def fq(vals, fmt="{:.2f}"):
    if not vals:
        return "n 0"
    return f"n {len(vals):3d}  " + " / ".join(fmt.format(q(vals, p)) for p in (.25, .5, .75))


def metrics(bars, dates, atr, month):
    """Metrics as of the last bar dated strictly before month-01.  None if that bar is not in the previous
    calendar month (review 2026-11-21: the old docstring said "15 days"; the code, unchanged, allows ~1 month)."""
    i = bisect.bisect_left(dates, month + "-01") - 1
    if i < 0:
        return None
    last = dates[i]
    if mdiff(month, last[:7]) > 1:
        return None
    out = dict(nbars=i + 1)
    tv = [b["c"] * b["v"] for b in bars[max(0, i - 59):i + 1]]
    vol = [b["v"] for b in bars[max(0, i - 59):i + 1]]
    if len(tv) >= 40:
        out["T60"] = st.median(tv) / 1e7
    if i - 126 - 59 >= 0 and "T60" in out:
        t0 = st.median([b["c"] * b["v"] for b in bars[i - 126 - 59:i - 126 + 1]])
        if t0 > 0:
            out["Tgrow6"] = out["T60"] * 1e7 / t0
        v0 = st.median([b["v"] for b in bars[i - 126 - 59:i - 126 + 1]])
        if v0 > 0:
            out["Vgrow6"] = st.median(vol) / v0
    if atr[i] is not None and bars[i]["c"] > 0:
        out["ATR"] = atr[i] / bars[i]["c"] * 100
    for name, n in (("ret3", 63), ("ret6", 126), ("ret12", 252)):
        if i - n >= 0 and bars[i - n]["c"] > 0:
            out[name] = (bars[i]["c"] / bars[i - n]["c"] - 1) * 100
    return out


def pct_in(x, pool):
    if not pool:
        return None
    return 100.0 * sum(1 for p in pool if p < x) / len(pool)


def main() -> None:
    mem = V.load_membership()
    rmap = json.loads(V.MAP_FILE.read_text())
    sets = V.eligible_sets(mem, rmap)
    months = sorted(sets)
    months_of = K.member_months(sets)
    first = {s: ms[0] for s, ms in months_of.items()}
    last_month = months[-1]

    # ---- joiner set
    raw_first = {}
    for m in months:
        for x in mem[m]:
            raw_first.setdefault(x, m)
    gone_join = sorted(x for x, m in raw_first.items() if m > START and V.resolve(x, rmap) is None)
    joiners = sorted(s for s, m in first.items() if m > START)
    print(f"months {months[0]}..{months[-1]}; members first month {len(sets[months[0]])}, last {len(sets[last_month])}")
    print(f"resolved ever-members {len(first)}; founders (member 2020-01) {sum(1 for m in first.values() if m == START)}; "
          f"JOINERS (first member month > 2020-01) {len(joiners)}; raw joiner symbols that are GONE {gone_join}")
    print("joiners per join month:", dict(sorted(Counter(first[s] for s in joiners).items())))
    print("joiners per join year:", dict(sorted(Counter(first[s][:4] for s in joiners).items())))
    left = [s for s in joiners if months_of[s][-1] < last_month]
    print(f"joiners that later LEFT F&O (final member month < {last_month}): {len(left)} {left}")
    spell = [s for s in joiners if len(months_of[s]) != mdiff(months_of[s][-1], months_of[s][0]) + 1]
    print(f"joiners with a broken spell (left and re-joined): {len(spell)} {spell}")

    daily = {}
    for s in sorted(first):
        b = load_daily(s)
        if b:
            daily[s] = b
    print(f"data symbols loaded {len(daily)}; joiners without data {[s for s in joiners if s not in daily]}")
    dates = {s: [b["d"] for b in bars] for s, bars in daily.items()}
    atrs = {s: K.F.N.wilder_atr(bars) for s, bars in daily.items()}

    # listing age at join: first Kite bar later than 2019-10-15 => listed (or data starts) after the data start
    young = {s: dates[s][0] for s in joiners if s in dates and dates[s][0] > "2019-10-15"}
    age_m = {s: mdiff(first[s], d0[:7]) for s, d0 in young.items()}
    print(f"joiners whose first Kite bar is after 2019-10-15 (IPO / demerger / new symbol): {len(young)}; "
          f"of these joined within 12 months of the first bar: {sum(1 for v in age_m.values() if v <= 12)}, "
          f"within 6: {sum(1 for v in age_m.values() if v <= 6)}")
    print("  months from first bar to join, young joiners:", fq(list(age_m.values()), "{:.0f}"))

    # ---- metric table per month for every data symbol
    all_months = [madd(START, k) for k in range(-1, len(months) + 1)]
    M = {m: {} for m in all_months}
    for s in daily:
        for m in all_months:
            x = metrics(daily[s], dates[s], atrs[s], m)
            if x:
                M[m][s] = x

    def incumbents(xm: str):
        """Members of month xm that were already members before xm (or founders)."""
        if xm not in sets:
            return None
        return {s for s in sets[xm] if first[s] < xm or first[s] == START}

    keys = ("T60", "Tgrow6", "Vgrow6", "ATR", "ret3", "ret6", "ret12")
    print("\nTABLE A: joiner raw values at k months before the first member month (snapshot = last session before "
          "month J-k+1). q1 / median / q3")
    print("TABLE B: joiner PERCENTILE within incumbent F&O members at the same snapshot. q1 / median / q3")
    print("TABLE C: incumbent median raw value at the same snapshots (median over joiner-snapshots)")
    A = defaultdict(lambda: defaultdict(list))
    B = defaultdict(lambda: defaultdict(list))
    C = defaultdict(lambda: defaultdict(list))
    ratio = defaultdict(list)
    cover = {}
    for k in OFFSETS:
        have = 0
        for s in joiners:
            xm = madd(first[s], -(k - 1))
            inc = incumbents(xm)
            x = M.get(xm, {}).get(s)
            if x is None or inc is None:
                continue
            have += 1
            for key in keys:
                if key not in x:
                    continue
                pool = [M[xm][t][key] for t in inc if t in M[xm] and key in M[xm][t] and t != s]
                if len(pool) < 50:
                    continue
                A[k][key].append(x[key])
                B[k][key].append(pct_in(x[key], pool))
                C[k][key].append(st.median(pool))
                if key == "T60":
                    ratio[k].append(x[key] / q(pool, .10))
        cover[k] = have
    print("joiners with a snapshot per offset:", cover, "of", len(joiners))
    fmts = dict(T60="{:.0f}", Tgrow6="{:.2f}", Vgrow6="{:.2f}", ATR="{:.2f}", ret3="{:+.0f}", ret6="{:+.0f}",
                ret12="{:+.0f}")
    for name, tab, f in (("A raw", A, None), ("B pct", B, "{:.0f}"), ("C incumbent median", C, None)):
        print(f"\n--- TABLE {name}")
        for key in keys:
            for k in OFFSETS:
                print(f"  {key:7s} k=-{k:2d}  {fq(tab[k][key], f or fmts[key])}")
    print("\n--- joiner T60 / incumbent 10th-percentile T60")
    for k in OFFSETS:
        print(f"  k=-{k:2d}  {fq(ratio[k])}")

    # ---- by join era (non-stationarity): T60 percentile and ret6 at k=6 and k=3
    print("\n--- by join era, percentile within incumbents, q1 / median / q3")
    for era, sel in (("join 2020-2023", lambda m: m <= "2023-12"), ("join 2024-2026", lambda m: m > "2023-12")):
        for k in (12, 6, 3):
            for key in ("T60", "Tgrow6", "ATR", "ret6"):
                vals = []
                for s in joiners:
                    if not sel(first[s]):
                        continue
                    xm = madd(first[s], -(k - 1))
                    inc = incumbents(xm)
                    x = M.get(xm, {}).get(s)
                    if x is None or inc is None or key not in x:
                        continue
                    pool = [M[xm][t][key] for t in inc if t in M[xm] and key in M[xm][t]]
                    if len(pool) >= 50:
                        vals.append(pct_in(x[key], pool))
                print(f"  {era} k=-{k:2d} {key:7s} {fq(vals, '{:.0f}')}")

    # ---- crude 2x2 INSIDE the held (future-selected) set: candidate trait vs 'joins within 12 months'
    print("\n--- 2x2 inside the 291 held stocks (ALL of them ever-F&O: biased; shape only). Unit = non-member "
          "stock-month with a snapshot. Trait = T60 >= incumbents' p-th percentile [and Tgrow6 >= g]")
    for p, g in ((.10, None), (.25, None), (.10, 1.5), (.25, 1.5), (.10, 2.0)):
        cnt = Counter()
        for xm in months:
            inc = incumbents(xm)
            pool = [M[xm][t]["T60"] for t in inc if t in M[xm] and "T60" in M[xm][t]]
            if len(pool) < 50:
                continue
            thr = q(pool, p)
            for s, x in M[xm].items():
                if s in sets[xm] or "T60" not in x:
                    continue
                if g is not None and "Tgrow6" not in x:
                    continue
                if s in first and first[s] > xm:
                    grp = "join<=12m" if mdiff(first[s], xm) <= 12 else "join>12m"
                else:
                    grp = "ex-member"
                pos = x["T60"] >= thr and (g is None or x["Tgrow6"] >= g)
                cnt[(grp, pos)] += 1
        line = f"  p={p:.2f} g={g}: "
        for grp in ("join<=12m", "join>12m", "ex-member"):
            a, b = cnt[(grp, True)], cnt[(grp, False)]
            line += f"{grp} trait+ {a} / {a + b} ({(a / (a + b) if a + b else 0):.0%});  "
        print(line)

    # ---- K1 PRE-JOIN trades by distance to join: COUNTS ONLY (no R is read or printed)
    try:
        by = V.load_rows(V.biased_files(K.SRS_ROWS218))
        days = [d for d in sorted(by) if K.FIRST_DAY <= d <= K.LAST_DAY]
        trades, _, _ = V.select_trades(by, days)
        buckets = ((1, 1), (2, 3), (4, 6), (7, 12), (13, 24), (25, 999))
        cnt = Counter()
        syms = defaultdict(set)
        side = Counter()
        for t in trades:
            d, s = t[0], t[1]
            if K.biased_group(s, d, months_of) != "PRE-JOIN":
                continue
            dist = mdiff(first[s], d[:7])
            b = next(f"{a}-{z}" for a, z in buckets if a <= dist <= z)
            w = "IS" if d <= K.IS_END else "OOS"
            cnt[(b, w)] += 1
            syms[(b, w)].add(s)
            side[(w, "LONG" if t[2] > 0 else "SHORT")] += 1
        print("\n--- K1 BIASED PRE-JOIN top-10 SRS trades by months before the first member month: COUNT (stocks)")
        for a, z in buckets:
            b = f"{a}-{z}"
            print(f"  {b:7s} IS {cnt[(b, 'IS')]:5d} ({len(syms[(b, 'IS')]):3d})   OOS {cnt[(b, 'OOS')]:5d} "
                  f"({len(syms[(b, 'OOS')]):3d})")
        print("  total", sum(cnt.values()), "side counts:", dict(side))
        oos_by_join = Counter()
        for t in trades:
            if t[0] > K.IS_END and K.biased_group(t[1], t[0], months_of) == "PRE-JOIN":
                oos_by_join[first[t[1]]] += 1
        print("  OOS PRE-JOIN trade count by the stock's join month:", dict(sorted(oos_by_join.items())))
    except Exception as e:                                  # rows cache may be wiped: say so, do not guess
        print("K1 count skipped:", type(e).__name__, e)


if __name__ == "__main__":
    main()
