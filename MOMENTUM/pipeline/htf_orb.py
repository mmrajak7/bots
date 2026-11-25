"""Weekly / monthly school run (HIGHER_TF_DESIGN.md, frozen 2026-11-20, owner "defaults ok").

    python -I htf_orb.py prep      # pass 1: market calendar, universe index, data checks (0)(1) -> scratchpad
    python -I htf_orb.py rows      # pass 2: per stock-period records for P, S1, S2, S3, C1, C1n -> scratchpad
    python -I htf_orb.py checks    # pre-run checks (0)-(6); prints NO result line
    python -I htf_orb.py report    # results (only after tests + independent review)

Deviations from the design (logged here; HIGHER_TF_DESIGN.md has no §13 yet — copy them there):
- C4 universe leg uses an equal-weight 5-min index chained from per-bar mean returns of all stocks, split into a
  gap leg (open / previous close) and a bar leg (close / open), so the index has an OPEN and a CLOSE level per bar
  (bars whose close / previous close moves > 20% are dropped as corporate actions). Entry = index OPEN of the entry
  bar; exit = index CLOSE of the exit bar, except an exit AT the open (gap stop, next-open exit) uses the index OPEN
  of that bar (the stock leg is an open price too).
- S2 is not given its own controls; it can only be named "candidate pending controls".
- IS/OOS of the MTM equity series is split by calendar date; trades by the period's day 1 (one boundary week).
- Daily bars of special sessions (dates outside the calendar that are weekends or whose 5-min bars do not start at
  09:15: Muhurat, Saturday DR sessions) are removed before SMA20 / ATR / CA guard ("ignored everywhere", §5).
- Since NSE's closing auction (2026-08-03) the continuous session ends with the 15:10 bar: post-CAS day-1 ranges,
  entry windows and marks run 09:15-15:10, not 15:25.
- Rerun rule: prep.json and rows/ carry FORMAT; a mismatch is a hard failure (delete htf/ and rerun prep + rows).
"""
from __future__ import annotations

import bisect

import json
import math
import random
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_argv = sys.argv[1:]
sys.argv = sys.argv[:1]                       # full_asrs parses --srs at import; neutral here
import full_asrs as F  # noqa: E402
import nov_stocks as N  # noqa: E402
import y2026_asrs as Y  # noqa: E402

WORK = F.BASE / "htf"
ROWS_DIR = WORK / "rows"
PREP = WORK / "prep.json"

# ---------------------------------------------------------------- §1 frozen parameters
MIN_SESS, MIN_SESS_MONTH = 4, 15
BUF_PCT, TICK = 0.0005, 0.05
RVOL_MIN, ATR_PCT, MIN_PX, MIN_RNG, CA_JUMP = 1.5, 1.5, 100.0, 0.005, 0.20
TOP_N, CAPITAL, RISK, SLOT = 10, 1_000_000.0, 10_000.0, 100_000.0
SLIP, SLIPS = 0.0005, (0.0002, 0.0005, 0.0010)
STT_DELIV, STAMP_BUY, TXN, SEBI, GST, BROK_DELIV, DP_SELL = 0.001, 0.00015, 0.0000297, 0.000001, 0.18, 0.0, 20.0
STT_FUT, STAMP_FUT, TXN_FUT, BROK_FUT = 0.0002, 0.00002, 0.0000173, 20.0 / 750_000
IS_END = "2023-12-31"
BOOT_N, SEED, RAND_DRAWS = 5000, 7, 1000
MIN_TRADES_ALL, MIN_TRADES_OOS = 400, 120
CAL_MIN_STOCKS, HIST_MIN, LATE_AFTER, EXIT_HHMM, UNIV_CLIP = 100, 34, "2019-10-31", "15:10", 0.20
VARIANTS = {"P": ("week", 1), "S2": ("week", 2), "S3": ("month", 3)}
FORMAT = 4                                    # bump on any change to prep / build_record output
CHK1_MAX_MISS = 0.01                          # pre-run check (1): > 1% of stock-days off → STOP
CAS_START = "2026-08-03"                      # NSE closing auction: daily close/H/L can lie outside the 5-min bars


# ---------------------------------------------------------------- calendar
def market_sessions(full_counts: dict[str, int], last_day: str = F.LAST_DAY,
                    open_counts: dict[str, int] | None = None) -> list[str]:
    """Session = >= CAL_MIN_STOCKS stocks with a full session, OR (owner 2026-11-20, review F5) a WEEKDAY where
    >= CAL_MIN_STOCKS stocks open at 09:15 (circuit-halt / outage days such as 2020-03-13, 2020-03-23, 2021-02-24)."""
    days = {d for d, n in full_counts.items() if n >= CAL_MIN_STOCKS}
    for d, n in (open_counts or {}).items():
        if n >= CAL_MIN_STOCKS and date(int(d[:4]), int(d[5:7]), int(d[8:10])).weekday() < 5:
            days.add(d)
    return sorted(d for d in days if d <= last_day)


def period_key(d: str, kind: str) -> str:
    if kind == "month":
        return d[:7]
    iy, iw, _ = date(int(d[:4]), int(d[5:7]), int(d[8:10])).isocalendar()
    return f"{iy}-W{iw:02d}"


def periods(sessions: list[str], kind: str) -> list[tuple[str, list[str]]]:
    """Calendar periods with enough sessions; the period holding the final session is dropped (incomplete)."""
    by: dict[str, list[str]] = defaultdict(list)
    for d in sessions:
        by[period_key(d, kind)].append(d)
    need = MIN_SESS if kind == "week" else MIN_SESS_MONTH
    final = period_key(sessions[-1], kind) if sessions else None
    return [(k, sorted(v)) for k, v in sorted(by.items()) if len(v) >= need and k != final]


# ---------------------------------------------------------------- levels, bars
def buffer(close: float) -> float:
    return math.ceil(close * BUF_PCT / TICK - 1e-9) * TICK


def session_bars(raw: list[dict], d: str, last: bool = False) -> list[dict]:
    """That day's bars with flag f (True = filled grid bar, cannot trigger or fill). Full grid via fill_gaps when
    the day starts 09:15, else the real bars as they are. last=True keeps bars up to the 15:10 bar only."""
    if not raw:
        return []
    raw = sorted(raw, key=lambda b: b["t"])
    if raw[0]["t"][11:] == "09:15":
        grid, _ = F.fill_gaps(raw)
        real = {b["t"] for b in raw}
        bars = [dict(b, f=b["t"] not in real, d=d) for b in grid]
    else:
        bars = [dict(b, f=False, d=d) for b in raw]
    if last:
        bars = [b for b in bars if b["t"][11:] <= EXIT_HHMM]
    return bars


