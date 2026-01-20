#!/usr/bin/env python3
"""
Options Opportunity Scanner

Scans BANKNIFTY, NIFTY, SENSEX for best reversal zone opportunities.
Automatically calculates ATM/OTM strikes and ranks trading setups.

Usage:
    python opportunity_scanner.py
    python opportunity_scanner.py --refresh    # Force refresh instruments
"""

import json
import csv
import argparse
import pickle
from pathlib import Path
from datetime import datetime, timedelta, date
from collections import defaultdict
from typing import Dict, List, Optional
from kiteconnect import KiteConnect

# =============================================================================
# PATHS
# =============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
HELPER_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == 'helper' else SCRIPT_DIR
BOTS_DIR = HELPER_DIR.parent
DATA_DIR = BOTS_DIR / 'data'
CACHE_DIR = HELPER_DIR / 'data' / 'cache'

TOKEN_FILE = DATA_DIR / 'kite_access_token.json'
CACHE_FILE = CACHE_DIR / 'options_cache.pkl'

CACHE_DIR.mkdir(parents=True, exist_ok=True)

# =============================================================================
# CONFIGURATION
# =============================================================================

INDICES = {
    'BANKNIFTY': {
        'spot_symbol': 'NSE:NIFTY BANK',
        'strike_interval': 100,
        'round_to': 1000,  # Round ATM to nearest 1000
    },
    'NIFTY': {
        'spot_symbol': 'NSE:NIFTY 50',
        'strike_interval': 50,
        'round_to': 100,  # Round ATM to nearest 100
    },
    'SENSEX': {
        'spot_symbol': 'BSE:SENSEX',
        'strike_interval': 100,
        'round_to': 1000,  # Round ATM to nearest 1000
    }
}

# Scoring weights (total = 100)
SCORING = {
    'bounces_weight': 40,       # More bounces = stronger zone
    'strength_weight': 20,      # Bounce strength (how well price recovered)
    'proximity_weight': 20,     # Closer to LTP = more tradeable
    'freshness_weight': 10,     # Recent touches
    'risk_reward_weight': 10,   # Tight stop vs potential gain
}

# Filters
MIN_BOUNCES = 5
MAX_DISTANCE_PCT = 20  # Only show zones within 20% of LTP
BUFFER_PCT = 2.0       # 2% buffer for entry


# =============================================================================
# INSTRUMENT CACHE
# =============================================================================

def should_refresh_cache() -> bool:
    """Check if cache needs refresh (older than today)."""
    if not CACHE_FILE.exists():
        return True

    cache_time = datetime.fromtimestamp(CACHE_FILE.stat().st_mtime)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    return cache_time < today


def load_cached_instruments() -> Optional[Dict]:
    """Load instruments from cache."""
    if not CACHE_FILE.exists():
        return None

    try:
        with open(CACHE_FILE, 'rb') as f:
            cache = pickle.load(f)
        return cache
    except:
        return None


def save_instruments_cache(instruments: Dict):
    """Save instruments to cache."""
    with open(CACHE_FILE, 'wb') as f:
        pickle.dump(instruments, f)


def fetch_instruments(kite: KiteConnect, force_refresh: bool = False) -> Dict:
    """
    Fetch and cache option instruments.

    Returns dict mapping (index, strike, option_type, expiry) -> instrument details
    """

    # Check cache first
    if not force_refresh:
        cached = load_cached_instruments()
        if cached:
            print("Using cached instruments (from today)")
            return cached

    print("Downloading instruments from Kite...")

    instruments = {}

    # Fetch from NFO (NIFTY, BANKNIFTY) and BFO (SENSEX)
    for exchange in ['NFO', 'BFO']:
        exchange_instruments = kite.instruments(exchange)

        for inst in exchange_instruments:
            if inst['instrument_type'] not in ('CE', 'PE'):
                continue

            index_name = inst['name']
            if index_name not in INDICES:
                continue

            key = (
                index_name,
                inst['strike'],
                inst['instrument_type'],
                inst['expiry']
            )

            instruments[key] = {
                'symbol': inst['tradingsymbol'],
                'token': inst['instrument_token'],
                'lot_size': inst['lot_size'],
                'exchange': inst['exchange']
            }

    # Save to cache
    save_instruments_cache(instruments)
    print(f"Cached {len(instruments)} option instruments")

    return instruments


# =============================================================================
# KITE CONNECTION
# =============================================================================

