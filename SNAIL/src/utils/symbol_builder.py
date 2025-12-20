"""
SNAIL Symbol Builder

Trading symbol generation for NIFTY options.

@file        symbol_builder.py
@description Option symbol format generation and validation
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 3
"""

import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, Tuple, List, Dict
from pathlib import Path
import pandas as pd
from loguru import logger


# =============================================================================
# CONSTANTS
# =============================================================================

# Month codes for weekly expiry
MONTH_CODES = {
    1: '1', 2: '2', 3: '3', 4: '4', 5: '5', 6: '6',
    7: '7', 8: '8', 9: '9', 10: 'O', 11: 'N', 12: 'D'
}

# NIFTY strike interval
NIFTY_STRIKE_INTERVAL = 50  # ATM rounds to nearest 50 (changed from 100)

# Default lot size (verify from instruments)
DEFAULT_LOT_SIZE = 75


# =============================================================================
# SYMBOL GENERATION
# =============================================================================

def generate_nifty_option_symbol(
    expiry_date: date,
    strike: int,
    option_type: str
) -> str:
    """
    Generate NIFTY option trading symbol for Kite.

    Symbol Formats:
    - Weekly:  NIFTY{YY}{M}{DD}{strike}{CE/PE}  (e.g., NIFTY2511424000CE)
    - Monthly: NIFTY{YY}{MMM}{strike}{CE/PE}    (e.g., NIFTY25DEC26000CE)

    Args:
        expiry_date: Option expiry date
        strike: Strike price
        option_type: "CE" or "PE"

    Returns:
        Trading symbol string

    Example:
        >>> generate_nifty_option_symbol(date(2025, 1, 14), 24000, "CE")
        'NIFTY2511424000CE'
    """
    year = expiry_date.year % 100  # 2-digit year

    if is_monthly_expiry(expiry_date):
        # Monthly format: NIFTY{YY}{MMM}{strike}{CE/PE}
        month_abbr = expiry_date.strftime("%b").upper()
        symbol = f"NIFTY{year}{month_abbr}{strike}{option_type}"
    else:
        # Weekly format: NIFTY{YY}{M}{DD}{strike}{CE/PE}
        month_code = MONTH_CODES[expiry_date.month]
        day = expiry_date.day
        symbol = f"NIFTY{year}{month_code}{day:02d}{strike}{option_type}"

    return symbol


def is_monthly_expiry(expiry_date: date) -> bool:
    """
    Check if date is monthly expiry (last Tuesday of month for NIFTY).

    IMPORTANT: NIFTY weekly expiry is typically Tuesday (not Thursday).
    However, always use expiry_date from instruments CSV - never hardcode
    the day of week as it can shift due to holidays.

    Args:
        expiry_date: Date to check

    Returns:
        True if this is the monthly expiry
    """
    # Check if this is a Tuesday
    if expiry_date.weekday() != 1:  # Tuesday = 1
        return False

    # Check if there's another Tuesday in the same month
    next_week = expiry_date + timedelta(days=7)
    return next_week.month != expiry_date.month


def generate_possible_symbols(
    instrument: str,
    strike: int,
    expiry: date,
    option_type: str
) -> List[str]:
    """
    Generate all possible trading symbol formats for an option.

    Use this when the exact format is uncertain - try multiple formats.

    Args:
        instrument: Base instrument (e.g., "NIFTY")
        strike: Strike price
        expiry: Expiry date
        option_type: "CE" or "PE"

    Returns:
        List of possible symbol strings
    """
    day = expiry.strftime('%d')           # '14'
    month_num = expiry.strftime('%m')     # '01'
    month_abbr = expiry.strftime('%b').upper()  # 'JAN'
    year_short = expiry.strftime('%y')    # '25'
    month_code = MONTH_CODES[expiry.month]

    formats = [
        # Primary weekly format: NIFTY{YY}{M}{DD}{strike}
        f"{instrument}{year_short}{month_code}{day}{strike}",

        # Monthly format: NIFTY{YY}{MMM}{strike}
        f"{instrument}{year_short}{month_abbr}{strike}",

        # Alternative: NIFTY{DD}{MM}{YY}{strike}
        f"{instrument}{day}{month_num}{year_short}{strike}",

        # Alternative: NIFTY{DD}{MMM}{YY}{strike}
        f"{instrument}{day}{month_abbr}{year_short}{strike}",
    ]

    # Add option type suffix
    return [f"{fmt}{option_type}" for fmt in formats]


