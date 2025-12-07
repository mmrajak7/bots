"""
Market Events Scraper

Scrapes market events from Zerodha Calendar and news from Zerodha Pulse.
Stores data in shared BOTS/data folder for use by SNAIL and other projects.

@file        market_events_scraper.py
@description Web scraping for market events and news
@author      SNAIL Development Team
@created     2025-12-07
@version     1.0.0
"""

import json
import re
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from loguru import logger

import requests
from bs4 import BeautifulSoup


# =============================================================================
# CONSTANTS
# =============================================================================

# Shared data folder for all BOTS projects
SHARED_DATA_DIR = Path(__file__).parent.parent.parent.parent / "data"

# Output files
EVENTS_FILE = SHARED_DATA_DIR / "market_events.json"
NEWS_FILE = SHARED_DATA_DIR / "market_news.json"

# Zerodha URLs
ZERODHA_CALENDAR_URL = "https://zerodha.com/markets/calendar/"
ZERODHA_PULSE_URL = "https://pulse.zerodha.com/"

# Request headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Countries to include - only India and US have real impact on NIFTY
INCLUDE_COUNTRIES = {"India", "United States", "US", "USA"}


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class MarketEvent:
    """Market calendar event."""
    date: str           # YYYY-MM-DD format
    event: str          # Event name
    importance: str     # High, Medium, Low
    country: str        # Country name

    def to_compact(self) -> str:
        """Return compact string representation."""
        imp_symbol = {"High": "!", "Medium": "*", "Low": "-"}.get(self.importance, "-")
        return f"{self.date} [{imp_symbol}] {self.event} ({self.country})"


@dataclass
class NewsItem:
    """News headline item."""
    headline: str       # News headline
    source: str         # News source
    scraped_at: str     # When scraped (ISO format)

    def to_compact(self) -> str:
        """Return compact string representation."""
        return f"- {self.headline} ({self.source})"


# =============================================================================
# SCRAPING FUNCTIONS
# =============================================================================

