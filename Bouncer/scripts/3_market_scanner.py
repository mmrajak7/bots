#!/usr/bin/env python3
"""
Step 3: Market Scanner (9:15 AM - 3:30 PM, every 5 mins)
========================================================
BULLISH ONLY - Support bounce strategy

- Loads pre-computed SUPPORT levels from levels.json
- Fetches LTP for all stocks (single batch call)
- Checks if price is near support level
- Invalidates broken levels
- When at support: builds Bull Call Spread and sends Telegram alert
- For 90+ score setups with illiquid options -> FUTURES LONG alert
- Position tracking in open_positions.json
- Exit signal checking (TP intraday, SL at 9 AM morning)

Run: python scripts/3_market_scanner.py
     python scripts/3_market_scanner.py --test       (no alerts)
     python scripts/3_market_scanner.py --loop       (continuous)
     python scripts/3_market_scanner.py --check-sl   (morning SL check)

Cron: */5 9-15 * * 1-5
"""

import json
import sys
import time
import csv
import logging
import requests
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict, field
from kiteconnect import KiteConnect
import pytz

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False  # Windows doesn't have fcntl

# ============================================================================
# PATHS & CONFIG
# ============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
BOUNCER_DIR = SCRIPT_DIR.parent
DATA_DIR = BOUNCER_DIR.parent / 'data'
CONFIG_FILE = BOUNCER_DIR / 'config' / 'config.json'
LEVELS_FILE = BOUNCER_DIR / 'data' / 'levels.json'
POSITIONS_FILE = BOUNCER_DIR / 'data' / 'open_positions.json'
RELIABILITY_FILE = BOUNCER_DIR / 'data' / 'sr_reliability.json'
LOGS_DIR = BOUNCER_DIR / 'logs'
STOCK_INSTRUMENTS_FILE = DATA_DIR / 'stock_instruments.csv'
LOGS_DIR.mkdir(exist_ok=True)

try:
    with open(CONFIG_FILE) as f:
        CONFIG = json.load(f)
except FileNotFoundError:
    print(f"ERROR: Config file not found: {CONFIG_FILE}")
    sys.exit(1)
except json.JSONDecodeError as e:
    print(f"ERROR: Config file is malformed: {e}")
    sys.exit(1)

# Futures alert thresholds
FUTURES_SCORE_THRESHOLD = 90  # Minimum score for futures consideration
FUTURES_ENABLED = CONFIG.get('futures', {}).get('enabled', True)

# Reliability filter - skip stocks that historically don't respect S/R levels
MIN_RELIABILITY_RATE = CONFIG.get('reliability', {}).get('min_success_rate', 0.30)
RELIABILITY_DATA: Dict = {}  # Loaded at scan time

def load_reliability_data():
    """Load S/R reliability scores for filtering poor performers."""
    global RELIABILITY_DATA
    RELIABILITY_DATA = {}

    if not RELIABILITY_FILE.exists():
        return

    try:
        with open(RELIABILITY_FILE) as f:
            data = json.load(f)
            RELIABILITY_DATA = data.get('stocks', {})
    except Exception as e:
        log.warning(f"Failed to load reliability data: {e}")

# ============================================================================
# INSTRUMENTS CACHE (loaded once from CSV - no manual symbol construction!)
# ============================================================================

INSTRUMENTS_CACHE = {}  # {symbol: [{strike, expiry, option_type, option_symbol, lot_size, dte}, ...]}

def load_instruments_cache():
    """Load instruments from CSV. NEVER construct symbols manually!"""
    global INSTRUMENTS_CACHE
    INSTRUMENTS_CACHE = {}

    if not STOCK_INSTRUMENTS_FILE.exists():
        return

    with open(STOCK_INSTRUMENTS_FILE) as f:
        reader = csv.DictReader(f)
        for row in reader:
            symbol = row.get('symbol', '')
            if not symbol or not row.get('option_symbol'):
                continue

            if symbol not in INSTRUMENTS_CACHE:
                INSTRUMENTS_CACHE[symbol] = []

            try:
                INSTRUMENTS_CACHE[symbol].append({
                    'strike': float(row['strike']),
                    'expiry': row['expiry'],
                    'option_type': row['option_type'],
                    'option_symbol': row['option_symbol'],  # Use this directly!
                    'lot_size': int(row['lot_size'] or 1),
                    'dte': int(row['dte'] or 0)
                })
            except (ValueError, KeyError):
                continue

# Load on module import
load_instruments_cache()

# ============================================================================
# LOGGING SETUP
# ============================================================================

