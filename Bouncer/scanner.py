#!/usr/bin/env python3
"""
Bouncer - Support/Resistance Bounce Strategy Scanner

Identifies high-probability trading setups at key technical levels.
Uses 4% target approach for optimal strike selection.
Integrates with Google Sheets and Telegram for alerts.
"""

import json
import logging
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta, date
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, asdict
import requests
from kiteconnect import KiteConnect

# Optional Google Sheets integration
try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    GSHEET_AVAILABLE = True
except ImportError:
    GSHEET_AVAILABLE = False

# ============================================================================
# CONFIGURATION
# ============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = SCRIPT_DIR / 'config' / 'config.json'

with open(CONFIG_FILE) as f:
    CONFIG = json.load(f)

# Resolve paths relative to BOTS folder
BOTS_DIR = SCRIPT_DIR.parent
TOKEN_FILE = BOTS_DIR / CONFIG['paths']['kite_token'].lstrip('../')
GSHEET_CREDS = BOTS_DIR / CONFIG['paths']['gsheet_credentials'].lstrip('../')
LOGS_DIR = SCRIPT_DIR / CONFIG['paths']['logs_dir']
LOGS_DIR.mkdir(exist_ok=True)

# ============================================================================
# LOGGING SETUP
# ============================================================================

def setup_logging():
    """Configure logging."""
    log_file = LOGS_DIR / f"scanner_{datetime.now().strftime('%Y%m%d')}.log"

    handlers = []
    if CONFIG['logging']['console']:
        handlers.append(logging.StreamHandler())
    if CONFIG['logging']['file']:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=getattr(logging, CONFIG['logging']['level']),
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=handlers
    )
    return logging.getLogger(__name__)

logger = setup_logging()

# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class Level:
    """Represents a support/resistance level."""
    price: float
    level_type: str
    touches: int
    first_touch: str
    last_touch: str
    is_polarity_flip: bool
    volume_at_touches: float
    score: int = 0

@dataclass
class Setup:
    """Represents a potential trade setup."""
    symbol: str
    ltp: float
    level_price: float
    level_type: str
    level_score: int
    touches: int
    is_polarity_flip: bool
    distance_pct: float
    direction: str
    strategy: str
    long_strike: int
    short_strike: int
    spread_width: int
    expiry: str
    dte: int
    timestamp: str

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
# TELEGRAM
# ============================================================================

def send_telegram(message: str) -> bool:
    """Send message via Telegram."""
    if not CONFIG['telegram']['enabled']:
        return False

    try:
        url = f"https://api.telegram.org/bot{CONFIG['telegram']['bot_token']}/sendMessage"
        payload = {
            'chat_id': CONFIG['telegram']['chat_id'],
            'text': message,
            'parse_mode': 'HTML'
        }
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False

def format_alert(setup: Setup) -> str:
    """Format setup as Telegram alert."""
    emoji = "🟢" if setup.direction == "BULLISH" else "🔴"
    flip = "✨ POLARITY FLIP" if setup.is_polarity_flip else ""

    return f"""
{emoji} <b>BOUNCER ALERT</b> {emoji}

<b>{setup.symbol}</b> - {setup.direction}
Score: <b>{setup.level_score}</b> ({setup.touches} touches) {flip}

Level: ₹{setup.level_price:.2f} ({setup.level_type})
LTP: ₹{setup.ltp:.2f} ({setup.distance_pct:.1f}% away)

<b>Strategy:</b> {setup.strategy}
Expiry: {setup.expiry} ({setup.dte} DTE)

⏰ {setup.timestamp}
"""

# ============================================================================
# GOOGLE SHEETS
# ============================================================================

