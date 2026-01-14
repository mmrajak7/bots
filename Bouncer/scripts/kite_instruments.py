#!/usr/bin/env python3
"""
Kite Instruments Fetcher

Fetches instruments from Kite public API (no auth required).
Used by momentum scanner and forward test.

Usage:
    from kite_instruments import fetch_index_options
    options = fetch_index_options()  # Returns {index: [options]}
"""

import csv
import requests
from io import StringIO
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from pathlib import Path

KITE_INSTRUMENTS_URL = "https://api.kite.trade/instruments"

INDEX_NAMES = ['NIFTY', 'BANKNIFTY', 'SENSEX', 'FINNIFTY']

INDEX_CONFIG = {
    'NIFTY': {'strike_gap': 50, 'index_token': 256265, 'exchange': 'NFO', 'lot_size': 75},
    'BANKNIFTY': {'strike_gap': 100, 'index_token': 260105, 'exchange': 'NFO', 'lot_size': 30},
    'SENSEX': {'strike_gap': 100, 'index_token': 265, 'exchange': 'BFO', 'lot_size': 20},
}

# Cache to avoid repeated API calls
_instruments_cache: Optional[List[dict]] = None
_cache_time: Optional[datetime] = None
CACHE_DURATION_HOURS = 4


def fetch_all_instruments(use_cache: bool = True) -> List[dict]:
    """
    Fetch all instruments from Kite public API.

    Args:
        use_cache: Use cached data if available and fresh

    Returns:
        List of instrument dictionaries
    """
    global _instruments_cache, _cache_time

    # Check cache
    if use_cache and _instruments_cache is not None and _cache_time is not None:
        cache_age = datetime.now() - _cache_time
        if cache_age < timedelta(hours=CACHE_DURATION_HOURS):
            return _instruments_cache

    # Fetch from API
    print("Fetching instruments from Kite API...")
    response = requests.get(KITE_INSTRUMENTS_URL, timeout=60)
    response.raise_for_status()

    reader = csv.DictReader(StringIO(response.text))
    instruments = list(reader)

    # Update cache
    _instruments_cache = instruments
    _cache_time = datetime.now()

    print(f"Fetched {len(instruments)} instruments")
    return instruments


def is_last_expiry_of_month(date_obj: datetime, all_expiries: set) -> bool:
    """
    Check if date is the last expiry of its month.
    Works with any expiry day (Thu/Wed/Tue).
    """
    # Find all expiries in the same month
    month_expiries = [
        e for e in all_expiries
        if e.month == date_obj.month and e.year == date_obj.year
    ]
    if not month_expiries:
        return False
    # Return True if this is the last one
    return date_obj.date() == max(e.date() for e in month_expiries)


