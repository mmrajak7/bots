"""
Timezone Helper - IST (Indian Standard Time) utilities for FIFTY Bot

All datetime operations use IST timezone.
Market hours are in IST, not UTC.

FIX TR-C5: Added NSE holiday calendar integration.
"""

import json
from datetime import datetime, date, time, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Tuple

import pytz
from loguru import logger


# IST Timezone (UTC+5:30)
IST = pytz.timezone('Asia/Kolkata')

# Project root for finding shared data
PROJECT_ROOT = Path(__file__).parent.parent.parent  # FIFTY/

# Cached holidays for performance
_nse_holidays_cache: Optional[Dict[str, List[str]]] = None


def load_nse_holidays() -> Dict[str, List[str]]:
    """
    Load NSE holiday calendar from shared data folder.

    FIX TR-C5: NSE Holiday Calendar Integration

    Returns:
        Dict mapping year to list of holiday dates (YYYY-MM-DD format)
    """
    # Use shared data folder: ../data/holiday_calendar.json (relative to FIFTY)
    holidays_path = PROJECT_ROOT.parent / "data" / "holiday_calendar.json"

    try:
        if holidays_path.exists():
            with open(holidays_path, 'r') as f:
                data = json.load(f)

            # Extract just the dates by year
            holidays_by_year = {}
            for year, holidays in data.items():
                if year.startswith('_'):  # Skip metadata fields
                    continue
                # Handle both list of dicts and list of strings
                if holidays and isinstance(holidays[0], dict):
                    holidays_by_year[year] = [h['date'] for h in holidays]
                else:
                    holidays_by_year[year] = holidays

            return holidays_by_year
        else:
            logger.warning(f"NSE holidays file not found: {holidays_path}")
            return {}
    except Exception as e:
        logger.error(f"Failed to load NSE holidays: {e}")
        return {}


def is_nse_holiday(check_date: Optional[date] = None) -> Tuple[bool, str]:
    """
    Check if a date is an NSE holiday.

    FIX TR-C5: NSE Holiday Calendar Integration

    Args:
        check_date: Date to check (default today)

    Returns:
        Tuple of (is_holiday, holiday_name or empty string)
    """
    global _nse_holidays_cache

    if check_date is None:
        check_date = today_ist()

    # Load holidays if not cached
    if _nse_holidays_cache is None:
        _nse_holidays_cache = load_nse_holidays()

    year = str(check_date.year)
    date_str = check_date.strftime('%Y-%m-%d')

    if year in _nse_holidays_cache:
        if date_str in _nse_holidays_cache[year]:
            # Get holiday name from shared data folder
            holidays_path = PROJECT_ROOT.parent / "data" / "holiday_calendar.json"
            try:
                with open(holidays_path, 'r') as f:
                    data = json.load(f)
                for holiday in data.get(year, []):
                    if isinstance(holiday, dict) and holiday.get('date') == date_str:
                        return True, holiday.get('name', 'NSE Holiday')
            except (json.JSONDecodeError, IOError, KeyError):
                pass
            return True, 'NSE Holiday'

    return False, ''


def now_ist() -> datetime:
    """Get current datetime in IST timezone (timezone-aware)"""
    return datetime.now(IST)


def today_ist() -> date:
    """Get current date in IST timezone"""
    return now_ist().date()


def to_ist(dt: datetime) -> datetime:
    """Convert any datetime to IST timezone"""
    if dt.tzinfo is None:
        return IST.localize(dt)
    else:
        return dt.astimezone(IST)


def naive_to_ist(dt: datetime) -> datetime:
    """Convert naive datetime to IST timezone-aware datetime"""
    if dt.tzinfo is not None:
        raise ValueError("Datetime is already timezone-aware")
    return IST.localize(dt)


def strip_timezone(dt: datetime) -> datetime:
    """Strip timezone info from datetime (for database storage)"""
    return dt.replace(tzinfo=None)


