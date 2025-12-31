#!/usr/bin/env python3
"""
Fast Parameter Optimization - Streamlined version
Tests ATR multipliers and timeout days on historical data.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple
import sys

SCRIPT_DIR = Path(__file__).parent.resolve()
BOUNCER_DIR = SCRIPT_DIR.parent
HISTORICAL_DIR = BOUNCER_DIR / 'data' / 'historical'


def find_swing_points_fast(df: pd.DataFrame, window: int = 5) -> Tuple[List, List]:
    """Vectorized swing point detection."""
    lows = df['low'].values
    highs = df['high'].values

    swing_lows = []
    swing_highs = []

    for i in range(window, len(df) - window):
        # Check swing low
        is_low = True
        for j in range(1, window + 1):
            if not (lows[i] < lows[i-j] and lows[i] < lows[i+j]):
                is_low = False
                break
        if is_low:
            swing_lows.append((i, lows[i]))

        # Check swing high
        is_high = True
        for j in range(1, window + 1):
            if not (highs[i] > highs[i-j] and highs[i] > highs[i+j]):
                is_high = False
                break
        if is_high:
            swing_highs.append((i, highs[i]))

    return swing_lows, swing_highs


def cluster_levels_fast(points: List, tolerance_pct: float = 1.0) -> List[Dict]:
    """Cluster nearby points."""
    if not points:
        return []

    sorted_points = sorted(points, key=lambda x: x[1])
    clusters = []
    current = [sorted_points[0]]

    for point in sorted_points[1:]:
        avg = np.mean([p[1] for p in current])
        if abs(point[1] - avg) / avg * 100 <= tolerance_pct:
            current.append(point)
        else:
            if len(current) >= 2:
                clusters.append({
                    'price': np.mean([p[1] for p in current]),
                    'touches': len(current),
                    'last_idx': max(p[0] for p in current)
                })
            current = [point]

    if len(current) >= 2:
        clusters.append({
            'price': np.mean([p[1] for p in current]),
            'touches': len(current),
            'last_idx': max(p[0] for p in current)
        })

    return clusters


def simulate_trade(df_array, entry_idx, entry_price, target_price, stop_price,
                   direction, max_days, close_col, high_col, low_col):
    """Fast trade simulation using numpy arrays."""
    max_favorable = 0.0
    target_distance = abs(target_price - entry_price)

    for i in range(1, min(max_days + 1, len(df_array) - entry_idx)):
        idx = entry_idx + i
        high = df_array[idx, high_col]
        low = df_array[idx, low_col]
        close = df_array[idx, close_col]

        if direction == 'LONG':
            favorable = high - entry_price
            max_favorable = max(max_favorable, favorable)

            if high >= target_price:
                return 'success', 100.0
            if close < stop_price:
                pct = (max_favorable / target_distance * 100) if target_distance > 0 else 0
                return 'failure', pct
        else:
            favorable = entry_price - low
            max_favorable = max(max_favorable, favorable)

            if low <= target_price:
                return 'success', 100.0
            if close > stop_price:
                pct = (max_favorable / target_distance * 100) if target_distance > 0 else 0
                return 'failure', pct

    pct = (max_favorable / target_distance * 100) if target_distance > 0 else 0
    return 'neutral', pct


def analyze_stock(symbol: str, df: pd.DataFrame, atr_mult: float, max_days: int) -> Dict:
    """Analyze a single stock with given parameters."""
    results = {'wins': 0, 'losses': 0, 'neutrals': 0, 'partial_75': 0, 'partial_50': 0}

    if len(df) < 250:
        return results

    # Precompute ATR for entire series
    df = df.copy()
    df['prev_close'] = df['close'].shift(1)
    df['tr'] = pd.concat([
        df['high'] - df['low'],
        abs(df['high'] - df['prev_close']),
        abs(df['low'] - df['prev_close'])
    ], axis=1).max(axis=1)
    df['atr'] = df['tr'].rolling(14).mean()

    # Convert to numpy for speed
    arr = df[['close', 'high', 'low', 'atr']].values
    close_col, high_col, low_col, atr_col = 0, 1, 2, 3

    lookback = 180
    approach_pct = 0.5
    break_pct = 1.5

    tested_levels = {}

    # Sample every 3rd day for speed
    for i in range(lookback + 30, len(df) - max_days - 5, 3):
        current_price = arr[i, close_col]
        atr = arr[i, atr_col]

        if np.isnan(atr) or atr == 0:
            continue

        # Find levels in lookback window
        df_slice = df.iloc[i-lookback:i]
        swing_lows, swing_highs = find_swing_points_fast(df_slice)

        supports = cluster_levels_fast(swing_lows)
        resistances = cluster_levels_fast(swing_highs)

        # Check supports (for LONG trades)
        for level in supports:
            level_price = level['price']
            level_key = f"S_{int(level_price)}"

            if level_key in tested_levels and (i - tested_levels[level_key]) < 10:
                continue

            distance_pct = abs(current_price - level_price) / level_price * 100
            if distance_pct > approach_pct or current_price < level_price:
                continue

            entry = current_price
            target = entry + (atr * atr_mult)
            stop = level_price * (1 - break_pct / 100)

            outcome, pct = simulate_trade(arr, i, entry, target, stop, 'LONG',
                                          max_days, close_col, high_col, low_col)

            if outcome == 'success':
                results['wins'] += 1
            elif outcome == 'failure':
                results['losses'] += 1
                if pct >= 75:
                    results['partial_75'] += 1
                if pct >= 50:
                    results['partial_50'] += 1
            else:
                results['neutrals'] += 1

            tested_levels[level_key] = i

        # Check resistances (for SHORT trades)
        for level in resistances:
            level_price = level['price']
            level_key = f"R_{int(level_price)}"

            if level_key in tested_levels and (i - tested_levels[level_key]) < 10:
                continue

            distance_pct = abs(current_price - level_price) / level_price * 100
            if distance_pct > approach_pct or current_price > level_price:
                continue

            entry = current_price
            target = entry - (atr * atr_mult)
            stop = level_price * (1 + break_pct / 100)

            outcome, pct = simulate_trade(arr, i, entry, target, stop, 'SHORT',
                                          max_days, close_col, high_col, low_col)

            if outcome == 'success':
                results['wins'] += 1
            elif outcome == 'failure':
                results['losses'] += 1
                if pct >= 75:
                    results['partial_75'] += 1
                if pct >= 50:
                    results['partial_50'] += 1
            else:
                results['neutrals'] += 1

            tested_levels[level_key] = i

    return results


def main():
    print("=" * 70)
    print("PARAMETER OPTIMIZATION - FAST VERSION")
    print("=" * 70)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    csv_files = sorted(HISTORICAL_DIR.glob("*_daily.csv"))
    if not csv_files:
        print("ERROR: No CSV files found!")
        return

    print(f"Found {len(csv_files)} stocks")

    # Parameters to test
    atr_mults = [1.0, 1.2, 1.5, 2.0]
    max_days_opts = [10, 15, 20]

    # Results storage
    results = {
        f"ATR_{a}_Days_{d}": {'wins': 0, 'losses': 0, 'neutrals': 0, 'p75': 0, 'p50': 0}
        for a in atr_mults for d in max_days_opts
    }

    # Use ALL stocks
    sample_files = csv_files
    total = len(sample_files)

    print(f"Analyzing {total} stocks...")
    print()

    for idx, csv_file in enumerate(sample_files, 1):
        symbol = csv_file.stem.replace('_daily', '')
        sys.stdout.write(f"\r[{idx}/{total}] {symbol:<15}")
        sys.stdout.flush()

        try:
            df = pd.read_csv(csv_file, parse_dates=['date'], index_col='date')

            if len(df) < 300:
                continue

            for atr_mult in atr_mults:
                for max_days in max_days_opts:
                    key = f"ATR_{atr_mult}_Days_{max_days}"
                    r = analyze_stock(symbol, df, atr_mult, max_days)

                    results[key]['wins'] += r['wins']
                    results[key]['losses'] += r['losses']
                    results[key]['neutrals'] += r['neutrals']
                    results[key]['p75'] += r['partial_75']
                    results[key]['p50'] += r['partial_50']

        except Exception as e:
            pass

    print("\n")
    print("=" * 70)
    print("RESULTS")
    print("=" * 70)
    print()
    print(f"{'Config':<20} {'Wins':>7} {'Losses':>7} {'Neut':>7} {'Win%':>8} {'Near-Miss':>10}")
    print("-" * 70)

    best_key = None
    best_rate = 0

    for key in sorted(results.keys()):
        data = results[key]
        total_trades = data['wins'] + data['losses']

        if total_trades == 0:
            continue

        win_rate = data['wins'] / total_trades * 100
        near_miss = data['p75']  # Trades that got 75%+ to target

        print(f"{key:<20} {data['wins']:>7} {data['losses']:>7} {data['neutrals']:>7} {win_rate:>7.1f}% {near_miss:>10}")

        if win_rate > best_rate:
            best_rate = win_rate
            best_key = key

    print("-" * 70)
    print()
    print(f"BEST: {best_key} with {best_rate:.1f}% win rate")
    print()

    # Detailed analysis of near-misses
    print("=" * 70)
    print("NEAR-MISS ANALYSIS")
    print("=" * 70)
    print("(Trades that reached 75%+ of target before failing)")
    print()

    for key in sorted(results.keys()):
        data = results[key]
        if data['losses'] > 0:
            near_miss_pct = data['p75'] / data['losses'] * 100
            print(f"{key}: {data['p75']}/{data['losses']} failures = {near_miss_pct:.1f}% near-misses")

    print()
    print("INSIGHT: Near-misses could become wins with:")
    print("  - Lower ATR multiplier (smaller target)")
    print("  - Longer timeout (more time to hit target)")
    print("  - Earlier exit at 75% of target")


if __name__ == "__main__":
    main()
