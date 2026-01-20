#!/usr/bin/env python3
"""
Support/Resistance Level Scanner
Identifies high-probability trading setups at key technical levels.
"""

import json
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
from kiteconnect import KiteConnect

# ============================================================================
# CONFIGURATION
# ============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve() if '__file__' in dir() else Path('.').resolve()
CONFIG_FILE = SCRIPT_DIR / 'config' / 'scanner_config.json'
TOKEN_FILE = Path('.').resolve().parent / 'data' / 'kite_access_token.json'
INSTRUMENTS_FILE = Path('.').resolve().parent / 'data' / 'instruments.csv'

# Load config
with open(CONFIG_FILE) as f:
    CONFIG = json.load(f)

# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class Level:
    """Represents a support/resistance level."""
    price: float
    level_type: str  # 'support' or 'resistance'
    touches: int
    first_touch: datetime
    last_touch: datetime
    is_polarity_flip: bool  # Resistance turned support or vice versa
    volume_at_touches: float
    score: int = 0

@dataclass
class Setup:
    """Represents a potential trade setup."""
    symbol: str
    ltp: float
    level: Level
    distance_pct: float
    direction: str  # 'BULLISH' or 'BEARISH'
    score: int
    suggested_strategy: str
    long_strike: int
    short_strike: int
    expiry: str

# ============================================================================
# KITE CONNECTION
# ============================================================================

def get_kite() -> KiteConnect:
    """Initialize Kite connection."""
    with open(TOKEN_FILE) as f:
        token_data = json.load(f)
    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite

# ============================================================================
# DATA FETCHING
# ============================================================================

def get_instrument_token(kite: KiteConnect, symbol: str) -> Optional[int]:
    """Get instrument token for a symbol."""
    try:
        instruments = kite.instruments('NSE')
        for inst in instruments:
            if inst['tradingsymbol'] == symbol:
                return inst['instrument_token']
    except Exception as e:
        print(f"Error fetching instrument token for {symbol}: {e}")
    return None

def get_historical_data(kite: KiteConnect, symbol: str, days: int = 365) -> Optional[pd.DataFrame]:
    """Fetch historical OHLC data for a symbol."""
    token = get_instrument_token(kite, symbol)
    if not token:
        return None

    to_date = datetime.now()
    from_date = to_date - timedelta(days=days)

    try:
        data = kite.historical_data(
            instrument_token=token,
            from_date=from_date,
            to_date=to_date,
            interval='day'
        )
        df = pd.DataFrame(data)
        if len(df) > 0:
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)
        return df
    except Exception as e:
        print(f"Error fetching historical data for {symbol}: {e}")
        return None

# ============================================================================
# SUPPORT/RESISTANCE DETECTION
# ============================================================================

def find_swing_points(df: pd.DataFrame, window: int = 5) -> Tuple[List[Tuple], List[Tuple]]:
    """
    Find swing highs and swing lows.
    Returns: (swing_lows, swing_highs) as lists of (index, price, volume) tuples
    """
    swing_lows = []
    swing_highs = []

    for i in range(window, len(df) - window):
        # Check for swing low
        is_swing_low = True
        for j in range(1, window + 1):
            if df['low'].iloc[i] >= df['low'].iloc[i - j] or df['low'].iloc[i] >= df['low'].iloc[i + j]:
                is_swing_low = False
                break
        if is_swing_low:
            swing_lows.append((df.index[i], df['low'].iloc[i], df['volume'].iloc[i]))

        # Check for swing high
        is_swing_high = True
        for j in range(1, window + 1):
            if df['high'].iloc[i] <= df['high'].iloc[i - j] or df['high'].iloc[i] <= df['high'].iloc[i + j]:
                is_swing_high = False
                break
        if is_swing_high:
            swing_highs.append((df.index[i], df['high'].iloc[i], df['volume'].iloc[i]))

    return swing_lows, swing_highs

