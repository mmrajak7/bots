"""
Screener.in — Comprehensive Fundamental Data Scraper

Scrapes screener.in/company/{SYMBOL}/consolidated/ (falls back to
standalone if 404) and extracts:
  1. Top ratios (Market Cap, PE, ROCE, ROE, D/E, etc.)
  2. Quarterly results (last 8 quarters)
  3. Annual P&L (last 5 years)
  4. Balance sheet highlights (last 3 years)
  5. Cash flow (last 3 years)
  6. Shareholding pattern (last 4 quarters)
  7. Compounded growth rates (Sales, Profit, Stock Price, ROE)
  8. Pros / Cons (Screener auto-generated)
  9. Peer comparison

Returns a ScreenerData dataclass. Missing sections return None/empty,
never crash.
"""

import logging
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup, Tag

from .cache import get_cache

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

SCREENER_URL = "https://www.screener.in/company/{symbol}/consolidated/"
SCREENER_URL_STANDALONE = "https://www.screener.in/company/{symbol}/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

CACHE_SOURCE = 'screener'


# ── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class QuarterlyResults:
    """Last N quarters of results."""
    quarters: List[str] = field(default_factory=list)
    sales: List[str] = field(default_factory=list)
    expenses: List[str] = field(default_factory=list)
    opm_pct: List[str] = field(default_factory=list)
    net_profit: List[str] = field(default_factory=list)
    eps: List[str] = field(default_factory=list)


@dataclass
class AnnualResults:
    """Last N years of annual P&L."""
    years: List[str] = field(default_factory=list)
    sales: List[str] = field(default_factory=list)
    opm_pct: List[str] = field(default_factory=list)
    net_profit: List[str] = field(default_factory=list)
    eps: List[str] = field(default_factory=list)


@dataclass
class BalanceSheet:
    """Key balance sheet items for last N years."""
    years: List[str] = field(default_factory=list)
    borrowings: List[str] = field(default_factory=list)
    reserves: List[str] = field(default_factory=list)
    total_assets: List[str] = field(default_factory=list)
    equity_capital: List[str] = field(default_factory=list)


@dataclass
class CashFlow:
    """Cash flow for last N years."""
    years: List[str] = field(default_factory=list)
    operating: List[str] = field(default_factory=list)
    investing: List[str] = field(default_factory=list)
    financing: List[str] = field(default_factory=list)


@dataclass
class Shareholding:
    """Shareholding pattern for last N quarters."""
    quarters: List[str] = field(default_factory=list)
    promoters: List[str] = field(default_factory=list)
    fiis: List[str] = field(default_factory=list)
    diis: List[str] = field(default_factory=list)
    public: List[str] = field(default_factory=list)
    government: List[str] = field(default_factory=list)
    pledged: List[str] = field(default_factory=list)


@dataclass
class GrowthRates:
    """Compounded growth rates from Screener."""
    sales_10y: Optional[str] = None
    sales_5y: Optional[str] = None
    sales_3y: Optional[str] = None
    sales_ttm: Optional[str] = None
    profit_10y: Optional[str] = None
    profit_5y: Optional[str] = None
    profit_3y: Optional[str] = None
    profit_ttm: Optional[str] = None
    stock_price_10y: Optional[str] = None
    stock_price_5y: Optional[str] = None
    stock_price_3y: Optional[str] = None
    stock_price_1y: Optional[str] = None
    roe_10y: Optional[str] = None
    roe_5y: Optional[str] = None
    roe_3y: Optional[str] = None
    roe_latest: Optional[str] = None


