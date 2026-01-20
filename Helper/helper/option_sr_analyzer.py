#!/usr/bin/env python3
"""
Option Support/Resistance Analyzer

Downloads historical data for specific option contracts in multiple timeframes
and calculates support levels using BOUNCER methodology.

Usage:
    python option_sr_analyzer.py BANKNIFTY 60000 CE    # Analyze CE
    python option_sr_analyzer.py BANKNIFTY 60000 PE    # Analyze PE
    python option_sr_analyzer.py BANKNIFTY 60000 BOTH  # Analyze both
"""

import json
import csv
import time
import argparse
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, asdict
from kiteconnect import KiteConnect

# =============================================================================
# PATHS
# =============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
HELPER_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == 'helper' else SCRIPT_DIR
BOTS_DIR = HELPER_DIR.parent
DATA_DIR = BOTS_DIR / 'data'
OUTPUT_DIR = HELPER_DIR / 'data' / 'option_analysis'
TOKEN_FILE = DATA_DIR / 'kite_access_token.json'
INDEX_OPTIONS_CSV = DATA_DIR / 'index_options.csv'

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# =============================================================================
# CONFIGURATION
# =============================================================================

TIMEFRAMES = {
    '15minute': '15min',
    '30minute': '30min',
    '60minute': '1hr'
}

# Support calculation parameters (from BOUNCER)
SR_CONFIG = {
    'swing_window': 5,           # Candles before/after for swing detection
    'touch_tolerance_pct': 1.5,  # Cluster tolerance %
    'min_touches': 2,            # Minimum touches to form level
    'recency_bonus_days': 30,    # Days for recency bonus
    'max_distance_pct': 15.0,    # Max distance from LTP
}

# Scoring parameters
SCORING = {
    'touches_2': 10,
    'touches_3': 20,
    'touches_4_plus': 30,
    'polarity_flip': 25,
    'round_number': 10,
    'recent_touch': 10,
    'high_volume': 15,
}

API_DELAY = 1.0  # Seconds between API calls

# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class SupportLevel:
    """Represents a support level."""
    price: float
    touches: int
    first_touch: str
    last_touch: str
    is_polarity_flip: bool
    avg_volume: float
    score: int
    distance_pct: float  # Distance from current LTP


# =============================================================================
# KITE CONNECTION
# =============================================================================

def get_kite() -> KiteConnect:
    """Get authenticated Kite connection."""
    if not TOKEN_FILE.exists():
        raise FileNotFoundError(
            f"Token file not found: {TOKEN_FILE}\n"
            "Ensure Kite authentication is complete."
        )

    with open(TOKEN_FILE) as f:
        token_data = json.load(f)

    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


# =============================================================================
# INSTRUMENT LOOKUP
# =============================================================================

def find_option_instrument(index_name: str, strike: int, option_type: str,
                           expiry_month: str = 'JAN') -> Optional[Dict]:
    """
    Find option instrument from index_options.csv

    Args:
        index_name: BANKNIFTY, NIFTY, etc.
        strike: Strike price (e.g., 60000)
        option_type: CE or PE
        expiry_month: Month name (JAN, FEB, etc.)

    Returns:
        Dict with tradingsymbol, instrument_token, expiry, lot_size
    """
    if not INDEX_OPTIONS_CSV.exists():
        print(f"ERROR: {INDEX_OPTIONS_CSV} not found!")
        print("Run: python kite_nse_options.py first")
        return None

    with open(INDEX_OPTIONS_CSV, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Columns: index, option_symbol, option_type, strike, expiry, option_token, lot_size, exchange, dte
            if (row['index'] == index_name and
                float(row['strike']) == strike and
                row['option_type'] == option_type and
                expiry_month in row['option_symbol']):
                return {
                    'tradingsymbol': row['option_symbol'],
                    'instrument_token': int(row['option_token']),
                    'expiry': row['expiry'],
                    'lot_size': int(row['lot_size']),
                    'exchange': row['exchange']
                }

    return None


# =============================================================================
# HISTORICAL DATA FETCHING
# =============================================================================

def fetch_historical_data(
    kite: KiteConnect,
    instrument_token: int,
    interval: str,
    from_date: datetime,
    to_date: datetime
) -> Optional[pd.DataFrame]:
    """
    Fetch historical OHLCV data for an option.

    Args:
        kite: Authenticated KiteConnect instance
        instrument_token: Instrument token
        interval: minute, 3minute, 5minute, 10minute, 15minute, 30minute, 60minute, day
        from_date: Start date
        to_date: End date

    Returns:
        DataFrame with OHLCV data
    """
    try:
        data = kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_date,
            to_date=to_date,
            interval=interval
        )

        if not data:
            return None

        df = pd.DataFrame(data)
        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)

        # Remove timezone info for easier handling
        df.index = df.index.tz_localize(None)

        return df

    except Exception as e:
        print(f"  Error fetching data: {e}")
        return None


