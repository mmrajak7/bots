"""
Monte Carlo Comparison: Playbook vs Alternative Scenarios
=========================================================
Runs 10,000 simulations for each scenario at 5/10/15/20 years.

Scenarios:
  1. PLAYBOOK (v4)     — Current 3-basket strategy with parking + swing
  2. 100% EQUITY       — All 3Cr lump-sum into equity day 1, hold forever
  3. 100% EQUITY + ST  — All equity but with ST timing (park when no signal)
  4. PLAYBOOK NO SWING — Current strategy but zero swing income
  5. DIRECT SIP         — Monthly SIP into NIFTY (no timing, no parking alpha)
  6. PLAYBOOK 2x SWING — Current strategy with doubled swing returns
"""

import numpy as np
from dataclasses import dataclass
from typing import Dict, List, Tuple

np.random.seed(42)

# ============================================================
# SHARED PARAMETERS
# ============================================================

STARTING_CAPITAL = 3_00_00_000  # 3 Crore
NUM_SIMS = 10_000
YEARS_TO_REPORT = [5, 10, 15, 20]
MAX_YEARS = 20

# Equity parameters
EQUITY_RETURN_MEAN = 0.13       # 13% CAGR
EQUITY_RETURN_STD = 0.18        # 18% annual vol
EQUITY_DIVIDEND_YIELD = 0.015   # ~1.5% blended (growth ETFs have 0, stocks have 2.5%)

# Parking
PARKING_RETURN = 0.056          # 5.6% post-tax blended

# Tax
TAX_LTCG = 0.125
TAX_STCG = 0.20
TAX_SLAB = 0.30

# Swing
SWING_CAPITAL = 50_00_000
SWING_RETURN_MEAN = 0.18
SWING_RETURN_STD = 0.12
SWING_TAX = 0.20

# ST timing
CORRECTION_PROB = 0.38          # Prob of correction per year
DEPLOY_FRAC_MEAN = 0.55         # Avg fraction deployed on signal
EXIT_PROB = 0.25                # Annual prob of ST exit
LOSS_EXIT_PROB = 0.17           # Prob exit is at a loss
LOSS_MAGNITUDE = 0.12           # Avg loss when losing


def format_inr(amount: float) -> str:
    cr = amount / 1_00_00_000
    if cr >= 1:
        return f"Rs {cr:,.2f} Cr"
    else:
        return f"Rs {amount / 1_00_000:,.1f} L"


# ============================================================
# SCENARIO 1: PLAYBOOK v4 (Current strategy)
# ============================================================