def find_trigger(bars: list[dict], el: float, es: float, sides: str) -> dict | None:
    """First bar touching a level (filled bars ignored). sides 'long': a low-side-first week is void."""
    for i, b in enumerate(bars):
        if b["f"]:
            continue
        up, dn = b["h"] >= el, b["l"] <= es
        if not (up or dn):
            continue
        if b["o"] >= el:
            side, fill, gap = 1, b["o"], True
        elif b["o"] <= es:
            side, fill, gap = -1, b["o"], True
        elif up and not dn:
            side, fill, gap = 1, el, False
        elif dn and not up:
            side, fill, gap = -1, es, False
        else:                                  # both levels, open between: long at EL then stopped (pessimistic;
            side, fill, gap = 1, el, False     # for S1 the short path has identical prices and costs)
        if side < 0 and sides == "long":
            return dict(status="void", idx=i)
        return dict(status="trig", idx=i, side=side, fill=fill, gap=gap)
    return None


def hold(flat: list[dict], i0: int, side: int, stop: float | None) -> tuple[int, float, str, bool] | None:
    """Stop walk from the entry bar i0. Entry bar: touching the stop = stopped AT the stop (pessimistic). Later bars:
    fill = the worse of stop and the bar open (gap). Returns (bar idx, exit px, reason, exit_at_open) or None."""
    if stop is None:
        return None
    for j in range(i0, len(flat)):
        b = flat[j]
        if b["f"]:
            continue
        hit = b["l"] <= stop if side > 0 else b["h"] >= stop
        if not hit:
            continue
        if j == i0:
            return j, stop, "stop", False
        gapped = b["o"] < stop if side > 0 else b["o"] > stop
        return (j, b["o"], "gap_stop", True) if gapped else (j, stop, "stop", False)
    return None


def outcome(flat: list[dict], last_d: str, i0: int, entry: float, side: int, stop: float | None,
            fallback: tuple[str, float] | None) -> dict:
    h = hold(flat, i0, side, stop)
    if h:
        j, x, reason, at_open = h
        xt, xd = flat[j]["t"], flat[j]["d"]
    elif flat and flat[-1]["d"] == last_d:
        x, reason, at_open = flat[-1]["c"], "period_end", False
        xt, xd = flat[-1]["t"], last_d
    elif fallback:
        xt, x = fallback
        reason, at_open, xd = "end_next_open", True, xt[:10]
    else:
        x, reason, at_open = flat[-1]["c"], "end_last_bar", False
        xt, xd = flat[-1]["t"], flat[-1]["d"]
    ed = flat[i0]["d"]
    last_close: dict[str, float] = {}
    for b in flat:
        last_close[b["d"]] = b["c"]
    marks = [[d, c] for d, c in last_close.items() if ed <= d < xd]
    return dict(status="trig", side=side, entry=entry, exit=x, reason=reason, et=flat[i0]["t"], xt=xt, xd=xd,
                ed=ed, at_open=at_open, marks=marks)


def arm_breakout(flat: list[dict], n_entry: int, el: float, es: float, sides: str, last_d: str,
                 fallback: tuple[str, float] | None) -> dict:
    trig = find_trigger(flat[:n_entry], el, es, sides)
    if trig is None:
        return dict(status="none")
    if trig["status"] == "void":
        return dict(status="void")
    side, f = trig["side"], trig["fill"]
    o = outcome(flat, last_d, trig["idx"], f, side, es if side > 0 else el, fallback)
    o["gap_entry"] = trig["gap"]
    if sides == "both":                        # C5: opposite side at the same bar and fill, stop one range away
        opp = outcome(flat, last_d, trig["idx"], f, -side, f + side * (el - es), fallback)
        o["opp"] = dict(side=opp["side"], entry=opp["entry"], exit=opp["exit"], reason=opp["reason"])
    return o


def arm_open(flat: list[dict], n_entry: int, es: float, use_stop: bool, last_d: str,
             fallback: tuple[str, float] | None) -> dict:
    """C1 / C1n: buy at the first real bar open of the hold (day 2 09:15), same stop (C1) or none (C1n)."""
    i0 = next((i for i in range(n_entry) if not flat[i]["f"]), None)
    if i0 is None:
        return dict(status="none")
    e = flat[i0]["o"]
    if use_stop and e <= es:
        return dict(status="skip_below_sl")
    o = outcome(flat, last_d, i0, e, 1, es if use_stop else None, fallback)
    o["gap_entry"] = False
    return o


def size(el: float, es: float) -> tuple[int, bool]:
    r = el - es
    if r <= 0 or el <= 0:
        return 0, False
    by_risk, by_slot = math.floor(RISK / r), math.floor(SLOT / el)
    return int(min(by_risk, by_slot)), by_slot < by_risk


# ---------------------------------------------------------------- costs (per share)
def cost_deliv(entry: float, exit_px: float, qty: int, slip: float) -> float:
    """Long cash delivery round trip: STT both legs, stamp on buy, fees+GST, DP per sell spread over qty, slip."""
    s = entry + exit_px
    return (STT_DELIV * s + STAMP_BUY * entry + (TXN + SEBI + BROK_DELIV) * s * (1 + GST) + DP_SELL / qty
            + slip * s)


def cost_fut(side: int, entry: float, exit_px: float, slip: float) -> float:
    sell_px = exit_px if side > 0 else entry
    buy_px = entry if side > 0 else exit_px
    s = entry + exit_px
    return STT_FUT * sell_px + STAMP_FUT * buy_px + (TXN_FUT + SEBI + BROK_FUT) * s * (1 + GST) + slip * s


def r_net(rec: dict, o: dict, slip: float, model: str = "deliv") -> float:
    cps = cost_deliv(o["entry"], o["exit"], rec["qty"], slip) if model == "deliv" else \
        cost_fut(o["side"], o["entry"], o["exit"], slip)
    return (o["side"] * (o["exit"] - o["entry"]) - cps) / (rec["el"] - rec["es"])


# ---------------------------------------------------------------- per stock-period record
def ca_in_hold(sess: list[str], by_date: dict[str, list[dict]]) -> bool:
    prev_c = None
    for d in sess:
        bars = by_date.get(d)
        if not bars:
            continue
        if prev_c and abs(bars[0]["o"] / prev_c - 1) > CA_JUMP:
            return True
        prev_c = bars[-1]["c"]
    return False


