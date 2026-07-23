"""
Momentum ETF + Monthly-ST Exit Backtest (avenue C from 20/01)
==============================================================
Question: does holding a momentum index (MOM30 / ALPHA 50) with the Tactical
rule — long while Monthly ST(10,3) is UP, exit to parking (5.5%) when a
completed month closes below ST — beat buy-and-hold, and what CAGR/DD does
the sleeve realistically deliver?

Data: NSE index series fetched from Kite (as far back as Kite has them),
cached to 20/index_cache/. Signals on COMPLETED monthly candles only;
entries/exits execute at the NEXT month's first trading day open.

Run from Helper/:  python 20/momentum_etf_backtest.py [--fetch]
"""
import sys
import json
import time
import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playbook.backtest_v4_1 import aggregate_to_monthly, compute_supertrend

HERE = Path(__file__).resolve().parent
CACHE = HERE / "index_cache"
TOKEN_FILE = Path(r"C:\Users\mail2\Documents\Projects\BOTS\data\kite_access_token.json")

# Kite index tradingsymbols (segment INDICES, exchange NSE)
INDICES = {
    "NIFTY200_MOM30": "NIFTY200 MOMENTM30",
    "ALPHA50": "NIFTY ALPHA 50",
    "MIDCAP150_MOM50": "NIFTY MIDCAP150 MOMENTUM 50",
    "NIFTY50": "NIFTY 50",
    "MIDCAP150": "NIFTY MIDCAP 150",
}
PARKING_RATE = 0.055
ST_PERIOD, ST_MULT = 10, 3


def fetch_all():
    from kiteconnect import KiteConnect
    tok = json.load(open(TOKEN_FILE))
    kite = KiteConnect(api_key=tok["api_key"])
    kite.set_access_token(tok["access_token"])

    print("Loading NSE instruments to find index tokens...")
    instruments = kite.instruments("NSE")
    tokens = {}
    for ins in instruments:
        if ins.get("segment") == "INDICES" and ins["tradingsymbol"] in INDICES.values():
            tokens[ins["tradingsymbol"]] = ins["instrument_token"]
    print(f"Found tokens: {tokens}")

    CACHE.mkdir(exist_ok=True)
    for name, ts in INDICES.items():
        if ts not in tokens:
            print(f"  !! {name} ({ts}) not found in instruments — skipped")
            continue
        token = tokens[ts]
        all_candles = []
        # fetch in 2000-day chunks from 2005 forward; Kite returns empty before inception
        start = date(2005, 1, 1)
        today = date.today()
        while start < today:
            end = min(start + timedelta(days=1999), today)
            try:
                chunk = kite.historical_data(token, start.isoformat(), end.isoformat(), "day")
                all_candles.extend(chunk)
            except Exception as e:
                print(f"  {name} chunk {start}..{end}: {e}")
            start = end + timedelta(days=1)
            time.sleep(0.4)
        if all_candles:
            out = CACHE / f"{name}.json"
            with open(out, "w") as f:
                json.dump(all_candles, f, default=str)
            print(f"  {name}: {len(all_candles)} candles "
                  f"({all_candles[0]['date']} .. {all_candles[-1]['date']})")
        else:
            print(f"  {name}: NO DATA")


def load_index(name):
    path = CACHE / f"{name}.json"
    if not path.exists():
        return None
    data = json.load(open(path))
    parsed = []
    for c in data:
        d = c["date"]
        if isinstance(d, str):
            d = datetime.fromisoformat(d.replace("+05:30", "")).date()
        parsed.append({"date": d, "open": float(c["open"]), "high": float(c["high"]),
                       "low": float(c["low"]), "close": float(c["close"])})
    parsed.sort(key=lambda x: x["date"])
    return parsed


