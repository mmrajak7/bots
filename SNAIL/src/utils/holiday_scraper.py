"""
SNAIL NSE Holiday Scraper

Automated scraping of NSE trading holidays from Zerodha marketintel.

@file        holiday_scraper.py
@description NSE holiday calendar scraping and management
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  tests/standalone/test_holiday_scrape.py
"""

import json
import sys
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from loguru import logger

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    logger.warning("requests or beautifulsoup4 not installed. Holiday scraping disabled.")
    requests = None
    BeautifulSoup = None

from src.utils.config import PROJECT_ROOT


# =============================================================================
# CONSTANTS
# =============================================================================

ZERODHA_HOLIDAY_URL = "https://zerodha.com/marketintel/holiday-calendar/"
HOLIDAY_FILE_PATH = PROJECT_ROOT / "config" / "nse_holidays.json"
REQUEST_TIMEOUT = 10

# Date formats to try when parsing
DATE_FORMATS = [
    "%d %b %Y",        # 26 Feb 2025 (Zerodha format)
    "%d %B %Y",        # 26 February 2025
    "%B %d",           # January 26
    "%d %B",           # 26 January
    "%d-%b-%Y",        # 26-Jan-2025
    "%d-%m-%Y",        # 26-01-2025
    "%Y-%m-%d",        # 2025-01-26
]


# =============================================================================
# SCRAPING FUNCTIONS
# =============================================================================

def fetch_holiday_page() -> Optional[str]:
    """
    Fetch holiday calendar page from Zerodha.

    Returns:
        HTML content or None on failure
    """
    if requests is None:
        logger.error("requests library not available")
        return None

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        response = requests.get(ZERODHA_HOLIDAY_URL, headers=headers, timeout=REQUEST_TIMEOUT)

        if response.status_code != 200:
            logger.error(f"Failed to fetch holidays: HTTP {response.status_code}")
            return None

        if len(response.text) < 1000:
            logger.error(f"Response too short: {len(response.text)} chars")
            return None

        logger.info(f"Fetched holiday page: {len(response.text)} chars")
        return response.text

    except requests.exceptions.Timeout:
        logger.error("Holiday page request timed out")
        return None
    except Exception as e:
        logger.error(f"Failed to fetch holiday page: {e}")
        return None


def parse_holidays(html_content: str) -> List[Dict[str, str]]:
    """
    Parse holidays from HTML content.

    Args:
        html_content: HTML from Zerodha holiday page

    Returns:
        List of holiday dicts with date, name, day
    """
    if BeautifulSoup is None:
        logger.error("BeautifulSoup not available")
        return []

    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        tables = soup.find_all('table')

        if not tables:
            logger.warning("No tables found in holiday page")
            return parse_holidays_alternative(soup)

        holidays = []
        current_year = datetime.now().year

        for table in tables:
            rows = table.find_all('tr')

            for row in rows:
                cells = row.find_all(['td', 'th'])

                if len(cells) >= 2:
                    # Zerodha table structure: Day | Date | Holiday | Exchanges
                    date_text = None
                    description = None

                    # Try different column layouts
                    if len(cells) >= 3:
                        # Layout: Day | Date | Holiday (Zerodha format)
                        date_text = cells[1].get_text(strip=True)
                        description = cells[2].get_text(strip=True)
                    else:
                        # Layout: Date | Holiday
                        date_text = cells[0].get_text(strip=True)
                        description = cells[1].get_text(strip=True)

                    # Skip header rows
                    if not date_text or 'date' in date_text.lower() or 'day' in date_text.lower():
                        continue

                    # Parse date
                    parsed_date = try_parse_date(date_text, current_year)

                    if parsed_date and description:
                        holidays.append({
                            "date": parsed_date.isoformat(),
                            "name": description,
                            "day": parsed_date.strftime("%A")
                        })

        if not holidays:
            holidays = parse_holidays_alternative(soup)

        # Remove duplicates and sort
        holidays = deduplicate_holidays(holidays)

        logger.info(f"Parsed {len(holidays)} holidays")
        return holidays

    except Exception as e:
        logger.error(f"Failed to parse holidays: {e}")
        return []


def try_parse_date(date_text: str, default_year: int) -> Optional[date]:
    """
    Try to parse date string with multiple formats.

    Args:
        date_text: Date string to parse
        default_year: Year to use if not in string

    Returns:
        Parsed date or None
    """
    for fmt in DATE_FORMATS:
        try:
            parsed_date = datetime.strptime(date_text, fmt).date()
            if parsed_date.year == 1900:  # No year in format
                parsed_date = parsed_date.replace(year=default_year)
            return parsed_date
        except ValueError:
            continue
    return None


