"""
Fundamental scoring engine — pure computation, zero I/O.

Takes structured dicts (from screener.py source) and computes:
  - Piotroski F-Score (0-9 quality metric)
  - PEG Ratio (growth-adjusted valuation)
  - DuPont Analysis (ROE decomposition)
  - Altman Z-Score (bankruptcy risk)
  - Composite fundamental score (0-100)

Expected screener_data structure (dict with these keys):
  annual_results: list[dict]  — yearly P&L rows, most recent first
      Each row: {sales, expenses, operating_profit, opm_pct, net_profit, eps}
  balance_sheet: list[dict]   — yearly balance sheet rows, most recent first
      Each row: {equity_capital, reserves, borrowings, total_liabilities,
                 fixed_assets, total_assets, investments, other_assets}
  cash_flow: list[dict]       — yearly cash flow rows, most recent first
      Each row: {cash_from_operations, cash_from_investing, cash_from_financing,
                 net_cash_flow}
  ratios: dict                — {roce_pct, roe_pct, debt_to_equity,
                                  current_ratio, pe, industry_pe, market_cap,
                                  book_value, dividend_yield}
  shareholding: list[dict]    — quarterly shareholding rows, most recent first
      Each row: {quarter, promoter_pct, fii_pct, dii_pct, public_pct,
                 pledged_pct}
  growth: dict                — {revenue_3y_cagr, profit_3y_cagr,
                                  revenue_5y_cagr, profit_5y_cagr,
                                  eps_growth_3y}
  sector: str                 — e.g. "Banking", "NBFC", "Insurance", "IT"
  company_name: str
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe_get(d: Optional[dict], key: str, default: Any = None) -> Any:
    """Safely extract a key from a dict that might be None."""
    if d is None:
        return default
    return d.get(key, default)


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Convert a value to float, returning default if conversion fails."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Safe division — returns default if denominator is zero or near-zero."""
    if abs(denominator) < 1e-10:
        return default
    return numerator / denominator


def _get_annual_rows(screener_data: dict, min_rows: int = 1) -> Optional[list]:
    """Extract annual results list, return None if insufficient data."""
    rows = screener_data.get('annual_results')
    if not rows or len(rows) < min_rows:
        return None
    return rows


def _get_balance_rows(screener_data: dict, min_rows: int = 1) -> Optional[list]:
    """Extract balance sheet list, return None if insufficient data."""
    rows = screener_data.get('balance_sheet')
    if not rows or len(rows) < min_rows:
        return None
    return rows


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    """Clamp a value to [lo, hi]."""
    return max(lo, min(hi, value))


# ── 3a. Piotroski F-Score ────────────────────────────────────────────────────

