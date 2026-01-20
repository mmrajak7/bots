#!/usr/bin/env python3
"""
Live Options Opportunity Scanner (Cron-Optimized)

Runs every 15 minutes and reports only NEW or IMPROVED opportunities.
Optimized for frequent execution with smart caching.

Cron Schedule (every 16th minute - after 15-min candle close):
    16,31,46 9-15 * * 1-5  python /path/to/opportunity_scanner_live.py

Usage:
    python opportunity_scanner_live.py              # Live mode (delta only)
    python opportunity_scanner_live.py --full       # Full scan (show all)
    python opportunity_scanner_live.py --test       # Test mode (no alerts)
"""

import json
import csv
import argparse
import pickle
from pathlib import Path
from datetime import datetime, timedelta, date, time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
from kiteconnect import KiteConnect
import logging

# =============================================================================
# PATHS
# =============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
HELPER_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == 'helper' else SCRIPT_DIR
BOTS_DIR = HELPER_DIR.parent
DATA_DIR = BOTS_DIR / 'data'
CACHE_DIR = HELPER_DIR / 'data' / 'cache'
LOGS_DIR = HELPER_DIR / 'logs'

TOKEN_FILE = DATA_DIR / 'kite_access_token.json'
INSTRUMENTS_CACHE = CACHE_DIR / 'options_cache.pkl'
PREVIOUS_SCAN_CACHE = CACHE_DIR / 'previous_scan.pkl'

CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# =============================================================================
# LOGGING SETUP
# =============================================================================

LOG_FILE = LOGS_DIR / f'opportunity_scanner_{datetime.now().strftime("%Y%m%d")}.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================

INDICES = {
    'BANKNIFTY': {
        'spot_symbol': 'NSE:NIFTY BANK',
        'strike_interval': 100,
        'round_to': 1000,
    },
    'NIFTY': {
        'spot_symbol': 'NSE:NIFTY 50',
        'strike_interval': 50,
        'round_to': 100,
    },
    'SENSEX': {
        'spot_symbol': 'BSE:SENSEX',
        'strike_interval': 100,
        'round_to': 1000,
    }
}

# Scoring weights
SCORING = {
    'bounces_weight': 40,
    'strength_weight': 20,
    'proximity_weight': 20,
    'freshness_weight': 10,
    'risk_reward_weight': 10,
}

# Filters
MIN_BOUNCES = 5
MAX_DISTANCE_PCT = 20
BUFFER_PCT = 2.0

# Alert thresholds
MIN_SCORE_ALERT = 60  # Only alert if score >= 60
MIN_SCORE_CHANGE = 5  # Alert if score improved by 5+ points

# Trading hours (IST)
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

# =============================================================================
# MARKET HOURS CHECK
# =============================================================================

def is_market_hours() -> bool:
    """Check if market is open."""
    now = datetime.now().time()
    today = datetime.now().weekday()

    # Monday = 0, Sunday = 6
    if today >= 5:  # Saturday, Sunday
        return False

    return MARKET_OPEN <= now <= MARKET_CLOSE


# =============================================================================
# INSTRUMENT CACHE (same as before)
# =============================================================================

def should_refresh_instruments() -> bool:
    if not INSTRUMENTS_CACHE.exists():
        return True
    cache_time = datetime.fromtimestamp(INSTRUMENTS_CACHE.stat().st_mtime)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return cache_time < today


def load_cached_instruments() -> Optional[Dict]:
    if not INSTRUMENTS_CACHE.exists():
        return None
    try:
        with open(INSTRUMENTS_CACHE, 'rb') as f:
            return pickle.load(f)
    except:
        return None


def save_instruments_cache(instruments: Dict):
    with open(INSTRUMENTS_CACHE, 'wb') as f:
        pickle.dump(instruments, f)


