"""ASRS on the Chartink 'stocks in play' scan, 2020-01..2026-11 (FULL_ASRS.md, frozen 2026-11-20).

    python -I full_asrs.py fetch     # 2020-2025 5-min + daily since 2019-10 per stock (gz, compact) -> scratchpad
    python -I full_asrs.py rows      # per stock: per-session pick/score/ASRS trade -> rows cache
    python -I full_asrs.py report    # capital 10L, risk 10k, top-10/day, monthly table, drawdown

2026 5-min bars come from the y2026 cache (already fetched and reviewed).
"""
from __future__ import annotations

import gzip
import json
import math
import os
import random
import statistics as st
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import nov_stocks as N  # noqa: E402
import stocks_asrs_check as S  # noqa: E402
import y2026_asrs as Y  # noqa: E402

# Durable copy (11-24) of the old session scratchpad's srs_week (htf calendar, rows, raw caches); its parent also holds
# fo_bhav/ and nse_symchg/.  SRS_BASE overrides.  The old Temp copy can be wiped by Windows: never point back at it.
BASE = Path(os.environ.get("SRS_BASE", r"C:/Users/mail2/Documents/Projects/NSE_DATA/srs_base/srs_week"))
RAW = BASE / "full_raw"
SIG_N = 3 if "--srs" in sys.argv else 1          # 1 = ASRS (09:30 bar), 3 = SRS (09:30-09:45)
SETUP = "SRS" if SIG_N == 3 else "ASRS"
ROWS = BASE / ("full_rows_srs" if SIG_N == 3 else "full_rows")
OUT = HERE / "results"
D_FROM, M_FROM, M_TO = date(2019, 10, 1), date(2020, 1, 1), date(2025, 12, 31)
LAST_DAY = "2026-11-19"
CAPITAL, RISK, SLOTS, LEVERAGE = 1_000_000.0, 10_000.0, 10, 5.0
NOTIONAL_CAP = CAPITAL * LEVERAGE / SLOTS          # Rs 5,00,000 per trade
TOP_N, SEEDS, IS_END = 10, 200, "2023-12-31"


def fname(s: str) -> str:
    return s.replace("&", "_")


# ---------------------------------------------------------------- fetch
def fetch() -> None:
    from kiteconnect import KiteConnect
    tok = json.loads((HERE.parent / "data/kite_access_token.json").read_text())
    k = KiteConnect(api_key=tok["api_key"])
    k.set_access_token(tok["access_token"])
    eq = {i["tradingsymbol"]: i["instrument_token"] for i in k.instruments("NSE")}
    RAW.mkdir(parents=True, exist_ok=True)

    def hist(token, a, b, iv):
        for attempt in range(4):
            try:
                return k.historical_data(token, a, b, iv)
            except Exception as e:                 # timeouts / transient API errors: retry, then fail loudly
                if attempt == 3:
                    raise
                print("retry", attempt + 1, type(e).__name__, flush=True)
                time.sleep(3 * (attempt + 1))
    for s in json.loads(Y.UNIVERSE.read_text()):
        f = RAW / f"{fname(s)}.json.gz"
        if f.exists():
            continue
        daily = []
        for a, b in ((D_FROM, date(2023, 6, 30)), (date(2023, 7, 1), date(2026, 11, 20))):
            daily += [[c["date"].strftime("%Y-%m-%d"), c["open"], c["high"], c["low"], c["close"], c["volume"]]
                      for c in hist(eq[s], a, b, "day")]
            time.sleep(0.35)
        m5, a = [], M_FROM
        while a <= M_TO:
            b = min(a + timedelta(days=95), M_TO)
            m5 += [[c["date"].strftime("%Y-%m-%d %H:%M"), c["open"], c["high"], c["low"], c["close"], c["volume"]]
                   for c in hist(eq[s], a, b, "5minute")]
            time.sleep(0.35)
            a = b + timedelta(days=1)
        with gzip.open(f, "wt") as g:
            json.dump(dict(daily=daily, m5=m5), g)
        print(s, len(daily), len(m5), flush=True)
    print("done")