def fetch_index_options(
    indices: Optional[List[str]] = None,
    monthly_only: bool = False,
    min_dte: int = 0,
    max_dte: int = 60
) -> Dict[str, List[dict]]:
    """
    Fetch index options from Kite public API.

    Args:
        indices: List of index names to fetch (default: NIFTY, BANKNIFTY, SENSEX)
        monthly_only: Only include monthly expiries
        min_dte: Minimum days to expiry

    Returns:
        Dict mapping index name to list of option dicts
    """
    if indices is None:
        indices = ['NIFTY', 'BANKNIFTY', 'SENSEX']

    instruments = fetch_all_instruments()
    today = datetime.now().date()

    # First pass: collect all options per index
    index_options: Dict[str, List[dict]] = {}
    index_expiries: Dict[str, set] = {}  # Track all expiries per index

    for inst in instruments:
        segment = inst.get('segment', '')
        name = inst.get('name', '')
        inst_type = inst.get('instrument_type', '')

        # Filter for index options
        if inst_type not in ('CE', 'PE'):
            continue
        if segment not in ('NFO-OPT', 'BFO-OPT'):
            continue
        if name not in indices:
            continue

        # Parse expiry
        try:
            expiry_str = inst.get('expiry', '')
            if not expiry_str:
                continue
            expiry_date = datetime.strptime(expiry_str, '%Y-%m-%d').date()
            dte = (expiry_date - today).days
        except (ValueError, TypeError):
            continue

        # Filter by DTE
        if dte < min_dte:
            continue
        if dte > max_dte:
            continue

        # Track expiry for this index
        if name not in index_expiries:
            index_expiries[name] = set()
        expiry_dt = datetime.combine(expiry_date, datetime.min.time())
        index_expiries[name].add(expiry_dt)

        # Add to results
        if name not in index_options:
            index_options[name] = []

        index_options[name].append({
            'option_symbol': inst['tradingsymbol'],
            'option_type': inst_type,
            'strike': float(inst['strike']),
            'expiry': expiry_str,
            'option_token': int(inst['instrument_token']),
            'lot_size': int(inst.get('lot_size') or 1),
            'exchange': inst.get('exchange', 'NFO'),
            'dte': dte,
        })

    # Second pass: filter for monthly expiry if needed
    if monthly_only:
        for idx in list(index_options.keys()):
            expiries = index_expiries.get(idx, set())
            index_options[idx] = [
                opt for opt in index_options[idx]
                if is_last_expiry_of_month(
                    datetime.strptime(opt['expiry'], '%Y-%m-%d'),
                    expiries
                )
            ]

    # Sort by expiry, then strike
    for idx in index_options:
        index_options[idx].sort(key=lambda x: (x['expiry'], x['strike']))

    # Summary
    total = sum(len(v) for v in index_options.values())
    print(f"Index options loaded: {total} total")
    for idx in indices:
        count = len(index_options.get(idx, []))
        print(f"  {idx}: {count} options")

    return index_options


def get_atm_strike(spot: float, strike_gap: int) -> int:
    """Calculate ATM strike."""
    return int(round(spot / strike_gap) * strike_gap)


def get_otm_strikes(
    spot: float,
    index: str,
    otm_positions: Optional[List[int]] = None
) -> Dict[str, List[int]]:
    """
    Get OTM strikes for CE and PE.

    Args:
        spot: Current spot price
        index: Index name (NIFTY, BANKNIFTY, SENSEX)
        otm_positions: Which OTM positions to get (default: [2, 3])

    Returns:
        {'CE': [strikes], 'PE': [strikes]}
    """
    if otm_positions is None:
        otm_positions = [2, 3]

    config = INDEX_CONFIG.get(index, {})
    strike_gap = int(config.get('strike_gap', 50))
    atm = get_atm_strike(spot, strike_gap)

    return {
        'CE': [atm + (i * strike_gap) for i in otm_positions],
        'PE': [atm - (i * strike_gap) for i in otm_positions]
    }


def find_option(
    options: List[dict],
    strike: float,
    option_type: str,
    expiry: Optional[str] = None
) -> Optional[dict]:
    """
    Find option matching criteria.

    Args:
        options: List of option dicts
        strike: Target strike price
        option_type: 'CE' or 'PE'
        expiry: Optional expiry date (YYYY-MM-DD)

    Returns:
        Option dict if found, else None
    """
    for opt in options:
        if opt['strike'] == strike and opt['option_type'] == option_type:
            if expiry is None or opt['expiry'] == expiry:
                return opt
    return None


def get_nearest_expiry(options: List[dict]) -> Optional[str]:
    """Get nearest expiry from options list."""
    if not options:
        return None
    expiries = sorted(set(opt['expiry'] for opt in options))
    return expiries[0] if expiries else None


# Test
if __name__ == '__main__':
    print("Testing kite_instruments module...")
    print()

    options = fetch_index_options()
    print()

    for idx, opts in options.items():
        nearest = get_nearest_expiry(opts)
        print(f"{idx}: {len(opts)} options, nearest expiry: {nearest}")