def piotroski_f_score(screener_data: dict) -> dict:
    """Compute the Piotroski F-Score (0-9) from annual financials.

    The F-Score measures financial strength across three dimensions:
      - Profitability (4 signals)
      - Leverage / Liquidity (3 signals)
      - Operating Efficiency (2 signals)

    Each signal is binary (0 or 1). Missing data defaults to 0 (conservative).

    Args:
        screener_data: Structured dict with annual_results, balance_sheet,
                       cash_flow keys.

    Returns:
        {"score": 0-9, "components": {name: 0|1}, "interpretation": str}
    """
    components: Dict[str, int] = {}

    annual = _get_annual_rows(screener_data, min_rows=1)
    annual_prev = _get_annual_rows(screener_data, min_rows=2)
    balance = _get_balance_rows(screener_data, min_rows=1)
    balance_prev = _get_balance_rows(screener_data, min_rows=2)
    cash_flow = screener_data.get('cash_flow')

    # Latest year values
    latest_pl = annual[0] if annual else {}
    prev_pl = annual[1] if annual_prev else {}
    latest_bs = balance[0] if balance else {}
    prev_bs = balance[1] if balance_prev else {}
    latest_cf = cash_flow[0] if cash_flow and len(cash_flow) > 0 else {}

    net_profit = _safe_float(latest_pl.get('net_profit'))
    prev_net_profit = _safe_float(prev_pl.get('net_profit'))
    cfo = _safe_float(latest_cf.get('cash_from_operations'))
    total_assets = _safe_float(latest_bs.get('total_assets'))
    prev_total_assets = _safe_float(prev_bs.get('total_assets'))
    borrowings = _safe_float(latest_bs.get('borrowings'))
    prev_borrowings = _safe_float(prev_bs.get('borrowings'))
    equity_capital = _safe_float(latest_bs.get('equity_capital'))
    prev_equity_capital = _safe_float(prev_bs.get('equity_capital'))
    sales = _safe_float(latest_pl.get('sales'))
    prev_sales = _safe_float(prev_pl.get('sales'))
    opm = _safe_float(latest_pl.get('opm_pct'))
    prev_opm = _safe_float(prev_pl.get('opm_pct'))

    # ── Profitability (4 signals) ────────────────────────────────────────

    # 1. Net Income > 0
    components['net_income_positive'] = 1 if net_profit > 0 else 0

    # 2. Operating Cash Flow > 0
    components['cfo_positive'] = 1 if cfo > 0 else 0

    # 3. ROA increasing (net_profit / total_assets, compare 2 years)
    roa_current = _safe_div(net_profit, total_assets)
    roa_prev = _safe_div(prev_net_profit, prev_total_assets)
    if total_assets > 0 and prev_total_assets > 0:
        components['roa_increasing'] = 1 if roa_current > roa_prev else 0
    else:
        components['roa_increasing'] = 0

    # 4. Cash Flow > Net Income (quality of earnings)
    components['cfo_exceeds_net_income'] = 1 if cfo > net_profit else 0

    # ── Leverage / Liquidity (3 signals) ─────────────────────────────────

    # 5. Long-term debt ratio decreasing (borrowings / total_assets)
    debt_ratio = _safe_div(borrowings, total_assets)
    prev_debt_ratio = _safe_div(prev_borrowings, prev_total_assets)
    if total_assets > 0 and prev_total_assets > 0:
        components['debt_ratio_decreasing'] = 1 if debt_ratio < prev_debt_ratio else 0
    else:
        components['debt_ratio_decreasing'] = 0

    # 6. Current ratio increasing
    ratios = screener_data.get('ratios') or {}
    current_ratio = _safe_float(ratios.get('current_ratio'))
    # If we have previous year current ratio data, compare; else neutral (assign 0)
    # Current ratio trend is hard to get from single-year data, so we check
    # if it's above 1.0 as a proxy when no historical data is available
    if current_ratio > 0:
        # We only have latest ratio — use 1.0 as the baseline
        components['current_ratio_healthy'] = 1 if current_ratio >= 1.0 else 0
    else:
        components['current_ratio_healthy'] = 0

    # 7. No new share issuance (equity capital unchanged or decreased)
    if equity_capital > 0 and prev_equity_capital > 0:
        components['no_dilution'] = 1 if equity_capital <= prev_equity_capital else 0
    else:
        components['no_dilution'] = 0

    # ── Operating Efficiency (2 signals) ─────────────────────────────────

    # 8. Gross margin / OPM increasing
    if opm != 0 or prev_opm != 0:
        components['opm_increasing'] = 1 if opm > prev_opm else 0
    else:
        components['opm_increasing'] = 0

    # 9. Asset turnover increasing (sales / total_assets)
    turnover_current = _safe_div(sales, total_assets)
    turnover_prev = _safe_div(prev_sales, prev_total_assets)
    if total_assets > 0 and prev_total_assets > 0:
        components['asset_turnover_increasing'] = 1 if turnover_current > turnover_prev else 0
    else:
        components['asset_turnover_increasing'] = 0

    # ── Score & interpretation ───────────────────────────────────────────

    score = sum(components.values())

    if score >= 8:
        interpretation = "Very strong financial health — all key metrics firing"
    elif score >= 7:
        interpretation = "Strong financial health — minor weaknesses"
    elif score >= 5:
        interpretation = "Average financial health — mixed signals"
    elif score >= 3:
        interpretation = "Below average — multiple red flags"
    else:
        interpretation = "Weak financial health — significant concerns"

    return {
        'score': score,
        'components': components,
        'interpretation': interpretation,
    }


# ── 3b. PEG Ratio ───────────────────────────────────────────────────────────

