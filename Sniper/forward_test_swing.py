#!/usr/bin/env python3
"""
Forward Test: Swing Support Strategy
Test Period: Jan 1-9, 2026

For each day:
1. Get spot price at 9:30 AM -> Calculate ATM
2. Find support levels on option premium (using data UP TO that point)
3. Track if price approached the level during the day
4. Record if it bounced (win) or broke through (loss)
"""

import json
import pickle
from pathlib import Path
from datetime import datetime, timedelta, date, time
from typing import Dict, List, Optional, Tuple
from kiteconnect import KiteConnect

# =============================================================================
# PATHS & CONFIG
# =============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
BOTS_DIR = SCRIPT_DIR.parent
DATA_DIR = BOTS_DIR / 'data'
TOKEN_FILE = DATA_DIR / 'kite_access_token.json'

# Parameters (same as scanner)
SWING_LOOKBACK = 5
MIN_SWING_LOWS = 2
OPTION_TOLERANCE = 5
LEVEL_TOLERANCE = 20
ALERT_DISTANCE = 10  # Alert when within 10 pts of support

INDICES = {
    'NIFTY': {
        'spot_token': 256265,
        'round_to': 100,
    },
    'BANKNIFTY': {
        'spot_token': 260105,
        'round_to': 100,
    }
}

# =============================================================================
# KITE CONNECTION
# =============================================================================

def get_kite() -> KiteConnect:
    with open(TOKEN_FILE) as f:
        token_data = json.load(f)
    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


def fetch_instruments(kite: KiteConnect) -> Dict:
    """Fetch instruments."""
    instruments = {}
    for exchange in ['NFO', 'BFO']:
        try:
            for inst in kite.instruments(exchange):
                if inst['instrument_type'] in ['CE', 'PE']:
                    key = (inst['name'], inst['strike'], inst['instrument_type'], inst['expiry'])
                    instruments[key] = {
                        'token': inst['instrument_token'],
                        'symbol': inst['tradingsymbol'],
                        'exchange': exchange
                    }
        except Exception as e:
            print(f"Failed to fetch {exchange}: {e}")
    return instruments


def get_monthly_expiry(index: str, instruments: Dict, as_of_date: date) -> Optional[date]:
    """Get monthly expiry valid as of a specific date."""
    expiries = set()
    for (idx, strike, opt_type, expiry) in instruments.keys():
        if idx == index:
            expiries.add(expiry)

    if not expiries:
        return None

    # Group by month, get last of each month (monthly expiry)
    by_month = {}
    for exp in expiries:
        key = (exp.year, exp.month)
        if key not in by_month:
            by_month[key] = []
        by_month[key].append(exp)

    monthly = sorted([max(exps) for exps in by_month.values()])

    # Find first monthly expiry >= 3 days from as_of_date
    for exp in monthly:
        if (exp - as_of_date).days >= 3:
            return exp

    return monthly[-1] if monthly else None

# =============================================================================
# SWING LOW DETECTION
# =============================================================================

def find_swing_lows(data: List[Dict], lookback: int = SWING_LOOKBACK) -> List[Dict]:
    """Find swing lows - candles whose low < N candles on each side."""
    if len(data) < (lookback * 2 + 1):
        return []

    swing_lows = []
    for i in range(lookback, len(data) - lookback):
        current_low = data[i]['low']
        left_lows = [data[j]['low'] for j in range(i - lookback, i)]
        right_lows = [data[j]['low'] for j in range(i + 1, i + lookback + 1)]

        if current_low < min(left_lows) and current_low < min(right_lows):
            swing_lows.append({
                'price': current_low,
                'date': data[i]['date'],
                'candle': data[i]
            })

    return swing_lows


def find_support_levels(data: List[Dict], tolerance: float, current_ltp: float = None) -> List[Dict]:
    """
    Find support levels by grouping swing lows.
    Only return levels BELOW current LTP (useful supports).
    """
    swing_lows = find_swing_lows(data)

    if len(swing_lows) < MIN_SWING_LOWS:
        return []

    # Sort by price
    swing_lows.sort(key=lambda x: x['price'])

    # Group into levels
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

    # Convert to support level objects
    support_levels = []
    for group in levels:
        prices = [s['price'] for s in group]
        level = {
            'price': round(sum(prices) / len(prices), 1),
            'low': min(prices),
            'high': max(prices),
            'touches': len(group),
            'last_touch': max(s['date'] for s in group),
            'strength': 'strong' if len(group) >= 3 else 'moderate'
        }

        # Only include levels BELOW current LTP (useful for buying at support)
        if current_ltp is None or level['high'] < current_ltp:
            support_levels.append(level)

    return support_levels