def get_gsheet_client():
    """Get authenticated Google Sheets client."""
    if not GSHEET_AVAILABLE:
        logger.warning("gspread/oauth2client not installed. Skipping Google Sheets.")
        return None

    if not CONFIG['google_sheets']['enabled']:
        return None

    if not GSHEET_CREDS.exists():
        logger.warning(f"Google credentials not found at {GSHEET_CREDS}")
        return None

    try:
        scope = [
            'https://spreadsheets.google.com/feeds',
            'https://www.googleapis.com/auth/drive'
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(str(GSHEET_CREDS), scope)
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        logger.error(f"Google Sheets auth failed: {e}")
        return None

def update_google_sheet(setups: List[Setup]):
    """Update Google Sheet with current setups."""
    client = get_gsheet_client()
    if not client:
        return

    try:
        spreadsheet = client.open_by_key(CONFIG['google_sheets']['spreadsheet_id'])

        # Try to get or create sheet
        sheet_name = CONFIG['google_sheets']['sheet_name']
        try:
            worksheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=100, cols=20)

        # Clear and write headers
        worksheet.clear()
        headers = [
            'Timestamp', 'Symbol', 'Direction', 'Score', 'Touches',
            'Level', 'Level Type', 'Polarity Flip', 'LTP', 'Distance %',
            'Strategy', 'Long Strike', 'Short Strike', 'Spread Width',
            'Expiry', 'DTE'
        ]
        worksheet.append_row(headers)

        # Write setups sorted by score
        for setup in sorted(setups, key=lambda x: x.level_score, reverse=True):
            row = [
                setup.timestamp,
                setup.symbol,
                setup.direction,
                setup.level_score,
                setup.touches,
                setup.level_price,
                setup.level_type,
                'YES' if setup.is_polarity_flip else 'No',
                setup.ltp,
                f"{setup.distance_pct:.2f}",
                setup.strategy,
                setup.long_strike,
                setup.short_strike,
                setup.spread_width,
                setup.expiry,
                setup.dte
            ]
            worksheet.append_row(row)

        logger.info(f"Updated Google Sheet with {len(setups)} setups")

    except Exception as e:
        logger.error(f"Google Sheet update failed: {e}")

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
        logger.error(f"Error fetching instrument token for {symbol}: {e}")
    return None

def get_historical_data(kite: KiteConnect, symbol: str, days: int = 180) -> Optional[pd.DataFrame]:
    """Fetch historical OHLC data."""
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
        logger.error(f"Error fetching historical data for {symbol}: {e}")
        return None

# ============================================================================
# SUPPORT/RESISTANCE DETECTION
# ============================================================================

def find_swing_points(df: pd.DataFrame, window: int = 5) -> Tuple[List, List]:
    """Find swing highs and swing lows."""
    swing_lows = []
    swing_highs = []

    for i in range(window, len(df) - window):
        is_swing_low = all(
            df['low'].iloc[i] < df['low'].iloc[i - j] and
            df['low'].iloc[i] < df['low'].iloc[i + j]
            for j in range(1, window + 1)
        )
        if is_swing_low:
            swing_lows.append((df.index[i], df['low'].iloc[i], df['volume'].iloc[i]))

        is_swing_high = all(
            df['high'].iloc[i] > df['high'].iloc[i - j] and
            df['high'].iloc[i] > df['high'].iloc[i + j]
            for j in range(1, window + 1)
        )
        if is_swing_high:
            swing_highs.append((df.index[i], df['high'].iloc[i], df['volume'].iloc[i]))

    return swing_lows, swing_highs

def cluster_levels(points: List, tolerance_pct: float = 1.5) -> List[Dict]:
    """Cluster nearby price points into levels."""
    if not points:
        return []

    sorted_points = sorted(points, key=lambda x: x[1])
    clusters = []
    current_cluster = [sorted_points[0]]

    for point in sorted_points[1:]:
        cluster_avg = np.mean([p[1] for p in current_cluster])
        if abs(point[1] - cluster_avg) / cluster_avg * 100 <= tolerance_pct:
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
    """Detect if a level has flipped polarity."""
    tolerance = level_price * tolerance_pct / 100

    touches_below = df[df['high'].between(level_price - tolerance, level_price + tolerance)]
    touches_above = df[df['low'].between(level_price - tolerance, level_price + tolerance)]

    if level_type == 'support' and len(touches_below) > 0 and len(touches_above) > 0:
        if touches_below.index.min() < touches_above.index.max():
            return True
    elif level_type == 'resistance' and len(touches_above) > 0 and len(touches_below) > 0:
        if touches_above.index.min() < touches_below.index.max():
            return True
    return False