def peg_ratio(pe: Optional[float], earnings_growth_3y: Optional[float]) -> dict:
    """Compute PEG ratio and interpret growth-adjusted valuation.

    PEG = PE / Earnings Growth Rate (in %)
    A PEG < 1 suggests the stock is undervalued relative to its growth.

    Args:
        pe: Price-to-earnings ratio. None or negative = loss-making.
        earnings_growth_3y: 3-year earnings CAGR in percent (e.g. 15.0 = 15%).
                            None or negative = negative growth.

    Returns:
        {"peg": float|None, "interpretation": str}
    """
    if pe is None or pe < 0:
        return {
            'peg': None,
            'interpretation': "Loss-making — PEG not applicable",
        }

    if earnings_growth_3y is None or earnings_growth_3y <= 0:
        return {
            'peg': None,
            'interpretation': "Negative growth — PEG not applicable",
        }

    peg = pe / earnings_growth_3y

    if peg < 0.5:
        interpretation = "Deeply undervalued"
    elif peg < 1.0:
        interpretation = "Undervalued"
    elif peg < 1.5:
        interpretation = "Fairly valued"
    elif peg < 2.0:
        interpretation = "Moderately expensive"
    else:
        interpretation = "Expensive"

    return {
        'peg': round(peg, 2),
        'interpretation': interpretation,
    }


# ── 3c. DuPont Analysis ─────────────────────────────────────────────────────

def dupont_analysis(screener_data: dict) -> dict:
    """Decompose ROE into its three DuPont drivers.

    ROE = Profit Margin x Asset Turnover x Equity Multiplier
        = (Net Profit / Sales) x (Sales / Total Assets) x (Total Assets / Equity)

    This tells us WHETHER a high ROE comes from genuine profitability (good),
    operational efficiency (good), or excessive leverage (risky).

    Args:
        screener_data: Dict with annual_results and balance_sheet.

    Returns:
        {"roe": float, "profit_margin": float, "asset_turnover": float,
         "equity_multiplier": float, "roe_driver": str, "interpretation": str}
    """
    annual = _get_annual_rows(screener_data)
    balance = _get_balance_rows(screener_data)

    if not annual or not balance:
        return {
            'roe': None,
            'profit_margin': None,
            'asset_turnover': None,
            'equity_multiplier': None,
            'roe_driver': "Insufficient data",
            'interpretation': "Cannot perform DuPont analysis — missing data",
        }

    latest_pl = annual[0]
    latest_bs = balance[0]

    net_profit = _safe_float(latest_pl.get('net_profit'))
    sales = _safe_float(latest_pl.get('sales'))
    total_assets = _safe_float(latest_bs.get('total_assets'))
    equity_capital = _safe_float(latest_bs.get('equity_capital'))
    reserves = _safe_float(latest_bs.get('reserves'))

    shareholder_equity = equity_capital + reserves

    # Compute the three components
    profit_margin = _safe_div(net_profit, sales)
    asset_turnover = _safe_div(sales, total_assets)
    equity_multiplier = _safe_div(total_assets, shareholder_equity) if shareholder_equity > 0 else 0.0

    # Computed ROE via DuPont
    roe = profit_margin * asset_turnover * equity_multiplier

    # Identify dominant driver by comparing normalized contributions
    # Each component's "contribution" to ROE magnitude
    abs_margin = abs(profit_margin)
    abs_turnover = abs(asset_turnover)
    abs_multiplier = abs(equity_multiplier)

    # Normalize to comparable scale: margin is typically 0.05-0.30,
    # turnover 0.3-2.0, multiplier 1.5-5.0. We compare deviations
    # from "neutral" baselines.
    margin_signal = abs_margin  # higher = better margins
    turnover_signal = abs_turnover  # higher = better efficiency
    # For multiplier, >2.0 means leverage is a significant driver
    leverage_signal = max(0, abs_multiplier - 1.5) / 3.0  # normalize

    if margin_signal >= turnover_signal and margin_signal >= leverage_signal:
        roe_driver = "Margin-driven ROE (high quality)"
    elif turnover_signal >= margin_signal and turnover_signal >= leverage_signal:
        roe_driver = "Efficiency-driven ROE (good)"
    else:
        roe_driver = "Leverage-driven ROE (risky)"

    # Interpretation
    roe_pct = roe * 100
    if roe_pct > 20:
        quality = "Excellent"
    elif roe_pct > 15:
        quality = "Good"
    elif roe_pct > 10:
        quality = "Average"
    elif roe_pct > 0:
        quality = "Below average"
    else:
        quality = "Negative (loss-making or negative equity)"

    interpretation = (
        f"{quality} ROE of {roe_pct:.1f}%. "
        f"Margin={profit_margin:.1%}, Turnover={asset_turnover:.2f}x, "
        f"Leverage={equity_multiplier:.2f}x. {roe_driver}."
    )

    return {
        'roe': round(roe * 100, 2),
        'profit_margin': round(profit_margin * 100, 2),
        'asset_turnover': round(asset_turnover, 3),
        'equity_multiplier': round(equity_multiplier, 2),
        'roe_driver': roe_driver,
        'interpretation': interpretation,
    }


