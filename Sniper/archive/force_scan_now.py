#!/usr/bin/env python3
"""
Force a full scan to show current zones with new logic
"""

import json
import sys
from pathlib import Path
from datetime import datetime

# Add current dir to path
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

# Import scanner functions
from scanner import get_kite, fetch_instruments, INDICES, calculate_atm, get_monthly_expiry
from scanner import get_historical_data, find_reversal_zones, score_zone, is_zone_broken
from scanner import MIN_SCORE, TIMEFRAMES

def show_zones():
    """Run full zone detection and display results"""

    print("="*80)
    print("FORCED FULL SCAN - NEW ZONE DETECTION LOGIC")
    print("="*80)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # Initialize Kite
    kite = get_kite()
    instruments = fetch_instruments(kite)

    # Get index LTPs
    index_ltps = {}
    for index, config in INDICES.items():
        try:
            quote = kite.quote(config['spot'])
            ltp = quote[config['spot']]['last_price']
            index_ltps[index] = ltp
            print(f"{index}: {ltp:.2f}")
        except Exception as e:
            print(f"{index} failed: {e}")

    print()
    print("="*80)

    # Scan all timeframes
    for timeframe, tf_config in TIMEFRAMES.items():
        if not tf_config['enabled']:
            continue

        print(f"\n{'='*80}")
        print(f"TIMEFRAME: {timeframe.upper()}")
        print(f"Lookback: {tf_config['lookback_days']} days | Bounce Age Filter: {tf_config['max_bounce_age']} days")
        print(f"{'='*80}")

        zones_found = 0

        for index, ltp in index_ltps.items():
            atm = calculate_atm(ltp, index)
            expiry = get_monthly_expiry(index, instruments)

            if not expiry:
                continue

            print(f"\n--- {index} (ATM: {atm}, Expiry: {expiry.strftime('%d-%b')}) ---")

            for opt_type in ['CE', 'PE']:
                key = (index, atm, opt_type, expiry)
                if key not in instruments:
                    continue

                inst = instruments[key]
                symbol = inst['symbol']

                try:
                    # Get option quote
                    quote_key = f"{inst['exchange']}:{symbol}"
                    quote = kite.quote(quote_key)
                    opt_ltp = quote[quote_key]['last_price']

                    if opt_ltp is None or opt_ltp <= 0:
                        continue

                    # Fetch historical data
                    data = get_historical_data(kite, inst['token'], timeframe)

                    # Get bounce age filter for this timeframe
                    max_bounce_age = tf_config.get('max_bounce_age', 30)

                    # Find zones with NEW logic
                    zones = find_reversal_zones(data, opt_ltp, max_bounce_age)

                    if not zones:
                        continue

                    # Score and filter zones
                    for z in zones:
                        z['score'] = score_zone(z, opt_ltp)

                    # Remove broken zones
                    zones = [z for z in zones if not is_zone_broken(z, opt_ltp)]

                    # Keep only strong zones
                    zones = [z for z in zones if z['score'] >= MIN_SCORE]

                    # Sort by score
                    zones.sort(key=lambda x: x['score'], reverse=True)

                    if zones:
                        zones_found += len(zones)
                        print(f"\n  {symbol} (LTP: {opt_ltp:.2f}) - {len(zones)} zone(s):")

                        for i, zone in enumerate(zones, 1):
                            days_old = (datetime.now() - zone['last_bounce'].replace(tzinfo=None)).days
                            distance_pct = abs(zone['price'] - opt_ltp) / opt_ltp * 100

                            print(f"    Zone {i}: {zone['low']:.0f}-{zone['high']:.0f} (width: {zone.get('width', 0):.0f}pts)")
                            print(f"      Center: {zone['price']} | Bounces: {zone['bounces']} | Strength: {zone['strength']:.0%}")
                            print(f"      Score: {zone['score']:.1f} | Last bounce: {days_old}d ago | Distance: {distance_pct:.1f}%")

                except Exception as e:
                    print(f"  {symbol} failed: {str(e)[:80]}")

        print(f"\n{'='*80}")
        print(f"Total zones found in {timeframe}: {zones_found}")
        print(f"{'='*80}")

    print("\n" + "="*80)
    print("SCAN COMPLETE")
    print("="*80)
    print("\nKey Improvements with NEW Logic:")
    print("  ✓ Zones are narrower (MAX_ZONE_WIDTH = 10 points)")
    print("  ✓ Only recent bounces count (15m: 12 days, 1h: 20 days)")
    print("  ✓ No over-merged fake zones (e.g., 155-172 split into separate levels)")
    print("  ✓ More relevant for current market conditions")
    print("="*80)

if __name__ == "__main__":
    try:
        show_zones()
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
