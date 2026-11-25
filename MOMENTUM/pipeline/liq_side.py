"""Liquidity floor (A) and long/short + WITH-MARKET (B) on the FULL_ASRS top-10 framework (LIQ_SIDE.md, frozen
2026-11-20, before any result).

    python -I liq_side.py build      # per-stock cache: turnover + one-sided long/short rows -> F.BASE/liq_side (counts only)
    python -I liq_side.py report     # NOT before the independent review (project protocol)
"""
from __future__ import annotations

import bisect
import gzip
import json
import math
import os
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_argv = sys.argv[1:]
sys.argv = sys.argv[:1]                      # full_asrs parses --srs at import; neutral (rows dirs are explicit here)
import full_asrs as F  # noqa: E402
import sim  # noqa: E402
import slip_test as T  # noqa: E402
import stocks_asrs_check as S  # noqa: E402
import y2026_asrs as Y  # noqa: E402

CACHE = F.BASE / "liq_side"
CACHE_VERSION = 1
NIFTY_5M = sim.DATA                                         # NIFTY 50 spot 5-min, 2020-01..2026-10-29
NIFTY_DAILY = sim.DATA.parent / "NIFTY.json"                # official daily closes, 2010..2026-10-29
NOV_SERIES = F.BASE / "nov_series"                          # real Kite NIFTY 1-min, 2026-09-01..2026-11-20
USE_NOV_TAIL = True                                         # declared fallback for 2026-10-30..11-19 (LIQ_SIDE.md)
OUT = HERE / "results" / "liq_side.txt"

# setup -> (signal 5-min bars, rows dir, NIFTY signal bar start whose CLOSE is the direction price)
SETUPS = {"SRS": (3, F.BASE / "full_rows_srs", "09:40"), "ASRS": (1, F.BASE / "full_rows", "09:30")}
CR = 1e7
TURN_N = 20
SMA_N = 20
FLOORS: dict[str, tuple[str, float] | None] = {"BASE": None, "PCT30": ("pct", 0.30), "PCT50": ("pct", 0.50),
                                               "ABS200": ("abs", 200 * CR)}
LIQ_CANDIDATES = ["BASE", "PCT30", "PCT50", "ABS200"]       # order = tie-break preference
TIER_HI, TIER_MID = 1000 * CR, 200 * CR
SLIP_HI, SLIP_MID, SLIP_LO = 0.0002, 0.0005, 0.0010
TIERED = "TIERED"
SLIP_MODELS: list[float | str] = [0.0002, 0.0005, 0.0010, TIERED]
READ_SLIP_B = 0.0002
INFO_SLIP_B = 0.0005
IS_END = F.IS_END
SideFn = Callable[[], bool]


# ---------------------------------------------------------------- small pure helpers
def sign(x: float) -> int:
    return (x > 0) - (x < 0)


def write_json_atomic(path: Path, obj) -> None:
    """Write to <name>.tmp, then os.replace: a crash never leaves a half-written cache file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


# ---------------------------------------------------------------- A: turnover, floors, slippage
def turnover_sma(daily: list[dict], d: str, n: int = TURN_N, dates: list[str] | None = None) -> float | None:
    """Mean of close*volume over the n daily bars dated STRICTLY before d (daily sorted by date). None if < n."""
    dates = dates if dates is not None else [b["d"] for b in daily]
    j = bisect.bisect_left(dates, d)
    if j < n:
        return None
    return sum(b["c"] * b["v"] for b in daily[j - n:j]) / n


def slip_for_turnover(t: float | None) -> float:
    """TIERED model (ASSUMPTION, LIQ_SIDE.md): >= 1000 Cr 0.02%, 200-1000 Cr 0.05%, < 200 Cr or unknown 0.10%."""
    if t is None:
        return SLIP_LO
    if t >= TIER_HI:
        return SLIP_HI
    if t >= TIER_MID:
        return SLIP_MID
    return SLIP_LO


def top_by_score(cands: list[tuple[str, list]], top_n: int = F.TOP_N) -> list[tuple[str, list]]:
    """Exactly the full_asrs / slip_test selection: stable sort by -score, first top_n."""
    return sorted(cands, key=lambda x: -x[1][2])[:top_n]


def select_floor(cands: list[tuple[str, list]], turn: dict[str, float | None],
                 floor: tuple[str, float] | None, top_n: int = F.TOP_N) -> list[tuple[str, list]]:
    """cands: that day's picks in load order. The floor is applied BEFORE the top-N cut (backfill from lower ranks).
    ("pct", q): drop the floor(q*n) lowest-turnover picks (None lowest, ties by symbol).
    ("abs", x): keep picks with turnover >= x.  KeyError on a pick with no turnover entry is intended."""
    if floor is None:
        return top_by_score(cands, top_n)
    kind, x = floor
    if kind == "pct":
        k = math.floor(x * len(cands) + 1e-9)
        order = sorted(cands, key=lambda c: (turn[c[0]] is not None, turn[c[0]] or 0.0, c[0]))
        drop = {c[0] for c in order[:k]}
        keep = [c for c in cands if c[0] not in drop]
    elif kind == "abs":
        keep = [c for c in cands if turn[c[0]] is not None and turn[c[0]] >= x]
    else:
        raise ValueError(kind)
    return top_by_score(keep, top_n)


def recost(r: list, slip: float) -> tuple[float, float]:
    """(r_net, cost_ps) of a row at another slippage per side; unchanged (bit-exact) at the rows' base slip."""
    if slip == T.BASE_SLIP:
        return r[8], r[9]
    x = T.extra_cost_ps(r[4], r[6], slip)
    return r[8] - x / r[7], r[9] + x


