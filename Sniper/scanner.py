#!/usr/bin/env python3
"""
Options Scanner - Two-Tier Strategy

FULL SCAN (every 15 mins at :16, :31, :46):
  - Analyze reversal zones
  - Calculate scores
  - Mark broken zones

QUICK CHECK (every minute):
  - Check if LTP near zones (within 2%)
  - Send real-time entry alerts
  - 1-hour cooldown per symbol/zone

Cron Setup:
    * 9-14 * * 1-5 cd /path/to/Sniper && python3 scanner.py >> logs/cron.log 2>&1
    0-30 15 * * 1-5 cd /path/to/Sniper && python3 scanner.py >> logs/cron.log 2>&1
"""

import json
import pickle
import logging
import requests
from pathlib import Path
from datetime import datetime, timedelta, date, time
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
LOGS_DIR = HELPER_DIR / 'logs'

BOUNCER_CONFIG = BOTS_DIR / 'Bouncer' / 'config' / 'config.json'

TOKEN_FILE = DATA_DIR / 'kite_access_token.json'
INSTRUMENTS_CACHE = CACHE_DIR / 'instruments.pkl'
ZONES_DB = CACHE_DIR / 'zones_db.pkl'
ALERTS_TRACKER = CACHE_DIR / 'alerts_tracker.pkl'

CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# =============================================================================
# LOGGING (Auto-rotates daily)
# =============================================================================

def setup_logging():
    today = datetime.now().strftime('%Y%m%d')
    log_file = LOGS_DIR / f'scanner_{today}.log'

    # Clear old logs
    for old_log in LOGS_DIR.glob('scanner_*.log'):
        if old_log.stem != f'scanner_{today}':
            try:
                old_log.unlink()
            except (OSError, PermissionError) as e:
                # Can't use logger yet (not set up), print to stderr
                import sys
                print(f"Warning: Could not delete old log {old_log}: {e}", file=sys.stderr)

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
# CONFIG
# =============================================================================

try:
    with open(BOUNCER_CONFIG) as f:
        BOUNCER_CFG = json.load(f)

    TELEGRAM_BOT_TOKEN = BOUNCER_CFG['telegram']['bot_token']
    TELEGRAM_CHAT_ID = BOUNCER_CFG['telegram']['chat_id']

    # Validate config
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("Telegram bot_token or chat_id is empty")

except FileNotFoundError:
    print(f"ERROR: Config file not found: {BOUNCER_CONFIG}", file=__import__('sys').stderr)
    print("Ensure BOTS/Bouncer/config/config.json exists", file=__import__('sys').stderr)
    __import__('sys').exit(1)
except (json.JSONDecodeError, KeyError, ValueError) as e:
    print(f"ERROR: Invalid config file: {e}", file=__import__('sys').stderr)
    print(f"Check structure of {BOUNCER_CONFIG}", file=__import__('sys').stderr)
    __import__('sys').exit(1)

INDICES = {
    'BANKNIFTY': {'spot': 'NSE:NIFTY BANK', 'round_to': 1000},
    'NIFTY': {'spot': 'NSE:NIFTY 50', 'round_to': 100},
    'SENSEX': {'spot': 'BSE:SENSEX', 'round_to': 1000},
}

# Scanning params
LOOKBACK_DAYS = 30
MIN_BOUNCES = 5
MIN_SCORE = 50
BUFFER_PCT = 2.0

# Proximity alert params
PROXIMITY_PCT = 2.0  # Alert when within 2% of zone
ALERT_COOLDOWN_HOURS = 1  # Don't re-alert same zone for 1 hour

# Full scan minutes (16, 31, 46)
FULL_SCAN_MINUTES = [16, 31, 46]

# =============================================================================
# MARKET HOURS
# =============================================================================

def is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    current_time = now.time()
    return time(9, 15) <= current_time <= time(15, 30)

def is_full_scan_time() -> bool:
    """Check if current minute is a full scan minute."""
    return datetime.now().minute in FULL_SCAN_MINUTES

# =============================================================================
# KITE
# =============================================================================

