"""November 2026 check (NOV_STOCKS.md, frozen 2026-11-20): rebuild the Chartink 'stocks in play' scan from Kite,
validate it against Chartink's own 09:30 lists, then ASRS/SRS on picks vs the whole F&O universe vs random picks.

    python -I nov_stocks.py fetch      # F&O list, daily bars (from 2026-09-01) and 5-min bars (Nov) -> scratchpad
    python -I nov_stocks.py validate   # replica vs Chartink lists (must pass before 'run')
    python -I nov_stocks.py run
"""
from __future__ import annotations

import json
import random
import statistics as st
import sys
import time
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import stocks_asrs_check as S  # noqa: E402

CACHE = Path(r"C:/Users/mail2/AppData/Local/Temp/claude/cab3a769884d34b1/"
             r"b6d89720-ff27-454d-9c33-e29855d0a4dc/scratchpad/srs_week/nov_stocks")
CHARTINK = HERE / "data/chartink_picks_0930.json"
DAY_FROM, NOV_FROM, NOV_TO = date(2026, 9, 1), date(2026, 11, 2), date(2026, 11, 20)
LIVE_DAY = "2026-11-20"
VOL_MULT, ATR_PCT, MIN_PX = 0.2, 1.5, 100.0
SEEDS = 500


# ---------------------------------------------------------------- scan replica (pure, tested)
def wilder_atr(daily: list[dict], n: int = 14) -> list[float | None]:
    """Wilder ATR aligned with daily bars; None until n true ranges exist."""
    out: list[float | None] = []
    trs, atr = [], None
    for i, b in enumerate(daily):
        tr = b["h"] - b["l"] if i == 0 else max(b["h"], daily[i - 1]["c"]) - min(b["l"], daily[i - 1]["c"])
        trs.append(tr)
        if len(trs) == n:
            atr = sum(trs) / n
        elif len(trs) > n:
            atr = (atr * (n - 1) + tr) / n
        out.append(atr)
    return out


def first15_volume(bars5: list[dict]) -> float | None:
    v = {b["t"][11:]: b["v"] for b in bars5}
    if not all(t in v for t in ("09:15", "09:20", "09:25")):
        return None
    return v["09:15"] + v["09:20"] + v["09:25"]


def is_pick(daily: list[dict], d: str, f15: float | None) -> bool:
    """daily sorted by date; uses only bars BEFORE day d (plus today's first-15-min volume)."""
    prev = [b for b in daily if b["d"] < d]
    if f15 is None or len(prev) < 20:
        return False
    atr = wilder_atr(prev)[-1]
    if atr is None:
        return False
    sma20 = sum(b["v"] for b in prev[-20:]) / 20
    c = prev[-1]["c"]
    return f15 > VOL_MULT * sma20 and atr / c * 100 > ATR_PCT and c > MIN_PX


# ---------------------------------------------------------------- data
def fetch() -> None:
    from kiteconnect import KiteConnect
    tok = json.loads((HERE.parent / "data/kite_access_token.json").read_text())
    k = KiteConnect(api_key=tok["api_key"])
    k.set_access_token(tok["access_token"])
    fo = sorted({i["name"] for i in k.instruments("NFO") if i["instrument_type"] == "FUT"
                 and i["segment"] == "NFO-FUT" and i["name"] not in ("NIFTY", "BANKNIFTY", "FINNIFTY",
                                                                       "MIDCPNIFTY", "NIFTYNXT50")})
    eq = {i["tradingsymbol"]: i["instrument_token"] for i in k.instruments("NSE")}
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / "universe.json").write_text(json.dumps([s for s in fo if s in eq]))
    missing = [s for s in fo if s not in eq]
    print("F&O stocks", len(fo), "missing on NSE", missing)
    for s in fo:
        if s not in eq:
            continue
        f = CACHE / f"{s.replace('&', '_')}.json"
        if f.exists():
            continue
        d = k.historical_data(eq[s], DAY_FROM, NOV_TO, "day")
        time.sleep(0.35)
        m = k.historical_data(eq[s], NOV_FROM, NOV_TO, "5minute")
        time.sleep(0.35)
        f.write_text(json.dumps(dict(
            daily=[dict(d=c["date"].strftime("%Y-%m-%d"), o=c["open"], h=c["high"], l=c["low"], c=c["close"],
                        v=c["volume"]) for c in d],
            m5=[dict(t=c["date"].strftime("%Y-%m-%d %H:%M"), o=c["open"], h=c["high"], l=c["low"], c=c["close"],
                     v=c["volume"]) for c in m])))
    print("done")


def load() -> dict:
    uni = json.loads((CACHE / "universe.json").read_text())
    out = {}
    for s in uni:
        f = CACHE / f"{s.replace('&', '_')}.json"
        if f.exists():
            out[s] = json.loads(f.read_text())
    return out


def nov_days(data: dict) -> list[str]:
    """Full sessions only: first bar 09:15 and >= 72 bars (drops the Muhurat session, review 11-20)."""
    days = set()
    for x in data.values():
        by: dict[str, list[str]] = {}
        for b in x["m5"]:
            by.setdefault(b["t"][:10], []).append(b["t"][11:])
        days |= {d for d, ts in by.items() if min(ts) == "09:15" and len(ts) >= 72}
    return sorted(days)