# ---------------------------------------------------------------- money / stats
TradeRow = tuple  # (d, s, side, r_net, rs)


def run_days(days: list[str], picks_of: Callable[[str], list[tuple[str, list, float]]]
             ) -> tuple[list[TradeRow], list[tuple[str, float]]]:
    """picks_of(d) -> [(symbol, row, slip)]: the rows actually ordered that day, in selection order.
    Same sizing / P&L / summation order as full_asrs.report."""
    trades: list[TradeRow] = []
    for d in days:
        for s, r, slip in picks_of(d):
            if r[3] == 0:
                continue
            q = F.size(r[4], r[5])
            if q <= 0:
                continue
            rn, cps = recost(r, slip)
            trades.append((d, s, r[3], rn, F.trade_rs(r[3], r[4], r[6], cps, q)))
    pnl: dict[str, float] = defaultdict(float)
    for t in trades:
        pnl[t[0]] += t[4]
    return trades, [(d, pnl.get(d, 0.0)) for d in days]


def stats(trades: list[TradeRow], daily: list[tuple[str, float]], sel: Callable[[str], bool], ci: bool = True) -> dict:
    ts = [t for t in trades if sel(t[0])]
    dp = [(d, p) for d, p in daily if sel(d)]
    if not ts:
        return dict(n=0, days=len(dp))
    dd = F.drawdown(dp)
    rs = sum(t[4] for t in ts)
    ratio = rs / dd["dd"] if dd["dd"] > 0 else (math.inf if rs > 0 else 0.0)
    return dict(n=len(ts), days=len(dp), r=st.mean(t[3] for t in ts),
                ci=Y.boot_ci([(t[0], t[3]) for t in ts]) if ci else (math.nan, math.nan),
                rs=rs, dd=dd["dd"], dd_pct=dd["pct_cap"], ratio=ratio, win=sum(t[4] > 0 for t in ts) / len(ts))


WINDOWS: list[tuple[str, Callable[[str], bool]]] = [("ALL", lambda d: True), ("2020-23", lambda d: d <= IS_END),
                                                    ("2024-26", lambda d: d > IS_END)]


# ---------------------------------------------------------------- read rules (frozen, LIQ_SIDE.md)
def read_rule_liq(is_stats: dict[str, dict], oos_stats: dict[str, dict]) -> tuple[str, bool, str]:
    """IS argmax mean R (TIERED) over LIQ_CANDIDATES (strict, ties keep the earlier; 0 trades cannot win).
    BASE wins -> no floor.  Else adopt iff OOS R > BASE OOS R and OOS max DD Rs <= BASE OOS max DD Rs."""
    def r_of(x: dict) -> float:
        return x["r"] if x.get("n", 0) > 0 else -math.inf
    best = LIQ_CANDIDATES[0]
    for v in LIQ_CANDIDATES[1:]:
        if r_of(is_stats[v]) > r_of(is_stats[best]):
            best = v
    if best == "BASE":
        return best, False, "BASE wins on IS -> no liquidity floor"
    b, w = oos_stats["BASE"], oos_stats[best]
    if w.get("n", 0) == 0 or b.get("n", 0) == 0:
        return best, False, "no OOS trades -> no liquidity floor"
    ok_r = w["r"] > b["r"]
    ok_dd = w["dd"] <= b["dd"]
    reason = (f"OOS R {w['r']:+.4f} vs BASE {b['r']:+.4f} ({'ok' if ok_r else 'FAIL'}); "
              f"OOS DD {w['dd']:,.0f} vs BASE {b['dd']:,.0f} ({'ok' if ok_dd else 'FAIL'})")
    return best, ok_r and ok_dd, reason


