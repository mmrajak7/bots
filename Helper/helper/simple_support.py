#!/usr/bin/env python3
"""
Simple Support Level Finder

Finds horizontal support levels where price has bounced multiple times.
Uses 15-min data only.

Usage:
    python simple_support.py BANKNIFTY 60000 CE
    python simple_support.py BANKNIFTY 60000 PE
"""

import json
import csv
import argparse
import pandas as pd
import numpy as np
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

# =============================================================================
# CONFIG
# =============================================================================

# Zone tolerance - candle lows within this % are considered same level
ZONE_TOLERANCE_PCT = 1.5

# Minimum touches to be a valid support
MIN_TOUCHES = 2

# How far back to look (days)
LOOKBACK_DAYS = 30


# =============================================================================
# KITE CONNECTION
# =============================================================================

def get_kite():
    with open(TOKEN_FILE) as f:
        token_data = json.load(f)
    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


def find_instrument(index_name, strike, option_type):
    """Find option from CSV."""
    with open(INDEX_OPTIONS_CSV, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row['index'] == index_name and
                float(row['strike']) == strike and
                row['option_type'] == option_type and
                'JAN' in row['option_symbol']):
                return {
                    'symbol': row['option_symbol'],
                    'token': int(row['option_token']),
                    'expiry': row['expiry']
                }
    return None


def get_15min_data(kite, token, days=30):
    """Fetch 15-min OHLCV data."""
    to_date = datetime.now()
    from_date = to_date - timedelta(days=days)

    data = kite.historical_data(
        instrument_token=token,
        from_date=from_date,
        to_date=to_date,
        interval='15minute'
    )

    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['date'])
    df.index = df['date']
    return df


# =============================================================================
# SUPPORT DETECTION - SIMPLE APPROACH
# =============================================================================

def find_support_levels(df, tolerance_pct=1.5, min_touches=2):
    """
    Find horizontal support levels.

    Approach:
    1. Look at all candle lows
    2. Group lows that are within tolerance % of each other
    3. Count touches per group
    4. Return levels with enough touches
    """

    # Get all lows
    lows = df['low'].values
    dates = df.index.values

    # Sort lows to find clusters
    low_data = sorted(zip(lows, dates), key=lambda x: x[0])

    # Cluster nearby lows
    clusters = []
    current_cluster = [low_data[0]]

    for i in range(1, len(low_data)):
        low_price, low_date = low_data[i]
        cluster_avg = np.mean([x[0] for x in current_cluster])

        # If within tolerance, add to cluster
        if abs(low_price - cluster_avg) / cluster_avg * 100 <= tolerance_pct:
            current_cluster.append((low_price, low_date))
        else:
            # Save cluster if has enough touches
            if len(current_cluster) >= min_touches:
                clusters.append(current_cluster)
            current_cluster = [(low_price, low_date)]

    # Don't forget last cluster
    if len(current_cluster) >= min_touches:
        clusters.append(current_cluster)

    # Convert clusters to support levels
    support_levels = []
    for cluster in clusters:
        prices = [x[0] for x in cluster]
        dates = [x[1] for x in cluster]

        support_levels.append({
            'price': round(np.mean(prices), 2),
            'touches': len(cluster),
            'low_range': (round(min(prices), 2), round(max(prices), 2)),
            'first_touch': pd.Timestamp(min(dates)).strftime('%Y-%m-%d %H:%M'),
            'last_touch': pd.Timestamp(max(dates)).strftime('%Y-%m-%d %H:%M'),
        })

    # Sort by number of touches (most touched first)
    support_levels.sort(key=lambda x: x['touches'], reverse=True)

    return support_levels


