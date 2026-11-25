"""Monthly school run (MONTHLY_DESIGN.md, frozen 2026-11-20; the OWNER ANSWERS section overrides the defaults).

    python -I monthly_orb.py prep      # lot history + lot gate (fo_bhav/), stamps; calendar + index = htf/prep.json
    python -I monthly_orb.py rows      # per stock: M1, M2, M1c6, M2c6 records (+ M3 walks) -> scratchpad monthly/rows
    python -I monthly_orb.py checks    # pre-run checks (0)-(9); prints NO result line
    python -I monthly_orb.py report    # ONLY after tests + the independent fresh-agent review (§10 protocol)

Owner answers 2026-11-20: Phase 2 (2015-19) DROPPED -> nothing is promoted on a fresh sample, the best M1 label is a
candidate for a small live test; today's 218 (BIASED) run now, labelled survivorship-biased; tax base STCG 20.8%,
sensitivity 31.2% business income, futures 31.2%. Owner 11-20: "the filter is key" -> the report opens with a
"WHAT THE FILTER ADDS" block (picks vs C2 random, vs C3 universe, C4 equal-weight excess).

Resolved ambiguities and deviations (to be copied into MONTHLY_DESIGN.md §13 by the review):
 A1 M1 signal month: the weekly guard "day 1 missing/partial -> skipped" is applied to EVERY signal-month session
    (any missing / partial signal session -> stock-month skipped, reason missing_signal_day; count in check (2)).
 A2 M1 RVOL_M = sum of the signal sessions' daily volumes / (n_signal x SMA20 of the 20 daily bars before the
    signal month) — the htf RVOL formula with n = sessions in the signal month. CA guard window = the 20 daily bars
    before the signal month + the whole signal month. M2 keeps the htf S3 window (20 bars ending on session 3).
 A3 C6 periods run 16th .. 15th, keyed "<start YYYY-MM>s"; IS/OOS by the start month. C6 is R-only (L arm).
 A4 M3 is long-only, cash delivery (the M1 verdict arm). BUF of a ratchet = htf buffer() of that month's last
    close. A month in which the stock has no bars does not move the stop. Roll count = month ends crossed while open
    (a stop on a month-end session after 15:10 counts that roll). M3 MTM uses month-end marks (owner: monthly series).
 A5 Lot history: format detected by the header, not the date (2024-07-01.zip is still the old format, so UDiFF
    truth starts 2024-08). Contract = first expiry on or after the trade month's last session, read from the file's
    own expiry dates (the last-Thursday/Tuesday rule is therefore not needed). UDiFF months use NewBrdLotQty of that
    contract even below 20 contracts (exact); the < 20 contracts -> near-month fallback applies to the old-format
    estimate only. Estimate = the UNIQUE integer in [V/(n.H), V/(n.L)] dividing OPEN_INT; zero OI or 0 / 2+ such
    integers -> None (counted out). UDiFF value is rounded to 0.01 lakh before the estimate (old-file precision).
    Gate denominator = the rows the lot view would use (universe stocks, trade months 2024-08..), None = a miss.
    Today's symbols are mapped to the bhavcopy's old names through data/fo_rename_map.json.
 A6 Futures book for switch (4)/(5): LS when shorts go, else the L-fut longs. Lot view and fallback size by risk
    only (lots = floor(RISK_F / (lot x range)); no slot cap, as §4).
 A7 COMPOUND liquidity cap: qty <= floor(PART_MAX x ADT20 / EL) (EL = the order price known at decision time);
    ADT20 unknown -> qty 0, counted.
 A8 ABSORBS K with uncapped Rs P&L <= 0 -> the P&L leg is not met (no ratio exists).
 A9 CG tax set-off order: current-year ST loss -> ST gains then LT gains; LT loss -> LT gains only; brought-forward
    LT loss -> LT gains, brought-forward ST loss -> ST then LT gains (oldest first); the Rs1.25L exemption applies
    to LT gains after set-off. Holding > 12 months = exit date after the same calendar date one year on.
 A10 M2 label: FAIL as M1; else "UNCONFIRMED — forward test only" iff (c)-(f) hold with 97.5% CIs, else
    INCONCLUSIVE (Phase 2 dropped, so CONFIRMED is unreachable). M2 "mean >= 95th pct of C2" stays the percentile.
 A11 MTM marks are kept for the L, LS, C1 and C1n arms of M1 / M2 (opp and C6 records are R-only; C1 / C1n added
    by owner decision O3, FORMAT 2).
 D1 PIT universe is NOT built: pit_ok / data_guard_pit / the PIT gate status exist and are tested; PIT rows (surv_raw
    names) and the PIT C4 index rebuild are a later re-run (owner Q7: two looks accepted).
 D2 Phase 2 code is not written (owner Q2): phase2_read only states DROPPED or reads a supplied result.
 (No D3 exists in this docstring; the review brief named D1-D3.)

Independent review 2026-11-21 (report-path fixes, no frozen parameter changed, rows/prep FORMAT unchanged):
 R1 COMPOUNDING GO/NO-GO takes the CAPPED M1 label (was the raw read: could print YES on BIASED / no Phase 2).
 R2 the §9 C1-type note is judged on the C1 / C1n arms' own (a)-(b); (c) is printed as not computable (A11).
 R5 futures views state how many trades are in names not in F&O that month (no contract existed).
 R6 the shorts line says UNTESTABLE when MIN_TRADES cannot be met (a sample-size drop, not a measured result).
 R7 F6 carry-over: M1 / M2 lines without signal periods from the closing auction (not in the verdict).
 R8 the filter block says that price > 100 and range >= 0.5% are shared guards, not measured by C2 / C3.
 R10 the NIFTY benchmark line prints its close source (official file + declared tail).

Owner decisions 2026-11-21 (MONTHLY_DESIGN.md §13; all DESCRIPTIVE, outside the verdict, except O3 which wires §9
leg (c) into the C1-type note):
 O1 (R3) M1 / M2 re-run with keep = F&O member at the decision snapshot (pit_ok on data/fo_membership.json +
    data/fo_rename_map.json), the WHAT THE FILTER ADDS block repeated for that subset.
 O2 (R4) M1 / M2 lines without the late listings (first daily bar after 2019-10-31), as htf.
 O3 (R2) C1 / C1n keep daily marks (FORMAT 2, A11 superseded for C1 / C1n) -> the C1-type note judges (a)-(c).
 O4 (R6) lot gate stays FAIL with the A5 estimator. No change.
 O5 new control C2a: random stocks drawn ONLY from that month's ATR%-eligible records (price / range are record
    guards already), same count per month, seed SEED -> isolates what the RVOL ranking adds on its own.
"""
from __future__ import annotations

import bisect
import csv
import io
import json
import math
import os
import random
import statistics as st
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_argv = sys.argv[1:]
sys.argv = sys.argv[:1]                       # full_asrs parses --srs at import; neutral here
import compounding as CP  # noqa: E402
import full_asrs as F  # noqa: E402
import htf_orb as H  # noqa: E402
import liq_side as LQ  # noqa: E402
import nov_stocks as N  # noqa: E402
import survivorship as SV  # noqa: E402
import y2026_asrs as Y  # noqa: E402

WORK = F.BASE / "monthly"
ROWS_DIR = WORK / "rows"
PREP = WORK / "prep.json"
BHAV_DIR = F.BASE.parent / "fo_bhav"
RENAME_MAP = HERE / "data" / "fo_rename_map.json"
OUT_FILE = HERE / "results" / "monthly_report.txt"
FORMAT = 2                                    # bump on any change to prep / rows output (2: C1/C1n marks, O3)

# ---------------------------------------------------------------- §1 frozen parameters
MIN_SESS_MONTH = H.MIN_SESS_MONTH             # 15
M2_SIG = 3                                    # M2 = htf S3: first 3 sessions
TOP_N = H.TOP_N
CAPITAL, RISK, SLOT = H.CAPITAL, H.RISK, H.SLOT
SLIP_D, SLIPS_D = 0.0005, (0.0002, 0.0005, 0.0010)
SLIP_F, SLIPS_F = 0.0010, (0.0005, 0.0010, 0.0015)
CARRY_PA = 0.065
FUT_CAPITAL, RISK_F, MARGIN = 10_000_000.0, 100_000.0, 0.20
PART_MAX, PART_HARD = 0.01, 0.05
CAP_LEVELS = (1_000_000.0, 10_000_000.0, 100_000_000.0)
IS_END_M = "2023-12"
BOOT_N, SEED, RAND_DRAWS = H.BOOT_N, H.SEED, H.RAND_DRAWS
M2_LEVEL = 0.975
MBB_BLOCK = 3
MIN_TRADES_ALL, MIN_TRADES_OOS, MIN_MONTHS = 300, 100, 60
DD_MAX = 25.0
STCG, STCG_SENS, LTCG, LTCG_EXEMPT, FUT_TAX, CARRY_FY = 0.208, 0.312, 0.130, 125_000.0, 0.312, 8
NIFTY_DIV = 0.012
LOT_MIN_CONTRACTS, LOT_GATE = 20, 0.98
ABSORB_P90, ABSORB_P99, ABSORB_RS = 0.01, 0.05, 0.90
UDIFF_FROM = "2024-08"                        # first month whose bhavcopy is UDiFF (detected by header, A5)
RULES = ("M1", "M2", "M1c6", "M2c6")
FULL_ARMS = ("L", "LS", "C1", "C1n")
KEEP_MARKS = ("L", "LS", "C1", "C1n")         # owner 2026-11-21 (R2): C1 / C1n marks kept -> §9 leg (c) computable
DAYS_PER_YEAR = 365.25


# ---------------------------------------------------------------- calendar and periods
def prev_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def shift_key(d: str) -> str:
    """C6 'month' = the 16th .. the 15th of the next month, keyed by its start month + 's'."""
    return (d[:7] if int(d[8:10]) >= 16 else prev_month(d[:7])) + "s"


def prev_key(k: str) -> str:
    return prev_month(k[:7]) + k[7:]


def shifted_periods(cal: list[str]) -> list[tuple[str, list[str]]]:
    """As H.periods for C6 months: >= MIN_SESS_MONTH sessions, the period holding the final session dropped."""
    by: dict[str, list[str]] = defaultdict(list)
    for d in cal:
        by[shift_key(d)].append(d)
    final = shift_key(cal[-1]) if cal else None
    return [(k, sorted(v)) for k, v in sorted(by.items()) if len(v) >= MIN_SESS_MONTH and k != final]


def month_pairs(per: list[tuple[str, list[str]]]) -> list[tuple[str, list[str], str, list[str]]]:
    """(signal key, signal sessions, trade key, trade sessions): the signal period is the period IMMEDIATELY before
    the trade period and both are eligible; never paired across a dropped month."""
    keys = dict(per)
    return [(prev_key(k), keys[prev_key(k)], k, ss) for k, ss in per if prev_key(k) in keys]


def build_plan(cal: list[str]) -> dict[str, list[tuple[str, list[str], str, list[str], list[str]]]]:
    """rule -> [(signal key, signal sessions, trade key, trade-period sessions, hold sessions)]."""
    cm, sm = H.periods(cal, "month"), shifted_periods(cal)
    out: dict[str, list] = {}
    for rule, per in (("M1", cm), ("M1c6", sm)):
        out[rule] = [(sk, sig, k, ss, ss) for sk, sig, k, ss in month_pairs(per)]
    for rule, per in (("M2", cm), ("M2c6", sm)):
        out[rule] = [(k, ss[:M2_SIG], k, ss, ss[M2_SIG:]) for k, ss in per]
    return out


def is_m(k: str) -> bool:
    """IS / OOS keyed on the TRADE period (M1's signal month is one earlier)."""
    return k[:7] <= IS_END_M


def m1_type(rule: str) -> bool:
    return rule in ("M1", "M1c6")


# ---------------------------------------------------------------- per-stock context
def make_ctx(daily: list[dict], by_date: dict[str, list[dict]], full: dict[str, list[dict]], cal: list[str]
             ) -> dict:
    """Everything build_month_record needs for one stock. daily = special-session bars already removed."""
    by_date = {d: sorted(v, key=lambda b: b["t"]) for d, v in by_date.items()}
    calset = set(cal)
    opens = sorted(d for d, v in by_date.items() if d in calset and v and v[0]["t"][11:] == "09:15")
    cache: dict[tuple[str, bool], list[dict]] = {}

    def next_open(after: str) -> tuple[str, float] | None:
        i = bisect.bisect_right(opens, after)
        if i >= len(opens):
            return None
        b = by_date[opens[i]][0]
        return b["t"], b["o"]

    def sbars(d: str, last: bool = False) -> list[dict]:
        if (d, last) not in cache:
            cache[(d, last)] = H.session_bars(by_date.get(d, []), d, last)
        return cache[(d, last)]

    return dict(daily=daily, didx={b["d"]: i for i, b in enumerate(daily)}, atr=N.wilder_atr(daily),
                dates=[b["d"] for b in daily], by_date=by_date, full=full, next_open=next_open, sbars=sbars,
                first5=min(by_date) if by_date else "9999", cal=cal)


def load_ctx(sym: str, cal: list[str], halt_days: set, raw: Path | None = None, y2026: bool = True
             ) -> tuple[dict, int]:
    """The htf_orb.stock_records preprocessing (F5 halt sessions, F7 special daily bars removed), with H's own
    primitives. Returns (ctx, number of special-session daily bars removed)."""
    daily, m5 = F.load_stock(sym, raw, y2026)
    m5 = [b for b in m5 if b["t"][:10] <= F.LAST_DAY]
    by_date: dict[str, list[dict]] = defaultdict(list)
    for b in m5:
        by_date[b["t"][:10]].append(b)
    full = Y.full_sessions(m5)
    calset = set(cal)
    H.add_halt_sessions(full, by_date, set(halt_days) & calset)
    n_daily = len(daily)
    daily = [b for b in daily if not H.is_special_daily(b["d"], by_date.get(b["d"]), calset, cal[0], cal[-1])]
    return make_ctx(daily, dict(by_date), full, cal), n_daily - len(daily)