def ist_to_naive(dt: datetime) -> datetime:
    """Convert IST datetime to naive datetime for DB storage"""
    if dt.tzinfo is None:
        return dt
    dt_ist = to_ist(dt)
    return strip_timezone(dt_ist)


def ist_now_naive() -> datetime:
    """Get current IST time as naive datetime (for DB storage)"""
    return strip_timezone(now_ist())


def combine_date_time_ist(date_obj: date, time_obj: time) -> datetime:
    """Combine date and time objects into IST timezone-aware datetime"""
    naive_dt = datetime.combine(date_obj, time_obj)
    return IST.localize(naive_dt)


def parse_time_ist(time_str: str) -> time:
    """Parse time string in HH:MM format"""
    return datetime.strptime(time_str, '%H:%M').time()


def get_market_datetime_ist(time_str: str, date_obj: Optional[date] = None) -> datetime:
    """Get market datetime for given time string"""
    if date_obj is None:
        date_obj = today_ist()
    time_obj = parse_time_ist(time_str)
    return combine_date_time_ist(date_obj, time_obj)


def is_market_day_ist(date_obj: Optional[date] = None) -> bool:
    """
    Check if given date is a market day (Monday-Friday, not NSE holiday).

    FIX TR-C5: Now checks NSE holiday calendar in addition to weekends.
    """
    if date_obj is None:
        date_obj = today_ist()

    # Check weekend first (faster)
    if date_obj.weekday() >= 5:
        return False

    # Check NSE holiday calendar
    is_holiday, holiday_name = is_nse_holiday(date_obj)
    if is_holiday:
        logger.debug(f"{date_obj} is NSE holiday: {holiday_name}")
        return False

    return True


def get_weekday_ist(date_obj: Optional[date] = None) -> int:
    """Get weekday for given date (Monday=0, Sunday=6)"""
    if date_obj is None:
        date_obj = today_ist()
    return date_obj.weekday()


def is_friday(date_obj: Optional[date] = None) -> bool:
    """Check if given date is Friday"""
    if date_obj is None:
        date_obj = today_ist()
    return date_obj.weekday() == 4


def format_ist_datetime(dt: datetime, format_str: str = '%Y-%m-%d %H:%M:%S IST') -> str:
    """Format datetime as string with IST indicator"""
    if dt.tzinfo is None:
        dt = naive_to_ist(dt)
    else:
        dt = to_ist(dt)
    return dt.strftime(format_str)


def days_ago_ist(days: int) -> datetime:
    """Get datetime N days ago in IST"""
    return now_ist() - timedelta(days=days)


def days_from_now_ist(days: int) -> datetime:
    """Get datetime N days from now in IST"""
    return now_ist() + timedelta(days=days)


def get_month_start(date_obj: Optional[date] = None) -> date:
    """Get first day of the month"""
    if date_obj is None:
        date_obj = today_ist()
    return date_obj.replace(day=1)


def get_month_end(date_obj: Optional[date] = None) -> date:
    """Get last day of the month"""
    if date_obj is None:
        date_obj = today_ist()
    # Go to next month, then back one day
    if date_obj.month == 12:
        next_month = date_obj.replace(year=date_obj.year + 1, month=1, day=1)
    else:
        next_month = date_obj.replace(month=date_obj.month + 1, day=1)
    return next_month - timedelta(days=1)


def in_time_window(current_time: datetime, start_str: str, end_str: str) -> bool:
    """
    Check if current time is within a time window (inclusive start, exclusive end)

    Args:
        current_time: Current datetime
        start_str: Start time string "HH:MM"
        end_str: End time string "HH:MM"

    Returns:
        True if current_time is in [start, end)
    """
    start_time = parse_time_ist(start_str)
    end_time = parse_time_ist(end_str)
    current = current_time.time()
    return start_time <= current < end_time


def is_market_hours(current_time: Optional[datetime] = None) -> bool:
    """Check if current time is during market hours (9:15 AM - 3:30 PM)"""
    if current_time is None:
        current_time = now_ist()

    if not is_market_day_ist(current_time.date()):
        return False

    return in_time_window(current_time, "09:15", "15:30")
