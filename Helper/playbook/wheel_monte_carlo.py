"""
Wheel Strategy Monte Carlo Simulation - NSE India
===================================================
Simulates 10,000 paths over 1-year, 3-year, and 5-year horizons for the
Wheel Strategy (Cash Secured Put -> Assignment -> Covered Call -> Called Away)
on 5 NSE stocks: COALINDIA, ITC, POWERGRID, SBIN, ICICIBANK.

Stock prices follow Geometric Brownian Motion (GBM) with daily steps.
Option premiums estimated via Black-Scholes (vectorized).
Full tax model (F&O business income, STCG, LTCG, dividends).
Risk events: market crashes and single-stock blowups.

Capital levels: Rs 25 Lakh, Rs 50 Lakh, Rs 1 Crore.
Market scenarios: Base, Bull, Bear, Sideways, High Volatility.

PERFORMANCE: Fully vectorized across all simulations using numpy.
All 10,000 sims run in parallel per stock-slot.

Output:
  - Console summary tables
  - playbook/MONTE_CARLO_RESULTS.md
  - playbook/charts/*.png (5 chart types)

Usage:
  python playbook/wheel_monte_carlo.py
"""

import os
import time
import warnings
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np
from scipy.stats import norm as sp_norm
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

warnings.filterwarnings("ignore", category=RuntimeWarning)

# ============================================================
# CONFIGURATION
# ============================================================

NUM_SIMULATIONS = 10_000
TRADING_DAYS_PER_YEAR = 252
TRADING_DAYS_PER_MONTH = 21
RISK_FREE_RATE = 0.065       # India 10Y benchmark
MONTHS_PER_YEAR = 12
HORIZONS = [1, 3, 5]
RNG_SEED = 42

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CHARTS_DIR = os.path.join(SCRIPT_DIR, "charts")
RESULTS_MD_PATH = os.path.join(SCRIPT_DIR, "MONTE_CARLO_RESULTS.md")


# ============================================================
# STOCK DEFINITIONS
# ============================================================

@dataclass
class StockConfig:
    name: str
    spot: float
    lot_size: int
    csp_strike: float
    annual_vol: float
    div_yield: float
    pe_ratio: float
    sector: str
    strike_interval: float


STOCKS = [
    StockConfig("COALINDIA",  423,  1350, 400, 0.35, 0.063, 7.4,  "Mining",   5.0),
    StockConfig("ITC",        326,  1600, 310, 0.28, 0.044, 20.0, "FMCG",     5.0),
    StockConfig("POWERGRID",  303,  1900, 285, 0.25, 0.031, 16.0, "Power",    2.5),
    StockConfig("SBIN",       1228, 750,  1150, 0.32, 0.016, 10.9, "Banking", 25.0),
    StockConfig("ICICIBANK",  1395, 700,  1300, 0.30, 0.008, 19.8, "Banking", 25.0),
]


# ============================================================
# CAPITAL & SCENARIOS
# ============================================================

CAPITAL_LEVELS = {
    "25L":  25_00_000,
    "50L":  50_00_000,
    "1Cr": 1_00_00_000,
}

ACTIVE_FRACTION = 0.60
RESERVE_FRACTION = 0.40
RESERVE_ANNUAL_RETURN = 0.055

MAX_ACTIVE_STOCKS = {"25L": 3, "50L": 3, "1Cr": 5}


@dataclass
class Scenario:
    name: str
    mu: float
    vol_multiplier: float
    description: str


SCENARIOS = [
    Scenario("Base",     0.11, 1.00, "mu=11%, normal vol"),
    Scenario("Bull",     0.18, 0.80, "mu=18%, vol -20%"),
    Scenario("Bear",    -0.05, 1.30, "mu=-5%, vol +30%"),
    Scenario("Sideways", 0.03, 1.00, "mu=3%, normal vol"),
    Scenario("HighVol",  0.08, 1.50, "mu=8%, vol +50%"),
]


# ============================================================
# TAX & TRANSACTION COSTS
# ============================================================

TAX_FNO_BUSINESS = 0.30
TAX_DIVIDEND = 0.30
TAX_STCG = 0.20
TAX_LTCG = 0.125
LTCG_EXEMPTION = 1_25_000

TXN_COST_ASSIGNMENT = 0.0041
TXN_COST_OPTION = 0.0015


# ============================================================
# RISK EVENTS
# ============================================================

MARKET_CRASH_PROB = 0.05
MARKET_CRASH_MAG_MIN = 0.15
MARKET_CRASH_MAG_MAX = 0.20
STOCK_BLOWUP_PROB = 0.02
STOCK_BLOWUP_MAG_MIN = 0.30
STOCK_BLOWUP_MAG_MAX = 0.50


# ============================================================
# VECTORIZED BLACK-SCHOLES
# ============================================================

def bs_put_vec(S: np.ndarray, K: float, T: float, r: float, sigma: np.ndarray) -> np.ndarray:
    """Vectorized Black-Scholes put price across N simulations."""
    with np.errstate(divide="ignore", invalid="ignore"):
        sqrt_T = np.sqrt(T)
        d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
        d2 = d1 - sigma * sqrt_T
        price = K * np.exp(-r * T) * sp_norm.cdf(-d2) - S * sp_norm.cdf(-d1)
    return np.maximum(price, 0.0)


def bs_call_vec(S: np.ndarray, K: np.ndarray, T: float, r: float, sigma: np.ndarray) -> np.ndarray:
    """Vectorized Black-Scholes call price across N simulations."""
    with np.errstate(divide="ignore", invalid="ignore"):
        sqrt_T = np.sqrt(T)
        d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
        d2 = d1 - sigma * sqrt_T
        price = S * sp_norm.cdf(d1) - K * np.exp(-r * T) * sp_norm.cdf(d2)
    return np.maximum(price, 0.0)


def round_to_strike_vec(prices: np.ndarray, interval: float) -> np.ndarray:
    """Round prices to nearest strike interval (vectorized)."""
    return np.round(prices / interval) * interval


# ============================================================
# VECTORIZED GBM PRICE GENERATION
# ============================================================

