"""
News Fetcher - Stock news from MoneyControl and Google News

Provides recent news headlines for stock signals:
- Primary: MoneyControl (India-specific, quality coverage)
- Fallback: Google News RSS (broader coverage)

Uses caching to avoid repeat fetches within 24 hours.
"""

import re
import time
from typing import List, Optional, Dict, Tuple
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
from loguru import logger

try:
    import feedparser
    FEEDPARSER_AVAILABLE = True
except ImportError:
    FEEDPARSER_AVAILABLE = False
    logger.warning("feedparser not installed - Google News fallback unavailable")


# Cache: {symbol: (headlines, timestamp)}
_news_cache: Dict[str, Tuple[List[Dict[str, str]], float]] = {}
CACHE_TTL = 24 * 60 * 60  # 24 hours

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _fetch_moneycontrol_news(symbol: str) -> List[Dict[str, str]]:
    """
    Fetch news from MoneyControl for a given stock symbol.

    Args:
        symbol: Stock symbol (e.g., 'RELIANCE', 'TCS')

    Returns:
        List of dicts with 'title', 'link', 'source', 'date' keys
    """
    headlines = []

    try:
        # MoneyControl search URL
        search_url = f"https://www.moneycontrol.com/stocks/company_info/stock_news.php?sc_id={symbol}&durationType=Y"

        # Try direct stock page first
        resp = requests.get(search_url, headers=HEADERS, timeout=10)

        if resp.status_code != 200:
            # Try search approach
            search_url = f"https://www.moneycontrol.com/news/tags/{symbol.lower()}.html"
            resp = requests.get(search_url, headers=HEADERS, timeout=10)

        if resp.status_code != 200:
            logger.debug(f"MoneyControl returned {resp.status_code} for {symbol}")
            return []

        soup = BeautifulSoup(resp.text, 'html.parser')

        # Look for news items in various MoneyControl formats
        news_items = []

        # Format 1: News list items
        for item in soup.select('li.clearfix, div.news_item, div.article_box'):
            title_el = item.select_one('a, h2 a, h3 a')
            if title_el:
                title = title_el.get_text(strip=True)
                link = title_el.get('href', '')
                if title and len(title) > 20:  # Filter out short/navigation links
                    news_items.append({
                        'title': title,
                        'link': link if link.startswith('http') else f"https://www.moneycontrol.com{link}",
                        'source': 'MoneyControl',
                        'date': ''
                    })

        # Format 2: Search results
        for item in soup.select('div.searchNews_wrp li, div.search_news li'):
            title_el = item.select_one('a')
            if title_el:
                title = title_el.get_text(strip=True)
                link = title_el.get('href', '')
                if title and len(title) > 20:
                    news_items.append({
                        'title': title,
                        'link': link if link.startswith('http') else f"https://www.moneycontrol.com{link}",
                        'source': 'MoneyControl',
                        'date': ''
                    })

        # Deduplicate by title
        seen_titles = set()
        for item in news_items:
            title_key = item['title'][:50].lower()
            if title_key not in seen_titles:
                seen_titles.add(title_key)
                headlines.append(item)

        logger.debug(f"MoneyControl: Found {len(headlines)} news items for {symbol}")

    except Exception as e:
        logger.warning(f"MoneyControl fetch failed for {symbol}: {e}")

    return headlines[:10]  # Limit to 10


def _fetch_google_news(symbol: str) -> List[Dict[str, str]]:
    """
    Fetch news from Google News RSS for a given stock symbol.

    Args:
        symbol: Stock symbol (e.g., 'RELIANCE', 'TCS')

    Returns:
        List of dicts with 'title', 'link', 'source', 'date' keys
    """
    if not FEEDPARSER_AVAILABLE:
        return []

    headlines = []

    try:
        # Google News RSS - search for symbol + NSE for India context
        search_query = quote_plus(f"{symbol} NSE stock")
        rss_url = f"https://news.google.com/rss/search?q={search_query}&hl=en-IN&gl=IN&ceid=IN:en"

        feed = feedparser.parse(rss_url)

        if not feed.entries:
            logger.debug(f"Google News: No entries for {symbol}")
            return []

        for entry in feed.entries[:10]:
            title = entry.get('title', '')
            link = entry.get('link', '')
            published = entry.get('published', '')

            # Google News title format: "Headline - Source"
            # Split to get source
            source = 'Google News'
            if ' - ' in title:
                parts = title.rsplit(' - ', 1)
                if len(parts) == 2:
                    title = parts[0]
                    source = parts[1]

            if title:
                headlines.append({
                    'title': title,
                    'link': link,
                    'source': source,
                    'date': published
                })

        logger.debug(f"Google News: Found {len(headlines)} items for {symbol}")

    except Exception as e:
        logger.warning(f"Google News fetch failed for {symbol}: {e}")

    return headlines


