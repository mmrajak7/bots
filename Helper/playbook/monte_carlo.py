"""
Monte Carlo Simulation: Investment Playbook
============================================
Models the 3-tier strategy over 5/10/15/20 years with 10,000 simulations.

Strategy:
  Tier 1: Park in Arbitrage Fund (80%) + LIQUIDBEES (20%)
  Tier 2: Deploy into ETFs/Stocks on Monthly ST signals, exit on ST flip
  Tier 3: FIFTY bot swing profits flow into the system

Key dynamics modeled:
  - Correction frequency and depth (based on historical NIFTY data)
  - Entry at discount (buying when ST signals fire)
  - Holding period until Monthly ST flips (2-5 years typically)
  - Exit with gains or losses
  - Parking returns while waiting
  - Swing trading profit pipeline
  - Tax drag on all transactions
"""

import numpy as np
import json
from dataclasses import dataclass, field
from typing import List, Tuple

np.random.seed(42)

# ============================================================
# PARAMETERS (based on research)
# ============================================================

STARTING_CAPITAL = 3_00_00_000  # 3 Crore
SWING_CAPITAL = 50_00_000       # 50L dedicated swing capital (not part of 3Cr)
NUM_SIMULATIONS = 10_000
YEARS = [5, 10, 15, 20]
MAX_YEARS = max(YEARS)

# --- Tier 1: Parking Returns (post-tax, 30% slab) ---
# Blended: 20% LIQUIDBEES @ 3.7% + 80% Arbitrage @ ~5.5% post-tax
PARKING_RETURN_MEAN = 0.051       # 5.1% post-tax blended
PARKING_RETURN_STD = 0.005        # Very low variance (near-guaranteed)

# --- Tier 2: Deployed Returns ---
# Indian equity long-term: ~13% CAGR nominal, ~18% std dev
# But we enter at 15-25% discount (ST signals), so entry advantage exists
EQUITY_RETURN_MEAN = 0.13         # 13% annual (conservative, NIFTY long-run)
EQUITY_RETURN_STD = 0.18          # 18% annual volatility
ENTRY_DISCOUNT_MEAN = 0.18        # Average 18% below ATH when we enter
ENTRY_DISCOUNT_STD = 0.05         # Varies 10-25%

# Gold/Silver/REIT blended (for non-equity deployments)
ALT_RETURN_MEAN = 0.10            # 10% nominal (gold ~11%, REITs ~12%, silver ~8%)
ALT_RETURN_STD = 0.15             # 15% volatility

# Allocation split when deployed: 65% equity, 20% gold/REIT, 15% stays parked
EQUITY_DEPLOY_RATIO = 0.65
ALT_DEPLOY_RATIO = 0.20
ALWAYS_PARKED_RATIO = 0.15        # 10% reserve + 5% buffer

# --- Correction / Deployment Frequency ---
# Probability of a significant correction in any given year
# Historical: 15%+ correction every 2-3 years = ~35-40% per year
CORRECTION_PROB_PER_YEAR = 0.38

# When correction happens, what fraction of parked capital do we deploy?
# Stage 1 (25%) happens first, then Stage 2 (75%) if Monthly ST touched
# On average across corrections, we deploy ~60% of available parked capital
DEPLOY_FRACTION_MEAN = 0.60
DEPLOY_FRACTION_STD = 0.15

# --- Holding Period (once deployed) ---
# Monthly ST stays bullish for 2-5 years typically
# Each year, there's a probability the Monthly ST flips and we exit
# Model as: ~25% chance per year of Monthly ST exit signal
MONTHLY_ST_EXIT_PROB = 0.25       # ~25% per year = average hold of 4 years

# When we exit, what's the probability it's at a loss?
# (entered on ST signal, but ST flipped quickly = loss)
# Historical: ~20% of Monthly ST entries fail within first year
EXIT_AT_LOSS_PROB = 0.15          # 15% chance of exiting at a loss
LOSS_MAGNITUDE_MEAN = 0.12        # Average loss of 12% when it happens

