#!/usr/bin/env python3
"""
Parameter Optimization Analysis
================================
Tests different combinations of:
- ATR multipliers (1.0, 1.2, 1.5, 2.0)
- Timeout days (10, 15, 20, 25)
- Analyzes partial wins (how close did failures get to target?)

Uses actual historical data to find optimal settings.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Tuple
from dataclasses import dataclass
import json

# Paths
SCRIPT_DIR = Path(__file__).parent.resolve()
BOUNCER_DIR = SCRIPT_DIR.parent
HISTORICAL_DIR = BOUNCER_DIR / 'data' / 'historical'

# =============================================================================
# S/R DETECTION (same as main code)
# =============================================================================

def find_swing_points(df: pd.DataFrame, window: int = 5) -> Tuple[List, List]:
    """Find swing highs and lows."""
    swing_lows = []
    swing_highs = []

    for i in range(window, len(df) - window):
        is_low = all(
            df['low'].iloc[i] < df['low'].iloc[i - j] and
            df['low'].iloc[i] < df['low'].iloc[i + j]
            for j in range(1, window + 1)
        )
        if is_low:
            swing_lows.append((df.index[i], df['low'].iloc[i], df['volume'].iloc[i]))

        is_high = all(
            df['high'].iloc[i] > df['high'].iloc[i - j] and
            df['high'].iloc[i] > df['high'].iloc[i + j]
            for j in range(1, window + 1)
        )
        if is_high:
            swing_highs.append((df.index[i], df['high'].iloc[i], df['volume'].iloc[i]))

    return swing_lows, swing_highs


def cluster_levels(points: List, tolerance_pct: float = 1.0) -> List[Dict]:
    """Cluster nearby swing points into levels."""
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
                    'price': round(np.mean([p[1] for p in current]), 2),
                    'touches': len(current),
                    'last_touch': max(p[0] for p in current)
                })
            current = [point]

    if len(current) >= 2:
        clusters.append({
            'price': round(np.mean([p[1] for p in current]), 2),
            'touches': len(current),
            'last_touch': max(p[0] for p in current)
        })

    return clusters


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Compute ATR at end of dataframe."""
    if len(df) < period + 1:
        return 0.0

    df = df.copy()
    df['prev_close'] = df['close'].shift(1)
    df['tr1'] = df['high'] - df['low']
    df['tr2'] = abs(df['high'] - df['prev_close'])
    df['tr3'] = abs(df['low'] - df['prev_close'])
    df['true_range'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)

    atr = df['true_range'].rolling(window=period).mean().iloc[-1]
    return round(atr, 2) if not pd.isna(atr) else 0.0


def detect_levels_at_point(df_lookback: pd.DataFrame, current_price: float) -> List[Dict]:
    """Detect S/R levels using lookback data."""
    swing_lows, swing_highs = find_swing_points(df_lookback)

    support_clusters = cluster_levels(swing_lows)
    resistance_clusters = cluster_levels(swing_highs)

    levels = []

    for cluster in support_clusters:
        distance_pct = abs(cluster['price'] - current_price) / current_price * 100
        if distance_pct <= 10:
            levels.append({
                'price': cluster['price'],
                'type': 'support',
                'touches': cluster['touches']
            })

    for cluster in resistance_clusters:
        distance_pct = abs(cluster['price'] - current_price) / current_price * 100
        if distance_pct <= 10:
            levels.append({
                'price': cluster['price'],
                'type': 'resistance',
                'touches': cluster['touches']
            })

    return levels


# =============================================================================
# ENHANCED TRADE SIMULATION
# =============================================================================

@dataclass
class TradeResult:
    """Enhanced trade result with partial progress tracking."""
    outcome: str  # 'success', 'failure', 'neutral'
    days_held: int
    exit_price: float
    pnl_pct: float
    max_favorable: float  # Maximum favorable excursion (how close to target)
    max_adverse: float    # Maximum adverse excursion (how close to stop)
    target_reached_pct: float  # What % of target was reached