def download_all_timeframes(
    kite: KiteConnect,
    instrument_token: int,
    symbol: str,
    from_date: datetime,
    to_date: datetime
) -> Dict[str, pd.DataFrame]:
    """
    Download historical data for all timeframes.

    Returns:
        Dict mapping timeframe name to DataFrame
    """
    results = {}

    for interval, tf_name in TIMEFRAMES.items():
        print(f"  Downloading {tf_name} data...", end=' ', flush=True)

        df = fetch_historical_data(kite, instrument_token, interval, from_date, to_date)

        if df is not None and len(df) > 0:
            results[tf_name] = df
            print(f"OK - {len(df)} candles")
        else:
            print("FAILED or no data")

        time.sleep(API_DELAY)

    return results


# =============================================================================
# SUPPORT LEVEL CALCULATION (BOUNCER METHODOLOGY)
# =============================================================================

def find_swing_points(df: pd.DataFrame, window: int = 5) -> Tuple[List, List]:
    """
    Find swing highs and swing lows using candle wicks.

    A swing low is when the current low is lower than all lows
    in the window on both sides.
    """
    swing_lows = []
    swing_highs = []

    for i in range(window, len(df) - window):
        # Check for swing low
        is_swing_low = all(
            df['low'].iloc[i] < df['low'].iloc[i - j] and
            df['low'].iloc[i] < df['low'].iloc[i + j]
            for j in range(1, window + 1)
        )
        if is_swing_low:
            swing_lows.append((
                df.index[i],
                df['low'].iloc[i],
                df['volume'].iloc[i]
            ))

        # Check for swing high (for polarity flip detection)
        is_swing_high = all(
            df['high'].iloc[i] > df['high'].iloc[i - j] and
            df['high'].iloc[i] > df['high'].iloc[i + j]
            for j in range(1, window + 1)
        )
        if is_swing_high:
            swing_highs.append((
                df.index[i],
                df['high'].iloc[i],
                df['volume'].iloc[i]
            ))

    return swing_lows, swing_highs


def cluster_levels(points: List, tolerance_pct: float = 1.5) -> List[Dict]:
    """
    Cluster nearby price points into levels.

    Points within tolerance_pct of the cluster average are grouped together.
    """
    if not points:
        return []

    sorted_points = sorted(points, key=lambda x: x[1])
    clusters = []
    current_cluster = [sorted_points[0]]

    for point in sorted_points[1:]:
        cluster_avg = np.mean([p[1] for p in current_cluster])

        if cluster_avg > 0 and abs(point[1] - cluster_avg) / cluster_avg * 100 <= tolerance_pct:
            current_cluster.append(point)
        else:
            if current_cluster:
                clusters.append({
                    'price': np.mean([p[1] for p in current_cluster]),
                    'touches': len(current_cluster),
                    'dates': [p[0] for p in current_cluster],
                    'volumes': [p[2] for p in current_cluster]
                })
            current_cluster = [point]

    # Don't forget the last cluster
    if current_cluster:
        clusters.append({
            'price': np.mean([p[1] for p in current_cluster]),
            'touches': len(current_cluster),
            'dates': [p[0] for p in current_cluster],
            'volumes': [p[2] for p in current_cluster]
        })

    return clusters