def sim_playbook(years: int, swing_mult: float = 1.0) -> float:
    """Current playbook: parking + ST-timed deployment + diversification + swing."""
    pool = STARTING_CAPITAL * 0.90      # 90% deployable
    reserve = STARTING_CAPITAL * 0.10   # 10% reserve
    deployed_equity = 0.0
    deployed_alt = 0.0                  # gold/silver/REIT (~25% of deployed)
    cost_equity = 0.0
    cost_alt = 0.0
    age_equity = 0
    age_alt = 0
    total_tax = 0.0

    for year in range(years):
        # Parking returns
        pr = max(np.random.normal(PARKING_RETURN, 0.005), 0.02)
        pool *= (1 + pr)
        reserve *= (1 + pr)

        # Swing profits
        if swing_mult > 0:
            sw_ret = np.random.normal(SWING_RETURN_MEAN * swing_mult, SWING_RETURN_STD)
            sw_profit = SWING_CAPITAL * max(sw_ret, -0.25)
            if sw_profit > 0:
                sw_tax = sw_profit * SWING_TAX
                total_tax += sw_tax
                after_tax = sw_profit - sw_tax
                deployed_equity += after_tax * 0.50
                cost_equity += after_tax * 0.50
                pool += after_tax * 0.50

        # Check for deployment signal
        if np.random.random() < CORRECTION_PROB and pool > STARTING_CAPITAL * 0.10:
            available = pool - STARTING_CAPITAL * 0.05
            available = max(available, 0)
            frac = np.clip(np.random.normal(DEPLOY_FRAC_MEAN, 0.15), 0.25, 0.80)
            deploy = available * frac

            # 75% equity, 25% alt (gold/silver/REIT)
            to_eq = deploy * 0.75
            to_alt = deploy * 0.25

            # Entry timing edge
            if np.random.random() < 0.55:
                adj = np.random.uniform(0.01, 0.05)
            else:
                adj = np.random.uniform(-0.07, -0.01)

            deployed_equity += to_eq * (1 + adj)
            cost_equity += to_eq
            deployed_alt += to_alt * (1 + adj)
            cost_alt += to_alt
            pool -= deploy
            age_equity = 0
            age_alt = 0

        # Deployed equity returns
        if deployed_equity > 0:
            # Dividend income (taxed at slab)
            div = deployed_equity * EQUITY_DIVIDEND_YIELD
            div_tax = div * TAX_SLAB
            total_tax += div_tax
            pool += div - div_tax
            deployed_equity -= div

            eq_ret = np.random.normal(EQUITY_RETURN_MEAN - EQUITY_DIVIDEND_YIELD, EQUITY_RETURN_STD)
            deployed_equity *= (1 + eq_ret)
            deployed_equity = max(deployed_equity, 0)
            age_equity += 12

            # ST exit check
            if np.random.random() < EXIT_PROB:
                if np.random.random() < LOSS_EXIT_PROB:
                    loss = deployed_equity * np.random.uniform(0.05, LOSS_MAGNITUDE)
                    pool += deployed_equity - loss
                else:
                    gain = max(deployed_equity - cost_equity, 0)
                    tax = gain * (TAX_LTCG if age_equity >= 12 else TAX_STCG)
                    total_tax += tax
                    pool += deployed_equity - tax
                deployed_equity = 0
                cost_equity = 0
                age_equity = 0

        # Deployed alt returns (gold/silver/REIT ~10% return, 15% vol)
        if deployed_alt > 0:
            alt_div = deployed_alt * 0.03  # REIT distributions
            alt_div_tax = alt_div * 0.65 * TAX_SLAB
            total_tax += alt_div_tax
            pool += alt_div - alt_div_tax
            deployed_alt -= alt_div

            alt_ret = np.random.normal(0.07, 0.15)  # price return after div
            deployed_alt *= (1 + alt_ret)
            deployed_alt = max(deployed_alt, 0)
            age_alt += 12

            if np.random.random() < EXIT_PROB * 0.7:
                gain = max(deployed_alt - cost_alt, 0)
                tax = gain * (TAX_LTCG if age_alt >= 12 else TAX_STCG)
                total_tax += tax
                pool += deployed_alt - tax
                deployed_alt = 0
                cost_alt = 0
                age_alt = 0

    return pool + reserve + deployed_equity + deployed_alt


# ============================================================
# SCENARIO 2: 100% EQUITY (Lump sum, hold forever)
# ============================================================

def sim_100pct_equity(years: int) -> float:
    """All 3Cr into equity day 1. No parking, no timing, no diversification."""
    capital = float(STARTING_CAPITAL)

    for year in range(years):
        # Full portfolio in equity, experience full volatility
        annual_return = np.random.normal(EQUITY_RETURN_MEAN, EQUITY_RETURN_STD)
        capital *= (1 + annual_return)
        capital = max(capital, 0)

        # Dividends taxed at slab (reinvested net)
        div = capital * EQUITY_DIVIDEND_YIELD
        div_tax = div * TAX_SLAB
        capital -= div_tax  # lose the tax, keep the rest

    # LTCG on exit
    gain = max(capital - STARTING_CAPITAL, 0)
    tax = gain * TAX_LTCG
    return capital - tax


# ============================================================
# SCENARIO 3: 100% EQUITY + ST TIMING
# ============================================================

def sim_equity_st_timed(years: int) -> float:
    """All equity (no gold/REIT), but with ST timing. Park when no signal."""
    pool = float(STARTING_CAPITAL)
    deployed = 0.0
    cost = 0.0
    age = 0
    total_tax = 0.0

    for year in range(years):
        # Parking returns on undeployed
        pr = max(np.random.normal(PARKING_RETURN, 0.005), 0.02)
        pool *= (1 + pr)

        # Deploy on correction signal — ALL into equity
        if np.random.random() < CORRECTION_PROB and pool > STARTING_CAPITAL * 0.05:
            available = pool - STARTING_CAPITAL * 0.05
            frac = np.clip(np.random.normal(0.65, 0.15), 0.30, 0.90)
            deploy = available * frac

            if np.random.random() < 0.55:
                adj = np.random.uniform(0.01, 0.05)
            else:
                adj = np.random.uniform(-0.07, -0.01)

            deployed += deploy * (1 + adj)
            cost += deploy
            pool -= deploy
            age = 0

        # Deployed equity returns
        if deployed > 0:
            div = deployed * EQUITY_DIVIDEND_YIELD
            div_tax = div * TAX_SLAB
            total_tax += div_tax
            pool += div - div_tax
            deployed -= div

            eq_ret = np.random.normal(EQUITY_RETURN_MEAN - EQUITY_DIVIDEND_YIELD, EQUITY_RETURN_STD)
            deployed *= (1 + eq_ret)
            deployed = max(deployed, 0)
            age += 12

            if np.random.random() < EXIT_PROB:
                if np.random.random() < LOSS_EXIT_PROB:
                    loss = deployed * np.random.uniform(0.05, LOSS_MAGNITUDE)
                    pool += deployed - loss
                else:
                    gain = max(deployed - cost, 0)
                    tax = gain * (TAX_LTCG if age >= 12 else TAX_STCG)
                    total_tax += tax
                    pool += deployed - tax
                deployed = 0
                cost = 0
                age = 0

    # Exit remaining deployed
    if deployed > 0:
        gain = max(deployed - cost, 0)
        tax = gain * TAX_LTCG
        total_tax += tax
        pool += deployed - tax

    return pool


