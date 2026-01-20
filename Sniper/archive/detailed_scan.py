#!/usr/bin/env python3
"""
Detailed scan with diagnostics
"""

import json
import sys
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from scanner import get_kite, fetch_instruments, INDICES, calculate_atm, get_monthly_expiry
from scanner import get_historical_data, find_reversal_zones, score_zone, is_zone_broken
from scanner import MIN_SCORE, MIN_BOUNCES, TIMEFRAMES

def detailed_analysis():
    print("="*80)
    print("DETAILED ZONE ANALYSIS - NEW LOGIC")
    print("="*80)

    kite = get_kite()
    instruments = fetch_instruments(kite)

    # Focus on NIFTY for detailed analysis
    index = 'NIFTY'
    config = INDICES[index]

    quote = kite.quote(config['spot'])
    ltp = quote[config['spot']]['last_price']
    atm = calculate_atm(ltp, index)
    expiry = get_monthly_expiry(index, instruments)

    print(f"\n{index}: {ltp:.2f}")
    print(f"ATM Strike: {atm}")
    print(f"Expiry: {expiry.strftime('%d-%b-%Y')}")
    print()

    # Check multiple strikes around ATM
    strikes_to_check = [atm - 200, atm - 100, atm, atm + 100, atm + 200]

    for timeframe in ['15m', '1h']:
        tf_config = TIMEFRAMES[timeframe]
        max_bounce_age = tf_config.get('max_bounce_age', 30)

        print("\n" + "="*80)
        print(f"TIMEFRAME: {timeframe.upper()}")
        print(f"Lookback: {tf_config['lookback_days']} days | Bounce Filter: {max_bounce_age} days")
        print("="*80)

        for strike in strikes_to_check:
            for opt_type in ['CE', 'PE']:
                key = (index, strike, opt_type, expiry)
                if key not in instruments:
                    continue

                inst = instruments[key]
                symbol = inst['symbol']

                try:
                    # Get quote
                    quote_key = f"{inst['exchange']}:{symbol}"
                    quote = kite.quote(quote_key)
                    opt_ltp = quote[quote_key]['last_price']

                    if opt_ltp is None or opt_ltp <= 0:
                        continue

                    # Get historical data
                    data = get_historical_data(kite, inst['token'], timeframe)
                    total_candles = len(data)

                    # Find zones WITHOUT age filter first
                    zones_all = find_reversal_zones(data, opt_ltp, max_bounce_age_days=999)

                    # Find zones WITH age filter (new logic)
                    zones_filtered = find_reversal_zones(data, opt_ltp, max_bounce_age)

                    # Count bounces
                    all_bounces = sum(z['bounces'] for z in zones_all)
                    filtered_bounces = sum(z['bounces'] for z in zones_filtered)

                    print(f"\n{symbol} (LTP: {opt_ltp:.2f})")
                    print(f"  Candles: {total_candles}")
                    print(f"  Without filter: {len(zones_all)} zones, {all_bounces} bounces")
                    print(f"  With {max_bounce_age}d filter: {len(zones_filtered)} zones, {filtered_bounces} bounces")

                    if zones_filtered:
                        for z in zones_filtered:
                            z['score'] = score_zone(z, opt_ltp)

                        zones_filtered = [z for z in zones_filtered if not is_zone_broken(z, opt_ltp)]
                        strong_zones = [z for z in zones_filtered if z['score'] >= MIN_SCORE]

                        print(f"  After scoring/filtering: {len(strong_zones)} strong zones (score >= {MIN_SCORE})")

                        for i, zone in enumerate(strong_zones, 1):
                            days_old = (datetime.now() - zone['last_bounce'].replace(tzinfo=None)).days
                            distance_pct = abs(zone['price'] - opt_ltp) / opt_ltp * 100

                            print(f"    Zone {i}: {zone['low']:.0f}-{zone['high']:.0f} (width: {zone.get('width', 0):.0f}pts)")
                            print(f"      Bounces: {zone['bounces']} | Score: {zone['score']:.1f} | Last: {days_old}d ago | Dist: {distance_pct:.1f}%")

                    elif zones_all:
                        print(f"  >> All zones filtered out by {max_bounce_age}d age limit!")

                    elif all_bounces > 0 and all_bounces < MIN_BOUNCES:
                        print(f"  >> Not enough bounces (need {MIN_BOUNCES}, found {all_bounces})")

                    else:
                        print(f"  >> No reversal patterns found")

                except Exception as e:
                    print(f"\n{symbol} ERROR: {str(e)[:100]}")

    print("\n" + "="*80)
    print("ANALYSIS COMPLETE")
    print("="*80)

if __name__ == "__main__":
    try:
        detailed_analysis()
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