def build_record(sym: str, k: str, ss: list[str], n: int, variant: str, daily: list[dict], didx: dict[str, int],
                 atr: list, by_date: dict[str, list[dict]], full: dict[str, list[dict]], next_open, late: bool,
                 first5: str = "") -> tuple[dict | None, str]:
    sig, hold_ss = ss[:n], ss[n:]
    if len(hold_ss) < 2:
        return None, "short_period"
    if sig[0] < first5:
        return None, "not_listed"                # before the stock's first 5-min bar (check 4: late listing)
    if any(d not in full for d in sig):
        return None, "missing_signal_day"
    if any(d not in didx for d in sig):
        return None, "missing_daily"
    j1, jn = didx[sig[0]], didx[sig[-1]]
    if j1 < HIST_MIN or atr[jn] is None:
        return None, "history"
    sma = sum(daily[i]["v"] for i in range(j1 - 20, j1)) / 20
    if sma <= 0:
        return None, "history"
    for i in range(jn - 19, jn + 1):
        if abs(daily[i]["o"] / daily[i - 1]["c"] - 1) > CA_JUMP:
            return None, "ca_guard"
    close = daily[jn]["c"]
    if close <= MIN_PX:
        return None, "min_px"
    sb = [b for d in sig for b in full[d]]
    hi, lo = max(b["h"] for b in sb), min(b["l"] for b in sb)
    if (hi - lo) / close < MIN_RNG:
        return None, "min_rng"
    buf = buffer(close)
    el, es = hi + buf, lo - buf
    qty, capped = size(el, es)
    entry_bars: list[dict] = []
    for d in hold_ss[:-1]:
        entry_bars += session_bars(by_date.get(d, []), d)
    flat = entry_bars + session_bars(by_date.get(hold_ss[-1], []), hold_ss[-1], last=True)
    # RVOL sums the signal sessions' own daily bars (not the index span j1..jn, which would also count any daily
    # bar of a non-calendar date lying between them)
    rec = dict(sym=sym, k=k, sig0=sig[0], rvol=sum(daily[didx[d]]["v"] for d in sig) / (n * sma),
               atr_pct=atr[jn] / close * 100, close=close, el=el, es=es, qty=qty, capped=capped, late=late,
               flag=ca_in_hold([sig[-1]] + hold_ss, by_date), n_hold=len(hold_ss), d2=hold_ss[0],
               miss_hold=sum(1 for d in hold_ss if not by_date.get(d)),
               part_hold=sum(1 for d in hold_ss if by_date.get(d) and d not in full))
    if not flat:
        rec["P"] = dict(status="none")
        if variant == "P":
            rec["S1"] = rec["C1"] = rec["C1n"] = dict(status="none")
        return rec, "ok"
    fb = next_open(hold_ss[-1])
    last_d = hold_ss[-1]
    n_entry = len(entry_bars)
    rec["P"] = arm_breakout(flat, n_entry, el, es, "long", last_d, fb)
    if variant == "P":
        rec["S1"] = arm_breakout(flat, n_entry, el, es, "both", last_d, fb)
        rec["C1"] = arm_open(flat, n_entry, es, True, last_d, fb)
        rec["C1n"] = arm_open(flat, n_entry, es, False, last_d, fb)
    return rec, "ok"


def is_special_daily(d: str, bars: list[dict] | None, calset: set, cal_lo: str, cal_hi: str) -> bool:
    """A daily bar of a special session (Muhurat, Saturday DR test): outside the calendar AND (a weekend OR the
    stock's 5-min bars that day do not start at 09:15). Weekday halt/outage days (09:15 start, < 72 bars) are real
    sessions and are kept; dates outside the 5-min era (before cal_lo / after cal_hi) are kept."""
    if d in calset or d < cal_lo or d > cal_hi:
        return False
    weekend = date(int(d[:4]), int(d[5:7]), int(d[8:10])).weekday() >= 5
    return weekend or bool(bars) and min(b["t"] for b in bars)[11:] != "09:15"


def add_halt_sessions(full: dict[str, list[dict]], by_date: dict[str, list[dict]], halt_days: set) -> int:
    """F5: on a MARKET-WIDE halt day (a calendar session with < CAL_MIN_STOCKS full stocks) the stock's real bars
    count as its session if they start 09:15. A stock's own partial day on a normal session stays partial (§5: a
    partial day 1 is skipped; it is counted in part_hold). Returns the number of days added."""
    n = 0
    for d in halt_days:
        v = sorted(by_date.get(d, []), key=lambda b: b["t"])
        if d not in full and v and v[0]["t"][11:] == "09:15":
            full[d] = v
            n += 1
    return n


def stock_records(sym: str, cal: list[str], cal_periods: dict[str, list], late: bool,
                  halt_days: set = frozenset()) -> dict:
    daily, m5 = F.load_stock(sym)
    m5 = [b for b in m5 if b["t"][:10] <= F.LAST_DAY]
    by_date: dict[str, list[dict]] = defaultdict(list)
    for b in m5:
        by_date[b["t"][:10]].append(b)
    full = Y.full_sessions(m5)
    calset = set(cal)
    add_halt_sessions(full, by_date, halt_days & calset)
    n_daily = len(daily)
    daily = [b for b in daily if not is_special_daily(b["d"], by_date.get(b["d"]), calset, cal[0], cal[-1])]
    special_daily = n_daily - len(daily)
    didx = {b["d"]: i for i, b in enumerate(daily)}
    atr = N.wilder_atr(daily)
    opens = sorted(d for d, v in by_date.items() if d in calset and v[0]["t"][11:] == "09:15")
    first5 = min(by_date) if by_date else "9999"

    def next_open(after: str) -> tuple[str, float] | None:
        i = bisect.bisect_right(opens, after)
        if i >= len(opens):
            return None
        b = by_date[opens[i]][0]
        return b["t"], b["o"]

    out: dict[str, list] = {v: [] for v in VARIANTS}
    skips: dict[str, Counter] = {v: Counter() for v in VARIANTS}
    for v, (kind, n) in VARIANTS.items():
        for k, ss in cal_periods[kind]:
            rec, why = build_record(sym, k, ss, n, v, daily, didx, atr, by_date, full, next_open, late, first5)
            skips[v][why] += 1
            if rec:
                out[v].append(rec)
    return dict(format=FORMAT, recs=out, skips={v: dict(c) for v, c in skips.items()}, special_daily=special_daily)


