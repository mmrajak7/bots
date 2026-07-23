"""
Universe Persistence Test (avenue E from 20/01)
================================================
Question: does a stock's PAST ST-touch swing record predict its FUTURE
touch performance, out of sample?

Method:
  - Detect ALL confirmed monthly-ST(10,3) touch events per stock directly
    from the backtest cache (no Chartink CSV needed):
      touch  = month low reaches the ST line (same tolerance as backtest_v4_1)
      dir    = ST direction UP (support test, not resistance)
      confirm= month CLOSE above ST
      price  >= Rs 100 at touch month close
  - Trade each event with the FROZEN rules: enter next month open,
    SL = touch-month wick, trail = peak - 1.5x ATR(14) monthly, flat size.
  - Split: Period A = entries before 2023-01-01, Period B = entries after.
  - Per-stock record in A (n, win rate, avg %) vs performance in B.
  - Verdict: bucket comparison + rank correlation + "filtered universe"
    portfolio vs "all stocks" portfolio on Period B only.

Run from Helper/:  python 20/universe_persistence_test.py
"""
import sys
import json
from datetime import date
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playbook.backtest_v4_1 import (
    load_daily, aggregate_to_monthly, compute_supertrend, should_skip,
    simulate_trade, CACHE_DIR, ST_PERIOD, ST_MULTIPLIER,
    TOUCH_ABOVE_PCT, TOUCH_BELOW_PCT, MIN_PRICE,
)

SPLIT_DATE = date(2023, 1, 1)
FLAT_CONFIG = {"l1_amount": 500_000, "levels": [], "sl_floor_after": 999}


def enumerate_confirmed_touches(daily):
    """Yield confirmed touch events: (touch_month_row, st_val, next_open, next_date)."""
    monthly = aggregate_to_monthly(daily)
    if len(monthly) < ST_PERIOD + 3:
        return []
    events = []
    # For each candidate touch month i, ST is computed from months[:i] (completed months before)
    for i in range(ST_PERIOD + 2, len(monthly) - 1):  # need next month to exist for entry
        prior = monthly[:i]
        st_data = compute_supertrend(prior, ST_PERIOD, ST_MULTIPLIER)
        if not st_data:
            continue
        st_val = st_data[-1]["supertrend"]
        st_dir = st_data[-1]["direction"]
        if st_dir != "UP":
            continue
        m = monthly[i]
        low_gap = (m["low"] - st_val) / st_val * 100
        close_gap = (m["close"] - st_val) / st_val * 100
        is_touch = (
            (TOUCH_ABOVE_PCT >= low_gap >= -TOUCH_BELOW_PCT)
            or (TOUCH_ABOVE_PCT >= close_gap >= -TOUCH_BELOW_PCT and m["low"] <= st_val * 1.05)
            or (m["low"] <= st_val * 1.01)
        )
        if not is_touch:
            continue
        if m["close"] <= st_val:          # confirmation: close above ST
            continue
        if m["close"] < MIN_PRICE:        # penny filter
            continue
        nxt = monthly[i + 1]
        events.append({
            "touch_month": (m["date"].year, m["date"].month),
            "st": st_val,
            "touch_low": m["low"],
            "entry_date": nxt["date"],
            "entry_price": nxt["open"],
        })
    return events