# --- Tier 3: Swing Trading ---
# FIFTY bot annual return on 50L capital
SWING_RETURN_MEAN = 0.18          # 18% annual on swing capital
SWING_RETURN_STD = 0.12           # Significant variance
SWING_PROFIT_TO_DEPLOY = 0.50     # 50% of profits go to immediate deployment
SWING_PROFIT_TO_PARKING = 0.50    # 50% go to parking (Tier 1)

# --- Tax ---
TAX_STCG = 0.20                   # 20% for < 12 months
TAX_LTCG = 0.125                  # 12.5% for > 12 months
LTCG_EXEMPTION = 1_25_000         # Rs 1.25L annual exemption
TAX_SWING = 0.20                  # Swing trading STCG


def simulate_one_run(years: int, correction_prob: float = None, equity_return: float = None) -> dict:
    """Simulate one complete run of the strategy over N years."""
    _correction_prob = correction_prob if correction_prob is not None else CORRECTION_PROB_PER_YEAR
    _equity_return_mean = equity_return if equity_return is not None else EQUITY_RETURN_MEAN

    # State
    parked_capital = STARTING_CAPITAL  # Tier 1
    deployed_equity = 0.0              # Tier 2 equity
    deployed_alt = 0.0                 # Tier 2 gold/REIT
    deployed_equity_age = 0            # Months since deployment (for tax)
    deployed_alt_age = 0
    total_realized_gains = 0.0
    total_taxes_paid = 0.0
    total_swing_added = 0.0
    deployment_count = 0
    exit_count = 0
    loss_count = 0

    yearly_values = []

    for year in range(years):
        # --- Tier 1: Parking returns ---
        parking_return = np.random.normal(PARKING_RETURN_MEAN, PARKING_RETURN_STD)
        parking_return = max(parking_return, 0.02)  # Floor at 2% (worst case)
        parked_capital *= (1 + parking_return)

        # --- Tier 3: Swing trading profits ---
        swing_return = np.random.normal(SWING_RETURN_MEAN, SWING_RETURN_STD)
        swing_profit = SWING_CAPITAL * max(swing_return, -0.20)  # Cap loss at 20%

        if swing_profit > 0:
            # After STCG tax
            swing_profit_after_tax = swing_profit * (1 - TAX_SWING)
            # Route 50/50
            immediate_deploy = swing_profit_after_tax * SWING_PROFIT_TO_DEPLOY
            to_parking = swing_profit_after_tax * SWING_PROFIT_TO_PARKING

            # Immediate deploy goes to equity (NIFTYBEES)
            deployed_equity += immediate_deploy
            parked_capital += to_parking
            total_swing_added += swing_profit_after_tax

        # --- Check for correction / deployment opportunity ---
        correction_happens = np.random.random() < _correction_prob

        if correction_happens and parked_capital > STARTING_CAPITAL * ALWAYS_PARKED_RATIO:
            deployment_count += 1

            # How much to deploy
            available = parked_capital - (STARTING_CAPITAL * 0.10)  # Keep 10% reserve
            available = max(available, 0)
            deploy_frac = np.clip(
                np.random.normal(DEPLOY_FRACTION_MEAN, DEPLOY_FRACTION_STD),
                0.30, 0.85
            )
            deploy_amount = available * deploy_frac

            # Split between equity and alternatives
            to_equity = deploy_amount * EQUITY_DEPLOY_RATIO
            to_alt = deploy_amount * ALT_DEPLOY_RATIO

            # Entry discount advantage (modeled as a one-time boost)
            discount = np.clip(
                np.random.normal(ENTRY_DISCOUNT_MEAN, ENTRY_DISCOUNT_STD),
                0.08, 0.35
            )
            # Buying at discount means our cost basis is lower
            # This effectively increases our return in the first year
            entry_boost = discount / 3  # Spread the discount advantage over ~3 years

            deployed_equity += to_equity * (1 + entry_boost)
            deployed_alt += to_alt * (1 + entry_boost)
            parked_capital -= deploy_amount

            # Reset deployment age for new deployments
            # (simplified: we track aggregate, not individual lots)
            if deployed_equity > 0:
                # Weighted average age
                deployed_equity_age = 0
            if deployed_alt > 0:
                deployed_alt_age = 0

        # --- Deployed capital returns ---
        if deployed_equity > 0:
            eq_return = np.random.normal(_equity_return_mean, EQUITY_RETURN_STD)
            deployed_equity *= (1 + eq_return)
            deployed_equity_age += 12  # One year in months

            # Check for Monthly ST exit
            if np.random.random() < MONTHLY_ST_EXIT_PROB:
                exit_count += 1

                # Is this exit at a loss?
                if np.random.random() < EXIT_AT_LOSS_PROB:
                    loss_count += 1
                    # Sell at a loss — no tax, but lose money
                    loss = deployed_equity * np.random.uniform(0.05, LOSS_MAGNITUDE_MEAN)
                    proceeds = deployed_equity - loss
                else:
                    proceeds = deployed_equity
                    gain = proceeds - (deployed_equity * 0.85)  # Rough cost basis
                    gain = max(gain, 0)

                    # Tax on gains
                    if deployed_equity_age >= 12:
                        taxable_gain = max(gain - LTCG_EXEMPTION, 0)
                        tax = taxable_gain * TAX_LTCG
                    else:
                        tax = gain * TAX_STCG

                    proceeds -= tax
                    total_taxes_paid += tax
                    total_realized_gains += gain

                parked_capital += max(proceeds, 0)
                deployed_equity = 0
                deployed_equity_age = 0

        if deployed_alt > 0:
            alt_return = np.random.normal(ALT_RETURN_MEAN, ALT_RETURN_STD)
            deployed_alt *= (1 + alt_return)
            deployed_alt_age += 12

            # Lower exit probability for gold/REITs (hold longer)
            if np.random.random() < (MONTHLY_ST_EXIT_PROB * 0.7):
                proceeds = deployed_alt
                gain = proceeds - (deployed_alt * 0.85)
                gain = max(gain, 0)

                if deployed_alt_age >= 12:
                    taxable_gain = max(gain - LTCG_EXEMPTION * 0.3, 0)
                    tax = taxable_gain * TAX_LTCG
                else:
                    tax = gain * TAX_STCG

                proceeds -= tax
                total_taxes_paid += tax

                parked_capital += max(proceeds, 0)
                deployed_alt = 0
                deployed_alt_age = 0

        # --- End of year snapshot ---
        total_portfolio = parked_capital + deployed_equity + deployed_alt
        yearly_values.append(total_portfolio)

    total_portfolio = parked_capital + deployed_equity + deployed_alt

    return {
        "final_value": total_portfolio,
        "parked": parked_capital,
        "deployed_equity": deployed_equity,
        "deployed_alt": deployed_alt,
        "total_return_pct": (total_portfolio / STARTING_CAPITAL - 1) * 100,
        "cagr": ((total_portfolio / STARTING_CAPITAL) ** (1 / years) - 1) * 100,
        "taxes_paid": total_taxes_paid,
        "swing_added": total_swing_added,
        "deployments": deployment_count,
        "exits": exit_count,
        "losses": loss_count,
        "yearly_values": yearly_values,
    }