def get_kite() -> KiteConnect:
    try:
        with open(TOKEN_FILE) as f:
            token_data = json.load(f)

        # Validate token structure
        required_keys = ['api_key', 'access_token']
        for key in required_keys:
            if key not in token_data or not token_data[key]:
                raise ValueError(f"Missing or empty '{key}' in token file")

        kite = KiteConnect(api_key=token_data['api_key'])
        kite.set_access_token(token_data['access_token'])
        return kite

    except FileNotFoundError:
        logger.error(f"Token file not found: {TOKEN_FILE}")
        logger.error("Ensure BOTS/data/kite_access_token.json exists")
        raise
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.error(f"Invalid token file: {e}")
        logger.error(f"Check structure of {TOKEN_FILE}")
        raise

# =============================================================================
# INSTRUMENTS CACHE
# =============================================================================

def load_instruments() -> Optional[Dict]:
    if not INSTRUMENTS_CACHE.exists():
        return None
    cache_time = datetime.fromtimestamp(INSTRUMENTS_CACHE.stat().st_mtime)
    if cache_time.date() < datetime.now().date():
        return None
    with open(INSTRUMENTS_CACHE, 'rb') as f:
        return pickle.load(f)

def save_instruments(instruments: Dict):
    with open(INSTRUMENTS_CACHE, 'wb') as f:
        pickle.dump(instruments, f)

def fetch_instruments(kite: KiteConnect) -> Dict:
    cached = load_instruments()
    if cached:
        return cached

    logger.info("Downloading instruments...")
    instruments = {}

    for exchange in ['NFO', 'BFO']:
        for inst in kite.instruments(exchange):
            if inst['instrument_type'] not in ('CE', 'PE'):
                continue
            if inst['name'] not in INDICES:
                continue

            key = (inst['name'], inst['strike'], inst['instrument_type'], inst['expiry'])
            instruments[key] = {
                'symbol': inst['tradingsymbol'],
                'token': inst['instrument_token'],
                'exchange': inst['exchange']
            }

    save_instruments(instruments)
    logger.info(f"Cached {len(instruments)} instruments")
    return instruments

# =============================================================================
# ZONES DATABASE
# =============================================================================

def load_zones_db() -> Dict:
    """Load zones database."""
    if not ZONES_DB.exists():
        return {}

    try:
        with open(ZONES_DB, 'rb') as f:
            db = pickle.load(f)
    except (pickle.UnpicklingError, EOFError) as e:
        logger.warning(f"Corrupted zones DB, resetting: {e}")
        return {}

    # Reset if new day or no date key
    db_date = db.get('date')
    if db_date is None or db_date != datetime.now().date():
        if db_date is not None:
            logger.info(f"Zones DB is from {db_date}, resetting for today")
        return {}

    return db

def save_zones_db(db: Dict):
    """Save zones database."""
    db['date'] = datetime.now().date()
    with open(ZONES_DB, 'wb') as f:
        pickle.dump(db, f)

# =============================================================================
# ALERTS TRACKER
# =============================================================================

def load_alerts_tracker() -> Dict:
    """Load alerts tracker (cooldown management)."""
    if not ALERTS_TRACKER.exists():
        logger.info("Alerts tracker file not found, creating new tracker")
        return {}

    try:
        with open(ALERTS_TRACKER, 'rb') as f:
            tracker = pickle.load(f)
    except (pickle.UnpicklingError, EOFError, Exception) as e:
        logger.warning(f"Corrupted alerts tracker, resetting: {e}")
        return {}

    # Clean old entries (> 2 hours old)
    now = datetime.now()
    cleaned = {k: v for k, v in tracker.items() if (now - v).total_seconds() < 7200}

    if len(cleaned) < len(tracker):
        logger.info(f"Cleaned {len(tracker) - len(cleaned)} old alert entries")

    return cleaned

def save_alerts_tracker(tracker: Dict):
    """Save alerts tracker to disk."""
    try:
        with open(ALERTS_TRACKER, 'wb') as f:
            pickle.dump(tracker, f)
        logger.debug(f"Saved alerts tracker ({len(tracker)} entries)")
    except Exception as e:
        logger.error(f"Failed to save alerts tracker: {e}")

