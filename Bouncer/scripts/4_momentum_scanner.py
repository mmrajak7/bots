#!/usr/bin/env python3
"""
Momentum Scanner - HH-HL Pattern Detection on Index Options (OPTIMIZED)

Detects 3 consecutive candles forming Higher Highs and Higher Lows
on ATM ± 1 strikes for NIFTY, BANKNIFTY, SENSEX.

OPTIMIZATIONS (based on forward test - 58% win rate, +1047 P&L):
1. Time Filter: Only trade 10:00-14:30 (skip volatile open/close)
2. Trend Filter: CE only above 20 EMA, PE only below 20 EMA
3. Strike Selection: ATM ± 1 (catches patterns near strike boundaries)
4. Tighter SL: Candle 2 low (not candle 1)
5. Profit Target: 1.5:1 risk-reward
6. Index-level freeze: 1 hour freeze after any alert in an index

Pattern: 3 green candles where each makes HH and HL vs previous
Signal: BUY CE when pattern on CE (above EMA), BUY PE when pattern on PE (below EMA)
SL: Low of second candle in pattern
Target: Entry + 1.5x risk
Trailing SL: Update to previous candle's low (immediate, not waiting for target)

Usage:
    python scripts/4_momentum_scanner.py           # Single scan (respects time filters)
    python scripts/4_momentum_scanner.py --test    # Test mode (no alerts)
    python scripts/4_momentum_scanner.py --force   # Bypass all time filters

Cron Setup (Linux):
    0,15,30,45 9-15 * * 1-5 cd /path/to/Bouncer && python scripts/4_momentum_scanner.py

Time Filters (built into script):
    - Days: Monday to Friday only
    - Hours: 10:00 AM to 2:30 PM (optimized window)
    - Minutes: :00, :15, :30, :45 (on 15M candle close)
    - Delay: 15 seconds wait for candle data to settle
"""

import json
import logging
import argparse
import requests
import shutil
import tempfile
import time
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Tuple, Any
from kiteconnect import KiteConnect


# =============================================================================
# PATHS
# =============================================================================
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
BOTS_DIR = PROJECT_DIR.parent
DATA_DIR = PROJECT_DIR / 'data'
LOGS_DIR = PROJECT_DIR / 'logs'
CONFIG_FILE = PROJECT_DIR / 'config' / 'config.json'

TOKEN_FILE = BOTS_DIR / 'data' / 'kite_access_token.json'
POSITIONS_FILE = DATA_DIR / 'momentum_positions.json'
ALERT_HISTORY_FILE = DATA_DIR / 'momentum_alert_history.json'

# Ensure directories exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


# =============================================================================
# CONFIGURATION
# =============================================================================
# Note: lot_size comes from Kite API at runtime, not from this config
INDEX_CONFIG: Dict[str, Dict[str, Any]] = {
    'NIFTY': {
        'strike_gap': 50,
        'index_token': 256265,  # NSE:NIFTY 50
        'exchange': 'NFO',
    },
    'BANKNIFTY': {
        'strike_gap': 100,
        'index_token': 260105,  # NSE:NIFTY BANK
        'exchange': 'NFO',
    },
    'SENSEX': {
        'strike_gap': 100,
        'index_token': 265,  # BSE:SENSEX
        'exchange': 'BFO',
    }
}

# Strike positions to track relative to ATM
# 0 = ATM, -1 = 1 strike below, 1 = 1 strike above
# We check ATM ± 1 to catch patterns when spot is near strike boundary
STRIKE_OFFSETS = [-1, 0, 1]

# Pattern settings
PATTERN_CANDLES = 3  # Need 3 candles for HH-HL pattern
LOOKBACK_HOURS = 6  # Hours of candle data to fetch (need 20+ candles for EMA)

# Trend filter
EMA_PERIOD = 20  # 20 EMA for trend confirmation

# Target/Risk settings
TARGET_RR_RATIO = 1.5  # 1.5:1 risk-reward ratio

# Position management
MAX_POSITIONS = 3  # Max open positions at any time
MAX_CLOSED_POSITIONS_TO_KEEP = 50  # Cleanup old closed positions
MIN_TRAIL_AMOUNT = 2.0  # Minimum Rs to trail SL (avoid spam alerts)

# Alert freeze - INDEX LEVEL (not symbol level)
# When any option in an index triggers, freeze ALL options in that index
# Rationale: Can't actively trade 2 symbols in same index, and patterns
# on adjacent strikes are just continuation of the same move
ALERT_FREEZE_MINUTES = 60  # 1 hour freeze after any alert in index


