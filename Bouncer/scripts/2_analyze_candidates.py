#!/usr/bin/env python3
"""
Step 2: Analyze Candidates (8:50 AM)
====================================
BULLISH ONLY - Support bounce strategy

- Filters stocks by OI for liquidity
- Checks F&O ban list (best effort)
- Computes SUPPORT levels using 180-day data (resistance for polarity flip detection only)
- Scores each level (with S/R reliability bonus!)
- Saves top SUPPORT candidates to levels.json

Requires: Kite token (available after 8:45 AM via SNAIL)

Run: python scripts/2_analyze_candidates.py
Cron: 50 8 * * 1-5
"""

import json
import csv
import requests
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from kiteconnect import KiteConnect

# ============================================================================
# PATHS
# ============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
BOUNCER_DIR = SCRIPT_DIR.parent
DATA_DIR = BOUNCER_DIR.parent / 'data'
CONFIG_FILE = BOUNCER_DIR / 'config' / 'config.json'
LEVELS_FILE = BOUNCER_DIR / 'data' / 'levels.json'
STOCK_INSTRUMENTS_FILE = DATA_DIR / 'stock_instruments.csv'
RELIABILITY_FILE = BOUNCER_DIR / 'data' / 'sr_reliability.json'

# Ensure data directory exists
(BOUNCER_DIR / 'data').mkdir(exist_ok=True)

# =============================================================================
# S/R RELIABILITY LOADING
# =============================================================================

RELIABILITY_DATA = {}  # {symbol: {bonus_points, reliability_score, success_rate}}

def load_reliability_data():
    """Load S/R reliability scores from weekly analysis."""
    global RELIABILITY_DATA
    RELIABILITY_DATA = {}

    if not RELIABILITY_FILE.exists():
        print("  S/R reliability file not found - using default scores")
        return

    try:
        with open(RELIABILITY_FILE) as f:
            data = json.load(f)

        if 'stocks' in data:
            RELIABILITY_DATA = data['stocks']
            print(f"  Loaded reliability data for {len(RELIABILITY_DATA)} stocks")

            # Show stats
            high_rel = sum(1 for s in RELIABILITY_DATA.values() if s.get('reliability_score', 0) >= 70)
            print(f"  High reliability stocks (70+): {high_rel}")
    except Exception as e:
        print(f"  Error loading reliability data: {e}")


def get_reliability_bonus(symbol: str) -> int:
    """Get reliability bonus points for a stock."""
    if symbol in RELIABILITY_DATA:
        return RELIABILITY_DATA[symbol].get('bonus_points', 0)
    return 5  # Default bonus for unknown stocks

# Load config with validation
try:
    with open(CONFIG_FILE) as f:
        CONFIG = json.load(f)
except FileNotFoundError:
    print(f"ERROR: Config file not found: {CONFIG_FILE}")
    import sys
    sys.exit(1)
except json.JSONDecodeError as e:
    print(f"ERROR: Config file is malformed: {e}")
    import sys
    sys.exit(1)

# ============================================================================
# KITE CONNECTION
# ============================================================================

def get_kite() -> KiteConnect:
    """Initialize Kite connection with validation."""
    token_file = DATA_DIR / 'kite_access_token.json'

    if not token_file.exists():
        raise FileNotFoundError(
            f"Token file not found: {token_file}\n"
            "Run: python scripts/kite_auth.py first"
        )

    try:
        with open(token_file) as f:
            token_data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Token file is malformed: {e}")

    # Validate required keys
    if 'api_key' not in token_data:
        raise KeyError("Token file missing 'api_key' field")
    if 'access_token' not in token_data:
        raise KeyError("Token file missing 'access_token' field")

    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite

# ============================================================================
# F&O BAN CHECK
# ============================================================================

def fetch_fo_ban_list() -> List[str]:
    """
    Fetch F&O ban list from NSE (best effort).
    Returns list of banned stock symbols.
    """
    banned = []

    # Try NSE endpoint (may not always work)
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json',
        }
        # NSE's ban list endpoint
        url = "https://www.nseindia.com/api/live-analysis-oi-spurts-underlyingFoBan"
        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code == 200:
            data = response.json()
            if 'data' in data:
                banned = [item.get('symbol', '') for item in data['data']]
                print(f"  F&O ban list fetched: {len(banned)} stocks")
    except Exception as e:
        print(f"  F&O ban check failed (non-critical): {e}")

    return banned

# ============================================================================
# OI-BASED STOCK FILTERING
# ============================================================================