# ---------------------------------------------------------------- passes
def prep() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    uni = json.loads(Y.UNIVERSE.read_text())
    counts: Counter = Counter()
    open915: Counter = Counter()               # stocks whose first bar that date is 09:15 (F5 halt-day sessions)
    seen: Counter = Counter()                  # stocks with ANY 5-min bar that date (check 2: specials, halts)
    gsum: dict[str, float] = defaultdict(float)   # gap leg: open / previous close - 1
    bsum: dict[str, float] = defaultdict(float)   # bar leg: close / open - 1
    rcnt: Counter = Counter()
    last_m5, first_daily, chk1 = {}, {}, dict(days=0, ok=0, misses=[])
    for s in uni:
        daily, m5 = F.load_stock(s)
        m5 = [b for b in m5 if b["t"][:10] <= F.LAST_DAY]
        full = Y.full_sessions(m5)
        counts.update(full.keys())
        seen.update({b["t"][:10] for b in m5})
        first_t: dict[str, str] = {}
        for b in m5:
            first_t[b["t"][:10]] = min(first_t.get(b["t"][:10], "99"), b["t"][11:])
        odays = {d for d, t in first_t.items() if t == "09:15"}
        open915.update(odays)
        last_m5[s] = m5[-1]["t"][:10] if m5 else None
        first_daily[s] = daily[0]["d"] if daily else None
        dmap = {b["d"]: b for b in daily}
        for d, bars in full.items():
            db = dmap.get(d)
            if not db:
                continue
            chk1["days"] += 1
            h5, l5 = max(b["h"] for b in bars), min(b["l"] for b in bars)
            if abs(h5 / db["h"] - 1) <= 0.005 and abs(l5 / db["l"] - 1) <= 0.005:
                chk1["ok"] += 1
            elif len(chk1["misses"]) < 30:
                chk1["misses"].append([s, d, h5, db["h"], l5, db["l"]])
        prev = None
        for b in m5:
            if (b["t"][:10] in full or b["t"][:10] in odays) and prev and b["o"] > 0:
                if abs(b["c"] / prev - 1) <= UNIV_CLIP:
                    gsum[b["t"]] += b["o"] / prev - 1
                    bsum[b["t"]] += b["c"] / b["o"] - 1
                    rcnt[b["t"]] += 1
            prev = b["c"]
        print("prep", s, flush=True)
    cal = market_sessions(counts, open_counts=open915)
    calset = set(cal)
    ts, olevels, levels = index_levels(gsum, bsum, rcnt, calset)
    specials = sorted(d for d, n in counts.items() if 0 < n < CAL_MIN_STOCKS)
    off_cal = sorted([d, n, counts.get(d, 0)] for d, n in seen.items() if d not in calset and n >= CAL_MIN_STOCKS)
    halt_days = [d for d in cal if counts.get(d, 0) < CAL_MIN_STOCKS]
    print("prep: halt-day sessions (F5):", halt_days, flush=True)
    PREP.write_text(json.dumps(dict(format=FORMAT, cal=cal, specials=specials, off_cal=off_cal, ts=ts,
                                    halt_days=halt_days,
                                    olevels=olevels, levels=levels, last_m5=last_m5, first_daily=first_daily,
                                    chk1=chk1)))
    print("prep done:", len(cal), "sessions", flush=True)


def index_levels(gsum: dict[str, float], bsum: dict[str, float], rcnt: Counter, calset: set
                 ) -> tuple[list[str], list[float], list[float]]:
    """Equal-weight universe index with an OPEN and a CLOSE level per 5-min timestamp: open = previous close x
    (1 + mean gap leg), close = open x (1 + mean bar leg). A 09:15 open therefore carries the overnight gap and the
    09:15 close does not double-count it."""
    lvl, ts, olevels, levels = 1.0, [], [], []
    for t in sorted(rcnt):
        if t[:10] in calset and rcnt[t]:
            lvl *= 1 + gsum[t] / rcnt[t]
            olevels.append(lvl)
            lvl *= 1 + bsum[t] / rcnt[t]
            levels.append(lvl)
            ts.append(t)
    return ts, olevels, levels


def load_prep() -> dict:
    p = json.loads(PREP.read_text())
    if p.get("format") != FORMAT:
        raise SystemExit(f"{PREP} has format {p.get('format')}, code expects {FORMAT}: delete htf/ and rerun prep")
    return p


def data_guard(p: dict) -> list[str]:
    """Pre-run checks (0) and (1) as hard failures: every stock has 5-min bars through LAST_DAY and the calendar
    ends there; 5-min H/L agree with the daily bar on >= 99% of stock-days."""
    bad = []
    cal = p["cal"]
    if not cal or cal[-1] != F.LAST_DAY:
        bad.append(f"(0) calendar ends {cal[-1] if cal else None}, LAST_DAY is {F.LAST_DAY}")
    short = sorted(s for s, d in p["last_m5"].items() if d is None or d < F.LAST_DAY)
    if short:
        bad.append(f"(0) {len(short)} stocks without 5-min bars through {F.LAST_DAY}: {short[:20]}")
    c1 = p["chk1"]
    if not c1["days"] or 1 - c1["ok"] / c1["days"] > CHK1_MAX_MISS:
        bad.append(f"(1) 5-min vs daily H/L miss rate above {CHK1_MAX_MISS:.0%}: {c1['ok']}/{c1['days']}")
    return bad


def cal_periods_of(cal: list[str]) -> dict[str, list]:
    return {"week": periods(cal, "week"), "month": periods(cal, "month")}


def rows() -> None:
    p = load_prep()
    cal = p["cal"]
    cp = cal_periods_of(cal)
    ROWS_DIR.mkdir(parents=True, exist_ok=True)
    for s in json.loads(Y.UNIVERSE.read_text()):
        f = ROWS_DIR / f"{F.fname(s)}.json"
        if f.exists():
            continue
        late = (p["first_daily"][s] or "9999") > LATE_AFTER
        x = stock_records(s, cal, cp, late, set(p["halt_days"]))
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(x))
        tmp.replace(f)
        print("rows", s, flush=True)
    print("rows done", flush=True)


def load_rows() -> tuple[dict[str, list], dict[str, Counter]]:
    recs: dict[str, list] = defaultdict(list)
    skips: dict[str, Counter] = defaultdict(Counter)
    for s in json.loads(Y.UNIVERSE.read_text()):
        x = json.loads((ROWS_DIR / f"{F.fname(s)}.json").read_text())    # missing file = crash, never a silent skip
        if x.get("format") != FORMAT:
            raise SystemExit(f"rows/{F.fname(s)}.json has format {x.get('format')}, code expects {FORMAT}: "
                             "delete htf/rows and rerun rows")
        skips["special_daily"]["bars"] += x["special_daily"]
        for v, lst in x["recs"].items():
            recs[v].extend(lst)
        for v, c in x["skips"].items():
            skips[v].update(c)
    return recs, skips