# =============================================================================
# STRIKE CALCULATIONS
# =============================================================================

def calculate_atm_strike(nifty_spot: float, interval: int = NIFTY_STRIKE_INTERVAL) -> int:
    """
    Calculate ATM strike from NIFTY spot price.

    ATM strike = NIFTY spot rounded to nearest strike interval.

    Args:
        nifty_spot: Current NIFTY spot price
        interval: Strike interval (default 50 for NIFTY)

    Returns:
        ATM strike price

    Example:
        >>> calculate_atm_strike(24073.45)
        24100
        >>> calculate_atm_strike(24049.00)
        24050
    """
    return round(nifty_spot / interval) * interval


def calculate_wing_distance(
    ce_premium: float,
    pe_premium: float,
    multiplier: float = 1.0,
    round_to: int = 50,
    min_distance: int = 200
) -> int:
    """
    Calculate dynamic wing distance based on straddle premium with multiplier.

    Wing distance = (CE + PE) * multiplier, rounded to nearest interval.

    Based on backtest analysis (2021-2025):
    - 1.0x multiplier: 32.8% breach rate, lower profit
    - 1.2x multiplier: 10.7% breach rate, higher profit (recommended)

    Args:
        ce_premium: ATM CE premium (bid price)
        pe_premium: ATM PE premium (bid price)
        multiplier: Wing distance multiplier (default 1.0, recommended 1.2)
        round_to: Round to nearest this value (default 50)
        min_distance: Minimum wing distance (default 200)

    Returns:
        Wing distance in points

    Example:
        >>> calculate_wing_distance(170, 130, multiplier=1.0)  # straddle=300
        300
        >>> calculate_wing_distance(170, 130, multiplier=1.2)  # straddle=300*1.2=360
        350  # rounds to nearest 50
        >>> calculate_wing_distance(160, 115, multiplier=1.2)  # straddle=275*1.2=330
        350  # rounds to nearest 50
    """
    straddle_premium = ce_premium + pe_premium
    adjusted_premium = straddle_premium * multiplier
    calculated = round(adjusted_premium / round_to) * round_to
    return max(calculated, min_distance)


def get_iron_fly_strikes(
    atm_strike: int,
    wing_distance: int
) -> Dict[str, int]:
    """
    Get all strikes for an Iron Fly position.

    Args:
        atm_strike: ATM strike price
        wing_distance: Wing distance in points

    Returns:
        Dictionary with strike prices for each leg
    """
    return {
        'straddle_ce': atm_strike,
        'straddle_pe': atm_strike,
        'wing_ce': atm_strike + wing_distance,
        'wing_pe': atm_strike - wing_distance
    }


# =============================================================================
# INSTRUMENTS CSV OPERATIONS (TDD Section 5.1)
# =============================================================================

# Instruments refresh configuration
INSTRUMENTS_MAX_RETRIES = 3
INSTRUMENTS_RETRY_BACKOFF = [2, 5, 10]  # Exponential backoff in seconds
INSTRUMENTS_MAX_AGE_HOURS = 24  # Max age before considered stale


def get_instruments_age(instruments_path: Path) -> Optional[float]:
    """
    Get age of instruments file in hours.

    Args:
        instruments_path: Path to instruments CSV

    Returns:
        Age in hours, or None if file doesn't exist
    """
    if not instruments_path.exists():
        return None

    import os
    mtime = os.path.getmtime(instruments_path)
    age_seconds = time.time() - mtime
    return age_seconds / 3600