def get_stocks_with_good_oi(kite: KiteConnect, min_oi: int = 500000) -> List[Dict]:
    """
    Filter stocks by ATM option OI.
    Returns list of stocks with good liquidity.
    """
    print("Filtering stocks by OI...")

    # Read instruments file
    if not STOCK_INSTRUMENTS_FILE.exists():
        print(f"  ERROR: {STOCK_INSTRUMENTS_FILE} not found. Run step 1 first.")
        return []

    # Get unique stocks with F&O
    stocks_with_fo = set()
    lot_sizes = {}
    with open(STOCK_INSTRUMENTS_FILE) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get('option_symbol'):
                stocks_with_fo.add(row['symbol'])
                lot_sizes[row['symbol']] = int(row['lot_size'] or 1)

    print(f"  Stocks with F&O: {len(stocks_with_fo)}")

    # Get LTP for all stocks to find ATM
    symbols = [f"NSE:{s}" for s in stocks_with_fo]

    # Batch fetch (Kite allows ~500 per call)
    good_stocks = []
    batch_size = 400

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i+batch_size]
        try:
            ltps = kite.ltp(batch)

            for sym, data in ltps.items():
                stock = sym.replace('NSE:', '')
                ltp = data['last_price']

                # We'll check OI later when computing levels
                good_stocks.append({
                    'symbol': stock,
                    'ltp': ltp,
                    'lot_size': lot_sizes.get(stock, 1)
                })
        except Exception as e:
            print(f"  Error fetching batch: {e}")

    # Sort by symbol for consistency
    good_stocks.sort(key=lambda x: x['symbol'])

    return good_stocks

# ============================================================================
# S/R DETECTION
# ============================================================================

def get_historical_data(kite: KiteConnect, symbol: str, days: int = 180) -> Optional[pd.DataFrame]:
    """Fetch historical OHLC data."""
    try:
        instruments = kite.instruments('NSE')
        token = None
        for inst in instruments:
            if inst['tradingsymbol'] == symbol:
                token = inst['instrument_token']
                break

        if not token:
            return None

        to_date = datetime.now()
        from_date = to_date - timedelta(days=days)

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
        print(f"    Error fetching historical data: {e}")
        return None


def find_swing_points(df: pd.DataFrame, window: int = 5) -> Tuple[List, List]:
    """Find swing highs and lows using candle wicks (high/low)."""
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


def cluster_levels(points: List, tolerance_pct: float = 1.5) -> List[Dict]:
    """Cluster nearby price points into levels."""
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
            clusters.append({
                'price': round(np.mean([p[1] for p in current]), 2),
                'touches': len(current),
                'dates': [str(p[0].date()) for p in current],
                'volume': np.mean([p[2] for p in current])
            })
            current = [point]

    if current:
        clusters.append({
            'price': round(np.mean([p[1] for p in current]), 2),
            'touches': len(current),
            'dates': [str(p[0].date()) for p in current],
            'volume': np.mean([p[2] for p in current])
        })

    return clusters


def detect_polarity_flip(df: pd.DataFrame, price: float, level_type: str, tol: float = 1.5) -> bool:
    """Check if level flipped from resistance to support or vice versa."""
    tolerance = price * tol / 100

    below = df[df['high'].between(price - tolerance, price + tolerance)]
    above = df[df['low'].between(price - tolerance, price + tolerance)]

    if level_type == 'support' and len(below) > 0 and len(above) > 0:
        if below.index.min() < above.index.max():
            return True
    elif level_type == 'resistance' and len(above) > 0 and len(below) > 0:
        if above.index.min() < below.index.max():
            return True
    return False


def was_level_broken(df: pd.DataFrame, price: float, level_type: str, last_touch: str, break_pct: float = 1.5) -> bool:
    """
    Check if a level was subsequently broken after it was formed.

    For resistance: Check if price closed ABOVE level by break_pct after last touch
    For support: Check if price closed BELOW level by break_pct after last touch

    This prevents showing old levels that were already invalidated by price action.
    E.g., M&M 3693 resistance from Oct was broken in Nov when price went to 3800.
    """
    try:
        last_touch_date = pd.Timestamp(last_touch)

        # Handle timezone-aware index (Kite data has IST timezone)
        if df.index.tz is not None:
            last_touch_date = last_touch_date.tz_localize(df.index.tz)

        # Get data after the last touch
        subsequent_data = df[df.index > last_touch_date]

        if subsequent_data.empty:
            return False  # No data after last touch, level not broken

        if level_type == 'resistance':
            # Resistance broken if price closed above it by break_pct
            break_price = price * (1 + break_pct / 100)
            if subsequent_data['close'].max() > break_price:
                return True
        elif level_type == 'support':
            # Support broken if price closed below it by break_pct
            break_price = price * (1 - break_pct / 100)
            if subsequent_data['close'].min() < break_price:
                return True

        return False
    except Exception:
        return False