def simulate_trade_enhanced(
    df: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    target_price: float,
    stop_price: float,
    direction: str,
    max_days: int = 10
) -> TradeResult:
    """
    Enhanced trade simulation tracking partial progress.
    """
    max_favorable = 0.0
    max_adverse = 0.0
    target_distance = abs(target_price - entry_price)

    for i in range(1, min(max_days + 1, len(df) - entry_idx)):
        idx = entry_idx + i
        candle = df.iloc[idx]

        if direction == 'LONG':
            # Track maximum favorable (highest high)
            favorable = candle['high'] - entry_price
            max_favorable = max(max_favorable, favorable)

            # Track maximum adverse (lowest low)
            adverse = entry_price - candle['low']
            max_adverse = max(max_adverse, adverse)

            # Check target hit
            if candle['high'] >= target_price:
                pnl = (target_price - entry_price) / entry_price * 100
                return TradeResult('success', i, target_price, pnl,
                                   max_favorable, max_adverse, 100.0)

            # Check stop hit
            if candle['close'] < stop_price:
                pnl = (candle['close'] - entry_price) / entry_price * 100
                target_pct = (max_favorable / target_distance * 100) if target_distance > 0 else 0
                return TradeResult('failure', i, candle['close'], pnl,
                                   max_favorable, max_adverse, target_pct)

        else:  # SHORT
            # Track maximum favorable (lowest low)
            favorable = entry_price - candle['low']
            max_favorable = max(max_favorable, favorable)

            # Track maximum adverse (highest high)
            adverse = candle['high'] - entry_price
            max_adverse = max(max_adverse, adverse)

            # Check target hit
            if candle['low'] <= target_price:
                pnl = (entry_price - target_price) / entry_price * 100
                return TradeResult('success', i, target_price, pnl,
                                   max_favorable, max_adverse, 100.0)

            # Check stop hit
            if candle['close'] > stop_price:
                pnl = (entry_price - candle['close']) / entry_price * 100
                target_pct = (max_favorable / target_distance * 100) if target_distance > 0 else 0
                return TradeResult('failure', i, candle['close'], pnl,
                                   max_favorable, max_adverse, target_pct)

    # Timeout
    target_pct = (max_favorable / target_distance * 100) if target_distance > 0 else 0
    return TradeResult('neutral', max_days, df.iloc[entry_idx + min(max_days, len(df) - entry_idx - 1)]['close'],
                       0.0, max_favorable, max_adverse, target_pct)


# =============================================================================
# BACKTEST WITH PARAMETERS
# =============================================================================

def backtest_with_params(
    symbol: str,
    df: pd.DataFrame,
    atr_multiplier: float = 1.5,
    max_days: int = 10
) -> Dict:
    """Run backtest with specific parameters."""
    results = {
        'symbol': symbol,
        'atr_mult': atr_multiplier,
        'max_days': max_days,
        'tests': [],
        'wins': 0,
        'losses': 0,
        'neutrals': 0,
        'partial_wins_75': 0,  # Reached 75%+ of target but failed
        'partial_wins_50': 0,  # Reached 50%+ of target but failed
        'avg_target_reached': 0,
    }

    lookback_days = 180
    min_start = lookback_days + 30

    if len(df) < min_start + 30:
        return results

    approach_pct = 0.5
    break_pct = 1.5
    tested_levels = {}
    all_target_pcts = []

    for i in range(min_start, len(df) - 25):  # Leave room for max 25 day timeout
        current_date = df.index[i]
        current_price = df.iloc[i]['close']

        df_lookback = df.iloc[i - lookback_days:i]
        atr = compute_atr(df_lookback, 14)
        if atr == 0:
            continue

        levels = detect_levels_at_point(df_lookback, current_price)

        for level in levels:
            level_price = level['price']
            level_type = level['type']

            level_key = f"{level_type}_{int(level_price)}"
            if level_key in tested_levels:
                days_since = (current_date - tested_levels[level_key]).days
                if days_since < 10:
                    continue

            distance_pct = abs(current_price - level_price) / level_price * 100
            if distance_pct > approach_pct:
                continue

            if level_type == 'support' and current_price >= level_price:
                direction = 'LONG'
                entry = current_price
                target = entry + (atr * atr_multiplier)
                stop = level_price * (1 - break_pct / 100)
            elif level_type == 'resistance' and current_price <= level_price:
                direction = 'SHORT'
                entry = current_price
                target = entry - (atr * atr_multiplier)
                stop = level_price * (1 + break_pct / 100)
            else:
                continue

            # Run simulation
            result = simulate_trade_enhanced(df, i, entry, target, stop, direction, max_days)

            results['tests'].append({
                'date': current_date.strftime('%Y-%m-%d'),
                'outcome': result.outcome,
                'target_reached_pct': result.target_reached_pct,
                'days_held': result.days_held,
                'pnl_pct': result.pnl_pct
            })

            if result.outcome == 'success':
                results['wins'] += 1
            elif result.outcome == 'failure':
                results['losses'] += 1
                all_target_pcts.append(result.target_reached_pct)
                if result.target_reached_pct >= 75:
                    results['partial_wins_75'] += 1
                if result.target_reached_pct >= 50:
                    results['partial_wins_50'] += 1
            else:
                results['neutrals'] += 1
                all_target_pcts.append(result.target_reached_pct)

            tested_levels[level_key] = current_date

    if all_target_pcts:
        results['avg_target_reached'] = round(np.mean(all_target_pcts), 1)

    return results


# =============================================================================
# MAIN ANALYSIS
# =============================================================================