def fetch_parallel(workers: int = 3, min_gap: float = 0.36, symbols: list[str] | None = None,
                   raw: Path | None = None, m5_to: date | None = None,
                   kite_alias: dict[str, str] | None = None) -> None:
    """Same data as fetch(), 3 stocks at a time under ONE shared limiter (<= ~2.8 requests/s, Kite allows 3/s).
    Files are written to *.tmp and renamed only when complete; damaged existing files are deleted and re-fetched.

    Defaults = the original 218-stock fetch into RAW with 5-min to M_TO.  The survivorship study passes its own
    symbol list, its own directory (tmp/damaged clean-up touches ONLY that directory), m5_to (2026 bars included)
    and kite_alias {stored symbol: Kite tradingsymbol} for series-suffixed names (HFCL -> HFCL-BE)."""
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from kiteconnect import KiteConnect
    raw = RAW if raw is None else raw
    m5_to = M_TO if m5_to is None else m5_to
    alias = kite_alias or {}
    tok = json.loads((HERE.parent / "data/kite_access_token.json").read_text())
    k = KiteConnect(api_key=tok["api_key"])
    k.set_access_token(tok["access_token"])
    eq = {i["tradingsymbol"]: i["instrument_token"] for i in k.instruments("NSE")}
    raw.mkdir(parents=True, exist_ok=True)
    for f in raw.glob("*.tmp"):
        f.unlink()
    bad = 0
    for f in raw.glob("*.json.gz"):
        try:
            with gzip.open(f, "rt") as g:
                x = json.load(g)
            assert x["daily"] and x["m5"]
        except Exception:
            f.unlink()
            bad += 1
    print("damaged files removed:", bad, flush=True)
    lock = threading.Lock()
    last = [0.0]

    def hist(token, a, b, iv):
        for attempt in range(5):
            with lock:
                wait = last[0] + min_gap - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                last[0] = time.monotonic()
            try:
                return k.historical_data(token, a, b, iv)
            except Exception as e:
                if attempt == 4:
                    raise
                print("retry", attempt + 1, type(e).__name__, flush=True)
                time.sleep(3 * (attempt + 1))

    def one(s: str) -> None:
        f = raw / f"{fname(s)}.json.gz"
        if f.exists():
            return
        token = eq[alias.get(s, s)]
        daily = []
        for a, b in ((D_FROM, date(2023, 6, 30)), (date(2023, 7, 1), date(2026, 11, 20))):
            daily += [[c["date"].strftime("%Y-%m-%d"), c["open"], c["high"], c["low"], c["close"], c["volume"]]
                      for c in hist(token, a, b, "day")]
        m5, a = [], M_FROM
        while a <= m5_to:
            b = min(a + timedelta(days=95), m5_to)
            m5 += [[c["date"].strftime("%Y-%m-%d %H:%M"), c["open"], c["high"], c["low"], c["close"], c["volume"]]
                   for c in hist(token, a, b, "5minute")]
            a = b + timedelta(days=1)
        tmp = f.with_suffix(".tmp")
        with gzip.open(tmp, "wt") as g:
            json.dump(dict(daily=daily, m5=m5), g)
        os.replace(tmp, f)
        print(s, len(daily), len(m5), flush=True)

    src = json.loads(Y.UNIVERSE.read_text()) if symbols is None else symbols
    todo = [s for s in src if not (raw / f"{fname(s)}.json.gz").exists()]
    print("to fetch:", len(todo), flush=True)
    with ThreadPoolExecutor(workers) as ex:
        for _ in ex.map(one, todo):
            pass
    print("done", len(list(raw.glob("*.json.gz"))), "files", flush=True)


# ---------------------------------------------------------------- fast scan (== nov_stocks.is_pick, tested)
def scan_series(daily: list[dict]) -> dict[str, tuple[float, float, float] | None]:
    """For each daily date D, the (sma20 volume, ATR, close) of the LAST bar before the NEXT date —
    i.e. returns a function of 'day d' via prev_stats(d).  Here: index -> stats at that bar."""
    atr = N.wilder_atr(daily)
    out = {}
    vols = [b["v"] for b in daily]
    run = 0.0
    for i, b in enumerate(daily):
        run += vols[i]
        if i >= 20:
            run -= vols[i - 20]
        out[b["d"]] = (run / 20, atr[i], b["c"]) if (i >= 19 and atr[i] is not None) else None
    return out