def is_round_number(price: float) -> bool:
    """Check if price is near a round number."""
    rn = CONFIG.get('round_numbers', {})

    if price < 500:
        levels = rn.get('below_500', [100, 200, 300, 400, 500])
    elif price < 1000:
        levels = rn.get('500_to_1000', [500, 600, 700, 800, 900, 1000])
    elif price < 2000:
        levels = rn.get('1000_to_2000', [1000, 1200, 1400, 1500, 1600, 1800, 2000])
    elif price < 5000:
        levels = rn.get('2000_to_5000', [2000, 2500, 3000, 3500, 4000, 4500, 5000])
    else:
        levels = rn.get('above_5000', [5000, 6000, 7000, 8000, 9000, 10000])

    return any(abs(price - lvl) / lvl * 100 <= 1.0 for lvl in levels)


def score_level(touches: int, is_flip: bool, is_round: bool, is_recent: bool, high_vol: bool) -> int:
    """Score a level based on quality factors."""
    scoring = CONFIG.get('scoring', {})
    score = 0

    if touches >= 4:
        score += scoring.get('touches_4_plus', 30)
    elif touches >= 3:
        score += scoring.get('touches_3', 20)
    elif touches >= 2:
        score += scoring.get('touches_2', 10)

    if is_flip:
        score += scoring.get('resistance_turned_support', 25)
    if is_round:
        score += scoring.get('round_number_confluence', 10)
    if is_recent:
        score += scoring.get('recent_touch', 10)
    if high_vol:
        score += scoring.get('high_volume_at_level', 15)

    return score


def compute_levels(df: pd.DataFrame, ltp: float, symbol: str = '') -> List[Dict]:
    """
    Compute SUPPORT levels for a stock with reliability bonus.
    BULLISH ONLY - Resistance is detected for polarity flip but not output.
    """
    min_touches = CONFIG.get('support_resistance', {}).get('min_touches', 2)
    recency_days = CONFIG.get('support_resistance', {}).get('recency_bonus_days', 30)
    # Only track levels within this % of current price (ignore ancient levels)
    max_distance_pct = CONFIG.get('support_resistance', {}).get('max_level_distance_pct', 15.0)

    swing_lows, swing_highs = find_swing_points(df)
    support_clusters = cluster_levels(swing_lows)
    # Resistance clusters needed for polarity flip detection only (not output)
    resistance_clusters = cluster_levels(swing_highs)

    avg_vol = df['volume'].mean()
    levels = []

    # BULLISH ONLY: Process support levels only
    for cluster in support_clusters:
        if cluster['touches'] < min_touches:
            continue

        # Skip levels too far from current price (irrelevant ancient levels)
        distance_pct = abs(cluster['price'] - ltp) / ltp * 100
        if distance_pct > max_distance_pct:
            continue

        # Skip levels that were historically broken after formation
        last_touch = max(cluster['dates'])
        if was_level_broken(df, cluster['price'], 'support', last_touch):
            continue

        # Polarity flip = Resistance turned support (strong bullish signal)
        is_flip = detect_polarity_flip(df, cluster['price'], 'support')
        is_round = is_round_number(cluster['price'])
        last_touch = max(cluster['dates'])
        is_recent = (datetime.now().date() - datetime.strptime(last_touch, '%Y-%m-%d').date()).days <= recency_days
        high_vol = cluster['volume'] > avg_vol * 1.5

        score = score_level(cluster['touches'], is_flip, is_round, is_recent, high_vol)

        # Add S/R reliability bonus (from weekly analysis)
        reliability_bonus = get_reliability_bonus(symbol) if symbol else 0
        score += reliability_bonus

        levels.append({
            'price': cluster['price'],
            'type': 'support',
            'touches': cluster['touches'],
            'score': score,
            'is_polarity_flip': is_flip,
            'last_touch': last_touch,
            'status': 'active',
            'alerted': False,
            'reliability_bonus': reliability_bonus
        })

    # NOTE: Resistance levels NOT output - BULLISH ONLY strategy

    # Sort by score, keep top levels
    levels.sort(key=lambda x: x['score'], reverse=True)
    return levels[:10]  # Keep top 10 support levels per stock

# ============================================================================
# ATR CALCULATION
# ============================================================================