def can_alert(symbol: str, zone_price: int, tracker: Dict) -> bool:
    """Check if we can alert for this symbol/zone (1 hour cooldown)."""
    key = f"{symbol}_{zone_price}"
    if key not in tracker:
        logger.debug(f"Can alert {key}: not in tracker")
        return True

    last_alert = tracker[key]
    hours_since = (datetime.now() - last_alert).total_seconds() / 3600

    can_send = hours_since >= ALERT_COOLDOWN_HOURS
    if can_send:
        logger.debug(f"Can alert {key}: {hours_since:.1f}h since last alert")
    else:
        logger.debug(f"Cooldown active {key}: {hours_since:.1f}h / {ALERT_COOLDOWN_HOURS}h")

    return can_send

def mark_alerted(symbol: str, zone_price: int, tracker: Dict):
    """Mark this symbol/zone as alerted."""
    key = f"{symbol}_{zone_price}"
    tracker[key] = datetime.now()
    logger.debug(f"Marked alerted: {key} at {tracker[key].strftime('%H:%M:%S')}")

# =============================================================================
# STRIKE CALCULATION
# =============================================================================

def get_monthly_expiry(index: str, instruments: Dict) -> Optional[date]:
    expiries = set()
    for (idx, strike, opt_type, expiry) in instruments.keys():
        if idx == index:
            expiries.add(expiry)

    if not expiries:
        return None

    today = date.today()
    current_month = [e for e in expiries if e.year == today.year and e.month == today.month]
    if current_month:
        return max(current_month)

    next_month = today.month + 1 if today.month < 12 else 1
    next_year = today.year if today.month < 12 else today.year + 1
    next_month_expiries = [e for e in expiries if e.year == next_year and e.month == next_month]

    return max(next_month_expiries) if next_month_expiries else max(expiries)

def calculate_atm(ltp: float, index: str) -> int:
    round_to = INDICES[index]['round_to']
    return int(round(ltp / round_to) * round_to)

# =============================================================================
# REVERSAL ZONE DETECTION (FULL SCAN ONLY)
# =============================================================================

def get_historical_data(kite: KiteConnect, token: int) -> List[Dict]:
    return kite.historical_data(
        instrument_token=token,
        from_date=datetime.now() - timedelta(days=LOOKBACK_DAYS),
        to_date=datetime.now(),
        interval='15minute'
    )

def find_reversal_zones(data: List[Dict], ltp: float) -> List[Dict]:
    bounces = []
    for candle in data:
        # Validate candle has all required keys
        required_keys = ['open', 'high', 'low', 'close', 'date']
        if not all(k in candle for k in required_keys):
            continue

        open_price, high, low, close = candle['open'], candle['high'], candle['low'], candle['close']

        # Validate OHLC values
        if any(v is None or v < 0 for v in [open_price, high, low, close]):
            continue

        candle_range = high - low
        if candle_range <= 0:
            continue

        close_position = (close - low) / candle_range
        lower_wick = min(open_price, close) - low
        wick_pct = lower_wick / candle_range

        if close_position >= 0.4 or wick_pct >= 0.3:
            bounces.append({
                'low': low,
                'date': candle['date'],
                'strength': close_position
            })

    if not bounces:
        return []

    zone_data = defaultdict(list)
    for b in bounces:
        rounded = round(b['low'] / 10) * 10
        zone_data[rounded].append(b)

    zones = []
    sorted_levels = sorted(zone_data.keys())
    used = set()

    for level in sorted_levels:
        if level in used:
            continue

        merged = [level]
        if level + 10 in zone_data:
            merged.append(level + 10)
            used.add(level + 10)

        all_bounces = []
        for price_level in merged:
            all_bounces.extend(zone_data[price_level])

        if len(all_bounces) >= MIN_BOUNCES:
            lows = [b['low'] for b in all_bounces]
            zone_center = sum(merged) / len(merged)

            zones.append({
                'price': int(zone_center),
                'low': min(lows),
                'high': max(lows),
                'bounces': len(all_bounces),
                'strength': sum(b['strength'] for b in all_bounces) / len(all_bounces),
                'last_bounce': max(b['date'] for b in all_bounces)
            })

        used.add(level)

    return zones