def format_inr(amount: float) -> str:
    """Format amount in Indian numbering (Lakhs/Crores)."""
    cr = amount / 1_00_00_000
    if cr >= 1:
        return f"Rs {cr:,.2f} Cr"
    else:
        lakhs = amount / 1_00_000
        return f"Rs {lakhs:,.1f} L"


def _run_sensitivity(vals: list, n: int, years: int, correction_prob=None, equity_return=None):
    """Run N simulations with parameter overrides for sensitivity analysis."""
    for _ in range(n):
        r = simulate_one_run(years, correction_prob=correction_prob, equity_return=equity_return)
        vals.append(r["final_value"])


def run_simulation():
    """Run full Monte Carlo simulation and print results."""

    print("=" * 70)
    print("MONTE CARLO SIMULATION: Investment Playbook")
    print(f"Starting Capital: {format_inr(STARTING_CAPITAL)}")
    print(f"Swing Capital (separate): {format_inr(SWING_CAPITAL)}")
    print(f"Simulations: {NUM_SIMULATIONS:,}")
    print("=" * 70)

    for target_year in YEARS:
        results = []
        for _ in range(NUM_SIMULATIONS):
            result = simulate_one_run(target_year)
            results.append(result)

        final_values = [r["final_value"] for r in results]
        cagrs = [r["cagr"] for r in results]
        taxes = [r["taxes_paid"] for r in results]
        swing_added = [r["swing_added"] for r in results]
        deployments = [r["deployments"] for r in results]
        losses = [r["losses"] for r in results]

        # Percentiles
        p5 = np.percentile(final_values, 5)
        p10 = np.percentile(final_values, 10)
        p25 = np.percentile(final_values, 25)
        p50 = np.percentile(final_values, 50)
        p75 = np.percentile(final_values, 75)
        p90 = np.percentile(final_values, 90)
        p95 = np.percentile(final_values, 95)
        mean = np.mean(final_values)

        cagr_p10 = np.percentile(cagrs, 10)
        cagr_p25 = np.percentile(cagrs, 25)
        cagr_p50 = np.percentile(cagrs, 50)
        cagr_p75 = np.percentile(cagrs, 75)
        cagr_p90 = np.percentile(cagrs, 90)

        print(f"\n{'-' * 70}")
        print(f"  AFTER {target_year} YEARS")
        print(f"{'-' * 70}")

        print(f"\n  Portfolio Value Distribution:")
        print(f"    {'Pessimistic (P5):':.<30} {format_inr(p5):>18}")
        print(f"    {'Conservative (P10):':.<30} {format_inr(p10):>18}")
        print(f"    {'Below Average (P25):':.<30} {format_inr(p25):>18}")
        print(f"    {'>> MEDIAN (P50):':.<30} {format_inr(p50):>18}  << Most likely")
        print(f"    {'Above Average (P75):':.<30} {format_inr(p75):>18}")
        print(f"    {'Optimistic (P90):':.<30} {format_inr(p90):>18}")
        print(f"    {'Best Case (P95):':.<30} {format_inr(p95):>18}")
        print(f"    {'Mean:':.<30} {format_inr(mean):>18}")

        print(f"\n  CAGR Distribution:")
        print(f"    {'Conservative (P10):':.<30} {cagr_p10:>7.1f}%")
        print(f"    {'Below Average (P25):':.<30} {cagr_p25:>7.1f}%")
        print(f"    {'>> MEDIAN (P50):':.<30} {cagr_p50:>7.1f}%  << Most likely")
        print(f"    {'Above Average (P75):':.<30} {cagr_p75:>7.1f}%")
        print(f"    {'Optimistic (P90):':.<30} {cagr_p90:>7.1f}%")

        print(f"\n  Multiplier (from 3 Cr):")
        print(f"    {'Pessimistic:':.<30} {p5/STARTING_CAPITAL:>7.1f}x")
        print(f"    {'Median:':.<30} {p50/STARTING_CAPITAL:>7.1f}x")
        print(f"    {'Optimistic:':.<30} {p90/STARTING_CAPITAL:>7.1f}x")

        print(f"\n  Strategy Metrics (averages across simulations):")
        print(f"    Deployment opportunities:   {np.mean(deployments):.1f} times in {target_year} years")
        print(f"    Loss exits (ST whipsaw):    {np.mean(losses):.1f} times")
        print(f"    Swing profits added:        {format_inr(np.mean(swing_added))}")
        print(f"    Total taxes paid:           {format_inr(np.mean(taxes))}")

        # Probability of beating alternatives
        fv = np.array(final_values)
        prob_beat_fd = np.mean(fv > STARTING_CAPITAL * (1.065 ** target_year)) * 100
        prob_beat_nifty = np.mean(fv > STARTING_CAPITAL * (1.12 ** target_year)) * 100
        prob_double = np.mean(fv > STARTING_CAPITAL * 2) * 100
        prob_5x = np.mean(fv > STARTING_CAPITAL * 5) * 100
        prob_10x = np.mean(fv > STARTING_CAPITAL * 10) * 100
        prob_loss = np.mean(fv < STARTING_CAPITAL) * 100

        print(f"\n  Probability Analysis:")
        print(f"    Beat FD (6.5% pre-tax):     {prob_beat_fd:>5.1f}%")
        print(f"    Beat NIFTY SIP (12% CAGR):  {prob_beat_nifty:>5.1f}%")
        print(f"    Double (> 6 Cr):            {prob_double:>5.1f}%")
        print(f"    5x (> 15 Cr):               {prob_5x:>5.1f}%")
        print(f"    10x (> 30 Cr):              {prob_10x:>5.1f}%")
        print(f"    Capital loss (< 3 Cr):      {prob_loss:>5.1f}%")

    # --- Comparison: What if just parked 100% in Arbitrage? ---
    print(f"\n{'=' * 70}")
    print("  COMPARISON: What if you did NOTHING (100% Arbitrage Fund)?")
    print(f"{'=' * 70}")
    for yr in YEARS:
        arb_value = STARTING_CAPITAL * (1 + 0.051) ** yr  # 5.1% post-tax
        print(f"    After {yr:>2} years: {format_inr(arb_value):>18}  ({arb_value/STARTING_CAPITAL:.1f}x)")

    print(f"\n{'=' * 70}")
    print("  COMPARISON: What if 100% in NIFTY from day 1 (12% CAGR)?")
    print(f"{'=' * 70}")
    for yr in YEARS:
        nifty_value = STARTING_CAPITAL * (1.12) ** yr
        print(f"    After {yr:>2} years: {format_inr(nifty_value):>18}  ({nifty_value/STARTING_CAPITAL:.1f}x)")

    # --- Sensitivity Analysis ---
    print(f"\n{'=' * 70}")
    print("  SENSITIVITY: What matters most?")
    print(f"{'=' * 70}")

    # Test: What if corrections are rarer (every 4-5 years instead of 2-3)?
    original_prob = CORRECTION_PROB_PER_YEAR
    scenarios = {
        "Fewer corrections (every 4-5 yrs)": 0.22,
        "Base case (every 2-3 yrs)": 0.38,
        "More corrections (every 1.5-2 yrs)": 0.55,
    }

    print(f"\n  Correction Frequency Impact (20-year median):")
    for label, prob in scenarios.items():
        vals = []
        _run_sensitivity(vals, 3000, 20, correction_prob=prob)
        median = np.percentile(vals, 50)
        print(f"    {label:.<45} {format_inr(median):>18}")

    # What if equity returns are lower?
    eq_scenarios = {
        "Bear case (equity 9% CAGR)": 0.09,
        "Base case (equity 13% CAGR)": 0.13,
        "Bull case (equity 16% CAGR)": 0.16,
    }

    print(f"\n  Equity Return Impact (20-year median):")
    for label, ret in eq_scenarios.items():
        vals = []
        _run_sensitivity(vals, 3000, 20, equity_return=ret)
        median = np.percentile(vals, 50)
        print(f"    {label:.<45} {format_inr(median):>18}")

    print(f"\n{'=' * 70}")
    print("  DISCLAIMER")
    print(f"{'=' * 70}")
    print("  This simulation models historical patterns, NOT guarantees.")
    print("  Key assumptions that may not hold:")
    print("    - Equity returns continue at 13% CAGR (India GDP growth sustains)")
    print("    - Corrections continue at historical frequency")
    print("    - Tax regime doesn't change adversely")
    print("    - Monthly ST remains a valid trend signal")
    print("    - Swing trading (FIFTY) continues generating ~18% annual returns")
    print("  Past performance does not predict future results.")


if __name__ == "__main__":
    run_simulation()
