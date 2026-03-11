"""Terminal formatter — rich tabular output for direct CLI usage."""

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


def format_terminal(symbol: str, screener_data: Optional[dict], technical_data: Optional[dict],
                    trendlyne_data: Optional[dict], scores: Optional[dict],
                    verdict: Optional[dict], data_quality: Optional[dict]) -> str:
    """Format analysis into a readable terminal report."""
    lines = []
    hr = "=" * 70

    # Header
    lines.append(hr)
    company = screener_data.get("company_name", symbol) if screener_data else symbol
    lines.append(f"  STOCK ANALYSIS: {company} ({symbol.upper()})")
    lines.append(hr)

    # Verdict banner
    if verdict:
        v = verdict.get("verdict", "N/A")
        conf = verdict.get("confidence", 0)
        composite = verdict.get("composite_score", 0)
        emoji_map = {"BUY": "[BUY]", "WAIT": "[WAIT]", "EXIT": "[EXIT]", "SELL": "[SELL]"}
        lines.append(f"\n  VERDICT: {emoji_map.get(v, v)} {v} | Confidence: {conf}/100 | Score: {composite:.1f}/100")
        if verdict.get("hard_override"):
            lines.append(f"  *** HARD OVERRIDE: {verdict['hard_override']} ***")
        lines.append("")

    # Key ratios
    if screener_data and screener_data.get("ratios"):
        ratios = screener_data["ratios"]
        lines.append("  KEY RATIOS")
        lines.append("-" * 70)
        ratio_pairs = [
            ("Market Cap", ratios.get("Market Cap")),
            ("PE", ratios.get("Stock P/E")),
            ("Industry PE", ratios.get("Industry PE")),
            ("ROCE", ratios.get("ROCE")),
            ("ROE", ratios.get("ROE")),
            ("D/E", ratios.get("Debt to equity")),
            ("Book Value", ratios.get("Book Value")),
            ("Div Yield", ratios.get("Dividend Yield")),
            ("Promoter %", ratios.get("Promoter holding")),
        ]
        col1 = ratio_pairs[:5]
        col2 = ratio_pairs[5:]
        for i in range(max(len(col1), len(col2))):
            left = f"  {col1[i][0]:>15}: {_fmt_val(col1[i][1]):<15}" if i < len(col1) else " " * 33
            right = f"  {col2[i][0]:>15}: {_fmt_val(col2[i][1])}" if i < len(col2) else ""
            lines.append(left + right)
        lines.append("")

    # Growth rates
    if screener_data and screener_data.get("growth_rates"):
        gr = screener_data["growth_rates"]
        lines.append("  GROWTH RATES (CAGR)")
        lines.append("-" * 70)
        for metric in ["Sales", "Profit", "Stock Price", "ROE"]:
            key_base = metric.lower().replace(" ", "_")
            vals = []
            for period in ["10y", "5y", "3y", "ttm"]:
                key = f"{key_base}_{period}"
                v = gr.get(key)
                vals.append(f"{period.upper():>4}: {_fmt_pct(v)}")
            lines.append(f"  {metric:>12}:  {'  |  '.join(vals)}")
        lines.append("")

    # Technical indicators
    if technical_data:
        lines.append("  TECHNICAL INDICATORS")
        lines.append("-" * 70)
        tech_items = [
            ("Price", _fmt_val(technical_data.get("current_price"))),
            ("RSI(14)", _fmt_val(technical_data.get("rsi"))),
            ("50 DMA", _fmt_val(technical_data.get("dma_50"))),
            ("200 DMA", _fmt_val(technical_data.get("dma_200"))),
            ("52W High", _fmt_val(technical_data.get("high_52w"))),
            ("52W Low", _fmt_val(technical_data.get("low_52w"))),
            ("MACD", _fmt_val(technical_data.get("macd_line"))),
            ("Signal", _fmt_val(technical_data.get("macd_signal"))),
            ("Vol Trend", technical_data.get("volume_trend", "N/A")),
        ]
        for i in range(0, len(tech_items), 3):
            row = tech_items[i:i+3]
            parts = [f"  {name:>10}: {val:<15}" for name, val in row]
            lines.append("".join(parts))
        lines.append("")

    # Shareholding (use original parallel-array format for display)
    if screener_data and (screener_data.get("shareholding_original") or screener_data.get("shareholding")):
        sh = screener_data.get("shareholding_original") or screener_data.get("shareholding")
        if not isinstance(sh, dict):
            sh = {}
        quarters = sh.get("quarters", [])
        if quarters:
            lines.append("  SHAREHOLDING PATTERN")
            lines.append("-" * 70)
            header = f"  {'':>12}" + "".join(f"{q:>12}" for q in quarters[-4:])
            lines.append(header)
            for label, key in [("Promoters", "promoters"), ("FII", "fiis"), ("DII", "diis"), ("Public", "public")]:
                vals = sh.get(key, [])[-4:]
                row = f"  {label:>12}" + "".join(f"{_fmt_pct(v):>12}" for v in vals)
                lines.append(row)
            pledged = sh.get("pledged", [])[-4:]
            if any(_parse_pct(p) > 0 for p in pledged if p):
                row = f"  {'Pledged':>12}" + "".join(f"{_fmt_pct(v):>12}" for v in pledged)
                lines.append(row)
            lines.append("")

    # Scores breakdown — prefer verdict's weighted scores, fall back to raw scores
    verdict_scores = verdict.get("scores", {}) if verdict else {}
    display_scores = verdict_scores or scores
    if display_scores:
        lines.append("  SCORE BREAKDOWN")
        lines.append("-" * 70)
        for category in ["fundamental", "valuation", "technical", "momentum", "risk"]:
            s = display_scores.get(category, {})
            if isinstance(s, dict):
                sc = s.get("score", "N/A")
                wt = s.get("weight", 0)
                wtd = s.get("weighted", 0)
                if wt > 0:
                    lines.append(f"  {category.upper():>15}: {sc:>5}/100  (weight: {wt:.0%}, contribution: {wtd:.1f})")
                else:
                    lines.append(f"  {category.upper():>15}: {sc:>5}/100")
            elif isinstance(s, (int, float)):
                lines.append(f"  {category.upper():>15}: {s:>5}/100")
        lines.append("")

    # Pros & Cons
    if screener_data and screener_data.get("pros_cons"):
        pc = screener_data["pros_cons"]
        pros = pc.get("pros", [])
        cons = pc.get("cons", [])
        if pros:
            lines.append("  STRENGTHS (Screener.in)")
            for p in pros[:5]:
                lines.append(f"    + {p}")
        if cons:
            lines.append("  WEAKNESSES (Screener.in)")
            for c in cons[:5]:
                lines.append(f"    - {c}")
        lines.append("")

    # Verdict reasons
    if verdict and verdict.get("reasons"):
        lines.append("  VERDICT REASONING")
        lines.append("-" * 70)
        for r in verdict["reasons"]:
            lines.append(f"    * {r}")
        lines.append("")

    # Flags
    if verdict and verdict.get("flags"):
        lines.append("  WARNING FLAGS")
        lines.append("-" * 70)
        for f in verdict["flags"]:
            lines.append(f"    ! {f}")
        lines.append("")

    # Data quality
    if data_quality:
        lines.append("  DATA SOURCES")
        lines.append("-" * 70)
        for src, status in data_quality.items():
            if src != "completeness_pct":
                lines.append(f"    {src:>15}: {status}")
        comp = data_quality.get("completeness_pct", "N/A")
        lines.append(f"    {'Completeness':>15}: {comp}%")
        lines.append("")

    lines.append(hr)
    return "\n".join(lines)


