"""
SNAIL Helper Utilities

General utility functions for the SNAIL trading system.

@file        helpers.py
@description Common utility functions
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 10
"""

import sys
import json
import hashlib
from datetime import datetime, date, timedelta, time
from typing import Optional, Any, Dict, List, Callable, TypeVar, Tuple
from pathlib import Path
from functools import wraps
import threading
from loguru import logger

from src.utils.config import PROJECT_ROOT


# =============================================================================
# TYPE VARIABLES
# =============================================================================

T = TypeVar('T')


# =============================================================================
# DATE/TIME UTILITIES
# =============================================================================

def get_ist_now() -> datetime:
    """
    Get current time in IST.

    Returns:
        Current datetime in IST
    """
    # IST is UTC+5:30
    from datetime import timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).replace(tzinfo=None)


def load_nse_holidays() -> Dict[str, List[str]]:
    """
    Load NSE holiday calendar from shared data folder.

    TDD Section 5.2: NSE Holiday Calendar Integration

    Returns:
        Dict mapping year to list of holiday dates (YYYY-MM-DD format)
    """
    # Use shared data folder: ../data/holiday_calendar.json (relative to SNAIL)
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


# Cached holidays for performance
_nse_holidays_cache: Optional[Dict[str, List[str]]] = None


def is_nse_holiday(check_date: Optional[date] = None) -> Tuple[bool, str]:
    """
    Check if a date is an NSE holiday.

    Args:
        check_date: Date to check (default today)

    Returns:
        Tuple of (is_holiday, holiday_name or empty string)
    """
    global _nse_holidays_cache

    if check_date is None:
        check_date = date.today()

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
            except (json.JSONDecodeError, IOError, KeyError) as e:
                logger.debug(f"Could not get holiday name: {e}")
            return True, 'NSE Holiday'

    return False, ''


def is_trading_day(check_date: Optional[date] = None) -> bool:
    """
    Check if a date is a trading day.

    TDD Section 5.2: Entry Checklist - Not Holiday

    Checks:
    1. Not a weekend (Saturday/Sunday)
    2. Not an NSE holiday

    Args:
        check_date: Date to check (default today)

    Returns:
        True if trading day (weekday AND not NSE holiday)
    """
    if check_date is None:
        check_date = date.today()

    # Check weekend first (Monday = 0, Sunday = 6)
    if check_date.weekday() >= 5:
        return False

    # Check NSE holiday calendar
    is_holiday, holiday_name = is_nse_holiday(check_date)
    if is_holiday:
        logger.info(f"NSE holiday detected: {holiday_name} ({check_date})")
        return False

    return True


