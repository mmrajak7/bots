#!/usr/bin/env python3
"""
Support Level Finder

Finds horizontal support levels where price bounced multiple times.
Uses 15-min data.

Usage:
    python support_finder.py BANKNIFTY 60000 CE
    python support_finder.py BANKNIFTY 60000 PE
"""

import json
import csv
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter
from kiteconnect import KiteConnect

# =============================================================================
# PATHS
# =============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
HELPER_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == 'helper' else SCRIPT_DIR
BOTS_DIR = HELPER_DIR.parent
DATA_DIR = BOTS_DIR / 'data'
TOKEN_FILE = DATA_DIR / 'kite_access_token.json'
INDEX_OPTIONS_CSV = DATA_DIR / 'index_options.csv'


def get_kite():
    with open(TOKEN_FILE) as f:
        token_data = json.load(f)
    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


def find_instrument(index_name, strike, option_type, expiry_month='JAN'):
    with open(INDEX_OPTIONS_CSV, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row['index'] == index_name and
                float(row['strike']) == strike and
                row['option_type'] == option_type and
                expiry_month in row['option_symbol']):
                return {
                    'symbol': row['option_symbol'],
                    'token': int(row['option_token']),
                    'expiry': row['expiry']
                }
    return None


def get_15min_data(kite, token, days=30):
    data = kite.historical_data(
        instrument_token=token,
        from_date=datetime.now() - timedelta(days=days),
        to_date=datetime.now(),
        interval='15minute'
    )
    return data


def find_support_levels(data, min_touches=8):
    """
    Find support levels by counting candle lows at each price zone.

    Method:
    1. Round lows to nearest 10
    2. Merge adjacent levels (520+530 = 525 zone)
    3. Return levels with sufficient touches
    """
    round_to = 10

    # Count touches at each rounded level
    level_touches = Counter()
    for candle in data:
        rounded = round(candle['low'] / round_to) * round_to
        level_touches[rounded] += 1

    # Merge adjacent levels
    sorted_levels = sorted(level_touches.keys())
    zones = []
    used = set()

    for level in sorted_levels:
        if level in used:
            continue

        zone_levels = [level]
        next_level = level + round_to

        if next_level in level_touches:
            zone_levels.append(next_level)
            used.add(next_level)

        total_touches = sum(level_touches[l] for l in zone_levels)
        center = sum(zone_levels) / len(zone_levels)

        if total_touches >= min_touches:
            zones.append({
                'price': int(center),
                'touches': total_touches
            })

        used.add(level)

    # Sort by price (for clean display)
    zones.sort(key=lambda x: x['price'])

    return zones


def main():
    parser = argparse.ArgumentParser(description='Support Level Finder')
    parser.add_argument('index', help='BANKNIFTY, NIFTY')
    parser.add_argument('strike', type=int, help='Strike price')
    parser.add_argument('option_type', choices=['CE', 'PE'], help='CE or PE')
    parser.add_argument('--days', type=int, default=30, help='Lookback days')
    parser.add_argument('--min-touches', type=int, default=10, help='Minimum touches')

    args = parser.parse_args()

    # Find instrument
    inst = find_instrument(args.index, args.strike, args.option_type)
    if not inst:
        print(f"ERROR: {args.index} {args.strike} {args.option_type} not found")
        return 1

    # Connect and get data
    kite = get_kite()
    data = get_15min_data(kite, inst['token'], args.days)

    # Get LTP
    quote = kite.quote(f"NFO:{inst['symbol']}")
    ltp = quote[f"NFO:{inst['symbol']}"]['last_price']

    print()
    print(f"{inst['symbol']}")
    print(f"LTP: {ltp:.0f}")
    print(f"Data: {len(data)} candles ({args.days} days)")
    print()

    # Find support levels
    levels = find_support_levels(data, args.min_touches)

    if not levels:
        print("No support levels found.")
        return 0

    # Display
    print("SUPPORT LEVELS")
    print("-" * 40)
    print(f"{'LEVEL':>8} {'TOUCHES':>10}  {'STATUS':<15}")
    print("-" * 40)

    for lvl in levels:
        price = lvl['price']
        touches = lvl['touches']

        # Determine status relative to LTP
        diff = price - ltp

        if abs(diff) < 15:
            status = "<-- AT LTP"
        elif diff < 0:
            pct = abs(diff) / ltp * 100
            status = f"BUY ({pct:.0f}% below)"
        else:
            pct = diff / ltp * 100
            status = f"above ({pct:.0f}%)"

        print(f"{price:>8} {touches:>10}  {status:<15}")

    # Summary
    print()
    print("=" * 40)
    buy_levels = [l for l in levels if l['price'] < ltp]
    if buy_levels:
        print("BUY ZONES (support below LTP):")
        for lvl in buy_levels[-3:]:  # Closest 3
            print(f"  {lvl['price']} ({lvl['touches']} touches)")
    else:
        print("No buy zones below current price")

    return 0


if __name__ == "__main__":
    exit(main())