def read_rule_side(is_b: dict, is_w: dict, oos_b: dict, oos_w: dict) -> tuple[bool, str]:
    """Adopt WITH-MARKET iff IS R and IS P&L/DD strictly better than BASE AND OOS DD <= BASE AND OOS R >= BASE."""
    if min(is_b.get("n", 0), is_w.get("n", 0), oos_b.get("n", 0), oos_w.get("n", 0)) == 0:
        return False, "a window has no trades -> no WITH-MARKET filter"
    c = [("IS R", is_w["r"] > is_b["r"]), ("IS P&L/DD", is_w["ratio"] > is_b["ratio"]),
         ("OOS DD", oos_w["dd"] <= oos_b["dd"]), ("OOS R", oos_w["r"] >= oos_b["r"])]
    why = (f"IS R {is_w['r']:+.4f} vs {is_b['r']:+.4f}; IS P&L/DD {is_w['ratio']:.2f} vs {is_b['ratio']:.2f}; "
           f"OOS DD {oos_w['dd']:,.0f} vs {oos_b['dd']:,.0f}; OOS R {oos_w['r']:+.4f} vs {oos_b['r']:+.4f} -> "
           + ", ".join(f"{k} {'ok' if ok else 'FAIL'}" for k, ok in c))
    return all(ok for _, ok in c), why


# ---------------------------------------------------------------- B: NIFTY direction
def bar_close_1m(mins: list[dict], hhmm: str) -> float | None:
    """Close of the 5-min bar starting hhmm from 1-min bars ('HH:MM'): the close of minute hhmm+4, None if missing."""
    m = int(hhmm[:2]) * 60 + int(hhmm[3:]) + 4
    want = f"{m // 60:02d}:{m % 60:02d}"
    for b in mins:
        if b["t"] == want:
            return b["c"]
    return None


def load_nifty(use_tail: bool = USE_NOV_TAIL) -> dict:
    """bar[d][hhmm] = 5-min close (signal bars only); closes = sorted [(d, official close)]; src = day counts."""
    want = sorted({v[2] for v in SETUPS.values()})
    bar: dict[str, dict[str, float]] = defaultdict(dict)
    last5: dict[str, float] = {}
    for b in sorted(json.loads(NIFTY_5M.read_text()), key=lambda b: b["t"]):
        d, t = b["t"][:10], b["t"][11:16]
        if t in want:
            bar[d][t] = b["c"]
        last5[d] = b["c"]                     # last 5-min close of the day (only for the sign-disagreement count)
    closes = {r["date"]: r["close"] for r in json.loads(NIFTY_DAILY.read_text())}
    src = Counter(bar_5m=len(bar), close_official=len(closes))
    if use_tail:
        for f in sorted(NOV_SERIES.glob("*.json")):
            rec = json.loads(f.read_text())
            d, mins = rec["day"], rec["nifty"]
            if d not in bar:
                got = {h: c for h in want if (c := bar_close_1m(mins, h)) is not None}
                if got:
                    bar[d] = got
                    src["bar_1m_tail"] += 1
            if d not in closes and len(mins) >= 375 and mins[-1]["t"] == "15:29":
                closes[d] = mins[-1]["c"]
                src["close_1m_tail"] += 1
    return dict(bar=dict(bar), closes=sorted(closes.items()), last5=last5, src=src)


def _prev_index(close_dates: list[str], d: str, stock_days: list[str]) -> int | None:
    """Index of the NIFTY close of the trading day before d, or None if missing / a stock session lies between."""
    j = bisect.bisect_left(close_dates, d) - 1
    if j < 0:
        return None
    k = bisect.bisect_right(stock_days, close_dates[j])
    if k < len(stock_days) and stock_days[k] < d:
        return None                            # NIFTY calendar gap: its "previous day" is not the previous session
    return j


