#!/usr/bin/env python3
"""
BASIS Z-SCORE LIVE TRADING BOT
==============================
Real-time trading based on spot-futures basis z-score signals.

Features:
- Paper trade mode for testing
- SQLite DB for position/order tracking (multi-bot safe)
- Rolling futures (current/next month) auto-detection
- Telegram alerts on all events
- WebSocket for real-time data
- All paths configurable via config.json
- Lot size from instruments file
- Daily summary at EOD
- Order verification with Kite API
- Holiday calendar support

Version: 3.0
"""

import os
import sys
import json
import time
import logging
import requests
import csv
import statistics
import uuid
import signal
import concurrent.futures
from datetime import datetime, timedelta, date
from collections import deque
from typing import Dict, Optional, Tuple
from dataclasses import dataclass

# Kite Connect
from kiteconnect import KiteConnect, KiteTicker

# Local imports
from db import TradingDB, Order, Position as DBPosition, DailySummary

# Estimated charges per lot (Rs) - brokerage + STT + GST + exchange
CHARGES_PER_LOT = 62

# BOTS folder is the parent of ZSCORE folder
BOTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_path(relative_path: str) -> str:
    """Resolve a path relative to BOTS folder.

    If path is already absolute, return as-is.
    Otherwise, resolve relative to BOTS_DIR.
    """
    if os.path.isabs(relative_path):
        return relative_path
    return os.path.join(BOTS_DIR, relative_path)


def load_holidays(config: dict) -> set:
    """Load holidays from JSON file"""
    holidays = set()

    # Try to load from config market.holidays_path or fallback to data_dir
    if config.get('market', {}).get('holidays_path'):
        holiday_file = resolve_path(config['market']['holidays_path'])
    else:
        data_dir = resolve_path(config.get('data_dir', 'data'))
        holiday_file = os.path.join(data_dir, 'holiday_calendar.json')

    if os.path.exists(holiday_file):
        try:
            with open(holiday_file, 'r') as f:
                data = json.load(f)
                # Support both list format and dict format
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and 'date' in item:
                            holidays.add(item['date'])
                        elif isinstance(item, str):
                            holidays.add(item)
                elif isinstance(data, dict):
                    # Format: {"2025": ["2025-01-26", ...]} or {"2025": [{"date": "...", "name": "..."}]}
                    for key, year_holidays in data.items():
                        if key.startswith('_'):  # Skip metadata keys like _comment
                            continue
                        if isinstance(year_holidays, list):
                            for item in year_holidays:
                                if isinstance(item, dict) and 'date' in item:
                                    holidays.add(item['date'])
                                elif isinstance(item, str):
                                    holidays.add(item)
            logging.info(f"Loaded {len(holidays)} holidays from {holiday_file}")
        except Exception as e:
            logging.warning(f"Could not load holidays: {e}")

    return holidays


def is_trading_day(holidays: set = None) -> bool:
    """Check if today is a trading day (not weekend, not holiday)"""
    today = date.today()

    # Weekend check
    if today.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False

    # Holiday check
    if holidays and today.isoformat() in holidays:
        return False

    return True


def get_next_trading_day(holidays: set = None) -> date:
    """Get next trading day with iteration limit to prevent infinite loop"""
    check_date = date.today() + timedelta(days=1)
    holidays = holidays or set()
    max_iterations = 365  # Safety limit - shouldn't need more than a year
    for _ in range(max_iterations):
        if check_date.weekday() < 5 and check_date.isoformat() not in holidays:
            return check_date
        check_date += timedelta(days=1)
    # Fallback: return next weekday if loop exhausted (should never happen)
    logging.warning("get_next_trading_day exceeded max iterations, returning next weekday")
    return check_date


# =============================================================================
# BSE INSTRUMENTS DOWNLOAD
# =============================================================================

# Retry configuration for instruments download
INSTRUMENTS_MAX_RETRIES = 3
INSTRUMENTS_RETRY_BACKOFF = [2, 5, 10]  # Exponential backoff in seconds
INSTRUMENTS_MAX_AGE_HOURS = 24


def get_instruments_age(instruments_path: str) -> Optional[float]:
    """Get age of instruments file in hours."""
    if not os.path.exists(instruments_path):
        return None
    mtime = os.path.getmtime(instruments_path)
    age_seconds = time.time() - mtime
    return age_seconds / 3600


def refresh_bse_instruments(
    instruments_path: str,
    max_retries: int = INSTRUMENTS_MAX_RETRIES
) -> Tuple[bool, str]:
    """
    Download and cache BSE (BFO) instruments filtered to SENSEX only.
    Uses Zerodha's public API - no authentication required.

    Implements:
    - 3 retries with exponential backoff (2s, 5s, 10s)
    - Falls back to cache if less than 24 hours old

    Args:
        instruments_path: Path to save instruments CSV
        max_retries: Maximum retry attempts

    Returns:
        Tuple of (success, message)
    """
    last_error = None
    BFO_URL = "https://api.kite.trade/instruments/BFO"
    BSE_URL = "https://api.kite.trade/instruments/BSE"

    for attempt in range(max_retries):
        try:
            logging.info(f"Downloading BSE instruments (attempt {attempt + 1}/{max_retries})...")

            # Fetch BFO (BSE F&O) instruments - public API, no auth needed
            bfo_response = requests.get(BFO_URL, timeout=30)
            bfo_response.raise_for_status()

            # Parse CSV response using csv module for proper handling of quoted fields
            import io
            bfo_reader = csv.DictReader(io.StringIO(bfo_response.text))
            bfo_instruments = list(bfo_reader)

            if not bfo_instruments:
                raise ValueError("Empty BFO instruments response")

            # Also fetch BSE for spot index token
            bse_response = requests.get(BSE_URL, timeout=30)
            bse_response.raise_for_status()

            bse_reader = csv.DictReader(io.StringIO(bse_response.text))
            bse_instruments = list(bse_reader)

            # Filter to SENSEX futures and options
            sensex_count = 0
            today = datetime.now().strftime("%Y-%m-%d")

            # Ensure directory exists
            os.makedirs(os.path.dirname(instruments_path) if os.path.dirname(instruments_path) else '.', exist_ok=True)

            with open(instruments_path, 'w', newline='') as f:
                fieldnames = ['fetch_date', 'instrument_token', 'tradingsymbol', 'name',
                              'exchange', 'instrument_type', 'expiry', 'strike', 'lot_size']
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()

                # Write SENSEX futures and options from BFO
                for inst in bfo_instruments:
                    if inst.get('name') == 'SENSEX':
                        writer.writerow({
                            'fetch_date': today,
                            'instrument_token': inst.get('instrument_token', ''),
                            'tradingsymbol': inst.get('tradingsymbol', ''),
                            'name': inst.get('name', ''),
                            'exchange': 'BFO',
                            'instrument_type': inst.get('instrument_type', ''),
                            'expiry': inst.get('expiry', ''),
                            'strike': inst.get('strike', ''),
                            'lot_size': inst.get('lot_size', '')
                        })
                        sensex_count += 1

                # Write SENSEX spot index from BSE (for spot price)
                for inst in bse_instruments:
                    if inst.get('tradingsymbol') == 'SENSEX':
                        writer.writerow({
                            'fetch_date': today,
                            'instrument_token': inst.get('instrument_token', ''),
                            'tradingsymbol': inst.get('tradingsymbol', ''),
                            'name': inst.get('name', ''),
                            'exchange': 'BSE',
                            'instrument_type': 'INDEX',
                            'expiry': '',
                            'strike': '',
                            'lot_size': ''
                        })
                        sensex_count += 1

            if sensex_count == 0:
                raise ValueError("No SENSEX instruments found in BFO response")

            logging.info(f"BSE instruments saved: {sensex_count} SENSEX instruments to {instruments_path}")
            return True, f"BSE instruments refreshed: {sensex_count} records"

        except Exception as e:
            last_error = str(e)
            logging.warning(f"BSE instruments refresh attempt {attempt + 1} failed: {e}")

            if attempt < max_retries - 1:
                backoff = INSTRUMENTS_RETRY_BACKOFF[min(attempt, len(INSTRUMENTS_RETRY_BACKOFF) - 1)]
                logging.info(f"Retrying in {backoff}s...")
                time.sleep(backoff)

    # All retries failed - check cache
    logging.error(f"All {max_retries} BSE instrument refresh attempts failed")

    cache_age = get_instruments_age(instruments_path)
    if cache_age is not None and cache_age < INSTRUMENTS_MAX_AGE_HOURS:
        logging.warning(f"Using cached BSE instruments ({cache_age:.1f} hours old)")
        return True, f"Using cached BSE instruments ({cache_age:.1f}h old). Refresh failed: {last_error}"

    # No valid cache
    if cache_age is not None:
        return False, f"BSE instruments cache too old ({cache_age:.1f}h). Refresh failed: {last_error}"
    else:
        return False, f"No BSE instruments cache available. Refresh failed: {last_error}"


# =============================================================================
# CONFIGURATION
# =============================================================================

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
VERSION = "3.0.0"

# =============================================================================
# DATA CLASSES (for internal use with SignalEngine)
# =============================================================================

@dataclass
class Position:
    """Internal position representation for signal engine"""
    active: bool = False
    symbol: str = ""
    instrument_token: int = 0
    qty: int = 0
    entry_price: float = 0.0
    entry_time: str = ""
    entry_spot: float = 0.0
    entry_z_score: float = 0.0
    entry_basis: float = 0.0
    fut_used: str = ""  # "CURRENT" or "NEXT"
    stop_loss: float = 0.0
    target: float = 0.0
    exit_deadline: str = ""

# =============================================================================
# INSTRUMENT MANAGER
# =============================================================================

