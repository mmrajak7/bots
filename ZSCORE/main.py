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
from datetime import datetime, timedelta, date
from collections import deque
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass

# Kite Connect
from kiteconnect import KiteConnect, KiteTicker

# Local imports
from db import TradingDB, Order, Position as DBPosition, DailySummary, BOT_ID

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
    """Get next trading day"""
    check_date = date.today() + timedelta(days=1)
    holidays = holidays or set()
    while True:
        if check_date.weekday() < 5 and check_date.isoformat() not in holidays:
            return check_date
        check_date += timedelta(days=1)

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
    """Manage instrument lookups from cached CSV"""

    def __init__(self, kite: KiteConnect, data_dir: str, instruments_path: str = None):
        self.kite = kite
        self.data_dir = data_dir
        self.external_instruments = instruments_path is not None
        self.instruments_file = instruments_path if instruments_path else os.path.join(data_dir, "nfo_instruments.csv")
        self.instruments = {}  # symbol -> {token, expiry, strike, etc.}
        self._load_or_refresh()

    def _load_or_refresh(self):
        """Load instruments from cache or refresh from API"""
        # If using external instruments file (e.g., from SNAIL), just load it
        if self.external_instruments:
            if os.path.exists(self.instruments_file):
                logging.info(f"Loading instruments from external file: {self.instruments_file}")
                self._load_from_csv()
            else:
                raise FileNotFoundError(f"External instruments file not found: {self.instruments_file}")
            return

        today = datetime.now().strftime("%Y-%m-%d")

        # Check if we have today's instruments
        if os.path.exists(self.instruments_file):
            try:
                with open(self.instruments_file, 'r') as f:
                    reader = csv.DictReader(f)
                    first_row = next(reader, None)
                    # Check file has data and is from today
                    if first_row is not None and first_row.get('fetch_date') == today:
                        logging.info(f"Loading cached instruments from {self.instruments_file}")
                        self._load_from_csv()
                        return
                    elif first_row is None:
                        logging.warning("Instruments file is empty, refreshing from API")
            except Exception as e:
                logging.warning(f"Error reading instruments cache: {e}")

        # Fetch fresh from API
        self._refresh_from_api()

    def _refresh_from_api(self):
        """Fetch instruments from Kite API and cache"""
        logging.info("Fetching NFO instruments from Kite API...")
        try:
            nfo = self.kite.instruments("NFO")
            nse = self.kite.instruments("NSE")

            today = datetime.now().strftime("%Y-%m-%d")
            os.makedirs(self.data_dir, exist_ok=True)

            # Save to CSV
            with open(self.instruments_file, 'w', newline='') as f:
                fieldnames = ['fetch_date', 'instrument_token', 'tradingsymbol', 'name',
                              'exchange', 'instrument_type', 'expiry', 'strike', 'lot_size']
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()

                # Write NFO instruments
                for inst in nfo:
                    writer.writerow({
                        'fetch_date': today,
                        'instrument_token': inst['instrument_token'],
                        'tradingsymbol': inst['tradingsymbol'],
                        'name': inst.get('name', ''),
                        'exchange': 'NFO',
                        'instrument_type': inst.get('instrument_type', ''),
                        'expiry': inst.get('expiry', ''),
                        'strike': inst.get('strike', ''),
                        'lot_size': inst.get('lot_size', '')
                    })

                # Write NSE spot indices (NIFTY 50, NIFTY BANK, etc.)
                for inst in nse:
                    symbol = inst['tradingsymbol']
                    # Include NIFTY 50, NIFTY BANK and similar indices
                    if 'NIFTY' in symbol:
                        writer.writerow({
                            'fetch_date': today,
                            'instrument_token': inst['instrument_token'],
                            'tradingsymbol': symbol,
                            'name': inst.get('name', ''),
                            'exchange': 'NSE',
                            'instrument_type': inst.get('instrument_type', ''),
                            'expiry': '',
                            'strike': '',
                            'lot_size': ''
                        })

            logging.info(f"Saved {len(nfo) + len(nse)} instruments to {self.instruments_file}")
            self._load_from_csv()

        except Exception as e:
            logging.error(f"Failed to fetch instruments: {e}")
            raise

    def _load_from_csv(self):
        """Load instruments from CSV into memory"""
        self.instruments = {}
        skipped = 0
        with open(self.instruments_file, 'r') as f:
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
                        'lot_size': row.get('lot_size', '')
                    }
                except (ValueError, KeyError) as e:
                    skipped += 1
                    continue
        if skipped > 0:
            logging.warning(f"Skipped {skipped} invalid instrument rows")
        logging.info(f"Loaded {len(self.instruments)} instruments into memory")

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

    def get_spot_token(self, symbol: str) -> Optional[int]:
        """Get spot/index token"""
        return self.get_token(symbol)

    def get_futures_token(self, symbol: str) -> Optional[int]:
        """Get futures token"""
        return self.get_token(symbol)

    def find_nifty_futures(self, underlying: str = "NIFTY") -> Tuple[Optional[Dict], Optional[Dict]]:
        """
        Auto-detect current and next month futures from instruments.
        Returns (current_month_fut, next_month_fut) dicts with symbol, token, expiry, lot_size.
        """
        today = datetime.now().date()
        futures = []

        for symbol, data in self.instruments.items():
            # Match NIFTY futures (e.g., NIFTY25DECFUT, NIFTY25JANFUT)
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
                    lot_size = int(data['lot_size']) if data['lot_size'] else 75
                    # Only consider futures that haven't expired
                    if exp_date >= today:
                        futures.append({
                            'symbol': symbol,
                            'token': data['token'],
                            'expiry': exp_date,
                            'lot_size': lot_size
                        })
            except Exception:
                continue

        # Sort by expiry date
        futures.sort(key=lambda x: x['expiry'])

        if len(futures) >= 2:
            current = futures[0]
            next_month = futures[1]
            logging.info(f"Auto-detected futures - Current: {current['symbol']} (exp: {current['expiry']}, lot: {current['lot_size']}), "
                        f"Next: {next_month['symbol']} (exp: {next_month['expiry']})")
            return current, next_month
        elif len(futures) == 1:
            logging.warning(f"Only one future found: {futures[0]['symbol']}")
            return futures[0], None
        else:
            logging.error(f"No {underlying} futures found in instruments!")
            return None, None

    def find_atm_option(self, spot_price: float, option_type: str = "CE",
                        min_dte: int = 3) -> Optional[Dict]:
        """Find ATM option for weekly expiry with minimum DTE"""
        atm_strike = round(spot_price / 50) * 50
        now = datetime.now()
        today = now.date()

        # Find next Thursday (weekly expiry)
        days_until_thursday = (3 - now.weekday()) % 7
        if days_until_thursday == 0 and now.hour >= 15:
            days_until_thursday = 7
        expiry_date = (now + timedelta(days=days_until_thursday)).date()

        # Check DTE - if too close to expiry, use next week
        dte = (expiry_date - today).days
        if dte < min_dte:
            expiry_date = expiry_date + timedelta(days=7)
            logging.info(f"Current expiry DTE={dte} < {min_dte}, using next week: {expiry_date}")

        # Search for matching option
        candidates = []
        for symbol, data in self.instruments.items():
            if not symbol.startswith('NIFTY'):
                continue
            if not symbol.endswith(option_type):
                continue
            if data['type'] not in ['CE', 'PE']:
                continue

            try:
                # Parse expiry
                exp_str = data['expiry']
                if exp_str:
                    exp_date = datetime.strptime(exp_str[:10], '%Y-%m-%d').date()
                    strike = float(data['strike']) if data['strike'] else 0

                    if exp_date == expiry_date and strike == atm_strike:
                        option_dte = (exp_date - today).days
                        lot_size = int(data['lot_size']) if data['lot_size'] else 75
                        candidates.append({
                            'symbol': symbol,
                            'token': data['token'],
                            'strike': strike,
                            'expiry': exp_date,
                            'dte': option_dte,
                            'lot_size': lot_size
                        })
            except Exception:
                continue

        if candidates:
            result = candidates[0]
            logging.info(f"Found ATM option: {result['symbol']} (strike={result['strike']}, "
                        f"expiry={result['expiry']}, DTE={result['dte']})")
            return result

        # Log available options for debugging
        logging.warning(f"No ATM option found for strike={atm_strike}, expiry={expiry_date}")
        sample = [s for s in self.instruments.keys() if s.startswith('NIFTY') and 'CE' in s][:5]
        logging.warning(f"Sample NIFTY options: {sample}")
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

    def alert_startup(self, paper_mode: bool):
        mode = "PAPER" if paper_mode else "LIVE"
        msg = f"""
🚀 <b>Z-Score Bot Started</b>
━━━━━━━━━━━━━━━━━━━
Mode: <code>{mode}</code>
Version: <code>{VERSION}</code>
Time: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>
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
        self.last_minute = None

    def update(self, spot: float, current_fut: float, next_fut: float,
               min_basis_current: float) -> Tuple[float, float, str, float]:
        """
        Update with new prices and return (z_score, basis, fut_used, basis_pct)
        """
        # Guard against division by zero (before first tick)
        if spot <= 0:
            return 0.0, 0.0, "CURRENT", 0.0

        # Determine which futures to use
        current_basis = current_fut - spot
        next_basis = next_fut - spot

        if current_basis >= min_basis_current:
            active_basis = current_basis
            fut_used = "CURRENT"
        else:
            active_basis = next_basis
            fut_used = "NEXT"

        # Calculate basis percentage
        basis_pct = (active_basis / spot) * 100

        # Update buffer (once per minute)
        current_minute = datetime.now().strftime("%H:%M")
        if current_minute != self.last_minute:
            self.basis_buffer.append(basis_pct)
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

    def should_exit(self, position: Position, current_premium: float,
                    z_score: float) -> Tuple[bool, str]:
        """Check if exit conditions are met"""

        if not position.active:
            return False, ""

        now = datetime.now()

        # Time-based exit (with validation)
        if position.exit_deadline:
            try:
                deadline = datetime.fromisoformat(position.exit_deadline)
                if now >= deadline:
                    return True, "TIME"
            except ValueError:
                logging.warning(f"Invalid exit_deadline format: {position.exit_deadline}")

        # Target hit
        if current_premium >= position.target:
            return True, "TARGET"

        # Stop loss hit
        if current_premium <= position.stop_loss:
            return True, "STOP_LOSS"

        # Z-score reversion (optional)
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

    def get_atm_option(self, spot: float) -> Optional[Dict]:
        """Get ATM CE option details with DTE filter"""
        min_dte = self.config.get('instruments', {}).get('min_dte', 3)
        return self.inst_mgr.find_atm_option(spot, "CE", min_dte)

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
                          current_price: float) -> Tuple[bool, float, str]:
        """Place entry order with DB tracking. Returns (success, fill_price, order_id)"""

        # Create order record in DB first
        order = Order(
            order_type="ENTRY",
            symbol=symbol,
            qty=qty,
            side="BUY",
            order_status="PENDING",
            expected_price=current_price,
            paper_trade=self.paper_mode
        )

        if self.paper_mode:
            order_id = f"PAPER_{int(time.time())}"
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

            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NFO,
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
                         current_price: float) -> Tuple[bool, float, str]:
        """Place exit order with DB tracking. Returns (success, fill_price, order_id)"""

        order = Order(
            order_type="EXIT",
            symbol=symbol,
            qty=qty,
            side="SELL",
            order_status="PENDING",
            expected_price=current_price,
            paper_trade=self.paper_mode
        )

        if self.paper_mode:
            order_id = f"PAPER_EXIT_{int(time.time())}"
            order.order_id = order_id
            order.fill_price = current_price
            order.order_status = "COMPLETE"
            self.db.create_order(order)
            logging.info(f"[PAPER] SELL {qty} {symbol} @ {current_price}")
            return True, current_price, order_id

        order_created = False
        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NFO,
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

# =============================================================================
# MAIN TRADING BOT
# =============================================================================

class ZScoreBot:
    """Main trading bot"""

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

        # Initialize database
        db_path = os.path.join(self.data_dir, "zscore_trades.db")
        self.db = TradingDB(db_path)

        # Initialize instrument manager (fetches/caches instruments)
        instruments_path = self.config.get('market', {}).get('instruments_path')
        if instruments_path:
            instruments_path = resolve_path(instruments_path)
        self.inst_mgr = InstrumentManager(self.kite, self.data_dir, instruments_path)

        # Resolve instrument tokens from config symbols
        self._resolve_tokens()

        # Initialize other components
        self.telegram = TelegramAlerter(
            self.config['telegram']['bot_token'],
            self.config['telegram']['chat_id'],
            self.config['telegram']['enabled']
        )

        self.signal_engine = SignalEngine(self.config['strategy']['lookback_minutes'])
        self.order_mgr = OrderManager(self.kite, self.inst_mgr, self.db,
                                       self.config['paper_trade'], self.config)

        # Price storage
        self.prices = {
            'spot': 0.0,
            'current_fut': 0.0,
            'next_fut': 0.0,
            'option': 0.0
        }
        self.option_symbol = None
        self.option_token = None
        self.lot_size = 75  # Will be updated from instruments

        # WebSocket
        self.ticker = None
        self.ws_connected = False

        # Control
        self.running = False
        self.daily_summary_sent = False

        # Error tracking
        self.consecutive_errors = 0
        self.max_consecutive_errors = 10

    def _load_config(self, config_file: str) -> dict:
        """Load configuration from JSON file"""
        with open(config_file, 'r') as f:
            return json.load(f)

    def _resolve_tokens(self):
        """Resolve instrument tokens - auto-detect futures from instruments"""
        inst = self.config['instruments']

        # Get spot token - direct config takes priority, then lookup, then well-known defaults
        if inst.get('spot_token'):
            self.spot_token = inst['spot_token']
            logging.info(f"Using configured spot_token: {self.spot_token}")
        else:
            self.spot_token = self.inst_mgr.get_token(inst['spot_symbol'])
            if not self.spot_token:
                # Well-known defaults for common indices
                well_known = {
                    'NIFTY 50': 256265,
                    'NIFTY BANK': 260105,
                    'NIFTY': 256265,
                }
                self.spot_token = well_known.get(inst['spot_symbol'])
                if self.spot_token:
                    logging.info(f"Using well-known token for {inst['spot_symbol']}: {self.spot_token}")
                else:
                    raise ValueError(f"Could not find spot token for {inst['spot_symbol']}")

        # Auto-detect current and next month futures
        underlying = inst.get('underlying', 'NIFTY')
        current_fut, next_fut = self.inst_mgr.find_nifty_futures(underlying)

        if not current_fut:
            raise ValueError(f"Could not find current month {underlying} futures")

        self.current_fut_token = current_fut['token']
        self.current_fut_symbol = current_fut['symbol']
        self.current_fut_expiry = current_fut['expiry']

        if next_fut:
            self.next_fut_token = next_fut['token']
            self.next_fut_symbol = next_fut['symbol']
            self.next_fut_expiry = next_fut['expiry']
        else:
            # Fallback to current if only one future available
            self.next_fut_token = self.current_fut_token
            self.next_fut_symbol = self.current_fut_symbol
            self.next_fut_expiry = self.current_fut_expiry
            logging.warning("Only one futures contract available, using same for next month")

        logging.info(f"Resolved tokens - Spot: {self.spot_token}, "
                    f"Current Fut: {self.current_fut_symbol} ({self.current_fut_token}), "
                    f"Next Fut: {self.next_fut_symbol} ({self.next_fut_token})")

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
        """WebSocket tick callback"""
        for tick in ticks:
            token = tick['instrument_token']
            ltp = tick['last_price']

            if token == self.spot_token:
                self.prices['spot'] = ltp
            elif token == self.current_fut_token:
                self.prices['current_fut'] = ltp
            elif token == self.next_fut_token:
                self.prices['next_fut'] = ltp
            elif token == self.option_token:
                self.prices['option'] = ltp

    def _on_connect(self, ws, response):
        """WebSocket connect callback"""
        logging.info("WebSocket connected")
        self.ws_connected = True

        # Subscribe to instruments
        tokens = [self.spot_token, self.current_fut_token, self.next_fut_token]
        if self.option_token:
            tokens.append(self.option_token)

        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_LTP, tokens)
        logging.info(f"Subscribed to {len(tokens)} instruments")

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
        """Attempt to reconnect WebSocket"""
        if self.ticker:
            logging.info("Attempting WebSocket reconnection...")
            try:
                self.ticker.close()
            except Exception:
                pass
            time.sleep(2)
            self.start_websocket()

    def check_and_recover_position(self):
        """Check for existing position on startup from DB"""
        db_pos = self.db.get_open_position()
        if db_pos:
            logging.info(f"Found existing position in DB: {db_pos.symbol}")

            # Get current option token
            token = self.inst_mgr.get_token(db_pos.symbol)
            if token:
                self.option_token = token
                self.option_symbol = db_pos.symbol
                self.lot_size = db_pos.lot_size
                # Re-subscribe if WS is connected
                if self.ws_connected and self.ticker:
                    self.ticker.subscribe([token])
                    self.ticker.set_mode(self.ticker.MODE_LTP, [token])

                # Create Position dataclass for alert
                pos = Position(
                    active=True,
                    symbol=db_pos.symbol,
                    entry_price=db_pos.entry_price,
                    stop_loss=db_pos.stop_loss,
                    target=db_pos.target,
                    exit_deadline=db_pos.exit_deadline
                )
                self.telegram.alert_recovery(pos)
                return True
            else:
                logging.error(f"Could not find token for {db_pos.symbol}")
                # Don't auto-close - this needs manual intervention

        return False

    def process_entry(self, z_score: float, basis: float, fut_used: str, spot: float):
        """Process entry signal using database"""

        # Get ATM option (includes lot_size from instruments)
        option = self.order_mgr.get_atm_option(spot)
        if not option:
            logging.error("Could not find ATM option")
            self.telegram.alert_error("ATM option not found")
            return

        symbol = option['symbol']
        token = option['token']
        lot_size = option.get('lot_size', 75)  # Get from instruments file

        # Subscribe to option
        self.option_token = token
        self.option_symbol = symbol
        self.lot_size = lot_size
        if self.ws_connected and self.ticker:
            self.ticker.subscribe([token])
            self.ticker.set_mode(self.ticker.MODE_LTP, [token])

        # Wait for option price with retry
        premium = None
        for attempt in range(5):
            time.sleep(1)
            premium = self.order_mgr.get_option_ltp(symbol)
            if premium and premium > 0:
                break
            logging.debug(f"Waiting for option price, attempt {attempt + 1}/5")

        if not premium or premium <= 0:
            logging.error("Could not get option price after 5 attempts")
            # Cleanup on failure
            if self.ws_connected and self.ticker and self.option_token:
                try:
                    self.ticker.unsubscribe([self.option_token])
                except Exception:
                    pass
            self.option_token = None
            self.option_symbol = None
            return

        # Calculate stop/target - qty from lot_size * max_lots
        qty = lot_size * self.config['risk']['max_lots']
        stop_loss = premium * (1 - self.config['risk']['stop_loss_pct'])
        target = premium * (1 + self.config['risk']['target_pct'])

        holding_mins = self.config['strategy']['holding_minutes']
        exit_deadline = (datetime.now() + timedelta(minutes=holding_mins)).isoformat()

        # Alert signal
        self.telegram.alert_signal(z_score, basis, fut_used, spot)

        # Place order
        success, fill_price, order_id = self.order_mgr.place_entry_order(
            symbol, token, qty, premium
        )

        if not success:
            self.telegram.alert_error(f"Entry order failed for {symbol}")
            # Cleanup - unsubscribe and clear option tracking
            if self.ws_connected and self.ticker and self.option_token:
                try:
                    self.ticker.unsubscribe([self.option_token])
                except Exception:
                    pass
            self.option_token = None
            self.option_symbol = None
            return

        # Create position in DB
        db_position = DBPosition(
            trade_date=date.today().isoformat(),
            symbol=symbol,
            instrument_token=token,
            qty=qty,
            lot_size=lot_size,
            entry_order_id=order_id,
            entry_price=fill_price,
            entry_time=datetime.now().isoformat(),
            entry_spot=spot,
            entry_z_score=z_score,
            entry_basis=basis,
            fut_used=fut_used,
            stop_loss=stop_loss,
            target=target,
            exit_deadline=exit_deadline,
            status="OPEN",
            paper_trade=self.config['paper_trade']
        )
        self.db.create_position(db_position)

        # Alert
        self.telegram.alert_entry(
            symbol, qty, fill_price, stop_loss, target,
            self.config['paper_trade']
        )

    def process_exit(self, db_pos: DBPosition, reason: str, current_premium: float):
        """Process exit using database with retry logic"""

        # Place exit order with retry
        max_retries = 2
        success = False
        fill_price = 0.0
        order_id = ""

        for attempt in range(max_retries):
            success, fill_price, order_id = self.order_mgr.place_exit_order(
                db_pos.symbol, db_pos.qty, current_premium
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

        # Close position in DB (calculates P&L internally)
        self.db.close_position(
            position_id=db_pos.id,
            exit_price=fill_price,
            exit_spot=self.prices['spot'],
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

        # Unsubscribe from option
        if self.ws_connected and self.ticker and self.option_token:
            try:
                self.ticker.unsubscribe([self.option_token])
            except Exception:
                pass  # Ignore unsubscribe errors
        self.option_token = None
        self.option_symbol = None

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
        """Main trading loop"""
        logging.info("Starting main loop...")
        last_log = 0
        last_ws_check = time.time()
        ws_reconnect_attempts = 0

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

                # Check if we have prices
                if self.prices['spot'] == 0 or self.prices['current_fut'] == 0:
                    time.sleep(1)
                    continue

                # Calculate z-score
                z_score, basis, fut_used, basis_pct = self.signal_engine.update(
                    self.prices['spot'],
                    self.prices['current_fut'],
                    self.prices['next_fut'],
                    self.config['strategy']['min_basis_current']
                )

                # Log periodically
                if time.time() - last_log > 60:
                    logging.info(f"Spot: {self.prices['spot']:.2f}, Basis: {basis:.1f}, "
                                f"Z: {z_score:.2f}, Fut: {fut_used}")
                    last_log = time.time()
                    self.consecutive_errors = 0  # Reset error count on successful tick

                # Get current position from DB
                open_position = self.db.get_open_position()

                # Check for exit first (if in position)
                if open_position:
                    current_premium = self.prices['option']

                    # If option price is stale (0), try REST API fallback
                    if current_premium <= 0 and self.option_symbol:
                        logging.debug("Option price stale, trying REST API")
                        rest_price = self.order_mgr.get_option_ltp(self.option_symbol)
                        if rest_price:
                            current_premium = rest_price
                            self.prices['option'] = rest_price  # Update cache

                    if current_premium > 0:
                        # Convert DB position to Position dataclass for signal engine
                        pos = Position(
                            active=True,
                            symbol=open_position.symbol,
                            entry_price=open_position.entry_price,
                            stop_loss=open_position.stop_loss,
                            target=open_position.target,
                            exit_deadline=open_position.exit_deadline
                        )
                        should_exit, reason = self.signal_engine.should_exit(
                            pos, current_premium, z_score
                        )
                        if should_exit:
                            self.process_exit(open_position, reason, current_premium)
                    else:
                        # Still no price - log warning
                        logging.warning(f"No option price available for {open_position.symbol}")

                # Check for entry (if no position)
                else:
                    # Get today's stats for limit checks
                    stats = self.db.get_today_stats()
                    should_enter, msg = self._check_entry_conditions(
                        z_score, basis, fut_used, stats
                    )
                    if should_enter:
                        logging.info(f"Entry signal! Z={z_score:.2f}, Basis={basis:.1f}")
                        self.process_entry(z_score, basis, fut_used, self.prices['spot'])

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

    def _check_entry_conditions(self, z_score: float, basis: float,
                                 fut_used: str, stats: Dict) -> Tuple[bool, str]:
        """Check if entry conditions are met using DB stats"""
        config = self.config

        # Daily limits from DB
        if stats.get('total_trades', 0) >= config['risk']['max_trades_per_day']:
            return False, "Max trades reached"

        if stats.get('gross_pnl', 0) <= -config['risk']['max_daily_loss']:
            return False, "Daily loss limit hit"

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
        logging.info("=" * 60)

        # Startup alert
        self.telegram.alert_startup(self.config['paper_trade'])

        # Start WebSocket
        self.start_websocket()

        # Check for existing position
        self.check_and_recover_position()

        # Start main loop
        self.running = True

        try:
            self.main_loop()
        except KeyboardInterrupt:
            logging.info("Shutting down...")
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
