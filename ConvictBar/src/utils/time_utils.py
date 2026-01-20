"""
Time utilities for ConvictBar strategy.
Handles market session, bar numbering, and time-based checks.
"""

from datetime import datetime, time, timedelta
from typing import Tuple


# Market timing constants (IST)
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
HARD_CLOSE = time(15, 20)
ENTRY_WINDOW_START = time(9, 20)
ENTRY_WINDOW_END = time(10, 30)
TIMEFRAME_MINUTES = 5


def get_bar_number(dt: datetime) -> int:
    """
    Calculate bar number from timestamp.

    Bar 1: 09:15:00 - 09:19:59
    Bar 2: 09:20:00 - 09:24:59
    ...

    Args:
        dt: Datetime of the bar

    Returns:
        Bar number (1-indexed)
    """
    market_open_dt = dt.replace(hour=9, minute=15, second=0, microsecond=0)
    minutes_since_open = (dt - market_open_dt).total_seconds() / 60
    return int(minutes_since_open // TIMEFRAME_MINUTES) + 1


def get_bar_time_range(bar_number: int, trade_date: datetime) -> Tuple[datetime, datetime]:
    """
    Get start and end time for a bar number.

    Args:
        bar_number: Bar number (1-indexed)
        trade_date: Date of the trading day

    Returns:
        Tuple of (bar_start, bar_end) datetimes
    """
    bar_start = trade_date.replace(hour=9, minute=15, second=0, microsecond=0)
    bar_start += timedelta(minutes=(bar_number - 1) * TIMEFRAME_MINUTES)
    bar_end = bar_start + timedelta(minutes=TIMEFRAME_MINUTES) - timedelta(seconds=1)
    return bar_start, bar_end


def is_entry_window(dt: datetime) -> bool:
    """
    Check if current time is within entry window.

    Entry window: 09:20 - 10:30

    Args:
        dt: Datetime to check

    Returns:
        True if within entry window
    """
    current_time = dt.time()
    return ENTRY_WINDOW_START <= current_time <= ENTRY_WINDOW_END


def is_market_hours(dt: datetime) -> bool:
    """
    Check if current time is during market hours.

    Args:
        dt: Datetime to check

    Returns:
        True if during market hours
    """
    current_time = dt.time()
    return MARKET_OPEN <= current_time <= MARKET_CLOSE


def is_before_hard_close(dt: datetime) -> bool:
    """
    Check if current time is before hard close.

    Args:
        dt: Datetime to check

    Returns:
        True if before hard close time
    """
    return dt.time() < HARD_CLOSE


def get_last_entry_bar() -> int:
    """
    Get the last bar number that can have new entries.

    Entry window ends at 10:30, which is Bar 15.

    Returns:
        Last bar number for entries (15)
    """
    # 10:30 is 75 minutes from open (09:15)
    # Bar 15: 10:25-10:29
    return 15


def get_previous_close_time(dt: datetime) -> datetime:
    """
    Get the close time of the previous trading day.

    Args:
        dt: Current datetime

    Returns:
        Previous day's market close datetime
    """
    prev_day = dt - timedelta(days=1)
    # Skip weekends
    while prev_day.weekday() >= 5:  # Saturday=5, Sunday=6
        prev_day -= timedelta(days=1)
    return prev_day.replace(hour=15, minute=30, second=0, microsecond=0)


def is_trading_day(dt: datetime) -> bool:
    """
    Check if the date is a potential trading day (not weekend).
    Note: Does not account for holidays.

    Args:
        dt: Date to check

    Returns:
        True if not a weekend
    """
    return dt.weekday() < 5


def bars_between(bar1: int, bar2: int) -> int:
    """
    Count bars between two bar numbers.

    Args:
        bar1: First bar number
        bar2: Second bar number

    Returns:
        Number of bars between (exclusive)
    """
    return abs(bar2 - bar1) - 1


def format_bar_time(bar_number: int) -> str:
    """
    Format bar number as time string.

    Args:
        bar_number: Bar number

    Returns:
        Time string like "09:20-09:25"
    """
    start_minutes = 9 * 60 + 15 + (bar_number - 1) * TIMEFRAME_MINUTES
    end_minutes = start_minutes + TIMEFRAME_MINUTES

    start_h, start_m = divmod(start_minutes, 60)
    end_h, end_m = divmod(end_minutes, 60)

    return f"{start_h:02d}:{start_m:02d}-{end_h:02d}:{end_m:02d}"