def get_kite() -> KiteConnect:
    with open(TOKEN_FILE) as f:
        token_data = json.load(f)
    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


# =============================================================================
# STRIKE SELECTION
# =============================================================================

def get_monthly_expiry(index_name: str, instruments: Dict) -> Optional[date]:
    """Get the monthly expiry (last Thursday of current month)."""

    # Find all expiries for this index
    expiries = set()
    for (idx, strike, opt_type, expiry) in instruments.keys():
        if idx == index_name:
            expiries.add(expiry)

    if not expiries:
        return None

    # Monthly expiry is typically the last one in the month
    # For NIFTY/BANKNIFTY: Last Thursday
    # For SENSEX: Last Friday

    today = date.today()
    current_month_expiries = [e for e in expiries if e.year == today.year and e.month == today.month]

    if current_month_expiries:
        # Return the latest expiry in current month
        return max(current_month_expiries)

    # If no expiry in current month, get next month's latest
    next_month = today.month + 1 if today.month < 12 else 1
    next_year = today.year if today.month < 12 else today.year + 1

    next_month_expiries = [e for e in expiries if e.year == next_year and e.month == next_month]
    if next_month_expiries:
        return max(next_month_expiries)

    # Fallback: return furthest expiry
    return max(expiries) if expiries else None


def calculate_atm_strike(ltp: float, index_name: str) -> int:
    """Calculate ATM strike rounded to appropriate interval."""
    config = INDICES[index_name]
    round_to = config['round_to']

    # Round to nearest
    atm = round(ltp / round_to) * round_to
    return int(atm)


# =============================================================================
# REVERSAL ZONE ANALYSIS
# =============================================================================

def get_15min_data(kite: KiteConnect, token: int, days: int = 30) -> List[Dict]:
    """Fetch 15-min OHLCV data."""
    data = kite.historical_data(
        instrument_token=token,
        from_date=datetime.now() - timedelta(days=days),
        to_date=datetime.now(),
        interval='15minute'
    )
    return data


def find_reversal_zones(data: List[Dict], ltp: float) -> List[Dict]:
    """Find reversal zones from OHLCV data."""

    # Step 1: Find bounce candles
    bounces = []
    for candle in data:
        o, h, l, c = candle['open'], candle['high'], candle['low'], candle['close']
        candle_range = h - l

        if candle_range <= 0:
            continue

        close_position = (c - l) / candle_range
        lower_wick = min(o, c) - l
        lower_wick_pct = lower_wick / candle_range

        # Bounce criteria
        is_bounce = (close_position >= 0.4) or (lower_wick_pct >= 0.3)

        if is_bounce:
            bounces.append({
                'low': l,
                'close': c,
                'date': candle['date'],
                'strength': close_position
            })

    if not bounces:
        return []

    # Step 2: Group into zones (round to nearest 10)
    round_to = 10
    zone_bounces = defaultdict(list)

    for bounce in bounces:
        rounded = round(bounce['low'] / round_to) * round_to
        zone_bounces[rounded].append(bounce)

    # Step 3: Merge adjacent levels
    sorted_levels = sorted(zone_bounces.keys())
    zones = []
    used = set()

    for level in sorted_levels:
        if level in used:
            continue

        zone_levels = [level]
        next_level = level + round_to

        if next_level in zone_bounces:
            zone_levels.append(next_level)
            used.add(next_level)

        all_bounces = []
        for l in zone_levels:
            all_bounces.extend(zone_bounces[l])

        if len(all_bounces) >= MIN_BOUNCES:
            lows = [b['low'] for b in all_bounces]
            dates = [b['date'] for b in all_bounces]

            zone_low = min(lows)
            zone_high = max(lows)
            zone_center = sum(zone_levels) / len(zone_levels)

            avg_strength = sum(b['strength'] for b in all_bounces) / len(all_bounces)

            # Calculate distance from LTP
            distance_pct = abs(zone_center - ltp) / ltp * 100

            # Skip zones too far from LTP
            if distance_pct > MAX_DISTANCE_PCT:
                continue

            zones.append({
                'price': int(zone_center),
                'zone_low': round(zone_low, 1),
                'zone_high': round(zone_high, 1),
                'bounces': len(all_bounces),
                'avg_strength': avg_strength,
                'last_bounce': max(dates),
                'distance_pct': distance_pct
            })

        used.add(level)

    return zones


# =============================================================================
# OPPORTUNITY SCORING
# =============================================================================