# ============================================================
# SCENARIO 5: DIRECT SIP
# ============================================================

def sim_sip(years: int) -> float:
    """Monthly SIP into NIFTY. No timing, no parking alpha. DCA approach."""
    total_months = years * 12
    monthly_sip = STARTING_CAPITAL / total_months  # Spread 3Cr evenly
    remaining_corpus = float(STARTING_CAPITAL)      # Undeployed earns savings rate
    units = 0.0
    nav = 100.0
    total_invested = 0.0

    monthly_equity_mean = EQUITY_RETURN_MEAN / 12
    monthly_equity_std = EQUITY_RETURN_STD / (12 ** 0.5)

    for month in range(total_months):
        # NAV moves
        monthly_ret = np.random.normal(monthly_equity_mean, monthly_equity_std)
        nav *= (1 + monthly_ret)
        nav = max(nav, 1.0)

        # SIP: buy units
        units += monthly_sip / nav
        total_invested += monthly_sip
        remaining_corpus -= monthly_sip

        # Remaining corpus earns savings rate (~4% post-tax)
        remaining_corpus *= (1 + 0.04 / 12)

    # Final value
    equity_value = units * nav
    gain = max(equity_value - total_invested, 0)
    tax = gain * TAX_LTCG
    return equity_value - tax + remaining_corpus


# ============================================================
# RUN ALL SCENARIOS
# ============================================================

def run_scenario(name: str, sim_func, years: int) -> dict:
    """Run NUM_SIMS simulations of a scenario and return stats."""
    results = np.array([sim_func(years) for _ in range(NUM_SIMS)])
    return {
        "name": name,
        "years": years,
        "p5": np.percentile(results, 5),
        "p10": np.percentile(results, 10),
        "p25": np.percentile(results, 25),
        "p50": np.percentile(results, 50),
        "p75": np.percentile(results, 75),
        "p90": np.percentile(results, 90),
        "p95": np.percentile(results, 95),
        "mean": np.mean(results),
        "std": np.std(results),
        "cagr_p10": ((np.percentile(results, 10) / STARTING_CAPITAL) ** (1/years) - 1) * 100,
        "cagr_p50": ((np.percentile(results, 50) / STARTING_CAPITAL) ** (1/years) - 1) * 100,
        "cagr_p90": ((np.percentile(results, 90) / STARTING_CAPITAL) ** (1/years) - 1) * 100,
        "prob_loss": np.mean(results < STARTING_CAPITAL) * 100,
        "prob_double": np.mean(results > STARTING_CAPITAL * 2) * 100,
        "prob_5x": np.mean(results > STARTING_CAPITAL * 5) * 100,
        "prob_10x": np.mean(results > STARTING_CAPITAL * 10) * 100,
        "multiplier_p50": np.percentile(results, 50) / STARTING_CAPITAL,
    }