def refresh_instruments_csv(
    kite,
    instruments_path: Path,
    max_retries: int = INSTRUMENTS_MAX_RETRIES
) -> Tuple[bool, str]:
    """
    Refresh instruments CSV from Kite API with retry logic.

    TDD Section 5.1: Daily Startup - Instruments Refresh

    Implements:
    - 3 retries with exponential backoff (2s, 5s, 10s)
    - Falls back to cache if less than 24 hours old
    - Alerts if no valid cache available

    Args:
        kite: Kite client instance
        instruments_path: Path to save instruments CSV
        max_retries: Maximum retry attempts

    Returns:
        Tuple of (success, message)
    """
    last_error = None

    for attempt in range(max_retries):
        try:
            logger.info(f"Downloading instruments (attempt {attempt + 1}/{max_retries})...")

            # Fetch NFO instruments
            instruments = kite.instruments("NFO")

            if not instruments:
                raise ValueError("Empty instruments response from Kite")

            # Convert to DataFrame and save
            df = pd.DataFrame(instruments)

            # Filter to NIFTY only
            nifty_df = df[df['name'] == 'NIFTY']

            if len(nifty_df) == 0:
                raise ValueError("No NIFTY instruments in response")

            # Save to CSV
            nifty_df.to_csv(instruments_path, index=False)
            logger.info(f"Instruments saved: {len(nifty_df)} NIFTY instruments")

            return True, f"Instruments refreshed: {len(nifty_df)} records"

        except Exception as e:
            last_error = str(e)
            logger.warning(f"Instruments refresh attempt {attempt + 1} failed: {e}")

            if attempt < max_retries - 1:
                backoff = INSTRUMENTS_RETRY_BACKOFF[min(attempt, len(INSTRUMENTS_RETRY_BACKOFF) - 1)]
                logger.info(f"Retrying in {backoff}s...")
                time.sleep(backoff)

    # All retries failed - check cache
    logger.error(f"All {max_retries} instrument refresh attempts failed")

    cache_age = get_instruments_age(instruments_path)
    if cache_age is not None and cache_age < INSTRUMENTS_MAX_AGE_HOURS:
        logger.warning(f"Using cached instruments ({cache_age:.1f} hours old)")
        return True, f"Using cached instruments ({cache_age:.1f}h old). Refresh failed: {last_error}"

    # No valid cache
    if cache_age is not None:
        return False, f"Instruments cache too old ({cache_age:.1f}h). Refresh failed: {last_error}"
    else:
        return False, f"No instruments cache available. Refresh failed: {last_error}"


def load_instruments(instruments_path: Path) -> pd.DataFrame:
    """
    Load NIFTY options instruments from CSV.

    Args:
        instruments_path: Path to instruments CSV file

    Returns:
        DataFrame with instrument data

    Raises:
        FileNotFoundError: If file doesn't exist
    """
    if not instruments_path.exists():
        raise FileNotFoundError(f"Instruments file not found: {instruments_path}")

    # Check staleness
    age = get_instruments_age(instruments_path)
    if age is not None and age > INSTRUMENTS_MAX_AGE_HOURS:
        logger.warning(f"Instruments file is stale ({age:.1f} hours old)")

    df = pd.read_csv(instruments_path)
    logger.debug(f"Loaded {len(df)} instruments from {instruments_path}")
    return df


def find_tradingsymbol(
    instruments_df: pd.DataFrame,
    instrument: str,
    strike: int,
    expiry: date,
    option_type: str
) -> Tuple[Optional[str], Optional[str]]:
    """
    Find matching tradingsymbol and instrument_token.

    Tries multiple symbol formats to find a match.

    Args:
        instruments_df: Instruments DataFrame
        instrument: Base instrument (e.g., "NIFTY")
        strike: Strike price
        expiry: Expiry date
        option_type: "CE" or "PE"

    Returns:
        (tradingsymbol, instrument_token) or (None, None) if not found
    """
    possible_symbols = generate_possible_symbols(instrument, strike, expiry, option_type)

    for symbol in possible_symbols:
        match = instruments_df[instruments_df['tradingsymbol'] == symbol]
        if not match.empty:
            return symbol, str(match['instrument_token'].iloc[0])

    logger.warning(f"No match found for {instrument} {strike} {option_type} exp {expiry}. "
                   f"Tried: {possible_symbols[:2]}...")
    return None, None


def get_lot_size_from_instruments(
    instruments_df: pd.DataFrame,
    expiry: date
) -> int:
    """
    Get lot size for NIFTY options from instruments CSV.

    Args:
        instruments_df: Instruments DataFrame
        expiry: Target expiry date

    Returns:
        Lot size (e.g., 75 for NIFTY)
    """
    # Convert expiry to match CSV format
    expiry_str = expiry.strftime('%Y-%m-%d')

    # Filter to NIFTY options for this expiry
    mask = (
        (instruments_df['name'] == 'NIFTY') &
        (instruments_df['expiry'].str.startswith(expiry_str))
    )
    filtered = instruments_df[mask]

    if filtered.empty:
        logger.warning(f"No instruments found for expiry {expiry}, using default lot size {DEFAULT_LOT_SIZE}")
        return DEFAULT_LOT_SIZE

    return int(filtered['lot_size'].iloc[0])