def run_strategy(daily, label):
    """Long while Monthly ST UP, parking while DOWN. Signals on completed months,
    execution at next month's first open. Returns dict of results."""
    monthly = aggregate_to_monthly(daily)
    if len(monthly) < ST_PERIOD + 3:
        return None
    st = compute_supertrend(monthly, ST_PERIOD, ST_MULT)
    st_by_idx = {i: s for i, s in
                 zip(range(ST_PERIOD - 1, len(monthly)), st)}

    # walk months: decision after month i closes -> position for month i+1
    start_i = ST_PERIOD  # first month with a prior completed ST
    equity = 1.0
    bh_equity = 1.0
    in_market = st_by_idx[start_i - 1]["direction"] == "UP"
    n_switches = 0
    peak = 1.0
    max_dd = 0.0
    bh_peak, bh_dd = 1.0, 0.0
    yearly = defaultdict(lambda: [1.0, 1.0])  # year -> [strat_factor, bh_factor]
    months_in = 0
    total_months = 0

    for i in range(start_i, len(monthly)):
        m = monthly[i]
        ret = (m["close"] - m["open"]) / m["open"] if i == start_i else \
              (m["close"] - monthly[i - 1]["close"]) / monthly[i - 1]["close"]
        # buy-hold
        bh_equity *= (1 + ret)
        bh_peak = max(bh_peak, bh_equity)
        bh_dd = max(bh_dd, 1 - bh_equity / bh_peak)
        # strategy
        if in_market:
            equity *= (1 + ret)
            months_in += 1
        else:
            equity *= (1 + PARKING_RATE / 12)
        total_months += 1
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak)
        y = m["date"].year
        yearly[y][0] *= (1 + (ret if in_market else PARKING_RATE / 12))
        yearly[y][1] *= (1 + ret)
        # decision for next month from THIS completed month's ST
        prev_dir = st_by_idx[i]["direction"] if i in st_by_idx else None
        if prev_dir is not None:
            want_in = prev_dir == "UP"
            if want_in != in_market:
                n_switches += 1
            in_market = want_in

    years = total_months / 12
    cagr = equity ** (1 / years) - 1 if years > 0 else 0
    bh_cagr = bh_equity ** (1 / years) - 1 if years > 0 else 0
    first = monthly[start_i]["date"]
    return {
        "label": label, "start": first.isoformat(), "years": round(years, 1),
        "strat_cagr": cagr * 100, "bh_cagr": bh_cagr * 100,
        "strat_dd": max_dd * 100, "bh_dd": bh_dd * 100,
        "switches": n_switches, "pct_invested": months_in / total_months * 100,
        "yearly": {y: ((v[0] - 1) * 100, (v[1] - 1) * 100) for y, v in sorted(yearly.items())},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    args = ap.parse_args()
    if args.fetch or not CACHE.exists():
        fetch_all()

    results = []
    for name in INDICES:
        daily = load_index(name)
        if not daily:
            print(f"{name}: no data cached")
            continue
        r = run_strategy(daily, name)
        if r:
            results.append(r)

    print("\n=== MONTHLY-ST(10,3) EXIT vs BUY-HOLD (price indices, parking 5.5%) ===")
    print(f"{'Index':<20} {'From':<11} {'Yrs':>4} {'ST CAGR':>8} {'BH CAGR':>8} "
          f"{'ST MaxDD':>9} {'BH MaxDD':>9} {'Switches':>9} {'%In':>5}")
    for r in results:
        print(f"{r['label']:<20} {r['start']:<11} {r['years']:>4} "
              f"{r['strat_cagr']:>7.1f}% {r['bh_cagr']:>7.1f}% "
              f"{r['strat_dd']:>8.1f}% {r['bh_dd']:>8.1f}% "
              f"{r['switches']:>9} {r['pct_invested']:>4.0f}%")

    print("\n=== YEAR-BY-YEAR (strategy / buy-hold) ===")
    for r in results:
        print(f"\n{r['label']}:")
        for y, (s, b) in r["yearly"].items():
            flag = "  <- ST out-performed" if s > b + 1 else ""
            print(f"  {y}: {s:>+6.1f}% / {b:>+6.1f}%{flag}")

    out = HERE / "momentum_etf_results.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=1, default=str)
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