@dataclass
class PeerComparison:
    """Sector peer data."""
    sector: str = ""
    peers: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ScreenerData:
    """Complete fundamental data from Screener.in."""
    symbol: str = ""
    company_name: str = ""
    page_type: str = ""  # 'consolidated' or 'standalone'

    # Top ratios
    market_cap: Optional[str] = None
    current_price: Optional[str] = None
    pe: Optional[str] = None
    book_value: Optional[str] = None
    dividend_yield: Optional[str] = None
    roce: Optional[str] = None
    roe: Optional[str] = None
    face_value: Optional[str] = None
    industry_pe: Optional[str] = None
    debt_to_equity: Optional[str] = None
    high_low_52w: Optional[str] = None
    peg_ratio: Optional[str] = None
    intrinsic_value: Optional[str] = None
    graham_number: Optional[str] = None
    piotroski_score: Optional[str] = None
    promoter_holding: Optional[str] = None
    all_ratios: Dict[str, str] = field(default_factory=dict)

    # Sections
    quarterly: Optional[QuarterlyResults] = None
    annual: Optional[AnnualResults] = None
    balance_sheet: Optional[BalanceSheet] = None
    cash_flow: Optional[CashFlow] = None
    shareholding: Optional[Shareholding] = None
    growth_rates: Optional[GrowthRates] = None

    # Pros / Cons
    pros: List[str] = field(default_factory=list)
    cons: List[str] = field(default_factory=list)

    # Peer comparison
    peer_comparison: Optional[PeerComparison] = None

    def to_dict(self) -> dict:
        """Convert to plain dict (JSON-serializable)."""
        return asdict(self)


# ── HTTP Fetching ────────────────────────────────────────────────────────────

def _fetch_page(url: str) -> Optional[BeautifulSoup]:
    """Fetch a single URL, return parsed soup or None."""
    logger.info("Fetching %s", url)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code == 200:
            return BeautifulSoup(resp.text, "html.parser")
        logger.warning("HTTP %d for %s", resp.status_code, url)
    except requests.RequestException as e:
        logger.warning("Request failed for %s: %s", url, e)
    return None


def _fetch_best_page(symbol: str) -> Tuple[Optional[BeautifulSoup], str]:
    """Try consolidated first, fall back to standalone.

    If consolidated has sparse quarterly data (< 5 quarters), compares
    with standalone and picks the richer one.

    Returns (soup, page_type) or (None, '') on total failure.
    """
    symbol = symbol.upper()
    con_url = SCREENER_URL.format(symbol=symbol)
    std_url = SCREENER_URL_STANDALONE.format(symbol=symbol)

    con_soup = _fetch_page(con_url)
    if con_soup is None:
        std_soup = _fetch_page(std_url)
        if std_soup is None:
            return None, ''
        return std_soup, 'standalone'

    # Check if consolidated has enough quarterly data
    con_q = _extract_quarterly(con_soup)
    con_count = len(con_q.quarters) if con_q else 0

    if con_count >= 5:
        return con_soup, 'consolidated'

    # Sparse consolidated — try standalone for richer data
    std_soup = _fetch_page(std_url)
    if std_soup is None:
        return con_soup, 'consolidated'

    std_q = _extract_quarterly(std_soup)
    std_count = len(std_q.quarters) if std_q else 0

    if std_count > con_count:
        logger.info("Using standalone (%d quarters vs consolidated %d)",
                     std_count, con_count)
        return std_soup, 'standalone'

    return con_soup, 'consolidated'


# ── Parsing Helpers ──────────────────────────────────────────────────────────

def _parse_number(text: str) -> Optional[float]:
    """Parse number from screener text like '1,23,456' or '23.8 %' or 'Rs 648'."""
    if not text:
        return None
    cleaned = text.replace("\u20b9", "").replace("₹", "").replace(
        "%", "").replace(",", "").replace("Rs", "").strip()
    if not cleaned or cleaned == "--" or cleaned == "":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _find_section(soup: BeautifulSoup, section_id: str,
                  heading_pattern: Optional[str] = None) -> Optional[Tag]:
    """Find a section by ID, falling back to heading text regex."""
    section = soup.find("section", id=section_id)
    if section:
        return section

    if heading_pattern:
        heading = soup.find(string=re.compile(heading_pattern, re.I))
        if heading:
            parent = heading.find_parent("section")
            if parent:
                return parent
    return None


def _parse_table_rows(section: Optional[Tag]) -> Tuple[List[str], Dict[str, List[str]]]:
    """Parse a screener table into (headers, {label: [values]}).

    Headers are detected by presence of month abbreviations, 'TTM', or year patterns.
    """
    if not section:
        return [], {}

    table = section.find("table")
    if not table:
        return [], {}

    headers: List[str] = []
    rows: Dict[str, List[str]] = {}

    header_pattern = re.compile(
        r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|ttm|\d{4})', re.I
    )

    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"])
        texts = [c.get_text(strip=True) for c in cells]
        if not texts:
            continue

        label = texts[0].lower().strip().rstrip("+")
        values = texts[1:]

        # Detect header row
        if not headers and any(header_pattern.search(v) for v in values):
            headers = values
        else:
            if label:
                rows[label] = values

    return headers, rows