def get_stock_news(symbol: str, max_headlines: int = 7) -> List[Dict[str, str]]:
    """
    Get recent news headlines for a stock.

    Primary: MoneyControl
    Fallback: Google News RSS

    Args:
        symbol: Stock symbol (e.g., 'RELIANCE', 'TCS')
        max_headlines: Maximum number of headlines to return

    Returns:
        List of dicts with 'title', 'link', 'source' keys
    """
    symbol = symbol.upper()

    # Check cache
    if symbol in _news_cache:
        headlines, ts = _news_cache[symbol]
        if time.time() - ts < CACHE_TTL:
            logger.debug(f"News cache hit for {symbol}")
            return headlines[:max_headlines]

    headlines = []

    # Try MoneyControl first
    mc_news = _fetch_moneycontrol_news(symbol)
    headlines.extend(mc_news)

    # If MoneyControl didn't return enough, supplement with Google News
    if len(headlines) < max_headlines:
        gn_news = _fetch_google_news(symbol)

        # Add unique Google News items
        existing_titles = {h['title'][:40].lower() for h in headlines}
        for item in gn_news:
            if item['title'][:40].lower() not in existing_titles:
                headlines.append(item)
                existing_titles.add(item['title'][:40].lower())

    # Cache results
    _news_cache[symbol] = (headlines, time.time())

    return headlines[:max_headlines]


def format_news_compact(headlines: List[Dict[str, str]], max_items: int = 2) -> Optional[str]:
    """
    Format news headlines for compact snapshot (2 headlines).

    Args:
        headlines: List of headline dicts
        max_items: Number of headlines to include

    Returns:
        Formatted string or None if no headlines
    """
    if not headlines:
        return None

    lines = ["━━━━━ Recent News ━━━━━"]

    for item in headlines[:max_items]:
        title = item['title']
        # Truncate long titles
        if len(title) > 80:
            title = title[:77] + "..."
        lines.append(f"📰 {title}")

    return "\n".join(lines)


def format_news_full(headlines: List[Dict[str, str]], max_items: int = 7) -> Optional[str]:
    """
    Format news headlines for full report (with links).

    Args:
        headlines: List of headline dicts
        max_items: Number of headlines to include

    Returns:
        Formatted HTML string or None if no headlines
    """
    if not headlines:
        return None

    lines = ["\n<b>Recent News</b>"]

    for i, item in enumerate(headlines[:max_items], 1):
        title = item['title']
        link = item.get('link', '')
        source = item.get('source', '')

        # Truncate long titles
        if len(title) > 100:
            title = title[:97] + "..."

        if link:
            lines.append(f"{i}. <a href=\"{link}\">{title}</a>")
        else:
            lines.append(f"{i}. {title}")

        if source and source not in ['Google News', 'MoneyControl']:
            lines.append(f"   <i>— {source}</i>")

    return "\n".join(lines)


# Test function
if __name__ == "__main__":
    import sys

    symbol = sys.argv[1] if len(sys.argv) > 1 else "RELIANCE"

    print(f"\nFetching news for {symbol}...")
    headlines = get_stock_news(symbol)

    print(f"\nFound {len(headlines)} headlines:\n")

    compact = format_news_compact(headlines)
    if compact:
        print("=== COMPACT FORMAT ===")
        print(compact)

    print("\n")

    full = format_news_full(headlines)
    if full:
        print("=== FULL FORMAT ===")
        print(full)