def stock_calendar(uni: list[str], raw: Path | None = None) -> list[str]:
    """Every date on which any universe stock has a DAILY bar, short sessions included (Muhurat, special Saturdays).
    Used as `stock_days` for the NIFTY previous-close gap check: the full-session days alone hide a short session,
    so a missing NIFTY close for it (2026-11-08 Muhurat, absent from the 1-min tail) would be silently filled by the
    close before it (review 11-20).  A missing raw file is skipped here; check_pick_sets guards the picks."""
    out: set[str] = set()
    for s in uni:
        f = (F.RAW if raw is None else raw) / f"{F.fname(s)}.json.gz"
        if f.exists():
            with gzip.open(f, "rt") as g:
                out.update(r[0] for r in json.load(g)["daily"])
    return sorted(out)


def wm_dirs(days: list[str], bar: dict[str, dict[str, float]], hhmm: str, closes: list[tuple[str, float]],
            stock_days: list[str]) -> tuple[dict[str, int], list[str]]:
    """d -> sign(signal-bar close - previous official close); days without data are returned as missing."""
    cd = [c[0] for c in closes]
    out, missing = {}, []
    for d in days:
        sc = bar.get(d, {}).get(hhmm)
        j = _prev_index(cd, d, stock_days)
        if sc is None or j is None:
            missing.append(d)
            continue
        out[d] = sign(sc - closes[j][1])
    return out, missing


def sma_dirs(days: list[str], closes: list[tuple[str, float]], stock_days: list[str], n: int = SMA_N
             ) -> tuple[dict[str, int], list[str]]:
    """d -> sign(close(d-1) - SMA_n of the n closes up to and including d-1)."""
    cd = [c[0] for c in closes]
    cv = [c[1] for c in closes]
    out, missing = {}, []
    for d in days:
        j = _prev_index(cd, d, stock_days)
        if j is None or j < n - 1:
            missing.append(d)
            continue
        out[d] = sign(cv[j] - sum(cv[j - n + 1:j + 1]) / n)
    return out, missing


# ---------------------------------------------------------------- B: one-sided bracket engine
def one_sided_day(bars: list[dict], spec: sim.Spec, side: int, choose: SideFn) -> list[sim.Trade]:
    """sim.breakout_day with max_trades=1 and ONLY `side`'s stop order placed (the other level is just the stop).
    Same levels, fill rule (gap -> open, else the level), last entry, exit and pessimistic same-bar handling."""
    if side not in (1, -1):
        raise ValueError(side)
    if spec.stop_mode != "opposite" or spec.max_trades != 1 or spec.hold_bars is not None:
        raise ValueError("one_sided_day supports the primary spec only")
    el, es, mid, buf = sim.levels(bars, spec)
    level = el if side > 0 else es
    pos: sim.Trade | None = None
    for i in range(spec.sig_start + spec.sig_n, spec.exit_idx + 1):
        b = bars[i]
        if pos is None:
            if i > spec.last_entry_idx:
                return []
            if not (b["h"] >= level if side > 0 else b["l"] <= level):
                continue
            at_open = b["o"] >= level if side > 0 else b["o"] <= level
            px = b["o"] if at_open else level
            stop, risk = sim._stop_for(side, px, spec, el, es, mid, buf)
            # the fill is this bar's first event (no other order exists), so first_event=True; order_known=False
            # because no second order makes the intrabar sequence known.
            pos = sim.Trade(side, i, px, stop, risk, at_open, first_event=True, order_known=False)
            touched = b["l"] <= stop if side > 0 else b["h"] >= stop
            if touched and (sim.same_bar_stop_certain(pos, b) or choose()):
                pos.exit_i, pos.exit_px, pos.reason = i, stop, "stop"
                return [pos]
        else:
            if b["l"] <= pos.stop if side > 0 else b["h"] >= pos.stop:
                pos.exit_i = i
                pos.exit_px = min(b["o"], pos.stop) if side > 0 else max(b["o"], pos.stop)
                pos.reason = "stop"
                return [pos]
        if i == spec.exit_idx:
            pos.exit_i, pos.exit_px, pos.reason = i, b["c"], "eod"
            return [pos]
    return []