def get_target_expiry(
    instruments_df: pd.DataFrame,
    min_dte: int = 6
) -> date:
    """
    Get target expiry based on DTE requirement.

    Automatically selects next week's expiry if current week's DTE < min_dte.

    Args:
        instruments_df: Instruments DataFrame with 'expiry' column
        min_dte: Minimum days to expiry (default 6)

    Returns:
        Target expiry date

    Raises:
        ValueError: If no valid expiry found
    """
    today = date.today()

    # Parse expiries
    instruments_df = instruments_df.copy()
    instruments_df['expiry_date'] = pd.to_datetime(instruments_df['expiry']).dt.date

    # Get unique expiries for NIFTY, sorted
    nifty_mask = instruments_df['name'] == 'NIFTY'
    expiries = sorted(instruments_df[nifty_mask]['expiry_date'].unique())

    # Filter by DTE
    valid_expiries = [exp for exp in expiries if (exp - today).days >= min_dte]

    if not valid_expiries:
        raise ValueError(f"No expiry found with DTE >= {min_dte}")

    selected = valid_expiries[0]
    dte = (selected - today).days
    logger.info(f"Selected expiry: {selected} (DTE: {dte} days)")

    return selected


def get_all_expiries(instruments_df: pd.DataFrame) -> List[date]:
    """
    Get all available NIFTY expiry dates.

    Args:
        instruments_df: Instruments DataFrame

    Returns:
        Sorted list of expiry dates
    """
    instruments_df = instruments_df.copy()
    instruments_df['expiry_date'] = pd.to_datetime(instruments_df['expiry']).dt.date

    nifty_mask = instruments_df['name'] == 'NIFTY'
    expiries = sorted(instruments_df[nifty_mask]['expiry_date'].unique())

    return [exp for exp in expiries if exp >= date.today()]


# =============================================================================
# FUTURES INSTRUMENTS
# =============================================================================

@dataclass
class FuturesContract:
    """
    NIFTY Futures contract details.

    Attributes:
        tradingsymbol: Trading symbol (e.g., NIFTY25DECFUT)
        instrument_token: Kite instrument token
        expiry_date: Contract expiry date
        lot_size: Contract lot size
        dte: Days to expiry
    """
    tradingsymbol: str
    instrument_token: int
    expiry_date: date
    lot_size: int
    dte: int


def get_nearest_futures_contract(instruments_df: pd.DataFrame) -> Optional[FuturesContract]:
    """
    Get the nearest (current month) NIFTY futures contract.

    Uses the futures contract with earliest expiry that is still valid (expiry >= today).
    This is typically the "current month" contract used for hedging/pricing.

    Args:
        instruments_df: Instruments DataFrame from Kite

    Returns:
        FuturesContract with details, or None if not found

    Example:
        >>> contract = get_nearest_futures_contract(instruments_df)
        >>> if contract:
        ...     print(f"Using {contract.tradingsymbol} (DTE: {contract.dte})")
    """
    today = date.today()

    # Validate required columns exist
    required_columns = ['name', 'instrument_type', 'expiry', 'tradingsymbol', 'instrument_token', 'lot_size']
    missing_columns = [col for col in required_columns if col not in instruments_df.columns]
    if missing_columns:
        logger.error(f"Instruments DataFrame missing required columns: {missing_columns}")
        return None

    # Filter to NIFTY futures only
    futures_mask = (
        (instruments_df['name'] == 'NIFTY') &
        (instruments_df['instrument_type'] == 'FUT')
    )
    futures_df = instruments_df[futures_mask].copy()

    if futures_df.empty:
        logger.warning("No NIFTY futures found in instruments")
        return None

    # Parse expiry dates with error handling
    try:
        futures_df['expiry_date'] = pd.to_datetime(futures_df['expiry']).dt.date
    except Exception as e:
        logger.error(f"Failed to parse futures expiry dates: {e}")
        return None

    # Filter to valid (not expired) contracts
    valid_futures = futures_df[futures_df['expiry_date'] >= today]

    if valid_futures.empty:
        logger.warning("No valid NIFTY futures (all expired)")
        return None

    # Sort by expiry and get nearest
    valid_futures = valid_futures.sort_values('expiry_date')
    nearest = valid_futures.iloc[0]

    expiry_date = nearest['expiry_date']
    # Use max(dte, 1) to avoid 0 DTE on expiry day
    dte = max((expiry_date - today).days, 1)

    contract = FuturesContract(
        tradingsymbol=str(nearest['tradingsymbol']),
        instrument_token=int(nearest['instrument_token']),
        expiry_date=expiry_date,
        lot_size=int(nearest['lot_size']),
        dte=dte
    )

    logger.debug(f"Nearest futures: {contract.tradingsymbol} (DTE: {contract.dte})")
    return contract


