#!/usr/bin/env python3
"""
Download Historical Data for Forward Testing

Downloads and saves 15M candle data for:
1. Index spot prices (NIFTY, BANKNIFTY, SENSEX)
2. Index options (2nd and 3rd OTM CE/PE)

Run this when Kite token is valid to save data for offline testing.

Usage:
    python tests/momentum_forward_test/download_data.py --start 2026-01-01 --end 2026-01-14
"""

import json
import csv
import argparse
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Any

# Add parent paths
import sys
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
BOTS_DIR = PROJECT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

from kiteconnect import KiteConnect

# =============================================================================
# PATHS
# =============================================================================
DATA_DIR = SCRIPT_DIR / 'data'
TOKEN_FILE = BOTS_DIR / 'data' / 'kite_access_token.json'
INDEX_OPTIONS_FILE = BOTS_DIR / 'data' / 'index_options.csv'

DATA_DIR.mkdir(parents=True, exist_ok=True)


# =============================================================================
# CONFIGURATION
# =============================================================================
INDEX_CONFIG: Dict[str, Dict[str, Any]] = {
    'NIFTY': {
        'strike_gap': 50,
        'index_token': 256265,
        'exchange': 'NFO',
        'lot_size': 75,
    },
    'BANKNIFTY': {
        'strike_gap': 100,
        'index_token': 260105,
        'exchange': 'NFO',
        'lot_size': 30,
    },
    'SENSEX': {
        'strike_gap': 100,
        'index_token': 265,
        'exchange': 'BFO',
        'lot_size': 20,
    }
}

OTM_POSITIONS = [2, 3]


# =============================================================================
# KITE CONNECTION
# =============================================================================
def get_kite() -> KiteConnect:
    """Get authenticated Kite connection."""
    if not TOKEN_FILE.exists():
        raise FileNotFoundError(f"Token file not found: {TOKEN_FILE}")

    with open(TOKEN_FILE) as f:
        token_data = json.load(f)

    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


# =============================================================================
# HELPERS
# =============================================================================
def is_last_thursday_of_month(date_obj: datetime) -> bool:
    """Check if date is the last Thursday of its month."""
    if date_obj.weekday() != 3:
        return False
    next_thursday = date_obj + timedelta(days=7)
    return next_thursday.month != date_obj.month


def get_atm_strike(spot: float, strike_gap: int) -> int:
    """Calculate ATM strike."""
    return int(round(spot / strike_gap) * strike_gap)


def get_otm_strikes(spot: float, index: str) -> Dict[str, List[int]]:
    """Get 2nd and 3rd OTM strikes for CE and PE."""
    config = INDEX_CONFIG[index]
    strike_gap = int(config['strike_gap'])
    atm = get_atm_strike(spot, strike_gap)

    return {
        'CE': [atm + (i * strike_gap) for i in OTM_POSITIONS],
        'PE': [atm - (i * strike_gap) for i in OTM_POSITIONS]
    }


