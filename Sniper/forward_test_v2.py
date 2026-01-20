#!/usr/bin/env python3
"""
Forward Test V2: Swing Support Strategy
- Tests ATM ± 200 strikes (5 strikes total)
- Jan 1-9, 2026
- Looks for levels that were touched and tracks outcome
"""

import json
from pathlib import Path
from datetime import datetime, timedelta, date, time
from typing import Dict, List, Optional
from kiteconnect import KiteConnect

SCRIPT_DIR = Path(__file__).parent.resolve()
BOTS_DIR = SCRIPT_DIR.parent
TOKEN_FILE = BOTS_DIR / 'data' / 'kite_access_token.json'

SWING_LOOKBACK = 5
MIN_SWING_LOWS = 2
OPTION_TOLERANCE = 5


def get_kite():
    with open(TOKEN_FILE) as f:
        token_data = json.load(f)
    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


def find_swing_lows(data, lookback=SWING_LOOKBACK):
    if len(data) < (lookback * 2 + 1):
        return []
    swing_lows = []
    for i in range(lookback, len(data) - lookback):
        current_low = data[i]['low']
        left_lows = [data[j]['low'] for j in range(i - lookback, i)]
        right_lows = [data[j]['low'] for j in range(i + 1, i + lookback + 1)]
        if current_low < min(left_lows) and current_low < min(right_lows):
            swing_lows.append({'price': current_low, 'date': data[i]['date']})
    return swing_lows


def find_support_levels(data, tolerance, current_ltp=None):
    swing_lows = find_swing_lows(data)
    if len(swing_lows) < MIN_SWING_LOWS:
        return []

    swing_lows.sort(key=lambda x: x['price'])
    levels = []
    current_group = [swing_lows[0]]

    for sl in swing_lows[1:]:
        group_low = min(s['price'] for s in current_group)
        if sl['price'] - group_low <= tolerance:
            current_group.append(sl)
        else:
            if len(current_group) >= MIN_SWING_LOWS:
                levels.append(current_group)
            current_group = [sl]

    if len(current_group) >= MIN_SWING_LOWS:
        levels.append(current_group)

    support_levels = []
    for group in levels:
        prices = [s['price'] for s in group]
        level = {
            'price': round(sum(prices) / len(prices), 1),
            'low': min(prices),
            'high': max(prices),
            'touches': len(group),
        }
        # Only levels below current LTP
        if current_ltp is None or level['high'] < current_ltp:
            support_levels.append(level)

    return support_levels


