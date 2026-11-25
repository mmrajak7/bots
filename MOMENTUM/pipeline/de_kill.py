"""Double-engine free kill tests K0-K2 (DOUBLE_ENGINE_DESIGN.md §0.5, frozen with the OWNER ANSWERS 2026-11-21).

    python -I de_kill.py counts    # K0 + pre-run checks: COUNTS / SHARES ONLY (no R, no Rs) -- allowed before review
    python -I de_kill.py report    # K1 + K2 + verdict (prints R) -- ONLY after the independent review

Data: the EXISTING point-in-time F&O SRS rows (full_rows_srs + surv_rows_srs), eligibility exactly as
survivorship.make_eligible (fo_membership.json + fo_rename_map.json).  The slow rank uses the Kite adjusted daily
bars already on disk (full_raw / surv_raw via survivorship.data_file + full_asrs.load_stock).  No download, no fetch.

Frozen choices implemented here (design §0.5; see the module constants):
- Rank date: month m is ranked on daily bars dated STRICTLY BEFORE the first day of m (no look-ahead).
- Window W: the last 63 MARKET sessions before m (market calendar = union of the daily dates of every loaded
  data symbol).  r(t) = ln(close(t) / close of the stock's previous bar), for every bar of the stock inside W.
  A stock needs >= 55 such returns, else it is UNRANKED that month (counted, never dropped silently).
  MOVE = |sum r|, VOL = median (high - low) / close over its bars in W, ER = |sum r| / sum |r| (0 if sum |r| = 0).
- Terciles per month over the month's RANKED resolved members: sort ascending by (value, symbol); position i of n
  -> tercile 1 + 3*i // n (T1 low .. T3 high).
- K0: share of eligible PICK-FLAGGED picks per MOVE tercile.  OVERLAP iff the T3 share > 70% over ranked picks OR
  over all eligible picks (errs toward not killing).
- K2: ALL eligible TRIGGERED picks (no top-10 cut), SRS, both sides.  D = mean R(T3) - mean R(T1); paired day-block
  bootstrap 95% CI (5,000 draws, seed 7).  Verdict = verdict() below.  VOL / ER / by-side are printed, never read.
- K1: report only, no verdict (real survivorship.select_trades top-10 for BIASED and PIT).
"""
from __future__ import annotations

import bisect
import json
import math
import random
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_argv = sys.argv[1:]                         # survivorship truncates sys.argv at import: capture ours first
import survivorship as V  # noqa: E402

F, Y = V.F, V.Y

W_SESSIONS = 63                              # §1.2: the last 63 sessions to the rebalance date
MIN_RETURNS = 55                             # §1.2 / §0.5: >= 55 traded days needed
RULES = ("MOVE", "VOL", "ER")                # MOVE read; VOL / ER printed only
READ_RULE = "MOVE"
MIN_TERCILE_PICKS = 2000                     # floor: T1 or T3 below this -> never KILL (INCONCLUSIVE)
OVERLAP_SHARE = 0.70                         # K0: T3 share above this -> OVERLAP
BOOT_N, BOOT_SEED = 5000, 7
FIRST_DAY, LAST_DAY = "2020-01-01", F.LAST_DAY
IS_END = F.IS_END                            # IS 2020-01-01..2023-12-31, OOS 2024-01-01..2026-11-19
SRS_ROWS218, SRS_ROWSNEW = V.SETUPS[0][2], V.SETUPS[0][3]
if V.SETUPS[0][0] != "SRS":
    raise RuntimeError("survivorship.SETUPS[0] is no longer SRS")
OUT_FILE = V.OUT / "de_kill.txt"
WINDOWS: Tuple[Tuple[str, Callable[[str], bool]], ...] = (
    ("ALL", lambda d: True), ("IS 2020-2023", lambda d: d <= IS_END), ("OOS 2024-2026", lambda d: d > IS_END))
UNRANKED = 0                                 # tercile code of an eligible member that could not be ranked


