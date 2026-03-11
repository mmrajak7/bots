#!/usr/bin/env python3
"""
Swing Support Scanner - Finds swing lows for high-reward reversal trades

Detects SWING LOWS (significant reversal points) on:
- Option premium charts (CE/PE for ATM strikes)
- Cross-validates with SPOT chart for higher confidence

Indices: NIFTY, BANKNIFTY, SENSEX
Timeframes: Daily, 1-Hour, 15-Minute (configurable)

Logic:
1. Find SWING LOWS - candles whose low < surrounding N candles
2. Group swing lows at similar prices = SUPPORT LEVEL
3. Cross-check with SPOT for confluence
4. Alert when price is 5-10 pts above support level

Usage:
    python scanner.py           # Normal mode
    python scanner.py --test    # Test mode (show levels)
    python scanner.py --force   # Force full scan
"""

import json
import pickle
import logging
import requests
from pathlib import Path
from datetime import datetime, timedelta, date, time
from typing import Dict, List, Optional, Tuple
from kiteconnect import KiteConnect

# =============================================================================
# PATHS
# =============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
BOTS_DIR = SCRIPT_DIR.parent
DATA_DIR = BOTS_DIR / 'data'
CACHE_DIR = SCRIPT_DIR / 'data' / 'cache'
LOGS_DIR = SCRIPT_DIR / 'logs'

BOUNCER_CONFIG = BOTS_DIR / 'Bouncer' / 'config' / 'config.json'
TOKEN_FILE = DATA_DIR / 'kite_access_token.json'
INSTRUMENTS_CACHE = CACHE_DIR / 'instruments.pkl'
LEVELS_DB = CACHE_DIR / 'levels_db.pkl'
ALERTS_TRACKER = CACHE_DIR / 'alerts_tracker.pkl'

CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# =============================================================================
# LOGGING
# =============================================================================

def setup_logging():
    today = datetime.now().strftime('%Y%m%d')
    log_file = LOGS_DIR / f'scanner_{today}.log'

    for old_log in LOGS_DIR.glob('scanner_*.log'):
        if old_log.stem != f'scanner_{today}':
            try:
                old_log.unlink()
            except (OSError, PermissionError):
                pass

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logging()

# =============================================================================
# TELEGRAM CONFIG
# =============================================================================

try:
    with open(BOUNCER_CONFIG) as f:
        BOUNCER_CFG = json.load(f)
    TELEGRAM_BOT_TOKEN = BOUNCER_CFG['telegram']['bot_token']
    TELEGRAM_CHAT_ID = BOUNCER_CFG['telegram']['chat_id']
except Exception as e:
    logger.error(f"Telegram config error: {e}")
    TELEGRAM_BOT_TOKEN = None
    TELEGRAM_CHAT_ID = None

# =============================================================================
# INDEX CONFIGURATION
# =============================================================================

INDICES = {
    'NIFTY': {
        'spot': 'NSE:NIFTY 50',
        'spot_token': 256265,
        'round_to': 100,
        'exchange': 'NFO'
    },
    'BANKNIFTY': {
        'spot': 'NSE:NIFTY BANK',
        'spot_token': 260105,
        'round_to': 100,
        'exchange': 'NFO'
    },
    'SENSEX': {
        'spot': 'BSE:SENSEX',
        'spot_token': 265,
        'round_to': 100,
        'exchange': 'BFO'
    }
}

# =============================================================================
# PARAMETERS - EASY TO TUNE
# =============================================================================

# Swing detection
SWING_LOOKBACK = 5       # A swing low must be lower than 5 candles on each side
MIN_SWING_LOWS = 2       # Minimum swing lows to form a support level
LEVEL_TOLERANCE = 20     # Swing lows within 20 pts = same level (for NIFTY spot)
OPTION_TOLERANCE = 5     # For option premiums, within 5 pts = same level

# Expiry selection
MIN_DTE = 3              # Minimum days to expiry (switch to next month if less)

# Alert trigger
ALERT_DISTANCE = 10      # Alert when LTP is within 10 pts of support
ALERT_COOLDOWN_HOURS = 2 # Don't re-alert same level for 2 hours

# Timeframe configuration (set enabled=False to disable)
TIMEFRAMES = {
    'day': {
        'interval': 'day',
        'lookback_days': 180,
        'scan_minutes': [16],      # Once per hour
        'enabled': True
    },
    '1h': {
        'interval': '60minute',
        'lookback_days': 60,
        'scan_minutes': [17, 47],  # Twice per hour
        'enabled': True
    },
    '15m': {
        'interval': '15minute',
        'lookback_days': 20,
        'scan_minutes': [16, 31, 46],  # 3x per hour
        'enabled': False  # Disabled for now
    }
}