def parse_holidays_alternative(soup) -> List[Dict[str, str]]:
    """
    Alternative parsing method for different page structures.

    Args:
        soup: BeautifulSoup parsed content

    Returns:
        List of holiday dicts
    """
    holidays = []
    current_year = datetime.now().year
    text_content = soup.get_text()

    # Known holidays with fixed dates
    known_holidays = {
        "Republic Day": (1, 26),
        "Dr Ambedkar Jayanti": (4, 14),
        "Independence Day": (8, 15),
        "Mahatma Gandhi Jayanti": (10, 2),
        "Christmas": (12, 25),
    }

    for holiday_name, (month, day) in known_holidays.items():
        if holiday_name.lower() in text_content.lower():
            holiday_date = date(current_year, month, day)
            holidays.append({
                "date": holiday_date.isoformat(),
                "name": holiday_name,
                "day": holiday_date.strftime("%A")
            })

    return holidays


def deduplicate_holidays(holidays: List[Dict]) -> List[Dict]:
    """Remove duplicate holidays and sort by date."""
    seen_dates = set()
    unique = []

    for h in holidays:
        if h["date"] not in seen_dates:
            seen_dates.add(h["date"])
            unique.append(h)

    unique.sort(key=lambda x: x["date"])
    return unique


# =============================================================================
# CALENDAR MANAGEMENT
# =============================================================================

def scrape_and_save_holidays() -> Tuple[bool, str]:
    """
    Scrape holidays from Zerodha and save to JSON file.

    Returns:
        Tuple of (success, message)
    """
    # Fetch page
    html_content = fetch_holiday_page()
    if not html_content:
        return False, "Failed to fetch holiday page"

    # Parse holidays
    holidays = parse_holidays(html_content)
    if not holidays:
        return False, "No holidays parsed from page"

    # Group by year
    holidays_by_year = {}
    for h in holidays:
        year = h["date"][:4]
        if year not in holidays_by_year:
            holidays_by_year[year] = []
        holidays_by_year[year].append({
            "date": h["date"],
            "name": h["name"]
        })

    # Load existing file to preserve manual entries
    existing_data = {}
    if HOLIDAY_FILE_PATH.exists():
        try:
            with open(HOLIDAY_FILE_PATH, 'r') as f:
                existing_data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not load existing holidays file: {e}")

    # Merge with existing data
    for year, year_holidays in holidays_by_year.items():
        existing_data[year] = year_holidays

    # Update metadata
    existing_data["_comment"] = "NSE Trading Holidays - Auto-updated from Zerodha"
    existing_data["_last_updated"] = datetime.now().strftime("%Y-%m-%d")
    existing_data["_source"] = ZERODHA_HOLIDAY_URL

    # Save
    try:
        HOLIDAY_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(HOLIDAY_FILE_PATH, 'w') as f:
            json.dump(existing_data, f, indent=2)

        total_holidays = sum(len(h) for y, h in existing_data.items() if not y.startswith('_'))
        return True, f"Saved {total_holidays} holidays to {HOLIDAY_FILE_PATH.name}"

    except Exception as e:
        return False, f"Failed to save holidays: {e}"


def get_upcoming_holidays(days_ahead: int = 30) -> List[Dict]:
    """
    Get holidays within the next N days.

    Args:
        days_ahead: Number of days to look ahead

    Returns:
        List of upcoming holidays with days_until field
    """
    today = date.today()
    end_date = today + timedelta(days=days_ahead)

    try:
        if not HOLIDAY_FILE_PATH.exists():
            return []

        with open(HOLIDAY_FILE_PATH, 'r') as f:
            data = json.load(f)

        upcoming = []

        for year, holidays in data.items():
            if year.startswith('_'):
                continue

            for h in holidays:
                try:
                    holiday_date = date.fromisoformat(h["date"])
                    if today <= holiday_date <= end_date:
                        upcoming.append({
                            "date": h["date"],
                            "name": h["name"],
                            "days_until": (holiday_date - today).days
                        })
                except (ValueError, KeyError) as e:
                    logger.debug(f"Invalid holiday entry: {h}, error: {e}")
                    continue

        upcoming.sort(key=lambda x: x["date"])
        return upcoming

    except Exception as e:
        logger.error(f"Failed to get upcoming holidays: {e}")
        return []