def get_futures_instrument_string(futures_contract: FuturesContract) -> str:
    """
    Build full instrument string for Kite quote API.

    Args:
        futures_contract: FuturesContract object

    Returns:
        Full instrument string (e.g., "NFO:NIFTY25DECFUT")
    """
    return f"NFO:{futures_contract.tradingsymbol}"


# =============================================================================
# SYMBOL BUILDING UTILITIES
# =============================================================================

def build_instrument_string(tradingsymbol: str, exchange: str = "NFO") -> str:
    """
    Build full instrument string for Kite quote API.

    Args:
        tradingsymbol: Symbol without exchange (e.g., "NIFTY24D0526100CE")
        exchange: Exchange code (default "NFO")

    Returns:
        Full instrument string (e.g., "NFO:NIFTY24D0526100CE")
    """
    return f"{exchange}:{tradingsymbol}"


def build_iron_fly_symbols(
    expiry: date,
    atm_strike: int,
    wing_distance: int
) -> Dict[str, str]:
    """
    Build all trading symbols for an Iron Fly position.

    Args:
        expiry: Expiry date
        atm_strike: ATM strike price
        wing_distance: Wing distance in points

    Returns:
        Dictionary with tradingsymbol for each leg
    """
    return {
        'straddle_ce': generate_nifty_option_symbol(expiry, atm_strike, 'CE'),
        'straddle_pe': generate_nifty_option_symbol(expiry, atm_strike, 'PE'),
        'wing_ce': generate_nifty_option_symbol(expiry, atm_strike + wing_distance, 'CE'),
        'wing_pe': generate_nifty_option_symbol(expiry, atm_strike - wing_distance, 'PE'),
    }


def build_iron_fly_instruments(
    expiry: date,
    atm_strike: int,
    wing_distance: int,
    exchange: str = "NFO"
) -> Dict[str, str]:
    """
    Build full instrument strings for Iron Fly quote fetching.

    Args:
        expiry: Expiry date
        atm_strike: ATM strike price
        wing_distance: Wing distance in points
        exchange: Exchange code

    Returns:
        Dictionary with full instrument string for each leg
    """
    symbols = build_iron_fly_symbols(expiry, atm_strike, wing_distance)
    return {key: f"{exchange}:{symbol}" for key, symbol in symbols.items()}


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':

    print("\n" + "=" * 60)
    print("SNAIL Symbol Builder Test")
    print("=" * 60)

    # Test weekly symbol
    test_date = date(2025, 1, 14)
    symbol = generate_nifty_option_symbol(test_date, 24000, "CE")
    print(f"\n[1] Weekly symbol for {test_date}, 24000CE: {symbol}")

    # Test monthly symbol
    monthly_date = date(2025, 12, 30)
    monthly_symbol = generate_nifty_option_symbol(monthly_date, 26000, "PE")
    print(f"[2] Monthly symbol for {monthly_date}, 26000PE: {monthly_symbol}")

    # Test ATM calculation
    spot = 24150.75
    atm = calculate_atm_strike(spot)
    print(f"\n[3] ATM strike for spot {spot}: {atm}")

    # Test wing distance
    ce_prem, pe_prem = 170, 130
    wing = calculate_wing_distance(ce_prem, pe_prem)
    print(f"[4] Wing distance for CE={ce_prem}, PE={pe_prem}: {wing}")

    # Test Iron Fly symbols
    print(f"\n[5] Iron Fly symbols (ATM={atm}, Wing={wing}):")
    symbols = build_iron_fly_symbols(test_date, atm, wing)
    for leg, sym in symbols.items():
        print(f"    {leg}: {sym}")

    # Test all month codes
    print("\n[6] Month code test:")
    for month in range(1, 13):
        first_day = date(2025, month, 1)
        days_to_tuesday = (1 - first_day.weekday()) % 7
        if days_to_tuesday == 0 and first_day.weekday() != 1:
            days_to_tuesday = 7
        tuesday = first_day + timedelta(days=days_to_tuesday)
        sym = generate_nifty_option_symbol(tuesday, 24000, "CE")
        print(f"    Month {month:2d}: {sym}")

    print("\n" + "=" * 60)
    print("Symbol builder test complete!")
    print("=" * 60 + "\n")