# =============================================================================
# MARKET HOURS
# =============================================================================

def is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    return time(9, 15) <= now.time() <= time(15, 30)

def is_full_scan_time() -> bool:
    minute = datetime.now().minute
    for tf, config in TIMEFRAMES.items():
        if config['enabled'] and minute in config['scan_minutes']:
            return True
    return False

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
    """Fetch and cache instruments."""
    if INSTRUMENTS_CACHE.exists():
        cache_time = datetime.fromtimestamp(INSTRUMENTS_CACHE.stat().st_mtime)
        if datetime.now() - cache_time < timedelta(hours=12):
            with open(INSTRUMENTS_CACHE, 'rb') as f:
                return pickle.load(f)

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
            logger.warning(f"Failed to fetch {exchange}: {e}")

    with open(INSTRUMENTS_CACHE, 'wb') as f:
        pickle.dump(instruments, f)

    return instruments

# =============================================================================
# SWING LOW DETECTION
# =============================================================================

def find_swing_lows(data: List[Dict], lookback: int = SWING_LOOKBACK) -> List[Dict]:
    """
    Find SWING LOWS - significant local minima.

    A swing low is a candle whose LOW is lower than:
    - The LOW of `lookback` candles BEFORE it
    - The LOW of `lookback` candles AFTER it

    This finds the "V" bottoms that stand out.
    """
    if len(data) < (lookback * 2 + 1):
        return []

    swing_lows = []

    for i in range(lookback, len(data) - lookback):
        current_low = data[i]['low']

        # Get lows of surrounding candles
        left_lows = [data[j]['low'] for j in range(i - lookback, i)]
        right_lows = [data[j]['low'] for j in range(i + 1, i + lookback + 1)]

        # Check if current is lower than ALL surrounding candles
        if current_low < min(left_lows) and current_low < min(right_lows):
            swing_lows.append({
                'price': current_low,
                'date': data[i]['date'],
                'candle': data[i]
            })

    return swing_lows