# ---------------------------------------------------------------- selection, stats
def pick_eligible(r: dict) -> bool:
    return r["rvol"] >= RVOL_MIN and r["atr_pct"] > ATR_PCT


def select_picks(recs: list[dict]) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        if pick_eligible(r):
            by[r["k"]].append(r)
    return {k: sorted(v, key=lambda r: (-r["rvol"], r["sym"]))[:TOP_N] for k, v in by.items()}


def traded(r: dict, arm: str) -> bool:
    o = r.get(arm)
    return bool(o) and o["status"] == "trig" and r["qty"] > 0


def block_ci(pairs: list[tuple[str, float]], n: int = BOOT_N, seed: int = SEED, level: float = 0.95
             ) -> tuple[float, float]:
    by: dict[str, list[float]] = defaultdict(list)
    for k, v in pairs:
        by[k].append(v)
    blocks = list(by.values())
    if not blocks:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    ms = []
    for _ in range(n):
        pick = [blocks[rng.randrange(len(blocks))] for _ in blocks]
        ms.append(sum(sum(v) for v in pick) / sum(len(v) for v in pick))
    ms.sort()
    q = (1 - level) / 2
    return ms[int(q * n)], ms[int((1 - q) * n) - 1]


def is_(r: dict) -> bool:
    return r["sig0"] <= IS_END


def cal_at_or_after(cal: list[str], d: str) -> str:
    i = bisect.bisect_left(cal, d)
    return cal[min(i, len(cal) - 1)]


def mtm_daily(trades: list[tuple[dict, dict]], slip: float, cal: list[str]) -> dict[str, float]:
    """Daily mark-to-market P&L: open positions marked at each session's last close; all costs on the exit day."""
    pnl: dict[str, float] = defaultdict(float)
    for r, o in trades:
        q, prev = r["qty"], o["entry"]
        for d, m in o["marks"]:
            pnl[d] += q * (m - prev)
            prev = m
        pnl[cal_at_or_after(cal, o["xd"])] += q * (o["exit"] - prev) - q * cost_deliv(o["entry"], o["exit"], q, slip)
    return pnl


def dd_vs_capital(series: list[tuple[str, float]]) -> dict:
    return F.drawdown(series)                   # peak-to-trough of equity from 10L, pct_cap = Rs DD / 10L


def index_at(uni: tuple[list[str], list[float], list[float]], t: str, at_open: bool) -> float | None:
    """Universe level at bar t: its OPEN (at_open) or CLOSE. A timestamp missing from the index falls back to the
    close of the last index bar before it (open) / at or before it (close); None if there is none."""
    ts, olv, lv = uni
    i = bisect.bisect_left(ts, t)
    if at_open:
        if i < len(ts) and ts[i] == t:
            return olv[i]
        return lv[i - 1] if i > 0 else None
    j = bisect.bisect_right(ts, t) - 1
    return lv[j] if j >= 0 else None


def universe_ret(uni: tuple[list[str], list[float], list[float]], et: str, xt: str, at_open: bool) -> float | None:
    """Universe leg of C4: OPEN of the entry bar -> CLOSE of the exit bar (OPEN for an exit at the open)."""
    a, b = index_at(uni, et, True), index_at(uni, xt, at_open)
    return None if a is None or b is None else b / a - 1


def c2_dist(picks: dict[str, list[dict]], pool: dict[str, list[dict]], values: dict[int, float],
            draws: int = RAND_DRAWS, seed: int = SEED, trace: list | None = None) -> list[float]:
    """C2: per draw, each week with P picks draws as many stock-weeks as P picked from that week's base-eligible
    pool (guards + MIN_PX + MIN_RNG, picks included); the drawn stock-weeks that trade give the draw's pooled mean R
    per trade (as P's mean). values: id(record) -> net R for records that trade. Sorted means."""
    rng = random.Random(seed)
    dist = []
    weeks = sorted(picks)
    for _ in range(draws):
        vals = []
        for k in weeks:
            got = rng.sample(pool[k], min(len(picks[k]), len(pool[k])))
            if trace is not None:
                trace.append((k, got))
            vals += [values[id(r)] for r in got if id(r) in values]
        dist.append(mean0(vals))
    return sorted(x for x in dist if x == x) + [x for x in dist if x != x]


def verdict(x: dict) -> str:
    if (x["all_mean"] <= 0 or x["oos_mean"] <= 0 or x["dd_pct"] >= 25 or x["d1_all"] <= 0
            or x["p_mean"] < x["c2_median"]):
        return "FAIL"
    a = x["all_ci"][0] > 0
    b = x["oos_mean"] > 0
    c = x["dd_pct"] < 25
    d = (x["d1_is"] > 0 and x["d1_oos"] > 0 and x["d1_ci"][0] > 0 and x["c2_pctile"] >= 95
         and x["p_is"] > x["c3_is"] and x["p_oos"] > x["c3_oos"] and x["c4_is"] > 0 and x["c4_oos"] > 0)
    e = x["n_all"] >= MIN_TRADES_ALL and x["n_oos"] >= MIN_TRADES_OOS
    return "TRADEABLE CANDIDATE" if (a and b and c and d and e) else "INCONCLUSIVE"


def mean0(xs: list[float]) -> float:
    return st.mean(xs) if xs else float("nan")