# ── Section Extractors ───────────────────────────────────────────────────────

def _extract_top_ratios(soup: BeautifulSoup) -> Dict[str, str]:
    """Extract key ratios from the top section."""
    ratios: Dict[str, str] = {}

    # Primary selector: li.flex.flex-space-between
    for li in soup.select("li.flex.flex-space-between"):
        name_el = li.select_one("span.name")
        value_el = li.select_one("span.number, span.value")
        if name_el and value_el:
            name = name_el.get_text(strip=True)
            value = value_el.get_text(strip=True)
            ratios[name] = value

    # Fallback: broader scan
    if not ratios:
        known_keys = {
            "Market Cap", "Current Price", "High / Low", "Stock P/E",
            "Book Value", "Dividend Yield", "ROCE", "ROE", "Face Value",
            "P/E", "PEG Ratio", "Industry PE", "Intrinsic Value",
            "Graham Number", "Piotroski", "Debt to equity",
            "Promoter holding", "Price to Earning",
        }
        for li in soup.find_all("li"):
            spans = li.find_all("span")
            if len(spans) >= 2:
                name = spans[0].get_text(strip=True)
                value = spans[-1].get_text(strip=True)
                if name and value and any(k in name for k in known_keys):
                    ratios[name] = value

    return ratios


def _extract_quarterly(soup: BeautifulSoup) -> Optional[QuarterlyResults]:
    """Extract quarterly results (last 8 quarters)."""
    try:
        section = _find_section(soup, "quarters", r"Quarterly\s+Results")
        if not section:
            return None

        headers, rows = _parse_table_rows(section)
        result = QuarterlyResults(quarters=headers)

        for label, values in rows.items():
            if label.startswith("sales") or label.startswith("revenue"):
                result.sales = values
            elif label.startswith("expenses") or label == "total expenses":
                result.expenses = values
            elif "opm" in label or "operating profit margin" in label:
                result.opm_pct = values
            elif "net profit" in label or "profit after tax" in label:
                result.net_profit = values
            elif label.startswith("eps"):
                result.eps = values

        return result
    except Exception as e:
        logger.warning("Failed to extract quarterly results: %s", e)
        return None


def _extract_annual(soup: BeautifulSoup) -> Optional[AnnualResults]:
    """Extract annual P&L (last 5 years)."""
    try:
        section = _find_section(soup, "profit-loss", r"Profit\s*&?\s*Loss")
        if not section:
            return None

        headers, rows = _parse_table_rows(section)
        result = AnnualResults(years=headers)

        for label, values in rows.items():
            if label.startswith("sales") or label.startswith("revenue"):
                result.sales = values
            elif "opm" in label or "operating profit margin" in label:
                result.opm_pct = values
            elif "net profit" in label or "profit after tax" in label:
                result.net_profit = values
            elif label.startswith("eps"):
                result.eps = values

        return result
    except Exception as e:
        logger.warning("Failed to extract annual results: %s", e)
        return None


def _extract_balance_sheet(soup: BeautifulSoup) -> Optional[BalanceSheet]:
    """Extract balance sheet highlights (last 3 years)."""
    try:
        section = _find_section(soup, "balance-sheet", r"Balance\s+Sheet")
        if not section:
            return None

        headers, rows = _parse_table_rows(section)
        result = BalanceSheet(years=headers)

        for label, values in rows.items():
            if "borrowing" in label:
                result.borrowings = values
            elif "reserves" in label and "revaluation" not in label:
                result.reserves = values
            elif "total assets" in label or "total liabilities" in label:
                result.total_assets = values
            elif "equity capital" in label or "share capital" in label:
                result.equity_capital = values

        return result
    except Exception as e:
        logger.warning("Failed to extract balance sheet: %s", e)
        return None