def format_comparison_terminal(results: List[dict]) -> str:
    """Format multiple stocks into a comparison table."""
    if not results:
        return "No results to compare."

    lines = []
    lines.append("=" * 100)
    lines.append("  STOCK COMPARISON")
    lines.append("=" * 100)

    # Header
    symbols = [r.get("meta", {}).get("symbol", "?") for r in results]
    header = f"  {'Metric':>20}" + "".join(f"{s:>14}" for s in symbols)
    lines.append(header)
    lines.append("-" * 100)

    # Extract data rows (Screener uses "Stock P/E", "ROCE" etc. as keys)
    def _get_ratio(r, *keys):
        ratios = r.get("fundamentals", {}).get("ratios", {})
        for k in keys:
            v = ratios.get(k)
            if v is not None:
                return v
        return None

    metrics = [
        ("Verdict", lambda r: r.get("verdict", {}).get("verdict", "N/A")),
        ("Score", lambda r: f"{r.get('verdict', {}).get('composite_score', 0):.0f}"),
        ("PE", lambda r: _fmt_val(_get_ratio(r, "Stock P/E", "pe"))),
        ("ROCE", lambda r: _fmt_val(_get_ratio(r, "ROCE", "roce"))),
        ("ROE", lambda r: _fmt_val(_get_ratio(r, "ROE", "roe"))),
        ("D/E", lambda r: _fmt_val(_get_ratio(r, "Debt to equity", "debt_to_equity"))),
        ("Div Yield", lambda r: _fmt_val(_get_ratio(r, "Dividend Yield", "dividend_yield"))),
        ("RSI", lambda r: _fmt_val(r.get("technicals", {}).get("rsi"))),
        ("52W From High", lambda r: _fmt_val(r.get("technicals", {}).get("pct_from_52w_high"))),
    ]

    for label, extractor in metrics:
        row = f"  {label:>20}"
        for r in results:
            val = extractor(r)
            row += f"{val:>14}"
        lines.append(row)

    lines.append("=" * 100)
    return "\n".join(lines)


def _fmt_val(val) -> str:
    if val is None:
        return "N/A"
    if isinstance(val, float):
        return f"{val:.2f}"
    return str(val)


def _parse_pct(val) -> float:
    """Parse percentage value (string '50.10%' or number) to float."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace('%', '').replace(',', '')
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _fmt_pct(val) -> str:
    if val is None:
        return "N/A"
    if isinstance(val, (int, float)):
        return f"{val:.1f}%"
    return str(val)
