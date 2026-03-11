"""
Trendlyne — SWOT, analyst consensus, and momentum data (best-effort).

Scrapes trendlyne.com/equity/{SYMBOL}/ to extract:
  - SWOT counts (strengths, weaknesses, opportunities, threats)
  - Analyst target price + analyst count
  - Momentum score / Trendlyne score

This is BEST-EFFORT. Trendlyne may return 403 or require JS rendering.
If scraping fails, returns None gracefully — never crashes.
"""

import logging
import re
from dataclasses import dataclass, asdict
from typing import Optional

import requests
from bs4 import BeautifulSoup

from .cache import get_cache

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

TRENDLYNE_URL = "https://trendlyne.com/equity/{symbol}/"
TRENDLYNE_SEARCH_URL = "https://trendlyne.com/equity/search/{symbol}/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://trendlyne.com/",
}

CACHE_SOURCE = 'trendlyne'


# ── Data Class ───────────────────────────────────────────────────────────────

@dataclass
class TrendlyneData:
    """Data extracted from Trendlyne."""
    symbol: str = ""

    # SWOT counts
    swot_strengths: Optional[int] = None
    swot_weaknesses: Optional[int] = None
    swot_opportunities: Optional[int] = None
    swot_threats: Optional[int] = None

    # Analyst consensus
    analyst_target_price: Optional[float] = None
    analyst_count: Optional[int] = None
    analyst_rating: Optional[str] = None   # 'Buy', 'Hold', 'Sell', etc.

    # Scores
    momentum_score: Optional[float] = None
    trendlyne_score: Optional[float] = None

    # Valuation
    fair_value: Optional[float] = None
    upside_pct: Optional[float] = None

    def to_dict(self) -> dict:
        """Convert to plain dict (JSON-serializable)."""
        return asdict(self)


# ── HTTP Fetching ────────────────────────────────────────────────────────────

def _fetch_trendlyne_page(symbol: str) -> Optional[BeautifulSoup]:
    """Fetch Trendlyne equity page, following redirects.

    Trendlyne URLs have numeric IDs, so /equity/SYMBOL/ may redirect
    to /equity/12345/SYMBOL-LTD/. We follow redirects.
    """
    url = TRENDLYNE_URL.format(symbol=symbol.upper())
    logger.info("Fetching %s", url)

    try:
        # Use session to follow redirects and maintain cookies
        session = requests.Session()
        session.headers.update(HEADERS)

        resp = session.get(url, timeout=20, allow_redirects=True)

        if resp.status_code == 200:
            return BeautifulSoup(resp.text, "html.parser")

        # If direct URL fails, try search-based approach
        logger.info("Direct URL returned %d, trying search URL", resp.status_code)
        search_url = TRENDLYNE_SEARCH_URL.format(symbol=symbol.upper())
        resp = session.get(search_url, timeout=20, allow_redirects=True)

        if resp.status_code == 200:
            return BeautifulSoup(resp.text, "html.parser")

        logger.warning("Trendlyne returned %d for %s", resp.status_code, symbol)
        return None

    except requests.RequestException as e:
        logger.warning("Trendlyne request failed for %s: %s", symbol, e)
        return None


# ── Extraction ───────────────────────────────────────────────────────────────