# ── 3d. Altman Z-Score ──────────────────────────────────────────────────────

# Sectors where Altman Z is not meaningful (balance sheet structure differs)
_FINANCIAL_SECTORS = {
    'banking', 'bank', 'banks',
    'nbfc', 'financial services', 'finance',
    'insurance',
    'housing finance',
    'microfinance',
    'asset management',
}


def altman_z_score(screener_data: dict, market_cap: Optional[float] = None) -> dict:
    """Compute Altman Z-Score for bankruptcy risk assessment.

    Z = 1.2*A + 1.4*B + 3.3*C + 0.6*D + 1.0*E
    Where:
      A = Working Capital / Total Assets
      B = Retained Earnings (Reserves) / Total Assets
      C = EBIT (Operating Profit) / Total Assets
      D = Market Cap / Total Liabilities
      E = Sales / Total Assets

    NOT applicable for banks, NBFCs, insurance — returns applicable=False.

    Args:
        screener_data: Dict with balance_sheet, annual_results, sector keys.
        market_cap: Market capitalization in Cr. If None, tries ratios.market_cap.

    Returns:
        {"z_score": float|None, "zone": str, "applicable": bool,
         "interpretation": str}
    """
    # Check sector applicability
    sector = (screener_data.get('sector') or '').strip().lower()
    if sector in _FINANCIAL_SECTORS or any(kw in sector for kw in _FINANCIAL_SECTORS):
        return {
            'z_score': None,
            'zone': 'N/A',
            'applicable': False,
            'interpretation': (
                f"Altman Z-Score not applicable for {screener_data.get('sector', 'financial')} "
                f"sector — balance sheet structure differs fundamentally."
            ),
        }

    balance = _get_balance_rows(screener_data)
    annual = _get_annual_rows(screener_data)

    if not balance or not annual:
        return {
            'z_score': None,
            'zone': 'N/A',
            'applicable': True,
            'interpretation': "Insufficient data to compute Altman Z-Score.",
        }

    latest_bs = balance[0]
    latest_pl = annual[0]

    total_assets = _safe_float(latest_bs.get('total_assets'))
    if total_assets <= 0:
        return {
            'z_score': None,
            'zone': 'N/A',
            'applicable': True,
            'interpretation': "Total assets is zero or negative — cannot compute Z-Score.",
        }

    # Resolve market cap
    if market_cap is None:
        ratios = screener_data.get('ratios') or {}
        market_cap = _safe_float(ratios.get('market_cap'))
    if market_cap is None or market_cap <= 0:
        return {
            'z_score': None,
            'zone': 'N/A',
            'applicable': True,
            'interpretation': "Market cap unavailable — cannot compute Z-Score.",
        }

    reserves = _safe_float(latest_bs.get('reserves'))
    equity_capital = _safe_float(latest_bs.get('equity_capital'))
    borrowings = _safe_float(latest_bs.get('borrowings'))
    total_liabilities_raw = _safe_float(latest_bs.get('total_liabilities'))
    operating_profit = _safe_float(latest_pl.get('operating_profit'))
    sales = _safe_float(latest_pl.get('sales'))

    # Working capital approximation:
    # total_liabilities from screener is total equity + liabilities side
    # Shareholder equity = equity_capital + reserves
    # Total liabilities (debt side) = total_liabilities_raw - shareholder_equity
    shareholder_equity = equity_capital + reserves
    total_liabilities = total_liabilities_raw - shareholder_equity
    if total_liabilities < 0:
        # Fallback: use borrowings as proxy for total liabilities
        total_liabilities = borrowings

    # Working capital = Current Assets - Current Liabilities
    # Approximation: (Total Assets - Fixed Assets - Investments) - (Total Liabilities - Borrowings)
    # Simplified: we use a rough proxy since screener data may not split current/non-current
    other_assets = _safe_float(latest_bs.get('other_assets'))
    fixed_assets = _safe_float(latest_bs.get('fixed_assets'))
    investments = _safe_float(latest_bs.get('investments'))
    current_assets_approx = total_assets - fixed_assets - investments
    # Current liabilities approx = total liabilities - long-term borrowings
    # This is rough; use what we have
    current_liabilities_approx = max(0, total_liabilities - borrowings * 0.7)
    working_capital = current_assets_approx - current_liabilities_approx

    # Z-Score components
    a = _safe_div(working_capital, total_assets)       # Working Capital / Total Assets
    b = _safe_div(reserves, total_assets)              # Retained Earnings / Total Assets
    c = _safe_div(operating_profit, total_assets)      # EBIT / Total Assets
    d = _safe_div(market_cap, total_liabilities) if total_liabilities > 0 else 5.0  # Cap at reasonable value
    e = _safe_div(sales, total_assets)                 # Sales / Total Assets

    z = 1.2 * a + 1.4 * b + 3.3 * c + 0.6 * d + 1.0 * e

    if z > 2.99:
        zone = "Safe"
        interpretation = f"Z-Score {z:.2f} — Safe zone. Low probability of financial distress."
    elif z > 1.81:
        zone = "Grey zone"
        interpretation = f"Z-Score {z:.2f} — Grey zone. Some financial stress indicators; monitor closely."
    else:
        zone = "Distress"
        interpretation = f"Z-Score {z:.2f} — Distress zone. Elevated bankruptcy risk."

    return {
        'z_score': round(z, 2),
        'zone': zone,
        'applicable': True,
        'interpretation': interpretation,
        'components': {
            'working_capital_to_assets': round(a, 4),
            'retained_earnings_to_assets': round(b, 4),
            'ebit_to_assets': round(c, 4),
            'market_cap_to_liabilities': round(d, 4),
            'sales_to_assets': round(e, 4),
        },
    }