def setup_logging():
    """Configure logging to file and console."""
    log_file = LOGS_DIR / f"scanner_{datetime.now().strftime('%Y%m%d')}.log"

    # Create formatter
    fmt = logging.Formatter(
        '%(asctime)s | %(levelname)-7s | %(message)s',
        datefmt='%H:%M:%S'
    )

    # File handler - all levels
    fh = logging.FileHandler(log_file, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    # Console handler - INFO and above
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    # Setup logger
    logger = logging.getLogger('bouncer')
    logger.setLevel(logging.DEBUG)
    logger.handlers = []  # Clear existing
    logger.addHandler(fh)
    logger.addHandler(ch)

    return logger

log = setup_logging()

# Thresholds
ALERT_DISTANCE_PCT = 0.5      # Alert when within 0.5% of level
APPROACH_DISTANCE_PCT = 1.5   # Consider "approaching" within 1.5%
INVALIDATE_PCT = 1.5          # Invalidate if breaks by 1.5%
MIN_DTE = CONFIG.get('entry_rules', {}).get('preferred_dte_min', 15)

# Slippage thresholds from config (spread × lot_size for BOTH legs combined)
slippage_cfg = CONFIG.get('entry_rules', {}).get('slippage', {})
MAX_ENTRY_SLIPPAGE = slippage_cfg.get('max_entry_rs', 750)    # Warn if above
SKIP_ALERT_SLIPPAGE = slippage_cfg.get('skip_alert_rs', 1000) # Skip alert entirely

# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class TradeSetup:
    symbol: str
    direction: str
    ltp: float
    level_price: float
    level_type: str
    level_score: int
    touches: int
    is_flip: bool
    distance_pct: float
    # Option details
    long_strike: float
    short_strike: float
    long_symbol: str
    short_symbol: str
    long_bid: float
    long_ask: float
    short_bid: float
    short_ask: float
    # Trade metrics
    spread_width: float
    net_debit: float
    max_profit: float
    max_loss: float
    breakeven: float
    risk_reward: float
    total_slippage: float  # Total slippage in Rs
    # Expiry
    expiry: str
    dte: int
    lot_size: int
    # Warnings
    warnings: List[str]


@dataclass
class FuturesSetup:
    """Represents a futures trade setup (for 90+ score with illiquid options)."""
    symbol: str
    direction: str
    ltp: float
    level_price: float
    level_type: str
    level_score: int
    touches: int
    is_flip: bool
    distance_pct: float
    # Futures details
    futures_symbol: str
    entry_price: float
    stop_price: float
    target_price: float
    # Trade metrics
    atr: float
    lot_size: int
    risk_per_lot: float
    reward_per_lot: float
    risk_reward: float
    # Expiry
    expiry: str
    dte: int
    # Reason
    reason: str  # Why futures instead of options


@dataclass
class Position:
    """Represents an open position being tracked."""
    id: str
    symbol: str
    instrument_type: str  # 'OPTIONS' or 'FUTURES'
    direction: str  # 'LONG' or 'SHORT'
    entry_price: float
    entry_date: str
    level_price: float
    stop_price: float
    target_price: float
    lot_size: int
    expiry: str
    score: int
    status: str  # 'open', 'closed'
    # Optional fields for options
    long_symbol: Optional[str] = None
    short_symbol: Optional[str] = None
    futures_symbol: Optional[str] = None
    # Exit info (filled when closed)
    exit_price: Optional[float] = None
    exit_date: Optional[str] = None
    exit_reason: Optional[str] = None


# ============================================================================
# POSITION TRACKING
# ============================================================================

def load_positions() -> List[Position]:
    """Load open positions from file."""
    if not POSITIONS_FILE.exists():
        return []

    try:
        with open(POSITIONS_FILE) as f:
            data = json.load(f)
        return [Position(**p) for p in data.get('positions', [])]
    except Exception as e:
        log.error(f"Error loading positions: {e}")
        return []


def save_positions(positions: List[Position]):
    """Save positions to file atomically to prevent corruption."""
    data = {
        'last_updated': datetime.now().isoformat(),
        'positions': [asdict(p) for p in positions]
    }

    # Atomic write: write to temp file, then rename
    POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Write to temp file in same directory (ensures same filesystem for rename)
        with tempfile.NamedTemporaryFile(
            mode='w',
            dir=POSITIONS_FILE.parent,
            suffix='.tmp',
            delete=False
        ) as f:
            json.dump(data, f, indent=2)
            temp_path = Path(f.name)

        # Atomic rename (on POSIX) / copy+delete (on Windows)
        shutil.move(str(temp_path), str(POSITIONS_FILE))

    except Exception as e:
        log.error(f"Failed to save positions: {e}")
        # Clean up temp file if it exists
        if 'temp_path' in locals() and temp_path.exists():
            temp_path.unlink()
        raise


def add_position(position: Position):
    """Add a new position to tracking."""
    positions = load_positions()
    positions.append(position)
    save_positions(positions)
    log.info(f"Position added: {position.symbol} {position.direction} {position.instrument_type}")


def close_position(position_id: str, exit_price: float, exit_reason: str):
    """Mark a position as closed."""
    positions = load_positions()
    for p in positions:
        if p.id == position_id:
            p.status = 'closed'
            p.exit_price = exit_price
            p.exit_date = datetime.now().strftime('%Y-%m-%d')
            p.exit_reason = exit_reason
            break
    save_positions(positions)


def get_open_positions() -> List[Position]:
    """Get only open positions."""
    return [p for p in load_positions() if p.status == 'open']


# ============================================================================
# KITE CONNECTION
# ============================================================================

def get_kite() -> KiteConnect:
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
# TELEGRAM
# ============================================================================

def send_telegram(message: str) -> bool:
    """Send Telegram message."""
    if not CONFIG.get('telegram', {}).get('enabled', False):
        return False

    try:
        url = f"https://api.telegram.org/bot{CONFIG['telegram']['bot_token']}/sendMessage"
        response = requests.post(url, json={
            'chat_id': CONFIG['telegram']['chat_id'],
            'text': message,
            'parse_mode': 'HTML'
        }, timeout=10)
        return response.status_code == 200
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False


def format_alert(setup: TradeSetup) -> str:
    """Format BULLISH trade setup as compact Telegram alert."""
    flip_tag = " ✨FLIP" if setup.is_flip else ""

    warnings_text = ""
    if setup.warnings:
        warnings_text = "\n⚠️ " + " | ".join(setup.warnings)

    return f"""🟢 <b>BOUNCER</b> | <b>{setup.symbol}</b>{flip_tag}
Score: {setup.level_score} ({setup.touches}T) | {setup.dte} DTE

Support: ₹{setup.level_price:.0f} | LTP: ₹{setup.ltp:.0f}

<b>BUY</b> {setup.long_symbol} @ ₹{setup.long_ask:.1f}
<b>SELL</b> {setup.short_symbol} @ ₹{setup.short_bid:.1f}

Debit: ₹{setup.net_debit * setup.lot_size:,.0f} | R:R 1:{setup.risk_reward:.1f}{warnings_text}"""


def format_futures_alert(setup: FuturesSetup) -> str:
    """Format LONG futures setup as compact Telegram alert."""
    flip_tag = " ✨FLIP" if setup.is_flip else ""

    return f"""🔵 <b>BOUNCER FUT</b> | <b>{setup.symbol}</b>{flip_tag}
Score: {setup.level_score} ({setup.touches}T) | {setup.dte} DTE

<b>BUY {setup.futures_symbol}</b>
Entry: ₹{setup.entry_price:.0f} | SL: ₹{setup.stop_price:.0f} | Target: ₹{setup.target_price:.0f}

Risk: ₹{setup.risk_per_lot:,.0f} | R:R 1:{setup.risk_reward:.1f}"""


def format_sl_alert(position: Position, prev_close: float) -> str:
    """Format stop loss exit alert for LONG positions."""
    if position.instrument_type == 'FUTURES':
        # Futures: P&L based on price difference
        pnl = prev_close - position.entry_price
        pnl_total = pnl * position.lot_size
        pnl_line = f"\nLoss: ~₹{abs(pnl_total):,.0f}"
    else:
        # Options: Can't calculate P&L without fetching spread quotes
        pnl_line = ""

    return f"""🔴 <b>SL HIT</b> | <b>{position.symbol}</b>
Support ₹{position.level_price:.0f} BROKEN

Prev Close: ₹{prev_close:.0f} (below SL ₹{position.stop_price:.0f}){pnl_line}

<b>ACTION: EXIT {position.instrument_type}</b>"""


def format_tp_alert(position: Position, exit_price: float) -> str:
    """Format take profit exit alert for LONG positions."""
    if position.instrument_type == 'FUTURES':
        # Futures: P&L based on price difference
        pnl = exit_price - position.entry_price
        pnl_total = pnl * position.lot_size
        pnl_line = f"\nProfit: ~₹{pnl_total:,.0f}"
    else:
        # Options: Can't calculate P&L without fetching spread quotes
        pnl_line = ""

    return f"""🎯 <b>TARGET HIT</b> | <b>{position.symbol}</b>
Target ₹{position.target_price:.0f} REACHED ✓

LTP: ₹{exit_price:.0f}{pnl_line}

<b>ACTION: BOOK PROFIT on {position.instrument_type}</b>"""


# ============================================================================
# FUTURES HELPERS
# ============================================================================

def get_futures_symbol(symbol: str) -> Tuple[Optional[str], Optional[int], Optional[str], Optional[int]]:
    """
    Get current month futures symbol for a stock.
    Returns: (futures_symbol, lot_size, expiry_str, dte)
    """
    if symbol not in INSTRUMENTS_CACHE:
        return None, None, None, None

    # Look for FUT entries in the data - we need to check instruments CSV for futures
    # For now, use the nearest expiry and construct based on pattern
    # Get unique expiries from options
    expiries = set()
    lot_size = None
    for opt in INSTRUMENTS_CACHE[symbol]:
        expiries.add(opt['expiry'])
        if lot_size is None:
            lot_size = opt['lot_size']

    if not expiries:
        return None, None, None, None

    # Get nearest expiry with DTE >= 10
    today = date.today()
    valid_expiries = []
    for exp in expiries:
        try:
            exp_date = datetime.strptime(exp, '%Y-%m-%d').date()
            dte = (exp_date - today).days
            if dte >= 10:
                valid_expiries.append((exp, dte))
        except (ValueError, TypeError):
            continue

    if not valid_expiries:
        return None, None, None, None

    # Sort by DTE and get nearest
    valid_expiries.sort(key=lambda x: x[1])
    expiry_str, dte = valid_expiries[0]

    # Construct futures symbol (e.g., RELIANCE25JANFUT)
    exp_date = datetime.strptime(expiry_str, '%Y-%m-%d')
    month_abbr = exp_date.strftime('%b').upper()
    year_suffix = exp_date.strftime('%y')
    futures_symbol = f"{symbol}{year_suffix}{month_abbr}FUT"

    return futures_symbol, lot_size, expiry_str, dte


def build_futures_setup(
    kite: KiteConnect,
    symbol: str,
    ltp: float,
    level: Dict,
    lot_size: int,
    atr: float,
    reason: str
) -> Optional[FuturesSetup]:
    """Build a LONG futures trade setup (support bounce only)."""
    level_price = level['price']
    level_type = level['type']

    # BULLISH ONLY: Only process support levels
    if level_type != 'support':
        log.debug(f"{symbol}: SKIP FUTURES - Not a support level (type={level_type})")
        return None

    # Get futures symbol
    futures_symbol, fut_lot_size, expiry_str, dte = get_futures_symbol(symbol)
    if not futures_symbol or not expiry_str or dte is None:
        log.debug(f"{symbol}: Could not find futures symbol or expiry")
        return None

    # Calculate ATR-based target and stop
    atr_multiplier = CONFIG.get('spread_config', {}).get('atr_multiplier', 1.5)
    break_pct = 1.5  # Level break percentage

    # LONG only: Support bounce
    if ltp >= level_price:
        entry = ltp
        target = entry + (atr * atr_multiplier)
        stop = level_price * (1 - break_pct / 100)
    else:
        log.debug(f"{symbol}: SKIP FUTURES - Price below support level")
        return None

    # Calculate risk/reward with zero checks
    risk_per_unit = abs(entry - stop)
    reward_per_unit = abs(target - entry)

    if risk_per_unit == 0:
        log.warning(f"{symbol}: SKIP FUTURES - Zero risk (entry == stop)")
        return None

    risk_per_lot = risk_per_unit * (fut_lot_size or lot_size)
    reward_per_lot = reward_per_unit * (fut_lot_size or lot_size)
    risk_reward = reward_per_unit / risk_per_unit

    distance_pct = abs(ltp - level_price) / level_price * 100

    log.info(f"{symbol}: FUTURES setup - LONG | Entry: {entry:.2f} | SL: {stop:.2f} | Target: {target:.2f}")

    return FuturesSetup(
        symbol=symbol,
        direction='LONG',
        ltp=ltp,
        level_price=level_price,
        level_type=level_type,
        level_score=level['score'],
        touches=level['touches'],
        is_flip=level.get('is_polarity_flip', False),
        distance_pct=round(distance_pct, 2),
        futures_symbol=futures_symbol,
        entry_price=round(entry, 2),
        stop_price=round(stop, 2),
        target_price=round(target, 2),
        atr=atr,
        lot_size=fut_lot_size or lot_size,
        risk_per_lot=round(risk_per_lot, 0),
        reward_per_lot=round(reward_per_lot, 0),
        risk_reward=round(risk_reward, 2),
        expiry=expiry_str,
        dte=dte,
        reason=reason
    )


# ============================================================================
# EXIT SIGNAL CHECKING
# ============================================================================

def get_previous_close(kite: KiteConnect, symbol: str) -> Optional[float]:
    """Fetch previous trading day's close price for a symbol."""
    try:
        # Get instrument token
        instruments = kite.instruments('NSE')
        token = None
        for inst in instruments:
            if inst['tradingsymbol'] == symbol:
                token = inst['instrument_token']
                break

        if not token:
            log.warning(f"{symbol}: Instrument token not found")
            return None

        # Fetch last 5 days of data to ensure we get previous trading day
        to_date = datetime.now()
        from_date = to_date - timedelta(days=5)

        data = kite.historical_data(
            instrument_token=token,
            from_date=from_date,
            to_date=to_date,
            interval='day'
        )

        if len(data) >= 2:
            # Return second-to-last day's close (last is today/incomplete)
            return data[-2]['close']
        elif len(data) == 1:
            return data[0]['close']

        return None

    except Exception as e:
        log.error(f"{symbol}: Failed to fetch historical data: {e}")
        return None


def check_sl_signals(kite: KiteConnect, send_alerts: bool = True) -> List[Position]:
    """
    Check stop loss signals for LONG positions.
    Called at 9 AM to check previous day's close.
    Returns list of positions that hit SL.
    """
    positions = get_open_positions()
    if not positions:
        log.info("No open positions to check for SL")
        return []

    log.info(f"Checking SL for {len(positions)} open positions")

    sl_hit = []

    for pos in positions:
        # Fetch actual previous day close (not current LTP!)
        prev_close = get_previous_close(kite, pos.symbol)

        if prev_close is None:
            log.warning(f"{pos.symbol}: Could not fetch previous close, skipping SL check")
            continue

        # LONG only: SL triggers when price closes below stop (support broken)
        if prev_close < pos.stop_price:
            log.warning(f"{pos.symbol}: SL TRIGGERED - Prev close {prev_close:.2f} vs Stop {pos.stop_price:.2f}")

            if send_alerts:
                msg = format_sl_alert(pos, prev_close)
                if send_telegram(msg):
                    log.info(f"{pos.symbol}: SL alert sent")
                    close_position(pos.id, prev_close, 'STOP_LOSS')
                    sl_hit.append(pos)
        else:
            log.debug(f"{pos.symbol}: SL not triggered - Prev close {prev_close:.2f} vs Stop {pos.stop_price:.2f}")

    return sl_hit


def check_tp_signals(kite: KiteConnect, send_alerts: bool = True) -> List[Position]:
    """
    Check take profit signals for LONG positions.
    Called during market hours - TP can trigger intraday.
    Returns list of positions that hit TP.
    """
    positions = get_open_positions()
    if not positions:
        return []

    # Get symbols to fetch
    symbols = list(set(p.symbol for p in positions))
    nse_symbols = [f"NSE:{s}" for s in symbols]

    try:
        ltps = kite.ltp(nse_symbols)
    except Exception as e:
        log.error(f"Failed to fetch prices for TP check: {e}")
        return []

    tp_hit = []

    for pos in positions:
        nse_sym = f"NSE:{pos.symbol}"
        if nse_sym not in ltps:
            continue

        ltp = ltps[nse_sym]['last_price']

        # LONG only: TP triggers when price reaches target (above)
        if ltp >= pos.target_price:
            log.info(f"{pos.symbol}: TARGET HIT! LTP {ltp:.2f} vs Target {pos.target_price:.2f}")

            if send_alerts:
                msg = format_tp_alert(pos, ltp)
                if send_telegram(msg):
                    log.info(f"{pos.symbol}: TP alert sent")
                    close_position(pos.id, ltp, 'TARGET_HIT')
                    tp_hit.append(pos)

    return tp_hit


# ============================================================================
# OPTION CHAIN HELPERS (using CSV cache - no symbol construction!)
# ============================================================================

def get_expiry_for_stock(symbol: str) -> Tuple[Optional[str], Optional[int]]:
    """
    Get best expiry for a stock based on DTE from CSV cache.
    Returns: (expiry_date_str, dte)
    """
    if symbol not in INSTRUMENTS_CACHE:
        return None, None

    # Get unique expiries with DTE >= MIN_DTE
    expiries = {}
    for opt in INSTRUMENTS_CACHE[symbol]:
        exp = opt['expiry']
        dte = opt['dte']
        if dte >= MIN_DTE and exp not in expiries:
            expiries[exp] = dte

    if not expiries:
        return None, None

    # Return earliest valid expiry
    sorted_exp = sorted(expiries.items(), key=lambda x: x[1])
    return sorted_exp[0][0], sorted_exp[0][1]


def get_strike_interval(symbol: str) -> float:
    """Get strike interval for a stock from CSV cache."""
    if symbol not in INSTRUMENTS_CACHE:
        return 10.0

    strikes = sorted(set(opt['strike'] for opt in INSTRUMENTS_CACHE[symbol]))
    if len(strikes) < 2:
        return 10.0

    intervals = [strikes[i+1] - strikes[i] for i in range(len(strikes)-1)]
    # Return MOST common interval (use max with count, not min)
    return max(set(intervals), key=lambda x: intervals.count(x))


def find_option_symbol(symbol: str, strike: float, expiry: str, opt_type: str) -> Optional[str]:
    """
    Find option symbol from CSV cache. NEVER construct manually!
    Returns the tradingsymbol from CSV.
    """
    if symbol not in INSTRUMENTS_CACHE:
        return None

    for opt in INSTRUMENTS_CACHE[symbol]:
        if (opt['strike'] == strike and
            opt['expiry'] == expiry and
            opt['option_type'] == opt_type):
            return opt['option_symbol']

    return None


def round_strike(price: float, interval: float, direction: str = 'nearest') -> float:
    """Round to nearest available strike."""
    if direction == 'down':
        return float(int(price // interval) * interval)
    elif direction == 'up':
        return float(int(price // interval + 1) * interval)
    return float(round(price / interval) * interval)


def get_option_quotes(kite: KiteConnect, symbol: str, expiry: str,
                      long_strike: float, short_strike: float, opt_type: str) -> Optional[Dict]:
    """
    Fetch option quotes for a spread using symbols from CSV.
    """
    # Look up actual symbols from CSV (not constructed!)
    long_sym_base = find_option_symbol(symbol, long_strike, expiry, opt_type)
    short_sym_base = find_option_symbol(symbol, short_strike, expiry, opt_type)

    if not long_sym_base or not short_sym_base:
        log.debug(f"{symbol}: Option symbols not found in CSV for strikes {long_strike}/{short_strike}")
        return None

    long_sym = f"NFO:{long_sym_base}"
    short_sym = f"NFO:{short_sym_base}"

    try:
        quotes = kite.quote([long_sym, short_sym])

        result = {
            'long_symbol': long_sym_base,
            'short_symbol': short_sym_base,
            'long_bid': 0, 'long_ask': 0,
            'short_bid': 0, 'short_ask': 0,
        }

        if long_sym in quotes:
            q = quotes[long_sym]
            result['long_bid'] = q['depth']['buy'][0]['price'] if q['depth']['buy'] else 0
            result['long_ask'] = q['depth']['sell'][0]['price'] if q['depth']['sell'] else 0

        if short_sym in quotes:
            q = quotes[short_sym]
            result['short_bid'] = q['depth']['buy'][0]['price'] if q['depth']['buy'] else 0
            result['short_ask'] = q['depth']['sell'][0]['price'] if q['depth']['sell'] else 0

        return result

    except Exception as e:
        log.error(f"Error fetching option quotes for {symbol}: {e}")
        return None

# ============================================================================
# TRADE ANALYSIS
# ============================================================================

# Load spread config guardrails
spread_cfg = CONFIG.get('spread_config', {})
ATR_MULTIPLIER = spread_cfg.get('atr_multiplier', 1.5)
FALLBACK_PCT = spread_cfg.get('fallback_pct', 4.0)
# Dynamic spread width: min_width = ATR × multiplier, with absolute floor
MIN_WIDTH_ATR_MULT = spread_cfg.get('min_width_atr_multiplier', 1.25)  # Dynamic: ATR × 1.25
MIN_WIDTH_FLOOR_PCT = spread_cfg.get('min_width_floor_pct', 1.0)      # Absolute floor: 1.0%
MAX_WIDTH_PCT = spread_cfg.get('max_spread_width_pct', 6.0)
MAX_DEBIT_PCT = spread_cfg.get('max_debit_pct_of_width', 50)
MIN_RR = spread_cfg.get('min_risk_reward', 0.8)


def build_trade_setup(kite: KiteConnect, symbol: str, ltp: float,
                      level: Dict, lot_size: int, atr: float, atr_pct: float) -> Optional[TradeSetup]:
    """
    Build BULLISH trade setup with option analysis (Bull Call Spread).
    SUPPORT LEVELS ONLY - Uses ATR-based targeting with guardrails.
    All decisions are logged for analysis.
    Option symbols are read from CSV - NEVER constructed manually!
    """
    level_price = level['price']
    level_type = level['type']

    # BULLISH ONLY: Only process support levels
    if level_type != 'support':
        log.debug(f"{symbol}: SKIP - Not a support level (type={level_type})")
        return None

    log.debug(f"{symbol}: Analyzing SUPPORT @ {level_price} | LTP: {ltp} | ATR: {atr:.1f} ({atr_pct:.1f}%)")

    # Get expiry from CSV cache
    expiry_str, dte = get_expiry_for_stock(symbol)
    if not expiry_str or dte is None:
        log.debug(f"{symbol}: SKIP - No valid expiry found in CSV")
        return None
    if dte < MIN_DTE:
        log.debug(f"{symbol}: SKIP - DTE {dte} < minimum {MIN_DTE}")
        return None

    # Calculate ATR-based target
    interval = get_strike_interval(symbol)

    # ATR-based target: use ATR × multiplier, fallback to fixed %
    if atr > 0:
        target_distance = atr * ATR_MULTIPLIER
        target_pct = (target_distance / ltp) * 100
        log.debug(f"{symbol}: Using ATR target: {atr:.1f} x {ATR_MULTIPLIER} = {target_distance:.1f} ({target_pct:.1f}%)")
    else:
        target_pct = FALLBACK_PCT
        target_distance = ltp * target_pct / 100
        log.debug(f"{symbol}: Using fallback target: {target_pct}%")

    # Allow tolerance for "at level" scenarios - price can be slightly through the level
    # This matches the scanner's ALERT_DISTANCE_PCT logic
    tolerance_pct = ALERT_DISTANCE_PCT / 100  # 0.5% = 0.005

    # For support: price should be at or above support (or slightly below within tolerance)
    min_price = level_price * (1 - tolerance_pct)
    if ltp >= min_price:
        direction = 'BULLISH'
        opt_type = 'CE'
        # Bull Call Spread: Long at/below support, Short at ATR-based target
        long_strike = round_strike(level_price, interval, 'down')
        target = ltp + target_distance
        short_strike = round_strike(target, interval, 'down')
    else:
        log.debug(f"{symbol}: SKIP - Price too far below support (LTP {ltp} < {min_price:.2f})")
        return None

    # Calculate spread width and check limits
    spread_width = abs(long_strike - short_strike)

    # Zero width check (can happen if strikes are same)
    if spread_width == 0:
        log.info(f"{symbol}: SKIP - Zero spread width (long={long_strike}, short={short_strike})")
        return None

    spread_width_pct = (spread_width / ltp) * 100

    # GUARDRAIL 1: Dynamic spread width limits based on ATR
    # min_width = max(ATR × multiplier, absolute_floor)
    # This ensures low-ATR stocks get proportionally lower thresholds
    dynamic_min_width_pct = max(atr_pct * MIN_WIDTH_ATR_MULT, MIN_WIDTH_FLOOR_PCT)

    log.debug(f"{symbol}: Spread {long_strike}/{short_strike} = {spread_width} pts ({spread_width_pct:.1f}%) | Min: {dynamic_min_width_pct:.1f}% (ATR {atr_pct:.1f}% × {MIN_WIDTH_ATR_MULT})")

    if spread_width_pct < dynamic_min_width_pct:
        log.info(f"{symbol}: SKIP - Spread too narrow: {spread_width_pct:.1f}% < {dynamic_min_width_pct:.1f}% (dynamic min for ATR {atr_pct:.1f}%)")
        return None
    if spread_width_pct > MAX_WIDTH_PCT:
        log.info(f"{symbol}: SKIP - Spread too wide: {spread_width_pct:.1f}% > {MAX_WIDTH_PCT}%")
        return None

    # Get option quotes (symbols from CSV, not constructed!)
    quotes = get_option_quotes(kite, symbol, expiry_str, long_strike, short_strike, opt_type)
    if not quotes:
        log.debug(f"{symbol}: SKIP - Failed to fetch option quotes")
        return None
    if quotes['long_ask'] == 0 or quotes['short_bid'] == 0:
        log.info(f"{symbol}: SKIP - No valid quotes (long_ask={quotes['long_ask']}, short_bid={quotes['short_bid']})")
        return None

    log.debug(f"{symbol}: Quotes - Long: {quotes['long_bid']}/{quotes['long_ask']} | Short: {quotes['short_bid']}/{quotes['short_ask']}")

    # Check slippage cost (spread × lot_size)
    warnings = []
    long_spread = quotes['long_ask'] - quotes['long_bid']
    short_spread = quotes['short_ask'] - quotes['short_bid']

    # Total slippage = both legs combined
    long_slippage = long_spread * lot_size
    short_slippage = short_spread * lot_size
    total_slippage = long_slippage + short_slippage

    log.debug(f"{symbol}: Slippage - Long: {long_spread:.2f}x{lot_size}={long_slippage:.0f} | Short: {short_spread:.2f}x{lot_size}={short_slippage:.0f} | Total: Rs {total_slippage:.0f}")

    if total_slippage > SKIP_ALERT_SLIPPAGE:
        log.info(f"{symbol}: SKIP - Slippage Rs {total_slippage:.0f} > max Rs {SKIP_ALERT_SLIPPAGE}")
        return None

    if total_slippage > MAX_ENTRY_SLIPPAGE:
        warnings.append(f"High slippage: Rs {total_slippage:,.0f}")
        log.warning(f"{symbol}: WARN - High slippage Rs {total_slippage:.0f}")

    # Calculate trade metrics
    net_debit = quotes['long_ask'] - quotes['short_bid']  # Buy at ask, sell at bid
    max_profit = spread_width - net_debit
    max_loss = net_debit

    # Zero/negative debit check
    if max_loss <= 0:
        log.info(f"{symbol}: SKIP - Invalid debit: {net_debit:.2f} (long_ask={quotes['long_ask']}, short_bid={quotes['short_bid']})")
        return None

    # GUARDRAIL 2: Max debit as % of spread width (spread_width already validated > 0)
    debit_pct = (net_debit / spread_width) * 100
    if debit_pct > MAX_DEBIT_PCT:
        log.info(f"{symbol}: SKIP - Debit {debit_pct:.0f}% > max {MAX_DEBIT_PCT}% of width (paying Rs {net_debit:.2f} for {spread_width} spread)")
        return None

    # GUARDRAIL 3: Minimum risk:reward (max_loss already validated > 0)
    risk_reward = max_profit / max_loss
    if risk_reward < MIN_RR:
        log.info(f"{symbol}: SKIP - R:R {risk_reward:.2f} < min {MIN_RR} (profit {max_profit:.2f} vs loss {max_loss:.2f})")
        return None

    log.debug(f"{symbol}: Metrics - Debit: {net_debit:.2f} ({debit_pct:.0f}%) | R:R: {risk_reward:.2f} | Max Profit: {max_profit:.2f}")

    # Bull Call Spread breakeven
    breakeven = long_strike + net_debit

    # Distance from level
    distance_pct = abs(ltp - level_price) / level_price * 100

    # Add info warnings
    if debit_pct > 40:
        warnings.append(f"Debit {debit_pct:.0f}% of width")

    # Log successful setup
    log.info(f"{symbol}: SETUP READY - {direction} {long_strike}/{short_strike} | Debit: {net_debit:.2f} | R:R: {risk_reward:.2f} | Slippage: Rs {total_slippage:.0f}")

    return TradeSetup(
        symbol=symbol,
        direction=direction,
        ltp=ltp,
        level_price=level_price,
        level_type=level_type,
        level_score=level['score'],
        touches=level['touches'],
        is_flip=level.get('is_polarity_flip', False),
        distance_pct=round(distance_pct, 2),
        long_strike=long_strike,
        short_strike=short_strike,
        long_symbol=quotes['long_symbol'],
        short_symbol=quotes['short_symbol'],
        long_bid=quotes['long_bid'],
        long_ask=quotes['long_ask'],
        short_bid=quotes['short_bid'],
        short_ask=quotes['short_ask'],
        spread_width=spread_width,
        net_debit=round(net_debit, 2),
        max_profit=round(max_profit, 2),
        max_loss=round(max_loss, 2),
        breakeven=round(breakeven, 2),
        risk_reward=round(risk_reward, 2),
        total_slippage=round(total_slippage, 0),
        expiry=expiry_str,
        dte=dte,
        lot_size=lot_size,
        warnings=warnings
    )

# ============================================================================
# MARKET HOURS CHECK
# ============================================================================

def is_market_hours() -> bool:
    """Check if within market hours."""
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)

    if now.weekday() >= 5:
        return False

    start = now.replace(hour=9, minute=15, second=0)
    end = now.replace(hour=15, minute=30, second=0)

    return start <= now <= end

# ============================================================================
# MAIN SCANNER
# ============================================================================

def run_scan(send_alerts: bool = True) -> List[TradeSetup]:
    """Run single scan iteration."""
    log.info("=" * 50)
    log.info("SCAN STARTED")

    # Connect to Kite first (needed for TP check)
    kite = get_kite()

    # Check for TP signals on open positions (can trigger intraday)
    tp_positions = check_tp_signals(kite, send_alerts)
    if tp_positions:
        log.info(f"TP triggered for {len(tp_positions)} positions")

    # Load reliability data for filtering poor performers
    load_reliability_data()
    if RELIABILITY_DATA:
        log.debug(f"Loaded reliability data for {len(RELIABILITY_DATA)} stocks")

    # Load levels
    if not LEVELS_FILE.exists():
        log.error(f"Levels file not found: {LEVELS_FILE}")
        return []

    with open(LEVELS_FILE) as f:
        data = json.load(f)

    if not data.get('stocks'):
        log.warning("No stocks in levels.json")
        return []

    stock_count = len(data['stocks'])
    level_count = sum(len(s['levels']) for s in data['stocks'].values())
    log.info(f"Loaded {stock_count} stocks with {level_count} levels")

    # Fetch LTP for all stocks (single batch call) with retry
    symbols = [f"NSE:{s}" for s in data['stocks'].keys()]
    ltps: Dict = {}
    max_retries = 3
    for attempt in range(max_retries):
        try:
            ltps = kite.ltp(symbols)
            log.debug(f"Fetched LTP for {len(ltps)} symbols")
            break
        except Exception as e:
            log.warning(f"LTP fetch attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s
            else:
                log.error(f"Failed to fetch LTPs after {max_retries} attempts")
                return []

    if not ltps:
        log.error("No LTP data retrieved")
        return []

    setups_found = []
    alert_score = CONFIG.get('scoring', {}).get('alert_score', 70)
    levels_checked = 0
    levels_at_price = 0
    levels_invalidated = 0
    stocks_skipped_reliability = 0

    for symbol, stock_data in data['stocks'].items():
        nse_sym = f"NSE:{symbol}"
        if nse_sym not in ltps:
            log.debug(f"{symbol}: No LTP data")
            continue

        # Check reliability - skip stocks that historically don't respect S/R levels
        if symbol in RELIABILITY_DATA:
            reliability = RELIABILITY_DATA[symbol]
            success_rate = reliability.get('success_rate', 0.5)
            if success_rate < MIN_RELIABILITY_RATE:
                log.debug(f"{symbol}: SKIP - Poor S/R reliability ({success_rate*100:.1f}% < {MIN_RELIABILITY_RATE*100:.0f}%)")
                stocks_skipped_reliability += 1
                continue

        ltp = ltps[nse_sym]['last_price']
        lot_size = stock_data.get('lot_size', 1)
        atr = stock_data.get('atr', 0)
        atr_pct = stock_data.get('atr_pct', 0)

        for level in stock_data['levels']:
            # Skip inactive or already alerted
            if level.get('status') != 'active':
                continue
            if level.get('alerted', False):
                continue

            levels_checked += 1
            level_price = level['price']
            level_type = level['type']

            # Calculate distance
            distance_pct = abs(ltp - level_price) / level_price * 100

            # BULLISH ONLY: Only process support levels
            if level_type != 'support':
                continue

            # Check if level is invalidated (broken through)
            if ltp < level_price * (1 - INVALIDATE_PCT / 100):
                level['status'] = 'invalidated'
                levels_invalidated += 1
                log.warning(f"{symbol}: INVALIDATED support {level_price} - LTP {ltp} broke through by {distance_pct:.1f}%")
                continue

            # Check if at level (within alert distance)
            if distance_pct <= ALERT_DISTANCE_PCT:
                levels_at_price += 1

                # Only alert for high-score levels
                if level['score'] < alert_score:
                    log.debug(f"{symbol}: At level {level_price} but score {level['score']} < {alert_score}")
                    continue

                log.info(f"{symbol}: AT LEVEL {level_type} @ {level_price} | LTP: {ltp} | Score: {level['score']} | ATR: {atr_pct:.1f}%")

                # Build full trade setup with ATR-based targeting
                setup = build_trade_setup(kite, symbol, ltp, level, lot_size, atr, atr_pct)

                if setup:
                    setups_found.append(setup)

                    # Send alert
                    if send_alerts:
                        msg = format_alert(setup)
                        if send_telegram(msg):
                            log.info(f"{symbol}: ALERT SENT - {setup.direction} {setup.long_strike}/{setup.short_strike}")
                            level['alerted'] = True

                            # Track LONG position (BULLISH only)
                            pos = Position(
                                id=f"{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                                symbol=symbol,
                                instrument_type='OPTIONS',
                                direction='LONG',
                                entry_price=setup.net_debit,
                                entry_date=datetime.now().strftime('%Y-%m-%d'),
                                level_price=level_price,
                                stop_price=level_price * 0.985,  # Support break
                                target_price=setup.breakeven + setup.max_profit,
                                lot_size=lot_size,
                                expiry=setup.expiry,
                                score=level['score'],
                                status='open',
                                long_symbol=setup.long_symbol,
                                short_symbol=setup.short_symbol
                            )
                            add_position(pos)
                        else:
                            log.error(f"{symbol}: Alert failed to send")
                    else:
                        log.info(f"{symbol}: Alert suppressed (test mode)")

                # FUTURES FALLBACK: If options setup failed (likely due to slippage) and score >= 90
                elif level['score'] >= FUTURES_SCORE_THRESHOLD and FUTURES_ENABLED:
                    log.info(f"{symbol}: Score {level['score']} >= {FUTURES_SCORE_THRESHOLD}, trying FUTURES fallback")

                    futures_setup = build_futures_setup(
                        kite, symbol, ltp, level, lot_size, atr,
                        reason="Options illiquid (high slippage) - exceptional setup warrants futures"
                    )

                    if futures_setup:
                        if send_alerts:
                            msg = format_futures_alert(futures_setup)
                            if send_telegram(msg):
                                log.info(f"{symbol}: FUTURES ALERT SENT - {futures_setup.direction} {futures_setup.futures_symbol}")
                                level['alerted'] = True

                                # Track futures position
                                pos = Position(
                                    id=f"{symbol}_FUT_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                                    symbol=symbol,
                                    instrument_type='FUTURES',
                                    direction=futures_setup.direction,
                                    entry_price=futures_setup.entry_price,
                                    entry_date=datetime.now().strftime('%Y-%m-%d'),
                                    level_price=level_price,
                                    stop_price=futures_setup.stop_price,
                                    target_price=futures_setup.target_price,
                                    lot_size=futures_setup.lot_size,
                                    expiry=futures_setup.expiry,
                                    score=level['score'],
                                    status='open',
                                    futures_symbol=futures_setup.futures_symbol
                                )
                                add_position(pos)
                            else:
                                log.error(f"{symbol}: Futures alert failed to send")
                        else:
                            log.info(f"{symbol}: Futures alert suppressed (test mode)")

    # Save updated levels
    with open(LEVELS_FILE, 'w') as f:
        json.dump(data, f, indent=2)

    # Summary
    log.info("-" * 50)
    if stocks_skipped_reliability > 0:
        log.info(f"Skipped {stocks_skipped_reliability} stocks due to poor S/R reliability (<{MIN_RELIABILITY_RATE*100:.0f}%)")
    log.info(f"SCAN COMPLETE | Checked: {levels_checked} | At level: {levels_at_price} | Invalidated: {levels_invalidated} | Setups: {len(setups_found)}")

    return setups_found


def run_loop(interval_mins: int = 5):
    """Run scanner in continuous loop."""
    log.info("=" * 60)
    log.info("BOUNCER SCANNER - LOOP MODE")
    log.info(f"Interval: {interval_mins} minutes")
    log.info("=" * 60)

    while True:
        try:
            if is_market_hours():
                setups = run_scan(send_alerts=True)
            else:
                log.info("Outside market hours - sleeping")

            time.sleep(interval_mins * 60)

        except KeyboardInterrupt:
            log.info("Stopping scanner (Ctrl+C)")
            break
        except Exception as e:
            log.exception(f"Scan error: {e}")
            time.sleep(60)


def main():
    log.info("=" * 60)
    log.info("BOUNCER MARKET SCANNER")
    today = datetime.now().strftime('%Y-%m-%d')
    log_filename = f"scanner_{datetime.now().strftime('%Y%m%d')}.log"
    log.info(f"Date: {today}")
    log.info(f"Log file: {LOGS_DIR / log_filename}")
    log.info("=" * 60)

    # Morning SL check mode (run at 9 AM)
    if '--check-sl' in sys.argv:
        log.info("SL CHECK MODE - Checking previous day close for stop losses")
        kite = get_kite()
        send_alerts = '--test' not in sys.argv

        sl_positions = check_sl_signals(kite, send_alerts)
        if sl_positions:
            log.info(f"SL triggered for {len(sl_positions)} positions:")
            for p in sl_positions:
                log.info(f"  {p.symbol}: {p.direction} {p.instrument_type}")
        else:
            log.info("No stop losses triggered")
        return 0

    # Loop mode
    if '--loop' in sys.argv:
        run_loop()
        return

    # Single scan mode
    send_alerts = '--test' not in sys.argv
    if not send_alerts:
        log.info("TEST MODE - Alerts suppressed")

    setups = run_scan(send_alerts=send_alerts)

    if setups:
        log.info(f"Summary: {len(setups)} setups found")
        for s in setups:
            log.info(f"  {s.symbol}: {s.direction} {s.long_strike}/{s.short_strike} | R:R {s.risk_reward}")
    else:
        log.info("No stocks currently at key levels")

    # Show open positions summary
    open_positions = get_open_positions()
    if open_positions:
        log.info(f"\nOpen positions: {len(open_positions)}")
        for p in open_positions:
            log.info(f"  {p.symbol}: {p.direction} {p.instrument_type} | Entry: {p.entry_price:.2f} | Target: {p.target_price:.2f} | Stop: {p.stop_price:.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