def score_zone(zone: Dict, ltp: float) -> float:
    # Guard against invalid LTP
    if ltp <= 0:
        return 0.0

    bounce_score = min(50, (zone['bounces'] / 50) * 50)
    strength_score = zone['strength'] * 20
    distance_pct = abs(zone['price'] - ltp) / ltp * 100
    proximity_score = max(0, 20 - distance_pct)
    days_ago = (datetime.now() - zone['last_bounce'].replace(tzinfo=None)).days
    freshness_score = max(0, 10 - (days_ago / 7) * 10)

    return round(bounce_score + strength_score + proximity_score + freshness_score, 1)

def is_zone_broken(zone: Dict, ltp: float) -> bool:
    if ltp < zone['low'] * 0.97:
        return True
    if ltp > zone['high'] * 1.03:
        return True
    return False

# =============================================================================
# TELEGRAM
# =============================================================================

def send_telegram(message: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'HTML'
        }
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Telegram failed: {e}")
        return False

def format_proximity_alert(symbol: str, opt_type: str, zone: Dict, ltp: float, score: float) -> str:
    """
    Compact proximity alert with full info.

    Format:
        🎯 NIFTY 25900 PE [Score: 62]
        Zone: 155-174 (24 bounces, 68% strength)
        Entry: 151 | Stop: 148 | LTP: 158

        ⚡ PRICE NEAR ZONE - Ready to enter
    """
    buffer = zone['low'] * BUFFER_PCT / 100
    entry = int(zone['low'] - buffer)
    stop = int(entry - buffer)

    emoji = "🟢" if opt_type == "CE" else "🔴"
    msg = f"{emoji} <b>{symbol} {opt_type}</b> [Score: {score:.0f}]\n"
    msg += f"Zone: {int(zone['low'])}-{int(zone['high'])} ({zone['bounces']} bounces, {int(zone['strength']*100)}% strength)\n"
    msg += f"Entry: {entry} | Stop: {stop} | LTP: {ltp:.0f}\n\n"
    msg += "⚡ <b>PRICE NEAR ZONE</b> - Ready to enter"

    return msg

# =============================================================================
# FULL SCAN (Every 16, 31, 46)
# =============================================================================

def full_scan(kite: KiteConnect, instruments: Dict):
    """
    Full reversal zone analysis.
    Updates zones database, NO alerts sent here.
    """
    logger.info("="*60)
    logger.info(f"FULL SCAN: {datetime.now().strftime('%H:%M:%S')}")
    logger.info("="*60)

    # Get index LTPs
    index_ltps = {}
    for index, config in INDICES.items():
        try:
            quote = kite.quote(config['spot'])
            spot_symbol = config['spot']

            # Validate quote structure
            if spot_symbol not in quote or 'last_price' not in quote[spot_symbol]:
                logger.error(f"{index}: Invalid quote structure")
                continue

            ltp = quote[spot_symbol]['last_price']

            # Validate LTP value
            if ltp is None or ltp <= 0:
                logger.error(f"{index}: Invalid LTP: {ltp}")
                continue

            index_ltps[index] = ltp
            logger.info(f"{index}: {ltp:.2f}")

        except Exception as e:
            logger.error(f"{index} failed: {e}")

    # Scan zones
    zones_db = {}

    for index, ltp in index_ltps.items():
        atm = calculate_atm(ltp, index)
        expiry = get_monthly_expiry(index, instruments)

        if not expiry:
            continue

        logger.info(f"{index}: ATM {atm}, Expiry {expiry.strftime('%d-%b')}")

        for opt_type in ['CE', 'PE']:
            key = (index, atm, opt_type, expiry)
            if key not in instruments:
                continue

            inst = instruments[key]
            symbol = inst['symbol']

            try:
                # Get option quote
                quote_key = f"{inst['exchange']}:{symbol}"
                quote = kite.quote(quote_key)

                # Validate quote structure
                if quote_key not in quote or 'last_price' not in quote[quote_key]:
                    logger.warning(f"{symbol}: Invalid quote structure")
                    continue

                opt_ltp = quote[quote_key]['last_price']

                # Validate LTP
                if opt_ltp is None or opt_ltp <= 0:
                    logger.warning(f"{symbol}: Invalid LTP: {opt_ltp}")
                    continue

                data = get_historical_data(kite, inst['token'])
                zones = find_reversal_zones(data, opt_ltp)

                if not zones:
                    continue

                # Score zones
                for z in zones:
                    z['score'] = score_zone(z, opt_ltp)

                # Remove broken zones
                zones = [z for z in zones if not is_zone_broken(z, opt_ltp)]

                # Keep only strong zones (score > MIN_SCORE)
                zones = [z for z in zones if z['score'] >= MIN_SCORE]

                # Sort by score
                zones.sort(key=lambda x: x['score'], reverse=True)

                if zones:
                    zones_db[symbol] = {
                        'ltp': opt_ltp,
                        'token': inst['token'],
                        'exchange': inst['exchange'],
                        'type': opt_type,
                        'zones': zones
                    }

                    logger.info(f"{symbol}: {len(zones)} zones tracked")

            except Exception as e:
                logger.error(f"{symbol} failed: {str(e)[:50]}")

    # Save zones DB
    save_zones_db(zones_db)
    logger.info(f"Zones DB updated: {len(zones_db)} symbols")
    logger.info("="*60)

