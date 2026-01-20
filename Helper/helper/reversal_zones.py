#!/usr/bin/env python3
"""
Reversal Zone Finder

Finds support zones where price BOUNCED (reversed) - not just any low.
A reversal = candle that wicked down to a level but closed significantly higher.

Usage:
    python reversal_zones.py BANKNIFTY 60000 CE
    python reversal_zones.py BANKNIFTY 60000 PE
"""

import json
import csv
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
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


def find_reversal_zones(data, min_bounces=2, buffer_pct=2.0):
    """
    Find zones where price bounced (reversed).

    A BOUNCE is defined as:
    - Candle with significant wick (low is much below close)
    - Close is in upper portion of candle range

    Args:
        data: List of OHLCV candles
        min_bounces: Minimum bounces to be a valid zone
        buffer_pct: Zone buffer as % of price

    Returns:
        List of reversal zones with bounce count
    """

    # Step 1: Find all bounce candles
    bounces = []

    for candle in data:
        o, h, l, c = candle['open'], candle['high'], candle['low'], candle['close']
        candle_range = h - l

        if candle_range <= 0:
            continue

        # Calculate where close is within the candle range (0 = at low, 1 = at high)
        close_position = (c - l) / candle_range

        # Calculate lower wick size relative to candle
        lower_wick = min(o, c) - l
        lower_wick_pct = lower_wick / candle_range if candle_range > 0 else 0

        # BOUNCE criteria:
        # 1. Close in upper 60% of candle (price recovered)
        # 2. OR significant lower wick (>30% of candle) showing rejection
        is_bounce = (close_position >= 0.4) or (lower_wick_pct >= 0.3)

        if is_bounce:
            bounces.append({
                'low': l,
                'close': c,
                'date': candle['date'],
                'strength': close_position,  # How strong the bounce was
                'wick_pct': lower_wick_pct
            })

    if not bounces:
        return []

    # Step 2: Group bounces into zones
    # Round to nearest 10 and merge adjacent
    round_to = 10
    zone_bounces = defaultdict(list)

    for bounce in bounces:
        rounded = round(bounce['low'] / round_to) * round_to
        zone_bounces[rounded].append(bounce)

    # Step 3: Merge adjacent zones
    sorted_levels = sorted(zone_bounces.keys())
    zones = []
    used = set()

    for level in sorted_levels:
        if level in used:
            continue

        # Merge with adjacent level
        zone_levels = [level]
        next_level = level + round_to

        if next_level in zone_bounces:
            zone_levels.append(next_level)
            used.add(next_level)

        # Collect all bounces in this zone
        all_bounces = []
        for l in zone_levels:
            all_bounces.extend(zone_bounces[l])

        if len(all_bounces) >= min_bounces:
            lows = [b['low'] for b in all_bounces]
            avg_strength = sum(b['strength'] for b in all_bounces) / len(all_bounces)

            zones.append({
                'price': int(sum(zone_levels) / len(zone_levels)),
                'bounces': len(all_bounces),
                'zone_low': round(min(lows), 0),
                'zone_high': round(max(lows), 0),
                'avg_strength': round(avg_strength * 100, 0),  # As percentage
                'last_bounce': max(b['date'] for b in all_bounces)
            })

        used.add(level)

    # Sort by bounces (most first)
    zones.sort(key=lambda x: x['bounces'], reverse=True)

    return zones


def main():
    parser = argparse.ArgumentParser(description='Reversal Zone Finder')
    parser.add_argument('index', help='BANKNIFTY, NIFTY')
    parser.add_argument('strike', type=int, help='Strike price')
    parser.add_argument('option_type', choices=['CE', 'PE'], help='CE or PE')
    parser.add_argument('--days', type=int, default=30, help='Lookback days')
    parser.add_argument('--min-bounces', type=int, default=3, help='Minimum bounces')

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

    # Find reversal zones
    zones = find_reversal_zones(data, args.min_bounces)

    if not zones:
        print("No reversal zones found.")
        return 0

    # Display - sorted by price for easy reading
    zones_by_price = sorted(zones, key=lambda x: x['price'])

    print("REVERSAL ZONES (Bounce Support)")
    print("-" * 55)
    print(f"{'ZONE':>8} {'RANGE':>15} {'BOUNCES':>8} {'STRENGTH':>10} {'STATUS':<12}")
    print("-" * 55)

    for zone in zones_by_price:
        price = zone['price']
        range_str = f"{int(zone['zone_low'])}-{int(zone['zone_high'])}"
        bounces = zone['bounces']
        strength = f"{int(zone['avg_strength'])}%"

        # Status relative to LTP
        diff = price - ltp
        if abs(diff) < 20:
            status = "<-- AT LTP"
        elif diff < 0:
            pct = abs(diff) / ltp * 100
            status = f"BUY ({pct:.0f}% below)"
        else:
            pct = diff / ltp * 100
            status = f"above ({pct:.0f}%)"

        # Highlight strong zones
        marker = ""
        if bounces >= 10:
            marker = " ***"
        elif bounces >= 5:
            marker = " **"

        print(f"{price:>8} {range_str:>15} {bounces:>8} {strength:>10} {status:<12}{marker}")

    # BUY ZONES with buffer
    print()
    print("=" * 55)
    print("BUY ZONES WITH BUFFER")
    print("=" * 55)

    # Sort all zones by bounces (strongest first)
    strong_zones = sorted(zones, key=lambda x: x['bounces'], reverse=True)

    # Show top 5 strongest zones
    print()
    print("Top 5 Reversal Zones (by bounce count):")
    print()

    buffer_pct = 2.0  # 2% buffer

    for i, z in enumerate(strong_zones[:5], 1):
        zone_low = z['zone_low']
        zone_high = z['zone_high']

        # Add buffer (2% below zone low)
        buffer = zone_low * buffer_pct / 100
        buy_entry = zone_low - buffer
        stop_loss = buy_entry - buffer  # Another 2% below

        dist_from_ltp = (ltp - z['price']) / ltp * 100 if z['price'] < ltp else -(z['price'] - ltp) / ltp * 100

        status = "ABOVE LTP" if z['price'] > ltp else ("AT LTP" if abs(dist_from_ltp) < 3 else f"{abs(dist_from_ltp):.0f}% below")

        print(f"  {i}. ZONE: {z['price']} ({int(zone_low)} - {int(zone_high)})")
        print(f"     Bounces: {z['bounces']} | Strength: {int(z['avg_strength'])}%")
        print(f"     BUY at: {int(buy_entry)} (with buffer)")
        print(f"     Stop Loss: {int(stop_loss)}")
        print(f"     Status: {status}")
        print()

    # Quick reference for current buy zones (below LTP)
    print("-" * 55)
    print("QUICK BUY LEVELS (zones below LTP):")
    buy_zones = [z for z in strong_zones if z['price'] < ltp]
    if buy_zones:
        for z in buy_zones[:3]:
            buffer = z['zone_low'] * 2 / 100
            entry = int(z['zone_low'] - buffer)
            print(f"  {z['price']}: Buy at {entry}, {z['bounces']} bounces")
    else:
        print("  No zones below current price")

    return 0


if __name__ == "__main__":
    exit(main())