class InstrumentManager:
    """Manage instrument lookups from multiple cached CSV files (NSE + BSE)"""

    def __init__(self, kite: KiteConnect, data_dir: str,
                 nse_instruments_path: str = None,
                 bse_instruments_path: str = None):
        """
        Initialize InstrumentManager with support for both NSE and BSE instruments.

        Args:
            kite: KiteConnect instance
            data_dir: Directory for caching instruments
            nse_instruments_path: Path to NSE/NFO instruments CSV (NIFTY)
            bse_instruments_path: Path to BSE/BFO instruments CSV (SENSEX)
        """
        self.kite = kite
        self.data_dir = data_dir
        self.nse_instruments_path = nse_instruments_path
        self.bse_instruments_path = bse_instruments_path
        self.instruments = {}  # symbol -> {token, exchange, expiry, strike, etc.}
        self._load_instruments()

    def _load_instruments(self):
        """Load instruments from both NSE and BSE CSV files"""
        self.instruments = {}

        # Load NSE instruments (NIFTY)
        if self.nse_instruments_path and os.path.exists(self.nse_instruments_path):
            logging.info(f"Loading NSE instruments from: {self.nse_instruments_path}")
            self._load_from_csv(self.nse_instruments_path)
        else:
            logging.warning(f"NSE instruments file not found: {self.nse_instruments_path}")

        # Load BSE instruments (SENSEX)
        if self.bse_instruments_path and os.path.exists(self.bse_instruments_path):
            logging.info(f"Loading BSE instruments from: {self.bse_instruments_path}")
            self._load_from_csv(self.bse_instruments_path)
        else:
            logging.warning(f"BSE instruments file not found: {self.bse_instruments_path}")

        logging.info(f"Total instruments loaded: {len(self.instruments)}")

    def _load_from_csv(self, csv_path: str):
        """Load instruments from a CSV file into memory (appends to existing)"""
        skipped = 0
        loaded = 0
        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    symbol = row['tradingsymbol']
                    token_str = row.get('instrument_token', '')
                    if not token_str:
                        skipped += 1
                        continue
                    self.instruments[symbol] = {
                        'token': int(token_str),
                        'exchange': row.get('exchange', 'NFO'),
                        'type': row.get('instrument_type', ''),
                        'expiry': row.get('expiry', ''),
                        'strike': row.get('strike', ''),
                        'lot_size': row.get('lot_size', ''),
                        'name': row.get('name', '')
                    }
                    loaded += 1
                except (ValueError, KeyError):
                    skipped += 1
                    continue
        if skipped > 0:
            logging.warning(f"Skipped {skipped} invalid rows from {csv_path}")
        logging.info(f"Loaded {loaded} instruments from {csv_path}")

    def get_token(self, symbol: str) -> Optional[int]:
        """Get instrument token by symbol"""
        inst = self.instruments.get(symbol)
        if inst:
            return inst['token']

        # Try case-insensitive search
        for sym, data in self.instruments.items():
            if sym.upper() == symbol.upper():
                return data['token']

        logging.warning(f"Symbol not found: {symbol}")
        return None

    def get_instrument(self, symbol: str) -> Optional[Dict]:
        """Get full instrument data by symbol"""
        return self.instruments.get(symbol)

    def get_spot_token(self, symbol: str) -> Optional[int]:
        """Get spot/index token"""
        return self.get_token(symbol)

    def get_futures_token(self, symbol: str) -> Optional[int]:
        """Get futures token"""
        return self.get_token(symbol)

    def find_futures(self, underlying: str, default_lot_size: int = 75) -> Tuple[Optional[Dict], Optional[Dict]]:
        """
        Auto-detect current and next month futures for any underlying.

        Args:
            underlying: Underlying symbol (e.g., "NIFTY", "SENSEX")
            default_lot_size: Default lot size if not found in instruments

        Returns:
            (current_month_fut, next_month_fut) dicts with symbol, token, expiry, lot_size, exchange.
        """
        today = datetime.now().date()
        futures = []

        for symbol, data in self.instruments.items():
            # Match futures for the underlying (e.g., NIFTY25DECFUT, SENSEX25DECFUT)
            if not symbol.startswith(underlying):
                continue
            if not symbol.endswith('FUT'):
                continue
            if data['type'] != 'FUT':
                continue

            try:
                exp_str = data['expiry']
                if exp_str:
                    exp_date = datetime.strptime(exp_str[:10], '%Y-%m-%d').date()
                    lot_size = int(data['lot_size']) if data['lot_size'] else default_lot_size
                    # Only consider futures that haven't expired
                    if exp_date >= today:
                        futures.append({
                            'symbol': symbol,
                            'token': data['token'],
                            'expiry': exp_date,
                            'lot_size': lot_size,
                            'exchange': data['exchange']
                        })
            except Exception:
                continue

        # Sort by expiry date
        futures.sort(key=lambda x: x['expiry'])

        if len(futures) >= 2:
            current = futures[0]
            next_month = futures[1]
            logging.info(f"Auto-detected {underlying} futures - Current: {current['symbol']} "
                        f"(exp: {current['expiry']}, lot: {current['lot_size']}, exchange: {current['exchange']}), "
                        f"Next: {next_month['symbol']} (exp: {next_month['expiry']})")
            return current, next_month
        elif len(futures) == 1:
            logging.warning(f"Only one {underlying} future found: {futures[0]['symbol']}")
            return futures[0], None
        else:
            logging.error(f"No {underlying} futures found in instruments!")
            return None, None

    # Backward compatibility alias
    def find_nifty_futures(self, underlying: str = "NIFTY") -> Tuple[Optional[Dict], Optional[Dict]]:
        """Backward compatible method - calls find_futures"""
        return self.find_futures(underlying)

    def find_atm_option(self, spot_price: float, option_type: str = "CE",
                        min_dte: int = 3, underlying: str = "NIFTY",
                        strike_interval: int = 50,
                        default_lot_size: int = 75) -> Optional[Dict]:
        """
        Find ATM option for weekly expiry with minimum DTE.

        Args:
            spot_price: Current spot price
            option_type: "CE" or "PE"
            min_dte: Minimum days to expiry
            underlying: Underlying symbol (e.g., "NIFTY", "SENSEX")
            strike_interval: Strike price interval (50 for NIFTY, 100 for SENSEX)
            default_lot_size: Default lot size if not found

        Returns:
            Dict with symbol, token, strike, expiry, dte, lot_size, exchange
        """
        atm_strike = round(spot_price / strike_interval) * strike_interval
        now = datetime.now()
        today = now.date()

        # Determine expiry day based on underlying
        # NIFTY: Thursday (3), SENSEX: Friday (4) typically
        # But always use instruments file as source of truth
        expiry_day = 3 if underlying == "NIFTY" else 4  # Thursday for NIFTY, Friday for SENSEX

        # Find next expiry day
        days_until_expiry = (expiry_day - now.weekday()) % 7
        if days_until_expiry == 0 and now.hour >= 15:
            days_until_expiry = 7
        ideal_expiry = (now + timedelta(days=days_until_expiry)).date()

        # Check DTE - if too close to expiry, use next week
        dte = (ideal_expiry - today).days
        if dte < min_dte:
            ideal_expiry = ideal_expiry + timedelta(days=7)
            logging.info(f"Current expiry DTE={dte} < {min_dte}, ideal next week: {ideal_expiry}")

        # First, collect all available expiries from instruments for this underlying
        available_expiries = set()
        for symbol, data in self.instruments.items():
            if not symbol.startswith(underlying):
                continue
            if data['type'] not in ['CE', 'PE']:
                continue
            exp_str = data.get('expiry', '')
            if exp_str:
                try:
                    exp_date = datetime.strptime(exp_str[:10], '%Y-%m-%d').date()
                    if exp_date >= today:
                        available_expiries.add(exp_date)
                except Exception:
                    continue

        if not available_expiries:
            logging.error(f"No valid expiries found for {underlying} in instruments file!")
            return None

        # Sort available expiries
        sorted_expiries = sorted(available_expiries)
        logging.debug(f"Available {underlying} expiries: {sorted_expiries}")

        # Find the best expiry to use
        expiry_date = None
        if ideal_expiry in available_expiries:
            expiry_date = ideal_expiry
        else:
            # Ideal expiry not available - find the nearest one >= today
            # Prefer expiries that meet min_dte requirement
            for exp in sorted_expiries:
                exp_dte = (exp - today).days
                if exp_dte >= min_dte:
                    expiry_date = exp
                    logging.info(f"Ideal expiry {ideal_expiry} not in {underlying} instruments, using available: {expiry_date}")
                    break

            # If no expiry meets min_dte, use the nearest available
            if not expiry_date and sorted_expiries:
                expiry_date = sorted_expiries[0]
                logging.warning(f"No {underlying} expiry meets min_dte={min_dte}, using nearest: {expiry_date}")

        if not expiry_date:
            logging.error(f"Could not determine expiry date for {underlying}")
            return None

        # Search for matching ATM option
        candidates = []
        for symbol, data in self.instruments.items():
            if not symbol.startswith(underlying):
                continue
            if not symbol.endswith(option_type):
                continue
            if data['type'] not in ['CE', 'PE']:
                continue

            try:
                exp_str = data['expiry']
                if exp_str:
                    exp_date = datetime.strptime(exp_str[:10], '%Y-%m-%d').date()
                    strike = float(data['strike']) if data['strike'] else 0

                    if exp_date == expiry_date and strike == atm_strike:
                        option_dte = (exp_date - today).days
                        lot_size = int(data['lot_size']) if data['lot_size'] else default_lot_size
                        candidates.append({
                            'symbol': symbol,
                            'token': data['token'],
                            'strike': strike,
                            'expiry': exp_date,
                            'dte': option_dte,
                            'lot_size': lot_size,
                            'exchange': data['exchange']
                        })
            except Exception:
                continue

        if candidates:
            result = candidates[0]
            logging.info(f"Found {underlying} ATM {option_type}: {result['symbol']} "
                        f"(strike={result['strike']}, expiry={result['expiry']}, DTE={result['dte']})")
            return result

        # Log available options for debugging
        logging.warning(f"No {underlying} ATM option found for strike={atm_strike}, expiry={expiry_date}")
        sample = [s for s in self.instruments.keys() if s.startswith(underlying) and option_type in s][:5]
        logging.warning(f"Sample {underlying} {option_type} options: {sample}")
        logging.warning(f"Available expiries: {sorted_expiries}")
        return None

    def find_atm_straddle(self, spot_price: float, min_dte: int = 3,
                          underlying: str = "NIFTY", strike_interval: int = 50,
                          default_lot_size: int = 75) -> Optional[Dict]:
        """
        Find ATM straddle (both CE and PE) for weekly expiry with minimum DTE.

        Args:
            spot_price: Current spot price
            min_dte: Minimum days to expiry
            underlying: Underlying symbol (e.g., "NIFTY", "SENSEX")
            strike_interval: Strike price interval
            default_lot_size: Default lot size if not found

        Returns:
            Dict with 'ce' and 'pe' option details, or None if not found.
        """
        ce = self.find_atm_option(spot_price, "CE", min_dte, underlying, strike_interval, default_lot_size)
        pe = self.find_atm_option(spot_price, "PE", min_dte, underlying, strike_interval, default_lot_size)

        if ce and pe:
            logging.info(f"Found {underlying} ATM straddle: CE={ce['symbol']}, PE={pe['symbol']}, "
                        f"strike={ce['strike']}, expiry={ce['expiry']}")
            return {
                'ce': ce,
                'pe': pe,
                'strike': ce['strike'],
                'expiry': ce['expiry'],
                'lot_size': ce['lot_size'],
                'exchange': ce['exchange'],
                'underlying': underlying
            }

        if not ce:
            logging.error(f"Could not find {underlying} ATM CE for straddle")
        if not pe:
            logging.error(f"Could not find {underlying} ATM PE for straddle")
        return None

    def get_ltp(self, symbol: str) -> Optional[float]:
        """Get LTP for a symbol"""
        token = self.get_token(symbol)
        if not token:
            return None

        inst = self.instruments.get(symbol, {})
        exchange = inst.get('exchange', 'NFO')

        try:
            quote = self.kite.ltp([f"{exchange}:{symbol}"])
            return quote.get(f"{exchange}:{symbol}", {}).get('last_price')
        except Exception as e:
            logging.error(f"Error fetching LTP for {symbol}: {e}")
            return None

# =============================================================================
# TELEGRAM ALERTER
# =============================================================================

class TelegramAlerter:
    """Send alerts to Telegram"""

    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def send(self, message: str, parse_mode: str = "HTML"):
        """Send message to Telegram"""
        if not self.enabled:
            return

        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode
            }
            response = requests.post(url, json=payload, timeout=10)
            return response.ok
        except Exception as e:
            logging.error(f"Telegram send failed: {e}")
            return False

    def alert_startup(self, paper_mode: bool, capital: float):
        mode = "PAPER" if paper_mode else "LIVE"
        msg = f"""
🚀 <b>Z-Score Bot Started</b>
Mode: <code>{mode}</code> | Capital: <code>{capital:,.0f}</code>
"""
        self.send(msg)

    def alert_signal(self, z_score: float, basis: float, fut_used: str, spot: float):
        msg = f"""
🎯 <b>SIGNAL DETECTED</b>
━━━━━━━━━━━━━━━━━━━
Z-Score: <code>{z_score:.2f}</code>
Basis: <code>{basis:.1f}</code>
Futures: <code>{fut_used}</code>
Spot: <code>{spot:.2f}</code>
Time: <code>{datetime.now().strftime('%H:%M:%S')}</code>
"""
        self.send(msg)

    def alert_entry(self, symbol: str, qty: int, price: float, stop: float, target: float, paper: bool):
        mode = "📝 PAPER" if paper else "💰 LIVE"
        msg = f"""
{mode} <b>ENTRY ORDER</b>
━━━━━━━━━━━━━━━━━━━
Symbol: <code>{symbol}</code>
Qty: <code>{qty}</code>
Price: <code>₹{price:.2f}</code>
Stop: <code>₹{stop:.2f}</code>
Target: <code>₹{target:.2f}</code>
"""
        self.send(msg)

    def alert_straddle_entry(self, ce_symbol: str, pe_symbol: str, qty: int,
                             ce_price: float, pe_price: float, target: float, paper: bool):
        mode = "📝 PAPER" if paper else "💰 LIVE"
        total = ce_price + pe_price
        msg = f"""
{mode} <b>STRADDLE ENTRY</b>
CE: <code>{ce_symbol}</code> @ <code>₹{ce_price:.2f}</code>
PE: <code>{pe_symbol}</code> @ <code>₹{pe_price:.2f}</code>
Qty: <code>{qty}</code> | Total: <code>₹{total:.2f}</code>
Target: <code>₹{target:.2f}</code> (+15%)
"""
        self.send(msg)

    def alert_straddle_exit(self, ce_symbol: str, pe_symbol: str,
                            ce_entry: float, ce_exit: float,
                            pe_entry: float, pe_exit: float,
                            total_pnl: float, reason: str, paper: bool):
        mode = "📝 PAPER" if paper else "💰 LIVE"
        emoji = "✅" if total_pnl > 0 else "❌"
        ce_pnl = ce_exit - ce_entry
        pe_pnl = pe_exit - pe_entry
        msg = f"""
{emoji} {mode} <b>STRADDLE EXIT</b>
━━━━━━━━━━━━━━━━━━━
CE: <code>₹{ce_entry:.2f}</code> → <code>₹{ce_exit:.2f}</code> ({ce_pnl:+.2f})
PE: <code>₹{pe_entry:.2f}</code> → <code>₹{pe_exit:.2f}</code> ({pe_pnl:+.2f})
━━━━━━━━━━━━━━━━━━━
Total P&L: <code>₹{total_pnl:+.2f}</code>
Reason: <code>{reason}</code>
"""
        self.send(msg)

    def alert_exit(self, symbol: str, entry_price: float, exit_price: float,
                   pnl: float, reason: str, paper: bool):
        mode = "📝 PAPER" if paper else "💰 LIVE"
        emoji = "✅" if pnl > 0 else "❌"
        msg = f"""
{emoji} {mode} <b>EXIT - {reason}</b>
━━━━━━━━━━━━━━━━━━━
Symbol: <code>{symbol}</code>
Entry: <code>₹{entry_price:.2f}</code>
Exit: <code>₹{exit_price:.2f}</code>
P&L: <code>₹{pnl:+.2f}</code>
"""
        self.send(msg)

    def alert_daily_summary(self, trades: int, pnl: float, wins: int, losses: int):
        msg = f"""
📊 <b>DAILY SUMMARY</b>
━━━━━━━━━━━━━━━━━━━
Total Trades: <code>{trades}</code>
Wins: <code>{wins}</code>
Losses: <code>{losses}</code>
Net P&L: <code>₹{pnl:+.2f}</code>
"""
        self.send(msg)

    def alert_error(self, error: str):
        msg = f"""
⚠️ <b>ERROR</b>
━━━━━━━━━━━━━━━━━━━
<code>{error}</code>
Time: <code>{datetime.now().strftime('%H:%M:%S')}</code>
"""
        self.send(msg)

    def alert_recovery(self, position: Position):
        msg = f"""
🔄 <b>POSITION RECOVERED</b>
━━━━━━━━━━━━━━━━━━━
Symbol: <code>{position.symbol}</code>
Entry: <code>₹{position.entry_price:.2f}</code>
Stop: <code>₹{position.stop_loss:.2f}</code>
Target: <code>₹{position.target:.2f}</code>
Deadline: <code>{position.exit_deadline}</code>
"""
        self.send(msg)

