#!/usr/bin/env python3
"""
Step 0a: Build Historical Data (Weekly - Sunday 8 PM)
=====================================================
- Fetches FRESH 4 years of daily OHLCV data for ALL F&O stocks
- Overwrites existing CSVs (to handle stock splits correctly)
- Uses 1 second delay between API calls (conservative rate limiting)
- Automatically detects F&O universe from Kite public API

Run: python scripts/0_build_historical.py
     python scripts/0_build_historical.py --force  (skip existing check)

Cron: 0 20 * * 0 (Sunday 8 PM)
"""

import csv
import json
import time
import sys
import requests
from io import StringIO
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Set, Optional
from kiteconnect import KiteConnect

# =============================================================================
# PATHS
# =============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
BOUNCER_DIR = SCRIPT_DIR.parent
BOTS_DIR = BOUNCER_DIR.parent
DATA_DIR = BOTS_DIR / 'data'
HISTORICAL_DIR = BOUNCER_DIR / 'data' / 'historical'

TOKEN_FILE = DATA_DIR / 'kite_access_token.json'
INSTRUMENTS_URL = "https://api.kite.trade/instruments"

# Ensure directories exist
HISTORICAL_DIR.mkdir(parents=True, exist_ok=True)

# =============================================================================
# CONFIGURATION
# =============================================================================

YEARS_TO_FETCH = 4
API_DELAY_SECONDS = 1.0  # 1 second between calls (conservative)
BATCH_SIZE_DAYS = 365    # 1 year per batch (Kite limit is ~2000 candles)


# =============================================================================
# INSTRUMENT FETCHING (Public API - No Auth)
# =============================================================================

def fetch_fo_universe() -> List[Dict]:
    """
    Fetch current F&O stock universe from Kite public API.
    Returns list of stocks with their lot sizes.
    """
    print("Fetching F&O universe from Kite...")

    response = requests.get(INSTRUMENTS_URL, timeout=60)
    response.raise_for_status()

    # Parse CSV
    reader = csv.DictReader(StringIO(response.text))
    instruments = list(reader)
    print(f"  Total instruments: {len(instruments)}")

    # Find NSE stocks
    nse_stocks = {}
    for inst in instruments:
        if inst['exchange'] == 'NSE' and inst['segment'] == 'NSE':
            nse_stocks[inst['tradingsymbol']] = {
                'instrument_token': int(inst['instrument_token']),
                'name': inst['name']
            }

    # Find stocks with F&O (have NFO options)
    fo_stocks = set()
    lot_sizes = {}
    for inst in instruments:
        if inst['exchange'] == 'NFO' and inst['instrument_type'] in ('CE', 'PE'):
            underlying = inst['name']
            if underlying in nse_stocks:
                fo_stocks.add(underlying)
                # Get lot size from options
                if underlying not in lot_sizes and inst.get('lot_size'):
                    lot_sizes[underlying] = int(inst['lot_size'])

    # Build result
    result = []
    for symbol in sorted(fo_stocks):
        if symbol in nse_stocks:
            result.append({
                'symbol': symbol,
                'instrument_token': nse_stocks[symbol]['instrument_token'],
                'lot_size': lot_sizes.get(symbol, 1)
            })

    print(f"  F&O stocks found: {len(result)}")
    return result


# =============================================================================
# KITE CONNECTION
# =============================================================================

def get_kite() -> KiteConnect:
    """Get authenticated Kite connection."""
    if not TOKEN_FILE.exists():
        raise FileNotFoundError(
            f"Token file not found: {TOKEN_FILE}\n"
            "Run: python scripts/kite_auth.py first"
        )

    with open(TOKEN_FILE) as f:
        token_data = json.load(f)

    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


# =============================================================================
# HISTORICAL DATA FETCHING
# =============================================================================

def fetch_historical_data(
    kite: KiteConnect,
    instrument_token: int,
    symbol: str,
    years: int = 4
) -> Optional[List[Dict]]:
    """
    Fetch historical OHLCV data in yearly batches.
    Returns list of candle dictionaries.
    """
    all_data = []
    to_date = datetime.now()

    for year in range(years):
        # Calculate date range for this batch
        batch_end = to_date - timedelta(days=year * 365)
        batch_start = batch_end - timedelta(days=365)

        # Don't go before 2020
        if batch_start.year < 2020:
            batch_start = datetime(2020, 1, 1)

        try:
            data = kite.historical_data(
                instrument_token=instrument_token,
                from_date=batch_start,
                to_date=batch_end,
                interval='day'
            )

            if data:
                all_data.extend(data)

            # Rate limiting
            time.sleep(API_DELAY_SECONDS)

        except Exception as e:
            print(f"    Error fetching year {year + 1}: {e}")
            time.sleep(API_DELAY_SECONDS)
            continue

    # Remove duplicates and sort by date
    if all_data:
        seen = set()
        unique_data = []
        for candle in all_data:
            date_key = candle['date'].strftime('%Y-%m-%d')
            if date_key not in seen:
                seen.add(date_key)
                unique_data.append(candle)

        unique_data.sort(key=lambda x: x['date'])
        return unique_data

    return None