def is_friday_before_holiday(check_date: Optional[date] = None) -> Tuple[bool, str]:
    """
    Check if the given date is a Friday before a Monday holiday.

    Important for early position exit decisions.

    Args:
        check_date: Date to check (default today)

    Returns:
        Tuple of (is_friday_before_holiday, holiday_name)
    """
    if check_date is None:
        check_date = date.today()

    # Must be Friday
    if check_date.weekday() != 4:  # Friday = 4
        return False, ""

    # Check if Monday is a holiday
    monday = check_date + timedelta(days=3)

    try:
        if not HOLIDAY_FILE_PATH.exists():
            return False, ""

        with open(HOLIDAY_FILE_PATH, 'r') as f:
            data = json.load(f)

        year = str(monday.year)
        monday_str = monday.isoformat()

        if year in data:
            for h in data[year]:
                if h.get("date") == monday_str:
                    return True, h.get("name", "Holiday")

        return False, ""

    except Exception as e:
        logger.error(f"Failed to check Friday before holiday: {e}")
        return False, ""


def is_long_weekend(check_date: Optional[date] = None) -> Tuple[bool, int]:
    """
    Check if the given Friday starts a long weekend (3+ consecutive off days).

    Args:
        check_date: Date to check (default today)

    Returns:
        Tuple of (is_long_weekend, total_off_days)
    """
    if check_date is None:
        check_date = date.today()

    # Must be Friday
    if check_date.weekday() != 4:
        return False, 0

    try:
        if not HOLIDAY_FILE_PATH.exists():
            return False, 2  # Just regular weekend

        with open(HOLIDAY_FILE_PATH, 'r') as f:
            data = json.load(f)

        # Build set of holiday dates
        holiday_dates = set()
        for year, holidays in data.items():
            if year.startswith('_'):
                continue
            for h in holidays:
                try:
                    holiday_dates.add(date.fromisoformat(h["date"]))
                except (ValueError, KeyError):
                    continue

        # Count consecutive off days starting from Saturday
        off_days = 2  # Saturday and Sunday
        current = check_date + timedelta(days=3)  # Monday

        while current in holiday_dates:
            off_days += 1
            current += timedelta(days=1)

        return off_days >= 3, off_days

    except Exception as e:
        logger.error(f"Failed to check long weekend: {e}")
        return False, 2


def get_next_trading_day_considering_holidays(from_date: Optional[date] = None) -> date:
    """
    Get the next trading day, skipping weekends AND holidays.

    Args:
        from_date: Starting date (default today)

    Returns:
        Next trading day
    """
    if from_date is None:
        from_date = date.today()

    try:
        if not HOLIDAY_FILE_PATH.exists():
            # Fall back to just skipping weekends
            next_day = from_date + timedelta(days=1)
            while next_day.weekday() >= 5:
                next_day += timedelta(days=1)
            return next_day

        with open(HOLIDAY_FILE_PATH, 'r') as f:
            data = json.load(f)

        # Build set of holiday dates
        holiday_dates = set()
        for year, holidays in data.items():
            if year.startswith('_'):
                continue
            for h in holidays:
                try:
                    holiday_dates.add(date.fromisoformat(h["date"]))
                except (ValueError, KeyError):
                    continue

        next_day = from_date + timedelta(days=1)

        while True:
            # Skip weekends
            if next_day.weekday() >= 5:
                next_day += timedelta(days=1)
                continue

            # Skip holidays
            if next_day in holiday_dates:
                next_day += timedelta(days=1)
                continue

            return next_day

    except Exception as e:
        logger.error(f"Failed to get next trading day: {e}")
        # Fall back
        next_day = from_date + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        return next_day


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("SNAIL NSE Holiday Scraper")
    print("=" * 60 + "\n")

    # Scrape and save
    print("Scraping holidays from Zerodha...")
    success, message = scrape_and_save_holidays()

    if success:
        print(f"[OK] {message}")
    else:
        print(f"[FAIL] {message}")

    # Show upcoming holidays
    print("\nUpcoming holidays (next 30 days):")
    upcoming = get_upcoming_holidays(30)

    if upcoming:
        for h in upcoming:
            print(f"  - {h['date']} ({h['days_until']} days): {h['name']}")
    else:
        print("  No upcoming holidays")

    # Check Friday scenarios
    print("\nFriday checks for today:")
    is_fri_before, holiday_name = is_friday_before_holiday()
    is_long, off_days = is_long_weekend()

    print(f"  Friday before holiday: {is_fri_before} ({holiday_name})")
    print(f"  Long weekend: {is_long} ({off_days} days off)")

    # Next trading day
    next_td = get_next_trading_day_considering_holidays()
    print(f"\nNext trading day: {next_td} ({next_td.strftime('%A')})")

    print("\n" + "=" * 60)