# =============================================================================
# SIGNAL ENGINE
# =============================================================================

class SignalEngine:
    """Calculate z-score and generate signals"""

    def __init__(self, lookback: int = 20):
        self.lookback = lookback
        self.basis_buffer = deque(maxlen=lookback)
        self.spot_buffer = deque(maxlen=lookback)  # Track spot for direction
        self.last_minute = None

    def update(self, spot: float, current_fut: float, next_fut: float,
               min_basis_current: float) -> Tuple[float, float, str, float]:
        """
        Update with new prices and return (z_score, basis, fut_used, basis_pct)
        """
        # Guard against division by zero (before first tick)
        if spot <= 0:
            return 0.0, 0.0, "CURRENT", 0.0

        # Always use CURRENT month futures (trade what you analyze)
        # Note: min_basis_current kept in signature for backward compatibility
        active_basis = current_fut - spot
        fut_used = "CURRENT"

        # Calculate basis percentage
        basis_pct = (active_basis / spot) * 100

        # Update buffer (once per minute)
        current_minute = datetime.now().strftime("%H:%M")
        if current_minute != self.last_minute:
            self.basis_buffer.append(basis_pct)
            self.spot_buffer.append(spot)  # Track spot for direction
            self.last_minute = current_minute

        # Calculate z-score
        if len(self.basis_buffer) < self.lookback:
            return 0.0, active_basis, fut_used, basis_pct

        mean = statistics.mean(self.basis_buffer)
        std = statistics.stdev(self.basis_buffer)

        if std == 0:
            return 0.0, active_basis, fut_used, basis_pct

        z_score = (basis_pct - mean) / std

        return z_score, active_basis, fut_used, basis_pct

    def get_direction(self, lookback_minutes: int = 5) -> str:
        """
        Determine market direction based on momentum (rate of change).

        Uses average per-minute momentum over the lookback period to smooth out noise.
        Also checks consistency - how many candles moved in the same direction.

        Args:
            lookback_minutes: How many minutes to analyze (default 5)

        Returns:
            "BULLISH" if strong upward momentum
            "BEARISH" if strong downward momentum
            "NEUTRAL" if weak/mixed momentum
        """
        if len(self.spot_buffer) < lookback_minutes + 1:
            return "NEUTRAL"

        # Get recent prices
        recent_prices = list(self.spot_buffer)[-lookback_minutes - 1:]

        # Calculate per-minute changes
        changes = []
        for i in range(1, len(recent_prices)):
            change = recent_prices[i] - recent_prices[i - 1]
            changes.append(change)

        if not changes:
            return "NEUTRAL"

        # Calculate momentum metrics
        total_change = sum(changes)
        avg_change = total_change / len(changes)

        # Count direction consistency
        up_candles = sum(1 for c in changes if c > 0)
        down_candles = sum(1 for c in changes if c < 0)

        # Calculate momentum as points per minute
        momentum = avg_change

        # Log for debugging
        logging.debug(f"Momentum: {momentum:.2f} pts/min, Up: {up_candles}, Down: {down_candles}")

        # Thresholds:
        # - Need at least 1 pt/min average momentum
        # - Need majority of candles in same direction (>60%)
        min_momentum = 1.0  # points per minute
        consistency_threshold = 0.6  # 60% of candles in same direction

        if momentum > min_momentum and (up_candles / len(changes)) >= consistency_threshold:
            return "BULLISH"
        elif momentum < -min_momentum and (down_candles / len(changes)) >= consistency_threshold:
            return "BEARISH"
        else:
            return "NEUTRAL"

    def should_exit(self, position: Position, current_premium: float,
                    z_score: float) -> Tuple[bool, str]:
        """Check if exit conditions are met.

        For straddle (symbol="STRADDLE"):
        - Only TIME and TARGET exits (no stop loss, no z-revert)
        - Rationale: One leg saves in volatility, theta decay minimal with DTE>3
        """

        if not position.active:
            return False, ""

        now = datetime.now()
        is_straddle = position.symbol == "STRADDLE"

        # Time-based exit (with validation) - applies to all
        if position.exit_deadline:
            try:
                deadline = datetime.fromisoformat(position.exit_deadline)
                if now >= deadline:
                    return True, "TIME"
            except ValueError:
                logging.warning(f"Invalid exit_deadline format: {position.exit_deadline}")

        # Target hit - applies to all
        if current_premium >= position.target:
            return True, "TARGET"

        # Stop loss - DISABLED for straddle (time exit handles risk)
        if not is_straddle and position.stop_loss > 0:
            if current_premium <= position.stop_loss:
                return True, "STOP_LOSS"

        # Z-score reversion - DISABLED for straddle (basis revert != volatility end)
        if not is_straddle:
            if z_score < 0:
                return True, "Z_REVERT"

        return False, ""

# =============================================================================
# ORDER MANAGER
# =============================================================================