# =============================================================================
# QUICK CHECK (Every minute)
# =============================================================================

def quick_check(kite: KiteConnect):
    """
    Quick proximity check.
    Fetches LTP, checks if near zones, sends alerts.
    """
    logger.info(f"Quick check: {datetime.now().strftime('%H:%M:%S')}")

    # Load zones DB
    zones_db = load_zones_db()
    if not zones_db:
        logger.info("No zones in DB, skipping")
        return

    # Load alerts tracker
    alerts_tracker = load_alerts_tracker()

    alerts_sent = 0

    try:
        for symbol, data in zones_db.items():
            # Validate zones_db entry structure
            if 'exchange' not in data or 'zones' not in data or 'type' not in data:
                logger.warning(f"{symbol}: Invalid zones_db entry structure")
                continue

            try:
                # Get current LTP
                quote_key = f"{data['exchange']}:{symbol}"
                quote = kite.quote(quote_key)

                # Validate quote structure
                if quote_key not in quote or 'last_price' not in quote[quote_key]:
                    logger.warning(f"{symbol}: Invalid quote structure")
                    continue

                ltp = quote[quote_key]['last_price']

                # Validate LTP
                if ltp is None or ltp <= 0:
                    continue

                # Check each zone
                for zone in data['zones']:
                    # Validate zone structure
                    if 'price' not in zone or 'low' not in zone or 'score' not in zone:
                        continue

                    # Check if price is within PROXIMITY_PCT of zone
                    zone_center = zone['price']

                    # Guard against invalid values
                    if zone_center <= 0 or ltp <= 0:
                        continue

                    distance_pct = abs(ltp - zone_center) / zone_center * 100

                    if distance_pct <= PROXIMITY_PCT:
                        # Price is near zone!
                        if can_alert(symbol, zone['price'], alerts_tracker):
                            # Send alert
                            msg = format_proximity_alert(symbol, data['type'], zone, ltp, zone['score'])
                            send_telegram(msg)

                            mark_alerted(symbol, zone['price'], alerts_tracker)
                            alerts_sent += 1

                            logger.info(f"ALERT: {symbol} @ {ltp:.0f} near zone {zone['price']} (Score: {zone['score']:.0f})")

            except Exception as e:
                logger.error(f"{symbol} quick check failed: {str(e)[:50]}")

    finally:
        # ALWAYS save alerts tracker, even if there's an exception
        save_alerts_tracker(alerts_tracker)

    if alerts_sent > 0:
        logger.info(f"Quick check: {alerts_sent} alerts sent")

# =============================================================================
# MAIN
# =============================================================================

def main(force=False):
    """Main entry point."""

    if not force and not is_market_open():
        logger.info("Market closed")
        return

    kite = get_kite()
    instruments = fetch_instruments(kite)

    # Determine mode
    if force or is_full_scan_time():
        # Full scan mode
        full_scan(kite, instruments)
    else:
        # Quick check mode
        quick_check(kite)

if __name__ == "__main__":
    import sys
    force = '--test' in sys.argv or '--force' in sys.argv

    try:
        main(force=force)
    except Exception as e:
        logger.error(f"Scanner failed: {e}")
        import traceback
        traceback.print_exc()