def is_near_round_number(price: float) -> bool:
    """Check if price is within 1% of a round number."""
    rn = CONFIG['round_numbers']

    if price < 500:
        levels = rn['below_500']
    elif price < 1000:
        levels = rn['500_to_1000']
    elif price < 2000:
        levels = rn['1000_to_2000']
    elif price < 5000:
        levels = rn['2000_to_5000']
    else:
        levels = rn['above_5000']

    return any(abs(price - level) / level * 100 <= 1.0 for level in levels)

def score_level(touches: int, is_flip: bool, is_round: bool,
                is_recent: bool, high_volume: bool) -> int:
    """Score a support/resistance level."""
    scoring = CONFIG['scoring']
    score = 0

    if touches >= 4:
        score += scoring['touches_4_plus']
    elif touches >= 3:
        score += scoring['touches_3']
    elif touches >= 2:
        score += scoring['touches_2']

    if is_flip:
        score += scoring['resistance_turned_support']
    if is_round:
        score += scoring['round_number_confluence']
    if is_recent:
        score += scoring['recent_touch']
    if high_volume:
        score += scoring['high_volume_at_level']

    return score

def find_levels(df: pd.DataFrame) -> List[Level]:
    """Find all support and resistance levels."""
    sr_config = CONFIG['support_resistance']
    min_touches = sr_config['min_touches']
    tolerance = sr_config['touch_tolerance_pct']
    recency_days = sr_config['recency_bonus_days']

    swing_lows, swing_highs = find_swing_points(df)
    support_clusters = cluster_levels(swing_lows, tolerance)
    resistance_clusters = cluster_levels(swing_highs, tolerance)

    levels = []
    avg_volume = df['volume'].mean()

    for cluster in support_clusters:
        if cluster['touches'] >= min_touches:
            is_flip = detect_polarity_flip(df, cluster['price'], 'support', tolerance)
            is_round = is_near_round_number(cluster['price'])
            last_touch = max(cluster['dates'])
            is_recent = (datetime.now() - last_touch.to_pydatetime().replace(tzinfo=None)).days <= recency_days
            high_vol = np.mean(cluster['volumes']) > avg_volume * 1.5

            level = Level(
                price=round(cluster['price'], 2),
                level_type='support',
                touches=cluster['touches'],
                first_touch=str(min(cluster['dates']).date()),
                last_touch=str(last_touch.date()),
                is_polarity_flip=is_flip,
                volume_at_touches=np.mean(cluster['volumes']),
                score=score_level(cluster['touches'], is_flip, is_round, is_recent, high_vol)
            )
            levels.append(level)

    for cluster in resistance_clusters:
        if cluster['touches'] >= min_touches:
            is_flip = detect_polarity_flip(df, cluster['price'], 'resistance', tolerance)
            is_round = is_near_round_number(cluster['price'])
            last_touch = max(cluster['dates'])
            is_recent = (datetime.now() - last_touch.to_pydatetime().replace(tzinfo=None)).days <= recency_days
            high_vol = np.mean(cluster['volumes']) > avg_volume * 1.5

            level = Level(
                price=round(cluster['price'], 2),
                level_type='resistance',
                touches=cluster['touches'],
                first_touch=str(min(cluster['dates']).date()),
                last_touch=str(last_touch.date()),
                is_polarity_flip=is_flip,
                volume_at_touches=np.mean(cluster['volumes']),
                score=score_level(cluster['touches'], is_flip, is_round, is_recent, high_vol)
            )
            levels.append(level)

    levels.sort(key=lambda x: x.score, reverse=True)
    return levels

# ============================================================================
# EXPIRY & STRIKE HELPERS
# ============================================================================