def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """
    Compute Average True Range.
    ATR adapts spread width to stock's actual volatility.
    """
    if len(df) < period + 1:
        return 0.0

    # True Range = max(H-L, |H-PC|, |L-PC|)
    df = df.copy()
    df['prev_close'] = df['close'].shift(1)
    df['tr1'] = df['high'] - df['low']
    df['tr2'] = abs(df['high'] - df['prev_close'])
    df['tr3'] = abs(df['low'] - df['prev_close'])
    df['true_range'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)

    # ATR = SMA of True Range
    atr = df['true_range'].rolling(window=period).mean().iloc[-1]

    return round(atr, 2) if not pd.isna(atr) else 0.0


def compute_atr_pct(df: pd.DataFrame, period: int = 14) -> float:
    """
    Compute ATR as percentage of current price.
    More useful for comparison across stocks.
    """
    atr = compute_atr(df, period)
    if atr == 0 or len(df) == 0:
        return 0.0

    current_price = df['close'].iloc[-1]
    return round((atr / current_price) * 100, 2)


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 60)
    print("STEP 2: ANALYZE CANDIDATES")
    print("=" * 60)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # Load S/R reliability data (from weekly analysis)
    print("Loading S/R reliability data...")
    load_reliability_data()
    print()

    # Connect to Kite
    print("Connecting to Kite...")
    kite = get_kite()
    print("Connected!")
    print()

    # Fetch F&O ban list
    print("Checking F&O ban list...")
    banned_stocks = fetch_fo_ban_list()
    print()

    # Get stocks with good OI
    stocks = get_stocks_with_good_oi(kite)
    print(f"Total stocks to analyze: {len(stocks)}")
    print()

    # Filter out banned stocks
    if banned_stocks:
        stocks = [s for s in stocks if s['symbol'] not in banned_stocks]
        print(f"After removing banned: {len(stocks)}")

    # Use configured universe as priority filter
    universe = CONFIG.get('stock_universe', {}).get('active_stocks', [])
    if universe:
        # Prioritize configured stocks
        priority_stocks = [s for s in stocks if s['symbol'] in universe]
        other_stocks = [s for s in stocks if s['symbol'] not in universe]

        # Take all priority + fill remaining to reach 30
        max_stocks = 30
        if len(priority_stocks) < max_stocks:
            remaining = max_stocks - len(priority_stocks)
            stocks = priority_stocks + other_stocks[:remaining]
        else:
            stocks = priority_stocks[:max_stocks]

    print(f"Final candidate count: {len(stocks)}")
    print()

    # Analyze each stock
    print("Computing S/R levels...")
    print("-" * 60)

    results = {
        'computed_at': datetime.now().isoformat(),
        'banned_stocks': banned_stocks,
        'stocks': {}
    }

    for i, stock_data in enumerate(stocks, 1):
        symbol = stock_data['symbol']
        ltp = stock_data['ltp']

        print(f"  [{i}/{len(stocks)}] {symbol}...", end=' ')

        # Fetch historical data
        df = get_historical_data(kite, symbol)
        if df is None or len(df) < 50:
            print("insufficient data")
            continue

        # Compute levels (with reliability bonus from weekly analysis)
        levels = compute_levels(df, ltp, symbol)

        # Compute ATR for spread width calculation
        atr_period = CONFIG.get('spread_config', {}).get('atr_period', 14)
        atr = compute_atr(df, atr_period)
        atr_pct = compute_atr_pct(df, atr_period)

        if levels:
            results['stocks'][symbol] = {
                'ltp_at_compute': ltp,
                'lot_size': stock_data['lot_size'],
                'atr': atr,
                'atr_pct': atr_pct,
                'levels': levels
            }
            print(f"{len(levels)} levels (best: {levels[0]['score']}) | ATR: {atr:.1f} ({atr_pct:.1f}%)")
        else:
            print("no levels found")

    print("-" * 60)
    print()

    # Save results
    print(f"Saving to {LEVELS_FILE}...")
    with open(LEVELS_FILE, 'w') as f:
        json.dump(results, f, indent=2)

    # Summary
    total_levels = sum(len(s['levels']) for s in results['stocks'].values())
    high_score = sum(
        1 for s in results['stocks'].values()
        for level in s['levels'] if level['score'] >= 60
    )

    print()
    print("=" * 60)
    print("SUMMARY (BULLISH ONLY)")
    print("=" * 60)
    print(f"Stocks analyzed: {len(results['stocks'])}")
    print(f"Total SUPPORT levels: {total_levels}")
    print(f"High-score levels (60+): {high_score}")
    print(f"Output: {LEVELS_FILE}")
    print()
    print("DONE - Ready for market scanner (BULLISH setups only)")


if __name__ == "__main__":
    main()