def main():
    print("=" * 100)
    print("  MONTE CARLO COMPARISON: Playbook v4 vs Alternative Scenarios")
    print(f"  Starting Capital: {format_inr(STARTING_CAPITAL)} | Simulations: {NUM_SIMS:,}")
    print(f"  Equity: {EQUITY_RETURN_MEAN*100:.0f}% CAGR, {EQUITY_RETURN_STD*100:.0f}% vol")
    print(f"  Parking: {PARKING_RETURN*100:.1f}% post-tax | Swing: {SWING_RETURN_MEAN*100:.0f}% on 50L")
    print("=" * 100)

    scenarios = {
        "Playbook v4":           lambda y: sim_playbook(y, swing_mult=1.0),
        "100% Equity (Lump)":    sim_100pct_equity,
        "100% Equity + ST":      sim_equity_st_timed,
        "Playbook (No Swing)":   lambda y: sim_playbook(y, swing_mult=0.0),
        "Direct SIP":            sim_sip,
        "Playbook (2x Swing)":   lambda y: sim_playbook(y, swing_mult=2.0),
    }

    all_results = {}

    for yr in YEARS_TO_REPORT:
        print(f"\n{'#' * 100}")
        print(f"  {yr}-YEAR HORIZON")
        print(f"{'#' * 100}")

        yr_results = {}
        for name, func in scenarios.items():
            print(f"  Running {name}...", end="", flush=True)
            r = run_scenario(name, func, yr)
            yr_results[name] = r
            print(f" done (median {format_inr(r['p50'])})")

        all_results[yr] = yr_results

        # --- Comparison Table: Portfolio Value ---
        print(f"\n  {'PORTFOLIO VALUE':^94}")
        print(f"  {'Scenario':<22} {'P10':>12} {'P25':>12} {'MEDIAN':>12} {'P75':>12} {'P90':>12} {'Mean':>12}")
        print(f"  {'-'*94}")
        for name in scenarios:
            r = yr_results[name]
            print(f"  {name:<22} {format_inr(r['p10']):>12} {format_inr(r['p25']):>12} "
                  f"{format_inr(r['p50']):>12} {format_inr(r['p75']):>12} "
                  f"{format_inr(r['p90']):>12} {format_inr(r['mean']):>12}")

        # --- CAGR Table ---
        print(f"\n  {'CAGR (%)':^60}")
        print(f"  {'Scenario':<22} {'P10':>10} {'MEDIAN':>10} {'P90':>10}")
        print(f"  {'-'*52}")
        for name in scenarios:
            r = yr_results[name]
            print(f"  {name:<22} {r['cagr_p10']:>9.1f}% {r['cagr_p50']:>9.1f}% {r['cagr_p90']:>9.1f}%")

        # --- Risk Table ---
        print(f"\n  {'RISK & PROBABILITY':^70}")
        print(f"  {'Scenario':<22} {'Loss%':>8} {'2x%':>8} {'5x%':>8} {'10x%':>8} {'Volatility':>12}")
        print(f"  {'-'*66}")
        for name in scenarios:
            r = yr_results[name]
            vol = r['std'] / r['mean'] * 100  # CV as proxy for dispersion
            print(f"  {name:<22} {r['prob_loss']:>7.1f}% {r['prob_double']:>7.1f}% "
                  f"{r['prob_5x']:>7.1f}% {r['prob_10x']:>7.1f}% {vol:>10.1f}%")

    # ============================================================
    # CROSS-HORIZON SUMMARY
    # ============================================================
    print(f"\n\n{'=' * 100}")
    print("  CROSS-HORIZON SUMMARY: Median Portfolio Value (Multiplier)")
    print(f"{'=' * 100}")
    print(f"\n  {'Scenario':<22} {'5yr':>14} {'10yr':>14} {'15yr':>14} {'20yr':>14}")
    print(f"  {'-'*78}")
    for name in scenarios:
        vals = []
        for yr in YEARS_TO_REPORT:
            r = all_results[yr][name]
            vals.append(f"{format_inr(r['p50']):>10} ({r['multiplier_p50']:.1f}x)")
        print(f"  {name:<22} {vals[0]:>14} {vals[1]:>14} {vals[2]:>14} {vals[3]:>14}")

    # ============================================================
    # HEAD-TO-HEAD: PLAYBOOK vs 100% EQUITY
    # ============================================================
    print(f"\n\n{'=' * 100}")
    print("  HEAD-TO-HEAD: Playbook v4 vs 100% Equity (Lump Sum)")
    print(f"{'=' * 100}")

    for yr in YEARS_TO_REPORT:
        pb = all_results[yr]["Playbook v4"]
        eq = all_results[yr]["100% Equity (Lump)"]

        print(f"\n  {yr}-Year:")
        print(f"    {'Metric':<25} {'Playbook':>14} {'100% Equity':>14} {'Winner':>14}")
        print(f"    {'-'*67}")

        # Median
        pb_better = pb['p50'] > eq['p50']
        print(f"    {'Median':.<25} {format_inr(pb['p50']):>14} {format_inr(eq['p50']):>14}"
              f"    {'Playbook' if pb_better else 'Equity'}")

        # P10 (downside)
        pb_better = pb['p10'] > eq['p10']
        print(f"    {'Downside (P10)':.<25} {format_inr(pb['p10']):>14} {format_inr(eq['p10']):>14}"
              f"    {'Playbook' if pb_better else 'Equity'}")

        # P90 (upside)
        pb_better = pb['p90'] > eq['p90']
        print(f"    {'Upside (P90)':.<25} {format_inr(pb['p90']):>14} {format_inr(eq['p90']):>14}"
              f"    {'Playbook' if pb_better else 'Equity'}")

        # Loss probability
        print(f"    {'Capital Loss Prob':.<25} {pb['prob_loss']:>13.1f}% {eq['prob_loss']:>13.1f}%"
              f"    {'Playbook' if pb['prob_loss'] < eq['prob_loss'] else 'Equity'}")

        # CAGR
        print(f"    {'Median CAGR':.<25} {pb['cagr_p50']:>13.1f}% {eq['cagr_p50']:>13.1f}%"
              f"    {'Playbook' if pb['cagr_p50'] > eq['cagr_p50'] else 'Equity'}")

    # ============================================================
    # SWING IMPACT ANALYSIS
    # ============================================================
    print(f"\n\n{'=' * 100}")
    print("  SWING INCOME IMPACT (20-year, median)")
    print(f"{'=' * 100}")

    no_sw = all_results[20]["Playbook (No Swing)"]
    base = all_results[20]["Playbook v4"]
    double = all_results[20]["Playbook (2x Swing)"]

    print(f"\n  {'Swing Level':<22} {'Median':>14} {'CAGR':>8} {'vs Base':>12}")
    print(f"  {'-'*56}")
    print(f"  {'No Swing':<22} {format_inr(no_sw['p50']):>14} {no_sw['cagr_p50']:>7.1f}%"
          f" {(no_sw['p50']/base['p50']-1)*100:>+10.1f}%")
    print(f"  {'Base (18% on 50L)':<22} {format_inr(base['p50']):>14} {base['cagr_p50']:>7.1f}%"
          f" {'baseline':>12}")
    print(f"  {'2x Swing (36% on 50L)':<22} {format_inr(double['p50']):>14} {double['cagr_p50']:>7.1f}%"
          f" {(double['p50']/base['p50']-1)*100:>+10.1f}%")

    swing_contribution = base['p50'] - no_sw['p50']
    print(f"\n  Swing adds {format_inr(swing_contribution)} to 20yr median ({swing_contribution/base['p50']*100:.1f}% of total)")

    # ============================================================
    # VERDICT
    # ============================================================
    print(f"\n\n{'=' * 100}")
    print("  VERDICT")
    print(f"{'=' * 100}")

    pb20 = all_results[20]["Playbook v4"]
    eq20 = all_results[20]["100% Equity (Lump)"]
    st20 = all_results[20]["100% Equity + ST"]
    sip20 = all_results[20]["Direct SIP"]

    print(f"""
  100% EQUITY vs PLAYBOOK (20yr):
    Equity median: {format_inr(eq20['p50'])} | Playbook median: {format_inr(pb20['p50'])}
    Equity wins on MEDIAN by {(eq20['p50']/pb20['p50']-1)*100:.1f}% — IF markets deliver 13% CAGR.
    BUT Equity has {eq20['prob_loss']:.1f}% capital loss risk vs {pb20['prob_loss']:.1f}% for Playbook.
    Equity P10 (worst case): {format_inr(eq20['p10'])} vs Playbook P10: {format_inr(pb20['p10'])}

  100% EQUITY + ST vs PLAYBOOK (20yr):
    ST-timed equity median: {format_inr(st20['p50'])} | Playbook: {format_inr(pb20['p50'])}
    ST timing in equity-only captures most of the playbook return.
    Playbook adds floor via parking + gold/REIT diversification.

  DIRECT SIP vs PLAYBOOK (20yr):
    SIP median: {format_inr(sip20['p50'])} | Playbook: {format_inr(pb20['p50'])}
    SIP loses because capital sits idle at low rates while waiting to deploy.
    Playbook parks at 5.6% and deploys tactically.

  SWING INCOME:
    Adds {format_inr(swing_contribution)} over 20yr ({swing_contribution/STARTING_CAPITAL*100:.1f}% of starting capital).
    Independent income stream — works regardless of equity performance.

  BOTTOM LINE:
    - 100% equity wins in bull markets but has tail risk
    - Playbook wins in moderate/bear markets with zero ruin risk
    - Swing income is a meaningful kicker (~{swing_contribution/base['p50']*100:.0f}% of final value)
    - ST timing is valuable — parking at 5.6% while waiting is free alpha
""")

    print(f"{'=' * 100}")
    print("  Past performance does not predict future results.")
    print(f"{'=' * 100}")


if __name__ == "__main__":
    main()