def cluster_levels(points: List[Tuple], tolerance_pct: float = 1.5) -> List[Dict]:
    """
    Cluster nearby price points into levels.
    Returns list of {price, touches, dates, volumes}
    """
    if not points:
        return []

    # Sort by price
    sorted_points = sorted(points, key=lambda x: x[1])

    clusters = []
    current_cluster = [sorted_points[0]]

    for point in sorted_points[1:]:
        # Check if point is within tolerance of current cluster's average
        cluster_avg = np.mean([p[1] for p in current_cluster])
        if abs(point[1] - cluster_avg) / cluster_avg * 100 <= tolerance_pct:
            current_cluster.append(point)
        else:
            # Save current cluster and start new one
            if current_cluster:
                clusters.append({
                    'price': np.mean([p[1] for p in current_cluster]),
                    'touches': len(current_cluster),
                    'dates': [p[0] for p in current_cluster],
                    'volumes': [p[2] for p in current_cluster]
                })
            current_cluster = [point]

    # Don't forget last cluster
    if current_cluster:
        clusters.append({
            'price': np.mean([p[1] for p in current_cluster]),
            'touches': len(current_cluster),
            'dates': [p[0] for p in current_cluster],
            'volumes': [p[2] for p in current_cluster]
        })

    return clusters

def detect_polarity_flip(df: pd.DataFrame, level_price: float,
                         level_type: str, tolerance_pct: float = 1.5) -> bool:
    """
    Detect if a level has flipped polarity (resistance -> support or vice versa).
    """
    tolerance = level_price * tolerance_pct / 100

    # Find touches at this level
    touches_below = df[df['high'].between(level_price - tolerance, level_price + tolerance)]
    touches_above = df[df['low'].between(level_price - tolerance, level_price + tolerance)]

    if level_type == 'support':
        # For current support, check if it was resistance before
        # Look for earlier touches from below (resistance) followed by later touches from above (support)
        if len(touches_below) > 0 and len(touches_above) > 0:
            earliest_below = touches_below.index.min()
            latest_above = touches_above.index.max()
            if earliest_below < latest_above:
                return True
    else:
        # For current resistance, check if it was support before
        if len(touches_above) > 0 and len(touches_below) > 0:
            earliest_above = touches_above.index.min()
            latest_below = touches_below.index.max()
            if earliest_above < latest_below:
                return True

    return False

def is_near_round_number(price: float) -> bool:
    """Check if price is within 1% of a round number."""
    round_numbers = CONFIG['round_numbers']

    if price < 500:
        levels = round_numbers['below_500']
    elif price < 1000:
        levels = round_numbers['500_to_1000']
    elif price < 2000:
        levels = round_numbers['1000_to_2000']
    elif price < 5000:
        levels = round_numbers['2000_to_5000']
    else:
        levels = round_numbers['above_5000']

    for level in levels:
        if abs(price - level) / level * 100 <= 1.0:
            return True
    return False

def score_level(level: Level, df: pd.DataFrame) -> int:
    """Score a support/resistance level based on quality."""
    scoring = CONFIG['scoring']
    score = 0

    # Touches scoring
    if level.touches >= 4:
        score += scoring['touches_4_plus']
    elif level.touches >= 3:
        score += scoring['touches_3']
    elif level.touches >= 2:
        score += scoring['touches_2']

    # Polarity flip bonus
    if level.is_polarity_flip:
        score += scoring['resistance_turned_support']

    # Round number confluence
    if is_near_round_number(level.price):
        score += scoring['round_number_confluence']

    # Recent touch bonus (within 30 days)
    recency_days = CONFIG['support_resistance']['recency_bonus_days']
    if (datetime.now() - level.last_touch.to_pydatetime().replace(tzinfo=None)).days <= recency_days:
        score += scoring['recent_touch']

    # High volume at level
    avg_volume = df['volume'].mean()
    if level.volume_at_touches > avg_volume * 1.5:
        score += scoring['high_volume_at_level']

    return score