def score_opportunity(zone: Dict, ltp: float) -> float:
    """
    Score an opportunity (0-100).

    Factors:
    - Bounces (more = better)
    - Strength (higher = better)
    - Proximity to LTP (closer = better)
    - Freshness (recent = better)
    - Risk/Reward (tight stop = better)
    """

    # Bounces score (0-40)
    # Normalize: 5 bounces = 10 pts, 50+ bounces = 40 pts
    bounces_score = min(40, (zone['bounces'] / 50) * 40)

    # Strength score (0-20)
    # avg_strength is 0-1, convert to 0-20
    strength_score = zone['avg_strength'] * 20

    # Proximity score (0-20)
    # Closer to LTP = higher score
    # 0% away = 20 pts, 20% away = 0 pts
    proximity_score = max(0, 20 - zone['distance_pct'])

    # Freshness score (0-10)
    # Recent bounce within 7 days = 10 pts, older = less
    days_ago = (datetime.now() - zone['last_bounce'].replace(tzinfo=None)).days
    freshness_score = max(0, 10 - (days_ago / 7) * 10)

    # Risk/Reward score (0-10)
    # Zone size vs distance from LTP
    zone_size = zone['zone_high'] - zone['zone_low']
    buffer = zone['zone_low'] * BUFFER_PCT / 100
    stop_distance = buffer * 2  # Stop is 2 buffers below entry

    if zone_size > 0:
        rr_ratio = stop_distance / zone_size
        # Lower risk (tight stop) = higher score
        risk_reward_score = min(10, (1 / (rr_ratio + 0.1)) * 5)
    else:
        risk_reward_score = 5

    # Total score
    total = (
        bounces_score * SCORING['bounces_weight'] / 40 +
        strength_score * SCORING['strength_weight'] / 20 +
        proximity_score * SCORING['proximity_weight'] / 20 +
        freshness_score * SCORING['freshness_weight'] / 10 +
        risk_reward_score * SCORING['risk_reward_weight'] / 10
    )

    return round(total, 1)


# =============================================================================
# MAIN SCANNER
# =============================================================================