# ---------------------------------------------------------------- slow rank (no look-ahead)
def market_calendar(daily_by_sym: Dict[str, List[dict]]) -> List[str]:
    return sorted({b["d"] for bars in daily_by_sym.values() for b in bars})


def window(cal: List[str], month: str) -> List[str]:
    """The last W_SESSIONS market sessions dated strictly before month-01."""
    j = bisect.bisect_left(cal, month + "-01")
    return cal[max(0, j - W_SESSIONS):j]


def slow_stats(bars: List[dict], dates: List[str], win: List[str]) -> Tuple[Optional[dict], int]:
    """(stats or None, number of returns) for one stock over window win.  bars sorted by date, dates = their dates.
    Only bars dated inside win (all strictly before the month) are used; the first bar's previous bar may lie just
    before win (that is the return INTO the window's first session)."""
    if not win:
        return None, 0
    lo = bisect.bisect_left(dates, win[0])
    hi = bisect.bisect_right(dates, win[-1])
    wset = set(win)
    rets: List[float] = []
    ranges: List[float] = []
    for i in range(lo, hi):
        b = bars[i]
        if b["d"] not in wset:
            continue
        if b["c"] > 0:
            ranges.append((b["h"] - b["l"]) / b["c"])
        if i > 0 and bars[i - 1]["c"] > 0 and b["c"] > 0:
            rets.append(math.log(b["c"] / bars[i - 1]["c"]))
    if len(rets) < MIN_RETURNS:
        return None, len(rets)
    s, a = sum(rets), sum(abs(r) for r in rets)
    return dict(MOVE=abs(s), VOL=st.median(ranges), ER=(abs(s) / a if a > 0 else 0.0)), len(rets)


