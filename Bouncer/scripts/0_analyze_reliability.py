#!/usr/bin/env python3
"""
Step 0b: Analyze S/R Reliability (Weekly - Sunday 8:30 PM)
==========================================================
BULLISH ONLY - Support bounce testing

- Backtests SUPPORT levels on historical data for each stock
- Calculates reliability scores with recency weighting
- Stores results in SQLite + exports JSON for scanner

Simulation Logic:
- Walk through history, identify SUPPORT levels at each point
- When price approaches support, simulate LONG trade:
  - Target = ATR × 1.5 (same as live)
  - Stop = Support break by 1.5%
  - Track: Success (target hit) vs Failure (stop hit)
- Calculate weighted success rate (recent tests weight more)

Run: python scripts/0_analyze_reliability.py
Cron: 30 20 * * 0 (Sunday 8:30 PM, after historical data fetch)
"""

import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple
from dataclasses import dataclass
import pandas as pd
import numpy as np

# =============================================================================
# PATHS
# =============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
BOUNCER_DIR = SCRIPT_DIR.parent
DATA_DIR = BOUNCER_DIR / 'data'
HISTORICAL_DIR = DATA_DIR / 'historical'
CONFIG_FILE = BOUNCER_DIR / 'config' / 'config.json'

# Output files
RELIABILITY_DB = DATA_DIR / 'sr_reliability.db'
RELIABILITY_JSON = DATA_DIR / 'sr_reliability.json'

# Load config
with open(CONFIG_FILE) as f:
    CONFIG = json.load(f)


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class LevelTest:
    """Represents a single S/R level test in history."""
    symbol: str
    test_date: str
    level_price: float
    level_type: str  # 'support' or 'resistance'
    entry_price: float
    atr: float
    target_price: float
    stop_price: float
    outcome: str  # 'success', 'failure', 'neutral'
    exit_date: str
    exit_price: float
    pnl_pct: float
    days_held: int


# =============================================================================
# S/R DETECTION (same as 2_analyze_candidates.py)
# =============================================================================

def find_swing_points(df: pd.DataFrame, window: int = 5) -> Tuple[List, List]:
    """Find swing highs and lows."""
    swing_lows = []
    swing_highs = []

    for i in range(window, len(df) - window):
        # Swing low
        is_low = all(
            df['low'].iloc[i] < df['low'].iloc[i - j] and
            df['low'].iloc[i] < df['low'].iloc[i + j]
            for j in range(1, window + 1)
        )
        if is_low:
            swing_lows.append((df.index[i], df['low'].iloc[i], df['volume'].iloc[i]))

        # Swing high
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
            if len(current) >= 2:  # At least 2 touches
                clusters.append({
                    'price': round(np.mean([p[1] for p in current]), 2),
                    'touches': len(current),
                    'last_touch': max(p[0] for p in current)
                })
            current = [point]

    # Don't forget the last cluster
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
    """
    Detect S/R levels using lookback data.
    Returns levels within 10% of current price.
    """
    swing_lows, swing_highs = find_swing_points(df_lookback)

    support_clusters = cluster_levels(swing_lows)
    resistance_clusters = cluster_levels(swing_highs)

    levels = []

    for cluster in support_clusters:
        distance_pct = abs(cluster['price'] - current_price) / current_price * 100
        if distance_pct <= 10:  # Within 10% of current price
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
# TRADE SIMULATION
# =============================================================================

def simulate_trade(
    df: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    target_price: float,
    stop_price: float,
    max_days: int = 10
) -> Tuple[str, int, float, float]:
    """
    Simulate a LONG trade forward from entry point (support bounce).

    Returns: (outcome, days_held, exit_price, pnl_pct)
    - outcome: 'success', 'failure', or 'neutral'
    """
    for i in range(1, min(max_days + 1, len(df) - entry_idx)):
        idx = entry_idx + i
        candle = df.iloc[idx]

        # LONG only: Check target hit (high reaches target)
        if candle['high'] >= target_price:
            pnl = (target_price - entry_price) / entry_price * 100
            return 'success', i, target_price, pnl

        # LONG only: Check stop hit (close below stop - support broken)
        if candle['close'] < stop_price:
            pnl = (candle['close'] - entry_price) / entry_price * 100
            return 'failure', i, candle['close'], pnl

    # Timeout - neither target nor stop hit
    return 'neutral', max_days, df.iloc[entry_idx + min(max_days, len(df) - entry_idx - 1)]['close'], 0.0