def fetch_instruments(kite: KiteConnect) -> Dict:
    cached = load_cached_instruments()
    if cached and not should_refresh_instruments():
        logger.info("Using cached instruments")
        return cached

    logger.info("Downloading instruments from Kite...")
    instruments = {}

    for exchange in ['NFO', 'BFO']:
        exchange_instruments = kite.instruments(exchange)
        for inst in exchange_instruments:
            if inst['instrument_type'] not in ('CE', 'PE'):
                continue
            if inst['name'] not in INDICES:
                continue

            key = (inst['name'], inst['strike'], inst['instrument_type'], inst['expiry'])
            instruments[key] = {
                'symbol': inst['tradingsymbol'],
                'token': inst['instrument_token'],
                'lot_size': inst['lot_size'],
                'exchange': inst['exchange']
            }

    save_instruments_cache(instruments)
    logger.info(f"Cached {len(instruments)} instruments")
    return instruments


# =============================================================================
# PREVIOUS SCAN CACHE
# =============================================================================

def load_previous_scan() -> Optional[List[Dict]]:
    """Load previous scan results."""
    if not PREVIOUS_SCAN_CACHE.exists():
        return None
    try:
        with open(PREVIOUS_SCAN_CACHE, 'rb') as f:
            data = pickle.load(f)
            # Check if scan is from today
            if data.get('date') == datetime.now().date():
                return data.get('opportunities', [])
    except:
        pass
    return None


def save_scan_results(opportunities: List[Dict]):
    """Save current scan results."""
    data = {
        'date': datetime.now().date(),
        'timestamp': datetime.now(),
        'opportunities': opportunities
    }
    with open(PREVIOUS_SCAN_CACHE, 'wb') as f:
        pickle.dump(data, f)


# =============================================================================
# DELTA CALCULATION
# =============================================================================

def find_changes(current: List[Dict], previous: Optional[List[Dict]]) -> Tuple[List, List, List]:
    """
    Compare current scan with previous.

    Returns:
        (new_opportunities, improved_opportunities, degraded_opportunities)
    """
    if not previous:
        # First scan of the day - return top 10
        return current[:10], [], []

    # Build lookup for previous scan
    prev_lookup = {}
    for opp in previous:
        key = (opp['symbol'], opp['zone_price'])
        prev_lookup[key] = opp

    new_opps = []
    improved = []
    degraded = []

    for opp in current:
        key = (opp['symbol'], opp['zone_price'])

        if key not in prev_lookup:
            # New opportunity
            if opp['score'] >= MIN_SCORE_ALERT:
                new_opps.append(opp)
        else:
            prev_opp = prev_lookup[key]
            score_change = opp['score'] - prev_opp['score']

            if score_change >= MIN_SCORE_CHANGE:
                opp['score_change'] = f"+{score_change:.1f}"
                improved.append(opp)
            elif score_change <= -MIN_SCORE_CHANGE:
                opp['score_change'] = f"{score_change:.1f}"
                degraded.append(opp)

    return new_opps, improved, degraded


# =============================================================================
# CORE FUNCTIONS (same as opportunity_scanner.py)
# =============================================================================

def get_kite() -> KiteConnect:
    with open(TOKEN_FILE) as f:
        token_data = json.load(f)
    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


def get_monthly_expiry(index_name: str, instruments: Dict) -> Optional[date]:
    expiries = set()
    for (idx, strike, opt_type, expiry) in instruments.keys():
        if idx == index_name:
            expiries.add(expiry)

    if not expiries:
        return None

    today = date.today()
    current_month_expiries = [e for e in expiries if e.year == today.year and e.month == today.month]

    if current_month_expiries:
        return max(current_month_expiries)

    next_month = today.month + 1 if today.month < 12 else 1
    next_year = today.year if today.month < 12 else today.year + 1
    next_month_expiries = [e for e in expiries if e.year == next_year and e.month == next_month]

    return max(next_month_expiries) if next_month_expiries else max(expiries)


def calculate_atm_strike(ltp: float, index_name: str) -> int:
    config = INDICES[index_name]
    round_to = config['round_to']
    atm = round(ltp / round_to) * round_to
    return int(atm)


def get_15min_data(kite: KiteConnect, token: int, days: int = 30) -> List[Dict]:
    data = kite.historical_data(
        instrument_token=token,
        from_date=datetime.now() - timedelta(days=days),
        to_date=datetime.now(),
        interval='15minute'
    )
    return data