def _spec(bars: list[dict], sig_n: int) -> sim.Spec | None:
    """The exact spec / validity checks of stocks_asrs_check.trade."""
    if len(bars) < 4 + sig_n:
        return None
    last = min(len(bars) - 1, sim.EXIT_IDX)
    spec = sim.Spec(3, sig_n, exit_idx=last, last_entry_idx=min(sim.LAST_ENTRY_IDX, last))
    sig = bars[3:3 + sig_n]
    if max(b["h"] for b in sig) - min(b["l"] for b in sig) < 0.001 * sig[-1]["c"]:
        return None                                          # frozen / circuit signal bar
    return spec


def trade_one_sided(bars: list[dict], sig_n: int, side: int) -> dict | None:
    """Like stocks_asrs_check.trade, one side only. None = not tradeable (too few bars / frozen signal)."""
    spec = _spec(bars, sig_n)
    if spec is None:
        return None
    paths = sim.all_paths(lambda ch: one_sided_day(bars, spec, side, ch))
    worst = min(paths, key=lambda p: sum(t.pts for t in p))
    if not worst:
        return dict(trig=False)
    t = worst[0]
    c = S.cost_per_share(t.side, t.entry_px, t.exit_px)
    return dict(trig=True, side=t.side, entry=t.entry_px, stop=t.stop, exit=t.exit_px, risk=t.risk,
                r_net=(t.pts - c) / t.risk, cost_r=c / t.risk, amb=len(paths) > 1, reason=t.reason)


def first_touch_both(bars: list[dict], sig_n: int) -> bool:
    """Did the first bar that touched either level (within the entry window) touch BOTH levels?"""
    spec = _spec(bars, sig_n)
    if spec is None:
        return False
    el, es, _, _ = sim.levels(bars, spec)
    for i in range(spec.sig_start + spec.sig_n, spec.last_entry_idx + 1):
        hi, lo = bars[i]["h"] >= el, bars[i]["l"] <= es
        if hi or lo:
            return hi and lo
    return False


def to_row(d: str, pick: bool, score: float, t: dict | None) -> list:
    """Same 10-field row (and the same cps = cost_r * risk arithmetic) as full_asrs.stock_rows."""
    if t and t["trig"]:
        return [d, pick, score, t["side"], t["entry"], t["stop"], t["exit"], t["risk"], t["r_net"],
                t["cost_r"] * t["risk"]]
    return [d, pick, score, 0, 0, 0, 0, 0, 0, 0]


# ---------------------------------------------------------------- cache build
def stock_cache(s: str, pick_rows: list[list]) -> dict:
    """pick_rows: this stock's SRS rows with pick true. -> {turn: {d: T}, SRS/ASRS: {d: [long_row, short_row, both]}}."""
    daily, m5 = F.load_stock(s)
    dates = [b["d"] for b in daily]
    sessions = Y.full_sessions(m5)
    out: dict = {"v": CACHE_VERSION, "turn": {}, **{k: {} for k in SETUPS}}
    for r in pick_rows:
        d = r[0]
        out["turn"][d] = turnover_sma(daily, d, dates=dates)
        bars, _ = F.fill_gaps(sessions[d])                     # KeyError = rows/raw out of sync: never guess
        cb = S.completed(bars, None) if bars else []
        for name, (sig_n, _, _) in SETUPS.items():
            out[name][d] = [to_row(d, r[1], r[2], trade_one_sided(cb, sig_n, side) if cb else None)
                            for side in (1, -1)] + [first_touch_both(cb, sig_n) if cb else False]
    return out


def load_cache(s: str) -> dict | None:
    f = CACHE / f"{F.fname(s)}.json"
    if not f.exists():
        return None
    x = json.loads(f.read_text())
    return x if x.get("v") == CACHE_VERSION else None


def invariant_counts(by_day: dict[str, list[tuple[str, list]]], cache: dict[str, dict], setup: str) -> Counter:
    """Two-sided first trigger X vs the one-sided X row (LIQ_SIDE.md invariants)."""
    c: Counter = Counter()
    for d, xs in by_day.items():
        for s, r in xs:
            if not r[1]:
                continue
            long_r, short_r, both = cache[s][setup][d]
            if r[3] == 0:
                c["untriggered"] += 1
                c["VIOLATION_untriggered_but_one_sided_trades"] += (long_r[3] != 0) or (short_r[3] != 0)
                continue
            same = long_r if r[3] > 0 else short_r
            if same == r:
                c["first_side_equal"] += 1
            elif both:
                c["first_side_differs_both_level_bar"] += 1
            else:
                c["VIOLATION_first_side_differs"] += 1
            other = short_r if r[3] > 0 else long_r
            c["other_side_also_triggers"] += other[3] != 0
    return c