def main():
    print("=" * 70)
    print("PARAMETER OPTIMIZATION ANALYSIS")
    print("=" * 70)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # Find CSV files
    csv_files = sorted(HISTORICAL_DIR.glob("*_daily.csv"))
    if not csv_files:
        print("ERROR: No historical CSV files found!")
        return

    print(f"Found {len(csv_files)} stocks")

    # Test parameters
    atr_multipliers = [1.0, 1.2, 1.5, 2.0]
    max_days_options = [10, 15, 20]

    # Results storage
    all_results = {
        f"atr_{atr}_days_{days}": {
            'total_wins': 0,
            'total_losses': 0,
            'total_neutrals': 0,
            'partial_75': 0,
            'partial_50': 0,
            'stocks': []
        }
        for atr in atr_multipliers
        for days in max_days_options
    }

    # Sample stocks for faster analysis (or use all)
    sample_size = min(50, len(csv_files))  # Analyze 50 stocks
    sample_files = csv_files[:sample_size]

    print(f"Analyzing {sample_size} stocks with {len(atr_multipliers) * len(max_days_options)} parameter combinations...")
    print()

    for file_idx, csv_file in enumerate(sample_files, 1):
        symbol = csv_file.stem.replace('_daily', '')
        print(f"[{file_idx}/{sample_size}] {symbol}...", end=' ', flush=True)

        try:
            df = pd.read_csv(csv_file, parse_dates=['date'], index_col='date')

            if len(df) < 300:
                print("SKIP (insufficient data)")
                continue

            # Test each parameter combination
            for atr_mult in atr_multipliers:
                for max_days in max_days_options:
                    key = f"atr_{atr_mult}_days_{max_days}"
                    result = backtest_with_params(symbol, df, atr_mult, max_days)

                    all_results[key]['total_wins'] += result['wins']
                    all_results[key]['total_losses'] += result['losses']
                    all_results[key]['total_neutrals'] += result['neutrals']
                    all_results[key]['partial_75'] += result['partial_wins_75']
                    all_results[key]['partial_50'] += result['partial_wins_50']

                    if result['wins'] + result['losses'] > 0:
                        all_results[key]['stocks'].append({
                            'symbol': symbol,
                            'wins': result['wins'],
                            'losses': result['losses'],
                            'win_rate': result['wins'] / (result['wins'] + result['losses']),
                            'avg_target_pct': result['avg_target_reached']
                        })

            print("OK")

        except Exception as e:
            print(f"ERROR: {e}")

    # Print results
    print()
    print("=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)
    print()
    print(f"{'Parameters':<20} {'Wins':>8} {'Losses':>8} {'Win%':>8} {'75%+':>8} {'50%+':>8}")
    print("-" * 70)

    best_config = None
    best_win_rate = 0

    for key in sorted(all_results.keys()):
        data = all_results[key]
        total = data['total_wins'] + data['total_losses']
        if total == 0:
            continue

        win_rate = data['total_wins'] / total * 100

        print(f"{key:<20} {data['total_wins']:>8} {data['total_losses']:>8} {win_rate:>7.1f}% {data['partial_75']:>8} {data['partial_50']:>8}")

        if win_rate > best_win_rate:
            best_win_rate = win_rate
            best_config = key

    print("-" * 70)
    print()
    print(f"BEST CONFIGURATION: {best_config} with {best_win_rate:.1f}% win rate")
    print()

    # Analyze partial wins for current config (ATR 1.5, 10 days)
    current_key = "atr_1.5_days_10"
    current = all_results[current_key]
    total_current = current['total_wins'] + current['total_losses']

    if total_current > 0:
        print("=" * 70)
        print("PARTIAL WIN ANALYSIS (Current: ATR 1.5, 10 days)")
        print("=" * 70)
        print(f"Total failures: {current['total_losses']}")
        print(f"  - Reached 75%+ of target: {current['partial_75']} ({current['partial_75']/current['total_losses']*100:.1f}% of failures)")
        print(f"  - Reached 50%+ of target: {current['partial_50']} ({current['partial_50']/current['total_losses']*100:.1f}% of failures)")
        print()
        print("These 'failures' were actually close to winning!")
        print("With lower ATR multiplier, many would convert to wins.")

    # Save detailed results
    output_file = BOUNCER_DIR / 'data' / 'parameter_optimization_results.json'

    # Convert for JSON serialization
    json_results = {}
    for key, data in all_results.items():
        total = data['total_wins'] + data['total_losses']
        json_results[key] = {
            'wins': data['total_wins'],
            'losses': data['total_losses'],
            'neutrals': data['total_neutrals'],
            'win_rate': round(data['total_wins'] / total * 100, 2) if total > 0 else 0,
            'partial_75_pct': data['partial_75'],
            'partial_50_pct': data['partial_50'],
            'stock_count': len(data['stocks'])
        }

    with open(output_file, 'w') as f:
        json.dump({
            'generated_at': datetime.now().isoformat(),
            'stocks_analyzed': sample_size,
            'results': json_results
        }, f, indent=2)

    print(f"\nDetailed results saved to: {output_file}")


if __name__ == "__main__":
    main()