# =============================================================================
# LOGGING
# =============================================================================
def setup_logging() -> logging.Logger:
    """Configure logging."""
    log_file = LOGS_DIR / f"momentum_{datetime.now().strftime('%Y%m%d')}.log"

    fmt = logging.Formatter(
        '%(asctime)s | %(levelname)-7s | %(message)s',
        datefmt='%H:%M:%S'
    )

    fh = logging.FileHandler(log_file, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger = logging.getLogger('momentum')
    logger.setLevel(logging.DEBUG)
    logger.handlers = []
    logger.addHandler(fh)
    logger.addHandler(ch)

    return logger


log = setup_logging()


# =============================================================================
# LOAD CONFIG
# =============================================================================
def load_config() -> dict:
    """Load configuration."""
    if not CONFIG_FILE.exists():
        log.warning(f"Config not found: {CONFIG_FILE}, using defaults")
        return {}
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        log.error(f"Config JSON parse error: {e}")
        return {}
    except IOError as e:
        log.error(f"Config read error: {e}")
        return {}


CONFIG = load_config()


# =============================================================================
# KITE CONNECTION
# =============================================================================
def get_kite() -> KiteConnect:
    """Get authenticated Kite connection."""
    if not TOKEN_FILE.exists():
        raise FileNotFoundError(
            f"Token file not found: {TOKEN_FILE}\n"
            "Run: python scripts/kite_auth.py first"
        )

    with open(TOKEN_FILE) as f:
        token_data = json.load(f)

    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


# =============================================================================
# TELEGRAM
# =============================================================================
def send_telegram(message: str, test_mode: bool = False) -> bool:
    """Send Telegram alert."""
    if test_mode:
        log.info(f"[TEST] Would send: {message[:100]}...")
        return True

    telegram_config = CONFIG.get('telegram', {})
    if not telegram_config.get('enabled', False):
        log.debug("Telegram disabled")
        return False

    # Validate required keys exist
    bot_token = telegram_config.get('bot_token')
    chat_id = telegram_config.get('chat_id')
    if not bot_token or not chat_id:
        log.error("Telegram config missing bot_token or chat_id")
        return False

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        response = requests.post(url, json={
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'HTML'
        }, timeout=10)

        if response.status_code == 200:
            log.info("Telegram alert sent")
            return True
        else:
            log.error(f"Telegram failed: {response.status_code} - {response.text}")
            return False
    except requests.RequestException as e:
        log.error(f"Telegram error: {e}")
        return False


# =============================================================================
# INDEX OPTIONS DATA (fetched from Kite public API)
# =============================================================================
def load_index_options() -> Dict[str, List[dict]]:
    """
    Fetch index options from Kite public API (no token needed).
    Filters for monthly expiry only.
    """
    try:
        # Import the shared module
        from kite_instruments import fetch_index_options

        options = fetch_index_options(
            indices=['NIFTY', 'BANKNIFTY', 'SENSEX'],
            monthly_only=True,   # Monthly expiry only
            min_dte=5,           # At least 5 DTE
            max_dte=45           # Max 45 DTE (for monthly)
        )

        log.info(f"Loaded options - NIFTY: {len(options.get('NIFTY', []))}, "
                 f"BANKNIFTY: {len(options.get('BANKNIFTY', []))}, "
                 f"SENSEX: {len(options.get('SENSEX', []))}")

        return options

    except Exception as e:
        log.error(f"Failed to fetch index options: {e}")
        return {}


# =============================================================================
# SPOT PRICE & ATM
# =============================================================================
def get_spot_prices(kite: KiteConnect) -> Dict[str, float]:
    """Get current spot prices for indices."""
    symbols = ['NSE:NIFTY 50', 'NSE:NIFTY BANK', 'BSE:SENSEX']
    mapping = {
        'NSE:NIFTY 50': 'NIFTY',
        'NSE:NIFTY BANK': 'BANKNIFTY',
        'BSE:SENSEX': 'SENSEX'
    }

    try:
        quotes = kite.quote(symbols)
        prices: Dict[str, float] = {}
        for sym, index in mapping.items():
            if sym in quotes and 'last_price' in quotes[sym]:
                prices[index] = float(quotes[sym]['last_price'])
                log.debug(f"{index} spot: {prices[index]:.2f}")
        return prices
    except Exception as e:
        log.error(f"Failed to get spot prices: {e}")
        return {}


def get_atm_strike(spot: float, strike_gap: int) -> int:
    """Calculate ATM strike."""
    return int(round(spot / strike_gap) * strike_gap)


def get_strikes_to_scan(spot: float, index: str) -> List[int]:
    """
    Get strikes to scan around ATM.

    Checks ATM ± 1 strike to catch patterns when spot is near strike boundary.
    For example, if spot=59932 (ATM=59900), we check [59800, 59900, 60000]
    to avoid missing patterns on 60000 when spot is about to cross.

    Returns:
        List of strikes to scan [ATM-gap, ATM, ATM+gap]
    """
    config = INDEX_CONFIG[index]
    strike_gap = int(config['strike_gap'])
    atm = get_atm_strike(spot, strike_gap)

    return [atm + (offset * strike_gap) for offset in STRIKE_OFFSETS]


# =============================================================================
# CANDLE DATA
# =============================================================================
def validate_candle(candle: dict) -> bool:
    """Validate candle has required OHLC fields."""
    required = ['open', 'high', 'low', 'close']
    return all(
        key in candle and isinstance(candle[key], (int, float))
        for key in required
    )


def get_option_candles(
    kite: KiteConnect,
    option_token: int,
    lookback_hours: int = LOOKBACK_HOURS
) -> Optional[List[dict]]:
    """Fetch 15M candles for an option."""
    try:
        to_date = datetime.now()
        from_date = to_date - timedelta(hours=lookback_hours)

        data = kite.historical_data(
            instrument_token=option_token,
            from_date=from_date,
            to_date=to_date,
            interval='15minute'
        )

        if not data:
            return None

        # Validate candles
        valid_candles = [c for c in data if validate_candle(c)]

        if len(valid_candles) >= PATTERN_CANDLES:
            return valid_candles
        return None

    except Exception as e:
        log.debug(f"Failed to fetch candles for token {option_token}: {e}")
        return None


def calculate_ema(candles: List[dict], period: int = EMA_PERIOD) -> Optional[float]:
    """
    Calculate EMA for the given candles.

    Args:
        candles: List of candles with 'close' field
        period: EMA period (default: 20)

    Returns:
        Current EMA value or None if not enough data
    """
    if len(candles) < period:
        return None

    closes = [float(c['close']) for c in candles]

    # Calculate EMA using multiplier
    multiplier = 2 / (period + 1)

    # Start with SMA for first 'period' values
    ema = sum(closes[:period]) / period

    # Apply EMA formula for remaining values
    for close in closes[period:]:
        ema = (close * multiplier) + (ema * (1 - multiplier))

    return ema


# =============================================================================
# PATTERN DETECTION
# =============================================================================
@dataclass
class PatternSignal:
    """Detected HH-HL pattern signal."""
    index: str
    option_symbol: str
    option_type: str  # CE or PE
    strike: float
    expiry: str
    option_token: int
    lot_size: int
    exchange: str
    otm_position: int  # 0=ATM, 1=1st OTM

    # Pattern data
    entry_price: float  # Current LTP
    sl_price: float  # Low of second candle (tighter SL)
    target_price: float  # Entry + 1.5x risk (1.5:1 R:R)
    candle_1_high: float
    candle_1_low: float
    candle_2_high: float
    candle_2_low: float
    candle_3_high: float
    candle_3_low: float

    # Timestamp
    detected_at: str = field(default_factory=lambda: datetime.now().isoformat())


def is_green_candle(candle: dict) -> bool:
    """Check if candle is green (close > open)."""
    return float(candle['close']) > float(candle['open'])


def detect_hh_hl_pattern(
    candles: List[dict]
) -> Optional[Tuple[dict, dict, dict]]:
    """
    Detect 3 consecutive COMPLETED candles with Higher Highs and Higher Lows.
    All candles must be green.

    Args:
        candles: List of candles (last one may be incomplete)

    Returns:
        (candle_1, candle_2, candle_3) if pattern found, else None
    """
    if len(candles) < PATTERN_CANDLES + 1:
        # Need at least 4 candles: 3 completed + 1 current
        return None

    # Take the 3 most recent COMPLETED candles (exclude last which may be current)
    # candles[-4], candles[-3], candles[-2] are the 3 completed ones
    # candles[-1] is the current incomplete candle
    c1, c2, c3 = candles[-4], candles[-3], candles[-2]

    # Validate candles
    if not all(validate_candle(c) for c in [c1, c2, c3]):
        return None

    # All must be green
    if not (is_green_candle(c1) and is_green_candle(c2) and is_green_candle(c3)):
        return None

    # Higher Highs: c2.high > c1.high AND c3.high > c2.high
    if not (c2['high'] > c1['high'] and c3['high'] > c2['high']):
        return None

    # Higher Lows: c2.low > c1.low AND c3.low > c2.low
    if not (c2['low'] > c1['low'] and c3['low'] > c2['low']):
        return None

    return (c1, c2, c3)


# =============================================================================
# POSITION TRACKING
# =============================================================================
@dataclass
class MomentumPosition:
    """Open momentum trade position."""
    id: str
    index: str
    option_symbol: str
    option_type: str
    strike: float
    expiry: str
    option_token: int
    lot_size: int
    exchange: str
    otm_position: int

    entry_price: float
    entry_time: str
    initial_sl: float  # Low of second candle (never changes)
    current_sl: float  # Trailing SL (updates)
    target_price: float  # 1.5:1 R:R target

    status: str = 'open'  # open, closed
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    exit_reason: Optional[str] = None
    target_hit: bool = False  # Track if target was hit (for trailing)

    # Track candle lows for trailing
    last_candle_low: Optional[float] = None


def load_positions() -> List[MomentumPosition]:
    """Load positions from JSON with error handling."""
    if not POSITIONS_FILE.exists():
        return []

    try:
        with open(POSITIONS_FILE) as f:
            data = json.load(f)

        positions: List[MomentumPosition] = []
        for p in data.get('positions', []):
            try:
                # Handle missing optional fields
                p.setdefault('exit_price', None)
                p.setdefault('exit_time', None)
                p.setdefault('exit_reason', None)
                p.setdefault('last_candle_low', None)
                p.setdefault('status', 'open')
                p.setdefault('target_hit', False)
                # For old positions without target, calculate default
                if 'target_price' not in p:
                    entry = p.get('entry_price', 0)
                    sl = p.get('initial_sl', 0)
                    risk = entry - sl if entry > sl else 0
                    p['target_price'] = entry + (risk * TARGET_RR_RATIO)

                positions.append(MomentumPosition(**p))
            except TypeError as e:
                log.warning(f"Skipping invalid position: {e}")
                continue
        return positions
    except (json.JSONDecodeError, KeyError) as e:
        log.error(f"Failed to load positions: {e}")
        return []


def save_positions(positions: List[MomentumPosition]) -> None:
    """Save positions atomically with cleanup of old closed positions."""
    # Cleanup: Keep only recent closed positions
    open_positions = [p for p in positions if p.status == 'open']
    closed_positions = [p for p in positions if p.status == 'closed']

    # Sort closed by exit_time, keep most recent
    closed_positions.sort(
        key=lambda x: x.exit_time or '',
        reverse=True
    )
    closed_positions = closed_positions[:MAX_CLOSED_POSITIONS_TO_KEEP]

    all_positions = open_positions + closed_positions

    data = {
        'last_updated': datetime.now().isoformat(),
        'positions': [asdict(p) for p in all_positions]
    }

    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', dir=DATA_DIR, suffix='.tmp', delete=False
        ) as f:
            json.dump(data, f, indent=2)
            temp_path = Path(f.name)

        shutil.move(str(temp_path), str(POSITIONS_FILE))
        log.debug(f"Saved {len(all_positions)} positions "
                  f"({len(open_positions)} open, {len(closed_positions)} closed)")
    except Exception as e:
        log.error(f"Failed to save positions: {e}")
        if temp_path and temp_path.exists():
            temp_path.unlink()