def find_support_by_bounce(df, tolerance_pct=2.0, min_touches=2):
    """
    Alternative: Find levels where price bounced UP after touching.

    A bounce = low followed by higher close
    """
    bounces = []

    for i in range(len(df)):
        row = df.iloc[i]

        # Check if candle bounced (close > open, or close in upper half)
        candle_range = row['high'] - row['low']
        if candle_range > 0:
            close_position = (row['close'] - row['low']) / candle_range

            # Bounce = close in upper 50% of candle
            if close_position > 0.5:
                bounces.append({
                    'low': row['low'],
                    'date': df.index[i],
                    'close': row['close'],
                    'bounce_strength': close_position
                })

    if not bounces:
        return []

    # Cluster bounces into support zones
    bounces.sort(key=lambda x: x['low'])

    clusters = []
    current = [bounces[0]]

    for b in bounces[1:]:
        cluster_avg = np.mean([x['low'] for x in current])

        if abs(b['low'] - cluster_avg) / cluster_avg * 100 <= tolerance_pct:
            current.append(b)
        else:
            if len(current) >= min_touches:
                clusters.append(current)
            current = [b]

    if len(current) >= min_touches:
        clusters.append(current)

    # Build support levels
    levels = []
    for cluster in clusters:
        lows = [x['low'] for x in cluster]
        dates = [x['date'] for x in cluster]

        levels.append({
            'price': round(np.mean(lows), 2),
            'touches': len(cluster),
            'zone': f"{round(min(lows), 2)} - {round(max(lows), 2)}",
            'first': pd.Timestamp(min(dates)).strftime('%Y-%m-%d'),
            'last': pd.Timestamp(max(dates)).strftime('%Y-%m-%d'),
            'avg_bounce': round(np.mean([x['bounce_strength'] for x in cluster]) * 100, 1)
        })

    levels.sort(key=lambda x: x['touches'], reverse=True)
    return levels


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='Simple Support Level Finder')
    parser.add_argument('index', help='BANKNIFTY, NIFTY')
    parser.add_argument('strike', type=int, help='Strike price')
    parser.add_argument('option_type', choices=['CE', 'PE'], help='CE or PE')
    parser.add_argument('--days', type=int, default=30, help='Lookback days (default: 30)')
    parser.add_argument('--tolerance', type=float, default=1.5, help='Zone tolerance %% (default: 1.5)')

    args = parser.parse_args()

    print("=" * 60)
    print("SIMPLE SUPPORT LEVEL FINDER")
    print("=" * 60)

    # Find instrument
    inst = find_instrument(args.index, args.strike, args.option_type)
    if not inst:
        print(f"ERROR: {args.index} {args.strike} {args.option_type} not found")
        return 1

    print(f"Symbol: {inst['symbol']}")
    print(f"Expiry: {inst['expiry']}")

    # Connect and get data
    print("\nFetching 15-min data...")
    kite = get_kite()
    df = get_15min_data(kite, inst['token'], args.days)
    print(f"Candles: {len(df)}")

    # Get current price
    quote = kite.quote(f"NFO:{inst['symbol']}")
    ltp = quote[f"NFO:{inst['symbol']}"]['last_price']
    print(f"Current LTP: {ltp}")

    # Find support levels
    print(f"\nFinding support levels (tolerance: {args.tolerance}%)...")
    print("-" * 60)

    levels = find_support_levels(df, args.tolerance, MIN_TOUCHES)

    if not levels:
        print("No support levels found.")
        return 0

    print(f"\n{'PRICE':>10} {'TOUCHES':>8} {'ZONE':>20} {'LAST TOUCH':>18}")
    print("-" * 60)

    for lvl in levels[:10]:  # Top 10
        zone = f"{lvl['low_range'][0]} - {lvl['low_range'][1]}"
        dist = abs(lvl['price'] - ltp) / ltp * 100

        # Mark levels near current price
        marker = ""
        if dist < 2:
            marker = " <-- NEAR LTP"
        elif dist < 5:
            marker = " <-- CLOSE"

        print(f"{lvl['price']:>10.2f} {lvl['touches']:>8} {zone:>20} {lvl['last_touch']:>18}{marker}")

    # Also show bounce-based analysis
    print("\n" + "=" * 60)
    print("BOUNCE-CONFIRMED SUPPORT (candles that bounced up)")
    print("=" * 60)

    bounce_levels = find_support_by_bounce(df, args.tolerance, MIN_TOUCHES)

    if bounce_levels:
        print(f"\n{'PRICE':>10} {'BOUNCES':>8} {'ZONE':>20} {'LAST':>12} {'STRENGTH':>10}")
        print("-" * 60)

        for lvl in bounce_levels[:10]:
            dist = abs(lvl['price'] - ltp) / ltp * 100
            marker = ""
            if dist < 2:
                marker = " <--"

            print(f"{lvl['price']:>10.2f} {lvl['touches']:>8} {lvl['zone']:>20} {lvl['last']:>12} {lvl['avg_bounce']:>9.1f}%{marker}")

    print("\n" + "=" * 60)
    print("BUY ZONES (Support levels BELOW current price)")
    print("=" * 60)

    buy_zones = [l for l in levels if l['price'] < ltp]
    if buy_zones:
        for lvl in buy_zones[:5]:
            dist = (ltp - lvl['price']) / ltp * 100
            print(f"  {lvl['price']:.2f}  ({lvl['touches']} touches, {dist:.1f}% below LTP)")
    else:
        print("  No support levels below current price")

    return 0


if __name__ == "__main__":
    exit(main())