def load_index_options(target_date: datetime) -> Dict[str, List[dict]]:
    """Load index options from CSV."""
    if not INDEX_OPTIONS_FILE.exists():
        print(f"ERROR: Index options file not found: {INDEX_OPTIONS_FILE}")
        return {}

    options: Dict[str, List[dict]] = {}

    with open(INDEX_OPTIONS_FILE, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            index = row.get('index', '')
            if index not in INDEX_CONFIG:
                continue

            try:
                expiry_str = row.get('expiry', '')
                if not expiry_str:
                    continue

                expiry_date = datetime.strptime(expiry_str, '%Y-%m-%d').date()

                if expiry_date < target_date.date():
                    continue

                expiry_dt = datetime.combine(expiry_date, datetime.min.time())
                if not is_last_thursday_of_month(expiry_dt):
                    continue

                if index not in options:
                    options[index] = []

                option_symbol = row.get('option_symbol', '')
                option_type = row.get('option_type', '')
                strike_str = row.get('strike', '')
                option_token_str = row.get('option_token', '')

                if not all([option_symbol, option_type, strike_str, option_token_str]):
                    continue

                options[index].append({
                    'option_symbol': option_symbol,
                    'option_type': option_type,
                    'strike': float(strike_str),
                    'expiry': expiry_str,
                    'option_token': int(option_token_str),
                    'lot_size': int(row.get('lot_size') or 1),
                    'exchange': row.get('exchange') or 'NFO',
                })
            except (ValueError, KeyError):
                continue

    for index in options:
        options[index].sort(key=lambda x: (x['expiry'], x['strike']))

    return options


def candle_to_dict(candle: dict) -> dict:
    """Convert candle to serializable dict."""
    result = {}
    for key, value in candle.items():
        if isinstance(value, datetime):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


# =============================================================================
# DOWNLOAD FUNCTIONS
# =============================================================================
def download_spot_candles(
    kite: KiteConnect,
    index: str,
    start_date: datetime,
    end_date: datetime
) -> List[dict]:
    """Download 15M spot candles for an index."""
    token = INDEX_CONFIG[index]['index_token']

    try:
        data = kite.historical_data(
            instrument_token=token,
            from_date=start_date,
            to_date=end_date,
            interval='15minute'
        )
        return [candle_to_dict(c) for c in data]
    except Exception as e:
        print(f"  ERROR downloading {index} spot: {e}")
        return []


def download_option_candles(
    kite: KiteConnect,
    option_token: int,
    option_symbol: str,
    start_date: datetime,
    end_date: datetime
) -> List[dict]:
    """Download 15M candles for an option."""
    try:
        data = kite.historical_data(
            instrument_token=option_token,
            from_date=start_date,
            to_date=end_date,
            interval='15minute'
        )
        return [candle_to_dict(c) for c in data]
    except Exception as e:
        print(f"  ERROR downloading {option_symbol}: {e}")
        return []


# =============================================================================
# MAIN
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description='Download Historical Data for Forward Test')
    parser.add_argument('--start', type=str, default='2026-01-01', help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, default=None, help='End date (YYYY-MM-DD)')
    args = parser.parse_args()

    start_date = datetime.strptime(args.start, '%Y-%m-%d')
    end_date = datetime.strptime(args.end, '%Y-%m-%d') if args.end else datetime.now()

    print("="*70)
    print("DOWNLOAD HISTORICAL DATA FOR FORWARD TEST")
    print("="*70)
    print(f"Period: {start_date.date()} to {end_date.date()}")
    print(f"Output: {DATA_DIR}")
    print()

    try:
        print("Connecting to Kite...")
        kite = get_kite()
        print("Connected!")

        # 1. Download spot candles for each index
        print("\n1. Downloading spot candles...")
        spot_data: Dict[str, List[dict]] = {}

        for index in INDEX_CONFIG:
            print(f"  Downloading {index} spot...")
            candles = download_spot_candles(kite, index, start_date, end_date)
            if candles:
                spot_data[index] = candles
                print(f"  {index}: {len(candles)} candles")
            time.sleep(0.5)  # Rate limiting

        # Save spot data
        spot_file = DATA_DIR / 'spot_candles.json'
        with open(spot_file, 'w') as f:
            json.dump({
                'start': start_date.isoformat(),
                'end': end_date.isoformat(),
                'data': spot_data
            }, f, indent=2)
        print(f"\n  Saved spot data to: {spot_file}")

        # 2. Determine which options to download
        print("\n2. Loading index options...")
        index_options = load_index_options(start_date)
        for idx, opts in index_options.items():
            print(f"  {idx}: {len(opts)} options")

        # 3. Get representative spot prices to determine ATM
        # Use the middle of the date range
        mid_date = start_date + (end_date - start_date) / 2
        print(f"\n3. Getting spot prices around {mid_date.date()}...")

        # Get ATM for each index based on spot data
        option_tokens_to_download: Dict[str, dict] = {}

        for index, candles in spot_data.items():
            if not candles:
                continue

            # Get average spot from the data
            avg_spot = sum(c['close'] for c in candles) / len(candles)
            print(f"  {index} avg spot: {avg_spot:.0f}")

            strike_gap = INDEX_CONFIG[index]['strike_gap']
            atm = get_atm_strike(avg_spot, strike_gap)
            otm_strikes = get_otm_strikes(avg_spot, index)

            print(f"    ATM: {atm}")
            print(f"    CE OTM strikes: {otm_strikes['CE']}")
            print(f"    PE OTM strikes: {otm_strikes['PE']}")

            # Find matching options
            options = index_options.get(index, [])
            for opt_type in ['CE', 'PE']:
                for strike in otm_strikes[opt_type]:
                    for opt in options:
                        if opt['strike'] == strike and opt['option_type'] == opt_type:
                            key = f"{index}_{opt['option_symbol']}"
                            option_tokens_to_download[key] = opt
                            break

        print(f"\n4. Downloading {len(option_tokens_to_download)} option contracts...")

        option_data: Dict[str, List[dict]] = {}
        for key, opt in option_tokens_to_download.items():
            print(f"  Downloading {opt['option_symbol']}...")
            candles = download_option_candles(
                kite, opt['option_token'], opt['option_symbol'],
                start_date, end_date
            )
            if candles:
                option_data[opt['option_symbol']] = {
                    'info': opt,
                    'candles': candles
                }
                print(f"    Got {len(candles)} candles")
            time.sleep(0.5)  # Rate limiting

        # Save option data
        option_file = DATA_DIR / 'option_candles.json'
        with open(option_file, 'w') as f:
            json.dump({
                'start': start_date.isoformat(),
                'end': end_date.isoformat(),
                'options': option_data
            }, f, indent=2)
        print(f"\n  Saved option data to: {option_file}")

        # Summary
        print("\n" + "="*70)
        print("DOWNLOAD COMPLETE")
        print("="*70)
        print(f"Spot candles saved: {sum(len(c) for c in spot_data.values())}")
        print(f"Option contracts: {len(option_data)}")
        print(f"Option candles: {sum(len(o['candles']) for o in option_data.values())}")
        print(f"\nData saved to: {DATA_DIR}")

    except FileNotFoundError as e:
        print(f"ERROR: {e}")
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    main()