def backtest_stock(symbol: str, df: pd.DataFrame) -> List[LevelTest]:
    """
    Backtest SUPPORT reliability for a single stock (BULLISH only).
    Walk through history, simulate LONG trades at support level approaches.
    """
    tests = []
    lookback_days = 180
    min_start = lookback_days + 30  # Need lookback + some forward data

    if len(df) < min_start + 30:
        return tests

    atr_period = CONFIG.get('spread_config', {}).get('atr_period', 14)
    atr_multiplier = CONFIG.get('spread_config', {}).get('atr_multiplier', 1.5)
    approach_pct = 0.5  # Consider "at level" when within 0.5%
    break_pct = 1.5     # Level breaks when price closes 1.5% through

    # Track tested levels to avoid re-testing same level too frequently
    tested_levels = {}  # level_price: last_test_date

    for i in range(min_start, len(df) - 10):  # Leave room for forward sim
        current_date = df.index[i]
        current_price = df.iloc[i]['close']

        # Get lookback window
        df_lookback = df.iloc[i - lookback_days:i]

        # Compute ATR at this point
        atr = compute_atr(df_lookback, atr_period)
        if atr == 0:
            continue

        # Find levels at this point in time
        levels = detect_levels_at_point(df_lookback, current_price)

        for level in levels:
            level_price = level['price']
            level_type = level['type']

            # BULLISH ONLY: Only test support levels
            if level_type != 'support':
                continue

            # Check if we've tested this level recently (within 10 days)
            level_key = f"support_{int(level_price)}"
            if level_key in tested_levels:
                days_since = (current_date - tested_levels[level_key]).days
                if days_since < 10:
                    continue

            # Check if price is near level (within approach_pct)
            distance_pct = abs(current_price - level_price) / level_price * 100
            if distance_pct > approach_pct:
                continue

            # LONG only: Price at/above support
            if current_price >= level_price:
                entry = current_price
                target = entry + (atr * atr_multiplier)
                stop = level_price * (1 - break_pct / 100)
            else:
                continue

            # Simulate LONG trade
            outcome, days_held, exit_price, pnl_pct = simulate_trade(
                df, i, entry, target, stop, max_days=10
            )

            # Record test
            exit_date = df.index[min(i + days_held, len(df) - 1)]

            tests.append(LevelTest(
                symbol=symbol,
                test_date=current_date.strftime('%Y-%m-%d'),
                level_price=level_price,
                level_type='support',
                entry_price=entry,
                atr=atr,
                target_price=target,
                stop_price=stop,
                outcome=outcome,
                exit_date=exit_date.strftime('%Y-%m-%d'),
                exit_price=exit_price,
                pnl_pct=round(pnl_pct, 2),
                days_held=days_held
            ))

            # Mark level as tested
            tested_levels[level_key] = current_date

    return tests


# =============================================================================
# RECENCY WEIGHTING
# =============================================================================

def calculate_recency_weight(test_date: str, today: datetime) -> float:
    """
    Calculate recency weight for a test.
    Recent tests count more than old tests.

    - Last 1 year: weight 1.0
    - 1-2 years ago: weight 0.7
    - 2-3 years ago: weight 0.4
    - 3+ years ago: weight 0.25
    """
    test_dt = datetime.strptime(test_date, '%Y-%m-%d')
    age_days = (today - test_dt).days

    if age_days <= 365:
        return 1.0
    elif age_days <= 730:
        return 0.7
    elif age_days <= 1095:
        return 0.4
    else:
        return 0.25