def build() -> None:
    uni = json.loads(Y.UNIVERSE.read_text())
    srs_dir = SETUPS["SRS"][1]
    n_new = n_old = 0
    for s in uni:
        f = srs_dir / f"{F.fname(s)}.json"
        if not f.exists():
            continue
        if load_cache(s) is not None:
            n_old += 1
            continue
        pick_rows = [r for r in json.loads(f.read_text()) if r[1]]
        write_json_atomic(CACHE / f"{F.fname(s)}.json", stock_cache(s, pick_rows))
        n_new += 1
        print("cache", s, len(pick_rows), "pick sessions", flush=True)
    print(f"cache files: {n_new} built, {n_old} already present")
    cache = load_all_cache(uni)
    turn_none = sum(v is None for x in cache.values() for v in x["turn"].values())
    print(f"pick sessions with turnover: {sum(len(x['turn']) for x in cache.values())}, turnover None: {turn_none}")
    nifty = load_nifty()
    print("NIFTY day counts per source:", dict(nifty["src"]))
    cal_all = stock_calendar(uni)
    for name, (_, rows_dir, hhmm) in SETUPS.items():
        by_day = T.load_days(rows_dir)
        check_pick_sets(by_day, cache, name)
        print(f"{name} invariants:", dict(invariant_counts(by_day, cache, name)))
        days = sorted(by_day)
        cal = sorted(set(days) | set(cal_all))
        dirs, miss = wm_dirs(days, nifty["bar"], hhmm, nifty["closes"], cal)
        print(f"{name} WM direction: {len(days)} stock days, up {sum(v > 0 for v in dirs.values())}, "
              f"down {sum(v < 0 for v in dirs.values())}, zero {sum(v == 0 for v in dirs.values())}, "
              f"excluded {len(miss)} {miss[:20]}")
        cd = [c[0] for c in nifty["closes"]]
        dis = 0
        for d, v in dirs.items():
            j = _prev_index(cd, d, cal)
            alt = nifty["last5"].get(cd[j]) if j is not None else None
            dis += alt is not None and sign(nifty["bar"][d][hhmm] - alt) != v
        print(f"{name} WM sign disagreements, official prev close vs last 5-min bar close: {dis}")
    days = sorted(T.load_days(SETUPS["SRS"][1]))
    sd, sm = sma_dirs(days, nifty["closes"], sorted(set(days) | set(cal_all)))
    print(f"SMA direction: up {sum(v > 0 for v in sd.values())}, down {sum(v < 0 for v in sd.values())}, "
          f"zero {sum(v == 0 for v in sd.values())}, excluded {len(sm)} {sm[:20]}")


def load_all_cache(uni: list[str]) -> dict[str, dict]:
    out = {}
    for s in uni:
        x = load_cache(s)
        if x is not None:
            out[s] = x
    return out


def check_pick_sets(by_day: dict[str, list[tuple[str, list]]], cache: dict[str, dict], setup: str) -> None:
    """Every pick session must be in the cache (built from the SRS rows; ASRS rows share the same scan)."""
    bad = sum(1 for d, xs in by_day.items() for s, r in xs if r[1] and (s not in cache or d not in cache[s][setup]))
    if bad:
        raise SystemExit(f"{setup}: {bad} pick sessions missing from the liq_side cache -> run 'build'")


# ---------------------------------------------------------------- report pieces (NOT run before the review)
def liq_pick_fn(by_day, turn: dict[tuple[str, str], float | None], floor, slip_model) -> Callable:
    """Turnover is looked up only when the floor or the TIERED model needs it (KeyError if missing: never guess)."""
    def picks_of(d: str) -> list[tuple[str, list, float]]:
        cands = [(s, r) for s, r in by_day[d] if r[1]]
        if floor is None and slip_model != TIERED:
            return [(s, r, slip_model) for s, r in top_by_score(cands)]
        tday = {s: turn[(s, d)] for s, _ in cands}
        sel = select_floor(cands, tday, floor)
        return [(s, r, slip_for_turnover(tday[s]) if slip_model == TIERED else slip_model) for s, r in sel]
    return picks_of