def _extract_cash_flow(soup: BeautifulSoup) -> Optional[CashFlow]:
    """Extract cash flow statement (last 3 years)."""
    try:
        section = _find_section(soup, "cash-flow", r"Cash\s+Flow")
        if not section:
            return None

        headers, rows = _parse_table_rows(section)
        result = CashFlow(years=headers)

        for label, values in rows.items():
            if "operating" in label or label.startswith("cash from operating"):
                result.operating = values
            elif "investing" in label or label.startswith("cash from investing"):
                result.investing = values
            elif "financing" in label or label.startswith("cash from financing"):
                result.financing = values

        return result
    except Exception as e:
        logger.warning("Failed to extract cash flow: %s", e)
        return None


def _extract_shareholding(soup: BeautifulSoup) -> Optional[Shareholding]:
    """Extract shareholding pattern (last 4 quarters)."""
    try:
        section = _find_section(soup, "shareholding", r"Shareholding\s+Pattern")
        if not section:
            return None

        tables = section.find_all("table")
        if not tables:
            return None

        result = Shareholding()
        table = tables[0]
        month_pattern = re.compile(
            r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)', re.I
        )

        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            texts = [c.get_text(strip=True) for c in cells]
            if not texts:
                continue

            label = texts[0].lower()
            values = texts[1:]

            # Detect header row (quarter names)
            if not result.quarters and any(month_pattern.search(v) for v in values):
                result.quarters = values
                continue

            if "promoter" in label and "pledge" not in label:
                result.promoters = values
            elif "fii" in label or "foreign" in label:
                result.fiis = values
            elif "dii" in label or "domestic" in label:
                result.diis = values
            elif "public" in label:
                result.public = values
            elif "government" in label or "govt" in label:
                result.government = values
            elif "pledge" in label:
                result.pledged = values

        # Try to extract pledged % from the promoter section sub-rows
        if not result.pledged:
            for row in table.find_all("tr"):
                cells = row.find_all(["th", "td"])
                texts = [c.get_text(strip=True) for c in cells]
                if texts and "pledge" in texts[0].lower():
                    result.pledged = texts[1:]

        return result
    except Exception as e:
        logger.warning("Failed to extract shareholding: %s", e)
        return None


def _extract_growth_rates(soup: BeautifulSoup) -> Optional[GrowthRates]:
    """Extract compounded growth rates from Screener.

    Growth tables have a <th> header row (e.g., 'Compounded Sales Growth')
    followed by data rows like '10 Years: 10%'. They live inside
    section#profit-loss on the current Screener layout.
    """
    try:
        growth = GrowthRates()
        found_any = False

        # Strategy: find tables by their <th> header content directly
        # This is resilient to layout changes since it doesn't depend on
        # parent section structure.
        growth_patterns = {
            'sales': re.compile(r'compounded\s+sales\s+growth', re.I),
            'profit': re.compile(r'compounded\s+profit\s+growth', re.I),
            'stock_price': re.compile(r'stock\s+price\s+cagr', re.I),
            'roe': re.compile(r'return\s+on\s+equity', re.I),
        }

        for category, pattern in growth_patterns.items():
            for th in soup.find_all('th', string=pattern):
                table = th.find_parent('table')
                if not table:
                    continue

                row_data = {}
                for row in table.find_all('tr'):
                    cells = row.find_all(['td', 'th'])
                    if len(cells) >= 2:
                        # Keys may have colons: "10 Years:" -> "10 years"
                        key = cells[0].get_text(strip=True).lower().rstrip(':')
                        val = cells[-1].get_text(strip=True)
                        row_data[key] = val

                def _get_period(period_key):
                    """Try common key variations for period lookup."""
                    return (row_data.get(period_key) or
                            row_data.get(period_key.replace(' ', '')) or None)

                if category == 'sales':
                    growth.sales_10y = _get_period('10 years')
                    growth.sales_5y = _get_period('5 years')
                    growth.sales_3y = _get_period('3 years')
                    growth.sales_ttm = _get_period('ttm') or _get_period('1 year')
                elif category == 'profit':
                    growth.profit_10y = _get_period('10 years')
                    growth.profit_5y = _get_period('5 years')
                    growth.profit_3y = _get_period('3 years')
                    growth.profit_ttm = _get_period('ttm') or _get_period('1 year')
                elif category == 'stock_price':
                    growth.stock_price_10y = _get_period('10 years')
                    growth.stock_price_5y = _get_period('5 years')
                    growth.stock_price_3y = _get_period('3 years')
                    growth.stock_price_1y = _get_period('1 year') or _get_period('ttm')
                elif category == 'roe':
                    growth.roe_10y = _get_period('10 years')
                    growth.roe_5y = _get_period('5 years')
                    growth.roe_3y = _get_period('3 years')
                    growth.roe_latest = _get_period('last year') or _get_period('ttm')

                found_any = True
                break  # Found table for this category, move to next

        if found_any:
            return growth

        # Fallback if no tables found via header match
        return _extract_growth_rates_fallback(soup)
    except Exception as e:
        logger.warning("Failed to extract growth rates: %s", e)
        return None


