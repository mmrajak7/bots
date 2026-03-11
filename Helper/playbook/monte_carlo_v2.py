"""
Monte Carlo Simulation V2: Dynamic Pool Model (Tax-Corrected)
=============================================================
Models the final playbook with dynamic pool draws, per-asset caps,
and realistic multi-asset deployment.

Tax corrections (v2.1):
1. Dividend/distribution income taxed at 30% slab ANNUALLY (not deferred LTCG)
   - REITs: ~65% of distribution taxable at slab, ~35% return of capital
   - Stocks: dividends fully taxable at slab
   - SGB: 2.5% interest fully taxable at slab
   - Growth ETFs: zero dividends (all price appreciation) -- no annual drag
2. LTCG 1.25L exemption applied once (parking already consumes it)
3. Loss exits: check if still net profitable after haircut
4. Parking return corrected to 5.6% post-tax blended
5. Entry timing: 50/50 chance of buying too early vs catching bounce
6. Swing NIFTYBEES respects 20% per-ETF cap

10,000 simulations x 20 years.
"""

import numpy as np
from dataclasses import dataclass
from typing import Dict

np.random.seed(42)

# ============================================================
# PARAMETERS
# ============================================================

STARTING_CAPITAL = 3_00_00_000  # 3 Crore
SWING_CAPITAL = 50_00_000       # 50L (separate)
NUM_SIMULATIONS = 10_000
YEARS_TO_REPORT = [5, 10, 15, 20]
MAX_YEARS = 20
RESERVE_PCT = 0.10              # 10% always in Tier 1

# --- Tier 1: Parking Returns (post-tax blended) ---
# LIQUIDBEES: 5.3% pre-tax, 30% slab -> 3.71% post-tax
# Arbitrage: 7.0% pre-tax, 12.5% LTCG (no exemption, consumed centrally) -> 6.125%
# Blended: 0.20 * 3.71 + 0.80 * 6.125 = 5.64%
PARKING_RETURN_MEAN = 0.056
PARKING_RETURN_STD = 0.005

# --- Tier 3: Swing Trading ---
SWING_RETURN_MEAN = 0.18
SWING_RETURN_STD = 0.12
SWING_TAX = 0.20                # STCG on swing profits

# --- Tax Rates ---
TAX_LTCG = 0.125                # Long-term capital gains (equity, > 12 months)
TAX_STCG = 0.20                 # Short-term capital gains (equity, < 12 months)
TAX_SLAB = 0.30                 # Income tax slab rate (dividends, interest, REIT distributions)
# NOTE: LTCG 1.25L annual exemption NOT applied to position exits
# because parking (arbitrage fund) redemptions already consume it.
# At 2Cr+ parked, arbitrage gains alone exceed 1.25L/year.


# ============================================================
# ASSET DEFINITIONS
# ============================================================

@dataclass
class AssetConfig:
    name: str
    category: str               # core_etf, sector_etf, smart_beta, international, gold, silver, reit, stock
    draw_stage1: float          # % of pool for Weekly ST (Stage 1). 0 = no Stage 1.
    draw_stage2: float          # % of pool for Monthly ST (Stage 2)
    max_pct_corpus: float       # Hard cap as % of total corpus
    return_mean: float          # TOTAL annual return (price appreciation + income)
    return_std: float           # Annual volatility (applied to price component)
    dividend_yield: float       # Income component: dividends/distributions/interest (annual %)
    income_taxable_pct: float   # Fraction of income taxed at slab rate (1.0 = full, 0.65 = REITs)
    correction_prob: float      # Probability this asset has a correction in any year
    monthly_st_prob: float      # Probability Monthly ST fires given Weekly ST fired
    exit_prob: float            # Annual probability of Monthly ST flip (exit signal)
    loss_exit_pct: float        # % of exits that are at a loss


# --- ASSET DEFINITIONS WITH DIVIDEND YIELDS ---

# Core ETFs - Growth funds, dividends reinvested in NAV, NO annual income tax
CORE_ETFS = [
    #                                              draw1  draw2  cap   ret   std   div   tax%  corr  mst   exit  loss
    AssetConfig("NIFTYBEES",   "core_etf",         0.15,  0.20,  0.20, 0.13, 0.18, 0.0,  0.0,  0.38, 0.55, 0.25, 0.15),
    AssetConfig("JUNIORBEES",  "core_etf",         0.15,  0.20,  0.20, 0.14, 0.22, 0.0,  0.0,  0.38, 0.60, 0.25, 0.18),
    AssetConfig("MID150BEES",  "core_etf",         0.15,  0.20,  0.20, 0.15, 0.25, 0.0,  0.0,  0.40, 0.65, 0.25, 0.20),
]

