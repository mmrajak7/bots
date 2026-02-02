#!/usr/bin/env python3
"""
Stock Fundamental Report - Screener.in Scraper

Scrapes screener.in public pages and outputs a compact Telegram-friendly
report for quick buy/no-buy decisions.

Usage:
    python stock_report.py RELIANCE
    python stock_report.py HDFCBANK
    python stock_report.py TCS --raw    # Also print raw scraped data

@author   Helper Bot
@created  2026-02-01
"""

import sys
import os
import re
import json
import logging
import argparse
from typing import Optional, List, Dict, Tuple

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore

import requests
from bs4 import BeautifulSoup, Tag

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

SCREENER_URL = "https://www.screener.in/company/{symbol}/consolidated/"
SCREENER_URL_STANDALONE = "https://www.screener.in/company/{symbol}/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


# ─── Scraping ────────────────────────────────────────────────────────────────

def fetch_page(url: str) -> Optional[BeautifulSoup]:
    """Fetch a single screener.in URL. Returns None on failure."""
    log.info("Fetching %s", url)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            return BeautifulSoup(resp.text, "html.parser")
        log.warning("Got %d for %s", resp.status_code, url)
    except requests.RequestException as e:
        log.warning("Request failed for %s: %s", url, e)
    return None


def fetch_best_page(symbol: str) -> Tuple[BeautifulSoup, str]:
    """Fetch screener.in page. Tries consolidated first, falls back to standalone.
    If consolidated has sparse quarterly data, also fetches standalone and picks the richer one.
    Returns (soup, page_type) where page_type is 'consolidated' or 'standalone'.
    """
    symbol = symbol.upper()
    consolidated_url = SCREENER_URL.format(symbol=symbol)
    standalone_url = SCREENER_URL_STANDALONE.format(symbol=symbol)

    con_soup = fetch_page(consolidated_url)
    if con_soup is None:
        std_soup = fetch_page(standalone_url)
        if std_soup is None:
            raise ValueError("Could not fetch screener.in page for %s" % symbol)
        return std_soup, "standalone"

    # Check if consolidated has quarterly data
    con_quarterly = extract_quarterly_results(con_soup)
    con_q_count = len(con_quarterly.get("sales", []))

    if con_q_count >= 5:
        # Consolidated has enough quarterly data
        return con_soup, "consolidated"

    # Sparse consolidated quarterly - try standalone for richer data
    std_soup = fetch_page(standalone_url)
    if std_soup is None:
        return con_soup, "consolidated"

    std_quarterly = extract_quarterly_results(std_soup)
    std_q_count = len(std_quarterly.get("sales", []))

    if std_q_count > con_q_count:
        log.info("Using standalone (has %d quarters vs consolidated %d)", std_q_count, con_q_count)
        return std_soup, "standalone"

    return con_soup, "consolidated"


def parse_number(text: str) -> Optional[float]:
    """Parse number from screener text like '1,23,456' or '23.8 %' or '₹ 648'."""
    if not text:
        return None
    cleaned = text.replace("₹", "").replace("%", "").replace(",", "").strip()
    if not cleaned or cleaned == "--":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_top_ratios(soup: BeautifulSoup) -> dict:
    """Extract key ratios from the top section of the page."""
    ratios = {}
    # Ratios are in #top-ratios-wrap or similar ul/li structure
    for li in soup.select("li.flex.flex-space-between"):
        name_el = li.select_one("span.name")
        value_el = li.select_one("span.number, span.value")
        if name_el and value_el:
            name = name_el.get_text(strip=True)
            value = value_el.get_text(strip=True)
            ratios[name] = value

    # Fallback: scan all li elements with Name/Number pattern
    if not ratios:
        for li in soup.find_all("li"):
            spans = li.find_all("span")
            if len(spans) >= 2:
                name = spans[0].get_text(strip=True)
                value = spans[-1].get_text(strip=True)
                if name and value and any(k in name for k in [
                    "Market Cap", "Current Price", "High", "Low", "Stock P/E",
                    "Book Value", "Dividend Yield", "ROCE", "ROE", "Face Value",
                    "P/E", "PEG Ratio", "Industry PE", "Intrinsic Value",
                    "Graham Number", "Piotroski", "Debt to equity",
                    "Promoter holding", "Price to Earning"
                ]):
                    ratios[name] = value

    return ratios


