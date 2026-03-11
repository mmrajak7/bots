"""JSON formatter for stock analysis output — designed for Claude consumption."""

import json
import logging
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)


def format_json(symbol: str, screener_data: Optional[dict], technical_data: Optional[dict],
                trendlyne_data: Optional[dict], scores: Optional[dict],
                verdict: Optional[dict], data_quality: Optional[dict]) -> str:
    """Format all analysis data into a structured JSON string.

    This JSON is designed to be consumed by Claude for interpretation.
    Fields are organized for easy parsing and narrative generation.
    """
    output = {
        "meta": {
            "symbol": symbol.upper(),
            "analyzed_at": datetime.now().isoformat(),
            "data_quality": data_quality or {}
        },
        "company": _extract_company_info(screener_data),
        "fundamentals": _extract_fundamentals(screener_data),
        "technicals": _extract_technicals(technical_data),
        "shareholding": _extract_shareholding(screener_data),
        "growth": _extract_growth(screener_data),
        "trendlyne": _extract_trendlyne(trendlyne_data),
        "scores": scores or {},
        "verdict": verdict or {},
    }
    return json.dumps(output, indent=2, default=str)


def _extract_company_info(data: Optional[dict]) -> dict:
    if not data:
        return {}
    return {
        "name": data.get("company_name", ""),
        "sector": data.get("sector", ""),
        "page_type": data.get("page_type", ""),
    }


def _extract_fundamentals(data: Optional[dict]) -> dict:
    if not data:
        return {}
    ratios = data.get("ratios", {})
    return {
        "ratios": {
            "market_cap": ratios.get("Market Cap"),
            "pe": ratios.get("Stock P/E"),
            "book_value": ratios.get("Book Value"),
            "dividend_yield": ratios.get("Dividend Yield"),
            "roce": ratios.get("ROCE"),
            "roe": ratios.get("ROE"),
            "face_value": ratios.get("Face Value"),
            "industry_pe": ratios.get("Industry PE"),
            "debt_to_equity": ratios.get("Debt to equity"),
            "promoter_holding": ratios.get("Promoter holding"),
        },
        "quarterly_results": data.get("quarterly", {}),
        "annual_results": data.get("annual", {}),
        "balance_sheet": data.get("balance_sheet", {}),
        "cash_flow": data.get("cash_flow", {}),
        "pros": data.get("pros_cons", {}).get("pros", []),
        "cons": data.get("pros_cons", {}).get("cons", []),
    }


def _extract_technicals(data: Optional[dict]) -> dict:
    if not data:
        return {}
    return {
        "current_price": data.get("current_price"),
        "rsi_14": data.get("rsi"),
        "macd": {
            "line": data.get("macd_line"),
            "signal": data.get("macd_signal"),
            "histogram": data.get("macd_histogram"),
        },
        "moving_averages": {
            "dma_50": data.get("dma_50"),
            "dma_200": data.get("dma_200"),
            "price_vs_50dma_pct": data.get("price_vs_50dma_pct"),
            "price_vs_200dma_pct": data.get("price_vs_200dma_pct"),
        },
        "52_week": {
            "high": data.get("high_52w"),
            "low": data.get("low_52w"),
            "pct_from_high": data.get("pct_from_52w_high"),
            "pct_from_low": data.get("pct_from_52w_low"),
        },
        "volume": {
            "current": data.get("current_volume"),
            "avg_20d": data.get("volume_20d_avg"),
            "avg_5d": data.get("volume_5d_avg"),
            "trend": data.get("volume_trend"),
        },
        "bollinger": {
            "upper": data.get("bb_upper"),
            "middle": data.get("bb_middle"),
            "lower": data.get("bb_lower"),
        },
        "support_levels": data.get("support_levels", []),
        "resistance_levels": data.get("resistance_levels", []),
    }


def _parse_sh_val(val):
    """Parse shareholding string like '50.10%' to float."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace('%', '').replace(',', '')
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _extract_shareholding(data: Optional[dict]) -> dict:
    if not data:
        return {}
    sh = data.get("shareholding_original") or data.get("shareholding", {})
    if not sh or not isinstance(sh, dict):
        return {}

    quarters = sh.get("quarters", [])
    promoters = sh.get("promoters", [])
    fiis = sh.get("fiis", [])
    diis = sh.get("diis", [])
    public = sh.get("public", [])
    pledged = sh.get("pledged", [])

    result = {
        "quarters": quarters,
        "promoters": promoters,
        "fiis": fiis,
        "diis": diis,
        "public": public,
        "pledged": pledged,
    }

    # Compute trends (parse strings to floats for comparison)
    if len(promoters) >= 2:
        latest = _parse_sh_val(promoters[-1])
        prev = _parse_sh_val(promoters[-2])
        result["promoter_trend"] = "increasing" if latest > prev else "decreasing" if latest < prev else "stable"
        result["promoter_change_1q"] = round(latest - prev, 2)
    if len(promoters) >= 4:
        oldest = _parse_sh_val(promoters[-4])
        latest = _parse_sh_val(promoters[-1])
        result["promoter_change_1y"] = round(latest - oldest, 2)
    if len(fiis) >= 2:
        fii_latest = _parse_sh_val(fiis[-1])
        fii_prev = _parse_sh_val(fiis[-2])
        result["fii_trend"] = "increasing" if fii_latest > fii_prev else "decreasing" if fii_latest < fii_prev else "stable"
    if len(diis) >= 2:
        dii_latest = _parse_sh_val(diis[-1])
        dii_prev = _parse_sh_val(diis[-2])
        result["dii_trend"] = "increasing" if dii_latest > dii_prev else "decreasing" if dii_latest < dii_prev else "stable"

    return result


def _extract_growth(data: Optional[dict]) -> dict:
    if not data:
        return {}
    return data.get("growth_rates", {})


def _extract_trendlyne(data: Optional[dict]) -> dict:
    if not data:
        return {"available": False}
    return {
        "available": True,
        **data
    }


def format_comparison_json(results: List[dict]) -> str:
    """Format multiple stock analyses for comparison."""
    comparison = {
        "meta": {
            "type": "comparison",
            "analyzed_at": datetime.now().isoformat(),
            "stock_count": len(results)
        },
        "stocks": results,
        "ranking": _compute_ranking(results)
    }
    return json.dumps(comparison, indent=2, default=str)


def _compute_ranking(results: List[dict]) -> List[dict]:
    """Rank stocks by composite score."""
    ranked = []
    for r in results:
        verdict = r.get("verdict", {})
        ranked.append({
            "symbol": r.get("meta", {}).get("symbol", ""),
            "verdict": verdict.get("verdict", "N/A"),
            "composite_score": verdict.get("composite_score", 0),
            "confidence": verdict.get("confidence", 0),
        })
    ranked.sort(key=lambda x: x["composite_score"], reverse=True)
    for i, item in enumerate(ranked):
        item["rank"] = i + 1
    return ranked
