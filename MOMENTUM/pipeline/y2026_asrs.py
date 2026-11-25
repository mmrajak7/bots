"""2026 YTD ASRS on the Chartink 'stocks in play' scan (Y2026_ASRS.md, frozen 2026-11-20).

Streams one stock at a time (low-RAM machine): for each stock and full session, records whether it was a pick, its
pick rank score, and the ASRS trade (if triggered).  Aggregation then compares picks vs universe vs random.

    python -I y2026_asrs.py fetch    # daily from 2025-11-01, 5-min 2026-01-01..2026-11-20, per stock -> scratchpad
    python -I y2026_asrs.py run
"""
from __future__ import annotations

import json
import random
import statistics as st
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import nov_stocks as N  # noqa: E402  (validated scan replica)
import stocks_asrs_check as S  # noqa: E402  (reviewed trade + costs)

CACHE = Path(r"C:/Users/mail2/AppData/Local/Temp/claude/cab3a769884d34b1/"
             r"b6d89720-ff27-454d-9c33-e29855d0a4dc/scratchpad/srs_week/y2026")
UNIVERSE = N.CACHE / "universe.json"
DAILY_FROM, M5_FROM, TO = date(2025, 11, 1), date(2026, 1, 1), date(2026, 11, 20)
LIVE_DAY = "2026-11-20"
SEEDS, TOP_N = 500, 10


def full_sessions(m5: list[dict]) -> dict[str, list[dict]]:
    """date -> 5-min bars, full sessions only (first bar 09:15, >= 72 bars)."""
    by: dict[str, list[dict]] = defaultdict(list)
    for b in m5:
        by[b["t"][:10]].append(b)
    return {d: v for d, v in by.items() if v[0]["t"][11:] == "09:15" and len(v) >= 72 and d != LIVE_DAY}


def fetch() -> None:
    from kiteconnect import KiteConnect
    tok = json.loads((HERE.parent / "data/kite_access_token.json").read_text())
    k = KiteConnect(api_key=tok["api_key"])
    k.set_access_token(tok["access_token"])
    eq = {i["tradingsymbol"]: i["instrument_token"] for i in k.instruments("NSE")}
    CACHE.mkdir(parents=True, exist_ok=True)
    conv = lambda rows, fmt: [dict(t=c["date"].strftime(fmt), o=c["open"], h=c["high"], l=c["low"],
                                   c=c["close"], v=c["volume"]) for c in rows]
    for s in json.loads(UNIVERSE.read_text()):
        f = CACHE / f"{s.replace('&', '_')}.json"
        if f.exists():
            continue
        daily = conv(k.historical_data(eq[s], DAILY_FROM, TO, "day"), "%Y-%m-%d")
        time.sleep(0.35)
        m5, a = [], M5_FROM
        while a <= TO:
            b = min(a + timedelta(days=95), TO)
            m5 += conv(k.historical_data(eq[s], a, b, "5minute"), "%Y-%m-%d %H:%M")
            time.sleep(0.35)
            a = b + timedelta(days=1)
        for x in daily:
            x["d"] = x.pop("t")
        f.write_text(json.dumps(dict(daily=daily, m5=m5)))
        print(s, len(daily), len(m5), flush=True)
    print("done")


def per_stock(s: str) -> list[dict]:
    """One row per full session: pick flag, rank score, ASRS trade (or None)."""
    x = json.loads((CACHE / f"{s.replace('&', '_')}.json").read_text())
    daily = sorted(x["daily"], key=lambda b: b["d"])
    out = []
    for d, bars in full_sessions(x["m5"]).items():
        f15 = N.first15_volume(bars)
        prev = [b for b in daily if b["d"] < d]
        pick = N.is_pick(daily, d, f15)
        score = (f15 / (sum(b["v"] for b in prev[-20:]) / 20)) if (pick and len(prev) >= 20) else 0.0
        t = S.trade(S.completed(bars, None), d, 1)
        trade = None
        if t and t["trig"]:
            trade = dict(side=t["side"], r=t["r_net"], rg=t["r_gross"])
        out.append(dict(s=s, d=d, pick=pick, score=score, trade=trade))
    return out


def boot_ci(pairs: list[tuple[str, float]], n: int = 2000, seed: int = 7) -> tuple[float, float]:
    by: dict[str, list[float]] = defaultdict(list)
    for d, v in pairs:
        by[d].append(v)
    days = list(by.values())
    rng = random.Random(seed)
    ms = []
    for _ in range(n):
        pick = [days[rng.randrange(len(days))] for _ in days]
        ms.append(sum(sum(v) for v in pick) / sum(len(v) for v in pick))
    ms.sort()
    return ms[int(.025 * n)], ms[int(.975 * n) - 1]