def is_friday_before_holiday(check_date: Optional[date] = None) -> Tuple[bool, str]:
    """
    Check if the given date is a Friday before a Monday holiday.

    Important for early position exit decisions due to weekend + holiday gap risk.

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
    is_holiday, holiday_name = is_nse_holiday(monday)

    return is_holiday, holiday_name


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

    # Count consecutive off days starting from Saturday
    off_days = 2  # Saturday and Sunday
    current = check_date + timedelta(days=3)  # Monday

    while True:
        is_holiday, _ = is_nse_holiday(current)
        if is_holiday:
            off_days += 1
            current += timedelta(days=1)
        else:
            break

        # Safety limit
        if off_days > 10:
            break

    return off_days >= 3, off_days


def get_trading_days_until(target_date: date, from_date: Optional[date] = None) -> int:
    """
    Count trading days between two dates.

    Args:
        target_date: End date
        from_date: Start date (default today)

    Returns:
        Number of trading days (excluding from_date, including target_date)
    """
    if from_date is None:
        from_date = date.today()

    trading_days = 0
    current = from_date + timedelta(days=1)

    while current <= target_date:
        if is_trading_day(current):
            trading_days += 1
        current += timedelta(days=1)

    return trading_days


def is_market_open(current_time: Optional[datetime] = None) -> bool:
    """
    Check if market is currently open.

    Market hours: 09:15 to 15:30 IST on trading days.

    Args:
        current_time: Time to check (default now)

    Returns:
        True if market is open
    """
    if current_time is None:
        current_time = datetime.now()

    # Check if trading day
    if not is_trading_day(current_time.date()):
        return False

    # Check time
    market_open = time(9, 15)
    market_close = time(15, 30)
    current = current_time.time()

    return market_open <= current <= market_close


def get_next_trading_day(from_date: Optional[date] = None) -> date:
    """
    Get the next trading day.

    Args:
        from_date: Starting date (default today)

    Returns:
        Next trading day date
    """
    if from_date is None:
        from_date = date.today()

    next_day = from_date + timedelta(days=1)

    while not is_trading_day(next_day):
        next_day += timedelta(days=1)

    return next_day


def get_market_open_time(for_date: Optional[date] = None) -> datetime:
    """
    Get market open time for a date.

    Args:
        for_date: Date (default today)

    Returns:
        Market open datetime (09:15)
    """
    if for_date is None:
        for_date = date.today()

    return datetime.combine(for_date, time(9, 15))


def get_market_close_time(for_date: Optional[date] = None) -> datetime:
    """
    Get market close time for a date.

    Args:
        for_date: Date (default today)

    Returns:
        Market close datetime (15:30)
    """
    if for_date is None:
        for_date = date.today()

    return datetime.combine(for_date, time(15, 30))


def format_time_ist(dt: datetime) -> str:
    """Format datetime as IST string."""
    return dt.strftime("%Y-%m-%d %H:%M:%S IST")


def parse_time(time_str: str) -> time:
    """
    Parse time string in HH:MM format.

    Args:
        time_str: Time string (e.g., "09:15")

    Returns:
        time object

    Raises:
        ValueError: If time_str is not in valid HH:MM format
    """
    parts = time_str.split(":")
    if len(parts) < 2:
        raise ValueError(f"Invalid time format: {time_str}. Expected HH:MM")
    return time(int(parts[0]), int(parts[1]))


# =============================================================================
# FORMATTING UTILITIES
# =============================================================================

def format_currency(amount: float, symbol: str = "₹") -> str:
    """
    Format amount as Indian currency.

    Args:
        amount: Amount to format
        symbol: Currency symbol

    Returns:
        Formatted string (e.g., "₹1,23,456.78")
    """
    # Indian number system formatting
    if amount < 0:
        return f"-{symbol}{abs(amount):,.2f}"
    return f"{symbol}{amount:,.2f}"


def format_percentage(value: float, decimals: int = 2) -> str:
    """
    Format value as percentage.

    Args:
        value: Value (0.15 = 15%)
        decimals: Decimal places

    Returns:
        Formatted percentage string
    """
    return f"{value * 100:.{decimals}f}%"


def format_points(value: float) -> str:
    """Format value as points."""
    if value >= 0:
        return f"+{value:.2f}"
    return f"{value:.2f}"


def format_pnl(pnl: float) -> str:
    """
    Format P&L with color indicators.

    Args:
        pnl: P&L value

    Returns:
        Formatted string with emoji indicator
    """
    if pnl >= 0:
        return f"🟢 +₹{pnl:,.2f}"
    return f"🔴 -₹{abs(pnl):,.2f}"


def truncate_string(s: str, max_length: int = 50, suffix: str = "...") -> str:
    """Truncate string to max length."""
    if len(s) <= max_length:
        return s
    return s[:max_length - len(suffix)] + suffix


# =============================================================================
# FILE UTILITIES
# =============================================================================

def ensure_directory(path: Path) -> Path:
    """
    Ensure directory exists, create if not.

    Args:
        path: Directory path

    Returns:
        Path object
    """
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_json_load(file_path: Path, default: Any = None) -> Any:
    """
    Safely load JSON file.

    Args:
        file_path: Path to JSON file
        default: Default value if load fails

    Returns:
        Loaded data or default
    """
    try:
        if file_path.exists():
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load JSON from {file_path}: {e}")

    return default


def safe_json_save(file_path: Path, data: Any, indent: int = 2) -> bool:
    """
    Safely save data to JSON file.

    Args:
        file_path: Path to JSON file
        data: Data to save
        indent: JSON indentation

    Returns:
        True if successful
    """
    try:
        ensure_directory(file_path.parent)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=indent, default=str)
        return True
    except Exception as e:
        logger.error(f"Failed to save JSON to {file_path}: {e}")
        return False


def get_file_hash(file_path: Path) -> str:
    """
    Get MD5 hash of file contents.

    Args:
        file_path: Path to file

    Returns:
        MD5 hash string
    """
    if not file_path.exists():
        return ""

    md5 = hashlib.md5()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            md5.update(chunk)

    return md5.hexdigest()


# =============================================================================
# RETRY UTILITIES
# =============================================================================

def retry_with_backoff(
    func: Callable[[], T],
    max_retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: tuple = (Exception,)
) -> T:
    """
    Retry function with exponential backoff.

    Args:
        func: Function to retry
        max_retries: Maximum number of retries
        initial_delay: Initial delay between retries
        backoff_factor: Multiplier for delay
        exceptions: Exceptions to catch and retry

    Returns:
        Function result

    Raises:
        Last exception if all retries fail
    """
    import time

    delay = initial_delay
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            return func()
        except exceptions as e:
            last_exception = e
            if attempt < max_retries:
                logger.warning(f"Retry {attempt + 1}/{max_retries} after {delay}s: {e}")
                time.sleep(delay)
                delay *= backoff_factor

    raise last_exception


def retry_decorator(
    max_retries: int = 3,
    delay: float = 1.0,
    exceptions: tuple = (Exception,)
):
    """
    Decorator for retry with backoff.

    Args:
        max_retries: Maximum retries
        delay: Delay between retries
        exceptions: Exceptions to catch
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            return retry_with_backoff(
                lambda: func(*args, **kwargs),
                max_retries=max_retries,
                initial_delay=delay,
                exceptions=exceptions
            )
        return wrapper
    return decorator