def detect_polarity_flip(df: pd.DataFrame, level_price: float,
                         tolerance_pct: float = 1.5) -> bool:
    """
    Detect if a support level has flipped from resistance.

    A polarity flip occurs when price first rejected above a level (resistance)
    and later found support below it.
    """
    tolerance = level_price * tolerance_pct / 100

    # Find touches from above (resistance behavior)
    touches_above = df[df['low'].between(level_price - tolerance, level_price + tolerance)]
    # Find touches from below (support behavior)
    touches_below = df[df['high'].between(level_price - tolerance, level_price + tolerance)]

    if len(touches_below) > 0 and len(touches_above) > 0:
        # If resistance was tested before support, it's a flip
        if touches_below.index.min() < touches_above.index.max():
            return True

    return False


def is_round_number(price: float) -> bool:
    """Check if price is near a round number (within 1%)."""
    # For options, common round numbers
    if price < 10:
        levels = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    elif price < 50:
        levels = [10, 15, 20, 25, 30, 35, 40, 45, 50]
    elif price < 100:
        levels = [50, 60, 70, 80, 90, 100]
    elif price < 500:
        levels = [100, 150, 200, 250, 300, 350, 400, 450, 500]
    elif price < 1000:
        levels = [500, 600, 700, 800, 900, 1000]
    else:
        levels = [1000, 1500, 2000, 2500, 3000]

    return any(abs(price - lvl) / lvl * 100 <= 1.0 for lvl in levels if lvl > 0)


def score_level(touches: int, is_flip: bool, is_round: bool,
                is_recent: bool, high_volume: bool) -> int:
    """Score a support level based on quality factors."""
    score = 0

    # Touches scoring
    if touches >= 4:
        score += SCORING['touches_4_plus']
    elif touches >= 3:
        score += SCORING['touches_3']
    elif touches >= 2:
        score += SCORING['touches_2']

    # Quality bonuses
    if is_flip:
        score += SCORING['polarity_flip']
    if is_round:
        score += SCORING['round_number']
    if is_recent:
        score += SCORING['recent_touch']
    if high_volume:
        score += SCORING['high_volume']

    return score


def calculate_support_levels(df: pd.DataFrame, current_price: float) -> List[SupportLevel]:
    """
    Calculate support levels using BOUNCER methodology.

    Args:
        df: DataFrame with OHLCV data
        current_price: Current LTP of the option

    Returns:
        List of SupportLevel objects sorted by score
    """
    config = SR_CONFIG

    # Find swing points
    swing_lows, swing_highs = find_swing_points(df, config['swing_window'])

    if not swing_lows:
        return []

    # Cluster into levels
    support_clusters = cluster_levels(swing_lows, config['touch_tolerance_pct'])

    levels = []
    avg_volume = df['volume'].mean() if 'volume' in df.columns else 0
    now = datetime.now()

    for cluster in support_clusters:
        # Filter by minimum touches
        if cluster['touches'] < config['min_touches']:
            continue

        # Calculate distance from current price
        distance_pct = abs(cluster['price'] - current_price) / current_price * 100 if current_price > 0 else 0

        # Filter by max distance (only show relevant levels)
        if distance_pct > config['max_distance_pct']:
            continue

        # Quality checks
        is_flip = detect_polarity_flip(df, cluster['price'], config['touch_tolerance_pct'])
        is_round = is_round_number(cluster['price'])

        last_touch = max(cluster['dates'])
        last_touch_dt = last_touch if isinstance(last_touch, datetime) else pd.Timestamp(last_touch).to_pydatetime()
        is_recent = (now - last_touch_dt).days <= config['recency_bonus_days']

        high_vol = np.mean(cluster['volumes']) > avg_volume * 1.5 if avg_volume > 0 else False

        # Score the level
        score = score_level(cluster['touches'], is_flip, is_round, is_recent, high_vol)

        level = SupportLevel(
            price=round(cluster['price'], 2),
            touches=cluster['touches'],
            first_touch=str(min(cluster['dates']).date() if hasattr(min(cluster['dates']), 'date') else min(cluster['dates'])),
            last_touch=str(last_touch.date() if hasattr(last_touch, 'date') else last_touch),
            is_polarity_flip=is_flip,
            avg_volume=round(np.mean(cluster['volumes']), 0),
            score=score,
            distance_pct=round(distance_pct, 2)
        )
        levels.append(level)

    # Sort by score (highest first)
    levels.sort(key=lambda x: x.score, reverse=True)

    return levels