# ---------------------------------------------------------------- per stock-month record
def enrich(o: dict, flat: list[dict], tix: dict[str, int], ctx: dict) -> None:
    """ADT20 strictly before the entry date (special-session bars removed), entry / exit bar volume."""
    if o.get("status") != "trig":
        return
    o["adt"] = LQ.turnover_sma(ctx["daily"], o["ed"], dates=ctx["dates"])
    o["ev"] = flat[tix[o["et"]]]["v"]
    xi = tix.get(o["xt"])
    o["xv"] = flat[xi]["v"] if xi is not None else None


def build_month_record(sym: str, rule: str, sk: str, sig: list[str], k: str, hold_ss: list[str], ctx: dict,
                       late: bool) -> tuple[dict | None, str]:
    """One stock x trade period for one rule. Guards and skip reasons are htf build_record's (M2 = S3 path)."""
    m1 = m1_type(rule)
    if len(hold_ss) < 2:
        return None, "short_period"
    if sig[0] < ctx["first5"]:
        return None, "not_listed"
    full, didx, daily, atr = ctx["full"], ctx["didx"], ctx["daily"], ctx["atr"]
    if any(d not in full for d in sig):
        return None, "missing_signal_day"
    if any(d not in didx for d in sig):
        return None, "missing_daily"
    j1, jn = didx[sig[0]], didx[sig[-1]]
    if j1 < H.HIST_MIN or atr[jn] is None:
        return None, "history"
    sma = sum(daily[i]["v"] for i in range(j1 - 20, j1)) / 20
    if sma <= 0:
        return None, "history"
    for i in range(j1 - 20 if m1 else jn - 19, jn + 1):
        if abs(daily[i]["o"] / daily[i - 1]["c"] - 1) > H.CA_JUMP:
            return None, "ca_guard"
    close = daily[jn]["c"]
    if close <= H.MIN_PX:
        return None, "min_px"
    sb = [b for d in sig for b in full[d]]
    hi, lo = max(b["h"] for b in sb), min(b["l"] for b in sb)
    if (hi - lo) / close < H.MIN_RNG:
        return None, "min_rng"
    buf = H.buffer(close)
    el, es = hi + buf, lo - buf
    qty, capped = H.size(el, es)
    by_date = ctx["by_date"]
    entry_bars = [b for d in hold_ss[:-1] for b in ctx["sbars"](d)]
    flat = entry_bars + ctx["sbars"](hold_ss[-1], True)
    rec = dict(sym=sym, rule=rule, k=k, sk=sk, sig0=sig[0], d1=hold_ss[0],
               rvol=sum(daily[didx[d]]["v"] for d in sig) / (len(sig) * sma),
               atr_pct=atr[jn] / close * 100, close=close, el=el, es=es, qty=qty, capped=capped, late=late,
               flag=H.ca_in_hold([sig[-1]] + hold_ss, by_date), n_hold=len(hold_ss),
               miss_hold=sum(1 for d in hold_ss if not by_date.get(d)),
               part_hold=sum(1 for d in hold_ss if by_date.get(d) and d not in full))
    arms = FULL_ARMS if rule in ("M1", "M2") else ("L",)
    if not flat:
        for a in arms:
            rec[a] = dict(status="none")
        return rec, "ok"
    fb = ctx["next_open"](hold_ss[-1])
    last_d, n_entry = hold_ss[-1], len(entry_bars)
    tix = {b["t"]: i for i, b in enumerate(flat)}
    rec["L"] = H.arm_breakout(flat, n_entry, el, es, "long", last_d, fb)
    if "LS" in arms:
        ls = H.arm_breakout(flat, n_entry, el, es, "both", last_d, fb)
        if ls["status"] == "trig":           # C5 opposite side, kept with its dates (carry needs them)
            opp = H.outcome(flat, last_d, tix[ls["et"]], ls["entry"], -ls["side"],
                            ls["entry"] + ls["side"] * (el - es), fb)
            ls["opp"] = {kk: opp[kk] for kk in ("side", "entry", "exit", "reason", "ed", "xd")}
        rec["LS"] = ls
        rec["C1"] = H.arm_open(flat, n_entry, es, True, last_d, fb)
        rec["C1n"] = H.arm_open(flat, n_entry, es, False, last_d, fb)
    for a in arms:
        enrich(rec[a], flat, tix, ctx)
        if a not in KEEP_MARKS or rule not in ("M1", "M2"):
            rec[a].pop("marks", None)
    return rec, "ok"


# ---------------------------------------------------------------- M3 trailing walk
def month_end_info(ctx: dict) -> list[tuple[str, float | None, float | None, float | None]]:
    """Per calendar month of the calendar: (its last session, ratchet candidate = the stock's 5-min low of the month
    - BUF(last close), roll price = the stock's 15:10 close on that session (else its last close of the month),
    mark = the stock's last close of the month). A month without bars: (date, None, None, None)."""
    by_m: dict[str, list[str]] = defaultdict(list)
    for d in ctx["cal"]:
        by_m[d[:7]].append(d)
    out: list[tuple[str, float | None, float | None, float | None]] = []
    for m in sorted(by_m):
        days = by_m[m]
        real = [b for d in days for b in ctx["by_date"].get(d, [])]
        if not real:
            out.append((days[-1], None, None, None))
            continue
        last_c = real[-1]["c"]
        cand = min(b["l"] for b in real) - H.buffer(last_c)
        lb = [b for b in ctx["by_date"].get(days[-1], []) if b["t"][11:] <= H.EXIT_HHMM]
        out.append((days[-1], cand, lb[-1]["c"] if lb else last_c, last_c))
    return out


def all_flat(ctx: dict) -> list[dict]:
    """Every calendar session's bars (grid-filled), the LAST_DAY session up to 15:10 (the M3 end mark)."""
    out: list[dict] = []
    for d in ctx["cal"]:
        out += ctx["sbars"](d, d == F.LAST_DAY)
    return out


def walk_trail(flat: list[dict], i0: int, stop0: float, mes: list[tuple[str, float | None, float | None,
                                                                         float | None]]) -> dict:
    """M3 long from entry bar i0: initial stop stop0; at each month end crossed while open the stop becomes
    max(stop, candidate), effective from the next session's 09:15. Same fill rules as H.hold. Never stopped ->
    exit at the last bar (LAST_DAY 15:10), reason open_at_end."""
    ed = flat[i0]["d"]
    me = [x for x in mes if x[0] >= ed]
    p, stop, prev_d = 0, stop0, ed
    stops, rolls, marks = [stop0], [], []
    hit_at = None
    for j in range(i0, len(flat)):
        b = flat[j]
        if b["d"] != prev_d:
            while p < len(me) and me[p][0] < b["d"]:
                dme, cand, rpx, mpx = me[p]
                if mpx is not None:
                    rolls.append(rpx)
                    marks.append([dme, mpx])
                if cand is not None:
                    stop = max(stop, cand)
                stops.append(stop)
                p += 1
            prev_d = b["d"]
        if b["f"]:
            continue
        if not b["l"] <= stop:
            continue
        if j == i0:
            x, reason, at_open = stop, "stop", False
        elif b["o"] < stop:
            x, reason, at_open = b["o"], "gap_stop", True
        else:
            x, reason, at_open = stop, "stop", False
        if p < len(me) and me[p][0] == b["d"] and b["t"][11:] > H.EXIT_HHMM and me[p][2] is not None:
            rolls.append(me[p][2])            # stopped on a month-end session after the 15:10 roll
        hit_at = (j, x, reason, at_open)
        break
    if hit_at is None:
        j, x, reason, at_open = len(flat) - 1, flat[-1]["c"], "open_at_end", False
    else:
        j, x, reason, at_open = hit_at
    return dict(status="trig", side=1, exit=x, reason=reason, et=flat[i0]["t"], xt=flat[j]["t"], ed=ed,
                xd=flat[j]["d"], at_open=at_open, marks=marks, rolls=rolls, stops=stops)


def add_m3(recs: list[dict], ctx: dict) -> int:
    """Attach an M3 walk to every rankable M1 record (ATR guard) whose L arm triggered — every eligible stock-month,
    not only the top 10, because held names push lower ranks up."""
    todo = [r for r in recs if r["L"].get("status") == "trig" and m1_eligible(r)]
    if not todo:
        return 0
    flat = all_flat(ctx)
    tix = {b["t"]: i for i, b in enumerate(flat)}
    mes = month_end_info(ctx)
    cal = ctx["cal"]
    for r in todo:
        o = r["L"]
        w = walk_trail(flat, tix[o["et"]], r["es"], mes)
        w["entry"] = o["entry"]
        i0, i1 = bisect.bisect_left(cal, r["d1"]), bisect.bisect_right(cal, w["xd"])
        w["flag"] = H.ca_in_hold([prev_session(cal, r["d1"])] + cal[i0:i1], ctx["by_date"])
        r["M3"] = w
    return len(todo)


def prev_session(cal: list[str], d: str) -> str:
    i = bisect.bisect_left(cal, d)
    return cal[i - 1] if i > 0 else d


def stock_records(sym: str, cal: list[str], plan: dict, late: bool, halt_days: set,
                  raw: Path | None = None, y2026: bool = True) -> dict:
    ctx, special = load_ctx(sym, cal, halt_days, raw, y2026)
    out: dict[str, list] = {r: [] for r in RULES}
    skips: dict[str, Counter] = {r: Counter() for r in RULES}
    for rule in RULES:
        for sk, sig, k, _, hold_ss in plan[rule]:
            rec, why = build_month_record(sym, rule, sk, sig, k, hold_ss, ctx, late)
            skips[rule][why] += 1
            if rec:
                out[rule].append(rec)
    n3 = add_m3(out["M1"], ctx)
    return dict(format=FORMAT, recs=out, skips={r: dict(c) for r, c in skips.items()}, special_daily=special,
                m3=n3)


# ---------------------------------------------------------------- PIT (built and tested; not run, D1)
def pit_ok(sym: str, rule: str, sk: str, k: str, eligible: Callable[[str, str], bool]) -> bool:
    """M1 decides at the prior month end -> the SIGNAL month's snapshot; M2 decides after session 3 -> the trade
    month's snapshot. Never a later one."""
    m = sk[:7] if m1_type(rule) else k[:7]
    return eligible(sym, m + "-01")


def data_guard_pit(last_m5: dict[str, str | None], last_member_day: dict[str, str], gone: set) -> list[str]:
    """PIT-aware (0): a member needs bars through the last session of its last member month, unless it is GONE
    (no data anywhere, listed). htf data_guard would refuse every merged / delisted member."""
    bad = []
    for s, need in sorted(last_member_day.items()):
        if s in gone:
            continue
        have = last_m5.get(s)
        if have is None or have < need:
            bad.append(f"(0-PIT) {s}: bars end {have}, member through {need}")
    return bad


def pit_gate_status(rename_map: Path = RENAME_MAP) -> dict:
    """§5 PIT readiness gate. Coverage and the survivorship review cannot be read from files here; PIT rows for this
    study are not built (D1), so the gate is NOT READY by construction."""
    return dict(rename_map=rename_map.exists(), coverage="not measured for this study (survivorship.py coverage)",
                review="not machine-checkable (owner)", pit_rows_built=False, ready=False)


# ---------------------------------------------------------------- lot history (§4)
def _num(v: str) -> float:
    v = (v or "").strip()
    return float(v) if v else 0.0


def bhav_rows(zpath: Path) -> tuple[str, dict[str, list[dict]]]:
    """(format, symbol -> stock-future rows). Format by header: 'old' (INSTRUMENT) or 'udiff' (FinInstrmTp)."""
    with zipfile.ZipFile(zpath) as z:
        text = z.read(z.namelist()[0]).decode("utf-8", errors="replace")
    return parse_bhav(text)


def parse_bhav(text: str) -> tuple[str, dict[str, list[dict]]]:
    rows = csv.DictReader(io.StringIO(text))
    hdr = [h.strip() for h in (rows.fieldnames or [])]
    if "INSTRUMENT" in hdr:
        fmt = "old"
    elif "FinInstrmTp" in hdr:
        fmt = "udiff"
    else:
        raise ValueError(f"unknown bhavcopy header {hdr[:6]}")
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if fmt == "old":
            if r.get("INSTRUMENT") != "FUTSTK":
                continue
            out[r["SYMBOL"]].append(dict(
                exp=datetime.strptime(r["EXPIRY_DT"], "%d-%b-%Y").date().isoformat(),
                n=int(_num(r["CONTRACTS"])), val=_num(r["VAL_INLAKH"]), oi=int(_num(r["OPEN_INT"])),
                h=_num(r["HIGH"]), l=_num(r["LOW"]), c=_num(r["CLOSE"]), lot=None))
        else:
            if r.get("FinInstrmTp") != "STF":
                continue
            out[r["TckrSymb"]].append(dict(
                exp=r["XpryDt"], n=int(_num(r["TtlTradgVol"])), val=round(_num(r["TtlTrfVal"]) / 1e5, 2),
                oi=int(_num(r["OpnIntrst"])), h=_num(r["HghPric"]), l=_num(r["LwPric"]), c=_num(r["ClsPric"]),
                lot=int(_num(r["NewBrdLotQty"])) or None))
    return fmt, dict(out)


def lot_estimate(row: dict) -> int | None:
    """Pre-UDiFF lot: the UNIQUE integer in [V/(n.H), V/(n.L)] that divides OPEN_INT (V = value in Rs). None when
    no trade, no OI, or 0 / 2+ such integers (ambiguous)."""
    n, v, oi, h, lo = row["n"], row["val"] * 1e5, row["oi"], row["h"], row["l"]
    if n <= 0 or v <= 0 or oi <= 0 or h <= 0 or lo <= 0 or h < lo:
        return None
    a, b = math.ceil(v / (n * h) - 1e-6), math.floor(v / (n * lo) + 1e-6)
    cands = [x for x in range(max(a, 1), b + 1) if oi % x == 0]
    return cands[0] if len(cands) == 1 else None