# =============================================================================
# VALIDATION UTILITIES
# =============================================================================

def validate_nifty_symbol(symbol: str) -> bool:
    """
    Validate NIFTY option symbol format.

    Args:
        symbol: Symbol to validate

    Returns:
        True if valid format
    """
    if not symbol.startswith("NIFTY"):
        return False

    if not symbol.endswith("CE") and not symbol.endswith("PE"):
        return False

    return len(symbol) >= 15


def validate_strike_price(strike: int) -> bool:
    """
    Validate NIFTY strike price.

    Args:
        strike: Strike price

    Returns:
        True if valid
    """
    # Must be multiple of 50
    if strike % 50 != 0:
        return False

    # Reasonable range
    return 10000 <= strike <= 50000


def validate_quantity(quantity: int, lot_size: int = 75) -> bool:
    """
    Validate order quantity.

    Args:
        quantity: Order quantity
        lot_size: Lot size (default 75)

    Returns:
        True if valid (positive multiple of lot size)
    """
    return quantity > 0 and quantity % lot_size == 0


def validate_price(price: float) -> bool:
    """
    Validate option price.

    Args:
        price: Option price

    Returns:
        True if valid
    """
    return 0 < price < 10000


# =============================================================================
# THREAD UTILITIES
# =============================================================================

class ThreadSafeCounter:
    """Thread-safe counter."""

    def __init__(self, initial: int = 0):
        self._value = initial
        self._lock = threading.Lock()

    def increment(self, amount: int = 1) -> int:
        """Increment and return new value."""
        with self._lock:
            self._value += amount
            return self._value

    def decrement(self, amount: int = 1) -> int:
        """Decrement and return new value."""
        with self._lock:
            self._value -= amount
            return self._value

    @property
    def value(self) -> int:
        """Get current value."""
        with self._lock:
            return self._value


class SingletonMeta(type):
    """
    Thread-safe singleton metaclass.

    Usage:
        class MyClass(metaclass=SingletonMeta):
            pass
    """
    _instances: Dict = {}
    _lock = threading.Lock()

    def __call__(cls, *args, **kwargs):
        with cls._lock:
            if cls not in cls._instances:
                instance = super().__call__(*args, **kwargs)
                cls._instances[cls] = instance
        return cls._instances[cls]


# =============================================================================
# LOGGING UTILITIES
# =============================================================================

def setup_logging(
    log_level: str = "INFO",
    log_file: Optional[Path] = None,
    rotation: str = "00:00",
    retention: str = "30 days"
) -> None:
    """
    Setup logging configuration with daily log files.

    Args:
        log_level: Minimum log level
        log_file: Path to log file (optional). Date will be appended to filename.
        rotation: Log rotation setting (default: "00:00" for daily rotation at midnight)
        retention: Log retention period (default: "30 days")
    """
    # Remove default handler
    logger.remove()

    # Console handler
    logger.add(
        sys.stderr,
        level=log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        colorize=True
    )

    # File handler with daily rotation
    if log_file:
        ensure_directory(log_file.parent)
        # Use date in filename: snail.log -> snail_2025-12-08.log
        log_dir = log_file.parent
        log_stem = log_file.stem  # e.g., "snail"
        log_suffix = log_file.suffix  # e.g., ".log"
        dated_log_file = log_dir / f"{log_stem}_{{time:YYYY-MM-DD}}{log_suffix}"

        logger.add(
            str(dated_log_file),
            level=log_level,
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
            rotation=rotation,
            retention=retention,
            compression="zip"
        )