# =============================================================================
# OUTPUT
# =============================================================================

def save_historical_csv(df: pd.DataFrame, symbol: str, timeframe: str) -> Path:
    """Save historical data to CSV."""
    filename = f"{symbol}_{timeframe}.csv"
    filepath = OUTPUT_DIR / filename

    # Reset index to include date as column
    df_out = df.reset_index()
    df_out.to_csv(filepath, index=False)

    return filepath


def save_support_levels_csv(levels: List[SupportLevel], symbol: str,
                            timeframe: str, current_price: float) -> Path:
    """Save support levels to CSV."""
    filename = f"{symbol}_{timeframe}_support.csv"
    filepath = OUTPUT_DIR / filename

    with open(filepath, 'w', newline='') as f:
        writer = csv.writer(f)

        # Header
        writer.writerow(['# Support Levels Analysis'])
        writer.writerow([f'# Symbol: {symbol}'])
        writer.writerow([f'# Timeframe: {timeframe}'])
        writer.writerow([f'# Current Price (LTP): {current_price}'])
        writer.writerow([f'# Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'])
        writer.writerow([])

        # Column headers
        writer.writerow([
            'Price', 'Touches', 'First Touch', 'Last Touch',
            'Polarity Flip', 'Avg Volume', 'Score', 'Distance %', 'Recommendation'
        ])

        # Data rows
        for level in levels:
            # Generate recommendation based on score
            if level.score >= 50:
                rec = 'STRONG BUY ZONE'
            elif level.score >= 40:
                rec = 'GOOD SUPPORT'
            elif level.score >= 30:
                rec = 'MODERATE SUPPORT'
            else:
                rec = 'WEAK SUPPORT'

            writer.writerow([
                level.price,
                level.touches,
                level.first_touch,
                level.last_touch,
                'YES' if level.is_polarity_flip else 'No',
                int(level.avg_volume),
                level.score,
                f"{level.distance_pct:.2f}%",
                rec
            ])

    return filepath


def print_support_analysis(levels: List[SupportLevel], symbol: str,
                           timeframe: str, current_price: float):
    """Print support level analysis to console."""
    print(f"\n{'='*70}")
    print(f"SUPPORT LEVELS - {symbol} ({timeframe})")
    print(f"Current Price: {current_price:.2f}")
    print(f"{'='*70}")

    if not levels:
        print("No support levels found.")
        return

    print(f"{'Price':>10} {'Touches':>8} {'Score':>6} {'Dist%':>8} {'Flip':>6} {'Last Touch':>12} {'Recommendation':<18}")
    print("-" * 70)

    for level in levels[:10]:  # Top 10 levels
        flip = "YES" if level.is_polarity_flip else ""

        if level.score >= 50:
            rec = "** STRONG **"
        elif level.score >= 40:
            rec = "* GOOD *"
        elif level.score >= 30:
            rec = "Moderate"
        else:
            rec = "Weak"

        print(f"{level.price:>10.2f} {level.touches:>8} {level.score:>6} "
              f"{level.distance_pct:>7.2f}% {flip:>6} {level.last_touch:>12} {rec:<18}")


# =============================================================================
# MAIN
# =============================================================================