def find_support_levels(data: List[Dict], tolerance: float) -> List[Dict]:
    """
    Find support levels by grouping swing lows at similar prices.

    Two swing lows within `tolerance` points = same support level
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
        support_levels.append({
            'price': round(sum(prices) / len(prices), 1),
            'low': min(prices),
            'high': max(prices),
            'touches': len(group),
            'last_touch': max(s['date'] for s in group),
            'strength': 'strong' if len(group) >= 3 else 'moderate'
        })

    return support_levels

# =============================================================================
# EXPIRY SELECTION
# =============================================================================

def get_monthly_expiries(expiries: set) -> List[date]:
    """
    Filter for MONTHLY expiries only.
    Monthly expiry = last expiry of each month.
    """
    if not expiries:
        return []

    # Group expiries by year-month
    by_month = {}
    for exp in expiries:
        key = (exp.year, exp.month)
        if key not in by_month:
            by_month[key] = []
        by_month[key].append(exp)

    # Get last expiry of each month (that's the monthly)
    monthly = []
    for key in by_month:
        monthly.append(max(by_month[key]))

    return sorted(monthly)


def get_valid_expiry(index: str, instruments: Dict) -> Optional[date]:
    """Get MONTHLY expiry with at least MIN_DTE days remaining."""
    expiries = set()
    for (idx, strike, opt_type, expiry) in instruments.keys():
        if idx == index:
            expiries.add(expiry)

    if not expiries:
        return None

    today = date.today()

    # Filter for monthly expiries only
    monthly_expiries = get_monthly_expiries(expiries)

    for exp in monthly_expiries:
        days_to_expiry = (exp - today).days
        if days_to_expiry >= MIN_DTE:
            return exp

    return monthly_expiries[-1] if monthly_expiries else None


def calculate_atm(ltp: float, index: str) -> int:
    round_to = INDICES[index]['round_to']
    return int(round(ltp / round_to) * round_to)

# =============================================================================
# PERSISTENCE
# =============================================================================

def load_levels_db() -> Dict:
    if not LEVELS_DB.exists():
        return {}
    try:
        with open(LEVELS_DB, 'rb') as f:
            return pickle.load(f)
    except Exception:
        return {}

def save_levels_db(db: Dict):
    with open(LEVELS_DB, 'wb') as f:
        pickle.dump(db, f)

def load_alerts_tracker() -> Dict:
    if not ALERTS_TRACKER.exists():
        return {}
    try:
        with open(ALERTS_TRACKER, 'rb') as f:
            tracker = pickle.load(f)
        now = datetime.now()
        return {k: v for k, v in tracker.items()
                if isinstance(v, datetime) and (now - v).total_seconds() < 7200}
    except Exception:
        return {}

def save_alerts_tracker(tracker: Dict):
    with open(ALERTS_TRACKER, 'wb') as f:
        pickle.dump(tracker, f)

def can_alert(key: str, tracker: Dict) -> bool:
    if key not in tracker:
        return True
    hours_since = (datetime.now() - tracker[key]).total_seconds() / 3600
    return hours_since >= ALERT_COOLDOWN_HOURS

def mark_alerted(key: str, tracker: Dict):
    tracker[key] = datetime.now()

# =============================================================================
# TELEGRAM
# =============================================================================

def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        response = requests.post(url, json={
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'HTML'
        }, timeout=10)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False


def format_alert(symbol: str, opt_type: str, level: Dict, ltp: float,
                 timeframe: str, spot_confluence: bool) -> str:
    """
    Format support level alert.

    Example:
        SWING SUPPORT - NIFTY26JAN25800CE [1H]
        Support: 145.2-148.5 (3 touches, strong)
        LTP: 152.3 | Entry: 148.5 | Stop: 143.7
        Spot Confluence: YES
    """
    emoji = "🟢" if opt_type == "CE" else "🔴"
    confluence = "YES" if spot_confluence else "NO"

    entry = level['high']
    stop = round(level['low'] - 2, 1)

    msg = f"{emoji} <b>SWING SUPPORT</b> - {symbol} [{timeframe.upper()}]\n"
    msg += f"Support: {level['low']:.1f}-{level['high']:.1f} ({level['touches']} touches, {level['strength']})\n"
    msg += f"LTP: {ltp:.1f} | Entry: {entry:.1f} | Stop: {stop:.1f}\n"
    msg += f"Spot Confluence: <b>{confluence}</b>"

    return msg

# =============================================================================
# FULL SCAN - Find Support Levels
# =============================================================================

def full_scan(kite: KiteConnect, instruments: Dict):
    """
    Scan for swing support levels on option premiums.
    Cross-validate with spot chart.
    """
    logger.info("=" * 50)
    logger.info(f"FULL SCAN: {datetime.now().strftime('%H:%M:%S')}")
    logger.info("=" * 50)

    levels_db = {}

    for index, config in INDICES.items():
        try:
            # Get spot price
            quote = kite.quote(config['spot'])
            spot_ltp = quote[config['spot']]['last_price']
            atm = calculate_atm(spot_ltp, index)
            expiry = get_valid_expiry(index, instruments)

            if not expiry:
                logger.warning(f"{index}: No valid expiry found")
                continue

            dte = (expiry - date.today()).days
            logger.info(f"\n{index}: Spot {spot_ltp:.0f}, ATM {atm}, Expiry {expiry} ({dte} DTE)")

            # Get SPOT support levels for cross-validation
            spot_levels = {}
            for tf_name, tf_config in TIMEFRAMES.items():
                if not tf_config['enabled']:
                    continue

                try:
                    spot_data = kite.historical_data(
                        instrument_token=config['spot_token'],
                        from_date=datetime.now() - timedelta(days=tf_config['lookback_days']),
                        to_date=datetime.now(),
                        interval=tf_config['interval']
                    )
                    spot_levels[tf_name] = find_support_levels(spot_data, LEVEL_TOLERANCE)
                    if spot_levels[tf_name]:
                        logger.info(f"  SPOT [{tf_name}]: {len(spot_levels[tf_name])} support levels")
                except Exception as e:
                    logger.debug(f"  SPOT [{tf_name}] error: {e}")

            # Scan CE and PE options
            for opt_type in ['CE', 'PE']:
                key = (index, atm, opt_type, expiry)
                if key not in instruments:
                    continue

                inst = instruments[key]
                symbol = inst['symbol']

                # Get option LTP
                quote_key = f"{inst['exchange']}:{symbol}"
                try:
                    opt_quote = kite.quote(quote_key)
                    opt_ltp = opt_quote[quote_key]['last_price']
                except Exception:
                    continue

                if opt_ltp <= 0:
                    continue

                if symbol not in levels_db:
                    levels_db[symbol] = {
                        'index': index,
                        'type': opt_type,
                        'exchange': inst['exchange'],
                        'token': inst['token'],
                        'ltp': opt_ltp,
                        'expiry': expiry,
                        'timeframes': {}
                    }

                # Scan each timeframe
                for tf_name, tf_config in TIMEFRAMES.items():
                    if not tf_config['enabled']:
                        continue

                    try:
                        data = kite.historical_data(
                            instrument_token=inst['token'],
                            from_date=datetime.now() - timedelta(days=tf_config['lookback_days']),
                            to_date=datetime.now(),
                            interval=tf_config['interval']
                        )

                        levels = find_support_levels(data, OPTION_TOLERANCE)

                        if levels:
                            # Check spot confluence for each level
                            for lvl in levels:
                                lvl['spot_confluence'] = False

                                # For CE: support on CE premium = NIFTY near support
                                # For PE: support on PE premium = NIFTY near resistance
                                # Cross-check with spot levels
                                if tf_name in spot_levels:
                                    for spot_lvl in spot_levels[tf_name]:
                                        # If spot has a support/resistance near current spot price
                                        # and option has support, it's confluence
                                        spot_distance = abs(spot_ltp - spot_lvl['price'])
                                        if spot_distance < LEVEL_TOLERANCE * 2:
                                            lvl['spot_confluence'] = True
                                            break

                            levels_db[symbol]['timeframes'][tf_name] = {
                                'levels': levels,
                                'updated': datetime.now()
                            }

                            logger.info(f"  {symbol} [{tf_name}]: {len(levels)} support levels")
                            for lvl in levels[:3]:
                                conf = "[SPOT]" if lvl.get('spot_confluence') else ""
                                logger.info(f"    {lvl['low']:.1f}-{lvl['high']:.1f} ({lvl['touches']} touches) {conf}")

                    except Exception as e:
                        logger.debug(f"  {symbol} [{tf_name}] error: {str(e)[:30]}")

        except Exception as e:
            logger.error(f"{index} failed: {e}")

    save_levels_db(levels_db)
    logger.info(f"\nSaved levels for {len(levels_db)} symbols")
    logger.info("=" * 50)

# =============================================================================
# QUICK CHECK - Alert on Proximity
# =============================================================================

def quick_check(kite: KiteConnect):
    """
    Check if price is approaching any support level.
    Alert when within ALERT_DISTANCE points.
    """
    logger.info(f"Quick check: {datetime.now().strftime('%H:%M:%S')}")

    levels_db = load_levels_db()
    if not levels_db:
        return

    alerts_tracker = load_alerts_tracker()
    alerts_sent = 0

    for symbol, data in levels_db.items():
        try:
            # Get current LTP
            quote_key = f"{data['exchange']}:{symbol}"
            quote = kite.quote(quote_key)
            ltp = quote[quote_key]['last_price']

            if ltp <= 0:
                continue

            # Update stored LTP
            data['ltp'] = ltp

            # Check levels in each timeframe
            for tf_name, tf_data in data.get('timeframes', {}).items():
                if not isinstance(tf_data, dict) or 'levels' not in tf_data:
                    continue

                for level in tf_data['levels']:
                    # Check if price is approaching support from above
                    distance = ltp - level['high']

                    # Alert if within ALERT_DISTANCE above the level
                    if 0 < distance <= ALERT_DISTANCE:
                        # Only alert when spot confluence is confirmed
                        if not level.get('spot_confluence', False):
                            continue

                        alert_key = f"{symbol}_{level['price']}_{tf_name}"

                        if can_alert(alert_key, alerts_tracker):
                            msg = format_alert(
                                symbol, data['type'], level, ltp, tf_name,
                                level.get('spot_confluence', False)
                            )
                            send_telegram(msg)
                            mark_alerted(alert_key, alerts_tracker)
                            alerts_sent += 1

                            logger.info(f"ALERT: {symbol} [{tf_name}] near support {level['price']:.1f}")

                    # Invalidate level if price breaks below
                    elif ltp < level['low'] - 5:
                        level['broken'] = True
                        logger.info(f"BROKEN: {symbol} [{tf_name}] support {level['price']:.1f}")

        except Exception as e:
            logger.debug(f"{symbol} check failed: {e}")

    save_levels_db(levels_db)
    save_alerts_tracker(alerts_tracker)

    if alerts_sent:
        logger.info(f"Sent {alerts_sent} alerts")

# =============================================================================
# LEVEL MANAGEMENT
# =============================================================================

def cleanup_broken_levels():
    """Remove broken/invalidated levels from DB."""
    levels_db = load_levels_db()

    for symbol, data in levels_db.items():
        for tf_name, tf_data in data.get('timeframes', {}).items():
            if 'levels' in tf_data:
                # Keep only unbroken levels
                tf_data['levels'] = [
                    lvl for lvl in tf_data['levels']
                    if not lvl.get('broken', False)
                ]

    save_levels_db(levels_db)

# =============================================================================
# MAIN
# =============================================================================

def main(force=False, test=False):
    """Main entry point."""
    if test:
        logger.info("TEST MODE - Ignoring market hours")
    elif not force and not is_market_open():
        logger.info("Market closed")
        return

    kite = get_kite()
    instruments = fetch_instruments(kite)

    if force or test or is_full_scan_time():
        full_scan(kite, instruments)
        cleanup_broken_levels()

    if not test:
        quick_check(kite)


if __name__ == '__main__':
    import sys
    force = '--force' in sys.argv
    test = '--test' in sys.argv
    main(force=force, test=test)