def calculate_reliability_score(
    successes: float,
    failures: float,
    neutrals: int
) -> Tuple[float, int, int]:
    """
    Calculate reliability score (0-100) with bonus/penalty based on performance.

    Returns: (success_rate, reliability_score, bonus_points)

    Bonus/Penalty system (updated 2026-01-01):
    - 75%+ success: +20 bonus (excellent S/R respect)
    - 60-74%: +15 bonus
    - 50-59%: +10 bonus
    - 40-49%: +5 bonus
    - 30-39%: -5 penalty (poor - ICICIBANK type, breaks levels often)
    - 20-29%: -10 penalty (very poor - avoid)
    - <20%: -15 penalty (terrible - CGPOWER type, never trade S/R!)
    """
    total = successes + failures
    if total < 10:  # Raised from 5 to 10 for statistical significance
        return 0.5, 50, 5  # Neutral - not enough data

    success_rate = successes / total

    # Base score: success_rate × 80 (max 80 points)
    base_score = success_rate * 80

    # Bonus for high performers
    if success_rate >= 0.75:
        bonus_score = 20
    elif success_rate >= 0.65:
        bonus_score = 15
    elif success_rate >= 0.55:
        bonus_score = 10
    else:
        bonus_score = 0

    reliability_score = min(100, int(base_score + bonus_score))

    # Calculate bonus/penalty points for level scoring
    # Good performers get bonus, poor performers get PENALTY
    if success_rate >= 0.75:
        bonus_points = 20   # Excellent - strong S/R respect
    elif success_rate >= 0.60:
        bonus_points = 15   # Strong
    elif success_rate >= 0.50:
        bonus_points = 10   # Moderate
    elif success_rate >= 0.40:
        bonus_points = 5    # Weak but acceptable
    elif success_rate >= 0.30:
        bonus_points = -5   # Poor - penalize (ICICIBANK 37.8% type)
    elif success_rate >= 0.20:
        bonus_points = -10  # Very poor - strong penalty
    else:
        bonus_points = -15  # Terrible - avoid these stocks completely!

    return round(success_rate, 3), reliability_score, bonus_points


# =============================================================================
# DATABASE
# =============================================================================

def init_database():
    """Initialize SQLite database."""
    conn = sqlite3.connect(RELIABILITY_DB)
    cursor = conn.cursor()

    # Drop existing tables (fresh start each week)
    cursor.execute("DROP TABLE IF EXISTS level_tests")
    cursor.execute("DROP TABLE IF EXISTS stocks")

    # Stock summary table
    cursor.execute("""
        CREATE TABLE stocks (
            symbol TEXT PRIMARY KEY,
            last_updated TEXT,
            total_tests INT,
            successes REAL,
            failures REAL,
            neutrals INT,
            success_rate REAL,
            reliability_score INT,
            bonus_points INT
        )
    """)

    # Individual test results
    cursor.execute("""
        CREATE TABLE level_tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            test_date TEXT,
            level_price REAL,
            level_type TEXT,
            entry_price REAL,
            atr REAL,
            target_price REAL,
            stop_price REAL,
            outcome TEXT,
            exit_date TEXT,
            exit_price REAL,
            pnl_pct REAL,
            days_held INT,
            recency_weight REAL
        )
    """)

    conn.commit()
    return conn


def save_stock_results(conn, symbol: str, tests: List[LevelTest], today: datetime):
    """Save test results for a stock."""
    cursor = conn.cursor()

    # Calculate weighted metrics
    weighted_successes = 0.0
    weighted_failures = 0.0
    neutrals = 0

    for test in tests:
        weight = calculate_recency_weight(test.test_date, today)

        # Insert test record
        cursor.execute("""
            INSERT INTO level_tests (
                symbol, test_date, level_price, level_type, entry_price,
                atr, target_price, stop_price, outcome, exit_date,
                exit_price, pnl_pct, days_held, recency_weight
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            test.symbol, test.test_date, test.level_price, test.level_type,
            test.entry_price, test.atr, test.target_price, test.stop_price,
            test.outcome, test.exit_date, test.exit_price, test.pnl_pct,
            test.days_held, weight
        ))

        # Accumulate weighted metrics
        if test.outcome == 'success':
            weighted_successes += weight
        elif test.outcome == 'failure':
            weighted_failures += weight
        else:
            neutrals += 1

    # Calculate reliability score
    success_rate, reliability_score, bonus_points = calculate_reliability_score(
        weighted_successes, weighted_failures, neutrals
    )

    # Insert stock summary
    cursor.execute("""
        INSERT OR REPLACE INTO stocks (
            symbol, last_updated, total_tests, successes, failures,
            neutrals, success_rate, reliability_score, bonus_points
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        symbol, today.isoformat(), len(tests),
        round(weighted_successes, 2), round(weighted_failures, 2), neutrals,
        success_rate, reliability_score, bonus_points
    ))

    conn.commit()