def scan_opportunities(kite: KiteConnect, instruments: Dict, force_refresh: bool = False):
    """Main scanning logic."""

    print("\n" + "="*80)
    print("OPTIONS OPPORTUNITY SCANNER")
    print("="*80)
    print(f"Scan Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # Step 1: Get LTPs for all indices
    print("Fetching index LTPs...")
    index_ltps = {}

    for index_name, config in INDICES.items():
        try:
            quote = kite.quote(config['spot_symbol'])
            ltp = quote[config['spot_symbol']]['last_price']
            index_ltps[index_name] = ltp
            print(f"  {index_name}: {ltp:.2f}")
        except Exception as e:
            print(f"  {index_name}: ERROR - {e}")
            continue

    print()

    # Step 2: Calculate ATM strikes and find monthly expiry
    print("Calculating strikes and expiries...")
    index_config = {}

    for index_name, ltp in index_ltps.items():
        atm = calculate_atm_strike(ltp, index_name)
        expiry = get_monthly_expiry(index_name, instruments)

        if not expiry:
            print(f"  {index_name}: No expiry found")
            continue

        index_config[index_name] = {
            'ltp': ltp,
            'atm': atm,
            'expiry': expiry
        }

        print(f"  {index_name}: ATM {atm}, Expiry {expiry.strftime('%d-%b-%Y')}")

    print()

    # Step 3: Scan each option for reversal zones
    print("Scanning for reversal zones...")
    opportunities = []

    for index_name, config in index_config.items():
        atm = config['atm']
        expiry = config['expiry']
        idx_ltp = config['ltp']

        # Scan CE and PE
        for option_type in ['CE', 'PE']:
            key = (index_name, atm, option_type, expiry)

            if key not in instruments:
                print(f"  {index_name} {atm} {option_type}: Not found")
                continue

            inst = instruments[key]
            symbol = inst['symbol']
            token = inst['token']

            try:
                # Get option LTP
                quote = kite.quote(f"{inst['exchange']}:{symbol}")
                opt_ltp = quote[f"{inst['exchange']}:{symbol}"]['last_price']

                # Get historical data
                data = get_15min_data(kite, token, days=30)

                # Find reversal zones
                zones = find_reversal_zones(data, opt_ltp)

                if not zones:
                    continue

                # Score each zone
                for zone in zones:
                    score = score_opportunity(zone, opt_ltp)

                    # Calculate entry and stop loss
                    buffer = zone['zone_low'] * BUFFER_PCT / 100
                    entry = zone['zone_low'] - buffer
                    stop_loss = entry - buffer

                    # Determine status
                    diff = zone['price'] - opt_ltp
                    if abs(diff) < 15:
                        status = "AT LTP"
                    elif diff < 0:
                        status = f"{abs(diff)/opt_ltp*100:.0f}% below"
                    else:
                        status = f"{diff/opt_ltp*100:.0f}% above"

                    opportunities.append({
                        'index': index_name,
                        'symbol': symbol,
                        'type': option_type,
                        'strike': atm,
                        'expiry': expiry,
                        'ltp': opt_ltp,
                        'zone_price': zone['price'],
                        'zone_low': zone['zone_low'],
                        'zone_high': zone['zone_high'],
                        'entry': round(entry, 1),
                        'stop_loss': round(stop_loss, 1),
                        'bounces': zone['bounces'],
                        'strength': int(zone['avg_strength'] * 100),
                        'score': score,
                        'status': status,
                        'distance_pct': zone['distance_pct']
                    })

                print(f"  {symbol}: {len(zones)} zones found")

            except Exception as e:
                print(f"  {symbol}: ERROR - {str(e)[:50]}")

    print()

    # Step 4: Rank and display
    if not opportunities:
        print("No opportunities found.")
        return

    # Sort by score (highest first)
    opportunities.sort(key=lambda x: x['score'], reverse=True)

    # Display top opportunities
    print("="*80)
    print("TOP TRADING OPPORTUNITIES (Ranked by Score)")
    print("="*80)
    print()

    # Header
    print(f"{'#':<3} {'SYMBOL':<25} {'TYPE':<4} {'LTP':<8} {'ZONE':<12} "
          f"{'ENTRY':<8} {'STOP':<8} {'BOUNCES':<8} {'STR%':<5} {'SCORE':<6} {'STATUS':<12}")
    print("-"*115)

    # Top 20 opportunities
    for i, opp in enumerate(opportunities[:20], 1):
        zone_str = f"{int(opp['zone_low'])}-{int(opp['zone_high'])}"

        # Color coding based on score
        if opp['score'] >= 70:
            marker = "***"
        elif opp['score'] >= 50:
            marker = "** "
        else:
            marker = "*  "

        print(f"{i:<3} {opp['symbol']:<25} {opp['type']:<4} {opp['ltp']:<8.1f} "
              f"{zone_str:<12} {opp['entry']:<8.1f} {opp['stop_loss']:<8.1f} "
              f"{opp['bounces']:<8} {opp['strength']:<5} {opp['score']:<6.1f} "
              f"{opp['status']:<12} {marker}")

    # Summary by index
    print()
    print("="*80)
    print("SUMMARY BY INDEX")
    print("="*80)

    for index_name in INDICES.keys():
        idx_opps = [o for o in opportunities if o['index'] == index_name]
        if idx_opps:
            top_ce = [o for o in idx_opps if o['type'] == 'CE']
            top_pe = [o for o in idx_opps if o['type'] == 'PE']

            print(f"\n{index_name} (LTP: {index_ltps.get(index_name, 0):.2f}):")

            if top_ce:
                best_ce = max(top_ce, key=lambda x: x['score'])
                print(f"  Best CE: {best_ce['symbol']} - Zone {int(best_ce['zone_price'])}, "
                      f"Entry {best_ce['entry']:.1f}, Score {best_ce['score']:.1f}")

            if top_pe:
                best_pe = max(top_pe, key=lambda x: x['score'])
                print(f"  Best PE: {best_pe['symbol']} - Zone {int(best_pe['zone_price'])}, "
                      f"Entry {best_pe['entry']:.1f}, Score {best_pe['score']:.1f}")

    print()
    print("="*80)
    print("Legend: *** = Excellent (70+) | ** = Good (50-70) | * = Fair (<50)")
    print("="*80)


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='Options Opportunity Scanner')
    parser.add_argument('--refresh', action='store_true', help='Force refresh instruments cache')

    args = parser.parse_args()

    try:
        # Connect to Kite
        kite = get_kite()

        # Load/fetch instruments
        force_refresh = args.refresh or should_refresh_cache()
        instruments = fetch_instruments(kite, force_refresh)

        # Run scanner
        scan_opportunities(kite, instruments, force_refresh)

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