def find_reversal_zones(data: List[Dict], ltp: float) -> List[Dict]:
    bounces = []
    for candle in data:
        o, h, l, c = candle['open'], candle['high'], candle['low'], candle['close']
        candle_range = h - l
        if candle_range <= 0:
            continue

        close_position = (c - l) / candle_range
        lower_wick = min(o, c) - l
        lower_wick_pct = lower_wick / candle_range

        is_bounce = (close_position >= 0.4) or (lower_wick_pct >= 0.3)
        if is_bounce:
            bounces.append({
                'low': l, 'close': c, 'date': candle['date'],
                'strength': close_position
            })

    if not bounces:
        return []

    round_to = 10
    zone_bounces = defaultdict(list)
    for bounce in bounces:
        rounded = round(bounce['low'] / round_to) * round_to
        zone_bounces[rounded].append(bounce)

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

            zone_center = sum(zone_levels) / len(zone_levels)
            distance_pct = abs(zone_center - ltp) / ltp * 100

            if distance_pct > MAX_DISTANCE_PCT:
                continue

            zones.append({
                'price': int(zone_center),
                'zone_low': round(min(lows), 1),
                'zone_high': round(max(lows), 1),
                'bounces': len(all_bounces),
                'avg_strength': sum(b['strength'] for b in all_bounces) / len(all_bounces),
                'last_bounce': max(dates),
                'distance_pct': distance_pct
            })

        used.add(level)

    return zones


def score_opportunity(zone: Dict, ltp: float) -> float:
    bounces_score = min(40, (zone['bounces'] / 50) * 40)
    strength_score = zone['avg_strength'] * 20
    proximity_score = max(0, 20 - zone['distance_pct'])

    days_ago = (datetime.now() - zone['last_bounce'].replace(tzinfo=None)).days
    freshness_score = max(0, 10 - (days_ago / 7) * 10)

    zone_size = zone['zone_high'] - zone['zone_low']
    buffer = zone['zone_low'] * BUFFER_PCT / 100
    stop_distance = buffer * 2

    if zone_size > 0:
        rr_ratio = stop_distance / zone_size
        risk_reward_score = min(10, (1 / (rr_ratio + 0.1)) * 5)
    else:
        risk_reward_score = 5

    total = (
        bounces_score * SCORING['bounces_weight'] / 40 +
        strength_score * SCORING['strength_weight'] / 20 +
        proximity_score * SCORING['proximity_weight'] / 20 +
        freshness_score * SCORING['freshness_weight'] / 10 +
        risk_reward_score * SCORING['risk_reward_weight'] / 10
    )

    return round(total, 1)


# =============================================================================
# LIVE SCANNER
# =============================================================================