def nifty_asrs(d: str) -> str:
    """NIFTY ASRS result that day (index, reviewed sim) as a trend-day flag; '' if no data."""
    import options_check as O
    import sim
    f = O.CACHE / f"{d}.json"
    if not f.exists():
        return ""
    n5 = O.to5(json.loads(f.read_text())["nifty"])
    for b in n5:
        b["t"] = d + b["t"][10:]
    if len(n5) < 72:
        return ""
    w = min(sim.all_paths(lambda ch: sim.breakout_day(n5, sim.Spec(3, 1), ch)), key=lambda p: sum(t.pts for t in p))
    if not w:
        return "NIFTY: no trigger"
    t = w[0]
    return f"NIFTY ASRS {'L' if t.side > 0 else 'S'} {t.pts / t.risk:+.1f}R"


def picks_for(data: dict, d: str) -> list[str]:
    out = []
    for s, x in data.items():
        bars = [b for b in x["m5"] if b["t"].startswith(d)]
        if is_pick(x["daily"], d, first15_volume(bars)):
            out.append(s)
    return sorted(out)


# ---------------------------------------------------------------- validate
def validate() -> bool:
    data = load()
    ref = {k: v for k, v in json.loads(CHARTINK.read_text()).items() if not k.startswith("_")}
    inter = union = 0
    for d, lst in ref.items():
        mine = set(picks_for(data, d))
        theirs = set(lst)
        inter += len(mine & theirs)
        union += len(mine | theirs)
        print(f"{d}: chartink {len(theirs)}, replica {len(mine)}, both {len(mine & theirs)} | "
              f"only chartink {sorted(theirs - mine)} | only replica {sorted(mine - theirs)}")
    j = inter / union if union else 0.0
    print(f"combined overlap |A&B|/|AuB| = {j:.2f}  ->  {'PASS' if j >= 0.8 else 'FAIL (investigate before results)'}")
    return j >= 0.8


# ---------------------------------------------------------------- run
def day_trades(data: dict, d: str, syms: list[str], sig_n: int) -> dict[str, dict]:
    out = {}
    for s in syms:
        bars = S.completed([b for b in data[s]["m5"] if b["t"].startswith(d)], None)
        x = S.trade(bars, d, sig_n)
        if x and x["trig"]:
            out[s] = x
    return out


def run() -> None:
    if not validate():
        raise SystemExit("replica does not match Chartink well enough — no results read")
    data = load()
    all_days = nov_days(data)
    days = [d for d in all_days if d != LIVE_DAY]
    uni = sorted(data)
    for setup, sig_n in (("ASRS", 1), ("SRS", 3)):
        print(f"\n===== {setup}: picks vs whole F&O universe (net R per trade, NEO cash costs) =====")
        allp, allu, per_day = [], [], []
        for d in days:
            picks = picks_for(data, d)
            tu = day_trades(data, d, uni, sig_n)
            tp = {s: tu[s] for s in picks if s in tu}
            pr = [x["r_net"] for x in tp.values()]
            ur = [x["r_net"] for x in tu.values()]
            allp += [(d, r) for r in pr]
            allu += [(d, r) for r in ur]
            per_day.append((d, len(picks), tu, list(tp)))
            print(f"   {d}: picks {len(picks):2d} triggered {len(pr):2d} sum {sum(pr):+6.1f}R avg "
                  f"{(st.mean(pr) if pr else 0):+.2f}R | universe {len(ur):3d} trades avg "
                  f"{(st.mean(ur) if ur else 0):+.2f}R (L {sum(1 for x in tu.values() if x['side'] > 0)} / "
                  f"S {sum(1 for x in tu.values() if x['side'] < 0)}) | {nifty_asrs(d)}")
        mp, mu = st.mean(r for _, r in allp), st.mean(r for _, r in allu)
        rng = random.Random(7)
        rnd = []
        for _ in range(SEEDS):
            vals = []
            for d, k, tu, _p in per_day:
                # like-for-like with the picks: draw k stocks from the WHOLE universe, keep those that triggered
                for s in rng.sample(uni, min(k, len(uni))):
                    if s in tu:
                        vals.append(tu[s]["r_net"])
            rnd.append(st.mean(vals) if vals else 0.0)
        rnd.sort()
        pct = sum(v < mp for v in rnd) / SEEDS
        print(f"   NOVEMBER {len(days)} days: PICKS {len(allp)} trades avg {mp:+.3f}R (total {sum(r for _, r in allp):+.1f}R"
              f" = Rs {sum(r for _, r in allp) * S.RISK_RS:+,.0f}) | UNIVERSE {len(allu)} trades avg {mu:+.3f}R | "
              f"RANDOM same-count: median {rnd[SEEDS // 2]:+.3f}R, 95th {rnd[int(.95 * SEEDS)]:+.3f}R -> picks at "
              f"{pct:.0%} percentile")
        print(f"   READ (frozen): picks beat universe {mp > mu}, above random 95th {mp > rnd[int(.95 * SEEDS)]}")
        if LIVE_DAY in all_days:
            picks = picks_for(data, LIVE_DAY)
            print(f"   (live {LIVE_DAY}, NOT in totals: {len(picks)} picks — see stocks_asrs_check.py for the marked trades)")


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    if cmd == ["fetch"]:
        fetch()
    elif cmd == ["validate"]:
        validate()
    else:
        run()