def save_to_csv(data: List[Dict], symbol: str) -> Path:
    """Save historical data to CSV."""
    csv_path = HISTORICAL_DIR / f"{symbol}_daily.csv"

    with open(csv_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'date', 'open', 'high', 'low', 'close', 'volume'
        ])
        writer.writeheader()

        for candle in data:
            writer.writerow({
                'date': candle['date'].strftime('%Y-%m-%d'),
                'open': candle['open'],
                'high': candle['high'],
                'low': candle['low'],
                'close': candle['close'],
                'volume': candle['volume']
            })

    return csv_path


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 70)
    print("STEP 0a: BUILD HISTORICAL DATA")
    print("=" * 70)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Years to fetch: {YEARS_TO_FETCH}")
    print(f"API delay: {API_DELAY_SECONDS}s between calls")
    print(f"Output: {HISTORICAL_DIR}")
    print("=" * 70)
    print()

    # Step 1: Get F&O universe
    fo_stocks = fetch_fo_universe()

    if not fo_stocks:
        print("ERROR: No F&O stocks found!")
        return 1

    # Save F&O universe for reference
    universe_file = BOUNCER_DIR / 'data' / 'fo_universe.json'
    with open(universe_file, 'w') as f:
        json.dump({
            'generated_at': datetime.now().isoformat(),
            'count': len(fo_stocks),
            'stocks': fo_stocks
        }, f, indent=2)
    print(f"\nF&O universe saved: {universe_file}")

    # Step 2: Connect to Kite
    print("\nConnecting to Kite...")
    try:
        kite = get_kite()
        # Validate token
        profile = kite.profile()
        print(f"Connected as: {profile.get('user_name', profile.get('user_id'))}")
    except Exception as e:
        print(f"ERROR: Failed to connect to Kite: {e}")
        print("Run: python scripts/kite_auth.py first")
        return 1

    # Step 3: Fetch historical data for each stock
    print("\n" + "-" * 70)
    print("Fetching historical data...")
    print("-" * 70)

    success_count = 0
    error_count = 0
    total = len(fo_stocks)
    start_time = time.time()

    for i, stock in enumerate(fo_stocks, 1):
        symbol = stock['symbol']
        token = stock['instrument_token']

        # Progress indicator
        elapsed = time.time() - start_time
        if i > 1:
            avg_time = elapsed / (i - 1)
            remaining = avg_time * (total - i + 1)
            eta = f"ETA: {remaining / 60:.1f} min"
        else:
            eta = ""

        print(f"[{i}/{total}] {symbol:15} ", end='', flush=True)

        try:
            data = fetch_historical_data(kite, token, symbol, YEARS_TO_FETCH)

            if data and len(data) > 100:  # At least ~100 days of data
                csv_path = save_to_csv(data, symbol)
                candle_count = len(data)
                date_range = f"{data[0]['date'].strftime('%Y-%m-%d')} to {data[-1]['date'].strftime('%Y-%m-%d')}"
                print(f"OK - {candle_count} candles ({date_range}) {eta}")
                success_count += 1
            else:
                print(f"SKIP - Insufficient data ({len(data) if data else 0} candles)")
                error_count += 1

        except Exception as e:
            print(f"ERROR - {str(e)[:50]}")
            error_count += 1
            time.sleep(API_DELAY_SECONDS)

    # Summary
    elapsed = time.time() - start_time
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Total stocks: {total}")
    print(f"Success: {success_count}")
    print(f"Errors/Skipped: {error_count}")
    print(f"Time taken: {elapsed / 60:.1f} minutes")
    print(f"Output directory: {HISTORICAL_DIR}")
    print()

    # List created files
    csv_files = list(HISTORICAL_DIR.glob("*_daily.csv"))
    print(f"CSV files created: {len(csv_files)}")

    if csv_files:
        total_size = sum(f.stat().st_size for f in csv_files)
        print(f"Total size: {total_size / (1024*1024):.1f} MB")

    print("\nDONE - Ready for reliability analysis")
    return 0


if __name__ == "__main__":
    sys.exit(main())