def run_test():
    print("=" * 70)
    print("FORWARD TEST V2: Multiple Strikes")
    print("Period: Jan 1-9, 2026 | TF: 1H")
    print("=" * 70)

    kite = get_kite()

    # Get NIFTY Jan 27 instruments
    instruments = {}
    for inst in kite.instruments('NFO'):
        if inst['name'] == 'NIFTY' and inst['instrument_type'] in ['CE', 'PE']:
            if inst['expiry'] == date(2026, 1, 27):
                instruments[(inst['strike'], inst['instrument_type'])] = {
                    'token': inst['instrument_token'],
                    'symbol': inst['tradingsymbol']
                }

    test_dates = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 5),
                  date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8), date(2026, 1, 9)]

    all_results = []

    for test_date in test_dates:
        print(f"\n{'='*70}")
        print(f"DATE: {test_date}")
        print("=" * 70)

        # Get spot at open
        spot_data = kite.historical_data(
            instrument_token=256265,
            from_date=datetime.combine(test_date, time(9, 15)),
            to_date=datetime.combine(test_date, time(9, 30)),
            interval='minute'
        )
        if not spot_data:
            continue

        spot_open = spot_data[0]['open']
        atm = int(round(spot_open / 100) * 100)

        # Test strikes: ATM, ATM±100, ATM±200
        test_strikes = [atm - 200, atm - 100, atm, atm + 100, atm + 200]

        print(f"Spot: {spot_open:.0f}, ATM: {atm}")
        print(f"Testing strikes: {test_strikes}")

        for strike in test_strikes:
            for opt_type in ['CE', 'PE']:
                key = (strike, opt_type)
                if key not in instruments:
                    continue

                inst = instruments[key]
                symbol = inst['symbol']

                try:
                    # Get option data at market open
                    opt_open_data = kite.historical_data(
                        instrument_token=inst['token'],
                        from_date=datetime.combine(test_date, time(9, 15)),
                        to_date=datetime.combine(test_date, time(9, 30)),
                        interval='minute'
                    )
                    if not opt_open_data:
                        continue

                    opt_ltp = opt_open_data[0]['open']

                    # Skip very low premium options
                    if opt_ltp < 20:
                        continue

                    # Get historical data UP TO this date for level detection
                    hist_data = kite.historical_data(
                        instrument_token=inst['token'],
                        from_date=test_date - timedelta(days=60),
                        to_date=datetime.combine(test_date, time(9, 15)),
                        interval='60minute'
                    )

                    if len(hist_data) < 20:
                        continue

                    # Find support levels BELOW current LTP
                    levels = find_support_levels(hist_data, OPTION_TOLERANCE, opt_ltp)

                    if not levels:
                        continue

                    # Get intraday data to see what happened
                    intraday = kite.historical_data(
                        instrument_token=inst['token'],
                        from_date=datetime.combine(test_date, time(9, 15)),
                        to_date=datetime.combine(test_date, time(15, 30)),
                        interval='5minute'
                    )

                    if not intraday:
                        continue

                    day_low = min(c['low'] for c in intraday)
                    day_high = max(c['high'] for c in intraday)
                    day_close = intraday[-1]['close']

                    # Check each level
                    for level in levels:
                        distance = opt_ltp - level['high']

                        # Skip levels too far away (>30% of LTP)
                        if distance > opt_ltp * 0.3:
                            continue

                        # Did price reach the level?
                        if day_low <= level['high'] + 5:  # Within 5 pts of level
                            touched = day_low <= level['high']
                            broke = day_low < level['low'] - 5

                            # Check bounce after touch
                            bounce_pct = 0
                            held = False

                            if touched and not broke:
                                # Find the candle that touched
                                for i, c in enumerate(intraday):
                                    if c['low'] <= level['high']:
                                        # Check max price after touch
                                        if i + 1 < len(intraday):
                                            max_after = max(cc['high'] for cc in intraday[i+1:])
                                            bounce_pct = ((max_after - level['high']) / level['high']) * 100
                                            if max_after > level['high'] + 10:
                                                held = True
                                        break

                            result = {
                                'date': test_date,
                                'symbol': symbol,
                                'strike': strike,
                                'type': opt_type,
                                'ltp_open': opt_ltp,
                                'level': f"{level['low']:.0f}-{level['high']:.0f}",
                                'touches': level['touches'],
                                'day_low': day_low,
                                'touched': touched,
                                'broke': broke,
                                'held': held,
                                'bounce_pct': bounce_pct,
                                'day_close': day_close
                            }
                            all_results.append(result)

                            status = "HELD" if held else ("BROKE" if broke else "TOUCHED")
                            print(f"\n  {symbol} (LTP: {opt_ltp:.0f})")
                            print(f"    Level: {level['low']:.0f}-{level['high']:.0f} ({level['touches']} touches)")
                            print(f"    Day Low: {day_low:.0f} -> {status}", end="")
                            if held:
                                print(f" +{bounce_pct:.1f}%")
                            else:
                                print()

                except Exception as e:
                    pass  # Skip errors silently

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    if not all_results:
        print("No levels were approached")
        return

    total = len(all_results)
    touched = sum(1 for r in all_results if r['touched'])
    held = sum(1 for r in all_results if r['held'])
    broke = sum(1 for r in all_results if r['broke'])

    print(f"\nLevels approached: {total}")
    print(f"Levels touched: {touched}")
    print(f"HELD (bounced): {held}")
    print(f"BROKE: {broke}")

    if touched > 0:
        win_rate = (held / touched) * 100
        print(f"\n*** WIN RATE: {win_rate:.1f}% ({held}/{touched}) ***")

    # By touch count
    print("\nBy Touch Count:")
    for min_t in [2, 3]:
        t_res = [r for r in all_results if r['touches'] >= min_t and r['touched']]
        t_held = sum(1 for r in t_res if r['held'])
        if t_res:
            print(f"  {min_t}+ touches: {t_held}/{len(t_res)} = {(t_held/len(t_res))*100:.0f}%")

    # By option type
    print("\nBy Option Type:")
    for ot in ['CE', 'PE']:
        ot_res = [r for r in all_results if r['type'] == ot and r['touched']]
        ot_held = sum(1 for r in ot_res if r['held'])
        if ot_res:
            print(f"  {ot}: {ot_held}/{len(ot_res)} = {(ot_held/len(ot_res))*100:.0f}%")

    # Individual results
    print("\n" + "-" * 70)
    print("ALL SIGNALS:")
    print("-" * 70)
    for r in all_results:
        status = "HELD" if r['held'] else ("BROKE" if r['broke'] else "TOUCH")
        bounce = f"+{r['bounce_pct']:.0f}%" if r['bounce_pct'] > 5 else ""
        print(f"{r['date']} | {r['symbol'][:22]:22} | Level: {r['level']:10} | {r['touches']}T | {status:5} {bounce}")


if __name__ == '__main__':
    run_test()
