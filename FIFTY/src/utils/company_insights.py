"""
Company Insights - Screener.in integration for signal notifications

Provides compact company snapshots and full reports by leveraging
the Helper/helper/stock_report.py scraper.

Uses in-memory cache with 24h TTL to avoid repeat scrapes.
"""

import sys
import os
import time
import importlib.util
from typing import Dict, Optional, Tuple

from loguru import logger

# Import stock_report directly by file path (avoids __init__.py / namespace issues)
_stock_report_path = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'Helper', 'helper', 'stock_report.py')
)
_IMPORT_OK = False
try:
    _spec = importlib.util.spec_from_file_location("stock_report", _stock_report_path)
    _stock_report = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_stock_report)

    fetch_best_page = _stock_report.fetch_best_page
    extract_top_ratios = _stock_report.extract_top_ratios
    extract_sector_info = _stock_report.extract_sector_info
    extract_shareholding = _stock_report.extract_shareholding
    extract_quarterly_results = _stock_report.extract_quarterly_results
    extract_balance_sheet_highlights = _stock_report.extract_balance_sheet_highlights
    build_telegram_message = _stock_report.build_telegram_message
    parse_number = _stock_report.parse_number
    clean_ratio = _stock_report.clean_ratio
    ensure_pct = _stock_report.ensure_pct
    shareholding_signal = _stock_report.shareholding_signal
    _IMPORT_OK = True
except Exception as e:
    logger.error(f"Failed to import stock_report from {_stock_report_path}: {e}")

# Cache: {symbol: (compact_snapshot, full_report, timestamp)}
_cache: Dict[str, Tuple[Optional[str], Optional[str], float]] = {}
CACHE_TTL = 24 * 60 * 60  # 24 hours


def _build_compact_snapshot(symbol: str) -> Tuple[Optional[str], Optional[str]]:
    """Scrape screener.in and build both compact snapshot and full report.

    Returns:
        (compact_snapshot, full_report) - either can be None on failure
    """
    if not _IMPORT_OK:
        return None, None

    soup, page_type = fetch_best_page(symbol)

    # Full report
    full_report = build_telegram_message(symbol, soup, page_type)

    # Compact snapshot (4 lines)
    ratios = extract_top_ratios(soup)
    sector = extract_sector_info(soup)
    holding = extract_shareholding(soup)
    quarterly = extract_quarterly_results(soup)

    balance = extract_balance_sheet_highlights(soup)

    cr = clean_ratio
    mcap = cr(ratios.get("Market Cap", "N/A"))
    pe = cr(ratios.get("Stock P/E", ratios.get("Price to Earning", "N/A")))
    roce = cr(ratios.get("ROCE", "N/A"))
    roe = cr(ratios.get("ROE", "N/A"))
    debt_eq = cr(ratios.get("Debt to equity", "N/A"))
    opm_latest = quarterly.get("opm", [])[-1] if quarterly.get("opm") else "N/A"

    # Fallback D/E from balance sheet (borrowings/reserves)
    borrowings_num = parse_number(balance.get("borrowings") or "")
    reserves_num = parse_number(balance.get("reserves") or "")
    debt_to_reserve = None
    if borrowings_num and reserves_num and reserves_num > 0:
        debt_to_reserve = borrowings_num / reserves_num

    # Sector - use last segment only
    sector_short = sector.split(" > ")[-1] if " > " in sector else sector

    # Format MCap nicely
    mcap_num = parse_number(mcap)
    if mcap_num is not None:
        if mcap_num >= 100000:
            mcap_display = f"{mcap_num / 100000:.1f}L Cr"
        elif mcap_num >= 1:
            mcap_display = f"{mcap_num:,.0f} Cr"
        else:
            mcap_display = mcap
    else:
        mcap_display = mcap

    # Build pros/cons (short version - top 2 each)
    pe_num = parse_number(pe)
    roce_num = parse_number(roce)
    roe_num = parse_number(roe)
    debt_num = parse_number(debt_eq)
    opm_num = parse_number(opm_latest)

    pros = []
    cons = []

    if roce_num and roce_num > 20:
        pros.append("Strong ROCE")
    elif roce_num and roce_num > 15:
        pros.append("Decent ROCE")
    elif roce_num and roce_num < 10:
        cons.append("Weak ROCE")

    if roe_num and roe_num > 15:
        pros.append("Good ROE")
    elif roe_num and roe_num < 8:
        cons.append("Low ROE")

    if debt_num is not None:
        if debt_num < 0.5:
            pros.append("Low debt")
        elif debt_num > 1.0:
            cons.append("High debt")
    elif debt_to_reserve is not None:
        if debt_to_reserve < 0.3:
            pros.append("Low debt")
        elif debt_to_reserve > 1.0:
            cons.append("Heavy debt")

    if pe_num:
        if pe_num < 15:
            pros.append("Attractive PE")
        elif pe_num > 25:
            cons.append("PE higher side")

    if opm_num and opm_num > 20:
        pros.append("Healthy margins")
    elif opm_num and opm_num < 8:
        cons.append("Thin margins")

    # Shareholding signals
    hold_signals = shareholding_signal(holding)
    for sig in hold_signals:
        if "FII decreasing" in sig:
            cons.append("FII reducing")
        elif "FII increasing" in sig:
            pros.append("FII accumulating")
        if "Promoter decreasing" in sig:
            cons.append("Promoter declining")

    # Limit to top 2 each
    pros = pros[:2]
    cons = cons[:2]

    lines = [
        f"\u2501\u2501\u2501\u2501\u2501 Company Snapshot \u2501\u2501\u2501\u2501\u2501",
        f"\U0001f3ed {sector_short} | MCap \u20b9{mcap_display}",
        f"\U0001f4ca PE: {pe} | ROCE: {ensure_pct(roce)} | ROE: {ensure_pct(roe)}",
        f"\U0001f4b0 D/E: {debt_eq if debt_eq != 'N/A' else (f'{debt_to_reserve:.1f}*' if debt_to_reserve is not None else 'N/A')} | OPM: {opm_latest}",
    ]

    if pros:
        lines.append(f"\u2705 {' | '.join(pros)}")
    if cons:
        lines.append(f"\u26a0\ufe0f {' | '.join(cons)}")

    compact = "\n".join(lines)
    return compact, full_report


def get_compact_snapshot(symbol: str) -> Optional[str]:
    """Get compact company snapshot for signal notification.

    Returns formatted string or None on failure.
    Cached for 24 hours.
    """
    symbol = symbol.upper()

    # Check cache
    if symbol in _cache:
        snapshot, _, ts = _cache[symbol]
        if time.time() - ts < CACHE_TTL:
            return snapshot

    try:
        snapshot, full_report = _build_compact_snapshot(symbol)
        _cache[symbol] = (snapshot, full_report, time.time())
        return snapshot
    except Exception as e:
        logger.warning(f"Failed to get company snapshot for {symbol}: {e}")
        return None


def get_full_report(symbol: str) -> Optional[str]:
    """Get full company report for details button.

    Returns formatted HTML string or None on failure.
    Cached for 24 hours.
    """
    symbol = symbol.upper()

    # Check cache
    if symbol in _cache:
        _, full_report, ts = _cache[symbol]
        if time.time() - ts < CACHE_TTL:
            return full_report

    try:
        snapshot, full_report = _build_compact_snapshot(symbol)
        _cache[symbol] = (snapshot, full_report, time.time())
        return full_report
    except Exception as e:
        logger.warning(f"Failed to get full report for {symbol}: {e}")
        return None