def scrape_zerodha_calendar() -> List[MarketEvent]:
    """
    Scrape market events from Zerodha Calendar.

    The table structure is:
    - Date row: first cell has date like "Mon, 08 Dec 2025"
    - Event rows: empty first cell, Event, Importance, Previous, Actual, Unit

    Returns:
        List of MarketEvent objects
    """
    logger.info(f"Scraping Zerodha Calendar: {ZERODHA_CALENDAR_URL}")

    events = []

    try:
        response = requests.get(ZERODHA_CALENDAR_URL, headers=HEADERS, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        # Find the table
        table = soup.find('table')
        if not table:
            logger.warning("No table found on Zerodha Calendar page")
            return events

        # Parse table rows
        rows = table.find_all('tr')
        current_date = None
        current_year = datetime.now().year

        for row in rows:
            cells = row.find_all(['td', 'th'])
            if not cells:
                continue

            first_cell = cells[0].get_text(strip=True)

            # Check if this is a date row (format: "Mon, 08 Dec 2025" or "Mon, 08 Dec")
            date_match = re.match(r'^[A-Za-z]{3},\s+(\d{1,2})\s+([A-Za-z]{3})(?:\s+(\d{4}))?$', first_cell)

            if date_match:
                # This is a date row
                day = int(date_match.group(1))
                month_str = date_match.group(2)
                year = int(date_match.group(3)) if date_match.group(3) else current_year

                month_map = {
                    'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
                    'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
                }
                month = month_map.get(month_str, 1)
                current_date = f"{year}-{month:02d}-{day:02d}"

            elif current_date and len(cells) >= 3 and not first_cell:
                # This is an event row (first cell is empty)
                event_cell = cells[1] if len(cells) > 1 else None
                importance = cells[2].get_text(strip=True) if len(cells) > 2 else "Medium"

                if not event_cell:
                    continue

                # Get event text - remove button text by getting only direct text
                # The cell has: event name (Country) and sometimes a button
                event_text = ""
                for child in event_cell.children:
                    if isinstance(child, str):
                        event_text += child
                    elif hasattr(child, 'name') and child.name not in ['button', 'a']:
                        event_text += child.get_text()
                event_text = ' '.join(event_text.split()).strip()

                # Extract country from parentheses if present
                country = "India"
                country_match = re.search(r'\(([^)]+)\)\s*$', event_text)
                if country_match:
                    country = country_match.group(1)
                    event_name = event_text[:country_match.start()].strip()
                else:
                    event_name = event_text

                # Remove "Remind me" text if present
                event_name = re.sub(r'Remind\s*me\s*$', '', event_name, flags=re.IGNORECASE).strip()

                # Normalize importance
                importance = importance.capitalize()
                if importance not in ["High", "Medium", "Low"]:
                    importance = "Medium"

                # Filter: only include India and US events
                if country not in INCLUDE_COUNTRIES:
                    continue

                if event_name and len(event_name) > 2:
                    events.append(MarketEvent(
                        date=current_date,
                        event=event_name[:200],
                        importance=importance,
                        country=country
                    ))

        logger.info(f"Scraped {len(events)} events from Zerodha Calendar")

    except requests.RequestException as e:
        logger.error(f"Failed to fetch Zerodha Calendar: {e}")
    except Exception as e:
        logger.error(f"Error parsing Zerodha Calendar: {e}")
        import traceback
        traceback.print_exc()

    return events


def scrape_zerodha_pulse() -> List[NewsItem]:
    """
    Scrape news headlines from Zerodha Pulse.

    Returns:
        List of NewsItem objects
    """
    logger.info(f"Scraping Zerodha Pulse: {ZERODHA_PULSE_URL}")

    news_items = []
    scraped_at = datetime.now().isoformat()

    try:
        response = requests.get(ZERODHA_PULSE_URL, headers=HEADERS, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        # Find news items - Pulse uses article or div elements
        articles = soup.find_all('div', class_=lambda x: x and ('item' in x.lower() or 'news' in x.lower() or 'post' in x.lower()))

        if not articles:
            # Try finding by li elements in a list
            articles = soup.find_all('li', class_=lambda x: x and 'item' in x.lower()) if soup else []

        if not articles:
            # Try any article elements
            articles = soup.find_all('article')

        # Also try finding links with headlines
        if not articles:
            articles = soup.find_all('a', class_=lambda x: x and ('title' in x.lower() or 'headline' in x.lower()))

        seen_headlines = set()

        for article in articles[:30]:  # Limit to 30 items
            headline = ""
            source = "Pulse"

            # Try to find headline
            title_elem = (
                article.find('h2') or
                article.find('h3') or
                article.find('h4') or
                article.find('a', class_=lambda x: x and 'title' in x.lower()) or
                article.find('div', class_=lambda x: x and 'title' in x.lower()) or
                article.find('span', class_=lambda x: x and 'title' in x.lower())
            )

            if title_elem:
                headline = title_elem.get_text(strip=True)
            else:
                # Get first significant text
                headline = article.get_text(strip=True)[:200]

            # Try to find source
            source_elem = article.find('span', class_=lambda x: x and 'source' in x.lower())
            if source_elem:
                source = source_elem.get_text(strip=True)
            else:
                # Check for common news source patterns
                text = article.get_text()
                for src in ['ET', 'Moneycontrol', 'LiveMint', 'Reuters', 'Bloomberg', 'CNBC', 'Business Standard']:
                    if src.lower() in text.lower():
                        source = src
                        break

            # Clean and validate headline
            headline = ' '.join(headline.split())  # Normalize whitespace

            if headline and len(headline) > 10 and headline not in seen_headlines:
                seen_headlines.add(headline)
                news_items.append(NewsItem(
                    headline=headline[:300],  # Limit length
                    source=source,
                    scraped_at=scraped_at
                ))

        # Alternative: parse from script tags (some sites embed data as JSON)
        if not news_items:
            scripts = soup.find_all('script', type='application/json')
            for script in scripts:
                try:
                    data = json.loads(script.string)
                    if isinstance(data, list):
                        for item in data[:20]:
                            if isinstance(item, dict) and 'title' in item:
                                news_items.append(NewsItem(
                                    headline=item['title'][:300],
                                    source=item.get('source', 'Pulse'),
                                    scraped_at=scraped_at
                                ))
                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    logger.debug(f"Failed to parse script JSON: {e}")

        logger.info(f"Scraped {len(news_items)} news items from Zerodha Pulse")

    except requests.RequestException as e:
        logger.error(f"Failed to fetch Zerodha Pulse: {e}")
    except Exception as e:
        logger.error(f"Error parsing Zerodha Pulse: {e}")
        import traceback
        traceback.print_exc()

    return news_items


# =============================================================================
# STORAGE FUNCTIONS
# =============================================================================

def save_events(events: List[MarketEvent]) -> bool:
    """
    Save events to shared data file.

    Args:
        events: List of MarketEvent objects

    Returns:
        True if successful
    """
    try:
        # Ensure directory exists
        SHARED_DATA_DIR.mkdir(parents=True, exist_ok=True)

        data = {
            "scraped_at": datetime.now().isoformat(),
            "source": "zerodha_calendar",
            "events": [asdict(e) for e in events]
        }

        with open(EVENTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        logger.info(f"Saved {len(events)} events to {EVENTS_FILE}")
        return True

    except Exception as e:
        logger.error(f"Failed to save events: {e}")
        return False


def save_news(news_items: List[NewsItem]) -> bool:
    """
    Save news to shared data file.

    Args:
        news_items: List of NewsItem objects

    Returns:
        True if successful
    """
    try:
        # Ensure directory exists
        SHARED_DATA_DIR.mkdir(parents=True, exist_ok=True)

        data = {
            "scraped_at": datetime.now().isoformat(),
            "source": "zerodha_pulse",
            "news": [asdict(n) for n in news_items]
        }

        with open(NEWS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        logger.info(f"Saved {len(news_items)} news items to {NEWS_FILE}")
        return True

    except Exception as e:
        logger.error(f"Failed to save news: {e}")
        return False


def load_events() -> Tuple[List[MarketEvent], str]:
    """
    Load events from saved file.

    Returns:
        (List of MarketEvent, scraped_at timestamp)
    """
    try:
        if EVENTS_FILE.exists():
            with open(EVENTS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)

            events = [MarketEvent(**e) for e in data.get('events', [])]
            scraped_at = data.get('scraped_at', '')
            return events, scraped_at
    except Exception as e:
        logger.error(f"Failed to load events: {e}")

    return [], ""


def load_news() -> Tuple[List[NewsItem], str]:
    """
    Load news from saved file.

    Returns:
        (List of NewsItem, scraped_at timestamp)
    """
    try:
        if NEWS_FILE.exists():
            with open(NEWS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)

            news = [NewsItem(**n) for n in data.get('news', [])]
            scraped_at = data.get('scraped_at', '')
            return news, scraped_at
    except Exception as e:
        logger.error(f"Failed to load news: {e}")

    return [], ""


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_upcoming_events(days: int = 10) -> List[MarketEvent]:
    """
    Get events for the next N days.

    Args:
        days: Number of days to look ahead

    Returns:
        List of MarketEvent objects within date range
    """
    events, _ = load_events()

    today = date.today()
    end_date = today + timedelta(days=days)

    upcoming = []
    for event in events:
        try:
            event_date = datetime.strptime(event.date, "%Y-%m-%d").date()
            if today <= event_date <= end_date:
                upcoming.append(event)
        except ValueError:
            continue

    # Sort by date
    upcoming.sort(key=lambda x: x.date)

    return upcoming


def get_events_compact(days: int = 10) -> str:
    """
    Get compact string of upcoming events for Claude prompt.

    Args:
        days: Number of days to look ahead

    Returns:
        Compact multi-line string of events
    """
    events = get_upcoming_events(days)

    if not events:
        return f"No upcoming events in the next {days} days."

    lines = []
    current_date = ""

    for event in events:
        if event.date != current_date:
            current_date = event.date
            lines.append(f"\n{current_date}:")
        lines.append(f"  {event.to_compact()}")

    return '\n'.join(lines).strip()


def get_news_compact(limit: int = 10) -> str:
    """
    Get compact string of latest news for Claude prompt.

    Args:
        limit: Maximum number of news items

    Returns:
        Compact multi-line string of news
    """
    news, scraped_at = load_news()

    if not news:
        return "No recent news available."

    lines = [f"Latest news (scraped: {scraped_at[:10]}):"]
    for item in news[:limit]:
        lines.append(item.to_compact())

    return '\n'.join(lines)


def get_events_for_telegram() -> str:
    """
    Get compact events summary for Telegram notification.

    Returns:
        Formatted string for Telegram
    """
    events = get_upcoming_events(10)

    if not events:
        return "No upcoming market events"

    # Group by importance
    high = [e for e in events if e.importance == "High"]
    medium = [e for e in events if e.importance == "Medium"]

    lines = []

    if high:
        lines.append("HIGH IMPACT:")
        for e in high[:5]:
            lines.append(f"  {e.date}: {e.event[:50]}")

    if medium:
        lines.append("MEDIUM:")
        for e in medium[:3]:
            lines.append(f"  {e.date}: {e.event[:50]}")

    return '\n'.join(lines) if lines else "No significant events"


def get_news_for_telegram(limit: int = 5) -> str:
    """
    Get compact news summary for Telegram notification.

    Returns:
        Formatted string for Telegram
    """
    news, _ = load_news()

    if not news:
        return "No recent news"

    lines = []
    for item in news[:limit]:
        headline = item.headline[:60] + "..." if len(item.headline) > 60 else item.headline
        lines.append(f"- {headline}")

    return '\n'.join(lines)


# =============================================================================
# MAIN SCRAPING FUNCTION
# =============================================================================

def scrape_and_save_all() -> Tuple[int, int]:
    """
    Scrape both calendar and pulse, save to shared folder.

    Returns:
        (events_count, news_count)
    """
    logger.info("Starting market events scraping...")

    # Scrape calendar
    events = scrape_zerodha_calendar()
    events_saved = save_events(events) if events else False

    # Scrape pulse
    news = scrape_zerodha_pulse()
    news_saved = save_news(news) if news else False

    events_count = len(events) if events_saved else 0
    news_count = len(news) if news_saved else 0

    logger.info(f"Scraping complete: {events_count} events, {news_count} news items")

    return events_count, news_count


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    import sys

    # Setup basic logging
    logger.remove()
    logger.add(sys.stderr, level="DEBUG")

    print("\n" + "=" * 60)
    print("Market Events Scraper Test")
    print("=" * 60)

    # Run scraping
    events_count, news_count = scrape_and_save_all()

    print(f"\n[RESULTS]")
    print(f"  Events scraped: {events_count}")
    print(f"  News scraped: {news_count}")
    print(f"  Events file: {EVENTS_FILE}")
    print(f"  News file: {NEWS_FILE}")

    # Show upcoming events
    print(f"\n[UPCOMING EVENTS (10 days)]:")
    print(get_events_compact(10))

    # Show news
    print(f"\n[LATEST NEWS]:")
    print(get_news_compact(5))

    # Show Telegram format
    print(f"\n[TELEGRAM FORMAT]:")
    print("Events:", get_events_for_telegram())
    print("\nNews:", get_news_for_telegram())

    print("\n" + "=" * 60)