# Sector ETFs - Growth funds, no dividends
SECTOR_ETFS = [
    AssetConfig("ITBEES",      "sector_etf",       0.05,  0.08,  0.20, 0.13, 0.22, 0.0,  0.0,  0.35, 0.55, 0.25, 0.18),
    AssetConfig("PHARMABEES",  "sector_etf",       0.05,  0.08,  0.20, 0.12, 0.20, 0.0,  0.0,  0.30, 0.50, 0.28, 0.15),
    AssetConfig("PSUBNKBEES",  "sector_etf",       0.05,  0.08,  0.20, 0.14, 0.28, 0.0,  0.0,  0.35, 0.60, 0.22, 0.22),
    AssetConfig("CPSEETF",     "sector_etf",       0.05,  0.08,  0.20, 0.13, 0.22, 0.0,  0.0,  0.32, 0.55, 0.25, 0.18),
]

# Smart Beta - Growth, no dividends
SMART_BETA = [
    AssetConfig("MOM30ETF",    "smart_beta",       0.05,  0.08,  0.20, 0.15, 0.24, 0.0,  0.0,  0.38, 0.58, 0.25, 0.20),
    AssetConfig("ALPHA",       "smart_beta",       0.05,  0.08,  0.20, 0.14, 0.23, 0.0,  0.0,  0.38, 0.58, 0.25, 0.20),
]

# International - Growth, no dividends
INTERNATIONAL = [
    AssetConfig("MON100",      "international",    0.05,  0.10,  0.20, 0.12, 0.20, 0.0,  0.0,  0.30, 0.50, 0.28, 0.15),
]

# Commodities
# GOLD: Blended 60% SGB (2.5% interest, taxed at slab) + 40% GOLDBEES (no income)
# Effective dividend_yield = 0.60 * 0.025 = 0.015, fully taxable at slab
# SILVER: No income component
COMMODITIES = [
    AssetConfig("GOLD",        "gold",             0.05,  0.08,  0.15, 0.10, 0.14, 0.015, 1.0, 0.25, 0.45, 0.30, 0.12),
    AssetConfig("SILVER",      "silver",           0.05,  0.08,  0.15, 0.09, 0.22, 0.0,   0.0, 0.30, 0.55, 0.25, 0.18),
]

# REITs - Significant distribution yield
# Distribution breakdown: ~65% taxable at slab (interest + dividend), ~35% return of capital
# Typical yields: Embassy 5.2%, Mindspace 4.8%, Biret 5.8%, Nexpoint 5.6%
REITS = [
    AssetConfig("EMBASSY",     "reit",             0.03,  0.05,  0.12, 0.11, 0.15, 0.052, 0.65, 0.28, 0.45, 0.30, 0.12),
    AssetConfig("MINDSPACE",   "reit",             0.03,  0.05,  0.12, 0.12, 0.16, 0.048, 0.65, 0.28, 0.45, 0.30, 0.12),
    AssetConfig("BIRET",       "reit",             0.03,  0.05,  0.12, 0.11, 0.17, 0.058, 0.65, 0.28, 0.45, 0.30, 0.14),
    AssetConfig("NXST",        "reit",             0.03,  0.05,  0.12, 0.12, 0.18, 0.056, 0.65, 0.28, 0.45, 0.30, 0.14),
]

# Stocks (Monthly ST only, no Stage 1)
# Average dividend yield ~2.5% for quality stock universe (mix of high-yield and growth)
# Dividends fully taxable at slab rate
STOCKS = [
    AssetConfig("STOCK_1",     "stock",            0.0,   0.03,  0.03, 0.14, 0.25, 0.025, 1.0, 0.35, 1.0,  0.22, 0.22),
    AssetConfig("STOCK_2",     "stock",            0.0,   0.03,  0.03, 0.14, 0.25, 0.025, 1.0, 0.35, 1.0,  0.22, 0.22),
    AssetConfig("STOCK_3",     "stock",            0.0,   0.03,  0.03, 0.14, 0.25, 0.025, 1.0, 0.35, 1.0,  0.22, 0.22),
    AssetConfig("STOCK_4",     "stock",            0.0,   0.03,  0.03, 0.14, 0.25, 0.025, 1.0, 0.30, 1.0,  0.22, 0.22),
    AssetConfig("STOCK_5",     "stock",            0.0,   0.03,  0.03, 0.14, 0.25, 0.025, 1.0, 0.30, 1.0,  0.22, 0.22),
]