# ── 3e. Composite Fundamental Score ─────────────────────────────────────────

def compute_fundamental_score(screener_data: dict, config: dict) -> dict:
    """Compute a 0-100 composite fundamental score.

    Combines Piotroski F-Score, ROCE, ROE, profit growth, revenue growth,
    and OPM trend into a single score with warning flags.

    All thresholds come from the config dict (thresholds key).

    Args:
        screener_data: Full screener data dict.
        config: Config dict with 'thresholds' key containing scoring cutoffs.

    Returns:
        {"score": 0-100, "sub_scores": dict, "flags": list[str]}
    """
    thresholds = config.get('thresholds', {})
    roce_strong = thresholds.get('roce_strong', 20)
    roce_weak = thresholds.get('roce_weak', 10)

    sub_scores: Dict[str, dict] = {}
    flags: List[str] = []
    raw_score = 50.0  # Start at neutral

    # ── Piotroski ────────────────────────────────────────────────────────

    pio = piotroski_f_score(screener_data)
    pio_score = pio['score']

    if pio_score >= 7:
        pio_contribution = 90
    elif pio_score >= 5:
        pio_contribution = 60
    elif pio_score >= 3:
        pio_contribution = 40
    else:
        pio_contribution = 15

    sub_scores['piotroski'] = {
        'f_score': pio_score,
        'contribution': pio_contribution,
        'weight': 0.30,
    }

    # ── ROCE ─────────────────────────────────────────────────────────────

    ratios = screener_data.get('ratios') or {}
    roce = _safe_float(ratios.get('roce_pct'))

    roce_adj = 0
    if roce > roce_strong:
        roce_adj = 20
    elif roce > 15:
        roce_adj = 10
    elif roce < roce_weak:
        roce_adj = -10

    sub_scores['roce'] = {'value': roce, 'adjustment': roce_adj}

    # ── ROE ──────────────────────────────────────────────────────────────

    roe = _safe_float(ratios.get('roe_pct'))

    roe_adj = 0
    if roe > 20:
        roe_adj = 15
    elif roe > 15:
        roe_adj = 10
    elif roe < 10:
        roe_adj = -10

    sub_scores['roe'] = {'value': roe, 'adjustment': roe_adj}

    # ── Profit Growth (3Y CAGR) ──────────────────────────────────────────

    growth = screener_data.get('growth') or {}
    profit_growth = _safe_float(growth.get('profit_3y_cagr'))

    profit_adj = 0
    if profit_growth > 15:
        profit_adj = 20
    elif profit_growth > 0:
        profit_adj = 10
    elif profit_growth < 0:
        profit_adj = -10

    sub_scores['profit_growth_3y'] = {'value': profit_growth, 'adjustment': profit_adj}

    # ── Revenue Growth (3Y CAGR) ─────────────────────────────────────────

    rev_growth = _safe_float(growth.get('revenue_3y_cagr'))

    rev_adj = 0
    if rev_growth > 15:
        rev_adj = 15
    elif rev_growth > 5:
        rev_adj = 5
    elif rev_growth < 0:
        rev_adj = -10
        flags.append("Declining revenue (3Y CAGR negative)")

    sub_scores['revenue_growth_3y'] = {'value': rev_growth, 'adjustment': rev_adj}

    # ── OPM Trend ────────────────────────────────────────────────────────

    annual = _get_annual_rows(screener_data, min_rows=3)
    opm_adj = 0
    opm_trend = "Insufficient data"

    if annual and len(annual) >= 3:
        opm_values = [_safe_float(row.get('opm_pct')) for row in annual[:3]]
        # annual[0] = latest, annual[1] = previous, annual[2] = two years ago
        if opm_values[0] > opm_values[1] > opm_values[2]:
            opm_adj = 10
            opm_trend = "Improving (2+ years)"
        elif opm_values[0] < opm_values[1] < opm_values[2]:
            opm_adj = -10
            opm_trend = "Declining (2+ years)"
            flags.append("OPM declining for 2+ years")
        else:
            opm_trend = "Mixed"

    sub_scores['opm_trend'] = {'trend': opm_trend, 'adjustment': opm_adj}

    # ── Compute final score ──────────────────────────────────────────────

    # Base score from Piotroski (weighted 30%)
    base = pio_contribution * 0.30
    # Add adjustments (remaining 70% comes from quality metrics)
    adjustments = roce_adj + roe_adj + profit_adj + rev_adj + opm_adj
    # Scale adjustments: total possible adjustments range is roughly -50 to +80
    # Map this to the remaining 70 points
    adjustment_score = 50 + adjustments  # Center at 50
    adjustment_score = _clamp(adjustment_score, 0, 100)

    # Final blend: 30% Piotroski base + 70% quality adjustments
    raw_score = base + adjustment_score * 0.70

    # ── Flags ────────────────────────────────────────────────────────────

    # Loss-making check
    annual_latest = _get_annual_rows(screener_data)
    if annual_latest:
        latest_profit = _safe_float(annual_latest[0].get('net_profit'))
        if latest_profit < 0:
            flags.append("Loss-making (latest year net profit negative)")

    # Debt increasing check
    balance_rows = _get_balance_rows(screener_data, min_rows=2)
    if balance_rows and len(balance_rows) >= 2:
        curr_borrowings = _safe_float(balance_rows[0].get('borrowings'))
        prev_borrowings = _safe_float(balance_rows[1].get('borrowings'))
        if curr_borrowings > prev_borrowings and prev_borrowings > 0:
            pct_increase = (curr_borrowings - prev_borrowings) / prev_borrowings * 100
            if pct_increase > 10:
                flags.append(f"Debt increasing (borrowings up {pct_increase:.0f}% YoY)")

    # Low ROCE flag
    if roce < roce_weak:
        flags.append(f"Low ROCE ({roce:.1f}% < {roce_weak}%)")

    final_score = _clamp(raw_score)

    return {
        'score': round(final_score, 1),
        'sub_scores': sub_scores,
        'flags': flags,
    }
