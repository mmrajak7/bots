"""
3 Cr No-F&O Portfolio Monte Carlo (task 3 from 20/ discussion)
===============================================================
Simulates the blended cash-market portfolio from 20/01 at Rs 3 Cr over
10 years, 10,000 paths, yearly steps.

Market engine: bootstrap ACTUAL joint yearly returns (NIFTY50, ALPHA50,
MIDCAP150) from Kite index data 2006-2025 — preserves cross-sleeve
correlation and fat tails instead of assuming normal distributions.

Sleeves (of 3 Cr):
  40%  Active stock-swing engine  — conditional on NIFTY year (regime pauses
                                    in bears; power-law right tail in bulls).
                                    Anchored to validated backtest numbers:
                                    normal years ~8-12%, 6yr OOS 17.3%.
  20%  Momentum factor (ALPHA50 buy-hold + dividends)
  15%  REITs/InvITs               — 6.75% yield + modest price move
  15%  Broad ETF dip-buy (NIFTY + small timing alpha + dividends)
  10%  Parking (Kotak Arb 5.5%)

Run from Helper/:  python 20/sim_3cr.py
"""
import sys
import json
import random
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playbook.backtest_v4_1 import aggregate_to_monthly  # noqa (path check)
from datetime import datetime

HERE = Path(__file__).resolve().parent
CACHE = HERE / "index_cache"

random.seed(42)

N_SIMS = 10_000
YEARS = 10
CAPITAL = 3_00_00_000  # 3 Cr

ALLOC = {
    "active": 0.40,
    "momentum": 0.20,
    "reit": 0.15,
    "etf_dip": 0.15,
    "parking": 0.10,
}

DIV_YIELD = {"NIFTY50": 0.013, "ALPHA50": 0.008, "MIDCAP150": 0.010}
PARKING = 0.055
EFFECTIVE_TAX = 0.10  # blended LTCG/STCG drag applied to final gains (approx)


def yearly_returns(name):
    data = json.load(open(CACHE / f"{name}.json"))
    closes = {}
    for c in data:
        d = c["date"]
        dt = datetime.fromisoformat(d.replace("+05:30", "")) if isinstance(d, str) else d
        closes[dt.year] = float(c["close"])  # last close of each year wins
    years = sorted(closes)
    rets = {}
    for a, b in zip(years, years[1:]):
        rets[b] = closes[b] / closes[a] - 1
    return rets


def build_joint_years():
    n50 = yearly_returns("NIFTY50")
    a50 = yearly_returns("ALPHA50")
    m150 = yearly_returns("MIDCAP150")
    common = sorted(set(n50) & set(a50) & set(m150))
    # drop partial current year
    common = [y for y in common if y < 2026]
    joint = [{"year": y,
              "nifty": n50[y] + DIV_YIELD["NIFTY50"],
              "alpha": a50[y] + DIV_YIELD["ALPHA50"],
              "midcap": m150[y] + DIV_YIELD["MIDCAP150"]} for y in common]
    return joint


def active_sleeve_return(nifty_ret, rng):
    """Active swing engine, conditional on market year.

    Anchors (validated backtests):
      - Deep bear: regime filter pauses -> mostly parking, small SL bleed.
      - Normal/choppy year: 8.6% strategy CAGR + parking on idle -> ~10-12%.
      - Bull year: OOS 25.7% (2020-24 confirmation-filter number).
      - Monster recovery (2020-like): fat right tail, power-law winners.
    """
    if nifty_ret < -0.10:                       # deep bear: paused most of year
        return rng.gauss(0.03, 0.04)
    if nifty_ret < 0.05:                        # flat/choppy
        return rng.gauss(0.10, 0.09)
    base = rng.gauss(0.22, 0.12)                # bull
    if rng.random() < 0.20:                     # power-law kicker (monster trades)
        base += rng.uniform(0.10, 0.35)
    return base


def reit_sleeve_return(rng):
    # 6.75% distribution yield + price drift 3% +/- 7% (rate-cycle swings)
    return 0.0675 + rng.gauss(0.03, 0.07)


def simulate(joint, alloc, years=YEARS, n_sims=N_SIMS):
    finals, cagrs = [], []
    yearly_matrix = []
    for s in range(n_sims):
        rng = random.Random(1000 + s)
        sleeves = {k: CAPITAL * v for k, v in alloc.items()}
        path = []
        for y in range(years):
            j = rng.choice(joint)
            r_active = active_sleeve_return(j["nifty"], rng)
            r_mom = j["alpha"]
            r_reit = reit_sleeve_return(rng)
            r_etf = j["nifty"] + rng.gauss(0.01, 0.02)   # dip-buy timing alpha
            r_park = PARKING
            before = sum(sleeves.values())
            sleeves["active"] *= (1 + r_active)
            sleeves["momentum"] *= (1 + r_mom)
            sleeves["reit"] *= (1 + r_reit)
            sleeves["etf_dip"] *= (1 + r_etf)
            sleeves["parking"] *= (1 + r_park)
            total = sum(sleeves.values())
            path.append(total / before - 1)
            # annual rebalance back to target weights
            sleeves = {k: total * v for k, v in alloc.items()}
        finals.append(total)
        cagrs.append((total / CAPITAL) ** (1 / years) - 1)
        yearly_matrix.append(path)
    return finals, cagrs, yearly_matrix