ALL_ASSETS = CORE_ETFS + SECTOR_ETFS + SMART_BETA + INTERNATIONAL + COMMODITIES + REITS + STOCKS

# Category caps (% of total corpus)
CATEGORY_CAPS = {
    "equity": 0.60,         # core_etf + sector_etf + smart_beta + international + stock
    "gold_silver": 0.15,
    "reit": 0.12,
}

EQUITY_CATEGORIES = {"core_etf", "sector_etf", "smart_beta", "international", "stock"}


def get_category_deployed(positions: Dict[str, float], category_set: set, assets: list) -> float:
    """Sum deployed capital across assets in a category set."""
    total = 0.0
    for a in assets:
        if a.category in category_set and a.name in positions:
            total += positions[a.name]
    return total


def simulate_one_run(years: int, equity_return_override: float = None,
                     correction_prob_mult: float = 1.0) -> dict:
    """Simulate one complete run with proper tax treatment."""

    pool = STARTING_CAPITAL * (1 - RESERVE_PCT)     # Deployable pool
    reserve = STARTING_CAPITAL * RESERVE_PCT         # 10% reserve
    positions: Dict[str, float] = {}                 # asset_name -> current market value
    position_age: Dict[str, int] = {}                # asset_name -> months held
    position_cost: Dict[str, float] = {}             # asset_name -> cost basis
    stage1_deployed: Dict[str, bool] = {}            # track if Stage 1 already done

    total_corpus = STARTING_CAPITAL
    tax_income = 0.0            # Tax on dividends/distributions/interest (annual, slab rate)
    tax_capital_gains = 0.0     # Tax on LTCG/STCG (on position exits)
    tax_swing = 0.0             # Tax on swing trading profits
    total_swing_added = 0.0
    total_dividends_received = 0.0
    deployment_count = 0
    exit_count = 0
    loss_exits = 0

    yearly_values = []

    for year in range(years):
        # --- Tier 1: Parking returns on pool + reserve ---
        parking_return = max(np.random.normal(PARKING_RETURN_MEAN, PARKING_RETURN_STD), 0.02)
        pool *= (1 + parking_return)
        reserve *= (1 + parking_return)

        # --- Tier 3: Swing trading profits ---
        swing_return = np.random.normal(SWING_RETURN_MEAN, SWING_RETURN_STD)
        swing_profit = SWING_CAPITAL * max(swing_return, -0.25)

        if swing_profit > 0:
            swing_tax_amt = swing_profit * SWING_TAX
            tax_swing += swing_tax_amt
            after_tax = swing_profit - swing_tax_amt

            # 50% immediate to NIFTYBEES (capped at 20% of corpus)
            immediate = after_tax * 0.50
            total_corpus_now = pool + reserve + sum(positions.values())
            nifty_cap = total_corpus_now * 0.20
            nifty_current = positions.get("NIFTYBEES", 0)
            nifty_room = max(nifty_cap - nifty_current, 0)
            to_nifty = min(immediate, nifty_room)
            overflow = immediate - to_nifty

            if to_nifty > 0:
                if "NIFTYBEES" in positions:
                    positions["NIFTYBEES"] += to_nifty
                    position_cost["NIFTYBEES"] = position_cost.get("NIFTYBEES", 0) + to_nifty
                else:
                    positions["NIFTYBEES"] = to_nifty
                    position_age["NIFTYBEES"] = 0
                    position_cost["NIFTYBEES"] = to_nifty

            # 50% to pool + any overflow from NIFTYBEES cap
            pool += after_tax * 0.50 + overflow
            total_swing_added += after_tax

        # --- Per-asset: check for correction signals and deploy ---
        for asset in ALL_ASSETS:
            adj_corr_prob = min(asset.correction_prob * correction_prob_mult, 0.85)
            has_correction = np.random.random() < adj_corr_prob

            if not has_correction:
                continue

            total_corpus = pool + reserve + sum(positions.values())
            available_pool = pool

            if available_pool <= 0:
                continue

            # --- Stage 1: Weekly ST flip ---
            if asset.draw_stage1 > 0 and asset.name not in stage1_deployed:
                draw_amount = available_pool * asset.draw_stage1

                # Per-asset cap
                current_in_asset = positions.get(asset.name, 0)
                asset_cap = total_corpus * asset.max_pct_corpus
                max_allowed = max(asset_cap - current_in_asset, 0)
                draw_amount = min(draw_amount, max_allowed)

                # Category cap
                if asset.category in EQUITY_CATEGORIES:
                    eq_deployed = get_category_deployed(positions, EQUITY_CATEGORIES, ALL_ASSETS)
                    cat_room = max(total_corpus * CATEGORY_CAPS["equity"] - eq_deployed, 0)
                    draw_amount = min(draw_amount, cat_room)
                elif asset.category in ("gold", "silver"):
                    gs_deployed = get_category_deployed(positions, {"gold", "silver"}, ALL_ASSETS)
                    cat_room = max(total_corpus * CATEGORY_CAPS["gold_silver"] - gs_deployed, 0)
                    draw_amount = min(draw_amount, cat_room)
                elif asset.category == "reit":
                    r_deployed = get_category_deployed(positions, {"reit"}, ALL_ASSETS)
                    cat_room = max(total_corpus * CATEGORY_CAPS["reit"] - r_deployed, 0)
                    draw_amount = min(draw_amount, cat_room)

                if draw_amount > 0:
                    # Entry timing: 50% catch bounce, 50% buy too early
                    if np.random.random() < 0.50:
                        entry_adj = np.random.uniform(0.01, 0.04)
                    else:
                        entry_adj = np.random.uniform(-0.06, -0.01)
                    entry_value = draw_amount * (1 + entry_adj)

                    if asset.name in positions:
                        positions[asset.name] += entry_value
                        position_cost[asset.name] = position_cost.get(asset.name, 0) + draw_amount
                    else:
                        positions[asset.name] = entry_value
                        position_age[asset.name] = 0
                        position_cost[asset.name] = draw_amount

                    pool -= draw_amount
                    stage1_deployed[asset.name] = True
                    deployment_count += 1

            # --- Stage 2: Monthly ST touch (deeper correction) ---
            monthly_st_fires = np.random.random() < asset.monthly_st_prob

            if monthly_st_fires and asset.draw_stage2 > 0:
                available_pool = pool
                if available_pool <= 0:
                    continue

                draw_amount = available_pool * asset.draw_stage2
                total_corpus = pool + reserve + sum(positions.values())

                # Per-asset cap
                current_in_asset = positions.get(asset.name, 0)
                asset_cap = total_corpus * asset.max_pct_corpus
                max_allowed = max(asset_cap - current_in_asset, 0)
                draw_amount = min(draw_amount, max_allowed)

                # Category cap
                if asset.category in EQUITY_CATEGORIES:
                    eq_deployed = get_category_deployed(positions, EQUITY_CATEGORIES, ALL_ASSETS)
                    cat_room = max(total_corpus * CATEGORY_CAPS["equity"] - eq_deployed, 0)
                    draw_amount = min(draw_amount, cat_room)
                elif asset.category in ("gold", "silver"):
                    gs_deployed = get_category_deployed(positions, {"gold", "silver"}, ALL_ASSETS)
                    cat_room = max(total_corpus * CATEGORY_CAPS["gold_silver"] - gs_deployed, 0)
                    draw_amount = min(draw_amount, cat_room)
                elif asset.category == "reit":
                    r_deployed = get_category_deployed(positions, {"reit"}, ALL_ASSETS)
                    cat_room = max(total_corpus * CATEGORY_CAPS["reit"] - r_deployed, 0)
                    draw_amount = min(draw_amount, cat_room)

                if draw_amount > 0:
                    # Monthly ST entry: deeper correction, slightly better odds
                    if np.random.random() < 0.55:
                        entry_adj = np.random.uniform(0.02, 0.06)
                    else:
                        entry_adj = np.random.uniform(-0.08, -0.02)
                    entry_value = draw_amount * (1 + entry_adj)

                    if asset.name in positions:
                        positions[asset.name] += entry_value
                        position_cost[asset.name] = position_cost.get(asset.name, 0) + draw_amount
                    else:
                        positions[asset.name] = entry_value
                        position_age[asset.name] = 0
                        position_cost[asset.name] = draw_amount

                    pool -= draw_amount
                    deployment_count += 1

        # --- Deployed positions: apply returns + income tax + check exits ---
        to_exit = []
        for name in list(positions.keys()):
            asset_cfg = next((a for a in ALL_ASSETS if a.name == name), None)
            if asset_cfg is None:
                continue

            # --- INCOME: Dividends/distributions/interest (taxed annually at slab) ---
            if asset_cfg.dividend_yield > 0 and positions[name] > 0:
                gross_income = positions[name] * asset_cfg.dividend_yield
                taxable_income = gross_income * asset_cfg.income_taxable_pct
                income_tax = taxable_income * TAX_SLAB
                net_income = gross_income - income_tax

                # Income is cash OUT of the position, INTO the pool
                # Position value drops by gross_income (it was distributed)
                # Net income (after tax) goes to pool
                positions[name] -= gross_income
                pool += net_income
                tax_income += income_tax
                total_dividends_received += net_income

            # --- PRICE APPRECIATION: Only the non-income component ---
            # Total return = price_return + dividend_yield
            # Since dividends already extracted above, apply only price return to position
            price_return_mean = asset_cfg.return_mean - asset_cfg.dividend_yield
            if equity_return_override is not None and asset_cfg.category in EQUITY_CATEGORIES:
                # Override total return, but still subtract dividend_yield for price component
                price_return_mean = equity_return_override - asset_cfg.dividend_yield

            annual_price_return = np.random.normal(price_return_mean, asset_cfg.return_std)
            positions[name] *= (1 + annual_price_return)
            positions[name] = max(positions[name], 0)
            position_age[name] = position_age.get(name, 0) + 12

            # Check Monthly ST exit
            if np.random.random() < asset_cfg.exit_prob:
                to_exit.append(name)

        # --- Process exits ---
        for name in to_exit:
            if name not in positions:
                continue
            exit_count += 1
            value = positions[name]
            cost = position_cost.get(name, value)
            asset_cfg = next((a for a in ALL_ASSETS if a.name == name), None)

            # Loss exit (whipsaw): position may or may not be net profitable
            if np.random.random() < (asset_cfg.loss_exit_pct if asset_cfg else 0.15):
                loss_exits += 1
                loss_pct = np.random.uniform(0.05, 0.20)
                value = value * (1 - loss_pct)

                # After haircut, check if still net profitable
                if value > cost:
                    # Still in profit despite the dip -- pay tax on gain
                    gain = value - cost
                    age = position_age.get(name, 0)
                    tax = gain * TAX_LTCG if age >= 12 else gain * TAX_STCG
                    tax_capital_gains += tax
                    pool += (value - tax)
                else:
                    # Genuine loss -- no tax (could offset gains, but not modeled)
                    pool += value
            else:
                # Normal exit (profitable)
                gain = max(value - cost, 0)
                age = position_age.get(name, 0)
                if age >= 12:
                    # LTCG 12.5% -- NO exemption (consumed by parking)
                    tax = gain * TAX_LTCG
                else:
                    tax = gain * TAX_STCG
                tax_capital_gains += tax
                pool += (value - tax)

            del positions[name]
            if name in position_age:
                del position_age[name]
            if name in position_cost:
                del position_cost[name]
            if name in stage1_deployed:
                del stage1_deployed[name]

        # --- End of year ---
        total_corpus = pool + reserve + sum(positions.values())
        yearly_values.append(total_corpus)

    total_corpus = pool + reserve + sum(positions.values())
    deployed_total = sum(positions.values())
    parked_total = pool + reserve
    total_taxes = tax_income + tax_capital_gains + tax_swing

    return {
        "final_value": total_corpus,
        "deployed": deployed_total,
        "parked": parked_total,
        "total_return_pct": (total_corpus / STARTING_CAPITAL - 1) * 100,
        "cagr": ((total_corpus / STARTING_CAPITAL) ** (1 / years) - 1) * 100 if years > 0 else 0,
        "tax_total": total_taxes,
        "tax_income": tax_income,
        "tax_capital_gains": tax_capital_gains,
        "tax_swing": tax_swing,
        "swing_added": total_swing_added,
        "dividends_received": total_dividends_received,
        "deployments": deployment_count,
        "exits": exit_count,
        "loss_exits": loss_exits,
        "num_positions": len(positions),
        "yearly_values": yearly_values,
    }