def lot_for(rows: list[dict], last_session: str, fmt: str) -> tuple[int | None, str, str | None, bool]:
    """(lot, source, contract expiry, near-month flag). Contract = first expiry on or after the trade month's last
    session (A5). udiff -> NewBrdLotQty (exact). old -> estimate; < 20 contracts -> the near-month row, flagged."""
    futs = sorted(rows, key=lambda r: r["exp"])
    tr = next((r for r in futs if r["exp"] >= last_session), None)
    if tr is None:
        return None, "no_contract", None, False
    if fmt == "udiff":
        return tr["lot"], ("udiff" if tr["lot"] else "udiff_missing"), tr["exp"], False
    use, near = (tr, False) if tr["n"] >= LOT_MIN_CONTRACTS else (futs[0], True)
    est = lot_estimate(use)
    return est, ("est" if est else "ambiguous"), tr["exp"], near


def aliases(sym: str, rmap: dict[str, str | None]) -> list[str]:
    """Bhavcopy names that are today's sym: itself first, then old names that resolve to it (rename map)."""
    return [sym] + sorted(x for x in rmap if x != sym and SV.resolve(x, rmap) == sym)


def rows_for(sym: str, by_sym: dict[str, list[dict]], rmap: dict[str, str | None]) -> list[dict] | None:
    """The sym's own rows, else the single alias present; two aliases present -> None (ambiguous)."""
    if sym in by_sym:
        return by_sym[sym]
    got = [a for a in aliases(sym, rmap)[1:] if a in by_sym]
    return by_sym[got[0]] if len(got) == 1 else None


def lot_history(universe: list[str], rmap: dict[str, str | None], cal: list[str], dl: Path = BHAV_DIR
                ) -> tuple[dict[str, dict[str, list]], dict]:
    """lots[YYYY-MM][sym] = [lot | None, source, expiry, near] from the bhavcopy of that month's first session;
    gate = the old method on UDiFF months vs NewBrdLotQty (A5)."""
    last_of: dict[str, str] = {}
    for d in cal:
        last_of[d[:7]] = d
    lots: dict[str, dict[str, list]] = {}
    gate: Counter = Counter()
    gate_miss: list = []
    fmts: dict[str, str] = {}
    for f in sorted(dl.glob("*.zip")):
        m = f.name[:7]
        if m not in last_of:
            continue
        fmt, by_sym = bhav_rows(f)
        fmts[m] = fmt
        lots[m] = {}
        for s in universe:
            rws = rows_for(s, by_sym, rmap)
            if rws is None:
                lots[m][s] = [None, "not_in_file", None, False]
                continue
            lot, src, exp, near = lot_for(rws, last_of[m], fmt)
            lots[m][s] = [lot, src, exp, near]
            if fmt == "udiff" and lot:
                est = lot_for(rws, last_of[m], "old")[0]
                gate["total"] += 1
                if est == lot:
                    gate["exact"] += 1
                elif est is None:
                    gate["none"] += 1
                else:
                    gate["wrong"] += 1
                    if len(gate_miss) < 20:
                        gate_miss.append([m, s, lot, est])
    pct = gate["exact"] / gate["total"] if gate["total"] else 0.0
    return lots, dict(gate, pct=pct, passed=bool(gate["total"]) and pct >= LOT_GATE, misses=gate_miss,
                      formats=dict(Counter(fmts.values())), udiff_from=min((m for m, x in fmts.items()
                                                                            if x == "udiff"), default=None))


# ---------------------------------------------------------------- passes
def load_mprep() -> dict:
    p = json.loads(PREP.read_text())
    if p.get("format") != FORMAT or p.get("htf_format") != H.FORMAT:
        raise SystemExit(f"{PREP} has format {p.get('format')}/{p.get('htf_format')}, code expects "
                         f"{FORMAT}/{H.FORMAT}: delete monthly/ and rerun prep + rows")
    return p