# =============================================================================
# FORWARD TEST
# =============================================================================

def test_level_performance(kite: KiteConnect, option_token: int, level: Dict,
                           test_date: date, tf_interval: str) -> Dict:
    """
    Test if a support level held or broke on a given day.

    Returns:
        - 'approached': Did price come within ALERT_DISTANCE of level?
        - 'touched': Did price actually touch the level?
        - 'held': Did price bounce (close above level)?
        - 'broke': Did price break below level?
        - 'bounce_pct': If held, how much did it bounce?
    """
    result = {
        'approached': False,
        'touched': False,
        'held': False,
        'broke': False,
        'bounce_pct': 0,
        'min_price': None,
        'max_after_touch': None
    }

    try:
        # Get intraday data for test date
        if tf_interval == 'day':
            data = kite.historical_data(
                instrument_token=option_token,
                from_date=test_date,
                to_date=test_date + timedelta(days=1),
                interval='day'
            )
        else:
            data = kite.historical_data(
                instrument_token=option_token,
                from_date=datetime.combine(test_date, time(9, 15)),
                to_date=datetime.combine(test_date, time(15, 30)),
                interval='15minute'  # Use 15m for intraday tracking
            )

        if not data:
            return result

        level_high = level['high']
        level_low = level['low']

        # Track price action
        min_price = min(c['low'] for c in data)
        result['min_price'] = min_price

        # Did price approach the level?
        for i, candle in enumerate(data):
            distance = candle['low'] - level_high

            if distance <= ALERT_DISTANCE:
                result['approached'] = True

                if candle['low'] <= level_high:
                    result['touched'] = True

                    # Check if it broke through
                    if candle['low'] < level_low - 5:
                        result['broke'] = True
                    else:
                        # Check subsequent candles for bounce
                        if i + 1 < len(data):
                            subsequent = data[i+1:]
                            if subsequent:
                                max_after = max(c['high'] for c in subsequent)
                                result['max_after_touch'] = max_after

                                # If price recovered above level + bounced
                                if max_after > level_high + 5:
                                    result['held'] = True
                                    result['bounce_pct'] = ((max_after - level_high) / level_high) * 100
                    break

    except Exception as e:
        print(f"    Error testing level: {e}")

    return result