def find_levels(df: pd.DataFrame, min_touches: int = 2) -> List[Level]:
    """
    Main function to find all support and resistance levels.
    """
    config = CONFIG['support_resistance']
    tolerance = config['touch_tolerance_pct']

    # Find swing points
    swing_lows, swing_highs = find_swing_points(df)

    # Cluster into levels
    support_clusters = cluster_levels(swing_lows, tolerance)
    resistance_clusters = cluster_levels(swing_highs, tolerance)

    levels = []

    # Process support levels
    for cluster in support_clusters:
        if cluster['touches'] >= min_touches:
            is_flip = detect_polarity_flip(df, cluster['price'], 'support', tolerance)
            level = Level(
                price=round(cluster['price'], 2),
                level_type='support',
                touches=cluster['touches'],
                first_touch=min(cluster['dates']),
                last_touch=max(cluster['dates']),
                is_polarity_flip=is_flip,
                volume_at_touches=np.mean(cluster['volumes'])
            )
            level.score = score_level(level, df)
            levels.append(level)

    # Process resistance levels
    for cluster in resistance_clusters:
        if cluster['touches'] >= min_touches:
            is_flip = detect_polarity_flip(df, cluster['price'], 'resistance', tolerance)
            level = Level(
                price=round(cluster['price'], 2),
                level_type='resistance',
                touches=cluster['touches'],
                first_touch=min(cluster['dates']),
                last_touch=max(cluster['dates']),
                is_polarity_flip=is_flip,
                volume_at_touches=np.mean(cluster['volumes'])
            )
            level.score = score_level(level, df)
            levels.append(level)

    # Sort by score
    levels.sort(key=lambda x: x.score, reverse=True)

    return levels

# ============================================================================
# SETUP IDENTIFICATION
# ============================================================================

def get_nearest_expiry(kite: KiteConnect, symbol: str) -> Optional[str]:
    """Get the nearest monthly expiry for a stock."""
    try:
        instruments = kite.instruments('NFO')
        stock_opts = [i for i in instruments
                      if i['name'] == symbol
                      and i['instrument_type'] == 'CE'
                      and i['expiry'] is not None]

        if not stock_opts:
            return None

        # Get unique expiries
        expiries = sorted(set(i['expiry'] for i in stock_opts))

        # Find expiry with DTE >= min_dte
        min_dte = CONFIG['entry_rules']['min_dte_stocks']
        today = datetime.now().date()

        for exp in expiries:
            dte = (exp - today).days
            if dte >= min_dte:
                # Format as YYMMMDD (e.g., 26JAN30)
                return exp.strftime('%y%b%d').upper()

        return None
    except Exception as e:
        print(f"Error getting expiry for {symbol}: {e}")
        return None

def get_strike_interval(kite: KiteConnect, symbol: str) -> int:
    """Determine strike interval for a stock."""
    try:
        instruments = kite.instruments('NFO')
        stock_opts = [i for i in instruments
                      if i['name'] == symbol
                      and i['instrument_type'] == 'CE']

        if len(stock_opts) < 2:
            return 10  # Default

        strikes = sorted(set(i['strike'] for i in stock_opts))
        if len(strikes) < 2:
            return 10

        # Find most common interval
        intervals = [strikes[i+1] - strikes[i] for i in range(len(strikes)-1)]
        return int(min(set(intervals), key=intervals.count))
    except:
        return 10

