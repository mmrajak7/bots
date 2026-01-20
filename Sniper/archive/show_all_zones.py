#!/usr/bin/env python3
"""
Show ALL zones regardless of score to see what we're filtering out
"""

import sys
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from scanner import get_kite, fetch_instruments, INDICES, calculate_atm, get_monthly_expiry
from scanner import get_historical_data, find_reversal_zones, score_zone, is_zone_broken

def show_all_zones():
    print("="*80)
    print("ALL ZONES WITH SCORES (No score filter)")
    print("="*80)

    kite = get_kite()
    instruments = fetch_instruments(kite)

    # Focus on NIFTY 25700 PE (from your original alert)
    index = 'NIFTY'
    config = INDICES[index]

    quote = kite.quote(config['spot'])
    ltp = quote[config['spot']]['last_price']
    atm = calculate_atm(ltp, index)
    expiry = get_monthly_expiry(index, instruments)

    print(f"\nAnalyzing: NIFTY26JAN25700PE")
    print(f"Current NIFTY: {ltp:.2f}")
    print()

    # Check the specific option
    key = (index, 25700, 'PE', expiry)
    if key not in instruments:
        print("Option not found!")
        return

    inst = instruments[key]
    symbol = inst['symbol']

    quote_key = f"{inst['exchange']}:{symbol}"
    quote = kite.quote(quote_key)
    opt_ltp = quote[quote_key]['last_price']

    print(f"Option LTP: {opt_ltp:.2f}")
    print()

    for timeframe in ['15m', '1h']:
        from scanner import TIMEFRAMES
        tf_config = TIMEFRAMES[timeframe]
        max_bounce_age = tf_config.get('max_bounce_age', 30)

        print("="*80)
        print(f"TIMEFRAME: {timeframe.upper()} (Bounce filter: {max_bounce_age} days)")
        print("="*80)

        data = get_historical_data(kite, inst['token'], timeframe)
        zones = find_reversal_zones(data, opt_ltp, max_bounce_age)

        if not zones:
            print("No zones found!")
            continue

        # Score all zones
        for z in zones:
            z['score'] = score_zone(z, opt_ltp)
            z['broken'] = is_zone_broken(z, opt_ltp)

        # Sort by score
        zones.sort(key=lambda x: x['score'], reverse=True)

        print(f"Found {len(zones)} zones:\n")

        for i, zone in enumerate(zones[:10], 1):  # Show top 10
            days_old = (datetime.now() - zone['last_bounce'].replace(tzinfo=None)).days
            distance = abs(zone['price'] - opt_ltp)
            distance_pct = distance / opt_ltp * 100
            broken_status = "BROKEN" if zone['broken'] else "VALID"

            print(f"Zone {i}: [{broken_status}] Score: {zone['score']:.1f}")
            print(f"  Range: {zone['low']:.0f}-{zone['high']:.0f} (width: {zone.get('width', 0):.0f}pts, center: {zone['price']})")
            print(f"  Bounces: {zone['bounces']} | Strength: {zone['strength']:.0%}")
            print(f"  Last bounce: {days_old} days ago")
            print(f"  Distance from LTP ({opt_ltp:.0f}): {distance:.0f} points ({distance_pct:.1f}%)")
            print()

        if len(zones) > 10:
            print(f"... and {len(zones)-10} more zones with lower scores")

    print("="*80)
    print("SCORE BREAKDOWN EXPLANATION")
    print("="*80)
    print("Score = Bounce Score + Strength Score + Proximity Score + Freshness Score")
    print("  Bounce Score:    max 50 (based on bounce count)")
    print("  Strength Score:  max 20 (based on bounce strength)")
    print("  Proximity Score: max 20 (based on distance from LTP)")
    print("  Freshness Score: max 10 (based on last bounce age)")
    print("\nCurrent MIN_SCORE setting: 50")
    print("="*80)

if __name__ == "__main__":
    try:
        show_all_zones()
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