def _extract_growth_rates_fallback(soup: BeautifulSoup) -> Optional[GrowthRates]:
    """Fallback growth rate extraction by scanning all tables for period rows."""
    try:
        growth = GrowthRates()
        # Look for small tables with "10 Years" / "5 Years" pattern
        for table in soup.find_all("table"):
            rows_text = []
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                texts = [c.get_text(strip=True) for c in cells]
                rows_text.append(texts)

            # Identify if this is a growth table (has period rows)
            has_period = any(
                any(p in " ".join(r).lower() for p in ["10 years", "5 years", "3 years"])
                for r in rows_text
            )
            if not has_period or len(rows_text) < 2:
                continue

            # Find the label from context around the table
            context = ""
            prev_el = table.find_previous_sibling()
            if prev_el:
                context = prev_el.get_text(strip=True).lower()

            row_data = {}
            for r in rows_text:
                if len(r) >= 2:
                    row_data[r[0].lower().strip()] = r[-1].strip()

            if "sales" in context or "revenue" in context:
                growth.sales_10y = row_data.get("10 years")
                growth.sales_5y = row_data.get("5 years")
                growth.sales_3y = row_data.get("3 years")
                growth.sales_ttm = row_data.get("ttm") or row_data.get("1 year")
            elif "profit" in context:
                growth.profit_10y = row_data.get("10 years")
                growth.profit_5y = row_data.get("5 years")
                growth.profit_3y = row_data.get("3 years")
                growth.profit_ttm = row_data.get("ttm") or row_data.get("1 year")
            elif "stock" in context or "price" in context:
                growth.stock_price_10y = row_data.get("10 years")
                growth.stock_price_5y = row_data.get("5 years")
                growth.stock_price_3y = row_data.get("3 years")
                growth.stock_price_1y = row_data.get("1 year") or row_data.get("ttm")
            elif "roe" in context or "return on equity" in context:
                growth.roe_10y = row_data.get("10 years")
                growth.roe_5y = row_data.get("5 years")
                growth.roe_3y = row_data.get("3 years")
                growth.roe_latest = row_data.get("last year") or row_data.get("ttm")

        return growth
    except Exception as e:
        logger.warning("Growth rates fallback extraction failed: %s", e)
        return None


def _extract_pros_cons(soup: BeautifulSoup) -> Tuple[List[str], List[str]]:
    """Extract Screener's auto-generated strengths and weaknesses."""
    pros: List[str] = []
    cons: List[str] = []

    try:
        # Pros section
        for section_class in ["pros", "strengths"]:
            pros_section = soup.find("div", class_=re.compile(section_class, re.I))
            if pros_section:
                for li in pros_section.find_all("li"):
                    text = li.get_text(strip=True)
                    if text:
                        pros.append(text)
                break

        # Try by heading if not found
        if not pros:
            for heading in soup.find_all(["h3", "h4", "p"]):
                if "strength" in heading.get_text().lower() or "pros" in heading.get_text().lower():
                    ul = heading.find_next("ul")
                    if ul:
                        for li in ul.find_all("li"):
                            text = li.get_text(strip=True)
                            if text:
                                pros.append(text)
                    break

        # Cons section
        for section_class in ["cons", "weaknesses"]:
            cons_section = soup.find("div", class_=re.compile(section_class, re.I))
            if cons_section:
                for li in cons_section.find_all("li"):
                    text = li.get_text(strip=True)
                    if text:
                        cons.append(text)
                break

        if not cons:
            for heading in soup.find_all(["h3", "h4", "p"]):
                if "weakness" in heading.get_text().lower() or "cons" in heading.get_text().lower():
                    ul = heading.find_next("ul")
                    if ul:
                        for li in ul.find_all("li"):
                            text = li.get_text(strip=True)
                            if text:
                                cons.append(text)
                    break

    except Exception as e:
        logger.warning("Failed to extract pros/cons: %s", e)

    return pros, cons