def write_json_atomic(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


def prep() -> None:
    p = H.load_prep()                         # calendar + C4 index (htf FORMAT checked there)
    cal = p["cal"]
    uni = json.loads(Y.UNIVERSE.read_text())
    rmap = json.loads(RENAME_MAP.read_text()) if RENAME_MAP.exists() else {}
    lots, gate = lot_history(uni, rmap, cal)
    plan = build_plan(cal)
    write_json_atomic(PREP, dict(format=FORMAT, htf_format=H.FORMAT, lots=lots, gate=gate,
                                 n_periods={r: len(v) for r, v in plan.items()}))
    src = Counter(x[1] for m in lots.values() for x in m.values())
    print(f"prep done: {len(lots)} bhavcopy months {gate['formats']}, UDiFF from {gate['udiff_from']}; "
          f"lot sources {dict(src)}; periods {dict((r, len(v)) for r, v in plan.items())}", flush=True)


def rows() -> None:
    p = H.load_prep()
    load_mprep()
    cal = p["cal"]
    plan = build_plan(cal)
    ROWS_DIR.mkdir(parents=True, exist_ok=True)
    for s in json.loads(Y.UNIVERSE.read_text()):
        f = ROWS_DIR / f"{F.fname(s)}.json"
        if f.exists():
            continue
        late = (p["first_daily"][s] or "9999") > H.LATE_AFTER
        write_json_atomic(f, stock_records(s, cal, plan, late, set(p["halt_days"])))
        print("rows", s, flush=True)
    print("rows done", flush=True)


def load_rows(universe: list[str] | None = None) -> tuple[dict[str, list], dict[str, Counter], int]:
    recs: dict[str, list] = defaultdict(list)
    skips: dict[str, Counter] = defaultdict(Counter)
    special = 0
    for s in (json.loads(Y.UNIVERSE.read_text()) if universe is None else universe):
        x = json.loads((ROWS_DIR / f"{F.fname(s)}.json").read_text())    # missing file = crash, never a skip
        if x.get("format") != FORMAT:
            raise SystemExit(f"monthly rows/{F.fname(s)}.json has format {x.get('format')}, code expects "
                             f"{FORMAT}: delete monthly/rows and rerun rows")
        special += x["special_daily"]
        for r, lst in x["recs"].items():
            recs[r].extend(lst)
        for r, c in x["skips"].items():
            skips[r].update(c)
    return recs, skips, special


# ---------------------------------------------------------------- selection
def m1_eligible(r: dict) -> bool:
    return r["atr_pct"] > H.ATR_PCT


def ranked(recs: list[dict]) -> dict[str, list[dict]]:
    """M1: every guard-passing stock-month with ATR% > 1.5, ranked by RVOL_M desc, ties alphabetical. No RVOL
    threshold (§2)."""
    by: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        if m1_eligible(r):
            by[r["k"]].append(r)
    return {k: sorted(v, key=lambda r: (-r["rvol"], r["sym"])) for k, v in by.items()}


def select_rank(recs: list[dict], top_n: int = TOP_N) -> dict[str, list[dict]]:
    return {k: v[:top_n] for k, v in ranked(recs).items()}


def select_for(rule: str) -> Callable[[list[dict]], dict[str, list[dict]]]:
    return select_rank if m1_type(rule) else H.select_picks     # M2 = S3 unchanged (RVOL >= 1.5)


# ---------------------------------------------------------------- costs
def cal_days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def carry_ps(o: dict) -> float:
    """Futures premium per share: a LONG pays entry x CARRY x calendar days / 365; a SHORT is not credited."""
    return o["entry"] * CARRY_PA * cal_days(o["ed"], o["xd"]) / 365 if o["side"] > 0 else 0.0


def roll_ps(o: dict, slip: float) -> float:
    return sum(H.cost_fut(o["side"], px, px, slip) for px in o.get("rolls", ()))


def cost_ps(r: dict, o: dict, slip: float, model: str, qty: int | None = None) -> float:
    if model == "deliv":
        return H.cost_deliv(o["entry"], o["exit"], qty or r["qty"], slip)
    return H.cost_fut(o["side"], o["entry"], o["exit"], slip) + carry_ps(o) + roll_ps(o, slip)


def r_arm(r: dict, o: dict, slip: float, model: str) -> float:
    if model == "deliv":
        return H.r_net(r, o, slip, "deliv")
    return H.r_net(r, o, slip, "fut") - (carry_ps(o) + roll_ps(o, slip)) / (r["el"] - r["es"])


def pct_arm(r: dict, o: dict, slip: float, model: str) -> float:
    return (o["side"] * (o["exit"] - o["entry"]) - cost_ps(r, o, slip, model)) / o["entry"] * 100


# ---------------------------------------------------------------- MTM, stats
def mtm_daily_arm(items: list[tuple[float, dict, float]], cal: list[str]) -> dict[str, float]:
    """items: (quantity, outcome with marks, total cost in Rs). Side-aware daily MTM: open positions marked at each
    mark close; the cost lands on the exit session."""
    pnl: dict[str, float] = defaultdict(float)
    for q, o, cost in items:
        sd, prev = o["side"], o["entry"]
        for d, m in o.get("marks", ()):
            pnl[d] += q * sd * (m - prev)
            prev = m
        pnl[H.cal_at_or_after(cal, o["xd"])] += q * sd * (o["exit"] - prev) - cost
    return pnl


def span_of(cal: list[str], lo: str, pnl: dict[str, float]) -> list[str]:
    hi = max(pnl) if pnl else lo
    return [d for d in cal if lo <= d <= max(hi, lo)]


def wo_best(R: list[tuple[dict, float]], n: int = 3) -> float:
    by: dict[str, float] = defaultdict(float)
    for r, v in R:
        by[r["k"]] += v
    drop = {k for k, _ in sorted(by.items(), key=lambda kv: -kv[1])[:n]}
    return H.mean0([v for r, v in R if r["k"] not in drop])


def summary_R(R: list[tuple[dict, float]], level: float = 0.95) -> dict:
    i = [(r["k"], v) for r, v in R if is_m(r["k"])]
    o = [(r["k"], v) for r, v in R if not is_m(r["k"])]
    al = [(r["k"], v) for r, v in R]
    return dict(n_all=len(al), n_is=len(i), n_oos=len(o), n_months=len({k for k, _ in al}),
                all_mean=H.mean0([v for _, v in al]), all_ci=H.block_ci(al, level=level),
                is_mean=H.mean0([v for _, v in i]), is_ci=H.block_ci(i, level=level),
                oos_mean=H.mean0([v for _, v in o]), oos_ci=H.block_ci(o, level=level),
                win=100 * sum(v > 0 for _, v in al) / len(al) if al else float("nan"), wo_best3=wo_best(R))


def c4_of(r: dict, o: dict, uni: tuple) -> float | None:
    """Universe excess per trade ÷ range %, sign flipped for shorts (§9 C4)."""
    u = H.universe_ret(uni, o["et"], o["xt"], o["at_open"])
    if u is None:
        return None
    return o["side"] * ((o["exit"] / o["entry"] - 1) - u) / ((r["el"] - r["es"]) / o["entry"])


def mbb_ci(vals: list[float], block: int = MBB_BLOCK, n: int = BOOT_N, seed: int = SEED, level: float = 0.95
           ) -> tuple[float, float]:
    """Moving-block bootstrap of the mean of an ordered series (M3 monthly %, overlapping holds)."""
    m = len(vals)
    if m == 0:
        return float("nan"), float("nan")
    b = min(block, m)
    nb = math.ceil(m / b)
    rng = random.Random(seed)
    ms = []
    for _ in range(n):
        s: list[float] = []
        for _ in range(nb):
            i = rng.randrange(m - b + 1)
            s += vals[i:i + b]
        ms.append(sum(s[:m]) / m)
    ms.sort()
    q = (1 - level) / 2
    return ms[int(q * n)], ms[int((1 - q) * n) - 1]


def c5_dist(pairs: list[tuple[float, float]], draws: int = RAND_DRAWS, seed: int = SEED) -> list[float]:
    """C5: per draw a coin picks the actual side or the opposite side (same bar, stop one range away)."""
    rng = random.Random(seed)
    return sorted(H.mean0([a if rng.random() < 0.5 else b for a, b in pairs]) for _ in range(draws))


def pctile(dist: list[float], x: float) -> float:
    return 100 * sum(v < x for v in dist) / len(dist) if dist else float("nan")


def evaluate_arm(recs: list[dict], rule: str, arm: str, model: str, slip: float, cal: list[str], uni: tuple,
                 side: int = 0, level: float = 0.95, keep: Callable[[dict], bool] | None = None,
                 drop: Callable[[dict], bool] | None = None, controls: bool = False, draws: int = RAND_DRAWS
                 ) -> dict:
    """One rule x arm x cost model x side. keep = universe filter BEFORE selection; drop = removed AFTER selection
    (never refilled; also leaves the C2/C3 pools), as htf evaluate."""
    keep = keep or (lambda r: True)
    drop = drop or (lambda r: False)
    recs = [r for r in recs if keep(r)]
    picks = {k: [r for r in v if not drop(r)] for k, v in select_for(rule)(recs).items()}
    picks = {k: v for k, v in picks.items() if v}
    pool_recs = [r for r in recs if not drop(r)]
    pk = [r for k in sorted(picks) for r in picks[k]]

    def ok(r: dict) -> bool:
        return H.traded(r, arm) and (side == 0 or r[arm]["side"] == side)

    tr = [(r, r[arm]) for r in pk if ok(r)]
    R = [(r, r_arm(r, o, slip, model)) for r, o in tr]
    x = summary_R(R, level)
    x.update(n_picks=len(pk), picks=picks, trades=R, tr=tr)
    pc = [(r, pct_arm(r, o, slip, model)) for r, o in tr]
    x["pct_all"] = H.mean0([v for _, v in pc])
    x["pct_is"] = H.mean0([v for r, v in pc if is_m(r["k"])])
    x["pct_oos"] = H.mean0([v for r, v in pc if not is_m(r["k"])])
    ex, miss = [], 0
    for r, o in tr:
        e = c4_of(r, o, uni)
        if e is None:
            miss += 1
        else:
            ex.append((r["k"], e))
    x.update(c4_all=H.mean0([e for _, e in ex]), c4_ci=H.block_ci(ex, level=level),
             c4_is=H.mean0([e for k, e in ex if is_m(k)]), c4_oos=H.mean0([e for k, e in ex if not is_m(k)]),
             c4_missing=miss)
    if all("marks" in o for _, o in tr) and pk:
        pnl = mtm_daily_arm([(r["qty"], o, r["qty"] * cost_ps(r, o, slip, model)) for r, o in tr], cal)
        span = span_of(cal, min(r["d1"] for r in pk), pnl)
        daily = [(d, pnl.get(d, 0.0)) for d in span]
        dd = H.dd_vs_capital(daily)
        mon: dict[str, float] = defaultdict(float)
        for d, v in daily:
            mon[d[:7]] += v
        mser = [(m, 100 * v / CAPITAL) for m, v in sorted(mon.items())]
        x.update(daily=daily, dd=dd, dd_pct=dd["pct_cap"], rs=sum(pnl.values()), monthly=mser,
                 mon_mean=H.mean0([v for _, v in mser]), mon_ci=H.block_ci(mser, level=level),
                 mon_is=H.mean0([v for m, v in mser if is_m(m)]), mon_oos=H.mean0([v for m, v in mser if not is_m(m)]),
                 dd_is=H.dd_vs_capital([z for z in daily if z[0][:7] <= IS_END_M])["pct_cap"],
                 dd_oos=H.dd_vs_capital([z for z in daily if z[0][:7] > IS_END_M])["pct_cap"])
    if controls:
        x.update(controls_block(picks, pk, pool_recs, arm, side, model, slip, uni, level, draws))
    return x


def c2a_pool(pool: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """C2a pool (owner O5): per month, only the records passing the ATR% guard (price > 100 and range >= 0.5% are
    record guards, so every pool record has them). The picks are inside it; the RVOL ranking is what is left."""
    return {k: [r for r in v if m1_eligible(r)] for k, v in pool.items()}


def controls_block(picks: dict[str, list[dict]], pk: list[dict], pool_recs: list[dict], arm: str, side: int,
                   model: str, slip: float, uni: tuple, level: float, draws: int) -> dict:
    """D1 per pick (L - C1; untriggered / void / skipped = 0R), C1 / C1n, C3 universe, C2 random, paired months."""
    def rp(r: dict, a: str) -> float:
        if not H.traded(r, a) or (side and r[a]["side"] != side):
            return 0.0
        return r_arm(r, r[a], slip, model)

    def xp(r: dict, a: str) -> float:
        if not H.traded(r, a):
            return 0.0
        e = c4_of(r, r[a], uni)
        return 0.0 if e is None else e

    out: dict = {}
    d1 = [(r["k"], rp(r, arm) - rp(r, "C1")) for r in pk]
    out.update(d1_all=H.mean0([v for _, v in d1]), d1_ci=H.block_ci(d1, level=level),
               d1_is=H.mean0([v for k, v in d1 if is_m(k)]), d1_oos=H.mean0([v for k, v in d1 if not is_m(k)]),
               d1x_all=H.mean0([xp(r, arm) - xp(r, "C1") for r in pk]),
               p_per_pick=H.mean0([rp(r, arm) for r in pk]), c1_per_pick=H.mean0([rp(r, "C1") for r in pk]),
               c1n_per_pick=H.mean0([rp(r, "C1n") for r in pk]))
    for a in ("C1", "C1n"):
        out[a.lower() + "_R"] = [(r, r_arm(r, r[a], slip, model)) for r in pk if H.traded(r, a)]
    out["c1_skipped"] = sum(r["C1"]["status"] == "skip_below_sl" for r in pk)
    pool: dict[str, list[dict]] = defaultdict(list)
    for r in pool_recs:
        pool[r["k"]].append(r)
    vals = {id(r): r_arm(r, r[arm], slip, model) for r in pool_recs
            if H.traded(r, arm) and (side == 0 or r[arm]["side"] == side)}
    c3 = [(r, vals[id(r)]) for r in pool_recs if id(r) in vals]
    out["c3_R"] = c3
    out["c3_all"] = H.mean0([v for _, v in c3])
    out["c3_is"] = H.mean0([v for r, v in c3 if is_m(r["k"])])
    out["c3_oos"] = H.mean0([v for r, v in c3 if not is_m(r["k"])])
    dist = H.c2_dist(picks, pool, vals, draws=draws)
    out["c2_dist"] = dist
    out["c2_median"] = dist[len(dist) // 2]
    out["c2_p95"] = dist[int(0.95 * len(dist)) - 1]
    # C2a (owner O5, descriptive): the same draw, pool restricted to that month's ATR%-eligible records
    pool_a = c2a_pool(pool)
    dist_a = H.c2_dist(picks, pool_a, vals, draws=draws)
    out.update(c2a_dist=dist_a, c2a_median=dist_a[len(dist_a) // 2], c2a_p95=dist_a[int(0.95 * len(dist_a)) - 1],
               c2a_pool_min=min((len(pool_a.get(k, [])) for k in picks), default=0))
    # paired months (descriptive, not in the verdict): month mean of the picks' trades - month mean of C3
    pm: dict[str, list[float]] = defaultdict(list)
    um: dict[str, list[float]] = defaultdict(list)
    for r in pk:
        if id(r) in vals:
            pm[r["k"]].append(vals[id(r)])
    for r, v in c3:
        um[r["k"]].append(v)
    diff = [(k, st.mean(pm[k]) - st.mean(um[k])) for k in sorted(pm) if um.get(k)]
    out["pair_c3"] = H.mean0([v for _, v in diff])
    out["pair_c3_ci"] = H.block_ci(diff, level=level)
    return out


# ---------------------------------------------------------------- read rules (§9, owner answers)
def m1_criteria(x: dict, ci_key: str = "all_ci") -> dict[str, bool]:
    c = {
        "a_mean_ci": x["all_mean"] > 0 and x[ci_key][0] > 0,
        "b_oos": x["oos_mean"] > 0,
        "c_dd": x["dd_pct"] < DD_MAX,
        "d_d1": x["d1_is"] > 0 and x["d1_oos"] > 0 and x["d1_ci"][0] > 0,
        "d_c2": pctile(x["c2_dist"], x["all_mean"]) >= 95,
        "d_c3": x["is_mean"] > x["c3_is"] and x["oos_mean"] > x["c3_oos"],
        "e_c4": x["c4_is"] > 0 and x["c4_oos"] > 0 and x["c4_ci"][0] > 0,
        "f_min": x["n_all"] >= MIN_TRADES_ALL and x["n_oos"] >= MIN_TRADES_OOS and x["n_months"] >= MIN_MONTHS,
        "f_best3": x["wo_best3"] > 0,
    }
    return c


def m1_fail(x: dict) -> list[str]:
    why = []
    if not x["all_mean"] > 0:
        why.append("all-period mean <= 0")
    if not x["oos_mean"] > 0:
        why.append("OOS mean <= 0")
    if not x["dd_pct"] < DD_MAX:
        why.append("DD >= 25%")
    if not x["d1_all"] > 0:
        why.append("D1 <= 0")
    if not x["all_mean"] >= x["c2_median"]:
        why.append("mean below the C2 median")
    if not x["c4_all"] > 0:
        why.append("all-period C4 <= 0 (beta)")
    return why


def verdict_m1(x: dict, x_noflag: dict) -> str:
    """Raw §9 read: FAIL / INCONCLUSIVE / TRADEABLE CANDIDATE (before the owner-answer caps, label_m1)."""
    if m1_fail(x):
        return "FAIL"
    if all(m1_criteria(x).values()) and not m1_fail(x_noflag) and all(m1_criteria(x_noflag).values()):
        return "TRADEABLE CANDIDATE"
    return "INCONCLUSIVE"


def label_m1(raw: str, universe: str, phase2: str | None = None) -> str:
    """Caps (owner answers): BIASED universe -> 'CANDIDATE (biased universe)'; no fresh sample (Phase 2 dropped) ->
    a candidate for a small live test at best. Only a Phase-2 PASS on PIT would allow TRADEABLE."""
    if raw != "TRADEABLE CANDIDATE":
        return raw
    if universe != "PIT":
        return "CANDIDATE (biased universe) — small live test at most; no capital decision before PIT"
    if phase2 != "PASS":
        return "CANDIDATE — small live test only (no fresh sample)"
    return "TRADEABLE CANDIDATE"


def label_m2(x: dict, x_noflag: dict, phase2: str | None = None) -> str:
    """M2 (seen rule, A10): FAIL as M1; (c)-(f) at 97.5% -> UNCONFIRMED; CONFIRMED only with a Phase-2 PASS."""
    if m1_fail(x):
        return "FAIL"
    c = m1_criteria(x)
    cf = all(v for kk, v in c.items() if kk[0] in "cdef")
    cfn = not m1_fail(x_noflag) and all(v for kk, v in m1_criteria(x_noflag).items() if kk[0] in "cdef")
    if cf and cfn:
        if phase2 == "PASS" and c["a_mean_ci"] and c["b_oos"]:
            return "CONFIRMED CANDIDATE"
        return "UNCONFIRMED — forward test only"
    return "INCONCLUSIVE"


def phase2_read(fresh: dict | None) -> str:
    """§8 Phase 2: owner DROPPED it (2026-11-20). With a supplied 2015-19 result: mean > 0, D1 > 0 with CI > 0 and
    C4 > 0 with CI > 0, else FAIL (final). A pooled CI never enters here."""
    if fresh is None:
        return "DROPPED (owner 2026-11-20) — no fresh sample, nothing promoted"
    ok = (fresh["all_mean"] > 0 and fresh["d1_all"] > 0 and fresh["d1_ci"][0] > 0 and fresh["c4_all"] > 0
          and fresh["c4_ci"][0] > 0)
    return "PASS" if ok else "FAIL"


def shorts_go(s: dict, c5_pct: float) -> tuple[bool, dict[str, bool]]:
    c = {"mean_ci": s["all_mean"] > 0 and s["all_ci"][0] > 0, "oos": s["oos_mean"] > 0,
         "c4": s["c4_is"] > 0 and s["c4_oos"] > 0, "c5": c5_pct >= 95, "best3": s["wo_best3"] > 0,
         "min": s["n_all"] >= MIN_TRADES_ALL and s["n_oos"] >= MIN_TRADES_OOS}
    return all(c.values()), c


def futures_switch(m1_raw: str, ls: dict, shorts: bool, lfut_mean: float, ldeliv_mean: float, gate_pass: bool,
                   sizable: float, lot_dd_pct: float, ratio_fut: float, ratio_deliv: float
                   ) -> tuple[bool, dict[str, bool]]:
    """§9 'switch to futures'. (5) is a ratio (post-tax Rs/yr ÷ max MTM DD Rs), so position size cannot win it."""
    c = {"1_not_fail": m1_raw != "FAIL",
         "2_ls": ls["all_mean"] > 0 and ls["all_ci"][0] > 0 and ls["oos_mean"] > 0,
         "3_shorts_or_cost": shorts or lfut_mean > ldeliv_mean,
         "4_lots": gate_pass and sizable >= 0.90 and lot_dd_pct < DD_MAX,
         "5_ratio": ratio_fut > ratio_deliv}
    return all(c.values()), c


def arm_dd(R: list[tuple[dict, float]], arm: str, cal: list[str], slip: float = SLIP_D) -> float | None:
    """MTM max DD (% of 10L) of one arm's trades at §1 sizes (delivery). None if any trade has no marks."""
    if not R or not all("marks" in r[arm] for r, _ in R):
        return None
    tr = [(r, r[arm]) for r, _ in R]
    pnl = mtm_daily_arm([(r["qty"], o, r["qty"] * cost_ps(r, o, slip, "deliv")) for r, o in tr], cal)
    span = span_of(cal, min(r["d1"] for r, _ in tr), pnl)
    return H.dd_vs_capital([(d, pnl.get(d, 0.0)) for d in span])["pct_cap"]


def c1_finding(x: dict, cal: list[str] | None = None, slip: float = SLIP_D) -> tuple[bool, list[str]]:
    """§9 'If C1/C1n passes (a)-(c) but D1 <= 0: the finding is "buy the picks on the 1st"' (review R2: judged on
    the C1 / C1n arms' own trades, not on the L arm). Owner O3 (2026-11-21): (c) = MTM max DD < 25% of 10L from the
    C1 / C1n daily marks (FORMAT 2). Rows without marks (or no calendar) -> (c) unknown and the finding can at most
    be 'PENDING (c)'; it never fires on an unknown (c). Returns (fires, lines)."""
    out, fires = [], False
    for a in ("c1", "c1n"):
        R = x[a + "_R"]
        if not R:
            out.append(f"  {a.upper()}: no trades")
            continue
        s = summary_R(R)
        ab = s["all_mean"] > 0 and s["all_ci"][0] > 0 and s["oos_mean"] > 0
        dd = arm_dd(R, "C1" if a == "c1" else "C1n", cal, slip) if cal else None
        c = None if dd is None else dd < DD_MAX
        hit = ab and c is True and x["d1_all"] <= 0
        fires = fires or hit
        ctxt = "not computed (no marks)" if dd is None else f"{dd:.1f}% of 10L -> {'ok' if c else 'NO'}"
        if hit:
            tail = "C1-type finding: a new pre-registration (§9), not a result of this one"
        elif ab and c is None and x["d1_all"] <= 0:
            tail = "C1-type finding PENDING (c)"
        else:
            tail = "no finding"
        out.append(f"  {a.upper()} (a) mean>0 & CI>0, (b) OOS>0: {'ok' if ab else 'NO'}; (c) MTM DD: {ctxt}; "
                   f"D1 <= 0: {x['d1_all'] <= 0} -> {tail}")
    return fires, out


def compound_go(candidate: bool, absorbs_1cr: bool, cagr_tax: float | None, cagr_nifty: float | None,
                dd: float, dd_nifty: float) -> bool:
    return (candidate and absorbs_1cr and cagr_tax is not None and cagr_nifty is not None
            and cagr_tax >= cagr_nifty and dd <= dd_nifty)


# ---------------------------------------------------------------- tax
def long_term(ed: str, xd: str) -> bool:
    """Held > 12 months: the exit is after the same calendar date one year on."""
    a = date.fromisoformat(ed)
    try:
        one = a.replace(year=a.year + 1)
    except ValueError:                        # 29 Feb -> 1 Mar
        one = date(a.year + 1, 3, 1)
    return date.fromisoformat(xd) > one


def tax_cg(fy_gains: list[tuple[int, float, float]], st_rate: float = STCG, lt_rate: float = LTCG,
           exempt: float = LTCG_EXEMPT, carry_years: int = CARRY_FY) -> list[dict]:
    """Capital-gains tax per FY with set-off and carry-forward (A9). fy_gains: (fy, net STCG, net LTCG), FYs strictly
    increasing (a gap still ages a loss)."""
    fys = [f for f, _, _ in fy_gains]
    if any(b <= a for a, b in zip(fys, fys[1:])):
        raise ValueError(f"FYs not strictly increasing: {fys}")
    stl: list[list] = []                      # [origin fy, amount] brought-forward ST losses, oldest first
    ltl: list[list] = []
    out = []
    for fy, sg, lg in fy_gains:
        expired = sum(a for o, a in stl + ltl if fy - o > carry_years)
        stl = [x for x in stl if fy - x[0] <= carry_years]
        ltl = [x for x in ltl if fy - x[0] <= carry_years]
        if sg < 0 < lg:                       # current-year ST loss -> LT gains
            use = min(-sg, lg)
            sg, lg = sg + use, lg - use

        def apply(losses: list[list], gain: float) -> float:
            for lot in losses:
                if gain <= 0:
                    break
                use = min(lot[1], gain)
                lot[1] -= use
                gain -= use
            return gain

        setoff0 = sum(a for _, a in stl + ltl)
        if lg > 0:
            lg = apply(ltl, lg)
        if sg > 0:
            sg = apply(stl, sg)
        if lg > 0:
            lg = apply(stl, lg)
        stl = [x for x in stl if x[1] > 0]
        ltl = [x for x in ltl if x[1] > 0]
        setoff = setoff0 - sum(a for _, a in stl + ltl)
        if sg < 0:
            stl.append([fy, -sg])
            sg = 0.0
        if lg < 0:
            ltl.append([fy, -lg])
            lg = 0.0
        lt_tax_base = max(0.0, lg - exempt)
        out.append(dict(fy=fy, st=sg, lt=lg, lt_taxable=lt_tax_base, setoff=setoff, expired=expired,
                        tax=st_rate * sg + lt_rate * lt_tax_base, carry=sum(a for _, a in stl + ltl)))
    return out


def fy_series(pnl_by_fy: dict[int, list[float]]) -> list[tuple[int, float, float]]:
    """{fy: [st, lt]} -> continuous FY list (missing FYs as zero, so carry ages)."""
    if not pnl_by_fy:
        return []
    out: list[tuple[int, float, float]] = []
    for f in range(min(pnl_by_fy), max(pnl_by_fy) + 1):
        s, lg = pnl_by_fy.get(f, [0.0, 0.0])
        out.append((f, s, lg))
    return out


# ---------------------------------------------------------------- compounding (§7)
def compound(months: list[tuple[str, list[str], list[tuple[dict, dict]]]], mode: str, slip: float, cal: list[str],
             tax: str | None = None, start: float = CAPITAL) -> dict:
    """months: (trade key, period sessions, [(rec, outcome)]) in order. FIXED = §1 qty; COMPOUND = slot equity/10,
    risk 1% of equity (equity = MTM at the previous month end), qty <= 1% of ADT20 (A7). tax: None | 'cg' (STCG/LTCG,
    tax_cg) | 'biz' (CP.apply_tax at STCG_SENS, 8 FYs); the whole FY's tax leaves equity at the first session after
    31 Mar (before that month's sizing). Returns daily equity, trades, fy rows, accrued (last FY, unpaid)."""
    if mode not in ("FIXED", "COMPOUND"):
        raise ValueError(mode)
    pnl: dict[str, float] = defaultdict(float)
    taxes: dict[str, float] = {}
    real: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    trades: list[tuple] = []
    cap_by_fy: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    q0 = 0
    done_fy: int | None = None

    def tax_rows(upto: int) -> list[dict]:
        ser = fy_series({f: v for f, v in real.items() if f <= upto} or {upto: [0.0, 0.0]})
        if tax == "cg":
            return tax_cg(ser)
        return CP.apply_tax([(f, s + lg) for f, s, lg in ser], STCG_SENS, CARRY_FY)

    for k, ss, trs in months:
        d0 = ss[0]
        fy0 = CP.fy_of(d0)
        if tax and done_fy is None:
            done_fy = fy0 - 1
        if tax and done_fy is not None and fy0 - 1 > done_fy and real:
            t = sum(x["tax"] for x in tax_rows(fy0 - 1) if x["fy"] > done_fy)
            if t:
                taxes[d0] = taxes.get(d0, 0.0) + t
            done_fy = fy0 - 1
        eq = start + sum(v for d, v in pnl.items() if d < d0) - sum(v for d, v in taxes.items() if d <= d0)
        items = []
        for r, o in trs:
            if mode == "FIXED":
                q = r["qty"]
            else:
                if eq <= 0:
                    q0 += 1
                    continue
                rng = r["el"] - r["es"]
                qr, qs = math.floor(0.01 * eq / rng), math.floor(eq / TOP_N / r["el"])
                adt = o.get("adt")
                ql = math.floor(PART_MAX * adt / r["el"]) if adt else 0
                q = int(min(qr, qs, ql))
                fy = CP.fy_of(o["xd"])
                cap_by_fy[fy][1] += 1
                cap_by_fy[fy][0] += ql < min(qr, qs)
            if q <= 0:
                q0 += 1
                continue
            cost = q * cost_ps(r, o, slip, "deliv", q)
            items.append((q, o, cost))
            rs = q * (o["exit"] - o["entry"]) - cost
            real[CP.fy_of(o["xd"])][1 if long_term(o["ed"], o["xd"]) else 0] += rs
            trades.append((k, r["sym"], q, q * o["entry"], rs))
        for d, v in mtm_daily_arm(items, cal).items():
            pnl[d] += v
    lo = months[0][1][0] if months else (cal[0] if cal else "")
    span = span_of(cal, lo, pnl)
    eq, daily = start, []
    for d in span:
        eq += pnl.get(d, 0.0) - taxes.get(d, 0.0)
        daily.append((d, eq))
    rows = tax_rows(max(real)) if tax and real else []
    paid_fys = done_fy if done_fy is not None else -1
    accrued = sum(x["tax"] for x in rows if x["fy"] > paid_fys)
    return dict(daily=daily, final=eq, trades=trades, taxes=taxes, fy_rows=rows, accrued=accrued, q0=q0,
                cap_by_fy={f: tuple(v) for f, v in sorted(cap_by_fy.items())}, rs=sum(pnl.values()))


def benchmark_nifty(closes: list[tuple[str, float]], first_d: str, last_d: str, start: float = CAPITAL,
                    div: float = NIFTY_DIV) -> dict | None:
    """NIFTY 50 buy-and-hold on the same dates: price x (1 + div)^years, LTCG at the end (13% above Rs1.25L)."""
    xs = [(d, c) for d, c in closes if first_d <= d <= last_d]
    if len(xs) < 2:
        return None
    d0, p0 = xs[0]
    ser = [(d, start * c / p0 * (1 + div) ** (cal_days(d0, d) / DAYS_PER_YEAR)) for d, c in xs]
    fin = ser[-1][1]
    post = fin - LTCG * max(0.0, fin - start - LTCG_EXEMPT)
    dd = CP.drawdown_vs_peak(ser, start)
    return dict(first=d0, last=xs[-1][0], final=fin, post=post, cagr=CP.cagr(post, start, d0, xs[-1][0]),
                cagr_pre=CP.cagr(fin, start, d0, xs[-1][0]), dd=dd["pct"], n=len(xs))


# ---------------------------------------------------------------- capacity (§6)
def notional_k(r: dict, o: dict, k_cap: float) -> float:
    return min(0.01 * k_cap / (r["el"] - r["es"]), (k_cap / TOP_N) / r["el"]) * o["entry"]


def qty_k(r: dict, k_cap: float) -> int:
    return int(min(math.floor(0.01 * k_cap / (r["el"] - r["es"])), math.floor((k_cap / TOP_N) / r["el"])))


def qstats(xs: list[float]) -> dict:
    if not xs:
        return dict(n=0)
    s = sorted(xs)
    q = lambda p: s[min(len(s) - 1, int(p * len(s)))]  # noqa: E731
    return dict(n=len(s), med=st.median(s), p90=q(0.90), p99=q(0.99), max=s[-1],
                gt1=100 * sum(v > PART_MAX for v in s) / len(s), gt5=100 * sum(v > PART_HARD for v in s) / len(s))


def capacity(tr: list[tuple[dict, dict]], k_cap: float, slip: float) -> dict:
    """Participation = notional_K / ADT20, all and by year; bars needed at entry / at stop exits; Rs with qty
    capped at 1% of ADT20 vs uncapped; ABSORBS K (frozen line)."""
    part, by_y, nadt = [], defaultdict(list), 0
    eb, sb = [], []
    unc = capd = 0.0
    for r, o in tr:
        adt = o.get("adt")
        nk = notional_k(r, o, k_cap)
        if o.get("ev"):
            eb.append(nk / (o["ev"] * o["entry"]))
        if o["reason"] in ("stop", "gap_stop") and o.get("xv"):
            sb.append(nk / (o["xv"] * o["exit"]))
        q = qty_k(r, k_cap)
        unit = o["side"] * (o["exit"] - o["entry"])
        if q > 0:
            unc += q * unit - q * cost_ps(r, o, slip, "deliv", q)
        if not adt:
            nadt += 1
            continue
        part.append(nk / adt)
        by_y[r["k"][:4]].append(nk / adt)
        qc = min(q, math.floor(PART_MAX * adt / r["el"]))
        if qc > 0:
            capd += qc * unit - qc * cost_ps(r, o, slip, "deliv", qc)
    a = qstats(part)
    absorbs = (a.get("n", 0) > 0 and a["p90"] <= ABSORB_P90 and a["p99"] <= ABSORB_P99 and unc > 0
               and capd >= ABSORB_RS * unc)
    return dict(all=a, by_year={y: qstats(v) for y, v in sorted(by_y.items())}, no_adt=nadt,
                entry_bars=qstats(eb), stop_bars=qstats(sb), rs_unc=unc, rs_cap=capd, absorbs=absorbs)


def utilisation(tr: list[tuple[dict, dict]], cal: list[str], span: list[str]) -> float:
    held = sum(r["qty"] * o["entry"] * (bisect.bisect_right(cal, o["xd"]) - bisect.bisect_left(cal, o["ed"]))
               for r, o in tr)
    return held / (CAPITAL * len(span)) if span else float("nan")


# ---------------------------------------------------------------- futures in rupees (§4)
def fut_rs_view(tr: list[tuple[dict, dict]], lots: dict[str, dict[str, list]] | None, slip: float,
                cal: list[str]) -> dict:
    """lots None -> fallback: fractional quantity RISK_F / range at 1 Cr with futures costs ('ignores lot size').
    Otherwise lots = floor(RISK_F / (lot x range)); 0 lots or no lot -> skipped and COUNTED."""
    items, skipped0, nolot, sized = [], 0, 0, []
    for r, o in tr:
        rng = r["el"] - r["es"]
        if lots is None:
            q: float = RISK_F / rng
        else:
            e = lots.get(r["k"], {}).get(r["sym"])
            lot = e[0] if e else None
            if not lot:
                nolot += 1
                continue
            n = math.floor(RISK_F / (lot * rng))
            if n <= 0:
                skipped0 += 1
                continue
            q = n * lot
        items.append((q, o, q * cost_ps(r, o, slip, "fut")))
        sized.append((q, o))
    pnl = mtm_daily_arm(items, cal)
    lo = min((o["ed"] for _, o in sized), default=None)
    span = span_of(cal, lo, pnl) if lo else []
    daily = [(d, pnl.get(d, 0.0)) for d in span]
    dd = H.dd_vs_capital(daily)["dd"] if daily else 0.0
    notional: dict[int, float] = defaultdict(float)
    for q, o in sized:
        for i in range(bisect.bisect_left(cal, o["ed"]), bisect.bisect_right(cal, o["xd"])):
            notional[i] += q * o["entry"]
    peak = max(notional.values(), default=0.0)
    years = cal_days(span[0], span[-1]) / DAYS_PER_YEAR if len(span) > 1 else float("nan")
    fy: dict[int, float] = defaultdict(float)
    for q, o, c in items:
        fy[CP.fy_of(o["xd"])] += q * o["side"] * (o["exit"] - o["entry"]) - c
    return dict(n=len(tr), sized=len(items), skipped0=skipped0, nolot=nolot,
                sizable=len(items) / len(tr) if tr else float("nan"), rs=sum(pnl.values()),
                rs_yr=sum(pnl.values()) / years if years == years and years > 0 else float("nan"),
                dd_rs=dd, dd_pct=100 * dd / FUT_CAPITAL, peak_notional_x=peak / FUT_CAPITAL,
                peak_margin_x=MARGIN * peak / FUT_CAPITAL, fy=dict(fy), years=years)


def non_fo(tr: list[tuple[dict, dict]], lots: dict[str, dict[str, list]]) -> int:
    """Trades whose stock is not in the trade month's F&O bhavcopy (lot source 'not_in_file' = not an F&O member
    that month; review R5): such a trade has no futures contract at all."""
    return sum(1 for r, _ in tr if (lots.get(r["k"], {}).get(r["sym"]) or [None, "not_in_file"])[1] == "not_in_file")


def post_tax_ratio(fy_pnl: dict[int, float], years: float, dd_rs: float, kind: str) -> float:
    """Post-tax Rs per year ÷ max MTM DD Rs. kind 'cg' = delivery STCG 20.8% (tax_cg), 'fut' = slab 31.2%."""
    if not fy_pnl or not dd_rs > 0 or not years > 0:
        return float("nan")
    ser = [(f, fy_pnl.get(f, 0.0)) for f in range(min(fy_pnl), max(fy_pnl) + 1)]
    if kind == "cg":
        tax = sum(x["tax"] for x in tax_cg([(f, v, 0.0) for f, v in ser]))
    else:
        tax = sum(x["tax"] for x in CP.apply_tax(ser, FUT_TAX, CARRY_FY))
    return (sum(fy_pnl.values()) - tax) / years / dd_rs


# ---------------------------------------------------------------- M3 (§2, reported only)
def alloc_slots(rank: dict[str, list[dict]], months: list[tuple[str, str]], top_n: int = TOP_N
                ) -> tuple[list[dict], list[dict], dict[str, int]]:
    """months: (trade key, first session) in order. At each month start: positions still open (exit on or after the
    first session) hold their slots; new picks = top (top_n - open) by rank, held names excluded. Returns picks,
    positions (records whose M3 walk exists = triggered), free slots per month."""
    open_pos: list[dict] = []
    picks, pos, free = [], [], {}
    for k, d0 in months:
        open_pos = [r for r in open_pos if r["M3"]["xd"] >= d0]
        held = {r["sym"] for r in open_pos}
        n = top_n - len(open_pos)
        free[k] = n
        new = [r for r in rank.get(k, []) if r["sym"] not in held][:max(n, 0)]
        for r in new:
            picks.append(r)
            if r.get("M3") and r["qty"] > 0:
                pos.append(r)
                open_pos.append(r)
    return picks, pos, free


def m3_eval(m1_recs: list[dict], plan_m1: list, cal: list[str], slip: float, m1_monthly: list[tuple[str, float]]
            ) -> dict:
    months = [(k, ss[0]) for _, _, k, ss, _ in plan_m1]
    picks, pos, free = alloc_slots(ranked(m1_recs), months)
    items = [(r["qty"], r["M3"], r["qty"] * cost_ps(r, r["M3"], slip, "deliv")) for r in pos]
    pnl = mtm_daily_arm(items, cal)
    lo = months[0][1] if months else cal[0]
    mon: dict[str, float] = defaultdict(float)
    for d in cal:
        if d >= lo:
            mon[d[:7]] += pnl.get(d, 0.0)
    ser = [(m, 100 * v / CAPITAL) for m, v in sorted(mon.items())]
    vals = [v for _, v in ser]
    m1m = dict(m1_monthly)
    diff = [v - m1m[m] for m, v in ser if m in m1m]
    R = [(r, r_arm(r, r["M3"], slip, "deliv")) for r in pos]
    fut = [(r, r_arm(r, r["M3"], SLIP_F, "fut")) for r in pos]
    return dict(n=len(pos), n_picks=len(picks), monthly=ser, mean=H.mean0(vals), ci=mbb_ci(vals),
                diff=H.mean0(diff), diff_ci=mbb_ci(diff), R=R, mean_R=H.mean0([v for _, v in R]),
                fut_R=H.mean0([v for _, v in fut]), reasons=dict(Counter(r["M3"]["reason"] for r in pos)),
                lt=sum(long_term(r["M3"]["ed"], r["M3"]["xd"]) for r in pos),
                rolls=H.mean0([len(r["M3"]["rolls"]) for r in pos]), flagged=sum(r["M3"]["flag"] for r in pos),
                free_min=min(free.values(), default=0), rs=sum(pnl.values()),
                months_held=H.mean0([cal_days(r["M3"]["ed"], r["M3"]["xd"]) / 30.44 for r in pos]))


# ---------------------------------------------------------------- KAT 1 identity (check 9)
OUTCOME_KEYS = ("status", "side", "entry", "exit", "reason", "et", "xt", "ed", "xd", "at_open", "marks", "gap_entry")
REC_KEYS = ("sig0", "rvol", "atr_pct", "close", "el", "es", "qty", "capped", "flag", "n_hold", "miss_hold",
            "part_hold", "d2")


def m2_matches_htf(m2: list[dict], m2_skips: dict, s3: list[dict], s3_skips: dict) -> tuple[bool, Counter]:
    """M2 L-deliv vs htf S3 P, record for record and selection for selection. Returns (identical, mismatch counts);
    prints nothing (a failure must not dump trades)."""
    bad: Counter = Counter()
    a = {(r["sym"], r["k"]): r for r in m2}
    b = {(r["sym"], r["k"]): r for r in s3}
    if set(a) != set(b):
        bad["key_set"] += len(set(a) ^ set(b))
    if dict(m2_skips) != dict(s3_skips):
        bad["skips"] += 1
    for key in set(a) & set(b):
        x, y = a[key], b[key]
        for f in REC_KEYS:
            xv = x["d1"] if f == "d2" else x.get(f)
            if xv != y.get(f):
                bad[f] += 1
        for f in OUTCOME_KEYS:
            if x["L"].get(f) != y["P"].get(f):
                bad["P." + f] += 1
    pa = {k: [r["sym"] for r in v] for k, v in H.select_picks(m2).items()}
    pb = {k: [r["sym"] for r in v] for k, v in H.select_picks(s3).items()}
    if pa != pb:
        bad["picks"] += 1
    return not bad, bad


def kat1_real() -> tuple[bool, Counter, int]:
    """Stream stock by stock (32-bit memory): monthly rows M2 vs htf rows S3."""
    tot: Counter = Counter()
    m2_all: list[dict] = []
    s3_all: list[dict] = []
    n = 0
    for s in json.loads(Y.UNIVERSE.read_text()):
        x = json.loads((ROWS_DIR / f"{F.fname(s)}.json").read_text())
        y = json.loads((H.ROWS_DIR / f"{F.fname(s)}.json").read_text())
        if y.get("format") != H.FORMAT:
            raise SystemExit("htf rows format mismatch: rerun htf rows")
        _, bad = m2_matches_htf(x["recs"]["M2"], x["skips"]["M2"], y["recs"]["S3"], y["skips"]["S3"])
        tot.update(bad)
        keep = ("sym", "k", "rvol", "atr_pct")
        m2_all += [{kk: r[kk] for kk in keep} for r in x["recs"]["M2"]]
        s3_all += [{kk: r[kk] for kk in keep} for r in y["recs"]["S3"]]
        n += len(x["recs"]["M2"])
    pa = {k: [r["sym"] for r in v] for k, v in H.select_picks(m2_all).items()}
    pb = {k: [r["sym"] for r in v] for k, v in H.select_picks(s3_all).items()}
    if pa != pb:
        tot["picks_all"] += 1
    return not tot, tot, n


# ---------------------------------------------------------------- checks (no results)
def checks() -> None:
    p = H.load_prep()
    pm = load_mprep()
    cal = p["cal"]
    bad = H.data_guard(p)
    print(f"(0) data guards (htf prep: calendar ends LAST_DAY, every stock has 5-min bars through it, 5-min vs daily "
          f"H/L): {'ok' if not bad else 'FAIL ' + str(bad)}")
    plan = build_plan(cal)
    for rule in RULES:
        ks = [k for _, _, k, _, _ in plan[rule]]
        print(f"(1) {rule}: eligible trade periods {len(ks)} (IS {sum(is_m(k) for k in ks)}, OOS "
              f"{sum(not is_m(k) for k in ks)}) {ks[0] if ks else '-'}..{ks[-1] if ks else '-'}")
    allm = sorted({d[:7] for d in cal})
    el = {k for k, _ in H.periods(cal, "month")}
    print(f"(1b) calendar months {len(allm)}; not eligible (< {MIN_SESS_MONTH} sessions or final incomplete): "
          f"{[m for m in allm if m not in el]}")
    recs, skips, special = load_rows()
    print(f"(2) special-session daily bars removed before SMA/ATR/CA: {special}")
    for rule in RULES:
        print(f"(2) {rule} BIASED stock-period outcomes: {dict(sorted(skips[rule].items()))}")
    for rule in RULES:
        picks = select_for(rule)(recs[rule])
        cnt = Counter(len(v) for v in picks.values())
        print(f"(3) {rule} picks per period: {sorted(cnt.items())}; periods with no pick: "
              f"{len(plan[rule]) - len(picks)}")
    for rule in ("M1", "M2"):
        pk = [r for v in select_for(rule)(recs[rule]).values() for r in v]
        st_l = Counter(r["L"]["status"] for r in pk)
        lt = [r for r in pk if H.traded(r, "L")]
        ls = [r for r in pk if H.traded(r, "LS")]
        sides = Counter("long" if r["LS"]["side"] > 0 else "short" for r in ls)
        sh = [r for r in ls if r["LS"]["side"] < 0]
        c1 = Counter(r["C1"]["status"] for r in pk)
        print(f"(4) {rule} picks {len(pk)}: L-arm status {dict(st_l)}; L trades {len(lt)} (OOS "
              f"{sum(not is_m(r['k']) for r in lt)}, months with >= 1 trade {len({r['k'] for r in lt})}); LS sides "
              f"{dict(sides)}; shorts OOS {sum(not is_m(r['k']) for r in sh)}; C1 status {dict(c1)}")
        print(f"(4) {rule} MIN_TRADES reachable (L): all {len(lt)} >= {MIN_TRADES_ALL} "
              f"{len(lt) >= MIN_TRADES_ALL}; OOS >= {MIN_TRADES_OOS} "
              f"{sum(not is_m(r['k']) for r in lt) >= MIN_TRADES_OOS}; "
              f"months >= {MIN_MONTHS} {len({r['k'] for r in lt}) >= MIN_MONTHS}; shorts all "
              f"{len(sh) >= MIN_TRADES_ALL}, OOS {sum(not is_m(r['k']) for r in sh) >= MIN_TRADES_OOS}")
        exits = Counter(r["L"]["reason"] for r in lt)
        print(f"(4b) {rule} L exit reasons (counts): {dict(exits)}; picks with missing / partial hold sessions: "
              f"{sum(r['miss_hold'] > 0 for r in pk)} / {sum(r['part_hold'] > 0 for r in pk)}; flagged "
              f"{sum(r['flag'] for r in pk)}; late listings {sum(r['late'] for r in pk)}")
        rng = [(r["el"] - r["es"]) / r["close"] * 100 for r in pk]
        cd = [cost_ps(r, r["L"], SLIP_D, "deliv") / (r["el"] - r["es"]) for r in lt]
        cf = [cost_ps(r, r["L"], SLIP_F, "fut") / (r["el"] - r["es"]) for r in lt]
        if rng and cd:
            print(f"(5) {rule} picks: median planned range {st.median(rng):.2f}% of price; median cost per trade "
                  f"delivery @ {SLIP_D * 100:.2f}%/side {st.median(cd):.3f}R, futures @ {SLIP_F * 100:.2f}%/side "
                  f"incl. carry {st.median(cf):.3f}R")
        else:
            print(f"(5) {rule}: cannot compute ({len(rng)} picks, {len(cd)} trades)")
    for rule in ("M1c6", "M2c6"):
        pk = [r for v in select_for(rule)(recs[rule]).values() for r in v]
        print(f"(4c) {rule} picks {len(pk)}, L trades {sum(H.traded(r, 'L') for r in pk)}")
    g = pm["gate"]
    print(f"(6) lot gate (old method on UDiFF months {g.get('udiff_from')}..): exact {g.get('exact', 0)} / "
          f"{g.get('total', 0)} = {100 * g['pct']:.2f}% (wrong {g.get('wrong', 0)}, none {g.get('none', 0)}) -> "
          f"{'PASS' if g['passed'] else 'FAIL: lot view dropped, fallback only'} (gate {LOT_GATE:.0%}); "
          f"bhavcopy formats {g.get('formats')}; first misses {g.get('misses', [])[:6]}")
    lots = pm["lots"]
    src = Counter(x[1] for m in lots.values() for x in m.values())
    near = sum(1 for m in lots.values() for x in m.values() if x[3])
    print(f"(6) lot sources over all (month, stock): {dict(src)}; near-month fallbacks {near}")
    pk1 = [r for v in select_rank(recs["M1"]).values() for r in v]
    for arm in ("L", "LS"):
        tr = [(r, r[arm]) for r in pk1 if H.traded(r, arm)]
        nolot = zero = 0
        for r, o in tr:
            e = lots.get(r["k"], {}).get(r["sym"])
            if not e or not e[0]:
                nolot += 1
            elif math.floor(RISK_F / (e[0] * (r["el"] - r["es"]))) <= 0:
                zero += 1
        print(f"(6) M1 {arm} trades {len(tr)}: no lot {nolot}, 0 lots at 1 Cr {zero}, sizable "
              f"{len(tr) - nolot - zero}")
    print(f"(7) PIT gate: {pit_gate_status()} -> universe for this run = BIASED (today's 218), label capped")
    lt1 = [r for r in pk1 if H.traded(r, "L")]
    by_y: dict[str, list[float]] = defaultdict(list)
    none_adt = 0
    for r in lt1:
        if r["L"].get("adt"):
            by_y[r["k"][:4]].append(r["L"]["adt"] / LQ.CR)
        else:
            none_adt += 1
    med = {y: round(st.median(v), 1) for y, v in sorted(by_y.items())}
    print(f"(8) M1 L trades: median ADT20 (Rs Cr) by year {med}; "
          f"no ADT20 {none_adt}")
    elig = load_eligible()
    for rule in ("M1", "M2"):
        keep = fo_keep(rule, elig)
        pk = [r for v in select_for(rule)(recs[rule]).values() for r in v]
        sub = [r for v in select_for(rule)([r for r in recs[rule] if keep(r)]).values() for r in v]
        nl = [r for v in select_for(rule)([r for r in recs[rule] if not r["late"]]).values() for r in v]
        pool_a = c2a_pool({k: [r for r in recs[rule] if r["k"] == k] for k in {r["k"] for r in recs[rule]}})
        print(f"(8b) {rule}: picks that were F&O members at the decision snapshot {sum(keep(r) for r in pk)}/"
              f"{len(pk)}; "
              f"O1 member-only picks {len(sub)} (L trades {sum(H.traded(r, 'L') for r in sub)}); O2 no-late picks "
              f"{len(nl)} (L trades {sum(H.traded(r, 'L') for r in nl)}); C2a ATR-eligible pool per month min "
              f"{min(len(v) for v in pool_a.values())}, median {st.median(len(v) for v in pool_a.values())}")
    ok, mism, n = kat1_real()
    print(f"(9) KAT 1 identity M2 == htf S3 (records, skips, outcomes, picks): {ok} ({n} M2 records; mismatch "
          f"fields {dict(mism)})")
    m3n = sum(1 for r in recs["M1"] if "M3" in r)
    oae = sum(1 for r in recs["M1"] if r.get("M3", {}).get("reason") == "open_at_end")
    print(f"(9b) M3 walks attached: {m3n}; open_at_end {oae}")
    print("(10) no result line printed by these checks.")
    if bad:
        raise SystemExit("HARD FAIL (pre-run checks):\n" + "\n".join(bad))


# ---------------------------------------------------------------- report (pure lines + thin CLI)
def fci(ci: tuple[float, float]) -> str:
    return f"[{ci[0]:+.3f},{ci[1]:+.3f}]"


def arm_line(name: str, x: dict) -> str:
    return (f"{name}: trades {x['n_all']} (IS {x['n_is']}, OOS {x['n_oos']}; months {x['n_months']}) win "
            f"{x['win']:.0f}% | mean {x['all_mean']:+.3f}R {fci(x['all_ci'])} | IS {x['is_mean']:+.3f} "
            f"{fci(x['is_ci'])} | OOS {x['oos_mean']:+.3f} {fci(x['oos_ci'])} | net % of notional "
            f"{x['pct_all']:+.2f}% (IS {x['pct_is']:+.2f}, OOS {x['pct_oos']:+.2f}) | without best 3 months "
            f"{x['wo_best3']:+.3f}R")


def mtm_line(x: dict) -> str:
    if "dd" not in x:
        return "  MTM: not computed (no marks)"
    dd = x["dd"]
    return (f"  MTM Rs {x['rs']:+,.0f}; monthly MTM % of 10L mean {x['mon_mean']:+.2f}% {fci(x['mon_ci'])} "
            f"(IS {x['mon_is']:+.2f}, OOS {x['mon_oos']:+.2f}); max DD Rs {dd['dd']:,.0f} = {dd['pct_cap']:.1f}% of "
            f"10L ({dd['peak']}..{dd['trough']}); IS DD {x['dd_is']:.1f}%, OOS DD {x['dd_oos']:.1f}%")


def cas_line(recs: list[dict], rule: str, cal: list[str], uni: tuple, level: float = 0.95) -> str:
    """HIGHER_TF_DESIGN §13 F6 (carried over by MONTHLY_DESIGN §5): a report line without the periods whose signal
    range starts on / after the closing auction (signal session 1 >= CAS_START). Not in the verdict."""
    x = evaluate_arm(recs, rule, "L", "deliv", SLIP_D, cal, uni, level=level,
                     keep=lambda r: r["sig0"] < H.CAS_START)
    return (f"  [F6, not in the verdict] {rule} without signal periods from {H.CAS_START} (closing auction): trades "
            f"{x['n_all']}, mean {x['all_mean']:+.3f}R, OOS {x['oos_mean']:+.3f} ({x['n_oos']})")


def filter_block(x: dict, rule: str = "M1") -> list[str]:
    """Owner 11-20: 'the filter is key'. Picks vs C2 random (same pool, same count), vs C3 every eligible stock,
    and the C4 equal-weight excess. Review R8: price > 100 and range >= 0.5% are record guards, so they are in the
    C2 / C3 pools too; the block measures only the ATR guard + the RVOL ranking (and M2's RVOL >= 1.5)."""
    pc = pctile(x["c2_dist"], x["all_mean"])
    sel = "ATR% > 1.5, ranked by RVOL_M, top 10" if m1_type(rule) else "ATR% > 1.5, RVOL(3 d) >= 1.5, ranked, top 10"
    return [
        f"  WHAT THE FILTER ADDS ({sel}; price > 100 and range >= 0.5% are guards shared with the C2/C3 pools, so "
        "they are NOT measured here):",
        f"    picks mean {x['all_mean']:+.3f}R vs C2 random (same months, same count, base-eligible pool, "
        f"{len(x['c2_dist'])} draws): median {x['c2_median']:+.3f}, 95th {x['c2_p95']:+.3f} -> picks at the "
        f"{pc:.1f}th percentile; picks - C2 median {x['all_mean'] - x['c2_median']:+.3f}R",
        f"    C2a random from the ATR%-eligible pool only (what the RVOL ranking adds on its own; descriptive, owner "
        f"O5): median {x['c2a_median']:+.3f}, 95th {x['c2a_p95']:+.3f} -> picks at the "
        f"{pctile(x['c2a_dist'], x['all_mean']):.1f}th percentile; picks - C2a median "
        f"{x['all_mean'] - x['c2a_median']:+.3f}R (smallest month pool {x['c2a_pool_min']})",
        f"    picks vs C3 unfiltered universe (every eligible stock-month): C3 all {x['c3_all']:+.3f} | IS picks "
        f"{x['is_mean']:+.3f} vs {x['c3_is']:+.3f} | OOS picks {x['oos_mean']:+.3f} vs {x['c3_oos']:+.3f}; "
        f"paired month difference picks - C3 {x['pair_c3']:+.3f}R {fci(x['pair_c3_ci'])} (descriptive)",
        f"    C4 equal-weight universe excess per trade: all {x['c4_all']:+.3f} {fci(x['c4_ci'])} | IS "
        f"{x['c4_is']:+.3f} | OOS {x['c4_oos']:+.3f} (trades outside the index: {x['c4_missing']})",
    ]


def d1_lines(x: dict) -> list[str]:
    c1, c1n = x["c1_R"], x["c1n_R"]
    return [f"  D1 (L - C1 per pick): all {x['d1_all']:+.3f} {fci(x['d1_ci'])}, IS {x['d1_is']:+.3f}, OOS "
            f"{x['d1_oos']:+.3f}; in universe-excess terms {x['d1x_all']:+.3f} (printed, not in the verdict) | per "
            f"pick: L {x['p_per_pick']:+.3f}, C1 {x['c1_per_pick']:+.3f}, C1n {x['c1n_per_pick']:+.3f}",
            f"  C1 per trade {H.mean0([v for _, v in c1]):+.3f} ({len(c1)}; skipped open <= SL {x['c1_skipped']}); "
            f"C1n per trade {H.mean0([v for _, v in c1n]):+.3f} ({len(c1n)}) — C1n on M1 = 'enter on the 1st, exit "
            f"at month end'"]


def crit_line(c: dict[str, bool]) -> str:
    return " ".join(f"{k}={'ok' if v else 'NO'}" for k, v in c.items())


def load_eligible() -> Callable[[str, str], bool]:
    """F&O membership (owner O1): data/fo_membership.json + data/fo_rename_map.json -> eligible(sym, date)."""
    return SV.make_eligible(json.loads(SV.MEM_FILE.read_text()), json.loads(RENAME_MAP.read_text()))


def fo_keep(rule: str, eligible: Callable[[str, str], bool]) -> Callable[[dict], bool]:
    """O1 keep: the stock was an F&O member at the rule's decision snapshot (pit_ok). A universe filter BEFORE
    selection, so a lower-ranked member may move up (removes the future-entrant channel only; leavers stay missing)."""
    return lambda r: pit_ok(r["sym"], rule, r["sk"], r["k"], eligible)


def not_late(r: dict) -> bool:
    return not r["late"]


def subset_lines(recs: list[dict], rule: str, cal: list[str], uni: tuple, eligible: Callable[[str, str], bool] | None,
                 level: float = 0.95, draws: int = RAND_DRAWS) -> list[str]:
    """Owner O1 (R3) and O2 (R4), DESCRIPTIVE, not in the verdict."""
    L: list[str] = []
    if eligible is None:
        L.append(f"  [O1, descriptive] {rule} F&O members only: not computed (no membership supplied)")
    else:
        x = evaluate_arm(recs, rule, "L", "deliv", SLIP_D, cal, uni, level=level, keep=fo_keep(rule, eligible),
                         controls=True, draws=draws)
        L.append(f"  [O1, descriptive, not in the verdict] {rule} restricted to stocks that were F&O members at the "
                 f"decision snapshot ({'signal' if m1_type(rule) else 'trade'} month):")
        L.append("    " + arm_line(f"{rule} L-deliv, F&O members", x))
        L += ["  " + s for s in filter_block(x, rule)]
    y = evaluate_arm(recs, rule, "L", "deliv", SLIP_D, cal, uni, level=level, keep=not_late)
    L.append(f"  [O2, descriptive, not in the verdict] {rule} without the stocks listed after {H.LATE_AFTER}: trades "
             f"{y['n_all']}, mean {y['all_mean']:+.3f}R {fci(y['all_ci'])}, IS {y['is_mean']:+.3f}, OOS "
             f"{y['oos_mean']:+.3f} ({y['n_oos']})")
    return L


def report_lines(b: dict) -> list[str]:
    """Pure: bundle -> report lines. b: cal, uni (ts, olv, lv), recs {rule: [rec]}, plan, lots, gate, closes
    [(d, close)], universe ('BIASED' | 'PIT'), draws."""
    cal, uni, recs, plan = b["cal"], b["uni"], b["recs"], b["plan"]
    draws = b.get("draws", RAND_DRAWS)
    L: list[str] = []
    L.append("MONTHLY SCHOOL RUN (MONTHLY_DESIGN.md + OWNER ANSWERS 2026-11-20) — M1 PRIMARY = prior-month range "
             "break, flat at month end; M2 = S3 unchanged (confirmation only); M3 reported only.")
    utag = "point-in-time" if b["universe"] == "PIT" else "SURVIVORSHIP-BIASED (today's 218)"
    L.append(f"UNIVERSE: {b['universe']} — {utag}. "
             f"Phase 2 (2015-19): {phase2_read(None)}. Tax base STCG {STCG:.1%}, sensitivity {STCG_SENS:.1%} "
             f"(business income), futures {FUT_TAX:.1%}. Delivery slip {SLIP_D:.2%}/side, futures {SLIP_F:.2%}/side.")

    # ---- M1 L-deliv (verdict arm)
    m1 = evaluate_arm(recs["M1"], "M1", "L", "deliv", SLIP_D, cal, uni, controls=True, draws=draws)
    m1nf = evaluate_arm(recs["M1"], "M1", "L", "deliv", SLIP_D, cal, uni, controls=True, draws=draws,
                        drop=lambda r: r["flag"])
    L.append("\n=== M1 L-deliv (verdict arm) ===")
    L.append("  " + arm_line("M1 L-deliv", m1))
    L.append(mtm_line(m1))
    L += filter_block(m1)
    L += d1_lines(m1)
    raw = verdict_m1(m1, m1nf)
    L.append(f"  criteria: {crit_line(m1_criteria(m1))}; FAIL reasons: {m1_fail(m1) or 'none'}")
    L.append(f"  without flagged (in-hold > 20% gap) trades: n {m1nf['n_all']}, mean {m1nf['all_mean']:+.3f}R; "
             f"criteria {crit_line(m1_criteria(m1nf))}")
    lab = label_m1(raw, b["universe"])
    L.append(f"VERDICT M1 (frozen §9 read): {raw} -> LABEL: {lab}")
    L.append(cas_line(recs["M1"], "M1", cal, uni))
    L += subset_lines(recs["M1"], "M1", cal, uni, b.get("eligible"), draws=draws)

    # ---- futures arms
    L.append("\n=== M1 futures views (R; same R unit) ===")
    lf = evaluate_arm(recs["M1"], "M1", "L", "fut", SLIP_F, cal, uni)
    ls = evaluate_arm(recs["M1"], "M1", "LS", "fut", SLIP_F, cal, uni)
    sf = evaluate_arm(recs["M1"], "M1", "LS", "fut", SLIP_F, cal, uni, side=-1)
    lsd = evaluate_arm(recs["M1"], "M1", "LS", "deliv", SLIP_D, cal, uni, side=1)
    L.append("  " + arm_line("L-fut (longs, futures cost + carry)", lf))
    L.append("  " + arm_line("LS-fut (first trigger either side)", ls))
    L.append("  " + arm_line("S-fut (shorts)", sf) + f" | C4 short IS {sf['c4_is']:+.3f} OOS {sf['c4_oos']:+.3f}")
    ident = [round(v, 12) for _, v in lsd["trades"]] == [round(v, 12) for _, v in m1["trades"]]
    L.append(f"  identity: LS longs at delivery cost == L-deliv trade for trade: {ident}")
    pairs = [(r_arm(r, o, SLIP_F, "fut"), r_arm(r, o["opp"], SLIP_F, "fut")) for r, o in ls["tr"] if "opp" in o]
    c5 = c5_dist(pairs, draws)
    c5p = pctile(c5, ls["all_mean"])
    L.append(f"  C5 random side: median {c5[len(c5) // 2]:+.3f}, 95th {c5[int(.95 * len(c5)) - 1]:+.3f}; LS at "
             f"{c5p:.1f}th pct" if c5 else "  C5: no LS trades")
    sg, sgc = shorts_go(sf, c5p)
    why_s = "" if sgc["min"] else (f" — UNTESTABLE: {sf['n_all']} shorts (OOS {sf['n_oos']}) < frozen MIN_TRADES "
                                   f"{MIN_TRADES_ALL} / {MIN_TRADES_OOS}; dropped by sample size, not by a measured "
                                   "result")
    L.append(f"SHORTS GO/NO-GO (S-fut): {'GO' if sg else 'DROPPED'}{why_s} ({crit_line(sgc)})")
    L.append(f"  trades in names NOT in F&O that month (could not be traded as futures): L-fut "
             f"{non_fo(lf['tr'], b['lots'])}/{len(lf['tr'])}, LS-fut {non_fo(ls['tr'], b['lots'])}/{len(ls['tr'])}, "
             f"S-fut {non_fo(sf['tr'], b['lots'])}/{len(sf['tr'])}")

    # ---- lots, rupees
    gate = b["gate"]
    book = ls["tr"] if sg else lf["tr"]
    fb = fut_rs_view(book, None, SLIP_F, cal)
    L.append(f"\n=== Futures in rupees (book {'LS' if sg else 'L-fut'}, 1 Cr, risk 1%) ===")
    L.append(f"  lot gate: exact {gate.get('exact', 0)}/{gate.get('total', 0)} = {100 * gate['pct']:.2f}% -> "
             f"{'PASS' if gate['passed'] else 'FAIL (lot view dropped)'}; mid-month lot changes ignored")
    L.append(f"  FALLBACK (ignores lot size AND F&O membership: {non_fo(book, b['lots'])}/{len(book)} trades are in "
             f"names not in F&O that month; fractional qty at 1 Cr): Rs/yr {fb['rs_yr']:+,.0f}; MTM DD Rs "
             f"{fb['dd_rs']:,.0f} = {fb['dd_pct']:.1f}% of 1 Cr; peak notional {fb['peak_notional_x']:.2f}x, "
             f"peak margin {fb['peak_margin_x']:.2f}x capital")
    if gate["passed"]:
        lv = fut_rs_view(book, b["lots"], SLIP_F, cal)
        L.append(f"  LOT VIEW: sized {lv['sized']}/{lv['n']} ({100 * lv['sizable']:.0f}%); 0 lots {lv['skipped0']}, "
                 f"no lot {lv['nolot']}; Rs/yr {lv['rs_yr']:+,.0f}; MTM DD Rs {lv['dd_rs']:,.0f} = "
                 f"{lv['dd_pct']:.1f}% of 1 Cr; peak notional {lv['peak_notional_x']:.2f}x, margin "
                 f"{lv['peak_margin_x']:.2f}x")
    else:
        lv = None
        L.append("  LOT VIEW: dropped (gate)")
    yrs = cal_days(m1["daily"][0][0], m1["daily"][-1][0]) / DAYS_PER_YEAR if m1.get("daily") else float("nan")
    dfy: dict[int, float] = defaultdict(float)
    for r, o in m1["tr"]:
        dfy[CP.fy_of(o["xd"])] += r["qty"] * (o["exit"] - o["entry"] - cost_ps(r, o, SLIP_D, "deliv"))
    rat_d = post_tax_ratio(dict(dfy), yrs, m1["dd"]["dd"] if "dd" in m1 else 0.0, "cg")
    rat_f = post_tax_ratio(lv["fy"], lv["years"], lv["dd_rs"], "fut") if lv else float("nan")
    fs, fsc = futures_switch(raw, ls, sg, lf["all_mean"], m1["all_mean"], gate["passed"],
                             lv["sizable"] if lv else 0.0, lv["dd_pct"] if lv else float("inf"), rat_f, rat_d)
    L.append(f"  post-tax Rs/yr ÷ max MTM DD Rs: futures lot view @ {FUT_TAX:.1%} {rat_f:.3f} vs delivery @ "
             f"{STCG:.1%} {rat_d:.3f}")
    L.append(f"SWITCH TO FUTURES: {'YES' if fs else 'NO — stay in cash delivery, long only'} ({crit_line(fsc)})")

    # ---- slippage sensitivity
    L.append("\n  slippage sensitivity M1 L-deliv: slip | trades | mean R [CI] | IS | OOS | MTM DD % | Rs")
    for s in SLIPS_D:
        x = m1 if s == SLIP_D else evaluate_arm(recs["M1"], "M1", "L", "deliv", s, cal, uni)
        L.append(f"  {s * 100:.2f}% | {x['n_all']} | {x['all_mean']:+.3f} {fci(x['all_ci'])} | {x['is_mean']:+.3f} | "
                 f"{x['oos_mean']:+.3f} | {x.get('dd_pct', float('nan')):.1f}% | {x.get('rs', float('nan')):+,.0f}")
    L.append("  slippage sensitivity M1 LS-fut: slip | trades | mean R [CI] | OOS")
    for s in SLIPS_F:
        x = ls if s == SLIP_F else evaluate_arm(recs["M1"], "M1", "LS", "fut", s, cal, uni)
        L.append(f"  {s * 100:.2f}% | {x['n_all']} | {x['all_mean']:+.3f} {fci(x['all_ci'])} | {x['oos_mean']:+.3f}")

    # ---- M2
    L.append("\n=== M2 (= S3 unchanged; seen rule, 97.5% CIs) ===")
    m2 = evaluate_arm(recs["M2"], "M2", "L", "deliv", SLIP_D, cal, uni, level=M2_LEVEL, controls=True, draws=draws)
    m2nf = evaluate_arm(recs["M2"], "M2", "L", "deliv", SLIP_D, cal, uni, level=M2_LEVEL, controls=True,
                        draws=draws, drop=lambda r: r["flag"])
    L.append("  " + arm_line("M2 L-deliv", m2))
    L.append(mtm_line(m2))
    L += filter_block(m2, "M2")
    L += d1_lines(m2)
    L.append(f"  criteria (97.5%): {crit_line(m1_criteria(m2))}; FAIL reasons: {m1_fail(m2) or 'none'}")
    L.append(f"LABEL M2: {label_m2(m2, m2nf)}")
    L.append(cas_line(recs["M2"], "M2", cal, uni, M2_LEVEL))
    L += subset_lines(recs["M2"], "M2", cal, uni, b.get("eligible"), level=M2_LEVEL, draws=draws)

    # ---- M3
    L.append("\n=== M3 trailing (reported, never promoted; long-only delivery; monthly MTM, 3-month moving blocks) ===")
    m3 = m3_eval(recs["M1"], plan["M1"], cal, SLIP_D, m1.get("monthly", []))
    L.append(f"  positions {m3['n']} (picks {m3['n_picks']}); mean R {m3['mean_R']:+.3f} (futures with rolls + carry "
             f"{m3['fut_R']:+.3f}); exits {m3['reasons']}; held > 12 months {m3['lt']}; mean rolls {m3['rolls']:.1f}; "
             f"mean months held {m3['months_held']:.1f}; flagged {m3['flagged']}; Rs {m3['rs']:+,.0f}")
    L.append(f"  monthly MTM % of 10L {m3['mean']:+.2f}% {fci(m3['ci'])}; M3 - M1 monthly {m3['diff']:+.2f}% "
             f"{fci(m3['diff_ci'])} -> {'NEW pre-registration candidate' if m3['diff_ci'][0] > 0 else 'no'}")
    L.append("  §9 C1-type check ('buy the picks on the 1st'), on the C1 / C1n arms' own trades:")
    L += c1_finding(m1, cal)[1]
    c1x = [(r, v) for r, v in m1["c1n_R"]]
    if c1x:
        L.append("  C1n on M1 (owner's literal 'enter on the 1st, exit at month end'): " +
                 arm_line("C1n", dict(summary_R(c1x), pct_all=float("nan"), pct_is=float("nan"),
                                      pct_oos=float("nan"))))

    # ---- C6
    L.append("\n=== C6 (16th -> 15th 'months'; is the month anchor real?) — report only, R only ===")
    for rule in ("M1c6", "M2c6"):
        x = evaluate_arm(recs[rule], rule, "L", "deliv", SLIP_D, cal, uni)
        L.append("  " + arm_line(rule, x))

    # ---- capacity
    L.append("\n=== Capacity (participation = notional_K ÷ ADT20; ABSORBS K: p90 <= 1%, p99 <= 5%, "
             "capped Rs >= 90%) ===")
    c3tr = [(r, r["L"]) for r, _ in m1["c3_R"]]
    absorbs_1cr = False
    for rule_name, tr in (("M1 picks", m1["tr"]), ("M2 picks", m2["tr"]), ("C3 every eligible (M1)", c3tr)):
        for kc in CAP_LEVELS:
            c = capacity(tr, kc, SLIP_D)
            a = c["all"]
            if rule_name == "M1 picks" and kc == 10_000_000.0:
                absorbs_1cr = c["absorbs"]
            if not a.get("n"):
                L.append(f"  {rule_name} K {kc / 1e5:,.0f}L: no ADT20")
                continue
            L.append(f"  {rule_name} K {kc / 1e5:,.0f}L: median {100 * a['med']:.3f}% p90 {100 * a['p90']:.3f}% p99 "
                     f"{100 * a['p99']:.3f}% max {100 * a['max']:.2f}%; > 1% {a['gt1']:.1f}%, > 5% {a['gt5']:.1f}%; "
                     f"bars needed entry p90 {c['entry_bars'].get('p90', float('nan')):.2f}, stop p90 "
                     f"{c['stop_bars'].get('p90', float('nan')):.2f}; capped/uncapped Rs "
                     f"{c['rs_cap']:+,.0f}/{c['rs_unc']:+,.0f}; no ADT20 {c['no_adt']}; ABSORBS {c['absorbs']}")
            if kc == 10_000_000.0:
                L.append("    by year p90: " + ", ".join(f"{y} {100 * v['p90']:.2f}%" for y, v in c["by_year"].items()
                                                         if v.get("n")))
    if m1.get("daily"):
        L.append(f"  utilisation M1 (rupee-days deployed ÷ available, 10L): "
                 f"{100 * utilisation(m1['tr'], cal, [d for d, _ in m1['daily']]):.1f}%")

    # ---- compounding + tax
    L.append("\n=== Compounding and tax (descriptive what-if; start 10L; M1 L-deliv) ===")
    mt = []
    for _, _, k, ss, _ in plan["M1"]:
        mt.append((k, ss, [(r, o) for r, o in m1["tr"] if r["k"] == k]))
    fx = compound(mt, "FIXED", SLIP_D, cal)
    L.append(f"  FIXED: Rs {fx['rs']:+,.0f} (evaluate MTM Rs {m1.get('rs', float('nan')):+,.0f})")
    bench = benchmark_nifty(b["closes"], fx["daily"][0][0], fx["daily"][-1][0]) if fx["daily"] else None
    res = {}
    for name, mode, tx in (("COMPOUND", "COMPOUND", None), ("COMPOUND_TAX STCG", "COMPOUND", "cg"),
                           ("COMPOUND_TAX 31.2% biz", "COMPOUND", "biz")):
        c = compound(mt, mode, SLIP_D, cal, tx)
        if not c["daily"]:
            continue
        d0, d1 = c["daily"][0][0], c["daily"][-1][0]
        post = c["final"] - c["accrued"]
        dd = CP.drawdown_vs_peak(c["daily"])
        res[name] = (CP.cagr(post, CAPITAL, d0, d1), dd["pct"])
        capfy = ", ".join(f"{CP.fy_label(f)} {100 * a / n:.0f}%" for f, (a, n) in c["cap_by_fy"].items() if n)
        L.append(f"  {name}: final Rs {c['final']:,.0f}, accrued tax Rs {c['accrued']:,.0f}, CAGR after it "
                 f"{CP._pct(res[name][0])}; max DD vs peak {dd['pct']:.1f}%; qty 0 {c['q0']}; trades capped by 1% "
                 f"ADT20 per FY: {capfy}")
        for x in c["fy_rows"]:
            L.append(f"    {CP.fy_label(x['fy'])}: tax Rs {x['tax']:,.0f} carry Rs {x['carry']:,.0f}")
    if bench:
        L.append(f"  NIFTY 50 buy-and-hold {bench['first']}..{bench['last']} (price + {NIFTY_DIV:.1%}/yr dividends, "
                 f"LTCG at the end): CAGR post-tax {CP._pct(bench['cagr'])} (pre {CP._pct(bench['cagr_pre'])}); max DD "
                 f"vs peak {bench['dd']:.1f}%; NIFTY close source (official file + declared 1-min tail) "
                 f"{b.get('nifty_src', 'not given')}")
    ct = res.get("COMPOUND_TAX STCG")
    # review R1: the frozen rule needs a TRADEABLE / CONFIRMED CANDIDATE on the verdict universe -> the CAPPED
    # label, never the raw read (BIASED universe and no Phase 2 can never be promoted)
    cand = lab == "TRADEABLE CANDIDATE"
    cg = compound_go(cand, absorbs_1cr, ct[0] if ct else None,
                     bench["cagr"] if bench else None, ct[1] if ct else float("inf"),
                     bench["dd"] if bench else -1.0)
    hurdle = compound_go(True, True, ct[0] if ct else None, bench["cagr"] if bench else None,
                         ct[1] if ct else float("inf"), bench["dd"] if bench else -1.0)
    L.append(f"COMPOUNDING GO/NO-GO: {'YES' if cg else 'NO — fixed size only, or no monthly system'} (M1 label "
             f"'{lab}' is {'' if cand else 'not '}a TRADEABLE CANDIDATE on the verdict universe); descriptive only: "
             f"ABSORBS 1 Cr {absorbs_1cr}, CAGR/DD hurdle vs NIFTY met on its own {hurdle}")
    L.append("  not modelled: interest on idle cash, advance tax, STT not deductible, dividends on held stock "
             "(~0.1%/month: hurts longs, flatters shorts), futures basis; current tax rates applied to all years.")

    # ---- by year, monthly
    L.append("\n  M1 L-deliv by year (trade month): year | trades | mean R | Rs (realised)")
    by_y: dict[str, list] = defaultdict(list)
    for (r, o), (_, v) in zip(m1["tr"], m1["trades"]):
        by_y[r["k"][:4]].append((v, r["qty"] * (o["exit"] - o["entry"] - cost_ps(r, o, SLIP_D, "deliv"))))
    for y in sorted(by_y):
        xs = by_y[y]
        L.append(f"  {y} | {len(xs)} | {H.mean0([a for a, _ in xs]):+.3f} | {sum(c for _, c in xs):+,.0f}")
    if m1.get("monthly"):
        L.append("  M1 monthly MTM % of 10L:")
        mm = dict(m1["monthly"])
        for y in sorted({m[:4] for m in mm}):
            L.append(f"  {y} " + " ".join(f"{mm[f'{y}-{i:02d}']:+6.2f}" if f"{y}-{i:02d}" in mm else "     -"
                                          for i in range(1, 13)))
    return L


def report() -> None:
    p = H.load_prep()
    bad = H.data_guard(p)
    if bad:
        raise SystemExit("report refused, pre-run checks fail:\n" + "\n".join(bad))
    pm = load_mprep()
    recs, _, _ = load_rows()
    cal = p["cal"]
    nf = LQ.load_nifty()
    b = dict(cal=cal, uni=(p["ts"], p["olevels"], p["levels"]), recs=recs, plan=build_plan(cal), lots=pm["lots"],
             gate=pm["gate"], closes=nf["closes"], nifty_src=dict(nf["src"]), universe="BIASED",
             eligible=load_eligible())
    L = report_lines(b)
    print("\n".join(L), flush=True)
    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text("\n".join(L), encoding="utf-8")


def main(argv: list[str]) -> None:
    cmds = {"prep": prep, "rows": rows, "checks": checks, "report": report}
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(argv) != 1 or argv[0] not in cmds:
        raise SystemExit("usage: monthly_orb.py prep|rows|checks|report")
    cmds[argv[0]]()


if __name__ == "__main__":
    main(_argv)