def format_inr(amount: float) -> str:
    cr = amount / 1_00_00_000
    if cr >= 1:
        return f"Rs {cr:,.2f} Cr"
    else:
        lakhs = amount / 1_00_000
        return f"Rs {lakhs:,.1f} L"


def run_simulation():
    print("=" * 72)
    print("  MONTE CARLO V2.1: Dynamic Pool Model (Tax-Corrected)")
    print(f"  Starting Capital: {format_inr(STARTING_CAPITAL)}")
    print(f"  Swing Capital (separate): {format_inr(SWING_CAPITAL)}")
    print(f"  Assets modeled: {len(ALL_ASSETS)} ({len(CORE_ETFS)} core + {len(SECTOR_ETFS)} sector"
          f" + {len(SMART_BETA)} smart beta + {len(INTERNATIONAL)} intl"
          f" + {len(COMMODITIES)} commodity + {len(REITS)} REIT + {len(STOCKS)} stock)")
    print(f"  Simulations: {NUM_SIMULATIONS:,}")
    print(f"  Reserve: {RESERVE_PCT*100:.0f}% always in Tier 1")
    print(f"  Parking return: {PARKING_RETURN_MEAN*100:.1f}% post-tax blended")
    print("=" * 72)

    # Income-generating assets summary
    income_assets = [a for a in ALL_ASSETS if a.dividend_yield > 0]
    if income_assets:
        print("\n  Income-generating assets (taxed at 30% slab annually):")
        for a in income_assets:
            tax_desc = f"{a.income_taxable_pct*100:.0f}% taxable" if a.income_taxable_pct < 1.0 else "fully taxable"
            print(f"    {a.name:<14} {a.dividend_yield*100:.1f}% yield ({tax_desc})")
        print(f"  Growth ETFs (13): zero annual income tax -- all capital gains on exit")

    for target_year in YEARS_TO_REPORT:
        results = []
        for _ in range(NUM_SIMULATIONS):
            results.append(simulate_one_run(target_year))

        fv = np.array([r["final_value"] for r in results])
        cagrs = np.array([r["cagr"] for r in results])
        tax_tot = np.array([r["tax_total"] for r in results])
        tax_inc = np.array([r["tax_income"] for r in results])
        tax_cg = np.array([r["tax_capital_gains"] for r in results])
        tax_sw = np.array([r["tax_swing"] for r in results])
        swing = np.array([r["swing_added"] for r in results])
        divs = np.array([r["dividends_received"] for r in results])
        deploys = np.array([r["deployments"] for r in results])
        exits = np.array([r["exits"] for r in results])
        losses = np.array([r["loss_exits"] for r in results])
        num_pos = np.array([r["num_positions"] for r in results])
        deployed_pct = np.array([
            r["deployed"] / r["final_value"] * 100 if r["final_value"] > 0 else 0
            for r in results
        ])

        print(f"\n{'-' * 72}")
        print(f"  AFTER {target_year} YEARS")
        print(f"{'-' * 72}")

        print(f"\n  Portfolio Value:")
        for label, pct in [("Pessimistic (P5)", 5), ("Conservative (P10)", 10),
                           ("Below Avg (P25)", 25), (">> MEDIAN (P50)", 50),
                           ("Above Avg (P75)", 75), ("Optimistic (P90)", 90),
                           ("Best Case (P95)", 95)]:
            v = np.percentile(fv, pct)
            marker = "  << Most likely" if pct == 50 else ""
            print(f"    {label:.<32} {format_inr(v):>16}{marker}")
        print(f"    {'Mean:':.<32} {format_inr(np.mean(fv)):>16}")

        print(f"\n  CAGR:")
        for label, pct in [("Conservative (P10)", 10), ("Below Avg (P25)", 25),
                           (">> MEDIAN (P50)", 50), ("Above Avg (P75)", 75),
                           ("Optimistic (P90)", 90)]:
            c = np.percentile(cagrs, pct)
            marker = "  << Most likely" if pct == 50 else ""
            print(f"    {label:.<32} {c:>7.1f}%{marker}")

        print(f"\n  Multiplier (from 3 Cr):")
        for label, pct in [("Pessimistic (P10)", 10), ("Median", 50), ("Optimistic (P90)", 90)]:
            v = np.percentile(fv, pct)
            print(f"    {label:.<32} {v/STARTING_CAPITAL:>7.1f}x")

        print(f"\n  Strategy Metrics (averages):")
        print(f"    Deployments (total signals):         {np.mean(deploys):>6.1f}")
        print(f"    Exits (Monthly ST flips):             {np.mean(exits):>6.1f}")
        print(f"    Loss exits (whipsaws):                {np.mean(losses):>6.1f}")
        print(f"    Open positions at end:                {np.mean(num_pos):>6.1f}")
        print(f"    Capital deployed at end:              {np.mean(deployed_pct):>5.1f}% of portfolio")
        print(f"    Swing profits added:                  {format_inr(np.mean(swing)):>16}")
        print(f"    Dividends received (net):             {format_inr(np.mean(divs)):>16}")

        print(f"\n  Tax Breakdown (averages):")
        print(f"    Income tax (div/dist at 30% slab):   {format_inr(np.mean(tax_inc)):>16}")
        print(f"    Capital gains tax (LTCG/STCG):       {format_inr(np.mean(tax_cg)):>16}")
        print(f"    Swing trading tax (STCG 20%):        {format_inr(np.mean(tax_sw)):>16}")
        print(f"    ------------------------------------------")
        print(f"    TOTAL TAXES PAID:                    {format_inr(np.mean(tax_tot)):>16}")
        # Effective tax rate on total gains
        avg_gains = np.mean(fv) - STARTING_CAPITAL + np.mean(tax_tot)
        if avg_gains > 0:
            eff_rate = np.mean(tax_tot) / avg_gains * 100
            print(f"    Effective tax rate on gains:          {eff_rate:>5.1f}%")

        print(f"\n  Probability Analysis:")
        prob_beat_fd = np.mean(fv > STARTING_CAPITAL * (1.065 ** target_year)) * 100
        prob_beat_nifty = np.mean(fv > STARTING_CAPITAL * (1.12 ** target_year)) * 100
        prob_double = np.mean(fv > STARTING_CAPITAL * 2) * 100
        prob_5x = np.mean(fv > STARTING_CAPITAL * 5) * 100
        prob_10x = np.mean(fv > STARTING_CAPITAL * 10) * 100
        prob_loss = np.mean(fv < STARTING_CAPITAL) * 100
        print(f"    Beat FD (6.5% pre-tax):     {prob_beat_fd:>6.1f}%")
        print(f"    Beat NIFTY SIP (12% CAGR):  {prob_beat_nifty:>6.1f}%")
        print(f"    Double (> 6 Cr):            {prob_double:>6.1f}%")
        print(f"    5x (> 15 Cr):               {prob_5x:>6.1f}%")
        print(f"    10x (> 30 Cr):              {prob_10x:>6.1f}%")
        print(f"    Capital loss (< 3 Cr):      {prob_loss:>6.1f}%")

    # --- Comparisons ---
    print(f"\n{'=' * 72}")
    print("  COMPARISON: Alternatives over 20 years (all post-tax)")
    print(f"{'=' * 72}")

    print(f"\n  100% Arbitrage Fund (do nothing):")
    for yr in YEARS_TO_REPORT:
        v = STARTING_CAPITAL * (1.056 ** yr)  # 5.6% post-tax
        print(f"    {yr:>2} years: {format_inr(v):>16}  ({v/STARTING_CAPITAL:.1f}x)")

    print(f"\n  100% NIFTY from day 1 (12% CAGR, 12.5% LTCG on exit):")
    for yr in YEARS_TO_REPORT:
        gross = STARTING_CAPITAL * (1.12 ** yr)
        gain = gross - STARTING_CAPITAL
        tax = gain * TAX_LTCG
        v = gross - tax
        print(f"    {yr:>2} years: {format_inr(v):>16}  ({v/STARTING_CAPITAL:.1f}x)")

    print(f"\n  100% NIFTY (8% CAGR, 12.5% LTCG on exit):")
    for yr in YEARS_TO_REPORT:
        gross = STARTING_CAPITAL * (1.08 ** yr)
        gain = gross - STARTING_CAPITAL
        tax = gain * TAX_LTCG
        v = gross - tax
        print(f"    {yr:>2} years: {format_inr(v):>16}  ({v/STARTING_CAPITAL:.1f}x)")

    # --- Sensitivity ---
    print(f"\n{'=' * 72}")
    print("  SENSITIVITY ANALYSIS (20-year median)")
    print(f"{'=' * 72}")

    print(f"\n  Correction Frequency:")
    for label, mult in [("Fewer corrections (every 4-5 yrs)", 0.58),
                        ("Base case (every 2-3 yrs)", 1.0),
                        ("More corrections (every 1.5-2 yrs)", 1.45)]:
        vals = [simulate_one_run(20, correction_prob_mult=mult)["final_value"] for _ in range(3000)]
        med = np.percentile(vals, 50)
        print(f"    {label:.<48} {format_inr(med):>16}")

    print(f"\n  Equity Returns:")
    for label, ret in [("Bear case (equity 9% CAGR)", 0.09),
                       ("Base case (equity 13% CAGR)", 0.13),
                       ("Bull case (equity 16% CAGR)", 0.16)]:
        vals = [simulate_one_run(20, equity_return_override=ret)["final_value"] for _ in range(3000)]
        med = np.percentile(vals, 50)
        print(f"    {label:.<48} {format_inr(med):>16}")

    print(f"\n  Japan Scenario (equity 0% for 20 years):")
    vals = [simulate_one_run(20, equity_return_override=0.0)["final_value"] for _ in range(3000)]
    med = np.percentile(vals, 50)
    p10 = np.percentile(vals, 10)
    p90 = np.percentile(vals, 90)
    print(f"    {'Pessimistic (P10):':.<48} {format_inr(p10):>16}")
    print(f"    {'Median:':.<48} {format_inr(med):>16}")
    print(f"    {'Optimistic (P90):':.<48} {format_inr(p90):>16}")
    japan_nifty = STARTING_CAPITAL  # 0% return = same capital, but LTCG tax = 0
    print(f"    (vs always-in-NIFTY Japan scenario: {format_inr(japan_nifty):>16})")

    # --- Tax impact analysis ---
    print(f"\n{'=' * 72}")
    print("  TAX IMPACT: Growth ETFs vs Income Assets (20-year)")
    print(f"{'=' * 72}")
    print(f"\n  Growth ETFs (NIFTYBEES, JUNIORBEES, etc.):")
    print(f"    Annual income tax drag:              0.00%")
    print(f"    Tax only on exit:                    12.5% LTCG")
    print(f"    20yr effective tax rate:              ~10-11%")
    print(f"\n  REITs (5% yield, 65% taxable at 30%):")
    print(f"    Annual income tax drag:              ~1.0% of position value")
    print(f"    + exit LTCG on price appreciation:   12.5%")
    print(f"    20yr effective tax rate:              ~18-20%")
    print(f"\n  Dividend Stocks (2.5% yield at 30%):")
    print(f"    Annual income tax drag:              ~0.75% of position value")
    print(f"    + exit LTCG on price appreciation:   12.5%")
    print(f"    20yr effective tax rate:              ~16-18%")
    print(f"\n  Gold/SGB blend (1.5% interest at 30%):")
    print(f"    Annual income tax drag:              ~0.45% of position value")
    print(f"    + exit LTCG on price appreciation:   12.5%")
    print(f"    20yr effective tax rate:              ~14-15%")
    print(f"\n  --> Growth ETFs are the most tax-efficient deployed asset")
    print(f"  --> REITs/stocks still worth it for diversification, but tax drag is real")

    # --- Disclaimer ---
    print(f"\n{'=' * 72}")
    print("  DISCLAIMER")
    print(f"{'=' * 72}")
    print("  This models historical patterns, NOT guarantees.")
    print("  Key assumptions: equity 13% CAGR, corrections every 2-3 years,")
    print("  swing trading 18% annual, ST remains valid.")
    print("  Tax regime: Budget 2024 rates (LTCG 12.5%, STCG 20%, slab 30%).")
    print("  SGB secondary market: 12.5% LTCG + 30% slab on interest (Apr 2026+).")
    print("  Past performance does not predict future results.")
    print(f"{'=' * 72}")


if __name__ == "__main__":
    run_simulation()