def evaluate(recs_p: list[dict], slip: float, cal: list[str], uni: tuple[list[str], list[float], list[float]],
             keep=lambda r: True, drop=lambda r: False) -> dict:
    """All P-verdict inputs for one slippage.
    keep: filters the UNIVERSE before selection (e.g. without late listings: a rank-11 name may move up).
    drop: removes stock-weeks AFTER selection (e.g. flagged in-hold gaps, criterion (e)): picks are never refilled,
          because the flag uses hold-period data; dropped stock-weeks also leave the C2 / C3 pools."""
    recs_p = [r for r in recs_p if keep(r)]
    picks = {k: [r for r in v if not drop(r)] for k, v in select_picks(recs_p).items()}
    picks = {k: v for k, v in picks.items() if v}
    recs_p = [r for r in recs_p if not drop(r)]
    pk = [r for v in picks.values() for r in v]
    trades = [(r, r["P"]) for r in pk if traded(r, "P")]
    R = [(r, r_net(r, o, slip)) for r, o in trades]
    out = dict(n_all=len(R), n_oos=sum(1 for r, _ in R if not is_(r)), n_picks=len(pk))
    out["all_mean"] = mean0([v for _, v in R])
    out["all_ci"] = block_ci([(r["k"], v) for r, v in R])
    out["p_mean"] = out["all_mean"]
    out["p_is"] = mean0([v for r, v in R if is_(r)])
    out["p_oos"] = out["oos_mean"] = mean0([v for r, v in R if not is_(r)])
    out["is_ci"] = block_ci([(r["k"], v) for r, v in R if is_(r)])
    out["oos_ci"] = block_ci([(r["k"], v) for r, v in R if not is_(r)])
    out["win"] = 100 * sum(v > 0 for _, v in R) / len(R) if R else float("nan")
    # MTM equity
    pnl = mtm_daily(trades, slip, cal)
    lo = min((r["sig0"] for r in pk), default=cal[0])
    span = [d for d in cal if d >= lo]
    daily = [(d, pnl.get(d, 0.0)) for d in span]
    out["dd"] = dd_vs_capital(daily)
    out["dd_pct"] = out["dd"]["pct_cap"]
    out["dd_is"] = dd_vs_capital([x for x in daily if x[0] <= IS_END])["pct_cap"]
    out["dd_oos"] = dd_vs_capital([x for x in daily if x[0] > IS_END])["pct_cap"]
    out["rs"] = sum(pnl.values())
    out["daily"] = daily
    # D1 per pick
    def rp(r, arm):
        return r_net(r, r[arm], slip) if traded(r, arm) else 0.0
    d1 = [(r["k"], rp(r, "P") - rp(r, "C1"), is_(r)) for r in pk]
    out["d1_all"] = mean0([v for _, v, _ in d1])
    out["d1_is"] = mean0([v for _, v, i in d1 if i])
    out["d1_oos"] = mean0([v for _, v, i in d1 if not i])
    out["d1_ci"] = block_ci([(k, v) for k, v, _ in d1])
    out["p_per_pick"] = mean0([rp(r, "P") for r in pk])
    out["c1_per_pick"] = mean0([rp(r, "C1") for r in pk])
    out["c1n_per_pick"] = mean0([rp(r, "C1n") for r in pk])
    c1t = [r_net(r, r["C1"], slip) for r in pk if traded(r, "C1")]
    out["c1_per_trade"] = mean0(c1t)
    # C3 universe and C2 random
    pool: dict[str, list[dict]] = defaultdict(list)
    for r in recs_p:
        pool[r["k"]].append(r)
    allR = {id(r): r_net(r, r["P"], slip) for r in recs_p if traded(r, "P")}
    out["c3_is"] = mean0([v for r in recs_p if id(r) in allR and is_(r) for v in [allR[id(r)]]])
    out["c3_oos"] = mean0([v for r in recs_p if id(r) in allR and not is_(r) for v in [allR[id(r)]]])
    dist = c2_dist(picks, pool, allR)
    out["c2_median"] = dist[len(dist) // 2]
    out["c2_p95"] = dist[int(0.95 * len(dist)) - 1]
    out["c2_pctile"] = 100 * sum(x < out["p_mean"] for x in dist) / len(dist)
    # C4 universe excess (a trade whose bars are outside the index is counted, never silently given 0)
    ex: list[tuple[dict, float]] = []
    out["c4_missing"] = 0
    for r, o in trades:
        u = universe_ret(uni, o["et"], o["xt"], o["at_open"])
        if u is None:
            out["c4_missing"] += 1
            continue
        ex.append((r, ((o["exit"] / o["entry"] - 1) - u) / ((r["el"] - r["es"]) / o["entry"])))
    out["c4_is"] = mean0([e for r, e in ex if is_(r)])
    out["c4_oos"] = mean0([e for r, e in ex if not is_(r)])
    out["trades"] = R
    out["trade_objs"] = trades
    out["picks"] = picks
    return out


# ---------------------------------------------------------------- checks (no results)
def checks() -> None:
    p = load_prep()
    cal = p["cal"]
    bad0 = {s: d for s, d in p["last_m5"].items() if d is None or d < cal[-1]}
    print(f"(0) stocks with 5-min bars short of the last session {cal[-1]}: {len(bad0)} {sorted(bad0.items())[:20]}")
    c1 = p["chk1"]
    miss = 1 - c1["ok"] / c1["days"]
    print(f"(1) 5-min H/L within 0.5% of the daily bar: {c1['ok']}/{c1['days']} ({miss * 100:.2f}% miss); "
          f"first misses: {c1['misses'][:8]}")
    sats = [d for d in cal if date(int(d[:4]), int(d[5:7]), int(d[8:10])).weekday() >= 5]
    print(f"(2) market sessions {len(cal)} ({cal[0]}..{cal[-1]}); weekend sessions {sats}; special dates "
          f"(1..{CAL_MIN_STOCKS - 1} stocks full) {len(p['specials'])}: {p['specials'][:15]}")
    print(f"(2b) dates with bars for >= {CAL_MIN_STOCKS} stocks but NOT market sessions [date, stocks with bars, "
          f"stocks full] (Muhurat / DR / halt / outage; a weekday here is a real session the calendar drops): "
          f"{p['off_cal']}")
    for kind in ("week", "month"):
        by: dict[str, list[str]] = defaultdict(list)
        for d in cal:
            by[period_key(d, kind)].append(d)
        dist = Counter(len(v) for v in by.values())
        el = periods(cal, kind)
        print(f"(3) {kind}: periods by session count {sorted(dist.items())}; eligible {len(el)} "
              f"(IS {sum(1 for _, s in el if s[0] <= IS_END)}, OOS {sum(1 for _, s in el if s[0] > IS_END)})")
    recs, skips = load_rows()
    print(f"(4) special-session daily bars removed before SMA/ATR/CA: {skips['special_daily']['bars']}")
    for v in VARIANTS:
        print(f"(4) {v} stock-period outcomes: {dict(skips[v])}")
        picks = select_picks(recs[v])
        cnt = Counter(len(x) for x in picks.values())
        print(f"(5) {v} picks per period: {sorted(cnt.items())}; periods with no pick: "
              f"{len(periods(cal, VARIANTS[v][0])) - len(picks)}")
    picks = select_picks(recs["P"])
    pk = [r for x in picks.values() for r in x]
    print(f"(4b) P picks with hold sessions missing entirely: {sum(r['miss_hold'] > 0 for r in pk)}; partial: "
          f"{sum(r['part_hold'] > 0 for r in pk)}; C1 entries not at the day-2 09:15 bar: "
          f"{sum(1 for r in pk if r['C1'].get('et') and r['C1']['et'] != r['d2'] + ' 09:15')}; "
          f"P exits by fallback (count only): "
          f"{dict(Counter(r['P']['reason'] for r in pk if r['P'].get('reason', '').startswith('end_')))}")
    rng = [(r["el"] - r["es"]) / r["close"] * 100 for r in pk]
    tr = [r for r in pk if traded(r, "P")]
    cost = [cost_deliv(r["P"]["entry"], r["P"]["exit"], r["qty"], SLIP) / r["P"]["entry"] * 100 for r in tr]
    if rng and cost:
        print(f"(6) P picks: median planned range {st.median(rng):.2f}% of price; median round-trip cost at "
              f"{SLIP * 100:.2f}%/side {st.median(cost):.3f}% -> cost ~ {st.median(cost) / st.median(rng):.3f}R "
              "per trade")
    else:
        print(f"(6) cannot compute: {len(rng)} P picks, {len(cost)} triggered")
    print("(7) no result line printed by these checks.")
    bad = data_guard(p)
    if bad:
        raise SystemExit("HARD FAIL (pre-run checks):\n" + "\n".join(bad))


# ---------------------------------------------------------------- report
def fmt_ci(ci: tuple[float, float]) -> str:
    return f"[{ci[0]:+.3f},{ci[1]:+.3f}]"


def arm_summary(name: str, R: list[tuple[dict, float]]) -> str:
    al = [v for _, v in R]
    i = [v for r, v in R if is_(r)]
    o = [v for r, v in R if not is_(r)]
    weeks = len({r["k"] for r, _ in R})
    return (f"{name}: trades {len(al)} periods {weeks} win {100 * sum(v > 0 for v in al) / max(len(al), 1):.0f}% "
            f"mean {mean0(al):+.3f}R {fmt_ci(block_ci([(r['k'], v) for r, v in R]))} | IS {mean0(i):+.3f} "
            f"({len(i)}) | OOS {mean0(o):+.3f} ({len(o)})")


def report() -> None:
    p = load_prep()
    bad = data_guard(p)
    if bad:
        raise SystemExit("report refused, pre-run checks fail:\n" + "\n".join(bad))
    cal, uni = p["cal"], (p["ts"], p["olevels"], p["levels"])
    recs, _ = load_rows()
    L: list[str] = []

    def out(s: str = "") -> None:
        print(s, flush=True)
        L.append(s)

    out("WEEKLY SCHOOL RUN (HIGHER_TF_DESIGN.md) — P = weekly range of day 1, cash delivery, LONG ONLY, top-10 by "
        "RVOL, 10 x Rs1L slots, risk Rs10k. SURVIVORSHIP-BIASED universe (today's F&O).")
    base = evaluate(recs["P"], SLIP, cal, uni)
    noflag = evaluate(recs["P"], SLIP, cal, uni, drop=lambda r: r["flag"])
    v_all, v_nf = verdict(base), verdict(noflag)
    final = v_all
    if v_all == "TRADEABLE CANDIDATE" and v_nf != v_all:
        final = "INCONCLUSIVE"                  # (e) verdict must survive removing flagged trades
    out(f"\nP @ slip {SLIP * 100:.2f}%: trades {base['n_all']} (OOS {base['n_oos']}), picks {base['n_picks']}, "
        f"win {base['win']:.0f}%, mean {base['all_mean']:+.3f}R {fmt_ci(base['all_ci'])}; IS {base['p_is']:+.3f} "
        f"{fmt_ci(base['is_ci'])}; OOS {base['p_oos']:+.3f} {fmt_ci(base['oos_ci'])}")
    dd = base["dd"]
    out(f"  MTM P&L Rs {base['rs']:+,.0f}; max DD Rs {dd['dd']:,.0f} = {dd['pct_cap']:.1f}% of 10L "
        f"({dd['peak']}..{dd['trough']}); IS DD {base['dd_is']:.1f}%, OOS DD {base['dd_oos']:.1f}%")
    out(f"  D1 (P - C1 per pick): all {base['d1_all']:+.3f} {fmt_ci(base['d1_ci'])}, IS {base['d1_is']:+.3f}, "
        f"OOS {base['d1_oos']:+.3f} | per pick: P {base['p_per_pick']:+.3f}, C1 {base['c1_per_pick']:+.3f}, "
        f"C1n {base['c1n_per_pick']:+.3f}; C1 per trade {base['c1_per_trade']:+.3f}")
    out(f"  C2 random stocks: median {base['c2_median']:+.3f}, 95th pct {base['c2_p95']:+.3f}; P at "
        f"{base['c2_pctile']:.1f}th pct | C3 universe IS {base['c3_is']:+.3f} OOS {base['c3_oos']:+.3f} | "
        f"C4 excess IS {base['c4_is']:+.3f} OOS {base['c4_oos']:+.3f} (trades outside the index: "
        f"{base['c4_missing']})")
    pk0 = [r for v in base["picks"].values() for r in v]
    for arm in ("C1", "C1n"):
        out("  " + arm_summary(f"{arm} per trade", [(r, r_net(r, r[arm], SLIP)) for r in pk0 if traded(r, arm)]))
    out("  " + arm_summary("C3 universe per trade", [(r, r_net(r, r["P"], SLIP)) for r in recs["P"]
                                                     if traded(r, "P")]))
    out(f"  skipped C1 picks (day-2 open <= SL): {sum(r['C1']['status'] == 'skip_below_sl' for r in pk0)}; "
        f"picks with a missing / partial hold session: {sum(r['miss_hold'] > 0 for r in pk0)} / "
        f"{sum(r['part_hold'] > 0 for r in pk0)}")
    out(f"  verdict all trades: {v_all}; without flagged (in-hold >20% gap) trades: {v_nf} "
        f"(n {noflag['n_all']}, mean {noflag['all_mean']:+.3f})")
    out(f"VERDICT (frozen read §8): {final}")

    tr = base["trade_objs"]
    reasons = Counter(o["reason"] for _, o in tr)
    gaps_e = [v for (r, o), (_, v) in zip(tr, base["trades"]) if o["gap_entry"]]
    risk_rs = [r["qty"] * (r["el"] - r["es"]) for r, _ in tr]
    notion = [r["qty"] * o["entry"] for r, o in tr]
    held = sum(r["qty"] * o["entry"] * (bisect.bisect_right(cal, o["xd"]) - bisect.bisect_left(cal, o["ed"]))
               for r, o in tr)
    avail = CAPITAL * sum(r_n for r_n in [len(s) - 1 for _, s in periods(cal, "week") if s[0] >= min(r["sig0"] for r, _ in tr)])
    yrs = len(base["daily"]) / 248
    out(f"\n  exits {dict(reasons)}; gap entries {len(gaps_e)} (mean {mean0(gaps_e):+.3f}R); worst trade "
        f"{min(v for _, v in base['trades']):+.2f}R; capped by slot {100 * sum(r['capped'] for r, _ in tr) / len(tr):.0f}%")
    out(f"  mean actual risk Rs {mean0(risk_rs):,.0f}; max notional Rs {max(notion):,.0f} "
        f"(> slot: {sum(x > SLOT for x in notion)}); slot utilisation {100 * held / avail:.1f}%; "
        f"Rs/yr {base['rs'] / yrs:+,.0f} = {base['rs'] / yrs / CAPITAL * 100:+.1f}% of 10L")
    nl = evaluate(recs["P"], SLIP, cal, uni, keep=lambda r: not r["late"])
    out(f"  P without the stocks listed after {LATE_AFTER}: trades {nl['n_all']}, mean {nl['all_mean']:+.3f}R "
        f"IS {nl['p_is']:+.3f} OOS {nl['p_oos']:+.3f}")
    pc = evaluate(recs["P"], SLIP, cal, uni, keep=lambda r: r["sig0"] < CAS_START)
    out(f"  [NOT pre-registered, not in the verdict] P without the post-closing-auction weeks (day 1 >= {CAS_START}; the auction close lies outside the "
        f"5-min range on ~12% of those stock-days): trades {pc['n_all']}, OOS {pc['p_oos']:+.3f} ({pc['n_oos']})")

    out("\n  by year (signal day): year | trades | mean R | Rs (realised by exit)")
    by_y: dict[str, list] = defaultdict(list)
    for (r, o), (_, v) in zip(tr, base["trades"]):
        by_y[r["sig0"][:4]].append((v, r["qty"] * (o["exit"] - o["entry"] - cost_deliv(o["entry"], o["exit"],
                                                                                         r["qty"], SLIP))))
    for y in sorted(by_y):
        xs = by_y[y]
        out(f"  {y} | {len(xs)} | {mean0([a for a, _ in xs]):+.3f} | {sum(b for _, b in xs):+,.0f}")
    out("\n  monthly MTM Rs:")
    mon: dict[str, float] = defaultdict(float)
    for d, x in base["daily"]:
        mon[d[:7]] += x
    for y in sorted({m[:4] for m in mon}):
        out("  " + y + " " + " ".join(f"{mon.get(f'{y}-{m:02d}', 0.0) / 1000:+7.0f}k" for m in range(1, 13)))

    out("\n  slippage sensitivity (P): slip | trades | mean R [CI] | IS | OOS | MTM DD % | Rs")
    for s in SLIPS:
        x = base if s == SLIP else evaluate(recs["P"], s, cal, uni)
        out(f"  {s * 100:.2f}% | {x['n_all']} | {x['all_mean']:+.3f} {fmt_ci(x['all_ci'])} | {x['p_is']:+.3f} | "
            f"{x['p_oos']:+.3f} | {x['dd_pct']:.1f}% | {x['rs']:+,.0f}")

    # S1 both sides, futures costs, R only
    picks1 = select_picks(recs["P"])
    pk1 = [r for v in picks1.values() for r in v]
    s1 = [(r, r_net(r, r["S1"], SLIP, "fut")) for r in pk1 if traded(r, "S1")]
    out("\n" + arm_summary("S1 both sides, futures costs (R only)", s1))
    for side, nm in ((1, "long"), (-1, "short")):
        out("  " + arm_summary(nm, [(r, v) for r, v in s1 if r["S1"]["side"] == side]))
    rng = random.Random(SEED)
    opp = [(r_net(r, r["S1"], SLIP, "fut"), r_net(r, dict(r["S1"]["opp"]), SLIP, "fut")) for r, _ in s1]
    dist = sorted(mean0([a if rng.random() < 0.5 else b for a, b in opp]) for _ in range(RAND_DRAWS))
    s1m = mean0([v for _, v in s1])
    out(f"  C5 random side: median {dist[len(dist) // 2]:+.3f}, 95th {dist[int(.95 * len(dist)) - 1]:+.3f}; S1 at "
        f"{100 * sum(x < s1m for x in dist) / len(dist):.1f}th pct")
    s1d = [(r, r_net(r, r["S1"], SLIP, "deliv")) for r, _ in s1 if r["S1"]["side"] > 0]
    out("  " + arm_summary("S1 longs at delivery cost (check: must equal P exactly)", s1d))

    # S2, S3
    for v in ("S2", "S3"):
        x = evaluate(recs[v], SLIP, cal, uni)
        out(f"\n{v}: trades {x['n_all']} (OOS {x['n_oos']}), mean {x['all_mean']:+.3f}R {fmt_ci(x['all_ci'])}; "
            f"IS {x['p_is']:+.3f}; OOS {x['p_oos']:+.3f} {fmt_ci(x['oos_ci'])}; MTM DD {x['dd_pct']:.1f}%; "
            f"Rs {x['rs']:+,.0f} (no C1 arm for S2: D1 and controls not computed)" if v == "S2" else
            f"\n{v} (monthly, report only): trades {x['n_all']}, mean {x['all_mean']:+.3f}R {fmt_ci(x['all_ci'])}; "
            f"IS {x['p_is']:+.3f}; OOS {x['p_oos']:+.3f}; MTM DD {x['dd_pct']:.1f}%; Rs {x['rs']:+,.0f}")
        if v == "S2":
            oos98 = block_ci([(r["k"], q) for r, q in x["trades"] if not is_(r)], level=1 - 0.05 / 3)
            cand = (x["p_is"] > base["p_is"] and oos98[0] > 0 and x["dd_pct"] < 25
                    and x["n_all"] >= MIN_TRADES_ALL and x["n_oos"] >= MIN_TRADES_OOS)
            out(f"  S2 rule: IS {x['p_is']:+.3f} vs P {base['p_is']:+.3f}; OOS 98.3% CI {fmt_ci(oos98)} -> "
                f"{'candidate pending its own controls' if cand else 'not a candidate'}")
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "htf_report.txt").write_text("\n".join(L), encoding="utf-8")


def main(argv: list[str]) -> None:
    cmds = {"prep": prep, "rows": rows, "checks": checks, "report": report}
    if hasattr(sys.stdout, "reconfigure"):     # Windows cp1252 console cannot print "—", "§", "≈"
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(argv) != 1 or argv[0] not in cmds:
        raise SystemExit("usage: htf_orb.py prep|rows|checks|report")
    cmds[argv[0]]()


if __name__ == "__main__":
    main(_argv)