def get_valid_expiry(kite: KiteConnect, symbol: str) -> Tuple[Optional[str], Optional[int]]:
    """Get valid expiry with sufficient DTE."""
    min_dte = CONFIG['entry_rules']['min_dte']
    preferred_min = CONFIG['entry_rules']['preferred_dte_min']
    preferred_max = CONFIG['entry_rules']['preferred_dte_max']

    try:
        instruments = kite.instruments('NFO')
        stock_opts = [i for i in instruments
                      if i['name'] == symbol
                      and i['instrument_type'] == 'CE'
                      and i['expiry'] is not None]

        if not stock_opts:
            return None, None

        today = date.today()
        expiries = sorted(set(i['expiry'] for i in stock_opts))

        # First try preferred DTE range
        for exp in expiries:
            dte = (exp - today).days
            if preferred_min <= dte <= preferred_max:
                return exp.strftime('%y%b%d').upper(), dte

        # Fall back to minimum DTE
        for exp in expiries:
            dte = (exp - today).days
            if dte >= min_dte:
                return exp.strftime('%y%b%d').upper(), dte

        return None, None
    except Exception as e:
        logger.error(f"Error getting expiry for {symbol}: {e}")
        return None, None

def get_strike_interval(kite: KiteConnect, symbol: str) -> int:
    """Determine strike interval for a stock."""
    try:
        instruments = kite.instruments('NFO')
        stock_opts = [i for i in instruments
                      if i['name'] == symbol
                      and i['instrument_type'] == 'CE']

        if len(stock_opts) < 2:
            return 10

        strikes = sorted(set(i['strike'] for i in stock_opts))
        if len(strikes) < 2:
            return 10

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

# ============================================================================
# SETUP IDENTIFICATION (4% TARGET APPROACH)
# ============================================================================

def identify_setups(kite: KiteConnect, symbol: str, df: pd.DataFrame,
                    levels: List[Level], ltp: float) -> List[Setup]:
    """
    Identify trade setups using 4% target approach.
    Long strike at level, short strike at target.
    """
    setups = []
    max_distance = CONFIG['entry_rules']['max_distance_from_level_pct']
    min_score = CONFIG['scoring']['min_score_to_trade']
    target_pct = CONFIG['spread_config']['target_pct']

    expiry, dte = get_valid_expiry(kite, symbol)
    if not expiry or not dte:
        logger.debug(f"{symbol}: No valid expiry with sufficient DTE")
        return []

    interval = get_strike_interval(kite, symbol)
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    for level in levels:
        if level.score < min_score:
            continue

        distance_pct = abs(ltp - level.price) / level.price * 100
        if distance_pct > max_distance:
            continue

        # 4% TARGET APPROACH
        if level.level_type == 'support' and ltp >= level.price:
            direction = 'BULLISH'
            # Long strike at support, short at 4% target
            long_strike = round_to_strike(level.price, interval, 'down')
            target_price = ltp * (1 + target_pct / 100)
            short_strike = round_to_strike(target_price, interval, 'down')
            opt_type = 'CE'
            strategy = f"Bull Call Spread {long_strike}/{short_strike}"

        elif level.level_type == 'resistance' and ltp <= level.price:
            direction = 'BEARISH'
            # Long strike at resistance, short at 4% below
            long_strike = round_to_strike(level.price, interval, 'up')
            target_price = ltp * (1 - target_pct / 100)
            short_strike = round_to_strike(target_price, interval, 'up')
            opt_type = 'PE'
            strategy = f"Bear Put Spread {long_strike}/{short_strike}"
        else:
            continue

        spread_width = abs(long_strike - short_strike)

        setup = Setup(
            symbol=symbol,
            ltp=round(ltp, 2),
            level_price=level.price,
            level_type=level.level_type,
            level_score=level.score,
            touches=level.touches,
            is_polarity_flip=level.is_polarity_flip,
            distance_pct=round(distance_pct, 2),
            direction=direction,
            strategy=strategy,
            long_strike=int(long_strike),
            short_strike=int(short_strike),
            spread_width=int(spread_width),
            expiry=expiry,
            dte=dte,
            timestamp=timestamp
        )
        setups.append(setup)

    return setups

# ============================================================================
# MAIN SCANNER
# ============================================================================

def scan_stock(kite: KiteConnect, symbol: str) -> List[Setup]:
    """Scan a single stock for setups."""
    df = get_historical_data(kite, symbol, CONFIG['support_resistance']['lookback_days'])
    if df is None or len(df) < 50:
        return []

    try:
        ltp_data = kite.ltp([f'NSE:{symbol}'])
        ltp = ltp_data[f'NSE:{symbol}']['last_price']
    except:
        return []

    levels = find_levels(df)
    return identify_setups(kite, symbol, df, levels, ltp)