def terciles(values: Dict[str, float]) -> Dict[str, int]:
    """Ascending by (value, symbol); position i of n -> 1 + 3*i // n."""
    order = sorted(values, key=lambda s: (values[s], s))
    n = len(order)
    return {s: 1 + (3 * i) // n for i, s in enumerate(order)}


def month_terciles(sets: Dict[str, set], daily_by_sym: Dict[str, List[dict]], cal: List[str]):
    """-> (terc {month: {rule: {sym: 1..3}}}, unranked {month: {sym: reason}}, wlen {month: sessions in W})."""
    dates_of = {s: [b["d"] for b in bars] for s, bars in daily_by_sym.items()}
    terc: Dict[str, Dict[str, Dict[str, int]]] = {}
    unranked: Dict[str, Dict[str, str]] = {}
    wlen: Dict[str, int] = {}
    for m in sorted(sets):
        win = window(cal, m)
        wlen[m] = len(win)
        vals: Dict[str, Dict[str, float]] = {r: {} for r in RULES}
        un: Dict[str, str] = {}
        for s in sorted(sets[m]):
            if s not in daily_by_sym:
                un[s] = "no data file"
                continue
            x, n = slow_stats(daily_by_sym[s], dates_of[s], win)
            if x is None:
                un[s] = f"short window ({n} < {MIN_RETURNS} returns)"
                continue
            for r in RULES:
                vals[r][s] = x[r]
        terc[m] = {r: (terciles(vals[r]) if vals[r] else {}) for r in RULES}
        unranked[m] = un
    return terc, unranked, wlen


# ---------------------------------------------------------------- picks
def classify_picks(by_day: Dict[str, List[Tuple[str, list]]], days: List[str], eligible: Callable[[str, str], bool],
                   terc: Dict[str, Dict[str, Dict[str, int]]], unranked: Dict[str, Dict[str, str]]) -> List[dict]:
    """Every eligible PICK-FLAGGED row of the window -> dict(d, s, side, r, trig, MOVE/VOL/ER tercile, 0 = UNRANKED).
    trig = triggered AND sizeable (the same condition survivorship.select_trades uses to make a trade)."""
    out = []
    for d in days:
        m = d[:7]
        for s, r in by_day.get(d, []):
            if not r[1] or not eligible(s, d):
                continue
            ranked = s in terc[m][READ_RULE]
            if not ranked and s not in unranked[m]:
                raise ValueError(f"{s} eligible on {d} but neither ranked nor unranked")
            trig = r[3] != 0 and F.size(r[4], r[5]) > 0
            p = dict(d=d, s=s, side=r[3], r=r[8], trig=trig)
            for rule in RULES:
                p[rule] = terc[m][rule].get(s, UNRANKED)
            out.append(p)
    return out


def bucket_counts(picks: List[dict], rule: str, sel: Callable[[str], bool], trig_only: bool) -> Dict[int, int]:
    c = {0: 0, 1: 0, 2: 0, 3: 0}
    for p in picks:
        if sel(p["d"]) and (p["trig"] or not trig_only):
            c[p[rule]] += 1
    return c


def overlap(c: Dict[int, int]) -> Tuple[bool, float, float]:
    """(OVERLAP?, T3 share of ranked picks, T3 share of all eligible picks).  Strictly above 70% trips it."""
    ranked = c[1] + c[2] + c[3]
    tot = ranked + c[0]
    s_r = c[3] / ranked if ranked else 0.0
    s_a = c[3] / tot if tot else 0.0
    return (s_r > OVERLAP_SHARE or s_a > OVERLAP_SHARE), s_r, s_a


# ---------------------------------------------------------------- statistics
def mean_or_none(v: Sequence[float]) -> Optional[float]:
    return st.mean(v) if v else None


def boot_diff(hi_pairs: List[Tuple[str, float]], lo_pairs: List[Tuple[str, float]], n: int = BOOT_N,
              seed: int = BOOT_SEED) -> Tuple[float, float]:
    """Paired day-block bootstrap of mean(hi) - mean(lo): each draw resamples the union of days once (with
    replacement) and takes both means on the SAME resampled days.  Percentiles as y2026_asrs.boot_ci.  A draw that
    leaves a group empty is an error (cannot happen at real sample sizes; loud, never skipped)."""
    if not hi_pairs or not lo_pairs:
        raise ValueError("boot_diff needs both groups non-empty")
    agg: Dict[str, List[float]] = defaultdict(lambda: [0.0, 0, 0.0, 0])
    for d, v in hi_pairs:
        agg[d][0] += v
        agg[d][1] += 1
    for d, v in lo_pairs:
        agg[d][2] += v
        agg[d][3] += 1
    days = [agg[d] for d in sorted(agg)]
    rng = random.Random(seed)
    k = len(days)
    ms = []
    for _ in range(n):
        sh = nh = sl = nl = 0.0
        for _ in range(k):
            x = days[rng.randrange(k)]
            sh += x[0]
            nh += x[1]
            sl += x[2]
            nl += x[3]
        if nh == 0 or nl == 0:
            raise ValueError("bootstrap draw with an empty group")
        ms.append(sh / nh - sl / nl)
    ms.sort()
    return ms[int(.025 * n)], ms[int(.975 * n) - 1]


def verdict(d_all: Optional[float], ci_lo: Optional[float], d_oos: Optional[float], overlap_flag: bool,
            n_t1: int, n_t3: int) -> Tuple[str, List[str]]:
    """Frozen K2 rule.  The floor (OVERLAP, or T1 / T3 < 2,000 triggered picks on ALL) makes the verdict
    INCONCLUSIVE whatever D says.  PROCEED: D > 0, CI low > 0, D OOS > 0.  KILL: D <= 0 and D OOS <= 0."""
    why: List[str] = []
    if overlap_flag:
        why.append("floor: K0 OVERLAP")
    if n_t1 < MIN_TERCILE_PICKS:
        why.append(f"floor: T1 has {n_t1} < {MIN_TERCILE_PICKS} triggered picks")
    if n_t3 < MIN_TERCILE_PICKS:
        why.append(f"floor: T3 has {n_t3} < {MIN_TERCILE_PICKS} triggered picks")
    if why:
        return "INCONCLUSIVE", why
    if d_all is None or ci_lo is None or d_oos is None:
        return "INCONCLUSIVE", ["D undefined on ALL or OOS (empty tercile)"]
    if d_all > 0 and ci_lo > 0 and d_oos > 0:
        return "PROCEED", ["D > 0, CI low > 0, D OOS > 0"]
    if d_all <= 0 and d_oos <= 0:
        return "KILL", ["D <= 0 on ALL and on OOS"]
    return "INCONCLUSIVE", ["neither PROCEED nor KILL"]


# ---------------------------------------------------------------- K1 groups (report only)
def member_months(sets: Dict[str, set]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = defaultdict(list)
    for m in sorted(sets):
        for s in sets[m]:
            out[s].append(m)
    return dict(out)


def biased_group(s: str, d: str, months_of: Dict[str, List[str]]) -> str:
    """MEMBER (resolved symbol is a member of month(d)), PRE-JOIN (before its first member month), GAP (not a
    member, but was one earlier: between spells), NEVER (never a member)."""
    ms = months_of.get(s, [])
    m = d[:7]
    if m in ms:
        return "MEMBER"
    if not ms:
        return "NEVER"
    return "PRE-JOIN" if m < ms[0] else "GAP"


def pit_group(s: str, d: str, months_of: Dict[str, List[str]], last_month: str) -> str:
    """LEAVER-LAST12 iff the resolved symbol's FINAL member month is before last_month (it left and did not come
    back) and month(d) is one of its last 12 member months (across spells).  Everything else: REST."""
    ms = months_of.get(s, [])
    if ms and ms[-1] < last_month and d[:7] in ms[-12:]:
        return "LEAVER-LAST12"
    return "REST"


# ---------------------------------------------------------------- loading
def load_daily(syms: List[str]) -> Tuple[Dict[str, List[dict]], List[str]]:
    """Daily bars of each data symbol from its raw file (full_raw first, then surv_raw: survivorship.data_file).
    -> (daily_by_sym, symbols without a raw file)."""
    out, missing = {}, []
    for s in sorted(syms):
        f = V.data_file(s)
        if f is None:
            missing.append(s)
            continue
        daily, _ = F.load_stock(s, raw=f.parent, y2026=False)
        out[s] = daily
    return out, missing


def load_context(with_biased: bool = False) -> dict:
    mem = V.load_membership()
    rmap = json.loads(V.MAP_FILE.read_text())
    sets = V.eligible_sets(mem, rmap)
    eligible = V.make_eligible(mem, rmap)
    pit_by = V.load_rows(V.pit_files(SRS_ROWS218, SRS_ROWSNEW, rmap, mem))
    days = [d for d in sorted(pit_by) if FIRST_DAY <= d <= LAST_DAY]
    syms = sorted(set().union(*sets.values()))
    daily, missing = load_daily(syms)
    cal = market_calendar(daily)
    terc, unranked, wlen = month_terciles(sets, daily, cal)
    picks = classify_picks(pit_by, days, eligible, terc, unranked)
    ctx = dict(mem=mem, rmap=rmap, sets=sets, eligible=eligible, pit_by=pit_by, days=days, daily=daily,
               missing=missing, cal=cal, terc=terc, unranked=unranked, wlen=wlen, picks=picks)
    if with_biased:
        ctx["bias_by"] = V.load_rows(V.biased_files(SRS_ROWS218))
    return ctx


# ---------------------------------------------------------------- K0 + pre-run checks (counts only)
def k0_lines(ctx: dict) -> Tuple[List[str], bool]:
    picks, days = ctx["picks"], ctx["days"]
    L = ["K0 (counts only): eligible PIT SRS picks per MOVE tercile (T1 low .. T3 high; U = eligible but unranked)"]
    for kind, trig in (("pick-flagged", False), ("triggered", True)):
        for wn, sel in WINDOWS:
            c = bucket_counts(picks, READ_RULE, sel, trig)
            ov, s_r, s_a = overlap(c)
            L.append(f"  {kind:12s} {wn:13s} T1 {c[1]:6d} T2 {c[2]:6d} T3 {c[3]:6d} U {c[0]:5d} | T3 share "
                     f"{s_r:.1%} of ranked, {s_a:.1%} of all")
    c_all = bucket_counts(picks, READ_RULE, WINDOWS[0][1], False)
    ov = overlap(c_all)[0]
    L.append(f"  K0 decision (pick-flagged, ALL, either share > {OVERLAP_SHARE:.0%}): "
             f"{'OVERLAP' if ov else 'no overlap'}")
    for rule in RULES:
        t = bucket_counts(picks, rule, WINDOWS[0][1], True)
        L.append(f"  triggered picks per {rule} tercile, ALL: T1 {t[1]} T2 {t[2]} T3 {t[3]} U {t[0]}")

    L.append("PRE-RUN CHECKS (counts / booleans only)")
    ref = sum(V.select_trades(ctx["pit_by"], days, ctx["eligible"])[2].values())
    tot = sum(c_all.values())
    L.append(f"  P-invariant: tercile buckets (incl. U) {tot} == survivorship eligible picks {ref}: {tot == ref}")
    months = sorted({d[:7] for d in days})
    wl = ctx["wlen"]
    other = [wl[m] for m in months if m != "2020-01"]
    L.append(f"  window sessions: 2020-01 {wl.get('2020-01')}, other months min {min(other)} max {max(other)}")
    L.append(f"  market calendar: {len(ctx['cal'])} dates {ctx['cal'][0]}..{ctx['cal'][-1]}; weekend dates "
             f"{sum(_weekday(d) >= 5 for d in ctx['cal'])}; data symbols {len(ctx['daily'])}, without raw file "
             f"{len(ctx['missing'])} {ctx['missing']}")
    sizes = [tuple(sum(1 for v in ctx["terc"][m][READ_RULE].values() if v == k) for k in (1, 2, 3)) for m in months]
    L.append(f"  members per MOVE tercile per month: T1 {min(s[0] for s in sizes)}-{max(s[0] for s in sizes)}, "
             f"T2 {min(s[1] for s in sizes)}-{max(s[1] for s in sizes)}, T3 {min(s[2] for s in sizes)}-"
             f"{max(s[2] for s in sizes)}")
    reasons: Dict[str, int] = defaultdict(int)
    for m in months:
        for why in ctx["unranked"][m].values():
            reasons[why.split(" (")[0]] += 1
    L.append(f"  unranked member-months by reason: {dict(reasons)}")
    bad = [m for m in months if len(ctx["terc"][m][READ_RULE]) + len(ctx["unranked"][m]) != len(ctx["sets"][m])]
    L.append(f"  months where ranked + unranked != resolved members: {len(bad)}")
    untrig_sized = sum(1 for p in picks if not p["trig"] and p["side"] != 0)
    L.append(f"  triggered but unsizeable picks (excluded like select_trades): {untrig_sized}")
    L.append(f"  sessions {len(days)} {days[0]}..{days[-1]}")
    return L, ov


def _weekday(d: str) -> int:
    from datetime import date
    return date(int(d[:4]), int(d[5:7]), int(d[8:10])).weekday()


# ---------------------------------------------------------------- K1 / K2 (prints R: report only)
def r_line(label: str, pairs: List[Tuple[str, float]]) -> str:
    if not pairs:
        return f"{label}: 0"
    lo, hi = Y.boot_ci(pairs, n=BOOT_N, seed=BOOT_SEED)
    return f"{label}: n {len(pairs)}, mean {st.mean(v for _, v in pairs):+.3f}R CI[{lo:+.3f},{hi:+.3f}]"


def k1_lines(ctx: dict) -> List[str]:
    days, months_of = ctx["days"], member_months(ctx["sets"])
    last_month = max(ctx["mem"])
    tb, _, _ = V.select_trades(ctx["bias_by"], days)
    tp, _, _ = V.select_trades(ctx["pit_by"], days, ctx["eligible"])
    L = ["K1 (report only, no verdict): which half of the survivorship gap carried the difference. Top-10 SRS, "
         "0.02% stored costs."]
    for name, trades, grp in (("BIASED", tb, lambda t: biased_group(t[1], t[0], months_of)),
                              ("PIT", tp, lambda t: pit_group(t[1], t[0], months_of, last_month))):
        groups = sorted({grp(t) for t in trades})
        for g in groups:
            for wn, sel in WINDOWS:
                L.append("  " + r_line(f"{name:6s} {g:13s} {wn:13s}",
                                       [(t[0], t[3]) for t in trades if sel(t[0]) and grp(t) == g]))
    return L


def d_stats(picks: List[dict], rule: str, sel: Callable[[str], bool], side: Optional[int] = None) -> dict:
    ps = [p for p in picks if p["trig"] and sel(p["d"]) and (side is None or p["side"] == side)]
    hi = [(p["d"], p["r"]) for p in ps if p[rule] == 3]
    lo = [(p["d"], p["r"]) for p in ps if p[rule] == 1]
    mh, ml = mean_or_none([v for _, v in hi]), mean_or_none([v for _, v in lo])
    d = None if mh is None or ml is None else mh - ml
    ci = boot_diff(hi, lo) if d is not None else (None, None)
    return dict(n1=len(lo), n3=len(hi), d=d, ci=ci, t1=lo, t3=hi,
                t2=[(p["d"], p["r"]) for p in ps if p[rule] == 2])


def _fmt_d(x: dict) -> str:
    if x["d"] is None:
        return "D undefined (empty tercile)"
    return f"D = {x['d']:+.3f}R CI[{x['ci'][0]:+.3f},{x['ci'][1]:+.3f}] (T1 n {x['n1']}, T3 n {x['n3']})"


def k2_lines(ctx: dict, overlap_flag: bool) -> List[str]:
    picks = ctx["picks"]
    L = ["K2 (the kill test): SRS, both sides, ALL eligible triggered picks, D = mean R(T3) - mean R(T1)"]
    read = {}
    for rule in RULES:
        tag = "READ" if rule == READ_RULE else "printed, never read"
        for wn, sel in WINDOWS:
            x = d_stats(picks, rule, sel)
            if rule == READ_RULE:
                read[wn] = x
            L.append(f"  {rule:4s} {wn:13s} {_fmt_d(x)} [{tag}]")
            for k in ("t1", "t2", "t3"):
                L.append("      " + r_line(f"{rule} {k.upper()}", x[k]))
    for side, sn in V.SIDES:
        for wn, sel in WINDOWS:
            L.append(f"  {READ_RULE} {sn:5s} {wn:13s} {_fmt_d(d_stats(picks, READ_RULE, sel, side))} "
                     "[by side, printed, never read]")
    a, o = read["ALL"], read["OOS 2024-2026"]
    v, why = verdict(a["d"], a["ci"][0], o["d"], overlap_flag, a["n1"], a["n3"])
    L.append(f"VERDICT (frozen §0.5): {v} -- {'; '.join(why)}")
    return L


# ---------------------------------------------------------------- commands
def counts() -> None:
    L, _ = k0_lines(load_context())
    print("\n".join(L))


def report() -> None:
    ctx = load_context(with_biased=True)
    L0, ov = k0_lines(ctx)
    L = ["Double-engine kill tests (DOUBLE_ENGINE_DESIGN.md §0.5)"] + L0 + [""] + k1_lines(ctx) + [""] + \
        k2_lines(ctx, ov)
    V.OUT.mkdir(exist_ok=True)
    OUT_FILE.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


COMMANDS = {"counts": counts, "report": report}


def main(argv: List[str]) -> None:
    if len(argv) != 1 or argv[0] not in COMMANDS:
        raise SystemExit("usage: de_kill.py counts|report")
    COMMANDS[argv[0]]()


if __name__ == "__main__":
    main(_argv)