def extract_shareholding(soup: BeautifulSoup) -> dict:
    """Extract shareholding pattern - latest and previous quarters."""
    result = {"quarters": [], "promoters": [], "fiis": [], "diis": [], "public": [], "government": []}
    section = soup.find("section", id="shareholding")
    if not section:
        heading = soup.find(string=re.compile(r"Shareholding\s+Pattern", re.I))
        if heading:
            section = heading.find_parent("section")
    if not section:
        log.warning("Shareholding section not found")
        return result

    tables = section.find_all("table")
    if not tables:
        return result

    table = tables[0]
    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"])
        texts = [c.get_text(strip=True) for c in cells]
        if not texts:
            continue

        label = texts[0].lower()
        values = texts[1:]

        if not result["quarters"] and any(re.match(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", v.lower()) for v in values):
            result["quarters"] = values
        elif "promoter" in label:
            result["promoters"] = values
        elif "fii" in label or "foreign" in label:
            result["fiis"] = values
        elif "dii" in label or "domestic" in label:
            result["diis"] = values
        elif "public" in label:
            result["public"] = values
        elif "government" in label or "govt" in label:
            result["government"] = values

    return result


def _parse_table_rows(section) -> dict:
    """Parse a screener table section into {label: [values]} dict."""
    result = {"headers": [], "rows": {}}
    if not section:
        return result
    table = section.find("table")
    if not table:
        return result

    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"])
        texts = [c.get_text(strip=True) for c in cells]
        if not texts:
            continue

        label = texts[0].lower().strip().rstrip("+")
        values = texts[1:]

        if not result["headers"] and any(re.match(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|ttm)", v.lower()) for v in values):
            result["headers"] = values
        else:
            result["rows"][label] = values

    return result


def extract_quarterly_results(soup: BeautifulSoup) -> dict:
    """Extract recent quarterly results (sales, profit, OPM)."""
    result = {"quarters": [], "sales": [], "opm": [], "net_profit": [], "eps": []}
    section = soup.find("section", id="quarters")
    if not section:
        heading = soup.find(string=re.compile(r"Quarterly\s+Results", re.I))
        if heading:
            section = heading.find_parent("section")
    if not section:
        return result

    parsed = _parse_table_rows(section)
    result["quarters"] = parsed["headers"]

    for label, values in parsed["rows"].items():
        if label.startswith("sales") or label.startswith("revenue"):
            result["sales"] = values
        elif "opm" in label:
            result["opm"] = values
        elif "net profit" in label or "profit after tax" in label:
            result["net_profit"] = values
        elif label.startswith("eps"):
            result["eps"] = values

    return result


def extract_annual_results(soup: BeautifulSoup) -> dict:
    """Extract annual P&L (sales, profit growth trend)."""
    result = {"years": [], "sales": [], "net_profit": [], "opm": [], "eps": []}
    section = soup.find("section", id="profit-loss")
    if not section:
        return result

    parsed = _parse_table_rows(section)
    result["years"] = parsed["headers"]

    for label, values in parsed["rows"].items():
        if label.startswith("sales") or label.startswith("revenue"):
            result["sales"] = values
        elif "opm" in label:
            result["opm"] = values
        elif "net profit" in label or "profit after tax" in label:
            result["net_profit"] = values
        elif label.startswith("eps"):
            result["eps"] = values

    return result


def extract_balance_sheet_highlights(soup: BeautifulSoup) -> dict:
    """Extract key balance sheet items: debt, reserves."""
    result = {"borrowings": None, "reserves": None, "total_assets": None}
    section = soup.find("section", id="balance-sheet")
    if not section:
        return result

    parsed = _parse_table_rows(section)
    for label, values in parsed["rows"].items():
        if not values:
            continue
        latest = values[-1]
        if "borrowing" in label:
            result["borrowings"] = latest
        elif "reserves" in label:
            result["reserves"] = latest
        elif "total assets" in label or "total liabilities" in label:
            result["total_assets"] = latest

    return result


def extract_company_name(soup: BeautifulSoup) -> str:
    """Extract full company name from page."""
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    return "Unknown"


def extract_sector_info(soup: BeautifulSoup) -> str:
    """Extract sector/industry classification from peers section."""
    section = soup.find("section", id="peers")
    if not section:
        return "N/A"
    links = []
    for a in section.find_all("a"):
        text = a.get_text(strip=True)
        if any(x in text for x in ["Nifty", "BSE", "Sensex"]):
            break
        if text and len(text) < 60:
            links.append(text)
    return " > ".join(links[:4]) if links else "N/A"


# ─── Analysis Helpers ─────────────────────────────────────────────────────────

def calc_growth(values: List[str], periods: int = 1) -> Optional[float]:
    """Calculate growth % between latest and N periods back."""
    nums = [parse_number(v) for v in values]
    if len(nums) < periods + 1:
        return None
    latest = nums[-1]
    older = nums[-(periods + 1)]
    if latest is None or older is None or older == 0:
        return None
    return ((latest - older) / abs(older)) * 100


def calc_trend(values: List[str]) -> str:
    """Return trend arrow based on last 3-4 data points."""
    nums = [parse_number(v) for v in values[-4:]]
    nums = [n for n in nums if n is not None]
    if len(nums) < 2:
        return "—"
    if all(nums[i] <= nums[i + 1] for i in range(len(nums) - 1)):
        return "↑"
    if all(nums[i] >= nums[i + 1] for i in range(len(nums) - 1)):
        return "↓"
    return "↔"


def shareholding_signal(data: dict) -> List[str]:
    """Analyze shareholding for buy/sell signals."""
    signals = []
    for label, key in [("FII", "fiis"), ("DII", "diis"), ("Promoter", "promoters")]:
        vals = data.get(key, [])
        if len(vals) >= 2:
            nums = [parse_number(v) for v in vals[-4:]]
            nums = [n for n in nums if n is not None]
            if len(nums) >= 2:
                latest = nums[-1]
                prev = nums[-2]
                diff = latest - prev
                trend = calc_trend(vals[-4:])
                if abs(diff) > 0.5:
                    direction = "increasing" if diff > 0 else "decreasing"
                    signals.append("%s %s (%+.1f%%) %s" % (label, direction, diff, trend))
    return signals


# ─── Telegram Formatter ──────────────────────────────────────────────────────

def format_cr(value_str: str) -> str:
    """Format large numbers for display."""
    n = parse_number(value_str)
    if n is None:
        return value_str
    if abs(n) >= 100000:
        return "₹%.1fL Cr" % (n / 100000)
    if abs(n) >= 100:
        return "₹%s Cr" % "{:,.0f}".format(n)
    return "₹%.1f" % n


def ensure_pct(v: str) -> str:
    """Ensure value ends with % if it's a percentage field."""
    return v if v.endswith("%") or v == "N/A" else v + "%"


def clean_ratio(val: str) -> str:
    """Strip ₹ prefix from ratio values for clean display."""
    return val.replace("₹", "").strip()


def build_telegram_message(symbol: str, soup: BeautifulSoup, page_type: str) -> str:
    """Build compact Telegram message with pros/cons conclusion."""
    name = extract_company_name(soup)
    sector = extract_sector_info(soup)
    ratios = extract_top_ratios(soup)
    holding = extract_shareholding(soup)
    quarterly = extract_quarterly_results(soup)
    annual = extract_annual_results(soup)
    balance = extract_balance_sheet_highlights(soup)

    # ── Parse all numbers upfront
    cr = clean_ratio
    mcap = cr(ratios.get("Market Cap", "N/A"))
    cmp = cr(ratios.get("Current Price", "N/A"))
    pe = cr(ratios.get("Stock P/E", ratios.get("Price to Earning", "N/A")))
    pb = cr(ratios.get("Book Value", "N/A"))
    div_yield = cr(ratios.get("Dividend Yield", "N/A"))
    roce = cr(ratios.get("ROCE", "N/A"))
    roe = cr(ratios.get("ROE", "N/A"))
    high_low = cr(ratios.get("High / Low", "N/A"))
    debt_eq = cr(ratios.get("Debt to equity", "N/A"))

    pe_num = parse_number(pe)
    roce_num = parse_number(roce)
    roe_num = parse_number(roe)
    debt_num = parse_number(debt_eq)
    div_num = parse_number(div_yield)
    cmp_num = parse_number(cmp)

    latest_qtr = quarterly.get("quarters", [])[-1] if quarterly.get("quarters") else "?"
    hold_qtr = holding.get("quarters", [])[-1] if holding.get("quarters") else "?"

    # ── Growth
    g = lambda vals, p: calc_growth(vals, p) if len(vals) >= p + 1 else None
    q_sales = quarterly.get("sales", [])
    q_profit = quarterly.get("net_profit", [])
    a_sales = annual.get("sales", [])
    a_profit = annual.get("net_profit", [])

    sales_qoq = g(q_sales, 1)
    sales_yoy = g(q_sales, 4)
    profit_qoq = g(q_profit, 1)
    profit_yoy = g(q_profit, 4)
    sales_3yr = g(a_sales, 3)
    profit_3yr = g(a_profit, 3)

    opm_latest = quarterly.get("opm", [])[-1] if quarterly.get("opm") else "N/A"
    opm_trend = calc_trend(quarterly.get("opm", []))
    eps_latest = quarterly.get("eps", [])[-1] if quarterly.get("eps") else "N/A"

    # ── Shareholding
    def lh(key):
        vals = holding.get(key, [])
        return vals[-1] if vals else "N/A"

    promo_pct = lh("promoters")
    fii_pct = lh("fiis")
    dii_pct = lh("diis")
    pub_pct = lh("public")
    hold_signals = shareholding_signal(holding)

    # ── 52W position
    hi_lo_parts = high_low.split("/") if "/" in str(high_low) else []
    pct_from_high = pct_from_low = None
    if len(hi_lo_parts) == 2 and cmp_num:
        hi = parse_number(hi_lo_parts[0])
        lo = parse_number(hi_lo_parts[1])
        if hi and lo and hi != lo:
            pct_from_high = ((hi - cmp_num) / hi) * 100
            pct_from_low = ((cmp_num - lo) / lo) * 100

    # ── Debt ratio from balance sheet (fallback if D/E not in ratios)
    borrowings_num = parse_number(balance.get("borrowings") or "")
    reserves_num = parse_number(balance.get("reserves") or "")
    debt_to_reserve = None
    if borrowings_num and reserves_num and reserves_num > 0:
        debt_to_reserve = borrowings_num / reserves_num

    def fg(val):
        if val is None: return "—"
        return "%s%.0f%%" % ("+" if val > 0 else "", val)

    # ═══════════════════════════════════════════════════════════════════════
    # ── PROS / CONS engine
    # ═══════════════════════════════════════════════════════════════════════
    pros = []   # type: List[str]
    cons = []   # type: List[str]

    # -- Valuation
    if pe_num:
        if pe_num < 15:
            pros.append("Attractive PE (%.1f)" % pe_num)
        elif pe_num > 40:
            cons.append("Expensive PE (%.1f)" % pe_num)
        elif pe_num > 25:
            cons.append("PE on higher side (%.1f)" % pe_num)

    # -- Profitability
    if roce_num:
        if roce_num > 20:
            pros.append("Strong ROCE %s" % ensure_pct(roce))
        elif roce_num > 15:
            pros.append("Decent ROCE %s" % ensure_pct(roce))
        elif roce_num < 10:
            cons.append("Weak ROCE %s" % ensure_pct(roce))

    if roe_num:
        if roe_num > 15:
            pros.append("Good ROE %s" % ensure_pct(roe))
        elif roe_num < 8:
            cons.append("Low ROE %s" % ensure_pct(roe))

    # -- OPM
    opm_num = parse_number(opm_latest)
    if opm_num:
        if opm_num > 20:
            pros.append("Healthy margins (OPM %s)" % opm_latest)
        elif opm_num < 8:
            cons.append("Thin margins (OPM %s)" % opm_latest)

    # -- Growth
    if profit_yoy is not None:
        if profit_yoy > 20:
            pros.append("Profit growing YoY %s" % fg(profit_yoy))
        elif profit_yoy < -15:
            cons.append("Profit falling YoY %s" % fg(profit_yoy))
        elif profit_yoy < 0:
            cons.append("Profit dipped YoY %s" % fg(profit_yoy))

    if profit_3yr is not None:
        if profit_3yr > 50:
            pros.append("Strong 3Y profit growth %s" % fg(profit_3yr))
        elif profit_3yr < -20:
            cons.append("3Y profit erosion %s" % fg(profit_3yr))

    if sales_yoy is not None:
        if sales_yoy > 15:
            pros.append("Revenue growing YoY %s" % fg(sales_yoy))
        elif sales_yoy < -10:
            cons.append("Revenue shrinking YoY %s" % fg(sales_yoy))

    # -- Debt
    if debt_num is not None:
        if debt_num < 0.3:
            pros.append("Very low debt (D/E %.1f)" % debt_num)
        elif debt_num < 0.5:
            pros.append("Low debt (D/E %.1f)" % debt_num)
        elif debt_num > 1.5:
            cons.append("High debt (D/E %.1f)" % debt_num)
        elif debt_num > 1.0:
            cons.append("Moderate debt (D/E %.1f)" % debt_num)
    elif debt_to_reserve is not None:
        if debt_to_reserve < 0.3:
            pros.append("Low debt (Borrow/Reserve %.1fx)" % debt_to_reserve)
        elif debt_to_reserve > 1.0:
            cons.append("Heavy debt (Borrow/Reserve %.1fx)" % debt_to_reserve)
        elif debt_to_reserve > 0.6:
            cons.append("Moderate debt (Borrow/Reserve %.1fx)" % debt_to_reserve)

    # -- Dividend
    if div_num:
        if div_num > 2.0:
            pros.append("Good dividend %s" % ensure_pct(div_yield))
        elif div_num < 0.3 and pe_num and pe_num > 20:
            cons.append("Negligible dividend")

    # -- 52W position
    if pct_from_high is not None:
        if pct_from_high > 30:
            cons.append("%.0f%% off 52W high" % pct_from_high)
        elif pct_from_high < 10:
            pros.append("Near 52W high (%.0f%% away)" % pct_from_high)
    if pct_from_low is not None and pct_from_low < 5:
        cons.append("Near 52W low")

    # -- Shareholding
    promo_num = parse_number(promo_pct)
    fii_num = parse_number(fii_pct)
    dii_num = parse_number(dii_pct)

    if promo_num:
        if promo_num > 60:
            pros.append("High promoter holding %.0f%%" % promo_num)
        elif promo_num < 30 and promo_num > 0:
            cons.append("Low promoter holding %.0f%%" % promo_num)

    for sig in hold_signals:
        if "FII increasing" in sig:
            pros.append("FII accumulating")
        elif "FII decreasing" in sig:
            cons.append("FII reducing")
        if "DII increasing" in sig:
            pros.append("DII accumulating")
        elif "DII decreasing" in sig:
            cons.append("DII reducing")
        if "Promoter decreasing" in sig:
            cons.append("Promoter stake declining")
        elif "Promoter increasing" in sig:
            pros.append("Promoter increasing stake")

    # ═══════════════════════════════════════════════════════════════════════
    # ── Format message
    # ═══════════════════════════════════════════════════════════════════════
    source = "C" if page_type == "consolidated" else "S"
    pos_52w = ""
    if pct_from_high is not None:
        pos_52w = " (%.0f%% off high)" % pct_from_high

    msg = "<b>%s</b> | %s\n" % (name, symbol)
    msg += "%s | ₹%s | MCap ₹%s\n" % (sector.split(" > ")[-1] if " > " in sector else sector, cmp, mcap)
    msg += "\n"

    # Row 1: PE, ROCE, ROE
    msg += "PE: %s | ROCE: %s | ROE: %s\n" % (pe, ensure_pct(roce), ensure_pct(roe))
    # Row 2: PB, D/E, DivYld
    msg += "PB: %s | D/E: %s | Div: %s\n" % (pb, debt_eq, ensure_pct(div_yield))
    # Row 3: 52W
    msg += "52W: ₹%s%s\n" % (high_low, pos_52w)
    msg += "\n"

    # Growth block
    msg += "<b>Growth</b> <i>(%s)</i>\n" % latest_qtr
    msg += "Sales: QoQ %s  YoY %s  3Y %s\n" % (fg(sales_qoq), fg(sales_yoy), fg(sales_3yr))
    msg += "Profit: QoQ %s  YoY %s  3Y %s\n" % (fg(profit_qoq), fg(profit_yoy), fg(profit_3yr))
    msg += "OPM: %s %s | EPS: %s\n" % (opm_latest, opm_trend, eps_latest)
    msg += "\n"

    # Holding block (single line)
    msg += "<b>Holding</b> <i>(%s)</i>\n" % hold_qtr
    msg += "Prom: %s | FII: %s | DII: %s\n" % (ensure_pct(promo_pct), ensure_pct(fii_pct), ensure_pct(dii_pct))
    if hold_signals:
        msg += "  " + " | ".join(hold_signals) + "\n"

    # Debt (only if meaningful)
    if borrowings_num and borrowings_num > 10:
        msg += "\nDebt: %s" % format_cr(balance["borrowings"])
        if reserves_num:
            msg += " vs Reserves: %s" % format_cr(balance["reserves"])
        msg += "\n"

    # ── CONCLUSION
    msg += "\n"
    if pros:
        msg += "<b>PROS:</b> " + " | ".join(pros) + "\n"
    if cons:
        msg += "<b>CONS:</b> " + " | ".join(cons) + "\n"

    if not pros and not cons:
        msg += "<b>No strong signals either way</b>\n"

    msg += "\n<i>[%s]</i>" % source

    return msg


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Stock fundamental report from screener.in")
    parser.add_argument("symbol", help="Stock symbol (e.g., RELIANCE, HDFCBANK)")
    parser.add_argument("--raw", action="store_true", help="Also print raw scraped data as JSON")
    args = parser.parse_args()

    symbol = args.symbol.upper()
    soup, page_type = fetch_best_page(symbol)

    if args.raw:
        raw = {
            "page_type": page_type,
            "ratios": extract_top_ratios(soup),
            "shareholding": extract_shareholding(soup),
            "quarterly": extract_quarterly_results(soup),
            "annual": extract_annual_results(soup),
            "balance_sheet": extract_balance_sheet_highlights(soup),
            "sector": extract_sector_info(soup),
        }
        print(json.dumps(raw, indent=2, ensure_ascii=False))
        print("\n" + "=" * 60 + "\n")

    msg = build_telegram_message(symbol, soup, page_type)
    print(msg)


if __name__ == "__main__":
    main()