def run_scanner(send_alerts: bool = True) -> List[Setup]:
    """Main scanner function."""
    logger.info("="*60)
    logger.info("BOUNCER SCANNER STARTING")
    logger.info("="*60)

    kite = get_kite()
    stocks = CONFIG['stock_universe']['active_stocks']
    exclude = CONFIG['stock_universe'].get('exclude', [])
    stocks = [s for s in stocks if s not in exclude]

    logger.info(f"Scanning {len(stocks)} stocks...")

    all_setups = []
    for symbol in stocks:
        try:
            setups = scan_stock(kite, symbol)
            if setups:
                logger.info(f"  {symbol}: {len(setups)} setup(s)")
            all_setups.extend(setups)
        except Exception as e:
            logger.error(f"  {symbol}: Error - {e}")

    # Sort by score
    all_setups.sort(key=lambda x: x.level_score, reverse=True)

    logger.info(f"Total setups found: {len(all_setups)}")

    # Update Google Sheet
    if all_setups:
        update_google_sheet(all_setups)

    # Send Telegram alerts for high-score setups
    alert_score = CONFIG['scoring']['alert_score']
    if send_alerts:
        for setup in all_setups:
            if setup.level_score >= alert_score:
                msg = format_alert(setup)
                if send_telegram(msg):
                    logger.info(f"Alert sent for {setup.symbol} (Score: {setup.level_score})")

    return all_setups

def print_results(setups: List[Setup]):
    """Print results to console."""
    if not setups:
        print("\nNo setups found.")
        return

    print("\n" + "="*80)
    print("BOUNCER SCAN RESULTS")
    print("="*80)
    print(f"\nFound {len(setups)} setup(s):\n")

    for i, s in enumerate(setups, 1):
        flip = "FLIP" if s.is_polarity_flip else ""
        print(f"{i}. {s.symbol} - {s.direction} (Score: {s.level_score}) {flip}")
        print(f"   Level: {s.level_price} ({s.level_type}), LTP: {s.ltp}, Dist: {s.distance_pct}%")
        print(f"   Strategy: {s.strategy}")
        print(f"   Expiry: {s.expiry} ({s.dte} DTE)")
        print()

def analyze_stock(symbol: str):
    """Detailed analysis of a single stock."""
    print("="*70)
    print(f"DETAILED ANALYSIS: {symbol}")
    print("="*70)

    kite = get_kite()
    df = get_historical_data(kite, symbol, CONFIG['support_resistance']['lookback_days'])

    if df is None:
        print("Failed to fetch data")
        return

    ltp_data = kite.ltp([f'NSE:{symbol}'])
    ltp = ltp_data[f'NSE:{symbol}']['last_price']

    print(f"\nCurrent Price: Rs {ltp:.2f}")
    print(f"52W High: Rs {df['high'].max():.2f}")
    print(f"52W Low: Rs {df['low'].min():.2f}")

    levels = find_levels(df)

    print("\nKEY LEVELS:")
    print("-"*70)
    print(f"{'Level':>10} {'Type':>12} {'Touches':>8} {'Score':>8} {'Flip':>6} {'Last Touch':>12}")
    print("-"*70)

    for level in levels[:10]:
        print(f"{level.price:>10.2f} {level.level_type:>12} {level.touches:>8} "
              f"{level.score:>8} {'YES' if level.is_polarity_flip else 'No':>6} {level.last_touch:>12}")

    setups = identify_setups(kite, symbol, df, levels, ltp)

    if setups:
        print("\nACTIVE SETUPS:")
        print("-"*70)
        for setup in setups:
            print(f"  {setup.direction} - {setup.strategy}")
            print(f"  Level: {setup.level_price}, Score: {setup.level_score}, DTE: {setup.dte}")
            print()

# ============================================================================
# CLI
# ============================================================================

if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1:
        if sys.argv[1] == '--no-alerts':
            setups = run_scanner(send_alerts=False)
        else:
            analyze_stock(sys.argv[1].upper())
    else:
        setups = run_scanner(send_alerts=True)

    if 'setups' in dir():
        print_results(setups)