def main():
    syms = sorted(p.stem for p in CACHE_DIR.glob("*.json")
                  if not p.stem.startswith("_") and not should_skip(p.stem))
    print(f"Universe: {len(syms)} symbols from cache")

    per_stock = defaultdict(lambda: {"A": [], "B": []})
    n_events = 0
    for k, sym in enumerate(syms):
        daily = load_daily(sym)
        if not daily or len(daily) < 300:
            continue
        events = enumerate_confirmed_touches(daily)
        open_until = None
        for ev in events:
            if open_until and ev["entry_date"] <= open_until:
                continue  # previous trade still open — no overlapping entries
            tr = simulate_trade(daily, ev["entry_date"], ev["entry_price"],
                                ev["touch_low"], FLAT_CONFIG)
            if not tr:
                continue
            open_until = date.fromisoformat(tr["exit_date"])
            n_events += 1
            period = "A" if ev["entry_date"] < SPLIT_DATE else "B"
            per_stock[sym][period].append({
                "entry": tr["entry_date"], "exit": tr["exit_date"],
                "pct": tr["flat_pnl_pct"], "pnl": tr["flat_pnl"],
                "status": tr["status"], "days": tr["holding_days"],
            })
        if (k + 1) % 150 == 0:
            print(f"  ... {k+1}/{len(syms)} scanned, {n_events} trades so far")

    print(f"\nTotal simulated trades: {n_events}")

    # ---------- Per-stock A record vs B performance ----------
    rows = []
    for sym, d in per_stock.items():
        a, b = d["A"], d["B"]
        if not a or not b:
            continue
        a_win = sum(1 for t in a if t["pct"] > 0) / len(a)
        a_avg = sum(t["pct"] for t in a) / len(a)
        b_win = sum(1 for t in b if t["pct"] > 0) / len(b)
        b_avg = sum(t["pct"] for t in b) / len(b)
        rows.append({"sym": sym, "nA": len(a), "winA": a_win, "avgA": a_avg,
                     "nB": len(b), "winB": b_win, "avgB": b_avg})
    print(f"Stocks with trades in BOTH periods: {len(rows)}")

    # ---------- Bucket test ----------
    # GOOD record: A win rate >= 50% AND A avg > 0. BAD: everything else.
    good = [r for r in rows if r["winA"] >= 0.5 and r["avgA"] > 0]
    bad = [r for r in rows if not (r["winA"] >= 0.5 and r["avgA"] > 0)]

    def agg(group):
        n_tr = sum(r["nB"] for r in group)
        if not group or n_tr == 0:
            return (0, 0.0, 0.0)
        wins = sum(r["winB"] * r["nB"] for r in group)
        avg = sum(r["avgB"] * r["nB"] for r in group) / n_tr
        return (n_tr, wins / n_tr * 100, avg)

    gn, gw, ga = agg(good)
    bn, bw, ba = agg(bad)
    print("\n=== BUCKET TEST: Period-A record → Period-B outcome ===")
    print(f"{'Bucket':<22} {'Stocks':>7} {'B trades':>9} {'B win%':>8} {'B avg%':>8}")
    print(f"{'GOOD A-record':<22} {len(good):>7} {gn:>9} {gw:>7.1f} {ga:>+7.2f}")
    print(f"{'BAD A-record':<22} {len(bad):>7} {bn:>9} {bw:>7.1f} {ba:>+7.2f}")

    # ---------- Quartile test on A avg return ----------
    rows_sorted = sorted(rows, key=lambda r: r["avgA"])
    quarts = [rows_sorted[i * len(rows_sorted) // 4:(i + 1) * len(rows_sorted) // 4]
              for i in range(4)]
    print("\n=== QUARTILES by Period-A avg return ===")
    print(f"{'Quartile':<14} {'A avg%':>8} {'B trades':>9} {'B win%':>8} {'B avg%':>8}")
    for qi, q in enumerate(quarts):
        if not q:
            continue
        a_avg = sum(r["avgA"] for r in q) / len(q)
        n, w, avg = agg(q)
        print(f"Q{qi+1:<13} {a_avg:>+7.1f} {n:>9} {w:>7.1f} {avg:>+7.2f}")

    # ---------- Spearman rank correlation A avg vs B avg ----------
    def rank(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        rk = [0.0] * len(vals)
        for pos, idx in enumerate(order):
            rk[idx] = pos
        return rk

    xa = rank([r["avgA"] for r in rows])
    xb = rank([r["avgB"] for r in rows])
    n = len(rows)
    if n > 2:
        d2 = sum((xa[i] - xb[i]) ** 2 for i in range(n))
        rho = 1 - 6 * d2 / (n * (n * n - 1))
        print(f"\nSpearman rank correlation (A avg% vs B avg%): rho = {rho:+.3f}  (n={n})")

    # ---------- Portfolio comparison on Period B ----------
    all_b = [t for d in per_stock.values() for t in d["B"]]
    # 'filtered universe' = B trades only from GOOD-record stocks
    good_syms = {r["sym"] for r in good}
    filt_b = [t for sym, d in per_stock.items() if sym in good_syms for t in d["B"]]

    def port(trades, label):
        if not trades:
            print(f"{label:<28} no trades")
            return
        n = len(trades)
        w = sum(1 for t in trades if t["pct"] > 0) / n * 100
        avg = sum(t["pct"] for t in trades) / n
        tot = sum(t["pnl"] for t in trades)
        big = sorted(trades, key=lambda t: -t["pct"])[:5]
        top5 = ", ".join(f"{t['pct']:+.0f}%" for t in big)
        print(f"{label:<28} {n:>6} {w:>7.1f} {avg:>+7.2f} {tot/100000:>+9.1f}L  top5: {top5}")

    print("\n=== PERIOD-B PORTFOLIO (flat 5L per trade, no cap) ===")
    print(f"{'Strategy':<28} {'N':>6} {'Win%':>7} {'Avg%':>8} {'Total P&L':>10}")
    port(all_b, "ALL stocks (no filter)")
    port(filt_b, "GOOD-A-record universe")

    # save raw results
    out = Path(__file__).parent / "universe_persistence_results.json"
    with open(out, "w") as f:
        json.dump({"rows": rows,
                   "all_B": {"n": len(all_b),
                             "total_pnl": sum(t["pnl"] for t in all_b)},
                   "filtered_B": {"n": len(filt_b),
                                  "total_pnl": sum(t["pnl"] for t in filt_b)}},
                  f, indent=1, default=str)
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