def round_to_strike(price: float, interval: int, direction: str = 'nearest') -> int:
    """Round price to nearest valid strike."""
    if direction == 'down':
        return int(price // interval * interval)
    elif direction == 'up':
        return int((price // interval + 1) * interval)
    else:
        return int(round(price / interval) * interval)

def identify_setups(kite: KiteConnect, symbol: str, df: pd.DataFrame,
                    levels: List[Level], ltp: float) -> List[Setup]:
    """
    Identify trade setups where price is near high-scoring levels.
    Uses ATM + N OTM for strike selection (simpler, more liquid).
    """
    setups = []
    max_distance = CONFIG['entry_rules']['max_distance_from_level_pct']
    min_score = CONFIG['scoring']['min_score_to_trade']

    # Get expiry and strike interval
    expiry = get_nearest_expiry(kite, symbol)
    if not expiry:
        return []

    interval = get_strike_interval(kite, symbol)

    # Get spread config
    spread_cfg = CONFIG.get('spread_config', {})
    otm_offset = spread_cfg.get('default_otm_offset', 2)

    for level in levels:
        # Check if score meets minimum
        if level.score < min_score:
            continue

        # Calculate distance from current price
        distance_pct = abs(ltp - level.price) / level.price * 100

        # Check if within trading range
        if distance_pct > max_distance:
            continue

        # Determine direction and strikes
        # Use ATM for long strike, ATM + N*interval for short
        atm_strike = round_to_strike(ltp, interval, 'nearest')

        if level.level_type == 'support' and ltp >= level.price:
            direction = 'BULLISH'
            # Bull Call Spread: Long ATM, Short ATM + N OTM
            long_strike = atm_strike
            short_strike = atm_strike + (otm_offset * interval)
            strategy = f"Bull Call Spread {long_strike}/{short_strike}"

        elif level.level_type == 'resistance' and ltp <= level.price:
            direction = 'BEARISH'
            # Bear Put Spread: Long ATM, Short ATM - N OTM
            long_strike = atm_strike
            short_strike = atm_strike - (otm_offset * interval)
            strategy = f"Bear Put Spread {long_strike}/{short_strike}"
        else:
            continue

        setup = Setup(
            symbol=symbol,
            ltp=ltp,
            level=level,
            distance_pct=round(distance_pct, 2),
            direction=direction,
            score=level.score,
            suggested_strategy=strategy,
            long_strike=int(long_strike),
            short_strike=int(short_strike),
            expiry=expiry
        )
        setups.append(setup)

    return setups

# ============================================================================
# MAIN SCANNER
# ============================================================================

def scan_stock(kite: KiteConnect, symbol: str) -> List[Setup]:
    """Scan a single stock for setups."""
    print(f"  Scanning {symbol}...", end=' ')

    # Get historical data
    df = get_historical_data(kite, symbol, CONFIG['data_settings']['ohlc_lookback_days'])
    if df is None or len(df) < 50:
        print("Insufficient data")
        return []

    # Get current price
    try:
        ltp_data = kite.ltp([f'NSE:{symbol}'])
        ltp = ltp_data[f'NSE:{symbol}']['last_price']
    except:
        print("LTP fetch failed")
        return []

    # Find S/R levels
    levels = find_levels(df, CONFIG['support_resistance']['min_touches'])

    # Identify setups
    setups = identify_setups(kite, symbol, df, levels, ltp)

    if setups:
        print(f"Found {len(setups)} setup(s)")
    else:
        print("No setups")

    return setups

def run_scanner():
    """Main scanner function."""
    print("="*70)
    print("SUPPORT/RESISTANCE SCANNER")
    print("="*70)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # Initialize Kite
    print("Connecting to Kite...")
    kite = get_kite()
    print("Connected!")
    print()

    # Get stock universe (only active stocks for faster, more liquid scans)
    stocks = CONFIG['stock_universe'].get('active_stocks', [])
    exclude = CONFIG['stock_universe'].get('exclude', [])
    stocks = [s for s in stocks if s not in exclude]

    print(f"Scanning {len(stocks)} stocks...")
    print("-"*70)

    all_setups = []

    for symbol in stocks:
        try:
            setups = scan_stock(kite, symbol)
            all_setups.extend(setups)
        except Exception as e:
            print(f"  Error scanning {symbol}: {e}")

    print("-"*70)
    print()

    # Sort setups by score
    all_setups.sort(key=lambda x: x.score, reverse=True)

    # Display results
    print("="*70)
    print("SCAN RESULTS")
    print("="*70)
    print()

    if not all_setups:
        print("No setups found meeting criteria.")
        return []

    print(f"Found {len(all_setups)} potential setup(s):")
    print()

    for i, setup in enumerate(all_setups, 1):
        level = setup.level

        print(f"SETUP #{i}: {setup.symbol}")
        print("-"*50)
        print(f"  Direction:     {setup.direction}")
        print(f"  Current LTP:   Rs {setup.ltp:.2f}")
        print(f"  Level:         Rs {level.price:.2f} ({level.level_type.upper()})")
        print(f"  Distance:      {setup.distance_pct:.2f}%")
        print(f"  Score:         {setup.score} points")
        print(f"  Touches:       {level.touches}")
        print(f"  Last Touch:    {level.last_touch.strftime('%Y-%m-%d')}")
        print(f"  Polarity Flip: {'YES' if level.is_polarity_flip else 'No'}")
        print(f"  Strategy:      {setup.suggested_strategy}")
        print(f"  Expiry:        {setup.expiry}")
        print()

    # Top recommendation
    if all_setups:
        top = all_setups[0]
        print("="*70)
        print("TOP RECOMMENDATION")
        print("="*70)
        print()
        print(f"  Stock:    {top.symbol}")
        print(f"  Setup:    {top.direction} at {top.level.level_type.upper()}")
        print(f"  Level:    Rs {top.level.price:.2f} (Score: {top.score})")
        print(f"  Strategy: {top.suggested_strategy}")
        print()

        # Print trade command
        opt_type = 'CE' if top.direction == 'BULLISH' else 'PE'
        print("  Suggested Trade:")
        print(f"    BUY  {top.symbol}{top.expiry}{top.long_strike}{opt_type}")
        print(f"    SELL {top.symbol}{top.expiry}{top.short_strike}{opt_type}")
        print()

    return all_setups

# ============================================================================
# SINGLE STOCK ANALYSIS
# ============================================================================

def analyze_stock(symbol: str):
    """Deep analysis of a single stock."""
    print("="*70)
    print(f"DETAILED ANALYSIS: {symbol}")
    print("="*70)
    print()

    kite = get_kite()

    # Get data
    df = get_historical_data(kite, symbol, 365)
    if df is None:
        print("Failed to fetch data")
        return

    # Get LTP
    ltp_data = kite.ltp([f'NSE:{symbol}'])
    ltp = ltp_data[f'NSE:{symbol}']['last_price']

    print(f"Current Price: Rs {ltp:.2f}")
    print(f"52W High: Rs {df['high'].max():.2f}")
    print(f"52W Low: Rs {df['low'].min():.2f}")
    print()

    # Find levels
    levels = find_levels(df, min_touches=2)

    print("KEY SUPPORT/RESISTANCE LEVELS:")
    print("-"*70)
    print(f"{'Level':>10} {'Type':>12} {'Touches':>8} {'Score':>8} {'Flip':>6} {'Last Touch':>12}")
    print("-"*70)

    for level in levels[:15]:  # Top 15 levels
        print(f"{level.price:>10.2f} {level.level_type:>12} {level.touches:>8} "
              f"{level.score:>8} {'YES' if level.is_polarity_flip else 'No':>6} "
              f"{level.last_touch.strftime('%Y-%m-%d'):>12}")

    print()

    # Find setups
    setups = identify_setups(kite, symbol, df, levels, ltp)

    if setups:
        print("ACTIVE SETUPS:")
        print("-"*70)
        for setup in setups:
            print(f"  {setup.direction} - {setup.suggested_strategy}")
            print(f"  Level: {setup.level.price:.2f}, Score: {setup.score}, Distance: {setup.distance_pct:.2f}%")
            print()
    else:
        print("No active setups at current price.")

        # Show nearest levels
        supports = [l for l in levels if l.level_type == 'support' and l.price < ltp]
        resistances = [l for l in levels if l.level_type == 'resistance' and l.price > ltp]

        if supports:
            nearest_support = max(supports, key=lambda x: x.price)
            dist = (ltp - nearest_support.price) / nearest_support.price * 100
            print(f"Nearest Support: Rs {nearest_support.price:.2f} ({dist:.1f}% below, Score: {nearest_support.score})")

        if resistances:
            nearest_resistance = min(resistances, key=lambda x: x.price)
            dist = (nearest_resistance.price - ltp) / ltp * 100
            print(f"Nearest Resistance: Rs {nearest_resistance.price:.2f} ({dist:.1f}% above, Score: {nearest_resistance.score})")

# ============================================================================
# CLI
# ============================================================================

if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1:
        # Single stock analysis
        symbol = sys.argv[1].upper()
        analyze_stock(symbol)
    else:
        # Full scan
        run_scanner()