def side_pick_fn(by_day, cache: dict[str, dict], setup: str, dirs: dict[str, int], mode: str, slip: float
                 ) -> Callable:
    """mode: base (BASE on these days) | ft (first-trigger filter) | os (one-sided, live rule) | against (one-sided,
    opposite side, diagnostic)."""
    if mode not in ("base", "ft", "os", "against"):
        raise ValueError(mode)

    def picks_of(d: str) -> list[tuple[str, list, float]]:
        sel = top_by_score([(s, r) for s, r in by_day[d] if r[1]])
        if mode == "base":
            return [(s, r, slip) for s, r in sel]
        v = dirs[d]
        if v == 0:
            return []
        if mode == "ft":
            return [(s, r, slip) for s, r in sel if r[3] == v]
        want = v if mode == "os" else -v
        return [(s, cache[s][setup][d][0 if want > 0 else 1], slip) for s, r in sel]
    return picks_of


def fmt(label: str, x: dict) -> str:
    if x.get("n", 0) == 0:
        return f"{label} | no trades ({x.get('days', 0)} days)"
    return (f"{label} | {x['n']:6d} | {x['n'] / x['days']:4.1f}/d | {x['r']:+.3f} [{x['ci'][0]:+.3f},{x['ci'][1]:+.3f}]"
            f" | win {x['win']:.0%} | {x['rs']:+13,.0f} | DD {x['dd']:11,.0f} = {x['dd_pct']:5.1f}% | "
            f"P&L/DD {x['ratio']:6.2f}")


def turnover_table(by_day, turn) -> list[str]:
    L = ["\nTurnover (Rs Cr, SMA20 of close x volume to d-1) of the BASE top-10 picks, and median picks/day per floor:",
         "year | n | p10 p25 p50 p75 p90 | <200 | 200-1000 | >=1000 | picks/day: all PCT30 PCT50 ABS200"]
    for y in sorted({d[:4] for d in by_day}):
        sel_t, counts = [], defaultdict(list)
        for d in sorted(x for x in by_day if x.startswith(y)):
            cands = [(s, r) for s, r in by_day[d] if r[1]]
            tday = {s: turn[(s, d)] for s, _ in cands}
            sel_t += [tday[s] for s, _ in top_by_score(cands)]
            for v, fl in FLOORS.items():
                counts[v].append(len(select_floor(cands, tday, fl, top_n=10 ** 6)))
        known = [t / CR for t in sel_t if t is not None]
        q = st.quantiles(known, n=20) if len(known) >= 2 else [math.nan] * 19
        n = len(sel_t)
        tiers = Counter(slip_for_turnover(t) for t in sel_t)
        L.append(f"{y} | {n} | {q[1]:.0f} {q[4]:.0f} {q[9]:.0f} {q[14]:.0f} {q[17]:.0f} | "
                 f"{tiers[SLIP_LO] / n:.0%} | {tiers[SLIP_MID] / n:.0%} | {tiers[SLIP_HI] / n:.0%} | "
                 + " ".join(f"{st.median(counts[v]):g}" for v in FLOORS))
    return L