def fast_pick(daily_dates: list[str], stats: dict, d: str, f15: float | None) -> tuple[bool, float]:
    """(pick?, score) for day d using only the last daily bar dated < d."""
    import bisect
    j = bisect.bisect_left(daily_dates, d) - 1
    if f15 is None or j < 0:
        return False, 0.0
    x = stats[daily_dates[j]]
    if x is None:
        return False, 0.0
    sma, atr, c = x
    ok = f15 > N.VOL_MULT * sma and atr / c * 100 > N.ATR_PCT and c > N.MIN_PX
    return ok, (f15 / sma if ok else 0.0)


# ---------------------------------------------------------------- rows
def load_stock(s: str, raw: Path | None = None, y2026: bool = True) -> tuple[list[dict], list[dict]]:
    """raw: directory of the gz files (default RAW).  y2026=False skips the y2026 merge (the survivorship raw files
    already hold 2026 5-min bars)."""
    with gzip.open((RAW if raw is None else raw) / f"{fname(s)}.json.gz", "rt") as g:
        x = json.load(g)
    daily = [dict(d=r[0], o=r[1], h=r[2], l=r[3], c=r[4], v=r[5]) for r in x["daily"]]
    m5 = [dict(t=r[0], o=r[1], h=r[2], l=r[3], c=r[4], v=r[5]) for r in x["m5"]]
    y = Y.CACHE / f"{fname(s)}.json"
    if y2026 and y.exists():
        m5 += [b for b in json.loads(y.read_text())["m5"] if b["t"][:10] <= LAST_DAY]
    daily = sorted({b["d"]: b for b in daily}.values(), key=lambda b: b["d"])
    m5 = sorted({b["t"]: b for b in m5}.values(), key=lambda b: b["t"])
    return daily, m5


def fill_gaps(bars: list[dict]) -> tuple[list[dict], int]:
    """Kite omits 5-min bars with no trades.  Rebuild the 09:15.. grid up to the last bar, filling a missing bar
    with the previous close (o=h=l=c, v=0), so a mid-day gap does not end a trade early (review 11-20).
    Returns (bars, number of filled bars)."""
    have = {b["t"][11:]: b for b in bars}
    d = bars[0]["t"][:10]
    last = bars[-1]["t"][11:]
    out, filled, prev = [], 0, None
    for i in range(75):
        m = 9 * 60 + 15 + 5 * i
        hhmm = f"{m // 60:02d}:{m % 60:02d}"
        if hhmm > last:
            break
        b = have.get(hhmm)
        if b is None:
            if prev is None:
                return [], 0                       # no 09:15 bar: cannot rebuild
            c = prev["c"]
            b = dict(t=f"{d} {hhmm}", o=c, h=c, l=c, c=c, v=0)
            filled += 1
        out.append(b)
        prev = b
    return out, filled


GAP_STATS = {"sessions": 0, "gapped": 0, "filled_bars": 0}


def stock_rows(s: str, sig_n: int | None = None, raw: Path | None = None, y2026: bool = True) -> list[list]:
    sig_n = SIG_N if sig_n is None else sig_n
    daily, m5 = load_stock(s, raw, y2026)
    stats = scan_series(daily)
    dates = [b["d"] for b in daily]
    out = []
    for d, raw in Y.full_sessions(m5).items():
        if d > LAST_DAY:
            continue
        pick, score = fast_pick(dates, stats, d, N.first15_volume(raw))   # scan on REAL bars only
        bars, filled = fill_gaps(raw)
        GAP_STATS["sessions"] += 1
        GAP_STATS["gapped"] += filled > 0
        GAP_STATS["filled_bars"] += filled
        t = S.trade(S.completed(bars, None), d, sig_n) if bars else None
        if t and t["trig"]:
            cps = t["cost_r"] * t["risk"]
            out.append([d, pick, score, t["side"], t["entry"], t["stop"], t["exit"], t["risk"], t["r_net"], cps])
        else:
            out.append([d, pick, score, 0, 0, 0, 0, 0, 0, 0])
    return out