def _extract_peer_comparison(soup: BeautifulSoup) -> Optional[PeerComparison]:
    """Extract sector name and basic peer data."""
    try:
        section = _find_section(soup, "peers", r"Peer\s+Comparison")
        if not section:
            return None

        result = PeerComparison()

        # Extract sector name from links in the section
        sector_parts = []
        for a in section.find_all("a"):
            text = a.get_text(strip=True)
            # Stop at index-like links
            if any(x in text for x in ["Nifty", "BSE", "Sensex"]):
                break
            if text and len(text) < 60:
                sector_parts.append(text)
        result.sector = " > ".join(sector_parts[:4]) if sector_parts else ""

        # Extract peer table
        table = section.find("table")
        if table:
            peer_headers: List[str] = []
            for row in table.find_all("tr"):
                cells = row.find_all(["th", "td"])
                texts = [c.get_text(strip=True) for c in cells]
                if not texts:
                    continue

                if not peer_headers:
                    # First row is likely headers
                    peer_headers = texts
                    continue

                # Build peer dict from headers + values
                if len(texts) >= 2:
                    peer = {}
                    for i, val in enumerate(texts):
                        if i < len(peer_headers):
                            peer[peer_headers[i]] = val
                        else:
                            peer[f"col_{i}"] = val
                    result.peers.append(peer)

        return result
    except Exception as e:
        logger.warning("Failed to extract peer comparison: %s", e)
        return None


def _extract_company_name(soup: BeautifulSoup) -> str:
    """Extract full company name from h1."""
    h1 = soup.find("h1")
    return h1.get_text(strip=True) if h1 else "Unknown"


# ── Main Assembly ────────────────────────────────────────────────────────────

def _clean_ratio_val(val: str) -> str:
    """Strip currency symbols for clean storage."""
    return val.replace("\u20b9", "").replace("₹", "").strip()


def _build_screener_data(symbol: str, soup: BeautifulSoup,
                         page_type: str) -> ScreenerData:
    """Assemble all extracted sections into a ScreenerData object."""
    ratios = _extract_top_ratios(soup)

    # Helper to get a ratio value by trying multiple known key variants
    def r(primary: str, *alternates: str) -> Optional[str]:
        val = ratios.get(primary)
        if val:
            return _clean_ratio_val(val)
        for alt in alternates:
            val = ratios.get(alt)
            if val:
                return _clean_ratio_val(val)
        return None

    pros, cons = _extract_pros_cons(soup)

    data = ScreenerData(
        symbol=symbol.upper(),
        company_name=_extract_company_name(soup),
        page_type=page_type,
        # Top ratios
        market_cap=r("Market Cap"),
        current_price=r("Current Price"),
        pe=r("Stock P/E", "Price to Earning", "P/E"),
        book_value=r("Book Value"),
        dividend_yield=r("Dividend Yield"),
        roce=r("ROCE"),
        roe=r("ROE"),
        face_value=r("Face Value"),
        industry_pe=r("Industry PE"),
        debt_to_equity=r("Debt to equity"),
        high_low_52w=r("High / Low"),
        peg_ratio=r("PEG Ratio"),
        intrinsic_value=r("Intrinsic Value"),
        graham_number=r("Graham Number"),
        piotroski_score=r("Piotroski"),
        promoter_holding=r("Promoter holding"),
        all_ratios={k: _clean_ratio_val(v) for k, v in ratios.items()},
        # Sections
        quarterly=_extract_quarterly(soup),
        annual=_extract_annual(soup),
        balance_sheet=_extract_balance_sheet(soup),
        cash_flow=_extract_cash_flow(soup),
        shareholding=_extract_shareholding(soup),
        growth_rates=_extract_growth_rates(soup),
        # Pros / Cons
        pros=pros,
        cons=cons,
        # Peers
        peer_comparison=_extract_peer_comparison(soup),
    )

    return data