def log_exception(e: Exception, context: str = "") -> None:
    """
    Log exception with traceback.

    Args:
        e: Exception to log
        context: Additional context
    """
    import traceback

    tb = traceback.format_exc()
    if context:
        logger.error(f"{context}: {e}\n{tb}")
    else:
        logger.error(f"{e}\n{tb}")


# =============================================================================
# STATE FILE UTILITIES
# =============================================================================

def load_state(state_name: str, default: Any = None) -> Any:
    """
    Load state from data directory.

    Args:
        state_name: State file name (without extension)
        default: Default value if not found

    Returns:
        Loaded state or default
    """
    state_file = PROJECT_ROOT / "data" / f"{state_name}.json"
    return safe_json_load(state_file, default)


def save_state(state_name: str, data: Any) -> bool:
    """
    Save state to data directory.

    Args:
        state_name: State file name (without extension)
        data: Data to save

    Returns:
        True if successful
    """
    state_file = PROJECT_ROOT / "data" / f"{state_name}.json"
    return safe_json_save(state_file, data)


# =============================================================================
# COOLDOWN MANAGEMENT
# =============================================================================

class CooldownManager:
    """
    Manages cooldown periods for various actions.

    Thread-safe cooldown tracking.
    """

    def __init__(self):
        self._cooldowns: Dict[str, datetime] = {}
        self._lock = threading.Lock()

    def set_cooldown(self, key: str, duration_seconds: int) -> None:
        """Set cooldown for a key."""
        with self._lock:
            self._cooldowns[key] = datetime.now() + timedelta(seconds=duration_seconds)

    def is_on_cooldown(self, key: str) -> bool:
        """Check if key is on cooldown."""
        with self._lock:
            if key not in self._cooldowns:
                return False
            return datetime.now() < self._cooldowns[key]

    def get_remaining(self, key: str) -> int:
        """Get remaining cooldown seconds."""
        with self._lock:
            if key not in self._cooldowns:
                return 0
            remaining = self._cooldowns[key] - datetime.now()
            return max(0, int(remaining.total_seconds()))

    def clear_cooldown(self, key: str) -> None:
        """Clear cooldown for a key."""
        with self._lock:
            self._cooldowns.pop(key, None)

    def clear_all(self) -> None:
        """Clear all cooldowns."""
        with self._lock:
            self._cooldowns.clear()


# =============================================================================
# GLOBAL COOLDOWN INSTANCE
# =============================================================================

_cooldown_manager: Optional[CooldownManager] = None


def get_cooldown_manager() -> CooldownManager:
    """Get singleton cooldown manager."""
    global _cooldown_manager
    if _cooldown_manager is None:
        _cooldown_manager = CooldownManager()
    return _cooldown_manager


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("SNAIL Helpers Test")
    print("=" * 60)

    # Test date/time utilities
    print("\n[1] Date/Time Utilities:")
    print(f"    Is trading day (today): {is_trading_day()}")
    print(f"    Is market open: {is_market_open()}")
    print(f"    Next trading day: {get_next_trading_day()}")
    print(f"    Market open time: {get_market_open_time()}")

    # Test formatting
    print("\n[2] Formatting Utilities:")
    print(f"    Currency: {format_currency(123456.78)}")
    print(f"    Percentage: {format_percentage(0.1234)}")
    print(f"    Points: {format_points(50.25)}")
    print(f"    P&L (positive): {format_pnl(5000)}")
    print(f"    P&L (negative): {format_pnl(-3000)}")

    # Test validation
    print("\n[3] Validation Utilities:")
    test_symbols = ["NIFTY2511424000CE", "NIFTY25JAN24000PE", "INVALID"]
    for sym in test_symbols:
        print(f"    {sym}: {validate_nifty_symbol(sym)}")

    # Test cooldown
    print("\n[4] Cooldown Manager:")
    cm = get_cooldown_manager()
    cm.set_cooldown("test", 5)
    print("    Set cooldown for 5s")
    print(f"    Is on cooldown: {cm.is_on_cooldown('test')}")
    print(f"    Remaining: {cm.get_remaining('test')}s")

    # Test thread-safe counter
    print("\n[5] Thread-Safe Counter:")
    counter = ThreadSafeCounter(0)
    counter.increment()
    counter.increment(5)
    print(f"    After +1, +5: {counter.value}")
    counter.decrement(2)
    print(f"    After -2: {counter.value}")

    print("\n" + "=" * 60)
    print("Helpers test complete!")
    print("=" * 60 + "\n")