def pct(vals, p):
    vals = sorted(vals)
    return vals[int(p / 100 * (len(vals) - 1))]


def report(label, finals, cagrs):
    med = pct(finals, 50)
    print(f"\n--- {label} ---")
    print(f"  Median corpus (10y): Rs {med/1e7:.2f} Cr   "
          f"P10: {pct(finals,10)/1e7:.2f} Cr   P90: {pct(finals,90)/1e7:.2f} Cr")
    print(f"  CAGR  median: {pct(cagrs,50)*100:.1f}%   "
          f"P10: {pct(cagrs,10)*100:.1f}%   P90: {pct(cagrs,90)*100:.1f}%")
    for target in (0.15, 0.20):
        p = sum(1 for c in cagrs if c >= target) / len(cagrs) * 100
        print(f"  P(CAGR >= {target*100:.0f}%): {p:.0f}%")
    # post-tax view (drag on gains at exit)
    post = [(CAPITAL + (f - CAPITAL) * (1 - EFFECTIVE_TAX)) for f in finals]
    post_cagr = [(f / CAPITAL) ** (1 / YEARS) - 1 for f in post]
    print(f"  Post-tax (~{EFFECTIVE_TAX*100:.0f}% drag) median CAGR: "
          f"{pct(post_cagr,50)*100:.1f}%")


def main():
    joint = build_joint_years()
    print(f"Bootstrap pool: {len(joint)} actual years "
          f"({joint[0]['year']}-{joint[-1]['year']})")
    avg_n = sum(j["nifty"] for j in joint) / len(joint)
    avg_a = sum(j["alpha"] for j in joint) / len(joint)
    print(f"  NIFTY mean yearly (total ret): {avg_n*100:+.1f}%   "
          f"ALPHA50: {avg_a*100:+.1f}%")

    # Main blend
    finals, cagrs, ym = simulate(joint, ALLOC)
    report("MAIN BLEND (40 Active / 20 Momentum / 15 REIT / 15 ETF / 10 Park)",
           finals, cagrs)

    # Worst single-year across paths (portfolio level)
    worst = [min(p) for p in ym]
    print(f"  Worst single year, median path: {pct(worst,50)*100:.1f}%   "
          f"P10 (bad luck): {pct(worst,10)*100:.1f}%")

    # Variant 1: NO active sleeve — "slow buckets only"
    alloc_slow = {"active": 0.0, "momentum": 0.30, "reit": 0.25,
                  "etf_dip": 0.30, "parking": 0.15}
    f2, c2, _ = simulate(joint, alloc_slow)
    report("SLOW-ONLY (no stock engine: 30 Mom / 25 REIT / 30 ETF / 15 Park)",
           f2, c2)

    # Variant 2: All NIFTY benchmark
    alloc_nifty = {"active": 0.0, "momentum": 0.0, "reit": 0.0,
                   "etf_dip": 1.0, "parking": 0.0}
    f3, c3, _ = simulate(joint, alloc_nifty)
    report("BENCHMARK (100% NIFTY buy-hold + dividends)", f3, c3)

    # Variant 3: Aggressive — 50 Active / 30 Momentum / 10 REIT / 10 ETF
    alloc_agg = {"active": 0.50, "momentum": 0.30, "reit": 0.10,
                 "etf_dip": 0.10, "parking": 0.0}
    f4, c4, _ = simulate(joint, alloc_agg)
    report("AGGRESSIVE (50 Active / 30 Momentum / 10 REIT / 10 ETF / 0 Park)",
           f4, c4)

    out = HERE / "sim_3cr_results.json"
    with open(out, "w") as f:
        json.dump({
            "main": {"median_cagr": pct(cagrs, 50), "p10": pct(cagrs, 10),
                     "p90": pct(cagrs, 90),
                     "p_ge_20": sum(1 for c in cagrs if c >= 0.20) / len(cagrs)},
            "slow_only": {"median_cagr": pct(c2, 50)},
            "nifty_bh": {"median_cagr": pct(c3, 50)},
            "aggressive": {"median_cagr": pct(c4, 50), "p10": pct(c4, 10),
                           "p_ge_20": sum(1 for c in c4 if c >= 0.20) / len(c4)},
        }, f, indent=1)
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