def summ(label: str, pairs: list[tuple[str, float]]) -> str:
    if not pairs:
        return f"{label}: none"
    v = [r for _, r in pairs]
    by: dict[str, float] = defaultdict(float)
    for d, r in pairs:
        by[d] += r
    dv = sorted(by.values(), reverse=True)
    pos = sum(x for x in dv if x > 0)
    lo, hi = boot_ci(pairs)
    return (f"{label}: {len(v)} trades, avg {st.mean(v):+.3f}R CI[{lo:+.3f},{hi:+.3f}], total {sum(v):+.1f}R "
            f"(Rs {sum(v) * S.RISK_RS:+,.0f}), win {sum(r > 0 for r in v) / len(v):.0%}, "
            f"top10days {(sum(dv[:10]) / pos if pos else float('nan')):.0%}")


def run() -> None:
    uni = json.loads(UNIVERSE.read_text())
    rows = []
    for s in uni:
        if (CACHE / f"{s.replace('&', '_')}.json").exists():
            rows += per_stock(s)
    days = sorted({r["d"] for r in rows})
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[r["d"]].append(r)
    picks = [(r["d"], r["trade"]["r"]) for r in rows if r["pick"] and r["trade"]]
    univ = [(r["d"], r["trade"]["r"]) for r in rows if r["trade"]]
    top = []
    for d, rs in by_day.items():
        best = sorted([r for r in rs if r["pick"]], key=lambda r: -r["score"])[:TOP_N]
        top += [(d, r["trade"]["r"]) for r in best if r["trade"]]
    n_picks = [sum(r["pick"] for r in by_day[d]) for d in days]
    print(f"2026 YTD: {len(days)} sessions, {len(uni)} stocks, picks/day median {st.median(n_picks)} "
          f"(min {min(n_picks)}, max {max(n_picks)}), days with 0 picks {sum(n == 0 for n in n_picks)}")
    print("   " + summ("PICKS (primary)", picks))
    print("   " + summ("UNIVERSE (all F&O)", univ))
    print("   " + summ("TOP-10 picks/day (secondary)", top))
    # random same-count control, drawn from the WHOLE universe each day, triggers kept
    rng = random.Random(7)
    rnd = []
    for _ in range(SEEDS):
        vals = []
        for d in days:
            rs = by_day[d]
            k = sum(r["pick"] for r in rs)
            for r in rng.sample(rs, min(k, len(rs))):
                if r["trade"]:
                    vals.append(r["trade"]["r"])
        rnd.append(st.mean(vals) if vals else 0.0)
    rnd.sort()
    mp = st.mean(v for _, v in picks)
    mu = st.mean(v for _, v in univ)
    pct = sum(x < mp for x in rnd) / SEEDS
    print(f"   RANDOM same-count: median {rnd[SEEDS // 2]:+.3f}R, 95th {rnd[int(.95 * SEEDS)]:+.3f}R -> picks at {pct:.0%}")
    lo = boot_ci(picks)[0]
    ok = mp > 0 and lo > 0 and mp > mu and mp > rnd[int(.95 * SEEDS)]
    print(f"   READ (frozen): avg>0 {mp > 0}, CI low>0 {lo > 0}, beats universe {mp > mu}, above random 95th "
          f"{mp > rnd[int(.95 * SEEDS)]} -> {'WORTH WORKING ON' if ok else 'NOT SHOWN'}")
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        print("   " + summ(f"picks {name}", [(r['d'], r['trade']['r']) for r in rows
                                             if r['pick'] and r['trade'] and r['trade']['side'] == side]))
        print("   " + summ(f"universe {name}", [(r['d'], r['trade']['r']) for r in rows
                                                if r['trade'] and r['trade']['side'] == side]))
    print("   by month (picks | universe), avg net R per trade:")
    for m in sorted({d[:7] for d in days}):
        p = [v for d, v in picks if d.startswith(m)]
        u = [v for d, v in univ if d.startswith(m)]
        print(f"     {m}: picks {len(p):4d} tr {(st.mean(p) if p else 0):+.3f}R tot {sum(p):+7.1f}R | "
              f"universe {(st.mean(u) if u else 0):+.3f}R")


if __name__ == "__main__":
    fetch() if sys.argv[1:2] == ["fetch"] else run()