def report_setup(name: str, cache: dict[str, dict], nifty: dict, calendar: list[str]) -> list[str]:
    """calendar: stock_calendar(uni) (every stock daily-bar date, short sessions included), required on purpose."""
    sig_n, rows_dir, hhmm = SETUPS[name]
    by_day = T.load_days(rows_dir)
    check_pick_sets(by_day, cache, name)
    days = sorted(by_day)
    cal = sorted(set(days) | set(calendar))
    turn = {(s, d): t for s, x in cache.items() for d, t in x["turn"].items()}
    L = [f"\n================ {name} ================"]
    # ---- A
    L.append("A. LIQUIDITY FLOOR. variant | slip | window | trades | /day | avg R [CI] | win | P&L Rs | max DD | P&L/DD")
    res: dict[tuple[str, str | float], dict[str, dict]] = {}
    for v, fl in FLOORS.items():
        for sm in SLIP_MODELS:
            trades, daily = run_days(days, liq_pick_fn(by_day, turn, fl, sm))
            res[(v, sm)] = {wn: stats(trades, daily, sel) for wn, sel in WINDOWS}
            sl = sm if sm == TIERED else f"{sm * 100:.2f}%"
            for wn, _ in WINDOWS:
                L.append(fmt(f"{v:6s} | {sl:6s} | {wn:7s}", res[(v, sm)][wn]))
    win, adopted, why = read_rule_liq({v: res[(v, TIERED)]["2020-23"] for v in FLOORS},
                                      {v: res[(v, TIERED)]["2024-26"] for v in FLOORS})
    L.append(f"READ A ({name}, TIERED = ASSUMED slippage): IS winner {win} -> "
             f"{'ADOPT ' + win if adopted else 'NO LIQUIDITY FLOOR'}. {why}")
    L += turnover_table(by_day, turn)
    # ---- B1
    L.append("\nB1. BASE BY SIDE (0.02%). side | window | trades | /day | avg R [CI] | win | P&L Rs | max DD | P&L/DD")
    trades, daily = run_days(days, liq_pick_fn(by_day, turn, None, T.BASE_SLIP))
    for side, sn in ((1, "LONG"), (-1, "SHORT")):
        ts = [t for t in trades if t[2] == side]
        pnl: dict[str, float] = defaultdict(float)
        for t in ts:
            pnl[t[0]] += t[4]
        dl = [(d, pnl.get(d, 0.0)) for d in days]
        for wn, sel in WINDOWS:
            L.append(fmt(f"{sn:5s} | {wn:7s}", stats(ts, dl, sel)))
        for y in sorted({d[:4] for d in days}):
            L.append(fmt(f"{sn:5s} | {y}   ", stats(ts, dl, lambda d, y=y: d.startswith(y))))
    # ---- B2
    L.append("\nB2. WITH-MARKET. def | engine | slip | window | trades | /day | avg R [CI] | win | P&L Rs | max DD | "
             "P&L/DD")
    defs = {"WM": wm_dirs(days, nifty["bar"], hhmm, nifty["closes"], cal),
            "SMA": sma_dirs(days, nifty["closes"], cal)}
    read_in = {}
    for dn, (dirs, miss) in defs.items():
        cdays = sorted(dirs)
        L.append(f"{dn}: common days {len(cdays)}, up {sum(v > 0 for v in dirs.values())}, down "
                 f"{sum(v < 0 for v in dirs.values())}, zero {sum(v == 0 for v in dirs.values())}, excluded {len(miss)}")
        late = sum(1 for d in cdays if dirs[d] != 0 for s, r in top_by_score([(s, r) for s, r in by_day[d] if r[1]])
                   if r[3] == -dirs[d] and cache[s][name][d][0 if dirs[d] > 0 else 1][3] != 0)
        L.append(f"{dn}: one-sided trades whose pick first triggered on the OTHER side (late agreeing): {late}")
        for slip in (READ_SLIP_B, INFO_SLIP_B):
            for mode in ("base", "os", "ft", "against"):
                trades, daily = run_days(cdays, side_pick_fn(by_day, cache, name, dirs, mode, slip))
                for wn, sel in WINDOWS:
                    x = stats(trades, daily, sel)
                    if slip == READ_SLIP_B and dn == "WM":
                        read_in[(mode, wn)] = x
                    L.append(fmt(f"{dn:3s} | {mode:7s} | {slip * 100:.2f}% | {wn:7s}", x))
    ok, why = read_rule_side(read_in[("base", "2020-23")], read_in[("os", "2020-23")],
                             read_in[("base", "2024-26")], read_in[("os", "2024-26")])
    L.append(f"READ B ({name}, WM one-sided vs BASE, 0.02%): {'ADOPT WITH-MARKET' if ok else 'NO WITH-MARKET FILTER'}."
             f" {why}")
    return L


def report() -> None:
    uni = json.loads(Y.UNIVERSE.read_text())
    cache = load_all_cache(uni)
    nifty = load_nifty()
    calendar = stock_calendar(uni)
    L = ["LIQ_SIDE (LIQ_SIDE.md, frozen 2026-11-20). SURVIVORSHIP-BIASED universe (today's F&O list). "
         "Capital 10L, risk 10k, notional cap 5L, NEO cash costs. TIERED slippage is an ASSUMPTION.",
         f"NIFTY sources (days): {dict(nifty['src'])}"]
    for name in SETUPS:
        part = report_setup(name, cache, nifty, calendar)
        print("\n".join(part), flush=True)
        L += part
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(L), encoding="utf-8")


def main(argv: list[str]) -> None:
    if argv == ["build"]:
        build()
    elif argv == ["report"]:
        report()
    else:
        raise SystemExit("usage: liq_side.py build|report")


if __name__ == "__main__":
    main(_argv)