def export_to_json(conn):
    """Export reliability data to JSON for scanner consumption."""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM stocks ORDER BY reliability_score DESC")
    rows = cursor.fetchall()

    columns = [desc[0] for desc in cursor.description]

    stocks = {}
    for row in rows:
        data = dict(zip(columns, row))
        symbol = data.pop('symbol')
        stocks[symbol] = data

    output = {
        'generated_at': datetime.now().isoformat(),
        'total_stocks': len(stocks),
        'stocks': stocks
    }

    with open(RELIABILITY_JSON, 'w') as f:
        json.dump(output, f, indent=2)

    return len(stocks)


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 70)
    print("STEP 0b: ANALYZE S/R RELIABILITY")
    print("=" * 70)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Historical data: {HISTORICAL_DIR}")
    print(f"Output DB: {RELIABILITY_DB}")
    print(f"Output JSON: {RELIABILITY_JSON}")
    print("=" * 70)
    print()

    # Find CSV files
    csv_files = sorted(HISTORICAL_DIR.glob("*_daily.csv"))
    if not csv_files:
        print("ERROR: No historical CSV files found!")
        print("Run 0_build_historical.py first")
        return 1

    print(f"Found {len(csv_files)} historical data files")
    print()

    # Initialize database
    print("Initializing database...")
    conn = init_database()

    # Process each stock
    print("\n" + "-" * 70)
    print("Analyzing S/R reliability...")
    print("-" * 70)

    today = datetime.now()
    total_tests = 0
    processed = 0

    for i, csv_file in enumerate(csv_files, 1):
        symbol = csv_file.stem.replace('_daily', '')

        print(f"[{i}/{len(csv_files)}] {symbol:15} ", end='', flush=True)

        try:
            # Load data
            df = pd.read_csv(csv_file, parse_dates=['date'], index_col='date')

            if len(df) < 250:  # Need at least ~1 year
                print("SKIP - Insufficient data")
                continue

            # Run backtest
            tests = backtest_stock(symbol, df)

            if tests:
                save_stock_results(conn, symbol, tests, today)
                total_tests += len(tests)
                processed += 1

                # Show summary
                successes = sum(1 for t in tests if t.outcome == 'success')
                failures = sum(1 for t in tests if t.outcome == 'failure')
                rate = successes / (successes + failures) if (successes + failures) > 0 else 0
                print(f"OK - {len(tests)} tests, {successes}W/{failures}L ({rate:.0%})")
            else:
                print("SKIP - No valid tests")

        except Exception as e:
            print(f"ERROR - {str(e)[:40]}")

    # Export to JSON
    print("\n" + "-" * 70)
    print("Exporting to JSON...")
    exported = export_to_json(conn)
    print(f"Exported {exported} stocks to {RELIABILITY_JSON}")

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Stocks processed: {processed}")
    print(f"Total tests simulated: {total_tests}")
    print(f"Database: {RELIABILITY_DB}")
    print(f"JSON export: {RELIABILITY_JSON}")

    # Show top 10 reliable stocks
    cursor = conn.cursor()
    cursor.execute("""
        SELECT symbol, success_rate, reliability_score, bonus_points, total_tests
        FROM stocks
        WHERE total_tests >= 10
        ORDER BY reliability_score DESC
        LIMIT 10
    """)
    top_stocks = cursor.fetchall()

    if top_stocks:
        print("\nTop 10 Most Reliable Stocks:")
        print("-" * 50)
        print(f"{'Symbol':12} {'Success%':>10} {'Score':>8} {'Bonus':>8} {'Tests':>8}")
        print("-" * 50)
        for row in top_stocks:
            symbol, rate, score, bonus, tests = row
            print(f"{symbol:12} {rate*100:>9.1f}% {score:>8} {bonus:>8} {tests:>8}")

    conn.close()

    print("\nDONE - SUPPORT reliability scores ready for scanner (BULLISH only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