def generate_position_id(index: str, option_symbol: str) -> str:
    """Generate unique position ID."""
    return f"{index}_{option_symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def is_position_expired(position: MomentumPosition) -> bool:
    """Check if position's option has expired."""
    try:
        expiry_date = datetime.strptime(position.expiry, '%Y-%m-%d').date()
        return datetime.now().date() > expiry_date
    except ValueError:
        return False


# =============================================================================
# ALERT FREEZE (avoid repeat alerts on continuing pattern)
# =============================================================================
def load_alert_history() -> Dict[str, str]:
    """Load alert history (index -> last_alert_time)."""
    if not ALERT_HISTORY_FILE.exists():
        return {}
    try:
        with open(ALERT_HISTORY_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_alert_history(history: Dict[str, str]) -> None:
    """Save alert history."""
    try:
        with open(ALERT_HISTORY_FILE, 'w') as f:
            json.dump(history, f, indent=2)
    except IOError as e:
        log.error(f"Failed to save alert history: {e}")


def is_index_frozen(index: str, history: Dict[str, str]) -> bool:
    """Check if index is in alert freeze period (index-level freeze)."""
    if index not in history:
        return False

    try:
        last_alert = datetime.fromisoformat(history[index])
        elapsed = (datetime.now() - last_alert).total_seconds() / 60
        return elapsed < ALERT_FREEZE_MINUTES
    except (ValueError, TypeError):
        return False


def record_index_alert(index: str, history: Dict[str, str]) -> None:
    """Record alert time for index (freezes all options in that index)."""
    history[index] = datetime.now().isoformat()


def cleanup_old_alerts(history: Dict[str, str]) -> Dict[str, str]:
    """Remove alerts older than freeze period."""
    now = datetime.now()
    cleaned = {}
    for key, time_str in history.items():
        try:
            alert_time = datetime.fromisoformat(time_str)
            elapsed = (now - alert_time).total_seconds() / 60
            if elapsed < ALERT_FREEZE_MINUTES * 2:  # Keep 2x freeze period
                cleaned[key] = time_str
        except (ValueError, TypeError):
            pass
    return cleaned


# =============================================================================
# ALERT FORMATTING
# =============================================================================
def format_entry_alert(signal: PatternSignal) -> str:
    """Format entry alert message."""
    # Map position to label: 0=ATM, positive=OTM, negative=ITM
    if signal.otm_position == 0:
        strike_label = 'ATM'
    elif signal.otm_position > 0:
        strike_label = f'{signal.otm_position}OTM'
    else:
        strike_label = f'{abs(signal.otm_position)}ITM'

    # Calculate percentages (guard against division by zero)
    if signal.entry_price > 0:
        sl_pct = ((signal.entry_price - signal.sl_price) / signal.entry_price) * 100
        target_pct = ((signal.target_price - signal.entry_price) / signal.entry_price) * 100
    else:
        sl_pct = 0.0
        target_pct = 0.0

    return f"""<b>MOMENTUM</b> | {signal.index} {signal.option_type} ({strike_label})

<b>BUY</b> <code>{signal.option_symbol}</code>
Expiry: {signal.expiry}

Entry: ₹{signal.entry_price:.2f}
SL: ₹{signal.sl_price:.2f} (-{sl_pct:.1f}%)
Target: ₹{signal.target_price:.2f} (+{target_pct:.1f}%)"""


def format_sl_update_alert(
    position: MomentumPosition,
    old_sl: float,
    new_sl: float,
    ltp: float
) -> str:
    """Format trailing SL update alert."""
    return f"""<b>MOMENTUM SL UPDATE</b> | {position.index}

{position.option_symbol}
LTP: ₹{ltp:.2f}

SL: ₹{old_sl:.2f} → ₹{new_sl:.2f}"""


def format_exit_alert(
    position: MomentumPosition,
    exit_price: float,
    reason: str
) -> str:
    """Format exit alert message."""
    # Guard against division by zero
    if position.entry_price <= 0:
        pnl = 0.0
        pnl_pct = 0.0
    else:
        pnl = (exit_price - position.entry_price) * position.lot_size
        pnl_pct = ((exit_price / position.entry_price) - 1) * 100

    pnl_sign = "+" if pnl >= 0 else ""

    return f"""<b>MOMENTUM EXIT</b> | {position.index}

<code>{position.option_symbol}</code>
Reason: {reason}

Entry: ₹{position.entry_price:.2f}
Exit: ₹{exit_price:.2f}

P&L: {pnl_sign}₹{pnl:.0f} ({pnl_sign}{pnl_pct:.1f}%)"""


# =============================================================================
# MAIN SCANNER LOGIC
# =============================================================================
def find_option_by_strike(
    options: List[dict],
    strike: float,
    option_type: str,
    expiry: str
) -> Optional[dict]:
    """Find option matching strike, type, and expiry."""
    # Use int comparison to avoid float precision issues
    strike_int = int(strike)
    for opt in options:
        if (int(opt['strike']) == strike_int and
                opt['option_type'] == option_type and
                opt['expiry'] == expiry):
            return opt
    return None


def get_nearest_expiry(options: List[dict]) -> Optional[str]:
    """Get nearest expiry from options list."""
    if not options:
        return None

    expiries = sorted(set(opt['expiry'] for opt in options))
    return expiries[0] if expiries else None


def scan_for_signals(
    kite: KiteConnect,
    index_options: Dict[str, List[dict]],
    spot_prices: Dict[str, float],
    existing_positions: List[MomentumPosition],
    alert_history: Dict[str, str]
) -> List[PatternSignal]:
    """Scan all target options for HH-HL pattern."""
    signals: List[PatternSignal] = []

    # Check max positions limit
    open_positions = [p for p in existing_positions if p.status == 'open']
    open_count = len(open_positions)
    if open_count >= MAX_POSITIONS:
        log.info(f"Max positions ({MAX_POSITIONS}) reached, skipping signal scan")
        return []

    # Get set of existing option symbols to avoid duplicates
    existing_symbols = {p.option_symbol for p in open_positions}

    # Get set of indices with open positions (one position per index rule)
    existing_indices = {p.index for p in open_positions}

    for index, options in index_options.items():
        # Skip if already have open position in this index
        if index in existing_indices:
            log.debug(f"{index}: Already have open position, skipping scan")
            continue

        # Check INDEX freeze EARLY - before fetching any candles (saves API calls)
        if is_index_frozen(index, alert_history):
            log.info(f"{index}: Frozen (alert within {ALERT_FREEZE_MINUTES} min), skipping scan")
            continue

        if index not in spot_prices:
            log.warning(f"{index}: No spot price, skipping")
            continue

        spot = spot_prices[index]
        strikes_to_scan = get_strikes_to_scan(spot, index)
        monthly_expiry = get_nearest_expiry(options)

        if not monthly_expiry:
            log.warning(f"{index}: No monthly expiry found")
            continue

        strike_gap = int(INDEX_CONFIG[index]['strike_gap'])
        atm = get_atm_strike(spot, strike_gap)
        log.info(
            f"{index}: Spot={spot:.0f}, "
            f"ATM={atm}, "
            f"Scanning strikes={strikes_to_scan}, "
            f"Expiry={monthly_expiry}"
        )

        # Check CE and PE for each strike around ATM
        for opt_type in ['CE', 'PE']:
            for strike in strikes_to_scan:
                # Calculate OTM position: 0=ATM, positive=OTM, negative=ITM
                otm_pos = int((strike - atm) / strike_gap) if opt_type == 'CE' else int((atm - strike) / strike_gap)

                opt_data = find_option_by_strike(
                    options, strike, opt_type, monthly_expiry
                )
                if not opt_data:
                    log.debug(f"{index} {opt_type} {strike}: Not found in options")
                    continue

                option_symbol = opt_data['option_symbol']

                # Skip if already have open position
                if option_symbol in existing_symbols:
                    log.debug(f"{option_symbol}: Already have open position")
                    continue

                # Fetch candles
                candles = get_option_candles(kite, opt_data['option_token'])
                if not candles:
                    log.debug(f"{option_symbol}: No candle data")
                    continue

                # Detect pattern
                pattern = detect_hh_hl_pattern(candles)
                if not pattern:
                    log.debug(f"{option_symbol}: No HH-HL pattern")
                    continue

                c1, c2, c3 = pattern

                # Trend filter: CE above EMA, PE below EMA
                ema = calculate_ema(candles)
                if ema:
                    current_close = float(c3['close'])
                    if opt_type == 'CE' and current_close < ema:
                        log.debug(
                            f"{option_symbol}: CE below EMA "
                            f"({current_close:.2f} < {ema:.2f}), skipping"
                        )
                        continue
                    if opt_type == 'PE' and current_close > ema:
                        log.debug(
                            f"{option_symbol}: PE above EMA "
                            f"({current_close:.2f} > {ema:.2f}), skipping"
                        )
                        continue

                # Pattern found with trend confirmation!
                log.info(f"SIGNAL: {option_symbol} - HH-HL pattern (trend aligned)!")

                # Get current LTP
                try:
                    exchange = opt_data['exchange']
                    quote = kite.quote([f"{exchange}:{option_symbol}"])
                    ltp = float(
                        quote[f"{exchange}:{option_symbol}"]['last_price']
                    )
                except Exception as e:
                    log.error(f"{option_symbol}: Failed to get LTP: {e}")
                    ltp = float(c3['close'])  # Fallback

                # Calculate target using 1.5:1 R:R
                sl_price = float(c2['low'])  # Tighter SL using 2nd candle
                risk = ltp - sl_price

                # Skip if risk is invalid (LTP at or below SL)
                if risk <= 0:
                    log.warning(
                        f"{option_symbol}: Invalid risk "
                        f"(LTP={ltp:.2f} <= SL={sl_price:.2f}), skipping"
                    )
                    continue

                target_price = ltp + (risk * TARGET_RR_RATIO)

                signal = PatternSignal(
                    index=index,
                    option_symbol=option_symbol,
                    option_type=opt_type,
                    strike=strike,
                    expiry=monthly_expiry,
                    option_token=opt_data['option_token'],
                    lot_size=opt_data['lot_size'],
                    exchange=opt_data['exchange'],
                    otm_position=otm_pos,
                    entry_price=ltp,
                    sl_price=sl_price,
                    target_price=target_price,
                    candle_1_high=float(c1['high']),
                    candle_1_low=float(c1['low']),
                    candle_2_high=float(c2['high']),
                    candle_2_low=float(c2['low']),
                    candle_3_high=float(c3['high']),
                    candle_3_low=float(c3['low']),
                )
                signals.append(signal)

    return signals


def check_and_update_positions(
    kite: KiteConnect,
    positions: List[MomentumPosition],
    test_mode: bool = False
) -> Tuple[List[MomentumPosition], List[str]]:
    """
    Check open positions:
    1. Auto-close expired positions
    2. Check TARGET HIT (LTP >= target)
    3. Check SL HIT (LTP < current_sl)
    4. Trail SL to previous candle's low (if above current SL and below LTP)

    Returns:
        (updated_positions, alert_messages)
    """
    alerts: List[str] = []

    open_positions = [p for p in positions if p.status == 'open']
    if not open_positions:
        return positions, alerts

    log.info(f"Checking {len(open_positions)} open positions...")

    for pos in open_positions:
        # Check expiry first
        if is_position_expired(pos):
            log.warning(f"{pos.option_symbol}: EXPIRED - closing position")
            pos.status = 'closed'
            pos.exit_time = datetime.now().isoformat()
            pos.exit_reason = 'EXPIRED'

            # Try to get last traded price for accurate P&L
            try:
                quote_sym = f"{pos.exchange}:{pos.option_symbol}"
                quote = kite.quote([quote_sym])
                pos.exit_price = float(quote.get(quote_sym, {}).get('last_price', 0))
            except Exception:
                pos.exit_price = 0.0  # Fallback if quote fails

            alert = format_exit_alert(pos, pos.exit_price, 'Option Expired')
            alerts.append(alert)
            send_telegram(alert, test_mode)
            continue

        try:
            # Get current quote
            quote_sym = f"{pos.exchange}:{pos.option_symbol}"
            quote = kite.quote([quote_sym])
            quote_data = quote.get(quote_sym, {})

            ltp = float(quote_data.get('last_price', 0))
            if ltp <= 0:
                log.warning(f"{pos.option_symbol}: Invalid LTP ({ltp}), skipping")
                continue

            log.debug(
                f"{pos.option_symbol}: LTP={ltp:.2f}, SL={pos.current_sl:.2f}, "
                f"Target={pos.target_price:.2f}"
            )

            # Check TARGET HIT first (priority over SL check)
            if ltp >= pos.target_price:
                log.info(
                    f"{pos.option_symbol}: TARGET HIT! "
                    f"LTP={ltp:.2f} >= Target={pos.target_price:.2f}"
                )

                pos.status = 'closed'
                pos.exit_price = ltp
                pos.exit_time = datetime.now().isoformat()
                pos.exit_reason = 'TARGET_HIT'
                pos.target_hit = True

                alert = format_exit_alert(pos, ltp, 'Target Hit')
                alerts.append(alert)
                send_telegram(alert, test_mode)
                continue

            # Check SL hit using LTP (catches intraday breaches in current candle)
            if ltp < pos.current_sl:
                log.warning(
                    f"{pos.option_symbol}: SL HIT! "
                    f"LTP={ltp:.2f} < SL={pos.current_sl:.2f}"
                )

                pos.status = 'closed'
                pos.exit_price = ltp
                pos.exit_time = datetime.now().isoformat()
                pos.exit_reason = 'SL_HIT'

                alert = format_exit_alert(pos, ltp, 'SL Hit')
                alerts.append(alert)
                # SL alerts tracked locally only (no Telegram)
                log.info(f"SL exit tracked locally: {pos.option_symbol}")
                continue

            # Get latest candles for trailing SL
            candles = get_option_candles(kite, pos.option_token, lookback_hours=1)

            if candles and len(candles) >= 2:
                # Use COMPLETED candle's low for trailing
                # (SL hit already checked using LTP above)
                completed_candle = candles[-2]
                if 'low' not in completed_candle:
                    log.warning(f"{pos.option_symbol}: Candle missing 'low' field, skipping trail")
                    continue
                candle_low = float(completed_candle['low'])

                # Trailing SL: Only trail UP if candle_low > current_sl
                # AND candle_low is below LTP (valid trail)
                if (candle_low > pos.current_sl + MIN_TRAIL_AMOUNT and
                        candle_low < ltp):  # New SL must be below current price
                    old_sl = pos.current_sl
                    pos.current_sl = candle_low
                    pos.last_candle_low = candle_low

                    log.info(
                        f"{pos.option_symbol}: "
                        f"Trailing SL {old_sl:.2f} → {candle_low:.2f}"
                    )
                    # Note: SL updates are logged only, no Telegram alert

        except Exception as e:
            log.error(f"{pos.option_symbol}: Error checking position: {e}")

    return positions, alerts


def _get_strike_priority(sig: PatternSignal) -> int:
    """Get strike priority: ATM (0) > OTM (1) > ITM (-1)."""
    if sig.otm_position == 0:
        return 0  # ATM - highest priority
    elif sig.otm_position > 0:
        return 1  # OTM - second priority
    else:
        return 2  # ITM - lowest priority


def _get_strike_label(otm_position: int) -> str:
    """Get human-readable strike label."""
    if otm_position == 0:
        return 'ATM'
    elif otm_position > 0:
        return f'{otm_position}OTM'
    else:
        return f'{abs(otm_position)}ITM'


def select_best_signals(
    signals: List[PatternSignal],
    max_signals: int = MAX_POSITIONS
) -> List[PatternSignal]:
    """
    Select best signal per INDEX (not per index+option_type).

    Logic:
    1. Group all signals by INDEX
    2. Within each index, pick ONE best signal (ATM preferred, then OTM)
    3. If both CE and PE have patterns in same index, pick the one with
       better strike position (ATM over OTM over ITM)
    4. Limit total signals to max_signals (for position limit enforcement)

    Returns:
        List with at most one signal per index, limited to max_signals
    """
    if not signals:
        return []

    # Group by INDEX only (not by option_type)
    # This prevents both CE and PE from alerting in same scan
    groups: Dict[str, List[PatternSignal]] = {}
    for sig in signals:
        if sig.index not in groups:
            groups[sig.index] = []
        groups[sig.index].append(sig)

    best_signals: List[PatternSignal] = []

    for index, group in groups.items():
        # Sort by: strike priority (ATM > OTM > ITM)
        # If tie, CE comes before PE (arbitrary but consistent)
        group.sort(key=lambda s: (_get_strike_priority(s), s.option_type))
        best = group[0]

        # Log what was found and selected
        if len(group) > 1:
            found_list = [f"{s.option_type} {_get_strike_label(s.otm_position)}" for s in group]
            selected = f"{best.option_type} {_get_strike_label(best.otm_position)}"
            log.info(
                f"{index}: Multiple patterns found {found_list}, "
                f"selected {selected} (one per index, prefer ATM)"
            )

        best_signals.append(best)

    # Limit to max_signals to enforce position limits
    if len(best_signals) > max_signals:
        log.info(
            f"Limiting signals from {len(best_signals)} to {max_signals} "
            f"(position limit)"
        )
        best_signals = best_signals[:max_signals]

    return best_signals


def process_signals(
    signals: List[PatternSignal],
    positions: List[MomentumPosition],
    alert_history: Dict[str, str],
    test_mode: bool = False
) -> List[MomentumPosition]:
    """Process new signals, create positions, send alerts.

    Note: Index freeze is already checked in scan_for_signals() before signals
    are generated. We still record the alert to freeze future scans.
    """
    # Calculate available position slots
    open_count = len([p for p in positions if p.status == 'open'])
    available_slots = max(0, MAX_POSITIONS - open_count)

    if available_slots == 0:
        log.info("No position slots available, skipping signal processing")
        return positions

    # Select best signal per INDEX (ATM preferred, limited to available slots)
    signals = select_best_signals(signals, max_signals=available_slots)

    for signal in signals:
        position = MomentumPosition(
            id=generate_position_id(signal.index, signal.option_symbol),
            index=signal.index,
            option_symbol=signal.option_symbol,
            option_type=signal.option_type,
            strike=signal.strike,
            expiry=signal.expiry,
            option_token=signal.option_token,
            lot_size=signal.lot_size,
            exchange=signal.exchange,
            otm_position=signal.otm_position,
            entry_price=signal.entry_price,
            entry_time=signal.detected_at,
            initial_sl=signal.sl_price,
            current_sl=signal.sl_price,
            target_price=signal.target_price,
            last_candle_low=signal.sl_price
        )
        positions.append(position)

        alert = format_entry_alert(signal)
        log.info(
            f"New position: {signal.option_symbol} "
            f"@ {signal.entry_price:.2f}, SL={signal.sl_price:.2f}, "
            f"Target={signal.target_price:.2f}"
        )

        # Record alert at INDEX level (freezes all options in this index)
        record_index_alert(signal.index, alert_history)
        send_telegram(alert, test_mode)

    return positions


# =============================================================================
# TIME FILTERS & DELAY
# =============================================================================
VALID_MINUTES = [0, 15, 30, 45]  # On 15M candle close
SCAN_DELAY_SECONDS = 15  # Wait for candle data to settle
MARKET_START = (10, 0)   # 10:00 AM (skip volatile open)
MARKET_END = (14, 30)    # 2:30 PM (skip volatile close)


def check_time_filters() -> Tuple[bool, str]:
    """
    Check all time filters:
    1. Monday to Friday only
    2. Time between 10:00 AM and 2:30 PM (optimized window)
    3. Minute is in [0, 15, 30, 45] (15M candle close times)

    Returns:
        (is_valid, reason_if_invalid)
    """
    now = datetime.now()

    # Check weekday (0=Monday, 4=Friday)
    if now.weekday() > 4:
        return False, f"Weekend (day={now.weekday()})"

    # Check minute
    if now.minute not in VALID_MINUTES:
        return False, f"Invalid minute ({now.minute} not in {VALID_MINUTES})"

    # Check time range (10:00 to 14:30)
    current_time = (now.hour, now.minute)
    if current_time < MARKET_START:
        return False, f"Before window ({now.strftime('%H:%M')} < 10:00)"
    if current_time > MARKET_END:
        return False, f"After window ({now.strftime('%H:%M')} > 14:30)"

    return True, "OK"


def wait_for_candle_data() -> None:
    """Wait for candle data to settle after candle close."""
    log.info(f"Waiting {SCAN_DELAY_SECONDS}s for candle data to settle...")
    time.sleep(SCAN_DELAY_SECONDS)


# =============================================================================
# MAIN
# =============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description='Momentum Scanner - HH-HL Pattern'
    )
    parser.add_argument(
        '--test', action='store_true',
        help='Test mode (no Telegram alerts)'
    )
    parser.add_argument(
        '--force', action='store_true',
        help='Bypass all time filters'
    )
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("MOMENTUM SCANNER - HH-HL Pattern Detection")
    log.info("=" * 60)

    # Check time filters (day, hour, minute)
    if not args.force:
        is_valid, reason = check_time_filters()
        if not is_valid:
            log.info(f"Skipping: {reason}. Use --force to bypass.")
            return

        # Wait for candle data to settle
        wait_for_candle_data()

    try:
        # Connect to Kite
        kite = get_kite()
        log.info("Kite connected")

        # Load index options (monthly only)
        index_options = load_index_options()
        if not index_options:
            log.error("No index options loaded. Run 1_fetch_symbols.py first.")
            return

        # Get spot prices
        spot_prices = get_spot_prices(kite)
        if not spot_prices:
            log.error("Failed to get spot prices")
            return

        # Load existing positions
        positions = load_positions()
        open_count = len([p for p in positions if p.status == 'open'])
        log.info(f"Loaded {len(positions)} positions ({open_count} open)")

        # Load alert history (for freeze mechanism)
        alert_history = load_alert_history()
        alert_history = cleanup_old_alerts(alert_history)

        # 1. Check and update existing positions (trail SL or exit)
        positions, sl_alerts = check_and_update_positions(
            kite, positions, args.test
        )

        # 2. Scan for new signals (freeze check happens inside, saves API calls)
        signals = scan_for_signals(
            kite, index_options, spot_prices, positions, alert_history
        )
        log.info(f"Found {len(signals)} new signals")

        # 3. Process new signals (with alert freeze check)
        if signals:
            positions = process_signals(
                signals, positions, alert_history, args.test
            )

        # 4. Save positions and alert history
        save_positions(positions)
        save_alert_history(alert_history)

        # Summary
        open_positions = [p for p in positions if p.status == 'open']
        log.info(f"Scan complete. Open positions: {len(open_positions)}")

        for pos in open_positions:
            log.info(
                f"  {pos.option_symbol}: "
                f"Entry={pos.entry_price:.2f}, SL={pos.current_sl:.2f}"
            )

    except FileNotFoundError as e:
        log.error(str(e))
    except Exception as e:
        log.exception(f"Scanner error: {e}")


if __name__ == '__main__':
    main()