# ── Public API ───────────────────────────────────────────────────────────────

def fetch_screener_data(symbol: str, use_cache: bool = True,
                        ttl_hours: Optional[float] = None) -> Optional[ScreenerData]:
    """Fetch comprehensive fundamental data from Screener.in.

    Args:
        symbol:     Stock symbol (e.g. 'RELIANCE', 'HDFCBANK')
        use_cache:  If True, check file cache first (default True)
        ttl_hours:  Override cache TTL (default: 24 hours)

    Returns:
        ScreenerData object with all available sections, or None on total failure.
        Individual sections may be None if that particular extraction failed.
    """
    symbol = symbol.upper()
    cache = get_cache()

    # Check cache
    if use_cache:
        cached = cache.get(symbol, CACHE_SOURCE, ttl_hours)
        if cached is not None:
            logger.info("Using cached Screener data for %s", symbol)
            return _dict_to_screener_data(cached)

    # Fetch fresh
    soup, page_type = _fetch_best_page(symbol)
    if soup is None:
        logger.error("Failed to fetch Screener page for %s", symbol)
        return None

    data = _build_screener_data(symbol, soup, page_type)

    # Cache the result
    if use_cache:
        cache.set(symbol, CACHE_SOURCE, data.to_dict())

    logger.info("Fetched Screener data for %s (%s)", symbol, page_type)
    return data


def _dict_to_screener_data(d: dict) -> ScreenerData:
    """Reconstruct ScreenerData from a cached dict."""
    data = ScreenerData(
        symbol=d.get('symbol', ''),
        company_name=d.get('company_name', ''),
        page_type=d.get('page_type', ''),
        market_cap=d.get('market_cap'),
        current_price=d.get('current_price'),
        pe=d.get('pe'),
        book_value=d.get('book_value'),
        dividend_yield=d.get('dividend_yield'),
        roce=d.get('roce'),
        roe=d.get('roe'),
        face_value=d.get('face_value'),
        industry_pe=d.get('industry_pe'),
        debt_to_equity=d.get('debt_to_equity'),
        high_low_52w=d.get('high_low_52w'),
        peg_ratio=d.get('peg_ratio'),
        intrinsic_value=d.get('intrinsic_value'),
        graham_number=d.get('graham_number'),
        piotroski_score=d.get('piotroski_score'),
        promoter_holding=d.get('promoter_holding'),
        all_ratios=d.get('all_ratios', {}),
        pros=d.get('pros', []),
        cons=d.get('cons', []),
    )

    # Reconstruct nested dataclasses
    q = d.get('quarterly')
    if q:
        data.quarterly = QuarterlyResults(**{k: v for k, v in q.items()
                                             if k in QuarterlyResults.__dataclass_fields__})

    a = d.get('annual')
    if a:
        data.annual = AnnualResults(**{k: v for k, v in a.items()
                                       if k in AnnualResults.__dataclass_fields__})

    bs = d.get('balance_sheet')
    if bs:
        data.balance_sheet = BalanceSheet(**{k: v for k, v in bs.items()
                                             if k in BalanceSheet.__dataclass_fields__})

    cf = d.get('cash_flow')
    if cf:
        data.cash_flow = CashFlow(**{k: v for k, v in cf.items()
                                     if k in CashFlow.__dataclass_fields__})

    sh = d.get('shareholding')
    if sh:
        data.shareholding = Shareholding(**{k: v for k, v in sh.items()
                                            if k in Shareholding.__dataclass_fields__})

    gr = d.get('growth_rates')
    if gr:
        data.growth_rates = GrowthRates(**{k: v for k, v in gr.items()
                                           if k in GrowthRates.__dataclass_fields__})

    pc = d.get('peer_comparison')
    if pc:
        data.peer_comparison = PeerComparison(
            sector=pc.get('sector', ''),
            peers=pc.get('peers', []),
        )

    return data