def analyze_option(index_name: str, strike: int, option_type: str,
                   from_date: datetime, to_date: datetime) -> Dict:
    """
    Main analysis function for a single option.

    Returns:
        Dict with results for each timeframe
    """
    print(f"\n{'#'*70}")
    print(f"# Analyzing: {index_name} {strike} {option_type}")
    print(f"# Period: {from_date.date()} to {to_date.date()}")
    print(f"{'#'*70}")

    # Find the instrument
    instrument = find_option_instrument(index_name, strike, option_type)

    if not instrument:
        print(f"ERROR: Could not find {index_name} {strike} {option_type}")
        print("Check index_options.csv or run kite_nse_options.py")
        return {}

    symbol = instrument['tradingsymbol']
    token = instrument['instrument_token']

    print(f"\nInstrument: {symbol}")
    print(f"Token: {token}")
    print(f"Expiry: {instrument['expiry']}")
    print(f"Lot Size: {instrument['lot_size']}")

    # Connect to Kite
    print("\nConnecting to Kite...")
    kite = get_kite()

    # Get current LTP
    try:
        quote = kite.quote(f"NFO:{symbol}")
        current_price = quote[f"NFO:{symbol}"]['last_price']
        print(f"Current LTP: {current_price}")
    except Exception as e:
        print(f"Warning: Could not get LTP: {e}")
        current_price = 0

    # Download historical data
    print("\nDownloading historical data...")
    data_by_tf = download_all_timeframes(kite, token, symbol, from_date, to_date)

    if not data_by_tf:
        print("ERROR: No data downloaded!")
        return {}

    # Analyze each timeframe
    results = {}

    for tf_name, df in data_by_tf.items():
        print(f"\n{'='*50}")
        print(f"Analyzing {tf_name} timeframe ({len(df)} candles)")
        print(f"{'='*50}")

        # Save raw data
        csv_path = save_historical_csv(df, symbol, tf_name)
        print(f"Saved: {csv_path}")

        # Calculate support levels
        # Use last close if no current price
        analysis_price = current_price if current_price > 0 else df['close'].iloc[-1]

        levels = calculate_support_levels(df, analysis_price)

        # Save support levels
        if levels:
            support_csv = save_support_levels_csv(levels, symbol, tf_name, analysis_price)
            print(f"Saved: {support_csv}")

        # Print analysis
        print_support_analysis(levels, symbol, tf_name, analysis_price)

        results[tf_name] = {
            'data_path': str(csv_path),
            'candles': len(df),
            'support_levels': [asdict(lvl) for lvl in levels],
            'current_price': analysis_price
        }

    return results


def main():
    parser = argparse.ArgumentParser(description='Option Support/Resistance Analyzer')
    parser.add_argument('index', help='Index name (BANKNIFTY, NIFTY)')
    parser.add_argument('strike', type=int, help='Strike price (e.g., 60000)')
    parser.add_argument('option_type', choices=['CE', 'PE', 'BOTH'],
                        help='Option type (CE, PE, or BOTH)')
    parser.add_argument('--from-date', type=str, default='2025-12-15',
                        help='Start date (YYYY-MM-DD), default: 2025-12-15')
    parser.add_argument('--to-date', type=str, default=None,
                        help='End date (YYYY-MM-DD), default: today')

    args = parser.parse_args()

    # Parse dates
    from_date = datetime.strptime(args.from_date, '%Y-%m-%d')
    to_date = datetime.strptime(args.to_date, '%Y-%m-%d') if args.to_date else datetime.now()

    print("=" * 70)
    print("OPTION SUPPORT/RESISTANCE ANALYZER")
    print("=" * 70)
    print(f"Index: {args.index}")
    print(f"Strike: {args.strike}")
    print(f"Option Type: {args.option_type}")
    print(f"Period: {from_date.date()} to {to_date.date()}")
    print(f"Output: {OUTPUT_DIR}")
    print("=" * 70)

    all_results = {}

    if args.option_type == 'BOTH':
        types_to_analyze = ['CE', 'PE']
    else:
        types_to_analyze = [args.option_type]

    for opt_type in types_to_analyze:
        results = analyze_option(args.index, args.strike, opt_type, from_date, to_date)
        all_results[opt_type] = results

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    for opt_type, results in all_results.items():
        print(f"\n{args.index} {args.strike} {opt_type}:")
        for tf_name, tf_data in results.items():
            levels = tf_data.get('support_levels', [])
            strong = sum(1 for l in levels if l['score'] >= 50)
            good = sum(1 for l in levels if 40 <= l['score'] < 50)
            print(f"  {tf_name}: {len(levels)} levels ({strong} strong, {good} good)")

    print(f"\nOutput files saved to: {OUTPUT_DIR}")
    print("\nDONE!")

    return 0


if __name__ == "__main__":
    exit(main())