def run_forward_test():
    """Run forward test from Jan 1-9, 2026."""
    print("=" * 70)
    print("FORWARD TEST: Swing Support Strategy")
    print("Period: Jan 1-9, 2026")
    print("Timeframes: 1H, Day")
    print("=" * 70)

    kite = get_kite()
    instruments = fetch_instruments(kite)

    # Test dates (trading days only)
    test_dates = []
    current = date(2026, 1, 1)
    end = date(2026, 1, 9)

    while current <= end:
        if current.weekday() < 5:  # Mon-Fri
            test_dates.append(current)
        current += timedelta(days=1)

    print(f"\nTest dates: {[d.strftime('%Y-%m-%d') for d in test_dates]}")

    # Track results
    all_results = []

    for index in ['NIFTY', 'BANKNIFTY']:
        print(f"\n{'='*70}")
        print(f"INDEX: {index}")
        print("=" * 70)

        config = INDICES[index]

        for test_date in test_dates:
            print(f"\n--- {test_date.strftime('%Y-%m-%d')} ---")

            try:
                # Get spot price at start of day
                spot_data = kite.historical_data(
                    instrument_token=config['spot_token'],
                    from_date=datetime.combine(test_date, time(9, 15)),
                    to_date=datetime.combine(test_date, time(9, 30)),
                    interval='minute'
                )

                if not spot_data:
                    print("  No spot data")
                    continue

                spot_open = spot_data[0]['open']
                atm = int(round(spot_open / config['round_to']) * config['round_to'])

                # Get monthly expiry as of test date
                expiry = get_monthly_expiry(index, instruments, test_date)
                if not expiry:
                    print("  No expiry found")
                    continue

                print(f"  Spot: {spot_open:.0f}, ATM: {atm}, Expiry: {expiry}")

                # Test both CE and PE
                for opt_type in ['CE', 'PE']:
                    key = (index, atm, opt_type, expiry)
                    if key not in instruments:
                        continue

                    inst = instruments[key]
                    symbol = inst['symbol']

                    # Get option price at start of day
                    opt_data = kite.historical_data(
                        instrument_token=inst['token'],
                        from_date=datetime.combine(test_date, time(9, 15)),
                        to_date=datetime.combine(test_date, time(9, 30)),
                        interval='minute'
                    )

                    if not opt_data:
                        continue

                    opt_open = opt_data[0]['open']

                    # Find support levels using data UP TO this date
                    for tf_name, tf_interval, lookback_days in [('1H', '60minute', 60), ('Day', 'day', 180)]:
                        try:
                            historical = kite.historical_data(
                                instrument_token=inst['token'],
                                from_date=test_date - timedelta(days=lookback_days),
                                to_date=datetime.combine(test_date, time(9, 15)),
                                interval=tf_interval
                            )

                            if len(historical) < 20:
                                continue

                            # Find levels BELOW current price
                            levels = find_support_levels(historical, OPTION_TOLERANCE, opt_open)

                            if not levels:
                                continue

                            print(f"\n  {symbol} [{tf_name}] - LTP: {opt_open:.1f}")
                            print(f"  Found {len(levels)} support levels below LTP")

                            # Test each level
                            for level in levels[:3]:  # Test top 3 closest levels
                                distance = opt_open - level['high']

                                print(f"    Level: {level['low']:.1f}-{level['high']:.1f} ({level['touches']} touches) - {distance:.1f} pts away")

                                # Test if level held during the day
                                perf = test_level_performance(kite, inst['token'], level, test_date, tf_interval)

                                if perf['approached']:
                                    result = {
                                        'date': test_date,
                                        'index': index,
                                        'symbol': symbol,
                                        'opt_type': opt_type,
                                        'timeframe': tf_name,
                                        'level_low': level['low'],
                                        'level_high': level['high'],
                                        'touches': level['touches'],
                                        'ltp_at_open': opt_open,
                                        'approached': perf['approached'],
                                        'touched': perf['touched'],
                                        'held': perf['held'],
                                        'broke': perf['broke'],
                                        'bounce_pct': perf['bounce_pct']
                                    }
                                    all_results.append(result)

                                    status = "HELD" if perf['held'] else ("BROKE" if perf['broke'] else "TOUCHED")
                                    bounce = f"+{perf['bounce_pct']:.1f}%" if perf['bounce_pct'] > 0 else ""
                                    print(f"      -> {status} {bounce}")
                                else:
                                    print(f"      -> Not approached")

                        except Exception as e:
                            print(f"    Error: {str(e)[:50]}")

            except Exception as e:
                print(f"  Error: {e}")

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    if not all_results:
        print("No levels were approached during test period")
        return

    approached = len(all_results)
    touched = sum(1 for r in all_results if r['touched'])
    held = sum(1 for r in all_results if r['held'])
    broke = sum(1 for r in all_results if r['broke'])

    print(f"\nTotal levels approached: {approached}")
    print(f"Levels touched: {touched}")
    print(f"Levels HELD (bounced): {held}")
    print(f"Levels BROKE: {broke}")

    if touched > 0:
        win_rate = (held / touched) * 100
        print(f"\nWIN RATE: {win_rate:.1f}% ({held}/{touched})")

    # Detail by timeframe
    print("\nBy Timeframe:")
    for tf in ['1H', 'Day']:
        tf_results = [r for r in all_results if r['timeframe'] == tf]
        tf_touched = sum(1 for r in tf_results if r['touched'])
        tf_held = sum(1 for r in tf_results if r['held'])
        if tf_touched > 0:
            print(f"  {tf}: {tf_held}/{tf_touched} held ({(tf_held/tf_touched)*100:.0f}%)")

    # Detail by touches
    print("\nBy Touch Count:")
    for min_touches in [2, 3]:
        t_results = [r for r in all_results if r['touches'] >= min_touches]
        t_touched = sum(1 for r in t_results if r['touched'])
        t_held = sum(1 for r in t_results if r['held'])
        if t_touched > 0:
            print(f"  {min_touches}+ touches: {t_held}/{t_touched} held ({(t_held/t_touched)*100:.0f}%)")

    # Show individual results
    print("\n" + "-" * 70)
    print("INDIVIDUAL RESULTS:")
    print("-" * 70)
    for r in all_results:
        status = "HELD" if r['held'] else ("BROKE" if r['broke'] else "TOUCHED")
        bounce = f"+{r['bounce_pct']:.1f}%" if r['bounce_pct'] > 0 else ""
        print(f"{r['date']} | {r['symbol'][:20]:20} | {r['timeframe']:4} | {r['level_low']:.0f}-{r['level_high']:.0f} | {status} {bounce}")


if __name__ == '__main__':
    run_forward_test()