class OrderManager:
    """Handle order placement (paper and live) with DB tracking"""

    def __init__(self, kite: KiteConnect, inst_mgr: InstrumentManager,
                 db: TradingDB, paper_mode: bool, config: dict):
        self.kite = kite
        self.inst_mgr = inst_mgr
        self.db = db
        self.paper_mode = paper_mode
        self.config = config

    def get_atm_option(self, spot: float, direction: str = "BULLISH") -> Optional[Dict]:
        """Get ATM option details with DTE filter.

        Args:
            spot: Current spot price for ATM strike calculation
            direction: Market direction - "BULLISH" for CE, "BEARISH" for PE

        Returns:
            Option dict with symbol, token, strike, expiry, dte, lot_size
        """
        min_dte = self.config.get('instruments', {}).get('min_dte', 3)
        option_type = "CE" if direction == "BULLISH" else "PE"
        return self.inst_mgr.find_atm_option(spot, option_type, min_dte)

    def get_atm_straddle(self, spot: float) -> Optional[Dict]:
        """Get ATM straddle (CE + PE) details with DTE filter.

        Returns:
            Dict with 'ce' and 'pe' option details, or None if not found
        """
        min_dte = self.config.get('instruments', {}).get('min_dte', 3)
        return self.inst_mgr.find_atm_straddle(spot, min_dte)

    def place_straddle_entry_parallel(self, ce_symbol: str, ce_token: int,
                                       pe_symbol: str, pe_token: int,
                                       qty: int, ce_price: float, pe_price: float,
                                       exchange: str = "NFO"
                                       ) -> Tuple[bool, bool, float, float, str, str]:
        """Place CE and PE entry orders in parallel for minimal leg risk.

        Args:
            ce_symbol: CE option symbol
            ce_token: CE instrument token
            pe_symbol: PE option symbol
            pe_token: PE instrument token
            qty: Quantity per leg
            ce_price: Expected CE price
            pe_price: Expected PE price
            exchange: Exchange (NFO for NIFTY, BFO for SENSEX)

        Returns:
            (ce_success, pe_success, ce_fill, pe_fill, ce_order_id, pe_order_id)
        """
        def place_ce():
            try:
                return self.place_entry_order(ce_symbol, ce_token, qty, ce_price, exchange)
            except Exception as e:
                logging.error(f"CE order exception: {e}")
                return False, 0.0, ""

        def place_pe():
            try:
                return self.place_entry_order(pe_symbol, pe_token, qty, pe_price, exchange)
            except Exception as e:
                logging.error(f"PE order exception: {e}")
                return False, 0.0, ""

        # Execute both orders in parallel using ThreadPoolExecutor
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                ce_future = executor.submit(place_ce)
                pe_future = executor.submit(place_pe)

                # Wait for both to complete with timeout
                ce_result = ce_future.result(timeout=30)
                pe_result = pe_future.result(timeout=30)

            ce_success, ce_fill, ce_order_id = ce_result
            pe_success, pe_fill, pe_order_id = pe_result

        except concurrent.futures.TimeoutError:
            logging.error("Parallel order placement timed out")
            return False, False, 0.0, 0.0, "", ""
        except Exception as e:
            logging.error(f"Parallel order placement failed: {e}")
            return False, False, 0.0, 0.0, "", ""

        logging.info(f"Parallel straddle entry: CE={ce_success}@{ce_fill}, PE={pe_success}@{pe_fill}")

        return ce_success, pe_success, ce_fill, pe_fill, ce_order_id, pe_order_id

    def get_option_ltp(self, symbol: str) -> Optional[float]:
        """Get LTP for option"""
        return self.inst_mgr.get_ltp(symbol)

    def check_margin(self, symbol: str, qty: int) -> Tuple[bool, float]:
        """Check if sufficient margin is available"""
        if self.paper_mode:
            return True, 100000.0  # Assume enough margin in paper mode

        try:
            margins = self.kite.margins()
            available = margins.get('equity', {}).get('available', {}).get('live_balance', 0)

            # Rough margin estimate for options (premium * qty * 1.5 for buffer)
            ltp = self.get_option_ltp(symbol)
            if ltp:
                required = ltp * qty * 1.5
                if available >= required:
                    return True, available
                else:
                    logging.warning(f"Insufficient margin: available={available}, required={required}")
                    return False, available

            return True, available  # If can't get LTP, proceed anyway
        except Exception as e:
            logging.error(f"Margin check failed: {e}")
            return True, 0  # Proceed if margin check fails

    def verify_order(self, order_id: str, max_wait: int = 10) -> Tuple[str, float]:
        """Verify order status with Kite API. Returns (status, fill_price)"""
        if self.paper_mode or order_id.startswith("PAPER"):
            return "COMPLETE", 0.0

        for _ in range(max_wait):
            try:
                orders = self.kite.order_history(order_id)
                if orders:
                    latest = orders[-1]
                    status = latest.get('status', '')
                    fill_price = latest.get('average_price', 0)

                    if status == 'COMPLETE':
                        return status, fill_price
                    elif status in ['REJECTED', 'CANCELLED']:
                        return status, 0.0

                time.sleep(1)
            except Exception as e:
                logging.error(f"Order verification failed: {e}")
                time.sleep(1)

        return "PENDING", 0.0

    def place_entry_order(self, symbol: str, token: int, qty: int,
                          current_price: float, exchange: str = "NFO") -> Tuple[bool, float, str]:
        """Place entry order with DB tracking. Returns (success, fill_price, order_id)

        Args:
            symbol: Trading symbol
            token: Instrument token
            qty: Quantity
            current_price: Expected price
            exchange: Exchange (NFO for NIFTY, BFO for SENSEX)
        """

        # Create order record in DB first
        order = Order(
            order_type="ENTRY",
            symbol=symbol,
            exchange=exchange,
            qty=qty,
            side="BUY",
            order_status="PENDING",
            expected_price=current_price,
            paper_trade=self.paper_mode
        )

        if self.paper_mode:
            order_id = f"PAPER_{int(time.time() * 1000)}_{symbol[-6:]}"  # Milliseconds + symbol suffix for uniqueness
            order.order_id = order_id
            order.fill_price = current_price
            order.order_status = "COMPLETE"
            self.db.create_order(order)
            logging.info(f"[PAPER] BUY {qty} {symbol} @ {current_price}")
            return True, current_price, order_id

        order_created = False
        try:
            # Check margin first
            has_margin, available = self.check_margin(symbol, qty)
            if not has_margin:
                order.order_status = "REJECTED"
                order.error_message = f"Insufficient margin: {available}"
                self.db.create_order(order)
                return False, 0.0, ""

            # Map exchange string to Kite constant
            kite_exchange = self.kite.EXCHANGE_BFO if exchange == "BFO" else self.kite.EXCHANGE_NFO

            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=kite_exchange,
                tradingsymbol=symbol,
                transaction_type=self.kite.TRANSACTION_TYPE_BUY,
                quantity=qty,
                product=self.kite.PRODUCT_MIS,
                order_type=self.kite.ORDER_TYPE_MARKET
            )

            order.order_id = str(order_id)
            self.db.create_order(order)
            order_created = True

            # Verify order completion
            status, fill_price = self.verify_order(str(order_id))

            if status == "COMPLETE":
                self.db.update_order(str(order_id), fill_price, status)
                slippage = fill_price - current_price
                logging.info(f"[LIVE] BUY {qty} {symbol} @ {fill_price} (slippage: {slippage:+.2f}), Order: {order_id}")
                return True, fill_price, str(order_id)
            else:
                self.db.update_order(str(order_id), 0, status, f"Order {status}")
                logging.error(f"Order {status}: {order_id}")
                return False, 0.0, str(order_id)

        except Exception as e:
            logging.error(f"Order failed: {e}")
            if order_created:
                # Update existing order record
                self.db.update_order(order.order_id, 0, "ERROR", str(e))
            else:
                # Create new error record
                order.order_status = "ERROR"
                order.error_message = str(e)
                self.db.create_order(order)
            return False, 0.0, order.order_id if order_created else ""

    def place_exit_order(self, symbol: str, qty: int,
                         current_price: float, exchange: str = "NFO") -> Tuple[bool, float, str]:
        """Place exit order with DB tracking. Returns (success, fill_price, order_id)

        Args:
            symbol: Trading symbol
            qty: Quantity
            current_price: Expected price
            exchange: Exchange (NFO for NIFTY, BFO for SENSEX)
        """

        order = Order(
            order_type="EXIT",
            symbol=symbol,
            exchange=exchange,
            qty=qty,
            side="SELL",
            order_status="PENDING",
            expected_price=current_price,
            paper_trade=self.paper_mode
        )

        if self.paper_mode:
            order_id = f"PAPER_EXIT_{int(time.time() * 1000)}_{symbol[-6:]}"
            order.order_id = order_id
            order.fill_price = current_price
            order.order_status = "COMPLETE"
            self.db.create_order(order)
            logging.info(f"[PAPER] SELL {qty} {symbol} @ {current_price}")
            return True, current_price, order_id

        order_created = False
        try:
            # Map exchange string to Kite constant
            kite_exchange = self.kite.EXCHANGE_BFO if exchange == "BFO" else self.kite.EXCHANGE_NFO

            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=kite_exchange,
                tradingsymbol=symbol,
                transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                quantity=qty,
                product=self.kite.PRODUCT_MIS,
                order_type=self.kite.ORDER_TYPE_MARKET
            )

            order.order_id = str(order_id)
            self.db.create_order(order)
            order_created = True

            # Verify order completion
            status, fill_price = self.verify_order(str(order_id))

            if status == "COMPLETE":
                self.db.update_order(str(order_id), fill_price, status)
                slippage = fill_price - current_price
                logging.info(f"[LIVE] SELL {qty} {symbol} @ {fill_price} (slippage: {slippage:+.2f}), Order: {order_id}")
                return True, fill_price, str(order_id)
            else:
                self.db.update_order(str(order_id), 0, status, f"Order {status}")
                logging.error(f"Exit order {status}: {order_id}")
                return False, 0.0, str(order_id)

        except Exception as e:
            logging.error(f"Exit order failed: {e}")
            if order_created:
                self.db.update_order(order.order_id, 0, "ERROR", str(e))
            else:
                order.order_status = "ERROR"
                order.error_message = str(e)
                self.db.create_order(order)
            return False, 0.0, order.order_id if order_created else ""

    # =========================================================================
    # SMART EXIT METHODS (Bid/Ask Optimization)
    # =========================================================================

    def get_market_depth(self, symbol: str, exchange: str = "NFO") -> Optional[Dict]:
        """
        Fetch bid/ask depth for a symbol.

        Args:
            symbol: Trading symbol
            exchange: Exchange (NFO for NIFTY, BFO for SENSEX)

        Returns:
            {
                'best_bid': float,
                'best_ask': float,
                'spread': float,
                'mid_price': float,
                'bid_qty': int,
                'ask_qty': int,
            }
            or None if fetch fails
        """
        try:
            quote_key = f"{exchange}:{symbol}"
            quote = self.kite.quote([quote_key])
            data = quote.get(quote_key, {})
            depth = data.get('depth', {})

            buy_depth = depth.get('buy', [])
            sell_depth = depth.get('sell', [])

            if not buy_depth or not sell_depth:
                logging.warning(f"Empty depth for {symbol}")
                return None

            best_bid = buy_depth[0]['price'] if buy_depth[0]['price'] > 0 else 0
            best_ask = sell_depth[0]['price'] if sell_depth[0]['price'] > 0 else 0

            if best_bid <= 0 or best_ask <= 0:
                logging.warning(f"Invalid bid/ask for {symbol}: bid={best_bid}, ask={best_ask}")
                return None

            spread = best_ask - best_bid
            mid_price = (best_bid + best_ask) / 2

            return {
                'best_bid': best_bid,
                'best_ask': best_ask,
                'spread': spread,
                'mid_price': mid_price,
                'bid_qty': buy_depth[0].get('quantity', 0),
                'ask_qty': sell_depth[0].get('quantity', 0),
                'last_price': data.get('last_price', mid_price),
            }
        except Exception as e:
            logging.error(f"Failed to get depth for {symbol}: {e}")
            return None

    def place_limit_exit(self, symbol: str, qty: int, price: float, exchange: str = "NFO") -> Optional[str]:
        """
        Place a LIMIT sell order. Returns order_id or None if failed.
        Does not wait for fill - caller must monitor.

        Args:
            symbol: Trading symbol
            qty: Quantity
            price: Limit price
            exchange: Exchange (NFO for NIFTY, BFO for SENSEX)
        """
        try:
            kite_exchange = self.kite.EXCHANGE_BFO if exchange == "BFO" else self.kite.EXCHANGE_NFO

            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=kite_exchange,
                tradingsymbol=symbol,
                transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                quantity=qty,
                product=self.kite.PRODUCT_MIS,
                order_type=self.kite.ORDER_TYPE_LIMIT,
                price=price
            )
            logging.info(f"Placed LIMIT sell {qty} {symbol} @ {price}, order_id={order_id}")
            return str(order_id)
        except Exception as e:
            logging.error(f"Failed to place limit exit for {symbol}: {e}")
            return None

    def place_market_exit(self, symbol: str, qty: int, exchange: str = "NFO") -> Optional[str]:
        """
        Place a MARKET sell order. Returns order_id or None if failed.

        Args:
            symbol: Trading symbol
            qty: Quantity
            exchange: Exchange (NFO for NIFTY, BFO for SENSEX)
        """
        try:
            kite_exchange = self.kite.EXCHANGE_BFO if exchange == "BFO" else self.kite.EXCHANGE_NFO

            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=kite_exchange,
                tradingsymbol=symbol,
                transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                quantity=qty,
                product=self.kite.PRODUCT_MIS,
                order_type=self.kite.ORDER_TYPE_MARKET
            )
            logging.info(f"Placed MARKET sell {qty} {symbol}, order_id={order_id}")
            return str(order_id)
        except Exception as e:
            logging.error(f"Failed to place market exit for {symbol}: {e}")
            return None

    def modify_order_to_market(self, order_id: str) -> bool:
        """Convert a limit order to market order."""
        try:
            self.kite.modify_order(
                variety=self.kite.VARIETY_REGULAR,
                order_id=order_id,
                order_type=self.kite.ORDER_TYPE_MARKET
            )
            logging.info(f"Modified order {order_id} to MARKET")
            return True
        except Exception as e:
            logging.error(f"Failed to modify order {order_id} to market: {e}")
            return False

    def get_order_status_quick(self, order_id: str) -> Dict:
        """
        Quick order status check (single call, no retry).
        Returns {'status': str, 'filled_qty': int, 'price': float}
        """
        try:
            orders = self.kite.order_history(order_id)
            if orders:
                latest = orders[-1]
                return {
                    'status': latest.get('status', 'UNKNOWN'),
                    'filled_qty': latest.get('filled_quantity', 0),
                    'price': latest.get('average_price', 0.0),
                }
        except Exception as e:
            logging.error(f"Order status check failed for {order_id}: {e}")
        return {'status': 'UNKNOWN', 'filled_qty': 0, 'price': 0.0}

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            self.kite.cancel_order(
                variety=self.kite.VARIETY_REGULAR,
                order_id=order_id
            )
            logging.info(f"Cancelled order {order_id}")
            return True
        except Exception as e:
            logging.error(f"Failed to cancel order {order_id}: {e}")
            return False

    def exit_straddle_smart(
        self,
        ce_symbol: str,
        pe_symbol: str,
        qty: int,
        ce_expected_price: float,
        pe_expected_price: float,
        exchange: str = "NFO"
    ) -> Tuple[bool, bool, float, float, str, str]:
        """
        Smart straddle exit with parallel limit orders and cross-leg acceleration.

        Algorithm:
        1. Fetch market depth for both legs
        2. If spread > 1pt, use LIMIT at mid-price; else use MARKET
        3. Monitor both orders in parallel (3s timeout)
        4. If one leg fills, immediately convert other to market (cross-leg acceleration)
        5. At timeout, convert any unfilled to market

        Args:
            ce_symbol: CE option symbol
            pe_symbol: PE option symbol
            qty: Quantity per leg
            ce_expected_price: Expected CE price (for paper mode / fallback)
            pe_expected_price: Expected PE price (for paper mode / fallback)
            exchange: Exchange (NFO for NIFTY, BFO for SENSEX)

        Returns:
            (ce_success, pe_success, ce_fill_price, pe_fill_price, ce_order_id, pe_order_id)
        """
        TIMEOUT = 3.0
        POLL_INTERVAL = 0.3
        SPREAD_THRESHOLD = 1.0

        # Paper trading mode - use mid-price simulation
        if self.paper_mode:
            ce_depth = self.get_market_depth(ce_symbol, exchange)
            pe_depth = self.get_market_depth(pe_symbol, exchange)

            # Use mid-price if available, else expected price
            ce_fill = ce_depth['mid_price'] if ce_depth else ce_expected_price
            pe_fill = pe_depth['mid_price'] if pe_depth else pe_expected_price

            # Use milliseconds for unique order IDs
            ts = int(time.time() * 1000)
            paper_ce_order_id = f"PAPER_EXIT_{ts}_CE"
            paper_pe_order_id = f"PAPER_EXIT_{ts + 1}_PE"

            # Calculate savings for logging
            if ce_depth and ce_depth['spread'] > SPREAD_THRESHOLD:
                ce_saved = ce_fill - ce_depth['best_bid']
                logging.info(f"[PAPER] CE smart exit: mid={ce_fill:.2f}, bid={ce_depth['best_bid']:.2f}, saved={ce_saved:+.2f}")
            if pe_depth and pe_depth['spread'] > SPREAD_THRESHOLD:
                pe_saved = pe_fill - pe_depth['best_bid']
                logging.info(f"[PAPER] PE smart exit: mid={pe_fill:.2f}, bid={pe_depth['best_bid']:.2f}, saved={pe_saved:+.2f}")

            logging.info(f"[PAPER] Smart straddle exit: CE={ce_fill:.2f}, PE={pe_fill:.2f}")

            # Create order records
            for symbol, fill, oid in [(ce_symbol, ce_fill, paper_ce_order_id), (pe_symbol, pe_fill, paper_pe_order_id)]:
                order = Order(
                    order_type="EXIT",
                    symbol=symbol,
                    exchange=exchange,
                    qty=qty,
                    side="SELL",
                    order_status="COMPLETE",
                    expected_price=fill,
                    fill_price=fill,
                    order_id=oid,
                    paper_trade=True
                )
                self.db.create_order(order)

            return True, True, ce_fill, pe_fill, paper_ce_order_id, paper_pe_order_id

        # Live trading mode
        # 1. Fetch depth for both legs
        ce_depth = self.get_market_depth(ce_symbol, exchange)
        pe_depth = self.get_market_depth(pe_symbol, exchange)

        ce_use_limit = ce_depth and ce_depth['spread'] > SPREAD_THRESHOLD
        pe_use_limit = pe_depth and pe_depth['spread'] > SPREAD_THRESHOLD

        # Log spread info
        if ce_depth:
            logging.info(f"CE depth: bid={ce_depth['best_bid']:.2f}, ask={ce_depth['best_ask']:.2f}, "
                        f"spread={ce_depth['spread']:.2f}, use_limit={ce_use_limit}")
        if pe_depth:
            logging.info(f"PE depth: bid={pe_depth['best_bid']:.2f}, ask={pe_depth['best_ask']:.2f}, "
                        f"spread={pe_depth['spread']:.2f}, use_limit={pe_use_limit}")

        # 2. Place both orders simultaneously
        ce_order_id: Optional[str] = None
        pe_order_id: Optional[str] = None
        ce_filled = False
        pe_filled = False
        ce_fill_price = 0.0
        pe_fill_price = 0.0
        ce_is_limit = False  # Track if order is currently a limit order
        pe_is_limit = False

        # Place CE order
        if ce_use_limit and ce_depth:
            ce_mid = ce_depth['mid_price']
            ce_order_id = self.place_limit_exit(ce_symbol, qty, ce_mid, exchange)
            if ce_order_id:
                ce_is_limit = True
            else:
                # Fallback to market
                ce_order_id = self.place_market_exit(ce_symbol, qty, exchange)
        else:
            ce_order_id = self.place_market_exit(ce_symbol, qty, exchange)

        # Place PE order
        if pe_use_limit and pe_depth:
            pe_mid = pe_depth['mid_price']
            pe_order_id = self.place_limit_exit(pe_symbol, qty, pe_mid, exchange)
            if pe_order_id:
                pe_is_limit = True
            else:
                pe_order_id = self.place_market_exit(pe_symbol, qty, exchange)
        else:
            pe_order_id = self.place_market_exit(pe_symbol, qty, exchange)

        # Handle order placement failures
        # FIX #14: If one leg fails to place, cancel the other to avoid orphan
        if not ce_order_id and not pe_order_id:
            logging.error("Both exit orders failed to place")
            return False, False, 0.0, 0.0, "", ""

        if not ce_order_id and pe_order_id:
            logging.error("CE order failed, canceling PE to avoid orphan")
            self.cancel_order(pe_order_id)
            return False, False, 0.0, 0.0, "", ""

        if ce_order_id and not pe_order_id:
            logging.error("PE order failed, canceling CE to avoid orphan")
            self.cancel_order(ce_order_id)
            return False, False, 0.0, 0.0, "", ""

        # 3. Monitor with cross-leg acceleration
        start = time.time()

        while time.time() - start < TIMEOUT:
            # Check CE status
            if ce_order_id and not ce_filled:
                ce_status = self.get_order_status_quick(ce_order_id)
                if ce_status['status'] == 'COMPLETE':
                    ce_filled = True
                    ce_fill_price = ce_status['price']
                    ce_is_limit = False  # Order complete, no longer modifiable
                    logging.info(f"CE filled at {ce_fill_price:.2f}")

                    # CROSS-LEG ACCELERATION: Convert PE to market immediately
                    # FIX #11 & #12: Re-check PE status before modifying to prevent double execution
                    if pe_order_id and not pe_filled and pe_is_limit:
                        # Re-check PE status first to avoid race condition
                        pe_recheck = self.get_order_status_quick(pe_order_id)
                        if pe_recheck['status'] == 'COMPLETE':
                            # PE already filled, don't modify
                            pe_filled = True
                            pe_fill_price = pe_recheck['price']
                            pe_is_limit = False
                            logging.info(f"PE already filled at {pe_fill_price:.2f}")
                        else:
                            logging.info("CE filled - converting PE to market (cross-leg)")
                            if self.modify_order_to_market(pe_order_id):
                                pe_is_limit = False
                            # If modify fails, order might be filled or rejected
                            # Don't place new order - will check status next iteration

            # Check PE status
            if pe_order_id and not pe_filled:
                pe_status = self.get_order_status_quick(pe_order_id)
                if pe_status['status'] == 'COMPLETE':
                    pe_filled = True
                    pe_fill_price = pe_status['price']
                    pe_is_limit = False
                    logging.info(f"PE filled at {pe_fill_price:.2f}")

                    # CROSS-LEG ACCELERATION: Convert CE to market immediately
                    if ce_order_id and not ce_filled and ce_is_limit:
                        # Re-check CE status first
                        ce_recheck = self.get_order_status_quick(ce_order_id)
                        if ce_recheck['status'] == 'COMPLETE':
                            ce_filled = True
                            ce_fill_price = ce_recheck['price']
                            ce_is_limit = False
                            logging.info(f"CE already filled at {ce_fill_price:.2f}")
                        else:
                            logging.info("PE filled - converting CE to market (cross-leg)")
                            if self.modify_order_to_market(ce_order_id):
                                ce_is_limit = False

            if ce_filled and pe_filled:
                break

            time.sleep(POLL_INTERVAL)

        # 4. Timeout - force market any remaining
        elapsed = time.time() - start

        # FIX #11: Check status BEFORE modifying to prevent double execution
        if ce_order_id and not ce_filled:
            ce_status = self.get_order_status_quick(ce_order_id)
            if ce_status['status'] == 'COMPLETE':
                ce_filled = True
                ce_fill_price = ce_status['price']
                logging.info(f"CE filled during timeout check at {ce_fill_price:.2f}")
            elif ce_is_limit:
                logging.warning(f"CE timeout after {elapsed:.1f}s - converting to market")
                self.modify_order_to_market(ce_order_id)
                ce_is_limit = False

        if pe_order_id and not pe_filled:
            pe_status = self.get_order_status_quick(pe_order_id)
            if pe_status['status'] == 'COMPLETE':
                pe_filled = True
                pe_fill_price = pe_status['price']
                logging.info(f"PE filled during timeout check at {pe_fill_price:.2f}")
            elif pe_is_limit:
                logging.warning(f"PE timeout after {elapsed:.1f}s - converting to market")
                self.modify_order_to_market(pe_order_id)
                pe_is_limit = False

        # 5. Wait for final fills (with shorter verify timeout)
        # FIX #16: Reduced verify timeout from 5s to 2s to limit total time
        if ce_order_id and not ce_filled:
            status, price = self.verify_order(ce_order_id, max_wait=2)
            ce_filled = status == 'COMPLETE'
            ce_fill_price = price if ce_filled else 0.0

        if pe_order_id and not pe_filled:
            status, price = self.verify_order(pe_order_id, max_wait=2)
            pe_filled = status == 'COMPLETE'
            pe_fill_price = price if pe_filled else 0.0

        # Log results
        total_time = time.time() - start
        logging.info(f"Smart exit complete in {total_time:.1f}s: "
                    f"CE={ce_filled}@{ce_fill_price:.2f}, PE={pe_filled}@{pe_fill_price:.2f}")

        # Calculate and log savings
        if ce_depth and ce_filled and ce_fill_price > 0:
            ce_saved = ce_fill_price - ce_depth['best_bid']
            if ce_saved > 0.1:
                logging.info(f"CE saved {ce_saved:.2f} pts vs market bid")
        if pe_depth and pe_filled and pe_fill_price > 0:
            pe_saved = pe_fill_price - pe_depth['best_bid']
            if pe_saved > 0.1:
                logging.info(f"PE saved {pe_saved:.2f} pts vs market bid")

        return (
            ce_filled,
            pe_filled,
            ce_fill_price,
            pe_fill_price,
            ce_order_id or "",
            pe_order_id or ""
        )