def rows() -> None:
    ROWS.mkdir(parents=True, exist_ok=True)
    for s in json.loads(Y.UNIVERSE.read_text()):
        f = ROWS / f"{fname(s)}.json"
        if f.exists() or not (RAW / f"{fname(s)}.json.gz").exists():
            continue
        f.write_text(json.dumps(stock_rows(s)))
        print("rows", s, flush=True)
    print("done; gap stats (this run):", GAP_STATS)


# ---------------------------------------------------------------- money
def size(entry: float, stop: float) -> int:
    risk_ps = abs(entry - stop)
    if risk_ps <= 0 or entry <= 0:
        return 0
    return int(min(math.floor(RISK / risk_ps), math.floor(NOTIONAL_CAP / entry)))


def trade_rs(side: int, entry: float, exit_px: float, cps: float, qty: int) -> float:
    return qty * (side * (exit_px - entry) - cps)


def drawdown(daily_pnl: list[tuple[str, float]]) -> dict:
    eq, peak, peak_d = CAPITAL, CAPITAL, (daily_pnl[0][0] if daily_pnl else None)
    worst = dict(dd=0.0, pct=0.0, pct_cap=0.0, peak=None, trough=None, days=0)
    start = None
    for d, p in daily_pnl:
        eq += p
        if eq >= peak:
            peak, peak_d = eq, d
        dd = peak - eq
        if dd > worst["dd"]:
            worst = dict(dd=dd, pct=dd / peak * 100, pct_cap=dd / CAPITAL * 100, peak=peak_d, trough=d)
    return worst


def longest_underwater(daily_pnl: list[tuple[str, float]]) -> tuple[int, str | None, str | None]:
    eq, peak, start, best = CAPITAL, CAPITAL, None, (0, None, None)
    n = 0
    for d, p in daily_pnl:
        eq += p
        if eq >= peak:
            peak, start, n = eq, None, 0
        else:
            start = start or d
            n += 1
            if n > best[0]:
                best = (n, start, d)
    return best


