"""
Timezone Helper - IST (Indian Standard Time) utilities

CRITICAL: All datetime operations in this bot MUST use IST timezone
Market hours are in IST, not UTC. Using UTC can cause off-by-one day errors.

Example of the problem:
- Friday 11:30 PM IST → Still Friday in India (market closed)
- Friday 11:30 PM IST → Friday 6:00 PM UTC
- If we use UTC for weekday check, Friday check works
- BUT if Friday 12:30 AM IST → Thursday 7:00 PM UTC
- Now weekday check gives Thursday instead of Friday! ❌

Solution: Always use IST timezone-aware datetimes
"""

from datetime import datetime, date, time, timedelta
from typing import Optional
import pytz


# IST Timezone (UTC+5:30)
IST = pytz.timezone('Asia/Kolkata')


def now_ist() -> datetime:
    """
    Get current datetime in IST timezone (timezone-aware)

    Use this instead of:
    - datetime.now() ❌
    - datetime.utcnow() ❌

    Returns:
        Timezone-aware datetime in IST
    """
    return datetime.now(IST)


def today_ist() -> date:
    """
    Get current date in IST timezone

    Returns:
        Date object for current IST date
    """
    return now_ist().date()


def to_ist(dt: datetime) -> datetime:
    """
    Convert any datetime to IST timezone

    Args:
        dt: Datetime to convert (can be naive or timezone-aware)

    Returns:
        Timezone-aware datetime in IST
    """
    if dt.tzinfo is None:
        # Naive datetime - assume it's already IST
        return IST.localize(dt)
    else:
        # Timezone-aware datetime - convert to IST
        return dt.astimezone(IST)


def naive_to_ist(dt: datetime) -> datetime:
    """
    Convert naive datetime to IST timezone-aware datetime
    Assumes the naive datetime is already in IST

    Args:
        dt: Naive datetime (no timezone info)

    Returns:
        Timezone-aware datetime in IST
    """
    if dt.tzinfo is not None:
        raise ValueError("Datetime is already timezone-aware")

    return IST.localize(dt)


def strip_timezone(dt: datetime) -> datetime:
    """
    Strip timezone info from datetime (for database storage)
    Database stores naive datetimes but they represent IST times

    Args:
        dt: Timezone-aware datetime

    Returns:
        Naive datetime
    """
    return dt.replace(tzinfo=None)


def ist_to_naive(dt: datetime) -> datetime:
    """
    Convert IST datetime to naive datetime for DB storage

    Args:
        dt: IST timezone-aware datetime

    Returns:
        Naive datetime (timezone stripped)
    """
    if dt.tzinfo is None:
        return dt  # Already naive

    # Convert to IST first (in case it's in different timezone)
    dt_ist = to_ist(dt)

    # Strip timezone
    return strip_timezone(dt_ist)


def combine_date_time_ist(date_obj: date, time_obj: time) -> datetime:
    """
    Combine date and time objects into IST timezone-aware datetime

    Args:
        date_obj: Date object
        time_obj: Time object

    Returns:
        Timezone-aware datetime in IST
    """
    naive_dt = datetime.combine(date_obj, time_obj)
    return IST.localize(naive_dt)


def parse_time_ist(time_str: str) -> time:
    """
    Parse time string in HH:MM format

    Args:
        time_str: Time string like "09:15" or "15:30"

    Returns:
        Time object
    """
    return datetime.strptime(time_str, '%H:%M').time()


def get_market_datetime_ist(time_str: str, date_obj: Optional[date] = None) -> datetime:
    """
    Get market datetime for given time string

    Args:
        time_str: Time string like "09:15" or "15:30"
        date_obj: Date (default: today IST)

    Returns:
        Timezone-aware datetime in IST
    """
    if date_obj is None:
        date_obj = today_ist()

    time_obj = parse_time_ist(time_str)
    return combine_date_time_ist(date_obj, time_obj)


def is_market_day_ist(date_obj: Optional[date] = None) -> bool:
    """
    Check if given date is a market day (Monday-Friday)

    Args:
        date_obj: Date to check (default: today IST)

    Returns:
        True if Monday-Friday
    """
    if date_obj is None:
        date_obj = today_ist()

    # Monday = 0, Friday = 4, Saturday = 5, Sunday = 6
    return date_obj.weekday() < 5


def get_weekday_ist(date_obj: Optional[date] = None) -> int:
    """
    Get weekday for given date (Monday=0, Sunday=6)

    Args:
        date_obj: Date to check (default: today IST)

    Returns:
        Weekday number (0-6)
    """
    if date_obj is None:
        date_obj = today_ist()

    return date_obj.weekday()


def format_ist_datetime(dt: datetime, format_str: str = '%Y-%m-%d %H:%M:%S IST') -> str:
    """
    Format datetime as string with IST indicator

    Args:
        dt: Datetime to format
        format_str: Format string (default includes IST suffix)

    Returns:
        Formatted string
    """
    if dt.tzinfo is None:
        # Assume naive datetime is IST
        dt = naive_to_ist(dt)
    else:
        # Convert to IST
        dt = to_ist(dt)

    return dt.strftime(format_str)


# Convenience functions for common datetime operations
def ist_now_naive() -> datetime:
    """Get current IST time as naive datetime (for DB storage)"""
    return strip_timezone(now_ist())


def days_ago_ist(days: int) -> datetime:
    """Get datetime N days ago in IST"""
    return now_ist() - timedelta(days=days)


def days_from_now_ist(days: int) -> datetime:
    """Get datetime N days from now in IST"""
    return now_ist() + timedelta(days=days)


# Migration helpers (for converting existing UTC code)
def utcnow_to_ist() -> datetime:
    """
    DEPRECATED: Use now_ist() instead
    This function exists only for gradual migration
    """
    import warnings
    warnings.warn(
        "utcnow_to_ist() is deprecated. Use now_ist() instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return now_ist()