def generate_monthly_prices(
    stock: StockConfig,
    scenario: Scenario,
    num_months: int,
    n_sims: int,
    rng: np.random.Generator,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Generate GBM price paths and return month-start and month-end prices.

    Returns:
        start_prices: shape (num_months, n_sims) - spot at start of each month
        end_prices:   shape (num_months, n_sims) - spot at end of each month
    """
    sigma = stock.annual_vol * scenario.vol_multiplier
    mu = scenario.mu
    dt = 1.0 / TRADING_DAYS_PER_YEAR
    drift = (mu - 0.5 * sigma**2) * dt
    diffusion = sigma * np.sqrt(dt)

    total_days = num_months * TRADING_DAYS_PER_MONTH

    # Generate all daily log-returns: shape (total_days, n_sims)
    z = rng.standard_normal((total_days, n_sims))
    log_returns = drift + diffusion * z

    # Apply risk events (vectorized across sims)
    years = num_months // MONTHS_PER_YEAR
    for yr in range(years):
        yr_start = yr * TRADING_DAYS_PER_YEAR
        yr_end = min(yr_start + TRADING_DAYS_PER_YEAR, total_days)

        # Market crash: each sim independently
        crash_mask = rng.random(n_sims) < MARKET_CRASH_PROB
        if np.any(crash_mask):
            crash_mag = rng.uniform(MARKET_CRASH_MAG_MIN, MARKET_CRASH_MAG_MAX, n_sims)
            # Pick a random month within the year for each sim
            crash_start_day = yr_start + rng.integers(0, max(yr_end - yr_start - TRADING_DAYS_PER_MONTH, 1), n_sims)
            for sim_idx in np.where(crash_mask)[0]:
                cs = crash_start_day[sim_idx]
                ce = min(cs + TRADING_DAYS_PER_MONTH, total_days)
                days_in_crash = ce - cs
                if days_in_crash > 0:
                    daily_crash = np.log(1 - crash_mag[sim_idx]) / days_in_crash
                    log_returns[cs:ce, sim_idx] += daily_crash

        # Stock blowup: each sim independently
        blowup_mask = rng.random(n_sims) < STOCK_BLOWUP_PROB
        if np.any(blowup_mask):
            blowup_mag = rng.uniform(STOCK_BLOWUP_MAG_MIN, STOCK_BLOWUP_MAG_MAX, n_sims)
            blowup_start_day = yr_start + rng.integers(0, max(yr_end - yr_start - 5, 1), n_sims)
            for sim_idx in np.where(blowup_mask)[0]:
                bs_day = blowup_start_day[sim_idx]
                be_day = min(bs_day + 5, total_days)
                days_in_blowup = be_day - bs_day
                if days_in_blowup > 0:
                    daily_blowup = np.log(1 - blowup_mag[sim_idx]) / days_in_blowup
                    log_returns[bs_day:be_day, sim_idx] += daily_blowup

    # Cumulative returns -> prices
    cum_log_returns = np.cumsum(log_returns, axis=0)
    all_prices = stock.spot * np.exp(cum_log_returns)  # shape (total_days, n_sims)

    # Extract month-start and month-end prices
    start_prices = np.empty((num_months, n_sims))
    end_prices = np.empty((num_months, n_sims))
    for m in range(num_months):
        day_s = m * TRADING_DAYS_PER_MONTH
        day_e = min(day_s + TRADING_DAYS_PER_MONTH - 1, total_days - 1)
        start_prices[m] = all_prices[day_s]
        end_prices[m] = all_prices[day_e]

    return start_prices, end_prices


# ============================================================
# VECTORIZED WHEEL SIMULATION
# ============================================================

def simulate_wheel_slot(
    stock: StockConfig,
    scenario: Scenario,
    num_months: int,
    n_sims: int,
    rng: np.random.Generator,
) -> dict:
    """
    Simulate the wheel strategy for one stock slot across ALL n_sims simultaneously.

    Each sim independently cycles: CSP -> assignment -> holding+CC -> called away -> CSP.
    All operations are vectorized across simulations.

    Returns dict of arrays, each shape (n_sims,).
    Monthly income array shape (num_months, n_sims).
    Portfolio path shape (num_months, n_sims).
    """
    sigma_base = stock.annual_vol * scenario.vol_multiplier
    dte_years = TRADING_DAYS_PER_MONTH / TRADING_DAYS_PER_YEAR

    # Generate price paths
    start_prices, end_prices = generate_monthly_prices(
        stock, scenario, num_months, n_sims, rng
    )

    # State arrays (vectorized across sims)
    # state: 0 = CSP, 1 = Holding
    state = np.zeros(n_sims, dtype=np.int8)
    cost_basis = np.zeros(n_sims)
    months_holding = np.zeros(n_sims, dtype=np.int32)
    cc_strike = np.zeros(n_sims)

    # Accumulators
    total_csp_premium = np.zeros(n_sims)
    total_cc_premium = np.zeros(n_sims)
    total_dividend = np.zeros(n_sims)
    total_cap_gains = np.zeros(n_sims)
    total_txn_costs = np.zeros(n_sims)
    tax_fno = np.zeros(n_sims)
    tax_div = np.zeros(n_sims)
    tax_stcg = np.zeros(n_sims)
    tax_ltcg = np.zeros(n_sims)
    num_assignments = np.zeros(n_sims, dtype=np.int32)
    num_calls_away = np.zeros(n_sims, dtype=np.int32)

    monthly_income = np.zeros((num_months, n_sims))
    portfolio_values = np.zeros((num_months, n_sims))

    # Sigma array (constant per stock but could add random IV component)
    sigma_arr = np.full(n_sims, sigma_base)

    # Bid-ask haircut: random per month per sim
    haircuts = rng.uniform(0.05, 0.15, (num_months, n_sims))

    for m in range(num_months):
        spot_start = start_prices[m]
        spot_end = end_prices[m]
        haircut = haircuts[m]

        csp_mask = (state == 0)
        hold_mask = (state == 1)
        m_income = np.zeros(n_sims)

        # =============================================
        # CSP PHASE (state == 0)
        # =============================================
        if np.any(csp_mask):
            csp_idx = csp_mask
            K = stock.csp_strike

            # Put premium via Black-Scholes
            premium_ps = bs_put_vec(spot_start, K, dte_years, RISK_FREE_RATE, sigma_arr)
            premium_ps *= (1.0 - haircut)
            premium_ps = np.maximum(premium_ps, 0.10)

            premium_total = premium_ps * stock.lot_size  # per sim

            # Transaction cost
            txn = premium_total * TXN_COST_OPTION
            net_premium = premium_total - txn

            # Tax on F&O premium
            tax_p = net_premium * TAX_FNO_BUSINESS

            # Only accumulate for sims in CSP state
            total_csp_premium += np.where(csp_idx, net_premium, 0)
            total_txn_costs += np.where(csp_idx, txn, 0)
            tax_fno += np.where(csp_idx, tax_p, 0)
            m_income += np.where(csp_idx, net_premium - tax_p, 0)

            # Check assignment: spot_end < csp_strike
            assigned = csp_idx & (spot_end < K)
            if np.any(assigned):
                # Transition to holding
                state = np.where(assigned, 1, state)
                assignment_value = K * stock.lot_size
                assignment_txn = assignment_value * TXN_COST_ASSIGNMENT
                total_txn_costs += np.where(assigned, assignment_txn, 0)

                # Cost basis = strike - premium_per_share + txn_per_share
                cb = K - premium_ps + (assignment_txn / stock.lot_size)
                cost_basis = np.where(assigned, cb, cost_basis)
                months_holding = np.where(assigned, 0, months_holding)
                num_assignments += np.where(assigned, 1, 0).astype(np.int32)

                # Set CC strike: cost_basis + 3-5% OTM
                cc_otm = rng.uniform(0.03, 0.05, n_sims)
                raw_cc = cost_basis * (1 + cc_otm)
                rounded_cc = round_to_strike_vec(raw_cc, stock.strike_interval)
                # Ensure CC strike > cost_basis
                too_low = rounded_cc <= cost_basis
                rounded_cc = np.where(
                    too_low,
                    round_to_strike_vec(cost_basis, stock.strike_interval) + stock.strike_interval,
                    rounded_cc,
                )
                cc_strike = np.where(assigned, rounded_cc, cc_strike)

        # =============================================
        # HOLDING PHASE (state == 1)
        # =============================================
        hold_mask = (state == 1)  # Recompute after potential assignments
        if np.any(hold_mask):
            months_holding += np.where(hold_mask, 1, 0).astype(np.int32)

            # --- Dividends (quarterly) ---
            is_div_month = (months_holding % 3 == 0) & (months_holding > 0)
            div_mask = hold_mask & is_div_month
            if np.any(div_mask):
                quarterly_div_ps = (stock.div_yield / 4.0) * spot_start
                div_total = quarterly_div_ps * stock.lot_size
                div_tax = div_total * TAX_DIVIDEND

                total_dividend += np.where(div_mask, div_total, 0)
                tax_div += np.where(div_mask, div_tax, 0)
                cost_basis -= np.where(div_mask, quarterly_div_ps, 0)
                m_income += np.where(div_mask, div_total - div_tax, 0)

            # --- Covered Call ---
            cc_premium_ps = bs_call_vec(spot_start, cc_strike, dte_years, RISK_FREE_RATE, sigma_arr)
            cc_premium_ps *= (1.0 - haircut)
            cc_premium_ps = np.maximum(cc_premium_ps, 0.10)
            cc_premium_total = cc_premium_ps * stock.lot_size

            cc_txn = cc_premium_total * TXN_COST_OPTION
            net_cc = cc_premium_total - cc_txn
            cc_tax = net_cc * TAX_FNO_BUSINESS

            total_cc_premium += np.where(hold_mask, net_cc, 0)
            total_txn_costs += np.where(hold_mask, cc_txn, 0)
            tax_fno += np.where(hold_mask, cc_tax, 0)
            m_income += np.where(hold_mask, net_cc - cc_tax, 0)

            # Reduce cost basis by CC premium
            cost_basis -= np.where(hold_mask, cc_premium_ps, 0)

            # --- Check if called away: spot_end > cc_strike ---
            called = hold_mask & (spot_end > cc_strike)
            if np.any(called):
                gain_ps = cc_strike - cost_basis
                gain_total = gain_ps * stock.lot_size

                # Tax on capital gains
                is_ltcg = months_holding >= 12
                # For simplicity: apply exemption proportionally (1.25L per year divided across exits)
                cg_tax = np.where(
                    is_ltcg & (gain_total > 0),
                    np.maximum(gain_total - LTCG_EXEMPTION / 5, 0) * TAX_LTCG,  # Share exemption across stocks
                    np.where(gain_total > 0, gain_total * TAX_STCG, 0)
                )

                total_cap_gains += np.where(called, gain_total, 0)
                tax_stcg += np.where(called & ~is_ltcg & (gain_total > 0), gain_total * TAX_STCG, 0)
                tax_ltcg += np.where(called & is_ltcg & (gain_total > 0),
                                     np.maximum(gain_total - LTCG_EXEMPTION / 5, 0) * TAX_LTCG, 0)

                # Exit transaction cost
                exit_val = cc_strike * stock.lot_size
                exit_txn = exit_val * TXN_COST_ASSIGNMENT
                total_txn_costs += np.where(called, exit_txn, 0)

                m_income += np.where(called, gain_total - cg_tax, 0)
                num_calls_away += np.where(called, 1, 0).astype(np.int32)

                # Reset to CSP
                state = np.where(called, 0, state)
                cost_basis = np.where(called, 0, cost_basis)
                months_holding = np.where(called, 0, months_holding)
                cc_strike = np.where(called, 0, cc_strike)

            # --- For not-called sims: update CC strike for next month ---
            still_holding = (state == 1)
            if np.any(still_holding):
                cc_otm = rng.uniform(0.03, 0.05, n_sims)
                base_for_cc = np.maximum(cost_basis, spot_end)
                raw_cc = base_for_cc * (1 + cc_otm)
                rounded_cc = round_to_strike_vec(raw_cc, stock.strike_interval)
                too_low = rounded_cc <= cost_basis
                rounded_cc = np.where(
                    too_low,
                    round_to_strike_vec(cost_basis, stock.strike_interval) + stock.strike_interval,
                    rounded_cc,
                )
                cc_strike = np.where(still_holding, rounded_cc, cc_strike)

        monthly_income[m] = m_income

        # Portfolio value for this slot: cash value of the slot
        # CSP: capital locked = strike * lot_size (cash secured)
        # Holding: shares at market value
        csp_val = stock.csp_strike * stock.lot_size
        hold_val = spot_end * stock.lot_size
        portfolio_values[m] = np.where(state == 0, csp_val, hold_val)

    total_tax = tax_fno + tax_div + tax_stcg + tax_ltcg

    return {
        "total_csp_premium": total_csp_premium,
        "total_cc_premium": total_cc_premium,
        "total_dividend": total_dividend,
        "total_cap_gains": total_cap_gains,
        "total_txn_costs": total_txn_costs,
        "total_tax": total_tax,
        "tax_fno": tax_fno,
        "tax_div": tax_div,
        "tax_stcg": tax_stcg,
        "tax_ltcg": tax_ltcg,
        "num_assignments": num_assignments,
        "num_calls_away": num_calls_away,
        "monthly_income": monthly_income,     # (num_months, n_sims)
        "portfolio_values": portfolio_values,  # (num_months, n_sims)
    }


# ============================================================
# FULL PORTFOLIO SIMULATION
# ============================================================

def run_portfolio_simulation(
    capital_label: str,
    scenario: Scenario,
    years: int,
    n_sims: int = NUM_SIMULATIONS,
) -> dict:
    """
    Run the full wheel portfolio simulation for one capital/scenario/horizon combo.
    Vectorized: all n_sims run simultaneously.
    """
    total_capital = CAPITAL_LEVELS[capital_label]
    max_active = MAX_ACTIVE_STOCKS[capital_label]
    num_months = years * MONTHS_PER_YEAR

    active_capital = total_capital * ACTIVE_FRACTION
    reserve_capital = total_capital * RESERVE_FRACTION

    # Select stocks by ascending lot value (most affordable first)
    sorted_stocks = sorted(STOCKS, key=lambda s: s.spot * s.lot_size)
    active_stocks = sorted_stocks[:max_active]

    # Compute capital locked per slot (CSP strike * lot_size)
    slot_capital_locked = sum(stk.csp_strike * stk.lot_size for stk in active_stocks)
    # Undeployed active capital: active capital that can't fit into slots
    # This earns the reserve rate (LIQUIDBEES) alongside the reserve capital
    undeployed_capital = max(active_capital - slot_capital_locked, 0.0)

    # Simulate each stock slot independently
    rng = np.random.default_rng(RNG_SEED)
    slot_results = []
    for stk in active_stocks:
        slot_rng = np.random.default_rng(rng.integers(0, 2**31))
        result = simulate_wheel_slot(stk, scenario, num_months, n_sims, slot_rng)
        slot_results.append(result)

    # Aggregate across all slots
    total_premium = np.zeros(n_sims)
    total_csp_prem = np.zeros(n_sims)
    total_cc_prem = np.zeros(n_sims)
    total_dividend = np.zeros(n_sims)
    total_cap_gains = np.zeros(n_sims)
    total_txn = np.zeros(n_sims)
    total_tax = np.zeros(n_sims)
    tax_fno_all = np.zeros(n_sims)
    tax_div_all = np.zeros(n_sims)
    tax_stcg_all = np.zeros(n_sims)
    tax_ltcg_all = np.zeros(n_sims)
    total_assignments = np.zeros(n_sims, dtype=np.int32)
    total_calls_away = np.zeros(n_sims, dtype=np.int32)
    agg_monthly_income = np.zeros((num_months, n_sims))
    agg_portfolio_values = np.zeros((num_months, n_sims))

    for sr in slot_results:
        total_csp_prem += sr["total_csp_premium"]
        total_cc_prem += sr["total_cc_premium"]
        total_premium += sr["total_csp_premium"] + sr["total_cc_premium"]
        total_dividend += sr["total_dividend"]
        total_cap_gains += sr["total_cap_gains"]
        total_txn += sr["total_txn_costs"]
        total_tax += sr["total_tax"]
        tax_fno_all += sr["tax_fno"]
        tax_div_all += sr["tax_div"]
        tax_stcg_all += sr["tax_stcg"]
        tax_ltcg_all += sr["tax_ltcg"]
        total_assignments += sr["num_assignments"]
        total_calls_away += sr["num_calls_away"]
        agg_monthly_income += sr["monthly_income"]
        agg_portfolio_values += sr["portfolio_values"]

    # Reserve + undeployed capital growth (both earn LIQUIDBEES rate)
    idle_capital = reserve_capital + undeployed_capital
    idle_values = np.full(n_sims, idle_capital)
    idle_monthly = np.zeros((num_months, n_sims))
    for m in range(num_months):
        idle_values *= (1 + RESERVE_ANNUAL_RETURN / 12.0)
        idle_monthly[m] = idle_values

    # Total portfolio = slot positions (marked to market) + idle capital + cumulative net income
    cum_net_income = np.cumsum(agg_monthly_income, axis=0)
    total_portfolio = agg_portfolio_values + idle_monthly + cum_net_income

    # Final portfolio value
    final_portfolio = total_portfolio[-1] if num_months > 0 else np.full(n_sims, total_capital)

    # Net return
    net_return = final_portfolio - total_capital
    net_return_pct = (net_return / total_capital) * 100

    # CAGR
    ratio = final_portfolio / total_capital
    ratio = np.maximum(ratio, 1e-10)  # Avoid log(0)
    cagr = (np.power(ratio, 1.0 / years) - 1.0) * 100

    # Max drawdown per sim
    max_dd = _compute_max_drawdown_vec(total_portfolio)

    # Monthly income as list of lists for charts
    monthly_income_lists = [agg_monthly_income[:, i].tolist() for i in range(n_sims)]

    # Portfolio path as list of lists
    portfolio_path_lists = [total_portfolio[:, i].tolist() for i in range(n_sims)]

    return {
        "final_value": final_portfolio,
        "net_return_pct": net_return_pct,
        "cagr": cagr,
        "total_premium": total_premium,
        "total_csp_premium": total_csp_prem,
        "total_cc_premium": total_cc_prem,
        "total_dividend": total_dividend,
        "total_cap_gains": total_cap_gains,
        "total_txn_costs": total_txn,
        "total_tax": total_tax,
        "tax_fno": tax_fno_all,
        "tax_div": tax_div_all,
        "tax_stcg": tax_stcg_all,
        "tax_ltcg": tax_ltcg_all,
        "num_assignments": total_assignments,
        "num_calls_away": total_calls_away,
        "max_drawdown": max_dd,
        "monthly_incomes": monthly_income_lists,
        "portfolio_paths": portfolio_path_lists,
        "agg_monthly_income": agg_monthly_income,   # (months, sims) for charts
        "total_portfolio": total_portfolio,          # (months, sims) for charts
    }


def _compute_max_drawdown_vec(portfolio: np.ndarray) -> np.ndarray:
    """
    Compute max drawdown for each sim from portfolio array (months x sims).
    Returns array of shape (n_sims,).
    """
    running_max = np.maximum.accumulate(portfolio, axis=0)
    drawdowns = (running_max - portfolio) / np.maximum(running_max, 1e-10)
    return np.max(drawdowns, axis=0)


# ============================================================
# STATISTICS
# ============================================================

def compute_statistics(result: dict, total_capital: float, years: int) -> dict:
    """Compute summary statistics from a batch result."""
    cagrs = result["cagr"]
    fv = result["final_value"]

    fd_target = total_capital * (1.049 ** years)
    nifty_target = total_capital * (1.105 ** years)

    return {
        "mean_cagr": float(np.mean(cagrs)),
        "median_cagr": float(np.median(cagrs)),
        "p5_cagr": float(np.percentile(cagrs, 5)),
        "p95_cagr": float(np.percentile(cagrs, 95)),
        "p1_cagr": float(np.percentile(cagrs, 1)),
        "worst_cagr": float(np.percentile(cagrs, 1)),
        "mean_final": float(np.mean(fv)),
        "median_final": float(np.median(fv)),
        "p5_final": float(np.percentile(fv, 5)),
        "p95_final": float(np.percentile(fv, 95)),
        "prob_positive": float(np.mean(fv > total_capital) * 100),
        "prob_beat_fd": float(np.mean(fv > fd_target) * 100),
        "prob_beat_nifty": float(np.mean(fv > nifty_target) * 100),
        "mean_max_dd": float(np.mean(result["max_drawdown"])),
        "mean_premium": float(np.mean(result["total_premium"])),
        "mean_csp_premium": float(np.mean(result["total_csp_premium"])),
        "mean_cc_premium": float(np.mean(result["total_cc_premium"])),
        "mean_dividend": float(np.mean(result["total_dividend"])),
        "mean_cap_gains": float(np.mean(result["total_cap_gains"])),
        "mean_txn_costs": float(np.mean(result["total_txn_costs"])),
        "mean_tax": float(np.mean(result["total_tax"])),
        "mean_assignments": float(np.mean(result["num_assignments"])),
        "mean_calls_away": float(np.mean(result["num_calls_away"])),
        "all_cagrs": cagrs,
        "all_final_values": fv,
        "all_assignments": result["num_assignments"],
    }


# ============================================================
# FORMATTING
# ============================================================

def format_inr(amount: float) -> str:
    if abs(amount) >= 1_00_00_000:
        return f"Rs {amount / 1_00_00_000:,.2f} Cr"
    elif abs(amount) >= 1_00_000:
        return f"Rs {amount / 1_00_000:,.2f} L"
    else:
        return f"Rs {amount:,.0f}"


def format_pct(value: float) -> str:
    return f"{value:.1f}%"


# ============================================================
# CONSOLE OUTPUT
# ============================================================

def print_section(text: str, width: int = 80):
    print()
    print("=" * width)
    print(f"  {text}")
    print("=" * width)


def print_results_table(
    stats_by_scenario: Dict[str, dict],
    capital_label: str,
    total_capital: float,
    years: int,
):
    print(f"\n{'~' * 80}")
    print(f"  Capital: {format_inr(total_capital)} | Horizon: {years} year(s)")
    print(f"{'~' * 80}")

    scenarios = list(stats_by_scenario.keys())
    col_w = 14
    header = f"  {'Metric':<28}"
    for s in scenarios:
        header += f" {s:>{col_w}}"
    print(header)
    print("  " + "-" * (28 + (col_w + 1) * len(scenarios)))

    rows = [
        ("Mean CAGR (after-tax)", "mean_cagr", format_pct),
        ("Median CAGR (after-tax)", "median_cagr", format_pct),
        ("5th %ile CAGR", "p5_cagr", format_pct),
        ("95th %ile CAGR", "p95_cagr", format_pct),
        ("Worst-case (1st %ile)", "worst_cagr", format_pct),
        ("P(positive return)", "prob_positive", format_pct),
        ("P(beat FD 4.9%)", "prob_beat_fd", format_pct),
        ("P(beat NIFTY 10.5%)", "prob_beat_nifty", format_pct),
        ("Avg max drawdown", "mean_max_dd", lambda x: format_pct(x * 100)),
        ("Avg assignments", "mean_assignments", lambda x: f"{x:.1f}"),
        ("Avg calls away", "mean_calls_away", lambda x: f"{x:.1f}"),
    ]

    for label, key, fmt in rows:
        line = f"  {label:<28}"
        for s in scenarios:
            val = stats_by_scenario[s].get(key, 0)
            line += f" {fmt(val):>{col_w}}"
        print(line)

    print()
    print(f"  {'--- Income Breakdown ---':<28}")
    income_rows = [
        ("Avg CSP premium", "mean_csp_premium", format_inr),
        ("Avg CC premium", "mean_cc_premium", format_inr),
        ("Avg dividend income", "mean_dividend", format_inr),
        ("Avg capital gains", "mean_cap_gains", format_inr),
        ("Avg txn costs", "mean_txn_costs", format_inr),
        ("Avg total tax", "mean_tax", format_inr),
    ]
    for label, key, fmt in income_rows:
        line = f"  {label:<28}"
        for s in scenarios:
            val = stats_by_scenario[s].get(key, 0)
            line += f" {fmt(val):>{col_w}}"
        print(line)


# ============================================================
# CHARTS
# ============================================================

def create_all_charts(all_stats: dict, all_results: dict, capital_label: str = "50L"):
    os.makedirs(CHARTS_DIR, exist_ok=True)
    _chart_1_return_distribution(all_stats, capital_label)
    _chart_2_wealth_growth(all_results, capital_label)
    _chart_3_comparison(all_stats, capital_label)
    _chart_4_monthly_income(all_results, capital_label)
    _chart_5_assignment_frequency(all_stats, capital_label)
    print(f"\n  Charts saved to: {CHARTS_DIR}/")


def _chart_1_return_distribution(all_stats: dict, capital_label: str):
    """Chart 1: Distribution of annual returns (histogram) for all scenarios."""
    fig, axes = plt.subplots(2, 3, figsize=(18, 10))
    axes = axes.flatten()
    horizon = 5
    colors = ["#2196F3", "#4CAF50", "#F44336", "#FF9800", "#9C27B0"]

    for idx, (scenario, color) in enumerate(zip(SCENARIOS, colors)):
        key = (capital_label, scenario.name, horizon)
        if key not in all_stats:
            continue
        stats = all_stats[key]
        cagrs = stats["all_cagrs"]

        ax = axes[idx]
        ax.hist(cagrs, bins=80, color=color, alpha=0.75, edgecolor="white", linewidth=0.5)
        ax.axvline(np.median(cagrs), color="black", linestyle="--", linewidth=1.5,
                   label=f"Median: {np.median(cagrs):.1f}%")
        ax.axvline(np.percentile(cagrs, 5), color="red", linestyle=":", linewidth=1,
                   label=f"P5: {np.percentile(cagrs, 5):.1f}%")
        ax.axvline(np.percentile(cagrs, 95), color="green", linestyle=":", linewidth=1,
                   label=f"P95: {np.percentile(cagrs, 95):.1f}%")
        ax.set_title(f"{scenario.name} ({scenario.description})", fontsize=11, fontweight="bold")
        ax.set_xlabel("Annual Return (CAGR %)")
        ax.set_ylabel("Frequency")
        ax.legend(fontsize=8)
        ax.grid(axis="y", alpha=0.3)

    axes[5].set_visible(False)
    fig.suptitle(
        f"Wheel Strategy: Distribution of After-Tax Annual Returns\n"
        f"Capital: {format_inr(CAPITAL_LEVELS[capital_label])}, Horizon: {horizon}Y, "
        f"{NUM_SIMULATIONS:,} simulations",
        fontsize=13, fontweight="bold"
    )
    plt.tight_layout(rect=[0, 0, 1, 0.93])
    path = os.path.join(CHARTS_DIR, "1_return_distribution.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"    Saved: {path}")


def _chart_2_wealth_growth(all_results: dict, capital_label: str):
    """Chart 2: Wealth growth over 5Y with percentile bands (Base scenario)."""
    horizon = 5
    key = (capital_label, "Base", horizon)
    if key not in all_results:
        return
    result = all_results[key]
    total_capital = CAPITAL_LEVELS[capital_label]

    portfolio = result["total_portfolio"]  # (months, sims)
    months = np.arange(1, portfolio.shape[0] + 1)

    p5 = np.percentile(portfolio, 5, axis=1)
    p25 = np.percentile(portfolio, 25, axis=1)
    median = np.median(portfolio, axis=1)
    mean = np.mean(portfolio, axis=1)
    p75 = np.percentile(portfolio, 75, axis=1)
    p95 = np.percentile(portfolio, 95, axis=1)

    fig, ax = plt.subplots(figsize=(14, 8))
    ax.fill_between(months, p5, p95, alpha=0.15, color="#2196F3", label="5th-95th percentile")
    ax.fill_between(months, p25, p75, alpha=0.30, color="#2196F3", label="25th-75th percentile")
    ax.plot(months, median, color="#1565C0", linewidth=2.5, label="Median")
    ax.plot(months, mean, color="#F44336", linewidth=1.5, linestyle="--", label="Mean")
    ax.axhline(total_capital, color="gray", linestyle=":", linewidth=1, alpha=0.7, label="Starting Capital")

    fd_values = [total_capital * (1.049) ** (m / 12) for m in months]
    ax.plot(months, fd_values, color="#FF9800", linewidth=1.5, linestyle="-.", label="FD (4.9% post-tax)")

    ax.set_xlabel("Month", fontsize=12)
    ax.set_ylabel("Portfolio Value (Rs)", fontsize=12)
    ax.set_title(
        f"Wheel Strategy: Wealth Growth Over {horizon} Years (Base Scenario)\n"
        f"Capital: {format_inr(total_capital)}, {NUM_SIMULATIONS:,} simulations",
        fontsize=13, fontweight="bold"
    )
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: format_inr(x)))
    ax.legend(loc="upper left", fontsize=10)
    ax.grid(alpha=0.3)
    plt.tight_layout()
    path = os.path.join(CHARTS_DIR, "2_wealth_growth.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"    Saved: {path}")


def _chart_3_comparison(all_stats: dict, capital_label: str):
    """Chart 3: Wheel vs FD vs NIFTY SIP (5-year)."""
    horizon = 5
    total_capital = CAPITAL_LEVELS[capital_label]

    fig, ax = plt.subplots(figsize=(12, 7))
    scenarios_to_plot = [s.name for s in SCENARIOS]
    colors = ["#2196F3", "#4CAF50", "#F44336", "#FF9800", "#9C27B0"]

    bar_width = 0.12
    x = np.arange(4)  # Median, P5, P95, Mean

    for idx, (scen_name, color) in enumerate(zip(scenarios_to_plot, colors)):
        key = (capital_label, scen_name, horizon)
        if key not in all_stats:
            continue
        stats = all_stats[key]
        values = [
            stats["median_final"],
            stats["p5_final"],
            stats["p95_final"],
            stats["mean_final"],
        ]
        ax.bar(x + idx * bar_width, [v / 1_00_000 for v in values],
               bar_width, color=color, alpha=0.85, label=scen_name)

    fd_final = total_capital * (1.049 ** horizon)
    nifty_final = total_capital * (1.105 ** horizon)

    ax.axhline(fd_final / 1_00_000, color="#795548", linestyle="--", linewidth=2,
               label=f"FD (4.9%): {format_inr(fd_final)}")
    ax.axhline(nifty_final / 1_00_000, color="#FF5722", linestyle="--", linewidth=2,
               label=f"NIFTY (10.5%): {format_inr(nifty_final)}")
    ax.axhline(total_capital / 1_00_000, color="gray", linestyle=":", linewidth=1, label="Starting Capital")

    ax.set_xticks(x + bar_width * 2)
    ax.set_xticklabels(["Median", "P5 (Worst 5%)", "P95 (Best 5%)", "Mean"])
    ax.set_ylabel("Final Portfolio Value (Rs Lakhs)", fontsize=11)
    ax.set_title(
        f"Wheel Strategy vs Benchmarks: {horizon}-Year Comparison\n"
        f"Capital: {format_inr(total_capital)}",
        fontsize=13, fontweight="bold"
    )
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(axis="y", alpha=0.3)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.0f}L"))
    plt.tight_layout()
    path = os.path.join(CHARTS_DIR, "3_comparison_wheel_vs_benchmarks.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"    Saved: {path}")


def _chart_4_monthly_income(all_results: dict, capital_label: str):
    """Chart 4: Monthly income distribution (box plot) - Base scenario, 5Y."""
    horizon = 5
    key = (capital_label, "Base", horizon)
    if key not in all_results:
        return
    result = all_results[key]
    total_capital = CAPITAL_LEVELS[capital_label]

    monthly_inc = result["agg_monthly_income"]  # (months, sims)
    num_months = monthly_inc.shape[0]

    # Group by quarter
    quarterly_data = []
    quarter_labels = []
    for q_start in range(0, num_months, 3):
        q_end = min(q_start + 3, num_months)
        combined = monthly_inc[q_start:q_end].flatten()
        quarterly_data.append(combined)
        quarter_labels.append(f"Q{(q_start // 3) + 1}")

    fig, ax = plt.subplots(figsize=(16, 7))
    bp = ax.boxplot(
        quarterly_data,
        patch_artist=True,
        showfliers=False,
        medianprops=dict(color="red", linewidth=2),
        boxprops=dict(facecolor="#E3F2FD", edgecolor="#1565C0"),
        whiskerprops=dict(color="#1565C0"),
        capprops=dict(color="#1565C0"),
    )
    ax.set_xticklabels(quarter_labels, fontsize=8, rotation=45)
    ax.set_xlabel("Quarter", fontsize=11)
    ax.set_ylabel("Monthly Income (Rs)", fontsize=11)
    ax.set_title(
        f"Wheel Strategy: Monthly Income Distribution (Base Scenario)\n"
        f"Capital: {format_inr(total_capital)}, {horizon}Y, {NUM_SIMULATIONS:,} sims",
        fontsize=13, fontweight="bold"
    )
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: format_inr(x)))
    ax.grid(axis="y", alpha=0.3)
    ax.axhline(0, color="gray", linewidth=0.5)
    plt.tight_layout()
    path = os.path.join(CHARTS_DIR, "4_monthly_income_distribution.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"    Saved: {path}")


def _chart_5_assignment_frequency(all_stats: dict, capital_label: str):
    """Chart 5: Assignment frequency distribution."""
    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    colors = ["#2196F3", "#4CAF50", "#F44336"]

    for ax, h, color in zip(axes, HORIZONS, colors):
        key = (capital_label, "Base", h)
        if key not in all_stats:
            continue
        stats = all_stats[key]
        assignments = stats["all_assignments"]
        max_val = int(np.max(assignments)) + 1
        bins = np.arange(-0.5, max_val + 1, 1)
        ax.hist(assignments, bins=bins, color=color, alpha=0.75, edgecolor="white", linewidth=0.5)
        ax.axvline(np.mean(assignments), color="black", linestyle="--", linewidth=1.5,
                   label=f"Mean: {np.mean(assignments):.1f}")
        ax.set_xlabel("Number of Assignments")
        ax.set_ylabel("Frequency")
        ax.set_title(f"{h}-Year Horizon", fontsize=11, fontweight="bold")
        ax.legend(fontsize=9)
        ax.grid(axis="y", alpha=0.3)

    fig.suptitle(
        f"Wheel Strategy: Assignment Frequency (Base Scenario)\n"
        f"Capital: {format_inr(CAPITAL_LEVELS[capital_label])}, {NUM_SIMULATIONS:,} sims",
        fontsize=13, fontweight="bold"
    )
    plt.tight_layout(rect=[0, 0, 1, 0.90])
    path = os.path.join(CHARTS_DIR, "5_assignment_frequency.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"    Saved: {path}")


# ============================================================
# MARKDOWN REPORT
# ============================================================

def generate_markdown(all_stats: dict) -> str:
    lines = []
    lines.append("# Wheel Strategy Monte Carlo Simulation Results")
    lines.append("")
    lines.append(f"**Generated:** {time.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"**Simulations:** {NUM_SIMULATIONS:,} per scenario")
    lines.append(f"**Horizons:** {', '.join(str(h) + 'Y' for h in HORIZONS)}")
    lines.append(f"**Stocks:** COALINDIA, ITC, POWERGRID, SBIN, ICICIBANK")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Stock parameters
    lines.append("## Stock Parameters (Feb 2026)")
    lines.append("")
    lines.append("| Stock | Spot | Lot Size | CSP Strike | Ann. Vol | Div Yield | P/E |")
    lines.append("|-------|------|----------|------------|----------|-----------|-----|")
    for s in STOCKS:
        lines.append(f"| {s.name} | Rs {s.spot:,.0f} | {s.lot_size:,} | Rs {s.csp_strike:,.0f} | "
                     f"{s.annual_vol*100:.0f}% | {s.div_yield*100:.1f}% | {s.pe_ratio:.1f} |")
    lines.append("")

    # Scenarios
    lines.append("## Market Scenarios")
    lines.append("")
    lines.append("| Scenario | Drift | Vol Mult | Description |")
    lines.append("|----------|-------|----------|-------------|")
    for sc in SCENARIOS:
        lines.append(f"| {sc.name} | {sc.mu*100:.0f}% | {sc.vol_multiplier:.2f}x | {sc.description} |")
    lines.append("")

    # Risk events
    lines.append("## Risk Events")
    lines.append("")
    lines.append(f"- Market crash: {MARKET_CRASH_PROB*100:.0f}%/year, "
                 f"{MARKET_CRASH_MAG_MIN*100:.0f}-{MARKET_CRASH_MAG_MAX*100:.0f}% over 1 month")
    lines.append(f"- Stock blowup: {STOCK_BLOWUP_PROB*100:.0f}%/year/stock, "
                 f"{STOCK_BLOWUP_MAG_MIN*100:.0f}-{STOCK_BLOWUP_MAG_MAX*100:.0f}% over 5 days")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Results per capital level
    for cap_label in CAPITAL_LEVELS:
        total_cap = CAPITAL_LEVELS[cap_label]
        lines.append(f"## Capital: {format_inr(total_cap)}")
        lines.append("")
        lines.append(f"Active stocks: {MAX_ACTIVE_STOCKS[cap_label]} | "
                     f"Active capital: {format_inr(total_cap * ACTIVE_FRACTION)} | "
                     f"Reserve: {format_inr(total_cap * RESERVE_FRACTION)}")
        lines.append("")

        for horizon in HORIZONS:
            lines.append(f"### {horizon}-Year Horizon")
            lines.append("")

            header = "| Metric | " + " | ".join(sc.name for sc in SCENARIOS) + " |"
            sep = "|--------|" + "|".join("-" * (len(sc.name) + 2) for sc in SCENARIOS) + "|"
            lines.append(header)
            lines.append(sep)

            metrics = [
                ("Mean CAGR", "mean_cagr", lambda x: f"{x:.1f}%"),
                ("Median CAGR", "median_cagr", lambda x: f"{x:.1f}%"),
                ("5th %ile CAGR", "p5_cagr", lambda x: f"{x:.1f}%"),
                ("95th %ile CAGR", "p95_cagr", lambda x: f"{x:.1f}%"),
                ("Worst (P1)", "worst_cagr", lambda x: f"{x:.1f}%"),
                ("P(positive)", "prob_positive", lambda x: f"{x:.1f}%"),
                ("P(beat FD)", "prob_beat_fd", lambda x: f"{x:.1f}%"),
                ("P(beat NIFTY)", "prob_beat_nifty", lambda x: f"{x:.1f}%"),
                ("Avg Max DD", "mean_max_dd", lambda x: f"{x*100:.1f}%"),
                ("Avg Assignments", "mean_assignments", lambda x: f"{x:.1f}"),
                ("Avg Calls Away", "mean_calls_away", lambda x: f"{x:.1f}"),
                ("Avg Premium", "mean_premium", lambda x: format_inr(x)),
                ("Avg Dividend", "mean_dividend", lambda x: format_inr(x)),
                ("Avg Tax", "mean_tax", lambda x: format_inr(x)),
                ("Avg Txn Cost", "mean_txn_costs", lambda x: format_inr(x)),
            ]

            for label, key, fmt in metrics:
                row = f"| **{label}** |"
                for sc in SCENARIOS:
                    skey = (cap_label, sc.name, horizon)
                    if skey in all_stats:
                        val = all_stats[skey].get(key, 0)
                        row += f" {fmt(val)} |"
                    else:
                        row += " N/A |"
                lines.append(row)
            lines.append("")

        lines.append("---")
        lines.append("")

    # Tax model
    lines.append("## Tax Model")
    lines.append("")
    lines.append("| Income Type | Rate |")
    lines.append("|------------|------|")
    lines.append(f"| F&O premium (CSP+CC) | {TAX_FNO_BUSINESS*100:.0f}% (business income) |")
    lines.append(f"| Dividends | {TAX_DIVIDEND*100:.0f}% (slab) |")
    lines.append(f"| STCG (< 12 months) | {TAX_STCG*100:.0f}% |")
    lines.append(f"| LTCG (> 12 months) | {TAX_LTCG*100:.1f}% after Rs {LTCG_EXEMPTION/100_000:.2f}L exemption |")
    lines.append(f"| Transaction costs | {TXN_COST_ASSIGNMENT*100:.2f}% assignment, {TXN_COST_OPTION*100:.2f}% option |")
    lines.append("")

    # Methodology
    lines.append("## Methodology")
    lines.append("")
    lines.append("- **Price model:** GBM with daily steps (252 days/year), monthly option cycles")
    lines.append("- **Option pricing:** Black-Scholes with 5-15% bid-ask haircut")
    lines.append("- **Wheel cycle:** Monthly CSP -> assignment check -> CC -> called-away check")
    lines.append("- **CC strike:** max(cost_basis, spot) + 3-5% OTM, rounded to strike interval")
    lines.append("- **Dividends:** Quarterly while holding shares")
    lines.append("- **Reserve:** 40% of capital at 5.5% annual (LIQUIDBEES)")
    lines.append(f"- **Risk events:** Market crash ({MARKET_CRASH_PROB*100:.0f}%/yr), "
                 f"stock blowup ({STOCK_BLOWUP_PROB*100:.0f}%/yr/stock)")
    lines.append(f"- **Simulations:** {NUM_SIMULATIONS:,} per scenario, fully vectorized")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*Generated by `playbook/wheel_monte_carlo.py`*")

    return "\n".join(lines)


# ============================================================
# MAIN
# ============================================================

def main():
    start_time = time.time()

    print("=" * 80)
    print("  WHEEL STRATEGY MONTE CARLO SIMULATION")
    print("  NSE India | COALINDIA, ITC, POWERGRID, SBIN, ICICIBANK")
    print(f"  Simulations: {NUM_SIMULATIONS:,} | Horizons: {HORIZONS}")
    print(f"  Capital: {', '.join(format_inr(v) for v in CAPITAL_LEVELS.values())}")
    print(f"  Scenarios: {', '.join(s.name for s in SCENARIOS)}")
    print("=" * 80)

    all_stats: Dict[tuple, dict] = {}
    all_results: Dict[tuple, dict] = {}

    total_combos = len(CAPITAL_LEVELS) * len(SCENARIOS) * len(HORIZONS)
    combo_num = 0

    for cap_label, total_cap in CAPITAL_LEVELS.items():
        for scenario in SCENARIOS:
            for horizon in HORIZONS:
                combo_num += 1
                tag = f"[{combo_num}/{total_combos}] {cap_label} | {scenario.name} | {horizon}Y"
                print(f"\n  {tag} ({NUM_SIMULATIONS:,} sims)...", end=" ", flush=True)

                t0 = time.time()
                result = run_portfolio_simulation(cap_label, scenario, horizon)
                stats = compute_statistics(result, total_cap, horizon)

                key = (cap_label, scenario.name, horizon)
                all_stats[key] = stats
                all_results[key] = result

                elapsed = time.time() - t0
                print(f"done ({elapsed:.1f}s) | Median CAGR: {stats['median_cagr']:.1f}%")

    # Console tables
    for cap_label, total_cap in CAPITAL_LEVELS.items():
        print_section(f"RESULTS: {format_inr(total_cap)}")
        for horizon in HORIZONS:
            by_scen = {}
            for sc in SCENARIOS:
                k = (cap_label, sc.name, horizon)
                if k in all_stats:
                    by_scen[sc.name] = all_stats[k]
            print_results_table(by_scen, cap_label, total_cap, horizon)

    # Benchmarks
    print_section("BENCHMARKS")
    print()
    for horizon in HORIZONS:
        fd = CAPITAL_LEVELS["50L"] * (1.049 ** horizon)
        nifty = CAPITAL_LEVELS["50L"] * (1.105 ** horizon)
        print(f"  {horizon}Y @ Rs 50L:  FD (4.9%) = {format_inr(fd)}"
              f"  |  NIFTY (10.5%) = {format_inr(nifty)}")

    # Charts
    print_section("GENERATING CHARTS (Rs 50L)")
    create_all_charts(all_stats, all_results, capital_label="50L")

    # Markdown report
    print_section("GENERATING REPORT")
    md = generate_markdown(all_stats)
    with open(RESULTS_MD_PATH, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"  Saved: {RESULTS_MD_PATH}")

    # Summary
    total_elapsed = time.time() - start_time
    total_sims = total_combos * NUM_SIMULATIONS
    print_section("COMPLETE")
    print(f"  Total simulations: {total_sims:,}")
    print(f"  Total time: {total_elapsed:.1f}s ({total_sims / max(total_elapsed, 0.1):,.0f} sims/sec)")
    print(f"  Results: {RESULTS_MD_PATH}")
    print(f"  Charts:  {CHARTS_DIR}/")
    print()

    base_50l_5y = all_stats.get(("50L", "Base", 5))
    if base_50l_5y:
        print(f"  HEADLINE (Rs 50L, Base, 5Y):")
        print(f"    Median CAGR:      {base_50l_5y['median_cagr']:.1f}%")
        print(f"    P(beat FD):       {base_50l_5y['prob_beat_fd']:.1f}%")
        print(f"    P(beat NIFTY):    {base_50l_5y['prob_beat_nifty']:.1f}%")
        print(f"    Avg Max Drawdown: {base_50l_5y['mean_max_dd']*100:.1f}%")
        print(f"    Avg Assignments:  {base_50l_5y['mean_assignments']:.1f} (over 5 years)")
    print()


if __name__ == "__main__":
    main()