def report() -> None:
    uni = json.loads(Y.UNIVERSE.read_text())
    by_day: dict[str, list[tuple[str, list]]] = defaultdict(list)
    for s in uni:
        f = ROWS / f"{fname(s)}.json"
        if f.exists():
            for r in json.loads(f.read_text()):
                by_day[r[0]].append((s, r))
    days = sorted(by_day)
    trades = []          # (d, s, side, R, rs, capped)
    for d in days:
        picks = sorted([(s, r) for s, r in by_day[d] if r[1]], key=lambda x: -x[1][2])[:TOP_N]
        for s, r in picks:
            if r[3] == 0:
                continue
            q = size(r[4], r[5])
            if q <= 0:
                continue
            risk_ps = abs(r[4] - r[5])
            capped = math.floor(NOTIONAL_CAP / r[4]) < math.floor(RISK / risk_ps)    # notional cap bound
            trades.append((d, s, r[3], r[8], trade_rs(r[3], r[4], r[6], r[9], q), capped))
    pnl_day = defaultdict(float)
    for t in trades:
        pnl_day[t[0]] += t[4]
    daily = [(d, pnl_day.get(d, 0.0)) for d in days]
    L = [f"{SETUP} top-{TOP_N} 'stocks in play' picks, {days[0]}..{days[-1]} ({len(days)} sessions, {len(uni)} stocks, "
         f"SURVIVORSHIP-BIASED universe). Capital Rs10L, risk Rs10k/trade, notional cap Rs5L/trade, NEO cash costs."]

    def summary(label, sel_days):
        ts = [t for t in trades if t[0] in sel_days]
        if not ts:
            return f"{label}: no trades"
        dp = [(d, pnl_day.get(d, 0.0)) for d in sorted(sel_days)]
        dd = drawdown(dp)
        uw = longest_underwater(dp)
        years = len(sel_days) / 248
        tot = sum(t[4] for t in ts)
        months = defaultdict(float)
        for d, p in dp:
            months[d[:7]] += p
        mv = sorted(months.items(), key=lambda x: x[1])
        rs = [t[3] for t in ts]
        lo, hi = Y.boot_ci([(t[0], t[3]) for t in ts])
        return (f"{label}: {len(ts)} trades ({len(ts) / len(sel_days):.1f}/day), win {sum(t[4] > 0 for t in ts) / len(ts):.0%}, "
                f"avg {st.mean(rs):+.3f}R CI[{lo:+.3f},{hi:+.3f}] | P&L Rs {tot:+,.0f} = {tot / CAPITAL:+.0%} of 10L "
                f"(~{tot / CAPITAL / years:+.0%}/yr) | MAX DD Rs {dd['dd']:,.0f} = {dd['pct_cap']:.1f}% of 10L "
                f"({dd['pct']:.1f}% of peak) "
                f"{dd['peak']}->{dd['trough']} | longest underwater {uw[0]} sessions ({uw[1]}..{uw[2]}) | "
                f"months + {sum(v > 0 for v in months.values())}/{len(months)}, worst {mv[0][0]} Rs {mv[0][1]:+,.0f}, "
                f"best {mv[-1][0]} Rs {mv[-1][1]:+,.0f} | capped {sum(t[5] for t in ts) / len(ts):.0%}")

    L.append(summary("ALL      ", set(days)))
    L.append(summary("2020-2023", {d for d in days if d <= IS_END}))
    L.append(summary("2024-2026", {d for d in days if d > IS_END}))
    ts_all = [(t[0], t[3]) for t in trades]
    lo = Y.boot_ci(ts_all)[0]
    oos = [t[3] for t in trades if t[0] > IS_END]
    dd = drawdown(daily)
    ok = st.mean(t[3] for t in trades) > 0 and lo > 0 and st.mean(oos) > 0 and dd["pct_cap"] < 25
    L.append(f"READ (frozen): avg>0 & CI>0 {lo > 0}, 2024-26>0 {st.mean(oos) > 0}, maxDD<25% of capital "
             f"{dd['pct_cap'] < 25} -> "
             f"{'TRADEABLE CANDIDATE' if ok else 'NOT YET'}")
    # yearly with honesty check
    L.append("\nYEAR  trades  win   avgR    P&L Rs        % of 10L  maxDD%  | universe R/trade  random-10 R/trade (median)")
    rng = random.Random(7)
    for y in sorted({d[:4] for d in days}):
        yd = [d for d in days if d.startswith(y)]
        ts = [t for t in trades if t[0][:4] == y]
        uv = [r[8] for d in yd for _, r in by_day[d] if r[3] != 0]
        meds = []
        for _ in range(SEEDS):
            v = []
            for d in yd:
                for s, r in rng.sample(by_day[d], min(TOP_N, len(by_day[d]))):
                    if r[3] != 0:
                        v.append(r[8])
            meds.append(st.mean(v) if v else 0)
        meds.sort()
        tot = sum(t[4] for t in ts)
        ddy = drawdown([(d, pnl_day.get(d, 0.0)) for d in yd])
        L.append(f"{y}  {len(ts):6d}  {(sum(t[4] > 0 for t in ts) / len(ts) if ts else 0):4.0%}  "
                 f"{(st.mean(t[3] for t in ts) if ts else 0):+.3f}  {tot:+13,.0f}  {tot / CAPITAL:+7.1%}  "
                 f"{ddy['pct_cap']:5.1f}%  | {st.mean(uv):+.3f}           {meds[SEEDS // 2]:+.3f}")
    # monthly table
    L.append("\nMONTHLY P&L (Rs, % of 10L):")
    months = defaultdict(float)
    for d, p in daily:
        months[d[:7]] += p
    for y in sorted({m[:4] for m in months}):
        cells = []
        for mth in range(1, 13):
            key = f"{y}-{mth:02d}"
            cells.append(f"{months[key] / CAPITAL * 100:+5.1f}%" if key in months else "   -  ")
        L.append(f"{y}: " + " ".join(cells) + f" | year {sum(v for k, v in months.items() if k.startswith(y)) / CAPITAL * 100:+.1f}%")
    OUT.mkdir(exist_ok=True)
    (OUT / f"full_{SETUP.lower()}_report.txt").write_text("\n".join(L), encoding="utf-8")
    (OUT / f"full_{SETUP.lower()}_daily.json").write_text(json.dumps(daily))
    print("\n".join(L))


if __name__ == "__main__":
    {"fetch": fetch, "fetchp": fetch_parallel, "rows": rows, "report": report}[sys.argv[1]]()