# =============================================================================
# MAIN TRADING BOT
# =============================================================================

class ZScoreBot:
    """Main trading bot with multi-instrument support (NIFTY + SENSEX)"""

    def __init__(self, config_file: str = CONFIG_FILE):
        self.config = self._load_config(config_file)
        self.data_dir = resolve_path(self.config['data_dir'])
        self.setup_logging()

        # Load holidays and check if trading day
        self.holidays = load_holidays(self.config)
        if not is_trading_day(self.holidays):
            logging.info("Not a trading day (weekend/holiday). Exiting.")
            sys.exit(0)

        # Initialize Kite first
        self.kite = self._init_kite()

        # Download BSE instruments (SENSEX) - ZSCORE's responsibility
        self._download_bse_instruments()

        # Initialize database
        db_path = os.path.join(self.data_dir, "zscore_trades.db")
        self.db = TradingDB(db_path)

        # Initialize instrument manager with both NSE and BSE paths
        nse_path = self.config.get('market', {}).get('instruments_nse_path')
        bse_path = self.config.get('market', {}).get('instruments_bse_path')

        # Backward compatibility: check old 'instruments_path' config
        if not nse_path:
            nse_path = self.config.get('market', {}).get('instruments_path')

        if nse_path:
            nse_path = resolve_path(nse_path)
        if bse_path:
            bse_path = resolve_path(bse_path)

        self.inst_mgr = InstrumentManager(self.kite, self.data_dir, nse_path, bse_path)

        # Identify enabled instruments from config
        self.enabled_instruments = self._get_enabled_instruments()
        logging.info(f"Enabled instruments: {list(self.enabled_instruments.keys())}")

        # Token to instrument mapping for WebSocket callbacks (must be initialized before _resolve_tokens)
        self.token_to_instrument = {}  # token -> (inst_key, price_type)

        # Resolve instrument tokens for all enabled instruments
        self._resolve_tokens()

        # Initialize other components
        self.telegram = TelegramAlerter(
            self.config['telegram']['bot_token'],
            self.config['telegram']['chat_id'],
            self.config['telegram']['enabled']
        )

        # Create separate SignalEngine per instrument (independent z-scores)
        self.signal_engines = {}
        for inst_key in self.enabled_instruments:
            self.signal_engines[inst_key] = SignalEngine(self.config['strategy']['lookback_minutes'])
        logging.info(f"Created {len(self.signal_engines)} SignalEngine instances")

        self.order_mgr = OrderManager(self.kite, self.inst_mgr, self.db,
                                       self.config['paper_trade'], self.config)

        # Price storage per instrument
        # Structure: self.prices[inst_key] = {'spot': 0.0, 'current_fut': 0.0, ...}
        self.prices = {}
        for inst_key in self.enabled_instruments:
            self.prices[inst_key] = {
                'spot': 0.0,
                'current_fut': 0.0,
                'next_fut': 0.0,
                'ce': 0.0,
                'pe': 0.0
            }

        # Straddle tracking per instrument
        # Structure: self.straddle_state[inst_key] = {ce_symbol, ce_token, pe_symbol, ...}
        self.straddle_state = {}
        for inst_key in self.enabled_instruments:
            self.straddle_state[inst_key] = {
                'ce_symbol': None,
                'ce_token': None,
                'pe_symbol': None,
                'pe_token': None,
                'entry_value': 0.0,
                'trade_group_id': '',
                'lot_size': self.enabled_instruments[inst_key].get('default_lot_size', 75)
            }

        # WebSocket
        self.ticker = None
        self.ws_connected = False

        # Control
        self.running = False
        self.daily_summary_sent = False

        # Error tracking
        self.consecutive_errors = 0
        self.max_consecutive_errors = 10

    def _download_bse_instruments(self):
        """Download BSE instruments (SENSEX) on startup"""
        bse_path = self.config.get('market', {}).get('instruments_bse_path')
        if not bse_path:
            logging.info("BSE instruments path not configured, skipping download")
            return

        bse_path = resolve_path(bse_path)

        # Check if SENSEX is enabled
        sensex_config = self.config.get('instruments', {}).get('sensex', {})
        if not sensex_config.get('enabled', False):
            logging.info("SENSEX is disabled in config, skipping BSE instruments download")
            return

        # Check if we need to refresh (once per day)
        cache_age = get_instruments_age(bse_path)
        if cache_age is not None and cache_age < 8:  # Less than 8 hours old
            logging.info(f"BSE instruments file is recent ({cache_age:.1f}h old), skipping download")
            return

        # Download BSE instruments (uses public API, no auth needed)
        success, message = refresh_bse_instruments(bse_path)
        if success:
            logging.info(f"BSE instruments: {message}")
        else:
            # Note: Can't use self.telegram here - not initialized yet
            logging.error(f"BSE instruments download failed: {message}")

    def _get_enabled_instruments(self) -> Dict[str, Dict]:
        """Get dictionary of enabled instruments from config"""
        instruments_config = self.config.get('instruments', {})
        enabled = {}

        # Check for new multi-instrument config format
        for inst_key, inst_config in instruments_config.items():
            if isinstance(inst_config, dict) and inst_config.get('enabled', False):
                enabled[inst_key] = {
                    'spot_symbol': inst_config.get('spot_symbol'),
                    'underlying': inst_config.get('underlying'),
                    'exchange': inst_config.get('exchange', 'NFO'),
                    'spot_exchange': inst_config.get('spot_exchange', 'NSE'),
                    'strike_interval': inst_config.get('strike_interval', 50),
                    'min_dte': inst_config.get('min_dte', 3),
                    'default_lot_size': 75 if inst_key == 'nifty' else 10  # SENSEX lot is typically 10
                }

        # Backward compatibility: if no instruments enabled, check for old format
        if not enabled:
            old_config = instruments_config
            if old_config.get('spot_symbol') and old_config.get('underlying'):
                enabled['nifty'] = {
                    'spot_symbol': old_config.get('spot_symbol', 'NIFTY 50'),
                    'underlying': old_config.get('underlying', 'NIFTY'),
                    'exchange': 'NFO',
                    'spot_exchange': 'NSE',
                    'strike_interval': 50,
                    'min_dte': old_config.get('min_dte', 3),
                    'default_lot_size': 75
                }
                logging.info("Using backward-compatible single instrument config")

        return enabled

    def _load_config(self, config_file: str) -> dict:
        """Load configuration from JSON file"""
        with open(config_file, 'r') as f:
            return json.load(f)

    def _resolve_tokens(self):
        """Resolve instrument tokens for all enabled instruments"""
        # Well-known tokens for common indices
        well_known = {
            'NIFTY 50': 256265,
            'NIFTY BANK': 260105,
            'NIFTY': 256265,
            'SENSEX': 265  # BSE SENSEX token
        }

        # Store resolved data per instrument
        self.instrument_data = {}

        for inst_key, inst_config in self.enabled_instruments.items():
            spot_symbol = inst_config['spot_symbol']
            underlying = inst_config['underlying']

            logging.info(f"Resolving tokens for {inst_key.upper()} ({underlying})...")

            # Get spot token
            spot_token = self.inst_mgr.get_token(spot_symbol)
            if not spot_token:
                spot_token = well_known.get(spot_symbol)
                if spot_token:
                    logging.info(f"Using well-known token for {spot_symbol}: {spot_token}")
                else:
                    logging.error(f"Could not find spot token for {spot_symbol}")
                    continue

            # Auto-detect current and next month futures
            default_lot_size = inst_config.get('default_lot_size', 75)
            current_fut, next_fut = self.inst_mgr.find_futures(underlying, default_lot_size)

            if not current_fut:
                logging.error(f"Could not find current month {underlying} futures")
                continue

            # Store resolved data
            self.instrument_data[inst_key] = {
                'spot_token': spot_token,
                'spot_symbol': spot_symbol,
                'underlying': underlying,
                'exchange': inst_config['exchange'],
                'spot_exchange': inst_config['spot_exchange'],
                'strike_interval': inst_config['strike_interval'],
                'min_dte': inst_config['min_dte'],
                'current_fut': current_fut,
                'next_fut': next_fut if next_fut else current_fut
            }

            # Build token to instrument mapping
            self.token_to_instrument[spot_token] = (inst_key, 'spot')
            self.token_to_instrument[current_fut['token']] = (inst_key, 'current_fut')
            if next_fut:
                self.token_to_instrument[next_fut['token']] = (inst_key, 'next_fut')

            logging.info(f"Resolved {inst_key.upper()}: spot={spot_token}, "
                        f"current_fut={current_fut['symbol']}, "
                        f"next_fut={next_fut['symbol'] if next_fut else 'N/A'}")

        # Verify at least one instrument was resolved
        if not self.instrument_data:
            raise ValueError("No instruments could be resolved - check config and instruments files")

    def setup_logging(self):
        """Setup logging"""
        log_dir = os.path.join(self.data_dir, "logs", "zscore")
        os.makedirs(log_dir, exist_ok=True)

        log_file = os.path.join(log_dir, f"{datetime.now().strftime('%Y-%m-%d')}.log")

        handlers = []
        if self.config['logging']['file']:
            handlers.append(logging.FileHandler(log_file))
        if self.config['logging']['console']:
            handlers.append(logging.StreamHandler())

        logging.basicConfig(
            level=getattr(logging, self.config['logging']['level']),
            format='%(asctime)s [%(levelname)s] %(message)s',
            handlers=handlers
        )

    def _init_kite(self) -> KiteConnect:
        """Initialize Kite Connect"""
        creds_path = resolve_path(self.config['credentials']['path'])
        with open(creds_path, 'r') as f:
            creds = json.load(f)

        kite = KiteConnect(api_key=creds['api_key'])
        kite.set_access_token(creds['access_token'])

        logging.info(f"Kite initialized for user: {creds.get('user_id', 'unknown')}")
        return kite

    def _on_ticks(self, ws, ticks):
        """WebSocket tick callback - handles multiple instruments"""
        for tick in ticks:
            token = tick['instrument_token']
            ltp = tick['last_price']

            # Check if this token is mapped to an instrument
            if token in self.token_to_instrument:
                inst_key, price_type = self.token_to_instrument[token]
                if inst_key in self.prices:
                    self.prices[inst_key][price_type] = ltp

            # Also check straddle tokens (CE/PE) for each instrument
            for inst_key, state in self.straddle_state.items():
                if token == state.get('ce_token'):
                    self.prices[inst_key]['ce'] = ltp
                elif token == state.get('pe_token'):
                    self.prices[inst_key]['pe'] = ltp

    def _on_connect(self, ws, response):
        """WebSocket connect callback - subscribes to all enabled instruments"""
        logging.info("WebSocket connected")
        self.ws_connected = True

        # Build list of all tokens to subscribe to
        tokens = []
        for inst_key, inst_data in self.instrument_data.items():
            tokens.append(inst_data['spot_token'])
            tokens.append(inst_data['current_fut']['token'])
            if inst_data['next_fut']:
                tokens.append(inst_data['next_fut']['token'])

            # Also subscribe to any active straddle tokens
            state = self.straddle_state.get(inst_key, {})
            if state.get('ce_token'):
                tokens.append(state['ce_token'])
            if state.get('pe_token'):
                tokens.append(state['pe_token'])

        # Remove duplicates and subscribe
        tokens = list(set(tokens))
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_LTP, tokens)
        logging.info(f"Subscribed to {len(tokens)} instruments across {len(self.instrument_data)} underlyings")

    def _on_close(self, ws, code, reason):
        """WebSocket close callback"""
        logging.warning(f"WebSocket closed: {code} - {reason}")
        self.ws_connected = False
        # Note: Auto-reconnect is handled in main_loop by checking ws_connected

    def _on_error(self, ws, code, reason):
        """WebSocket error callback"""
        logging.error(f"WebSocket error: {code} - {reason}")
        self.telegram.alert_error(f"WebSocket error: {reason}")

    def _on_reconnect(self, ws, attempts_count):
        """WebSocket reconnect callback"""
        logging.info(f"WebSocket reconnecting, attempt {attempts_count}")

    def start_websocket(self):
        """Start WebSocket connection"""
        creds_path = resolve_path(self.config['credentials']['path'])
        with open(creds_path, 'r') as f:
            creds = json.load(f)

        self.ticker = KiteTicker(creds['api_key'], creds['access_token'])
        self.ticker.on_ticks = self._on_ticks
        self.ticker.on_connect = self._on_connect
        self.ticker.on_close = self._on_close
        self.ticker.on_error = self._on_error
        self.ticker.on_reconnect = self._on_reconnect

        # Connect in threaded mode with reconnect enabled
        self.ticker.connect(threaded=True)

        # Wait for connection with timeout
        connected = False
        for _ in range(15):  # 15 seconds timeout
            if self.ws_connected:
                connected = True
                break
            time.sleep(1)

        if not connected:
            error_msg = "WebSocket failed to connect within 15 seconds"
            logging.error(error_msg)
            self.telegram.alert_error(error_msg)

    def reconnect_websocket(self):
        """Attempt to reconnect WebSocket and resubscribe to active tokens"""
        if self.ticker:
            logging.info("Attempting WebSocket reconnection...")
            try:
                self.ticker.close()
            except Exception:
                pass
            time.sleep(2)
            self.start_websocket()

            # Resubscribe to any active straddle tokens after reconnection
            if self.ws_connected:
                straddle_tokens = []
                for inst_key, state in self.straddle_state.items():
                    if state.get('ce_token'):
                        straddle_tokens.append(state['ce_token'])
                    if state.get('pe_token'):
                        straddle_tokens.append(state['pe_token'])

                if straddle_tokens and self.ticker:
                    logging.info(f"Resubscribing to {len(straddle_tokens)} straddle tokens after reconnect")
                    self.ticker.subscribe(straddle_tokens)
                    self.ticker.set_mode(self.ticker.MODE_LTP, straddle_tokens)

    def check_and_recover_position(self):
        """Check for existing positions on startup from DB (handles straddle per instrument)"""
        positions = self.db.get_all_open_positions()

        if not positions:
            return False

        recovered = False

        # Group positions by instrument (NIFTY vs SENSEX)
        positions_by_instrument = {}
        for pos in positions:
            # Determine instrument based on symbol prefix
            inst_key = None
            for key, data in self.instrument_data.items():
                if pos.symbol.startswith(data['underlying']):
                    inst_key = key
                    break

            if inst_key:
                if inst_key not in positions_by_instrument:
                    positions_by_instrument[inst_key] = []
                positions_by_instrument[inst_key].append(pos)
            else:
                logging.warning(f"Could not identify instrument for position: {pos.symbol}")

        # Recover each instrument's positions
        for inst_key, inst_positions in positions_by_instrument.items():
            # Straddle recovery (2 positions for this instrument)
            if len(inst_positions) == 2:
                ce_pos = None
                pe_pos = None
                for pos in inst_positions:
                    if 'CE' in pos.symbol:
                        ce_pos = pos
                    elif 'PE' in pos.symbol:
                        pe_pos = pos

                if ce_pos and pe_pos:
                    logging.info(f"[{inst_key.upper()}] Recovering straddle: CE={ce_pos.symbol}, PE={pe_pos.symbol}")

                    # Restore straddle tracking
                    ce_token = self.inst_mgr.get_token(ce_pos.symbol)
                    pe_token = self.inst_mgr.get_token(pe_pos.symbol)

                    if ce_token and pe_token:
                        state = self.straddle_state[inst_key]
                        state['ce_symbol'] = ce_pos.symbol
                        state['ce_token'] = ce_token
                        state['pe_symbol'] = pe_pos.symbol
                        state['pe_token'] = pe_token
                        state['lot_size'] = ce_pos.lot_size
                        state['entry_value'] = ce_pos.entry_price + pe_pos.entry_price
                        state['trade_group_id'] = ce_pos.trade_group_id

                        # Add to token mapping for WebSocket
                        self.token_to_instrument[ce_token] = (inst_key, 'ce')
                        self.token_to_instrument[pe_token] = (inst_key, 'pe')

                        # Subscribe to both options
                        if self.ws_connected and self.ticker:
                            self.ticker.subscribe([ce_token, pe_token])
                            self.ticker.set_mode(self.ticker.MODE_LTP, [ce_token, pe_token])

                        self.telegram.send(f"🔄 <b>[{inst_key.upper()}] Straddle Recovered</b>\nCE: {ce_pos.symbol}\nPE: {pe_pos.symbol}\nEntry: ₹{state['entry_value']:.2f}")
                        recovered = True
                    else:
                        logging.error(f"[{inst_key.upper()}] Could not find tokens for straddle recovery")
                else:
                    logging.warning(f"[{inst_key.upper()}] Found 2 positions but couldn't identify CE/PE pair")

            # Single position recovery (orphan from partial straddle failure)
            elif len(inst_positions) == 1:
                db_pos = inst_positions[0]
                logging.info(f"[{inst_key.upper()}] Found existing single position: {db_pos.symbol}")

                token = self.inst_mgr.get_token(db_pos.symbol)
                if token:
                    # Store in straddle state (as orphan)
                    state = self.straddle_state[inst_key]
                    if 'CE' in db_pos.symbol:
                        state['ce_symbol'] = db_pos.symbol
                        state['ce_token'] = token
                        self.token_to_instrument[token] = (inst_key, 'ce')
                    elif 'PE' in db_pos.symbol:
                        state['pe_symbol'] = db_pos.symbol
                        state['pe_token'] = token
                        self.token_to_instrument[token] = (inst_key, 'pe')

                    state['lot_size'] = db_pos.lot_size

                    if self.ws_connected and self.ticker:
                        self.ticker.subscribe([token])
                        self.ticker.set_mode(self.ticker.MODE_LTP, [token])

                    pos = Position(
                        active=True,
                        symbol=db_pos.symbol,
                        entry_price=db_pos.entry_price,
                        stop_loss=db_pos.stop_loss,
                        target=db_pos.target,
                        exit_deadline=db_pos.exit_deadline
                    )
                    self.telegram.alert_recovery(pos)
                    recovered = True
                else:
                    logging.error(f"[{inst_key.upper()}] Could not find token for {db_pos.symbol}")

        return recovered

    def process_entry(self, z_score: float, basis: float, fut_used: str, spot: float, inst_key: str = 'nifty'):
        """Process entry signal - buy straddle (both CE and PE) for specific instrument"""

        # Get instrument config
        inst_data = self.instrument_data.get(inst_key)
        if not inst_data:
            logging.error(f"No instrument data found for {inst_key}")
            return

        underlying = inst_data['underlying']
        strike_interval = inst_data['strike_interval']
        min_dte = inst_data['min_dte']
        default_lot_size = self.straddle_state[inst_key]['lot_size']

        # Get ATM straddle (both CE and PE) for this instrument
        straddle = self.inst_mgr.find_atm_straddle(
            spot, min_dte, underlying, strike_interval, default_lot_size
        )
        if not straddle:
            logging.error(f"[{inst_key.upper()}] Could not find ATM straddle")
            self.telegram.alert_error(f"[{inst_key.upper()}] ATM straddle not found")
            return

        ce = straddle['ce']
        pe = straddle['pe']
        lot_size = straddle['lot_size']

        # Store straddle details for this instrument
        state = self.straddle_state[inst_key]
        state['ce_symbol'] = ce['symbol']
        state['ce_token'] = ce['token']
        state['pe_symbol'] = pe['symbol']
        state['pe_token'] = pe['token']
        state['lot_size'] = lot_size

        # Also add tokens to the mapping for WebSocket callbacks
        self.token_to_instrument[ce['token']] = (inst_key, 'ce')
        self.token_to_instrument[pe['token']] = (inst_key, 'pe')

        logging.info(f"[{inst_key.upper()}] Straddle: CE={ce['symbol']}, PE={pe['symbol']}, Strike={straddle['strike']}")

        # Subscribe to both options
        if self.ws_connected and self.ticker:
            self.ticker.subscribe([ce['token'], pe['token']])
            self.ticker.set_mode(self.ticker.MODE_LTP, [ce['token'], pe['token']])

        # Wait for option prices with retry
        ce_premium = None
        pe_premium = None
        for attempt in range(5):
            time.sleep(1)
            ce_premium = self.order_mgr.get_option_ltp(ce['symbol'])
            pe_premium = self.order_mgr.get_option_ltp(pe['symbol'])
            if ce_premium and ce_premium > 0 and pe_premium and pe_premium > 0:
                break
            logging.debug(f"[{inst_key.upper()}] Waiting for straddle prices, attempt {attempt + 1}/5 (CE={ce_premium}, PE={pe_premium})")

        if not ce_premium or ce_premium <= 0 or not pe_premium or pe_premium <= 0:
            logging.error(f"[{inst_key.upper()}] Could not get straddle prices after 5 attempts (CE={ce_premium}, PE={pe_premium})")
            self._cleanup_straddle(inst_key)
            return

        # Calculate combined straddle value and stop/target
        straddle_value = ce_premium + pe_premium
        state['entry_value'] = straddle_value
        qty = lot_size * self.config['risk']['max_lots']

        # Stop/target based on combined straddle value
        stop_loss = straddle_value * (1 - self.config['risk']['stop_loss_pct'])
        target = straddle_value * (1 + self.config['risk']['target_pct'])

        holding_mins = self.config['strategy']['holding_minutes']
        exit_deadline = (datetime.now() + timedelta(minutes=holding_mins)).isoformat()
        entry_time = datetime.now().isoformat()

        # Generate trade_group_id to link CE and PE legs
        trade_group_id = f"{inst_key}_{str(uuid.uuid4())[:8]}"  # Include inst_key for clarity
        state['trade_group_id'] = trade_group_id

        # Alert signal
        self.telegram.alert_signal(z_score, basis, fut_used, spot)

        # Place both orders in PARALLEL for minimal leg risk
        # Use exchange from straddle (NFO for NIFTY, BFO for SENSEX)
        exchange = straddle.get('exchange', 'NFO')
        ce_success, pe_success, ce_fill, pe_fill, ce_order_id, pe_order_id = \
            self.order_mgr.place_straddle_entry_parallel(
                ce['symbol'], ce['token'],
                pe['symbol'], pe['token'],
                qty, ce_premium, pe_premium,
                exchange
            )

        # Handle failures
        if not ce_success and not pe_success:
            self.telegram.alert_error(f"[{inst_key.upper()}] Both legs failed: CE={ce['symbol']}, PE={pe['symbol']}")
            self._cleanup_straddle(inst_key)
            return

        if not ce_success:
            # PE succeeded but CE failed - exit PE
            self.telegram.alert_error(f"[{inst_key.upper()}] CE entry failed, exiting PE leg")
            exit_success, _, _ = self.order_mgr.place_exit_order(pe['symbol'], qty, pe_fill, exchange)
            if not exit_success:
                # Create orphan PE position
                logging.critical(f"[{inst_key.upper()}] ORPHANED POSITION: PE {pe['symbol']} is live, CE failed, PE exit also failed!")
                pe_position = DBPosition(
                    trade_date=date.today().isoformat(),
                    trade_group_id=trade_group_id,
                    symbol=pe['symbol'],
                    instrument_token=pe['token'],
                    qty=qty,
                    lot_size=lot_size,
                    entry_order_id=pe_order_id,
                    entry_price=pe_fill,
                    entry_time=entry_time,
                    entry_spot=spot,
                    entry_z_score=z_score,
                    entry_basis=basis,
                    fut_used=fut_used,
                    stop_loss=pe_fill * 0.8,
                    target=pe_fill * 1.2,
                    exit_deadline=(datetime.now() + timedelta(minutes=30)).isoformat(),
                    status="OPEN",
                    paper_trade=self.config['paper_trade']
                )
                self.db.create_position(pe_position)
                self.telegram.alert_error("CRITICAL: Orphaned PE position created - MANUAL INTERVENTION REQUIRED")
            self._cleanup_straddle(inst_key)
            return

        if not pe_success:
            # CE succeeded but PE failed - exit CE
            self.telegram.alert_error(f"[{inst_key.upper()}] PE entry failed, exiting CE leg")
            exit_success, _, _ = self.order_mgr.place_exit_order(ce['symbol'], qty, ce_fill, exchange)
            if not exit_success:
                # Create orphan CE position
                logging.critical(f"[{inst_key.upper()}] ORPHANED POSITION: CE {ce['symbol']} is live, PE failed, CE exit also failed!")
                ce_position = DBPosition(
                    trade_date=date.today().isoformat(),
                    trade_group_id=trade_group_id,
                    symbol=ce['symbol'],
                    instrument_token=ce['token'],
                    qty=qty,
                    lot_size=lot_size,
                    entry_order_id=ce_order_id,
                    entry_price=ce_fill,
                    entry_time=entry_time,
                    entry_spot=spot,
                    entry_z_score=z_score,
                    entry_basis=basis,
                    fut_used=fut_used,
                    stop_loss=ce_fill * 0.8,
                    target=ce_fill * 1.2,
                    exit_deadline=(datetime.now() + timedelta(minutes=30)).isoformat(),
                    status="OPEN",
                    paper_trade=self.config['paper_trade']
                )
                self.db.create_position(ce_position)
                self.telegram.alert_error("CRITICAL: Orphaned CE position created - MANUAL INTERVENTION REQUIRED")
            self._cleanup_straddle(inst_key)
            return

        # Both legs succeeded - create positions in DB
        ce_position = DBPosition(
            trade_date=date.today().isoformat(),
            trade_group_id=trade_group_id,
            symbol=ce['symbol'],
            instrument_token=ce['token'],
            qty=qty,
            lot_size=lot_size,
            entry_order_id=ce_order_id,
            entry_price=ce_fill,
            entry_time=entry_time,
            entry_spot=spot,
            entry_z_score=z_score,
            entry_basis=basis,
            fut_used=fut_used,
            stop_loss=stop_loss,  # Combined straddle stop
            target=target,  # Combined straddle target
            exit_deadline=exit_deadline,
            status="OPEN",
            paper_trade=self.config['paper_trade']
        )
        self.db.create_position(ce_position)

        # Create PE position in DB
        pe_position = DBPosition(
            trade_date=date.today().isoformat(),
            trade_group_id=trade_group_id,
            symbol=pe['symbol'],
            instrument_token=pe['token'],
            qty=qty,
            lot_size=lot_size,
            entry_order_id=pe_order_id,
            entry_price=pe_fill,
            entry_time=entry_time,
            entry_spot=spot,
            entry_z_score=z_score,
            entry_basis=basis,
            fut_used=fut_used,
            stop_loss=stop_loss,  # Combined straddle stop
            target=target,  # Combined straddle target
            exit_deadline=exit_deadline,
            status="OPEN",
            paper_trade=self.config['paper_trade']
        )
        self.db.create_position(pe_position)

        # Update combined entry value
        state['entry_value'] = ce_fill + pe_fill

        # Alert (stop_loss not shown - disabled for straddles)
        self.telegram.alert_straddle_entry(
            ce['symbol'], pe['symbol'], qty, ce_fill, pe_fill,
            target, self.config['paper_trade']
        )

        logging.info(f"[{inst_key.upper()}] Straddle entry: CE@{ce_fill:.2f} + PE@{pe_fill:.2f} = {state['entry_value']:.2f}")

    def _cleanup_straddle(self, inst_key: str = 'nifty'):
        """Cleanup straddle tracking on failure for specific instrument"""
        state = self.straddle_state.get(inst_key, {})

        if self.ws_connected and self.ticker:
            tokens = []
            if state.get('ce_token'):
                tokens.append(state['ce_token'])
            if state.get('pe_token'):
                tokens.append(state['pe_token'])
            if tokens:
                try:
                    self.ticker.unsubscribe(tokens)
                except Exception:
                    pass

        # Clear state for this instrument
        if inst_key in self.straddle_state:
            self.straddle_state[inst_key]['ce_token'] = None
            self.straddle_state[inst_key]['ce_symbol'] = None
            self.straddle_state[inst_key]['pe_token'] = None
            self.straddle_state[inst_key]['pe_symbol'] = None
            self.straddle_state[inst_key]['entry_value'] = 0.0
            self.straddle_state[inst_key]['trade_group_id'] = ''

        # Clear prices for this instrument
        if inst_key in self.prices:
            self.prices[inst_key]['ce'] = 0.0
            self.prices[inst_key]['pe'] = 0.0

    def process_straddle_exit(self, positions: list, reason: str, ce_price: float, pe_price: float, inst_key: str = 'nifty'):
        """Process straddle exit using smart bid/ask optimization with cross-leg acceleration"""

        if len(positions) != 2:
            logging.error(f"[{inst_key.upper()}] Expected 2 positions for straddle exit, got {len(positions)}")
            return

        # Identify CE and PE positions
        ce_pos = None
        pe_pos = None
        for pos in positions:
            if 'CE' in pos.symbol:
                ce_pos = pos
            elif 'PE' in pos.symbol:
                pe_pos = pos

        if not ce_pos or not pe_pos:
            logging.error(f"[{inst_key.upper()}] Could not identify CE and PE positions")
            return

        # Determine exchange from instrument data (NFO for NIFTY, BFO for SENSEX)
        exchange = self.instrument_data.get(inst_key, {}).get('exchange', 'NFO')

        # Use smart exit with bid/ask optimization and cross-leg acceleration
        # This handles parallel execution, limit orders at mid-price, and market fallback
        logging.info(f"[{inst_key.upper()}] Starting smart straddle exit: CE={ce_pos.symbol}, PE={pe_pos.symbol}")

        ce_success, pe_success, ce_fill, pe_fill, ce_order_id, pe_order_id = \
            self.order_mgr.exit_straddle_smart(
                ce_symbol=ce_pos.symbol,
                pe_symbol=pe_pos.symbol,
                qty=ce_pos.qty,
                ce_expected_price=ce_price,
                pe_expected_price=pe_price,
                exchange=exchange
            )

        # If smart exit failed completely, try fallback with simple market orders
        if not ce_success and not pe_success:
            logging.warning(f"[{inst_key.upper()}] Smart exit failed for both legs, attempting market fallback")
            ce_success, ce_fill, ce_order_id = self.order_mgr.place_exit_order(
                ce_pos.symbol, ce_pos.qty, ce_price, exchange
            )
            pe_success, pe_fill, pe_order_id = self.order_mgr.place_exit_order(
                pe_pos.symbol, pe_pos.qty, pe_price, exchange
            )

        # Get spot price for this instrument
        spot_price = self.prices.get(inst_key, {}).get('spot', 0)

        # Handle failures
        if not ce_success:
            self.telegram.alert_error(f"[{inst_key.upper()}] CE exit failed for {ce_pos.symbol}")
            self.db.mark_position_error(ce_pos.id, f"EXIT_FAILED_{reason}")

        if not pe_success:
            self.telegram.alert_error(f"[{inst_key.upper()}] PE exit failed for {pe_pos.symbol}")
            self.db.mark_position_error(pe_pos.id, f"EXIT_FAILED_{reason}")

        # Handle partial success - close successful leg in DB even if other failed
        if ce_success and not pe_success:
            self.db.close_position(ce_pos.id, ce_fill, spot_price, reason, ce_order_id)
            logging.critical(f"[{inst_key.upper()}] CE exit succeeded but PE failed - CE closed, PE needs manual intervention")
            self._cleanup_straddle(inst_key)
            return

        if pe_success and not ce_success:
            self.db.close_position(pe_pos.id, pe_fill, spot_price, reason, pe_order_id)
            logging.critical(f"[{inst_key.upper()}] PE exit succeeded but CE failed - PE closed, CE needs manual intervention")
            self._cleanup_straddle(inst_key)
            return

        if not ce_success and not pe_success:
            logging.critical(f"[{inst_key.upper()}] Both exits failed - MANUAL INTERVENTION REQUIRED")
            self._cleanup_straddle(inst_key)
            return

        # Both succeeded - close positions in DB
        self.db.close_position(ce_pos.id, ce_fill, spot_price, reason, ce_order_id)
        self.db.close_position(pe_pos.id, pe_fill, spot_price, reason, pe_order_id)

        # Calculate combined P&L
        ce_pnl = (ce_fill - ce_pos.entry_price) * ce_pos.qty
        pe_pnl = (pe_fill - pe_pos.entry_price) * pe_pos.qty
        total_pnl = ce_pnl + pe_pnl

        # Check daily loss for this instrument
        underlying = self.instrument_data.get(inst_key, {}).get('underlying', '')
        stats = self._get_instrument_stats(underlying)
        if stats.get('gross_pnl', 0) <= -self.config['risk']['max_daily_loss']:
            self.telegram.alert_error(f"[{inst_key.upper()}] Daily loss limit hit! Trading stopped.")

        # Alert
        self.telegram.alert_straddle_exit(
            ce_pos.symbol, pe_pos.symbol,
            ce_pos.entry_price, ce_fill,
            pe_pos.entry_price, pe_fill,
            total_pnl, reason, self.config['paper_trade']
        )

        # Cleanup
        self._cleanup_straddle(inst_key)

        logging.info(f"[{inst_key.upper()}] Straddle exit: CE P&L=₹{ce_pnl:+.2f}, PE P&L=₹{pe_pnl:+.2f}, Total=₹{total_pnl:+.2f}, Reason={reason}")

    def process_exit(self, db_pos: DBPosition, reason: str, current_premium: float):
        """Process exit for single/orphan position using database with retry logic"""

        # Determine instrument key and exchange from symbol
        inst_key = None
        exchange = "NFO"  # Default to NFO
        for key, data in self.instrument_data.items():
            if db_pos.symbol.startswith(data['underlying']):
                inst_key = key
                exchange = data.get('exchange', 'NFO')
                break

        # Place exit order with retry
        max_retries = 2
        success = False
        fill_price = 0.0
        order_id = ""

        for attempt in range(max_retries):
            success, fill_price, order_id = self.order_mgr.place_exit_order(
                db_pos.symbol, db_pos.qty, current_premium, exchange
            )
            if success:
                break
            logging.warning(f"Exit order attempt {attempt + 1} failed, {'retrying...' if attempt < max_retries - 1 else 'giving up'}")
            if attempt < max_retries - 1:
                time.sleep(2)  # Wait before retry

        if not success:
            error_msg = f"Exit order failed for {db_pos.symbol} after {max_retries} attempts - MANUAL INTERVENTION REQUIRED"
            self.telegram.alert_error(error_msg)
            logging.critical(error_msg)
            # Mark position as error so we don't keep retrying
            self.db.mark_position_error(db_pos.id, f"EXIT_FAILED_{reason}")
            return

        # Get spot price for this instrument
        spot_price = self.prices.get(inst_key, {}).get('spot', 0) if inst_key else 0

        # Close position in DB (calculates P&L internally)
        self.db.close_position(
            position_id=db_pos.id,
            exit_price=fill_price,
            exit_spot=spot_price,
            exit_reason=reason,
            exit_order_id=order_id
        )

        # Calculate P&L for alert
        pnl = (fill_price - db_pos.entry_price) * db_pos.qty
        pnl_pct = (fill_price - db_pos.entry_price) / db_pos.entry_price * 100 if db_pos.entry_price > 0 else 0

        # Check daily loss
        stats = self.db.get_today_stats()
        if stats.get('gross_pnl', 0) <= -self.config['risk']['max_daily_loss']:
            self.telegram.alert_error("Daily loss limit hit! Trading stopped.")

        # Alert
        self.telegram.alert_exit(
            db_pos.symbol, db_pos.entry_price, fill_price, pnl, reason,
            self.config['paper_trade']
        )

        # Cleanup straddle state for this instrument
        if inst_key:
            state = self.straddle_state.get(inst_key, {})
            token_to_unsubscribe = None

            if 'CE' in db_pos.symbol and state.get('ce_token'):
                token_to_unsubscribe = state['ce_token']
                state['ce_token'] = None
                state['ce_symbol'] = None
            elif 'PE' in db_pos.symbol and state.get('pe_token'):
                token_to_unsubscribe = state['pe_token']
                state['pe_token'] = None
                state['pe_symbol'] = None

            if token_to_unsubscribe and self.ws_connected and self.ticker:
                try:
                    self.ticker.unsubscribe([token_to_unsubscribe])
                except Exception:
                    pass  # Ignore unsubscribe errors

        logging.info(f"Position closed: {db_pos.symbol}, P&L: ₹{pnl:+.2f} ({pnl_pct:+.1f}%), Reason: {reason}")

    def is_trading_time(self) -> bool:
        """Check if current time is within trading hours"""
        now = datetime.now()
        start = datetime.strptime(self.config['trading_hours']['start'], "%H:%M").time()
        end = datetime.strptime(self.config['trading_hours']['end'], "%H:%M").time()
        return start <= now.time() <= end

    def send_daily_summary(self):
        """Send daily summary to Telegram and save to DB"""
        if self.daily_summary_sent:
            return

        stats = self.db.get_today_stats()
        trades = stats.get('total_trades', 0)
        wins = stats.get('wins', 0)
        losses = stats.get('losses', 0)
        gross_pnl = stats.get('gross_pnl', 0)

        # Estimate charges
        num_lots = trades  # Assuming 1 lot per trade
        charges = num_lots * CHARGES_PER_LOT
        net_pnl = gross_pnl - charges

        # Save to DB
        summary = DailySummary(
            trade_date=date.today().isoformat(),
            total_trades=trades,
            wins=wins,
            losses=losses,
            gross_pnl=gross_pnl,
            charges=charges,
            net_pnl=net_pnl,
            paper_trade=self.config['paper_trade']
        )
        self.db.save_daily_summary(summary)

        # Send Telegram
        mode = "PAPER" if self.config['paper_trade'] else "LIVE"
        win_rate = (wins / trades * 100) if trades > 0 else 0

        msg = f"""
📊 <b>DAILY SUMMARY - {mode}</b>
━━━━━━━━━━━━━━━━━━━
Date: <code>{date.today().isoformat()}</code>
Trades: <code>{trades}</code>
Wins/Losses: <code>{wins}/{losses}</code>
Win Rate: <code>{win_rate:.1f}%</code>
━━━━━━━━━━━━━━━━━━━
Gross P&L: <code>₹{gross_pnl:+,.2f}</code>
Charges: <code>₹{charges:,.2f}</code>
<b>Net P&L: <code>₹{net_pnl:+,.2f}</code></b>
"""
        self.telegram.send(msg)
        self.daily_summary_sent = True
        logging.info(f"Daily summary sent: {trades} trades, Net P&L: ₹{net_pnl:+,.2f}")

    def main_loop(self):
        """Main trading loop - processes all enabled instruments"""
        logging.info("Starting main loop...")
        last_log = {}  # Per-instrument last log time
        last_ws_check = time.time()
        ws_reconnect_attempts = 0

        # Initialize last_log for each instrument
        for inst_key in self.instrument_data:
            last_log[inst_key] = 0

        while self.running:
            try:
                now = datetime.now()

                # Send daily summary at 15:20
                if now.hour == 15 and now.minute >= 20 and not self.daily_summary_sent:
                    self.send_daily_summary()

                # Check WebSocket connection periodically (every 30 seconds)
                if time.time() - last_ws_check > 30:
                    last_ws_check = time.time()
                    if not self.ws_connected:
                        ws_reconnect_attempts += 1
                        if ws_reconnect_attempts <= 5:
                            logging.warning(f"WebSocket disconnected, attempting reconnect ({ws_reconnect_attempts}/5)")
                            self.reconnect_websocket()
                        else:
                            logging.error("WebSocket reconnection failed 5 times, continuing with REST API")
                    else:
                        ws_reconnect_attempts = 0  # Reset on successful connection

                # Process each enabled instrument
                for inst_key, inst_data in self.instrument_data.items():
                    self._process_instrument(inst_key, inst_data, last_log)

                # Small sleep
                time.sleep(0.5)

            except Exception as e:
                self.consecutive_errors += 1
                logging.error(f"Error in main loop ({self.consecutive_errors}): {e}")

                if self.consecutive_errors >= self.max_consecutive_errors:
                    self.telegram.alert_error(f"Too many errors ({self.consecutive_errors}), stopping bot")
                    self.running = False
                    break

                self.telegram.alert_error(str(e))
                time.sleep(5)

    def _process_instrument(self, inst_key: str, inst_data: Dict, last_log: Dict):
        """Process a single instrument - z-score update, entry/exit checks"""
        underlying = inst_data['underlying']
        prices = self.prices.get(inst_key, {})

        # Check if we have prices for this instrument
        spot_price = prices.get('spot', 0)
        current_fut_price = prices.get('current_fut', 0)

        if spot_price == 0 or current_fut_price == 0:
            return  # Skip this instrument until we have prices

        next_fut_price = prices.get('next_fut', 0) or current_fut_price

        # Calculate z-score using instrument-specific SignalEngine
        signal_engine = self.signal_engines.get(inst_key)
        if not signal_engine:
            return

        z_score, basis, fut_used, basis_pct = signal_engine.update(
            spot_price,
            current_fut_price,
            next_fut_price,
            self.config['strategy']['min_basis_current']
        )

        # Log periodically (per instrument)
        if time.time() - last_log.get(inst_key, 0) > 60:
            logging.info(f"[{inst_key.upper()}] Spot: {spot_price:.2f}, Basis: {basis:.1f}, "
                        f"Z: {z_score:.2f}, Fut: {fut_used}")
            last_log[inst_key] = time.time()
            self.consecutive_errors = 0  # Reset error count on successful tick

        # Get open positions for this specific instrument
        open_positions = self._get_positions_for_instrument(inst_key, underlying)

        # Check for exit first (if in position)
        if open_positions:
            self._check_exit_conditions(inst_key, open_positions, z_score, prices)
        else:
            # Check for entry
            self._check_entry_for_instrument(inst_key, inst_data, z_score, basis, fut_used, spot_price)

    def _get_positions_for_instrument(self, inst_key: str, underlying: str) -> list:
        """Get open positions for a specific instrument"""
        all_positions = self.db.get_all_open_positions()
        # Filter positions by symbol prefix (NIFTY vs SENSEX)
        return [p for p in all_positions if p.symbol.startswith(underlying)]

    def _check_exit_conditions(self, inst_key: str, open_positions: list, z_score: float, prices: Dict):
        """Check exit conditions for an instrument's positions"""
        state = self.straddle_state.get(inst_key, {})
        signal_engine = self.signal_engines.get(inst_key)

        # Straddle mode - check combined value
        if len(open_positions) == 2:
            ce_price = prices.get('ce', 0)
            pe_price = prices.get('pe', 0)

            # Try REST API fallback if prices are stale
            if ce_price <= 0 and state.get('ce_symbol'):
                ce_price = self.order_mgr.get_option_ltp(state['ce_symbol']) or 0
                prices['ce'] = ce_price
            if pe_price <= 0 and state.get('pe_symbol'):
                pe_price = self.order_mgr.get_option_ltp(state['pe_symbol']) or 0
                prices['pe'] = pe_price

            if ce_price > 0 and pe_price > 0:
                current_straddle_value = ce_price + pe_price
                first_pos = open_positions[0]

                # Validate entry_value - if 0 or missing, reconstruct from DB positions
                entry_value = state.get('entry_value', 0)
                if entry_value <= 0:
                    # Reconstruct entry_value from DB positions
                    entry_value = sum(p.entry_price for p in open_positions)
                    if entry_value > 0:
                        state['entry_value'] = entry_value
                        logging.info(f"[{inst_key.upper()}] Reconstructed entry_value from DB: {entry_value:.2f}")
                    else:
                        logging.error(f"[{inst_key.upper()}] Cannot determine straddle entry value")
                        return

                pos = Position(
                    active=True,
                    symbol="STRADDLE",
                    entry_price=entry_value,
                    stop_loss=first_pos.stop_loss,
                    target=first_pos.target,
                    exit_deadline=first_pos.exit_deadline
                )
                should_exit, reason = signal_engine.should_exit(
                    pos, current_straddle_value, z_score
                )
                if should_exit:
                    self.process_straddle_exit(open_positions, reason, ce_price, pe_price, inst_key)
            else:
                logging.warning(f"[{inst_key.upper()}] Straddle prices stale: CE={ce_price}, PE={pe_price}")

        # Single position (orphan from partial straddle failure)
        elif len(open_positions) == 1:
            open_position = open_positions[0]
            if 'CE' in open_position.symbol:
                current_premium = prices.get('ce', 0)
            elif 'PE' in open_position.symbol:
                current_premium = prices.get('pe', 0)
            else:
                current_premium = 0

            if current_premium <= 0:
                rest_price = self.order_mgr.get_option_ltp(open_position.symbol)
                if rest_price:
                    current_premium = rest_price

            if current_premium > 0:
                pos = Position(
                    active=True,
                    symbol=open_position.symbol,
                    entry_price=open_position.entry_price,
                    stop_loss=open_position.stop_loss,
                    target=open_position.target,
                    exit_deadline=open_position.exit_deadline
                )
                should_exit, reason = signal_engine.should_exit(
                    pos, current_premium, z_score
                )
                if should_exit:
                    self.process_exit(open_position, reason, current_premium)
            else:
                logging.warning(f"[{inst_key.upper()}] No option price for {open_position.symbol}")

    def _check_entry_for_instrument(self, inst_key: str, inst_data: Dict,
                                     z_score: float, basis: float, fut_used: str, spot_price: float):
        """Check entry conditions for a specific instrument"""
        underlying = inst_data['underlying']

        # Get today's stats for this instrument
        stats = self._get_instrument_stats(underlying)

        should_enter, msg = self._check_entry_conditions(
            z_score, basis, fut_used, stats, inst_key
        )
        if should_enter:
            logging.info(f"[{inst_key.upper()}] Entry signal! Z={z_score:.2f}, Basis={basis:.1f}")
            self.process_entry(z_score, basis, fut_used, spot_price, inst_key)

    def _get_instrument_stats(self, underlying: str) -> Dict:
        """Get today's trading stats filtered by instrument underlying"""
        # Get positions for this specific instrument
        # Note: For production, consider adding instrument column to DB for efficient filtering
        all_positions = self.db.get_today_positions()
        inst_positions = [p for p in all_positions if p.symbol.startswith(underlying)]

        return {
            'total_trades': len([p for p in inst_positions if p.status == 'CLOSED']) // 2,  # Straddle = 2 positions
            'gross_pnl': sum(p.pnl or 0 for p in inst_positions if p.status == 'CLOSED'),
        }

    def _check_entry_conditions(self, z_score: float, basis: float,
                                 fut_used: str, stats: Dict,
                                 inst_key: str = None) -> Tuple[bool, str]:
        """Check if entry conditions are met using DB stats (per instrument)"""
        config = self.config
        max_trades = config['risk']['max_trades_per_day']
        max_loss = config['risk']['max_daily_loss']

        # Per-instrument trade limits
        if stats.get('total_trades', 0) >= max_trades:
            return False, f"Max trades reached for {inst_key or 'instrument'}"

        if stats.get('gross_pnl', 0) <= -max_loss:
            return False, f"Daily loss limit hit for {inst_key or 'instrument'}"

        # Time check
        now = datetime.now()
        if now.hour not in config['strategy']['valid_hours']:
            return False, f"Outside trading hours ({now.hour})"

        # Z-score threshold
        if fut_used == "CURRENT":
            threshold = config['strategy']['z_threshold']
        else:
            threshold = config['strategy']['z_threshold_next_month']

        if z_score < threshold:
            return False, f"Z-score {z_score:.2f} < {threshold}"

        # Basis check
        if basis < config['strategy']['min_basis_current']:
            return False, f"Basis {basis:.1f} < {config['strategy']['min_basis_current']}"

        return True, "All conditions met"

    def run(self):
        """Run the bot"""
        logging.info("=" * 60)
        logging.info("Z-SCORE TRADING BOT STARTING")
        logging.info(f"Paper Mode: {self.config['paper_trade']}")
        logging.info(f"Data Dir: {self.data_dir}")
        logging.info(f"Enabled Instruments: {list(self.instrument_data.keys())}")
        for inst_key, inst_data in self.instrument_data.items():
            logging.info(f"  {inst_key.upper()}: {inst_data['underlying']} "
                        f"(spot={inst_data['spot_symbol']}, "
                        f"fut={inst_data['current_fut']['symbol']})")
        logging.info("=" * 60)

        # Get available capital for startup alert
        capital = 0.0
        if self.config['paper_trade']:
            capital = 100000.0  # Simulated capital for paper mode
        else:
            try:
                margins = self.kite.margins()
                capital = margins.get('equity', {}).get('available', {}).get('live_balance', 0)
            except Exception as e:
                logging.warning(f"Could not fetch margins: {e}")

        # Startup alert
        self.telegram.alert_startup(self.config['paper_trade'], capital)

        # Start WebSocket
        self.start_websocket()

        # Check for existing position
        self.check_and_recover_position()

        # Setup signal handlers for graceful shutdown
        def handle_shutdown(signum, frame):
            sig_name = signal.Signals(signum).name
            logging.info(f"Received {sig_name}, initiating graceful shutdown...")
            self.running = False

        signal.signal(signal.SIGTERM, handle_shutdown)
        signal.signal(signal.SIGINT, handle_shutdown)

        # Start main loop
        self.running = True

        try:
            self.main_loop()
        except KeyboardInterrupt:
            logging.info("Shutting down (KeyboardInterrupt)...")
        finally:
            self.running = False

            # Close WebSocket connection
            if self.ticker:
                try:
                    self.ticker.close()
                    logging.info("WebSocket closed")
                except Exception as e:
                    logging.warning(f"Error closing WebSocket: {e}")

            # Send daily summary from DB
            if not self.daily_summary_sent:
                self.send_daily_summary()

            logging.info("Bot stopped")

# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    """Entry point"""
    import argparse

    parser = argparse.ArgumentParser(description='Z-Score Trading Bot')
    parser.add_argument('--config', default=CONFIG_FILE, help='Config file path')
    parser.add_argument('--paper', action='store_true', help='Force paper trade mode')
    args = parser.parse_args()

    # Create bot
    bot = ZScoreBot(args.config)

    # Override paper mode if specified
    if args.paper:
        bot.config['paper_trade'] = True

    # Run
    bot.run()

if __name__ == "__main__":
    main()