def _parse_number(text: str) -> Optional[float]:
    """Parse number from Trendlyne text."""
    if not text:
        return None
    cleaned = text.replace(",", "").replace("₹", "").replace(
        "Rs", "").replace("%", "").strip()
    if not cleaned or cleaned in ("--", "N/A", "NA"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_swot(soup: BeautifulSoup) -> dict:
    """Extract SWOT counts from the page.

    Trendlyne shows SWOT as colored boxes with counts like "5 Strengths".
    """
    swot = {
        'strengths': None,
        'weaknesses': None,
        'opportunities': None,
        'threats': None,
    }

    try:
        # Look for SWOT-related elements
        for text_match in ['strength', 'weakness', 'opportunit', 'threat']:
            # Try various element selectors
            for el in soup.find_all(string=re.compile(text_match, re.I)):
                parent = el.parent
                if not parent:
                    continue

                # Get the count — usually in the same element or an adjacent one
                text = parent.get_text(strip=True)
                numbers = re.findall(r'(\d+)', text)

                if numbers:
                    count = int(numbers[0])
                    if 'strength' in text_match:
                        swot['strengths'] = count
                    elif 'weakness' in text_match:
                        swot['weaknesses'] = count
                    elif 'opportunit' in text_match:
                        swot['opportunities'] = count
                    elif 'threat' in text_match:
                        swot['threats'] = count
                    break  # Found for this category

        # Alternative: look for SWOT section by class/id
        swot_section = soup.find(id=re.compile(r'swot', re.I))
        if swot_section is None:
            swot_section = soup.find(class_=re.compile(r'swot', re.I))

        if swot_section:
            for item in swot_section.find_all(['div', 'span', 'li']):
                text = item.get_text(strip=True).lower()
                numbers = re.findall(r'(\d+)', text)
                if numbers:
                    count = int(numbers[0])
                    if 'strength' in text and swot['strengths'] is None:
                        swot['strengths'] = count
                    elif 'weakness' in text and swot['weaknesses'] is None:
                        swot['weaknesses'] = count
                    elif 'opportunit' in text and swot['opportunities'] is None:
                        swot['opportunities'] = count
                    elif 'threat' in text and swot['threats'] is None:
                        swot['threats'] = count

    except Exception as e:
        logger.warning("SWOT extraction failed: %s", e)

    return swot


def _extract_analyst_data(soup: BeautifulSoup) -> dict:
    """Extract analyst target price, count, and consensus rating."""
    result = {
        'target_price': None,
        'analyst_count': None,
        'rating': None,
    }

    try:
        # Look for analyst-related text
        for el in soup.find_all(string=re.compile(r'analyst.*target|target.*price|consensus', re.I)):
            parent = el.parent
            if not parent:
                continue

            # Walk up to find the containing block
            block = parent.parent or parent
            block_text = block.get_text(strip=True)

            # Extract target price (look for Rs/₹ followed by number)
            price_match = re.search(r'[₹Rs.]*\s*([\d,]+\.?\d*)', block_text)
            if price_match and result['target_price'] is None:
                result['target_price'] = _parse_number(price_match.group(1))

            # Extract analyst count
            count_match = re.search(r'(\d+)\s*analyst', block_text, re.I)
            if count_match and result['analyst_count'] is None:
                result['analyst_count'] = int(count_match.group(1))

        # Look for consensus rating (Buy/Hold/Sell)
        for pattern in [r'\b(Strong\s*Buy|Buy|Hold|Sell|Strong\s*Sell)\b']:
            for el in soup.find_all(string=re.compile(pattern, re.I)):
                text = el.strip()
                if text.lower() in ('buy', 'strong buy', 'hold', 'sell', 'strong sell'):
                    result['rating'] = text
                    break
            if result['rating']:
                break

    except Exception as e:
        logger.warning("Analyst data extraction failed: %s", e)

    return result


def _extract_scores(soup: BeautifulSoup) -> dict:
    """Extract momentum score / Trendlyne score."""
    scores = {
        'momentum_score': None,
        'trendlyne_score': None,
        'fair_value': None,
    }

    try:
        # Momentum score
        for el in soup.find_all(string=re.compile(r'momentum\s*(score)?', re.I)):
            parent = el.parent
            if parent:
                text = parent.get_text(strip=True)
                numbers = re.findall(r'([\d.]+)', text)
                if numbers:
                    scores['momentum_score'] = float(numbers[0])
                    break

        # Trendlyne score / Durability score
        for keyword in ['trendlyne.*score', 'durability', 'valuation.*score']:
            for el in soup.find_all(string=re.compile(keyword, re.I)):
                parent = el.parent
                if parent:
                    text = parent.get_text(strip=True)
                    numbers = re.findall(r'([\d.]+)', text)
                    if numbers:
                        scores['trendlyne_score'] = float(numbers[0])
                        break
            if scores['trendlyne_score'] is not None:
                break

        # Fair value / Intrinsic value
        for el in soup.find_all(string=re.compile(r'fair\s*value|intrinsic', re.I)):
            parent = el.parent
            if parent:
                block = parent.parent or parent
                text = block.get_text(strip=True)
                price_match = re.search(r'[₹Rs.]*\s*([\d,]+\.?\d*)', text)
                if price_match:
                    scores['fair_value'] = _parse_number(price_match.group(1))
                    break

    except Exception as e:
        logger.warning("Score extraction failed: %s", e)

    return scores


def _build_trendlyne_data(symbol: str,
                          soup: BeautifulSoup) -> TrendlyneData:
    """Assemble all extracted sections into TrendlyneData."""
    swot = _extract_swot(soup)
    analyst = _extract_analyst_data(soup)
    scores = _extract_scores(soup)

    data = TrendlyneData(
        symbol=symbol.upper(),
        swot_strengths=swot['strengths'],
        swot_weaknesses=swot['weaknesses'],
        swot_opportunities=swot['opportunities'],
        swot_threats=swot['threats'],
        analyst_target_price=analyst['target_price'],
        analyst_count=analyst['analyst_count'],
        analyst_rating=analyst['rating'],
        momentum_score=scores['momentum_score'],
        trendlyne_score=scores['trendlyne_score'],
        fair_value=scores['fair_value'],
    )

    # Compute upside if we have both target and need current price
    # (We don't have LTP here; caller can compute this externally)
    # Leaving upside_pct as None for now

    return data


# ── Public API ───────────────────────────────────────────────────────────────

def fetch_trendlyne_data(symbol: str, use_cache: bool = True,
                         ttl_hours: Optional[float] = None) -> Optional[TrendlyneData]:
    """Fetch SWOT + analyst data from Trendlyne (best-effort).

    Args:
        symbol:     Stock symbol (e.g. 'RELIANCE')
        use_cache:  If True, check file cache first (default True)
        ttl_hours:  Override cache TTL (default: 4 hours)

    Returns:
        TrendlyneData object, or None if scraping fails entirely.
        Individual fields may be None if that extraction failed.
    """
    symbol = symbol.upper()
    cache = get_cache()

    # Check cache
    if use_cache:
        cached = cache.get(symbol, CACHE_SOURCE, ttl_hours)
        if cached is not None:
            logger.info("Using cached Trendlyne data for %s", symbol)
            return _dict_to_trendlyne_data(cached)

    # Fetch fresh
    soup = _fetch_trendlyne_page(symbol)
    if soup is None:
        logger.warning("Could not fetch Trendlyne page for %s", symbol)
        return None

    data = _build_trendlyne_data(symbol, soup)

    # Cache even partially-filled data (better than re-fetching)
    if use_cache:
        cache.set(symbol, CACHE_SOURCE, data.to_dict())

    # Log what we got
    found = sum(1 for v in [
        data.swot_strengths, data.analyst_target_price,
        data.momentum_score, data.trendlyne_score
    ] if v is not None)
    logger.info("Trendlyne data for %s: %d/4 key fields populated", symbol, found)

    return data


def _dict_to_trendlyne_data(d: dict) -> TrendlyneData:
    """Reconstruct TrendlyneData from a cached dict."""
    return TrendlyneData(
        symbol=d.get('symbol', ''),
        swot_strengths=d.get('swot_strengths'),
        swot_weaknesses=d.get('swot_weaknesses'),
        swot_opportunities=d.get('swot_opportunities'),
        swot_threats=d.get('swot_threats'),
        analyst_target_price=d.get('analyst_target_price'),
        analyst_count=d.get('analyst_count'),
        analyst_rating=d.get('analyst_rating'),
        momentum_score=d.get('momentum_score'),
        trendlyne_score=d.get('trendlyne_score'),
        fair_value=d.get('fair_value'),
        upside_pct=d.get('upside_pct'),
    )