def scan_live(kite: KiteConnect, instruments: Dict, show_full: bool = False):
    """Run live scan with delta reporting."""

    logger.info("="*80)
    logger.info(f"LIVE OPPORTUNITY SCAN - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("="*80)

    # Get index LTPs
    index_ltps = {}
    for index_name, config in INDICES.items():
        try:
            quote = kite.quote(config['spot_symbol'])
            ltp = quote[config['spot_symbol']]['last_price']
            index_ltps[index_name] = ltp
        except Exception as e:
            logger.error(f"{index_name} LTP fetch failed: {e}")
            continue

    # Calculate strikes and expiries
    index_config = {}
    for index_name, ltp in index_ltps.items():
        atm = calculate_atm_strike(ltp, index_name)
        expiry = get_monthly_expiry(index_name, instruments)
        if expiry:
            index_config[index_name] = {'ltp': ltp, 'atm': atm, 'expiry': expiry}

    # Scan for opportunities
    opportunities = []

    for index_name, config in index_config.items():
        atm = config['atm']
        expiry = config['expiry']

        for option_type in ['CE', 'PE']:
            key = (index_name, atm, option_type, expiry)
            if key not in instruments:
                continue

            inst = instruments[key]
            symbol = inst['symbol']
            token = inst['token']

            try:
                quote = kite.quote(f"{inst['exchange']}:{symbol}")
                opt_ltp = quote[f"{inst['exchange']}:{symbol}"]['last_price']

                data = get_15min_data(kite, token, days=30)
                zones = find_reversal_zones(data, opt_ltp)

                for zone in zones:
                    score = score_opportunity(zone, opt_ltp)

                    buffer = zone['zone_low'] * BUFFER_PCT / 100
                    entry = zone['zone_low'] - buffer
                    stop_loss = entry - buffer

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

            except Exception as e:
                logger.error(f"{symbol} scan failed: {str(e)[:50]}")

    # Sort by score
    opportunities.sort(key=lambda x: x['score'], reverse=True)

    # Load previous scan and find changes
    previous = load_previous_scan()
    new_opps, improved, degraded = find_changes(opportunities, previous)

    # Save current scan
    save_scan_results(opportunities)

    # Display results
    if show_full:
        display_full_scan(opportunities)
    else:
        display_delta(new_opps, improved, degraded)


def display_full_scan(opportunities: List[Dict]):
    """Display full scan results."""
    print("\n" + "="*115)
    print("FULL SCAN - TOP 20 OPPORTUNITIES")
    print("="*115)

    print(f"{'#':<3} {'SYMBOL':<25} {'TYPE':<4} {'LTP':<8} {'ZONE':<12} "
          f"{'ENTRY':<8} {'STOP':<8} {'BOUNCES':<8} {'STR%':<5} {'SCORE':<6} {'STATUS':<12}")
    print("-"*115)

    for i, opp in enumerate(opportunities[:20], 1):
        zone_str = f"{int(opp['zone_low'])}-{int(opp['zone_high'])}"
        marker = "***" if opp['score'] >= 70 else ("**" if opp['score'] >= 50 else "*")

        print(f"{i:<3} {opp['symbol']:<25} {opp['type']:<4} {opp['ltp']:<8.1f} "
              f"{zone_str:<12} {opp['entry']:<8.1f} {opp['stop_loss']:<8.1f} "
              f"{opp['bounces']:<8} {opp['strength']:<5} {opp['score']:<6.1f} "
              f"{opp['status']:<12} {marker}")


def display_delta(new_opps: List[Dict], improved: List[Dict], degraded: List[Dict]):
    """Display only changes since last scan."""

    has_changes = bool(new_opps or improved)

    if not has_changes:
        logger.info("No new or improved opportunities since last scan.")
        return

    print("\n" + "="*115)
    print(f"DELTA REPORT - {datetime.now().strftime('%H:%M:%S')}")
    print("="*115)

    if new_opps:
        print(f"\n🆕 NEW OPPORTUNITIES ({len(new_opps)}):")
        print("-"*115)
        print(f"{'#':<3} {'SYMBOL':<25} {'TYPE':<4} {'LTP':<8} {'ZONE':<12} "
              f"{'ENTRY':<8} {'STOP':<8} {'BOUNCES':<8} {'SCORE':<6} {'STATUS':<12}")
        print("-"*115)

        for i, opp in enumerate(new_opps, 1):
            zone_str = f"{int(opp['zone_low'])}-{int(opp['zone_high'])}"
            print(f"{i:<3} {opp['symbol']:<25} {opp['type']:<4} {opp['ltp']:<8.1f} "
                  f"{zone_str:<12} {opp['entry']:<8.1f} {opp['stop_loss']:<8.1f} "
                  f"{opp['bounces']:<8} {opp['score']:<6.1f} {opp['status']:<12}")

    if improved:
        print(f"\n📈 IMPROVED OPPORTUNITIES ({len(improved)}):")
        print("-"*115)
        print(f"{'#':<3} {'SYMBOL':<25} {'TYPE':<4} {'SCORE':<6} {'CHANGE':<8} {'ENTRY':<8} {'STATUS':<12}")
        print("-"*115)

        for i, opp in enumerate(improved, 1):
            print(f"{i:<3} {opp['symbol']:<25} {opp['type']:<4} {opp['score']:<6.1f} "
                  f"{opp['score_change']:<8} {opp['entry']:<8.1f} {opp['status']:<12}")

    print("\n" + "="*115)


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='Live Options Opportunity Scanner')
    parser.add_argument('--full', action='store_true', help='Show full scan (not delta)')
    parser.add_argument('--test', action='store_true', help='Test mode (ignore market hours)')

    args = parser.parse_args()

    # Check market hours
    if not args.test and not is_market_hours():
        logger.info("Market closed. Exiting.")
        return 0

    try:
        kite = get_kite()
        instruments = fetch_instruments(kite)
        scan_live(kite, instruments, args.full)

    except Exception as e:
        logger.error(f"Scan failed: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
