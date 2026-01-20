#!/usr/bin/env python3
"""
Debug script to investigate zone detection issues
"""

import json
import sys
from datetime import datetime, timedelta
from kiteconnect import KiteConnect
from pathlib import Path

# Add parent directory to path
SCRIPT_DIR = Path(__file__).parent.resolve()
BOTS_DIR = SCRIPT_DIR.parent
DATA_DIR = BOTS_DIR / 'data'

# Import from scanner
sys.path.insert(0, str(SCRIPT_DIR))
from scanner import find_reversal_zones, score_zone

def main():
    try:
        # Get token from file
        TOKEN_FILE = DATA_DIR / 'kite_access_token.json'
        with open(TOKEN_FILE) as f:
            token_data = json.load(f)

        api_key = token_data['api_key']
        access_token = token_data['access_token']

        # Initialize Kite
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)

        # Test symbol from alert
        symbol = "NIFTY26JAN25700PE"

        # Get all instruments to find token
        print(f"Looking for {symbol}...")
        instruments = kite.instruments("NFO")

        token = None
        for inst in instruments:
            if inst['tradingsymbol'] == symbol:
                token = inst['instrument_token']
                print(f"Found token: {token}")
                print(f"Instrument: {inst}")
                break

        if not token:
            print(f"ERROR: Symbol {symbol} not found")
            return

        # Get current quote
        quote = kite.quote(f"NFO:{symbol}")
        ltp = quote[f"NFO:{symbol}"]['last_price']
        print(f"\nCurrent LTP: {ltp}")

        # Fetch 1H historical data (90 days)
        print(f"\nFetching 1H historical data (90 days lookback)...")
        data = kite.historical_data(
            instrument_token=token,
            from_date=datetime.now() - timedelta(days=90),
            to_date=datetime.now(),
            interval='60minute'
        )

        print(f"Fetched {len(data)} candles")

        # Show last 5 candles
        print("\nLast 5 candles:")
        for candle in data[-5:]:
            print(f"  {candle['date']}: O={candle['open']:.2f}, H={candle['high']:.2f}, "
                  f"L={candle['low']:.2f}, C={candle['close']:.2f}")

        # Run zone detection
        print("\n" + "="*60)
        print("Running zone detection...")
        print("="*60)

        zones = find_reversal_zones(data, ltp)

        if not zones:
            print("No zones found!")
        else:
            print(f"Found {len(zones)} zones:\n")
            for i, zone in enumerate(zones, 1):
                score = score_zone(zone, ltp)
                print(f"Zone {i}:")
                print(f"  Price Center: {zone['price']}")
                print(f"  Range: {zone['low']:.2f} - {zone['high']:.2f}")
                print(f"  Bounces: {zone['bounces']}")
                print(f"  Strength: {zone['strength']:.2%}")
                print(f"  Last Bounce: {zone['last_bounce']}")
                print(f"  Score: {score:.1f}")
                print(f"  Distance from LTP: {abs(zone['price'] - ltp):.2f} ({abs(zone['price'] - ltp)/ltp*100:.1f}%)")
                print()

        # Detailed bounce analysis
        print("\n" + "="*60)
        print("Detailed bounce analysis:")
        print("="*60)

        bounces_by_level = {}
        for candle in data:
            open_price, high, low, close = candle['open'], candle['high'], candle['low'], candle['close']

            candle_range = high - low
            if candle_range <= 0:
                continue

            close_position = (close - low) / candle_range
            lower_wick = min(open_price, close) - low
            wick_pct = lower_wick / candle_range

            # Check if qualifies as bounce
            if close_position >= 0.4 or wick_pct >= 0.3:
                rounded = round(low / 10) * 10
                if rounded not in bounces_by_level:
                    bounces_by_level[rounded] = []
                bounces_by_level[rounded].append({
                    'date': candle['date'],
                    'low': low,
                    'close_pos': close_position,
                    'wick_pct': wick_pct
                })

        # Show levels with most bounces
        sorted_levels = sorted(bounces_by_level.items(), key=lambda x: len(x[1]), reverse=True)

        print(f"\nTop 10 levels by bounce count:")
        for level, bounces in sorted_levels[:10]:
            print(f"\nLevel {level}:")
            print(f"  Total bounces: {len(bounces)}")
            print(f"  Actual lows: {[b['low'] for b in bounces[:5]]}")  # Show first 5
            if len(bounces) > 5:
                print(f"  ... and {len(bounces)-5} more")

        # Check the specific zone from alert (155-172)
        print("\n" + "="*60)
        print("Analyzing reported zone (155-172):")
        print("="*60)

        alert_zone_bounces = []
        for candle in data:
            low = candle['low']
            if 155 <= low <= 172:
                candle_range = candle['high'] - candle['low']
                if candle_range > 0:
                    close_position = (candle['close'] - low) / candle_range
                    lower_wick = min(candle['open'], candle['close']) - low
                    wick_pct = lower_wick / candle_range

                    if close_position >= 0.4 or wick_pct >= 0.3:
                        alert_zone_bounces.append({
                            'date': candle['date'],
                            'low': low,
                            'close': candle['close'],
                            'close_pos': close_position,
                            'wick_pct': wick_pct
                        })

        print(f"Found {len(alert_zone_bounces)} bounces in 155-172 range")
        if alert_zone_bounces:
            print("\nBounces:")
            for b in alert_zone_bounces[:10]:  # Show first 10
                print(f"  {b['date']}: Low={b['low']:.2f}, Close={b['close']:.2f}, "
                      f"ClosePos={b['close_pos']:.2%}, WickPct={b['wick_pct']:.2%}")
            if len(alert_zone_bounces) > 10:
                print(f"  ... and {len(alert_zone_bounces)-10} more")

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
