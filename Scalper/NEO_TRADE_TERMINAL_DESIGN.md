# NEO Trade Terminal - Complete Architecture & Design Document

## Executive Summary

A high-speed GUI-based trading terminal for Kotak NEO API, optimized for intraday options trading with one-click execution, inspired by 1Cliq's speed-focused design philosophy.

---

## Core Design Principles (Trader's Perspective)

### 1. **Speed First**
- Every millisecond counts in scalping
- Pre-loaded symbol mappings (no lookup delay during trade)
- Keyboard shortcuts for everything
- One-click buttons for common actions

### 2. **Zero Friction Entry**
- Paste Kite symbol → Auto-derive NEO token → Show confirmation → Execute
- No modal popups, no "are you sure?" dialogs (configurable)
- Visual feedback, not blocking confirmations

### 3. **Position Awareness**
- Live P&L always visible
- Color-coded profit/loss indicators
- Quick exit buttons at position level

### 4. **Multi-leg Ready**
- Basket order support for spreads
- Execute 2-4 legs simultaneously
- Combined margin display

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           NEO TRADE TERMINAL v1.0                               │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  ┌─────────────┐     ┌──────────────────┐     ┌─────────────────────────────┐  │
│  │   CONFIG    │     │   SESSION MGR    │     │      SYMBOL MAPPER          │  │
│  │   (YAML)    │────▶│   (Auto TOTP)    │────▶│   (Kite → NEO Lookup)       │  │
│  └─────────────┘     └──────────────────┘     └─────────────────────────────┘  │
│         │                    │                            │                     │
│         ▼                    ▼                            ▼                     │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                         NEO API WRAPPER                                  │   │
│  │   • place_order()    • modify_order()    • cancel_order()               │   │
│  │   • positions()      • order_report()    • limits()                     │   │
│  │   • bracket_order()  • cover_order()     • margin_required()            │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                         WEBSOCKET HANDLER                                │   │
│  │   • Live LTP Feed        • Order Updates        • Position Updates      │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                              GUI LAYER                                   │   │
│  │                         (PyQt6 / CustomTkinter)                          │   │
│  │  ┌────────────────┐  ┌────────────────┐  ┌────────────────────────────┐ │   │
│  │  │  QUICK ENTRY   │  │   POSITIONS    │  │      ORDER BOOK            │ │   │
│  │  │    PANEL       │  │     PANEL      │  │        PANEL               │ │   │
│  │  └────────────────┘  └────────────────┘  └────────────────────────────┘ │   │
│  │  ┌────────────────┐  ┌────────────────┐  ┌────────────────────────────┐ │   │
│  │  │  BASKET ORDER  │  │   SL/TARGET    │  │      STATUS BAR            │ │   │
│  │  │    BUILDER     │  │    MANAGER     │  │   (Margin, Day P&L, Time)  │ │   │
│  │  └────────────────┘  └────────────────┘  └────────────────────────────┘ │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## Module Breakdown

### Module 1: Configuration Manager (`config/`)

```yaml
# config/settings.yaml
neo_credentials:
  consumer_key: "YOUR_CONSUMER_KEY"
  mobile_number: "+91XXXXXXXXXX"
  ucc: "YOUR_UCC"
  mpin: "XXXXXX"
  totp_secret: "YOUR_TOTP_SECRET"  # For auto-TOTP generation

kite_credentials:
  api_key: "YOUR_KITE_API_KEY"
  access_token: ""  # Will be read from existing session file if empty
  session_file: "~/BOTS/kite_session.json"  # Your existing Kite session

telegram:
  enabled: true
  bot_token: "YOUR_BOT_TOKEN"
  chat_id: "YOUR_CHAT_ID"

trading_defaults:
  product: "MIS"           # MIS for intraday, NRML for positional
  order_type: "L"          # L=Limit, MKT=Market
  validity: "DAY"
  default_lots: 1
  
trailing_sl:
  # Default trail mode: manual, auto_points, auto_pct, lock_cost
  default_mode: "manual"
  
  # Quick trail button increments (points)
  trail_buttons: [10, 25, 50]
  
  # LTP minus buffer for quick trail
  ltp_buffer: 20
  
  # Auto-trail settings (when mode != manual)
  auto_trail_points: 15        # Trail SL every 15 points of profit
  auto_trail_percent: 50       # Lock 50% of profit
  lock_cost_trigger: 30        # Lock cost when profit > 30 points
  
  # Safety: minimum distance from LTP
  min_sl_distance: 5           # SL must be at least 5 points from LTP

risk_management:
  max_loss_per_day: 10000          # ₹ - Circuit breaker
  max_position_value: 500000       # ₹ - Per trade
  max_open_positions: 5
  duplicate_order_window_sec: 5    # Prevent double-clicks
  
ui_preferences:
  theme: "dark"
  confirm_before_order: false      # Speed mode - no confirmation
  confirm_before_exit: false
  sound_on_fill: true
  keyboard_shortcuts_enabled: true

signal_watcher:
  enabled: false                   # Enable when ready for auto mode
  signal_dir: "signals"
  poll_interval_sec: 1

lot_sizes:  # Fallback if Kite lookup fails
  NIFTY: 75
  BANKNIFTY: 15
  FINNIFTY: 25
  SENSEX: 10
  BANKEX: 15
  MIDCPNIFTY: 50
```

### Module 2: Session Manager (`core/session_manager.py`)

```python
"""
Auto-login with TOTP generation on terminal startup.
Maintains session tokens and handles reconnection.
"""

import pyotp
from neo_api_client import NeoAPI
from datetime import datetime, timedelta
import json
import os

class SessionManager:
    def __init__(self, config):
        self.config = config
        self.client = None
        self.session_file = "data/session.json"
        self.session_valid_until = None
        
    def auto_login(self) -> tuple[bool, str]:
        """
        Automatic login flow:
        1. Check if existing session is valid
        2. If not, generate TOTP and login fresh
        """
        # Try to restore session first
        if self._restore_session():
            return True, "Session restored from cache"
        
        # Fresh login with TOTP
        try:
            self.client = NeoAPI(
                environment='prod',
                access_token=None,
                neo_fin_key=None,
                consumer_key=self.config['neo_credentials']['consumer_key']
            )
            
            # Generate TOTP automatically
            totp = pyotp.TOTP(self.config['neo_credentials']['totp_secret'])
            current_totp = totp.now()
            
            # Step 1: TOTP Login
            self.client.totp_login(
                mobile_number=self.config['neo_credentials']['mobile_number'],
                ucc=self.config['neo_credentials']['ucc'],
                totp=current_totp
            )
            
            # Step 2: MPIN Validation
            self.client.totp_validate(
                mpin=self.config['neo_credentials']['mpin']
            )
            
            # Save session for reuse
            self._save_session()
            
            return True, f"Login successful at {datetime.now().strftime('%H:%M:%S')}"
            
        except Exception as e:
            return False, f"Login failed: {str(e)}"
    
    def _save_session(self):
        """Save session tokens for quick restoration"""
        session_data = {
            'timestamp': datetime.now().isoformat(),
            'valid_until': (datetime.now() + timedelta(hours=8)).isoformat(),
            # Store relevant tokens from client object
        }
        os.makedirs('data', exist_ok=True)
        with open(self.session_file, 'w') as f:
            json.dump(session_data, f)
    
    def _restore_session(self) -> bool:
        """Attempt to restore previous session"""
        if not os.path.exists(self.session_file):
            return False
        
        try:
            with open(self.session_file, 'r') as f:
                session_data = json.load(f)
            
            valid_until = datetime.fromisoformat(session_data['valid_until'])
            if datetime.now() < valid_until:
                # Session still valid, restore client
                # ... restoration logic
                return True
        except:
            pass
        
        return False
    
    def get_client(self) -> NeoAPI:
        return self.client
    
    def is_session_active(self) -> bool:
        """Check if current session is active"""
        try:
            # Quick API call to verify session
            self.client.limits()
            return True
        except:
            return False
```

### Module 3: Symbol Mapper (`core/symbol_mapper.py`)

```python
"""
Maps Kite/Zerodha symbol format to NEO instrument tokens.
Downloads and caches scrip master daily.
"""

import pandas as pd
import os
from datetime import datetime, date
import re

class SymbolMapper:
    def __init__(self, neo_client):
        self.client = neo_client
        self.cache_dir = "data/scrip_master"
        self.nse_fo_df = None
        self.nse_cm_df = None
        self.loaded_date = None
        
    def initialize(self) -> tuple[bool, str]:
        """
        Download and load scrip master files.
        Should be called once at startup before market hours.
        """
        os.makedirs(self.cache_dir, exist_ok=True)
        today = date.today().isoformat()
        
        # Check if today's cache exists
        nse_fo_file = f"{self.cache_dir}/nse_fo_{today}.csv"
        
        if os.path.exists(nse_fo_file):
            self._load_from_cache(today)
            return True, f"Loaded scrip master from cache ({today})"
        
        # Download fresh
        try:
            # Download NSE F&O scrip master
            nse_fo_data = self.client.scrip_master(exchange_segment="nse_fo")
            self.nse_fo_df = pd.DataFrame(nse_fo_data)
            self.nse_fo_df.to_csv(nse_fo_file, index=False)
            
            # Download NSE Cash scrip master
            nse_cm_data = self.client.scrip_master(exchange_segment="nse_cm")
            self.nse_cm_df = pd.DataFrame(nse_cm_data)
            self.nse_cm_df.to_csv(f"{self.cache_dir}/nse_cm_{today}.csv", index=False)
            
            self.loaded_date = today
            return True, f"Downloaded fresh scrip master ({today})"
            
        except Exception as e:
            return False, f"Failed to download scrip master: {str(e)}"
    
    def _load_from_cache(self, date_str):
        """Load from cached CSV files"""
        self.nse_fo_df = pd.read_csv(f"{self.cache_dir}/nse_fo_{date_str}.csv")
        nse_cm_file = f"{self.cache_dir}/nse_cm_{date_str}.csv"
        if os.path.exists(nse_cm_file):
            self.nse_cm_df = pd.read_csv(nse_cm_file)
        self.loaded_date = date_str
    
    def parse_kite_symbol(self, kite_symbol: str) -> dict:
        """
        Parse Kite symbol format into components.
        
        Examples:
            NIFTY25JAN24000CE → {underlying: NIFTY, expiry: 25JAN, strike: 24000, opt_type: CE}
            BANKNIFTY25JAN52000PE → {underlying: BANKNIFTY, expiry: 25JAN, strike: 52000, opt_type: PE}
            RELIANCE25JAN1400CE → {underlying: RELIANCE, expiry: 25JAN, strike: 1400, opt_type: CE}
            NIFTY25JANFUT → {underlying: NIFTY, expiry: 25JAN, instrument: FUT}
        """
        kite_symbol = kite_symbol.upper().strip()
        
        # Pattern for options: SYMBOL + EXPIRY + STRIKE + CE/PE
        option_pattern = r'^([A-Z]+)(\d{2}[A-Z]{3})(\d+)(CE|PE)$'
        # Pattern for futures: SYMBOL + EXPIRY + FUT
        future_pattern = r'^([A-Z]+)(\d{2}[A-Z]{3})(FUT)$'
        # Pattern for weekly options: SYMBOL + DDMMM + STRIKE + CE/PE (e.g., NIFTY16JAN24000CE)
        weekly_pattern = r'^([A-Z]+)(\d{1,2}[A-Z]{3})(\d+)(CE|PE)$'
        
        # Try options pattern
        match = re.match(option_pattern, kite_symbol)
        if match:
            return {
                'underlying': match.group(1),
                'expiry': match.group(2),
                'strike': match.group(3),
                'option_type': match.group(4),
                'instrument_type': 'OPTION'
            }
        
        # Try futures pattern
        match = re.match(future_pattern, kite_symbol)
        if match:
            return {
                'underlying': match.group(1),
                'expiry': match.group(2),
                'option_type': None,
                'strike': None,
                'instrument_type': 'FUTURE'
            }
        
        # Try weekly pattern (same as option but different interpretation)
        match = re.match(weekly_pattern, kite_symbol)
        if match:
            return {
                'underlying': match.group(1),
                'expiry': match.group(2),
                'strike': match.group(3),
                'option_type': match.group(4),
                'instrument_type': 'OPTION'
            }
        
        raise ValueError(f"Unable to parse symbol: {kite_symbol}")
    
    def map_to_neo(self, kite_symbol: str) -> dict:
        """
        Map Kite symbol to NEO trading parameters.
        
        Returns dict ready for place_order():
        {
            'exchange_segment': 'nse_fo',
            'trading_symbol': 'NIFTY25JAN24000CE',
            'instrument_token': '12345',
            'lot_size': 75,
            'tick_size': 0.05
        }
        """
        parsed = self.parse_kite_symbol(kite_symbol)
        
        # Search in scrip master
        df = self.nse_fo_df
        
        # Build search criteria based on parsed data
        # NEO trading_symbol format may differ, need to match on components
        
        # Filter by underlying
        mask = df['pSymbol'].str.upper() == parsed['underlying']
        
        if parsed['instrument_type'] == 'OPTION':
            # Filter by option type and strike
            mask &= df['pOptionType'].str.upper() == parsed['option_type']
            mask &= df['pStrikePrice'].astype(str) == parsed['strike']
            # Filter by expiry (need to match format)
            # NEO format might be different, adjust as needed
        
        matches = df[mask]
        
        if matches.empty:
            raise ValueError(f"Symbol not found in NEO scrip master: {kite_symbol}")
        
        # Get the matching row (prefer nearest expiry if multiple)
        row = matches.iloc[0]
        
        return {
            'exchange_segment': 'nse_fo',
            'trading_symbol': row['pTradingSymbol'],
            'instrument_token': str(row['pScripCode']),
            'lot_size': int(row.get('pLotSize', self._get_default_lot_size(parsed['underlying']))),
            'tick_size': float(row.get('pTickSize', 0.05)),
            'underlying': parsed['underlying'],
            'strike': parsed.get('strike'),
            'option_type': parsed.get('option_type'),
            'expiry': parsed.get('expiry')
        }
    
    def _get_default_lot_size(self, underlying: str) -> int:
        """Fallback lot sizes"""
        lot_sizes = {
            'NIFTY': 75,
            'BANKNIFTY': 15,
            'FINNIFTY': 25,
            'SENSEX': 10,
            'BANKEX': 15,
            'MIDCPNIFTY': 50,
        }
        return lot_sizes.get(underlying, 1)
    
    def search_symbol(self, query: str) -> list:
        """
        Search for symbols matching query.
        Useful for autocomplete in GUI.
        """
        if self.nse_fo_df is None:
            return []
        
        query = query.upper()
        mask = self.nse_fo_df['pTradingSymbol'].str.contains(query, na=False)
        matches = self.nse_fo_df[mask].head(20)
        
        return matches[['pTradingSymbol', 'pScripCode', 'pLotSize']].to_dict('records')
```

### Module 4: Order Manager (`core/order_manager.py`)

```python
"""
Handles all order operations with safety checks.
Implements bracket orders, multi-leg orders, and position management.
"""

from dataclasses import dataclass
from typing import Optional, List
from datetime import datetime
import threading
import time

@dataclass
class OrderParams:
    """Standard order parameters"""
    symbol: str                    # NEO trading symbol
    exchange_segment: str          # nse_fo, nse_cm, etc.
    instrument_token: str
    transaction_type: str          # B or S
    quantity: int                  # Total quantity (not lots)
    product: str                   # MIS, NRML
    order_type: str               # L, MKT, SL, SL-M
    price: Optional[float] = None  # For limit orders
    trigger_price: Optional[float] = None  # For SL orders
    disclosed_qty: int = 0
    validity: str = "DAY"
    tag: Optional[str] = None

@dataclass  
class BracketOrderParams:
    """Bracket order with SL and Target"""
    entry: OrderParams
    stop_loss_points: float        # Points from entry
    target_points: float           # Points from entry
    trailing_sl: bool = False
    trailing_sl_points: float = 0

class OrderManager:
    def __init__(self, neo_client, config, symbol_mapper):
        self.client = neo_client
        self.config = config
        self.mapper = symbol_mapper
        self.recent_orders = []  # For duplicate prevention
        self._lock = threading.Lock()
        
    def place_order(self, params: OrderParams) -> dict:
        """
        Place single order with safety checks.
        Returns order response or raises exception.
        """
        # Safety Check 1: Duplicate prevention
        if self._is_duplicate_order(params):
            raise ValueError("Duplicate order detected within time window")
        
        # Safety Check 2: Position limits
        if not self._check_position_limits(params):
            raise ValueError("Position limit exceeded")
        
        # Safety Check 3: Daily loss limit
        if not self._check_daily_loss_limit():
            raise ValueError("Daily loss limit reached - trading halted")
        
        # Place the order
        try:
            response = self.client.place_order(
                exchange_segment=params.exchange_segment,
                product=params.product,
                price=str(params.price) if params.price else "0",
                order_type=params.order_type,
                quantity=str(params.quantity),
                validity=params.validity,
                trading_symbol=params.symbol,
                transaction_type=params.transaction_type,
                amo="NO",
                disclosed_quantity=str(params.disclosed_qty),
                trigger_price=str(params.trigger_price) if params.trigger_price else "0",
                tag=params.tag
            )
            
            # Record for duplicate prevention
            self._record_order(params)
            
            return response
            
        except Exception as e:
            raise Exception(f"Order placement failed: {str(e)}")
    
    def place_bracket_order(self, params: BracketOrderParams) -> dict:
        """
        Place bracket order (entry + SL + target as OCO).
        NEO supports BO for F&O with specific parameters.
        """
        entry = params.entry
        
        # Calculate SL and Target prices
        if entry.transaction_type == 'B':
            sl_trigger = entry.price - params.stop_loss_points
            target_price = entry.price + params.target_points
        else:
            sl_trigger = entry.price + params.stop_loss_points
            target_price = entry.price - params.target_points
        
        try:
            response = self.client.place_order(
                exchange_segment=entry.exchange_segment,
                product="BO",
                price=str(entry.price),
                order_type="L",
                quantity=str(entry.quantity),
                validity="DAY",
                trading_symbol=entry.symbol,
                transaction_type=entry.transaction_type,
                amo="NO",
                trigger_price=str(sl_trigger),
                square_off_type="Absolute",
                stop_loss_type="Absolute",
                stop_loss_value=str(params.stop_loss_points),
                square_off_value=str(params.target_points),
                trailing_stop_loss="Y" if params.trailing_sl else "N",
                trailing_sl_value=str(params.trailing_sl_points) if params.trailing_sl else "0"
            )
            
            self._record_order(entry)
            return response
            
        except Exception as e:
            # Bracket order not supported - fall back to manual SL/Target
            raise Exception(f"Bracket order failed: {str(e)}. Use manual SL/Target.")
    
    def place_multi_leg_order(self, legs: List[OrderParams]) -> List[dict]:
        """
        Place multiple legs simultaneously (for spreads).
        Uses threading for parallel execution.
        """
        results = []
        threads = []
        errors = []
        
        def place_leg(leg, index):
            try:
                result = self.place_order(leg)
                results.append((index, result))
            except Exception as e:
                errors.append((index, str(e)))
        
        # Start all orders in parallel
        for i, leg in enumerate(legs):
            t = threading.Thread(target=place_leg, args=(leg, i))
            threads.append(t)
            t.start()
        
        # Wait for all to complete
        for t in threads:
            t.join()
        
        if errors:
            # Some legs failed - need to handle partial fills
            raise Exception(f"Multi-leg order partial failure: {errors}")
        
        # Sort results by index to maintain order
        results.sort(key=lambda x: x[0])
        return [r[1] for r in results]
    
    def modify_order(self, order_id: str, new_price: float = None, 
                     new_quantity: int = None, new_trigger: float = None) -> dict:
        """Modify existing order"""
        return self.client.modify_order(
            order_id=order_id,
            price=str(new_price) if new_price else "",
            quantity=str(new_quantity) if new_quantity else "",
            trigger_price=str(new_trigger) if new_trigger else "",
            validity="DAY"
        )
    
    def cancel_order(self, order_id: str) -> dict:
        """Cancel order by ID"""
        return self.client.cancel_order(order_id=order_id)
    
    def exit_position(self, position: dict, exit_qty_percent: int = 100) -> dict:
        """
        Exit a position by placing opposite order.
        
        Args:
            position: Position dict from positions()
            exit_qty_percent: 25, 50, 75, or 100
        """
        qty = int(position['qty'])
        exit_qty = int(qty * exit_qty_percent / 100)
        
        # Opposite transaction
        txn_type = 'S' if qty > 0 else 'B'
        
        params = OrderParams(
            symbol=position['symbol'],
            exchange_segment=position['exchange_segment'],
            instrument_token=position['instrument_token'],
            transaction_type=txn_type,
            quantity=abs(exit_qty),
            product=position['product'],
            order_type='MKT',  # Market order for quick exit
            tag='EXIT'
        )
        
        return self.place_order(params)
    
    def exit_all_positions(self) -> List[dict]:
        """Square off all open positions"""
        positions = self.client.positions()
        results = []
        
        for pos in positions.get('data', []):
            if int(pos.get('qty', 0)) != 0:
                try:
                    result = self.exit_position(pos)
                    results.append({'symbol': pos['symbol'], 'status': 'success', 'response': result})
                except Exception as e:
                    results.append({'symbol': pos['symbol'], 'status': 'failed', 'error': str(e)})
        
        return results
    
    def set_sl_target(self, position: dict, sl_price: float, target_price: float) -> dict:
        """
        Set SL and Target for existing position.
        Places two separate orders (no native OCO in NEO).
        Returns order IDs for both.
        """
        qty = abs(int(position['qty']))
        exit_type = 'S' if int(position['qty']) > 0 else 'B'
        
        # Place SL order
        sl_order = self.client.place_order(
            exchange_segment=position['exchange_segment'],
            product=position['product'],
            price="0",
            order_type="SL-M",  # Stop Loss Market
            quantity=str(qty),
            validity="DAY",
            trading_symbol=position['symbol'],
            transaction_type=exit_type,
            trigger_price=str(sl_price),
            tag='SL'
        )
        
        # Place Target order
        target_order = self.client.place_order(
            exchange_segment=position['exchange_segment'],
            product=position['product'],
            price=str(target_price),
            order_type="L",
            quantity=str(qty),
            validity="DAY",
            trading_symbol=position['symbol'],
            transaction_type=exit_type,
            tag='TARGET'
        )
        
        return {
            'sl_order_id': sl_order.get('nOrdNo'),
            'target_order_id': target_order.get('nOrdNo')
        }
    
    # Safety check methods
    def _is_duplicate_order(self, params: OrderParams) -> bool:
        """Check if same order was placed within time window"""
        window = self.config['risk_management']['duplicate_order_window_sec']
        now = time.time()
        
        with self._lock:
            for order_time, order_params in self.recent_orders:
                if now - order_time < window:
                    if (order_params.symbol == params.symbol and
                        order_params.transaction_type == params.transaction_type and
                        order_params.quantity == params.quantity):
                        return True
            return False
    
    def _record_order(self, params: OrderParams):
        """Record order for duplicate prevention"""
        with self._lock:
            self.recent_orders.append((time.time(), params))
            # Cleanup old entries
            cutoff = time.time() - 60  # Keep last 60 seconds
            self.recent_orders = [(t, p) for t, p in self.recent_orders if t > cutoff]
    
    def _check_position_limits(self, params: OrderParams) -> bool:
        """Check if new order exceeds position limits"""
        try:
            positions = self.client.positions()
            open_count = sum(1 for p in positions.get('data', []) if int(p.get('qty', 0)) != 0)
            return open_count < self.config['risk_management']['max_open_positions']
        except:
            return True  # Allow if check fails
    
    def _check_daily_loss_limit(self) -> bool:
        """Check if daily loss limit is breached"""
        try:
            positions = self.client.positions()
            total_pnl = sum(float(p.get('dayBuyAmt', 0)) - float(p.get('daySellAmt', 0)) 
                          for p in positions.get('data', []))
            return total_pnl > -self.config['risk_management']['max_loss_per_day']
        except:
            return True
```

### Module 5: Kite Spot Price Fetcher (`core/kite_spot.py`)

```python
"""
Fetches live NIFTY/BANKNIFTY spot prices from Kite for ATM calculation.
Uses your existing Kite credentials.
"""

from kiteconnect import KiteConnect
import os

class KiteSpotFetcher:
    def __init__(self, config):
        self.config = config
        self.kite = None
        self.spot_tokens = {
            'NIFTY': 256265,      # NIFTY 50 token
            'BANKNIFTY': 260105,  # BANK NIFTY token
            'FINNIFTY': 257801,   # FINNIFTY token
            'SENSEX': 265,        # SENSEX token (BSE)
        }
        self.strike_gaps = {
            'NIFTY': 50,
            'BANKNIFTY': 100,
            'FINNIFTY': 50,
            'SENSEX': 100,
        }
    
    def connect(self) -> tuple[bool, str]:
        """Connect to Kite using existing credentials"""
        try:
            api_key = self.config.get('kite_credentials', {}).get('api_key')
            access_token = self.config.get('kite_credentials', {}).get('access_token')
            
            if not api_key or not access_token:
                # Try to read from existing Kite session file
                session_file = os.path.expanduser("~/BOTS/kite_session.json")
                if os.path.exists(session_file):
                    import json
                    with open(session_file) as f:
                        session = json.load(f)
                        api_key = session.get('api_key')
                        access_token = session.get('access_token')
            
            if not api_key or not access_token:
                return False, "Kite credentials not found"
            
            self.kite = KiteConnect(api_key=api_key)
            self.kite.set_access_token(access_token)
            
            # Test connection
            self.kite.profile()
            return True, "Kite connected for spot prices"
            
        except Exception as e:
            return False, f"Kite connection failed: {str(e)}"
    
    def get_spot_price(self, underlying: str) -> float:
        """Get current spot price for underlying"""
        if not self.kite:
            raise Exception("Kite not connected")
        
        underlying = underlying.upper()
        token = self.spot_tokens.get(underlying)
        
        if not token:
            raise ValueError(f"Unknown underlying: {underlying}")
        
        try:
            quote = self.kite.quote([f"NSE:{underlying}"])
            return quote[f"NSE:{underlying}"]['last_price']
        except:
            # Fallback to LTP
            ltp = self.kite.ltp([f"NSE:{underlying}"])
            return ltp[f"NSE:{underlying}"]['last_price']
    
    def get_atm_strike(self, underlying: str) -> int:
        """Calculate ATM strike for underlying"""
        spot = self.get_spot_price(underlying)
        gap = self.strike_gaps.get(underlying, 50)
        
        # Round to nearest strike
        atm = round(spot / gap) * gap
        return int(atm)
    
    def get_option_symbol(self, underlying: str, opt_type: str, 
                          strike_offset: int = 0) -> str:
        """
        Generate Kite-format option symbol.
        
        Args:
            underlying: NIFTY, BANKNIFTY, etc.
            opt_type: CE or PE
            strike_offset: 0 for ATM, +1 for OTM1, -1 for ITM1, etc.
        
        Returns:
            Symbol like NIFTY25JAN24000CE
        """
        from datetime import datetime, timedelta
        
        atm = self.get_atm_strike(underlying)
        gap = self.strike_gaps.get(underlying, 50)
        strike = atm + (strike_offset * gap)
        
        # Get current expiry (Thursday for index options)
        today = datetime.now()
        days_until_thursday = (3 - today.weekday()) % 7
        if days_until_thursday == 0 and today.hour >= 15:
            days_until_thursday = 7
        expiry = today + timedelta(days=days_until_thursday)
        
        # Format: NIFTY25JAN24000CE (for monthly) or NIFTY2511624000CE (for weekly)
        # Using weekly format: YYMMDD
        expiry_str = expiry.strftime("%y%b").upper()  # 25JAN
        
        return f"{underlying}{expiry_str}{strike}{opt_type}"
    
    def get_option_chain_strikes(self, underlying: str, 
                                  range_count: int = 5) -> dict:
        """
        Get range of strikes around ATM.
        
        Returns:
            {
                'spot': 24150,
                'atm': 24150,
                'strikes': [24000, 24050, 24100, 24150, 24200, 24250, 24300],
                'ce_symbols': [...],
                'pe_symbols': [...]
            }
        """
        spot = self.get_spot_price(underlying)
        atm = self.get_atm_strike(underlying)
        gap = self.strike_gaps.get(underlying, 50)
        
        strikes = []
        ce_symbols = []
        pe_symbols = []
        
        for i in range(-range_count, range_count + 1):
            strike = atm + (i * gap)
            strikes.append(strike)
            
            from datetime import datetime, timedelta
            today = datetime.now()
            days_until_thursday = (3 - today.weekday()) % 7
            if days_until_thursday == 0 and today.hour >= 15:
                days_until_thursday = 7
            expiry = today + timedelta(days=days_until_thursday)
            expiry_str = expiry.strftime("%y%b").upper()
            
            ce_symbols.append(f"{underlying}{expiry_str}{strike}CE")
            pe_symbols.append(f"{underlying}{expiry_str}{strike}PE")
        
        return {
            'spot': spot,
            'atm': atm,
            'strikes': strikes,
            'ce_symbols': ce_symbols,
            'pe_symbols': pe_symbols
        }
```

### Module 6: Sound Alert Manager (`core/sound_alerts.py`)

```python
"""
Sound alerts for order events.
Uses system sounds or custom WAV files.
"""

import os
import threading
from typing import Optional

class SoundAlertManager:
    def __init__(self, config):
        self.config = config
        self.enabled = config.get('ui_preferences', {}).get('sound_on_fill', True)
        self.sounds_dir = "assets/sounds"
        
        # Sound mapping
        self.sound_files = {
            'order_placed': 'click.wav',
            'order_filled': 'success.wav',
            'order_rejected': 'error.wav',
            'sl_hit': 'alert.wav',
            'target_hit': 'cash.wav',
            'position_exit': 'ding.wav',
        }
        
        # Try to import sound library
        self._sound_lib = None
        try:
            import winsound
            self._sound_lib = 'winsound'
        except ImportError:
            try:
                from playsound import playsound
                self._sound_lib = 'playsound'
            except ImportError:
                try:
                    import simpleaudio
                    self._sound_lib = 'simpleaudio'
                except ImportError:
                    print("[SOUND] No sound library available")
    
    def play(self, event_type: str):
        """Play sound for event type (non-blocking)"""
        if not self.enabled or not self._sound_lib:
            return
        
        # Play in background thread to not block
        threading.Thread(target=self._play_sound, args=(event_type,), daemon=True).start()
    
    def _play_sound(self, event_type: str):
        """Internal sound player"""
        sound_file = self.sound_files.get(event_type)
        if not sound_file:
            # Use system beep as fallback
            self._beep(event_type)
            return
        
        filepath = os.path.join(self.sounds_dir, sound_file)
        
        if os.path.exists(filepath):
            try:
                if self._sound_lib == 'winsound':
                    import winsound
                    winsound.PlaySound(filepath, winsound.SND_FILENAME | winsound.SND_ASYNC)
                elif self._sound_lib == 'playsound':
                    from playsound import playsound
                    playsound(filepath, block=False)
                elif self._sound_lib == 'simpleaudio':
                    import simpleaudio as sa
                    wave_obj = sa.WaveObject.from_wave_file(filepath)
                    wave_obj.play()
            except Exception as e:
                print(f"[SOUND] Playback error: {e}")
                self._beep(event_type)
        else:
            self._beep(event_type)
    
    def _beep(self, event_type: str):
        """System beep fallback"""
        try:
            if self._sound_lib == 'winsound':
                import winsound
                freq_map = {
                    'order_placed': 800,
                    'order_filled': 1200,
                    'order_rejected': 400,
                    'sl_hit': 300,
                    'target_hit': 1500,
                }
                freq = freq_map.get(event_type, 600)
                duration = 150 if event_type == 'order_placed' else 300
                winsound.Beep(freq, duration)
            else:
                print('\a')  # Terminal bell
        except:
            pass
    
    def toggle(self, enabled: bool):
        """Enable/disable sounds"""
        self.enabled = enabled
    
    def test_all(self):
        """Test all sound alerts"""
        import time
        for event in self.sound_files.keys():
            print(f"Testing: {event}")
            self.play(event)
            time.sleep(1)
```

### Module 7: Telegram Notifier (`core/telegram_notifier.py`)

```python
"""
Sends trade notifications to Telegram.
Integrates with your existing bot.
"""

import requests
from typing import Optional
from datetime import datetime

class TelegramNotifier:
    def __init__(self, config):
        self.config = config
        self.enabled = config.get('telegram', {}).get('enabled', True)
        self.bot_token = config.get('telegram', {}).get('bot_token', '')
        self.chat_id = config.get('telegram', {}).get('chat_id', '')
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
    
    def send(self, message: str, parse_mode: str = 'HTML'):
        """Send message to Telegram (non-blocking)"""
        if not self.enabled or not self.bot_token or not self.chat_id:
            return
        
        import threading
        threading.Thread(
            target=self._send_message, 
            args=(message, parse_mode), 
            daemon=True
        ).start()
    
    def _send_message(self, message: str, parse_mode: str):
        """Internal message sender"""
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': parse_mode
            }
            response = requests.post(url, json=payload, timeout=5)
            if not response.ok:
                print(f"[TG] Send failed: {response.text}")
        except Exception as e:
            print(f"[TG] Error: {e}")
    
    def notify_order_placed(self, symbol: str, action: str, qty: int, 
                           price: float, order_id: str):
        """Notify order placement"""
        emoji = "🟢" if action == 'B' else "🔴"
        action_text = "BUY" if action == 'B' else "SELL"
        
        msg = f"""
{emoji} <b>ORDER PLACED</b>
━━━━━━━━━━━━━━━━━
Symbol: <code>{symbol}</code>
Action: {action_text}
Qty: {qty}
Price: ₹{price:.2f}
Order ID: {order_id}
Time: {datetime.now().strftime('%H:%M:%S')}
"""
        self.send(msg)
    
    def notify_order_filled(self, symbol: str, action: str, qty: int,
                           fill_price: float, order_id: str):
        """Notify order fill"""
        emoji = "✅"
        action_text = "BOUGHT" if action == 'B' else "SOLD"
        
        msg = f"""
{emoji} <b>ORDER FILLED</b>
━━━━━━━━━━━━━━━━━
Symbol: <code>{symbol}</code>
{action_text} {qty} @ ₹{fill_price:.2f}
Order ID: {order_id}
Time: {datetime.now().strftime('%H:%M:%S')}
"""
        self.send(msg)
    
    def notify_order_rejected(self, symbol: str, action: str, 
                             reason: str, order_id: str):
        """Notify order rejection"""
        msg = f"""
❌ <b>ORDER REJECTED</b>
━━━━━━━━━━━━━━━━━
Symbol: <code>{symbol}</code>
Action: {'BUY' if action == 'B' else 'SELL'}
Reason: {reason}
Order ID: {order_id}
"""
        self.send(msg)
    
    def notify_sl_hit(self, symbol: str, exit_price: float, 
                     pnl: float, pnl_pct: float):
        """Notify SL hit"""
        msg = f"""
🛑 <b>STOP LOSS HIT</b>
━━━━━━━━━━━━━━━━━
Symbol: <code>{symbol}</code>
Exit Price: ₹{exit_price:.2f}
P&L: ₹{pnl:,.0f} ({pnl_pct:+.1f}%)
Time: {datetime.now().strftime('%H:%M:%S')}
"""
        self.send(msg)
    
    def notify_target_hit(self, symbol: str, exit_price: float,
                         pnl: float, pnl_pct: float):
        """Notify target hit"""
        msg = f"""
🎯 <b>TARGET HIT</b>
━━━━━━━━━━━━━━━━━
Symbol: <code>{symbol}</code>
Exit Price: ₹{exit_price:.2f}
P&L: ₹{pnl:,.0f} ({pnl_pct:+.1f}%)
Time: {datetime.now().strftime('%H:%M:%S')}
"""
        self.send(msg)
    
    def notify_position_exit(self, symbol: str, qty: int, 
                            avg_price: float, exit_price: float,
                            pnl: float):
        """Notify position exit"""
        emoji = "💰" if pnl >= 0 else "📉"
        
        msg = f"""
{emoji} <b>POSITION CLOSED</b>
━━━━━━━━━━━━━━━━━
Symbol: <code>{symbol}</code>
Qty: {qty}
Avg: ₹{avg_price:.2f}
Exit: ₹{exit_price:.2f}
P&L: ₹{pnl:,.0f}
Time: {datetime.now().strftime('%H:%M:%S')}
"""
        self.send(msg)
    
    def notify_daily_summary(self, total_trades: int, winners: int,
                            losers: int, total_pnl: float):
        """Send daily trading summary"""
        emoji = "📊"
        pnl_emoji = "💚" if total_pnl >= 0 else "❤️"
        win_rate = (winners / total_trades * 100) if total_trades > 0 else 0
        
        msg = f"""
{emoji} <b>DAILY SUMMARY - NEO</b>
━━━━━━━━━━━━━━━━━
Total Trades: {total_trades}
Winners: {winners} ✅
Losers: {losers} ❌
Win Rate: {win_rate:.1f}%

{pnl_emoji} <b>Net P&L: ₹{total_pnl:,.0f}</b>

Date: {datetime.now().strftime('%d-%b-%Y')}
"""
        self.send(msg)
    
    def notify_circuit_breaker(self, current_loss: float, limit: float):
        """Notify when circuit breaker triggers"""
        msg = f"""
🚨 <b>CIRCUIT BREAKER TRIGGERED</b>
━━━━━━━━━━━━━━━━━
Current Loss: ₹{abs(current_loss):,.0f}
Daily Limit: ₹{limit:,.0f}

⚠️ <b>TRADING HALTED</b>
New orders blocked until tomorrow.
"""
        self.send(msg)


### Module 8: Trailing SL Manager (`core/trailing_sl.py`)

```python
"""
Trailing Stop Loss Manager.
Provides multiple ways to trail SL:
1. Manual one-click trail (button click moves SL to cost/LTP-X)
2. Auto-trail based on LTP movement
3. Quick keyboard shortcuts for trail increments
"""

import threading
import time
from typing import Dict, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum

class TrailMode(Enum):
    MANUAL = "manual"           # User clicks to trail
    AUTO_POINTS = "auto_points" # Trail by fixed points when profit increases
    AUTO_PERCENT = "auto_pct"   # Trail by percentage of profit
    LOCK_COST = "lock_cost"     # Move SL to cost when X profit reached

@dataclass
class TrailingPosition:
    """Position being trailed"""
    symbol: str
    exchange_segment: str
    entry_price: float
    quantity: int
    side: str                    # 'LONG' or 'SHORT'
    current_sl: float
    sl_order_id: str
    target_order_id: Optional[str] = None
    
    # Trailing config
    trail_mode: TrailMode = TrailMode.MANUAL
    trail_points: float = 0      # For AUTO_POINTS: trail by X points
    trail_percent: float = 0     # For AUTO_PERCENT: trail by X% of profit
    lock_cost_trigger: float = 0 # For LOCK_COST: lock when profit > X points
    cost_locked: bool = False
    
    # Tracking
    highest_profit: float = 0    # Track peak profit (for LONG)
    lowest_profit: float = 0     # Track lowest profit (for SHORT)
    last_ltp: float = 0
    trail_history: list = field(default_factory=list)


class TrailingSLManager:
    def __init__(self, neo_client, order_manager, 
                 sound_mgr=None, telegram_mgr=None):
        self.client = neo_client
        self.order_mgr = order_manager
        self.sound = sound_mgr
        self.telegram = telegram_mgr
        
        self.positions: Dict[str, TrailingPosition] = {}
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._check_interval = 0.5  # Check every 500ms for fast trailing
        
        # Callbacks for GUI updates
        self.on_sl_updated: Optional[Callable] = None
        self.on_cost_locked: Optional[Callable] = None
    
    def add_position(self, symbol: str, exchange_segment: str,
                    entry_price: float, quantity: int, side: str,
                    sl_price: float, sl_order_id: str,
                    target_order_id: str = None,
                    trail_mode: TrailMode = TrailMode.MANUAL,
                    trail_points: float = 0,
                    trail_percent: float = 0,
                    lock_cost_trigger: float = 0) -> bool:
        """Register position for trailing"""
        with self._lock:
            pos = TrailingPosition(
                symbol=symbol,
                exchange_segment=exchange_segment,
                entry_price=entry_price,
                quantity=quantity,
                side=side,
                current_sl=sl_price,
                sl_order_id=sl_order_id,
                target_order_id=target_order_id,
                trail_mode=trail_mode,
                trail_points=trail_points,
                trail_percent=trail_percent,
                lock_cost_trigger=lock_cost_trigger,
                last_ltp=entry_price
            )
            self.positions[symbol] = pos
            print(f"[TRAIL] Added {symbol} mode={trail_mode.value}")
            return True
    
    def remove_position(self, symbol: str):
        """Remove position from trailing"""
        with self._lock:
            if symbol in self.positions:
                del self.positions[symbol]
    
    # ==================== MANUAL TRAIL METHODS ====================
    
    def trail_to_cost(self, symbol: str) -> dict:
        """
        One-click: Move SL to entry price (cost).
        Use when position is in profit and want to lock breakeven.
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos:
                return {'success': False, 'error': 'Position not found'}
            
            new_sl = pos.entry_price
            return self._update_sl(pos, new_sl, "TRAIL_TO_COST")
    
    def trail_by_points(self, symbol: str, points: float) -> dict:
        """
        One-click: Move SL up/down by X points from current SL.
        Positive = tighter SL (favorable), Negative = wider SL.
        
        For LONG: trail_by_points(+10) moves SL from 100 to 110
        For SHORT: trail_by_points(+10) moves SL from 100 to 90
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos:
                return {'success': False, 'error': 'Position not found'}
            
            if pos.side == 'LONG':
                new_sl = pos.current_sl + points
            else:
                new_sl = pos.current_sl - points
            
            return self._update_sl(pos, new_sl, f"TRAIL_+{points}pts")
    
    def trail_to_price(self, symbol: str, new_sl: float) -> dict:
        """
        Set SL to specific price.
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos:
                return {'success': False, 'error': 'Position not found'}
            
            return self._update_sl(pos, new_sl, f"TRAIL_TO_{new_sl}")
    
    def trail_to_ltp_minus(self, symbol: str, buffer_points: float, 
                          current_ltp: float) -> dict:
        """
        One-click: Move SL to (LTP - buffer).
        Useful for quick trailing in momentum.
        
        For LONG: SL = LTP - buffer
        For SHORT: SL = LTP + buffer
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos:
                return {'success': False, 'error': 'Position not found'}
            
            if pos.side == 'LONG':
                new_sl = current_ltp - buffer_points
                # Don't trail backwards
                if new_sl <= pos.current_sl:
                    return {'success': False, 'error': 'New SL not favorable'}
            else:
                new_sl = current_ltp + buffer_points
                if new_sl >= pos.current_sl:
                    return {'success': False, 'error': 'New SL not favorable'}
            
            return self._update_sl(pos, new_sl, f"TRAIL_LTP-{buffer_points}")
    
    # ==================== AUTO TRAIL METHODS ====================
    
    def start_auto_trail(self):
        """Start auto-trailing thread"""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._auto_trail_loop, daemon=True)
        self._thread.start()
        print("[TRAIL] Auto-trail started")
    
    def stop_auto_trail(self):
        """Stop auto-trailing"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        print("[TRAIL] Auto-trail stopped")
    
    def _auto_trail_loop(self):
        """Background loop for auto-trailing"""
        while self._running:
            try:
                self._process_auto_trails()
            except Exception as e:
                print(f"[TRAIL] Error: {e}")
            time.sleep(self._check_interval)
    
    def _process_auto_trails(self):
        """Process all positions for auto-trailing"""
        # Get current LTPs
        positions_to_check = []
        with self._lock:
            for pos in self.positions.values():
                if pos.trail_mode != TrailMode.MANUAL:
                    positions_to_check.append(pos)
        
        if not positions_to_check:
            return
        
        # Fetch LTPs
        tokens = [
            {"instrument_token": pos.symbol, "exchange_segment": pos.exchange_segment}
            for pos in positions_to_check
        ]
        
        try:
            quotes = self.client.quotes(instrument_tokens=tokens, quote_type="ltp")
            ltp_map = {q['instrument_token']: q['ltp'] for q in quotes.get('data', [])}
        except:
            return
        
        # Process each position
        with self._lock:
            for pos in positions_to_check:
                ltp = ltp_map.get(pos.symbol, pos.last_ltp)
                pos.last_ltp = ltp
                
                # Calculate current profit in points
                if pos.side == 'LONG':
                    profit_points = ltp - pos.entry_price
                else:
                    profit_points = pos.entry_price - ltp
                
                # Track peak profit
                if profit_points > pos.highest_profit:
                    pos.highest_profit = profit_points
                
                # Process based on mode
                if pos.trail_mode == TrailMode.LOCK_COST:
                    self._process_lock_cost(pos, profit_points)
                
                elif pos.trail_mode == TrailMode.AUTO_POINTS:
                    self._process_auto_points(pos, profit_points, ltp)
                
                elif pos.trail_mode == TrailMode.AUTO_PERCENT:
                    self._process_auto_percent(pos, profit_points, ltp)
    
    def _process_lock_cost(self, pos: TrailingPosition, profit_points: float):
        """Lock SL to cost when trigger profit reached"""
        if pos.cost_locked:
            return
        
        if profit_points >= pos.lock_cost_trigger:
            result = self._update_sl(pos, pos.entry_price, "AUTO_LOCK_COST")
            if result['success']:
                pos.cost_locked = True
                if self.on_cost_locked:
                    self.on_cost_locked(pos.symbol, pos.entry_price)
    
    def _process_auto_points(self, pos: TrailingPosition, 
                            profit_points: float, ltp: float):
        """
        Auto trail by fixed points.
        When profit increases by trail_points, move SL up by same amount.
        """
        if profit_points <= 0:
            return
        
        # Calculate how many trail increments we've achieved
        trail_increments = int(profit_points / pos.trail_points)
        
        if trail_increments > 0:
            if pos.side == 'LONG':
                new_sl = pos.entry_price + ((trail_increments - 1) * pos.trail_points)
            else:
                new_sl = pos.entry_price - ((trail_increments - 1) * pos.trail_points)
            
            # Only trail if new SL is better
            if pos.side == 'LONG' and new_sl > pos.current_sl:
                self._update_sl(pos, new_sl, f"AUTO_TRAIL_{trail_increments}x")
            elif pos.side == 'SHORT' and new_sl < pos.current_sl:
                self._update_sl(pos, new_sl, f"AUTO_TRAIL_{trail_increments}x")
    
    def _process_auto_percent(self, pos: TrailingPosition,
                             profit_points: float, ltp: float):
        """
        Auto trail by percentage of profit.
        SL = Entry + (profit * (1 - trail_percent))
        """
        if profit_points <= 0:
            return
        
        # Calculate trailing amount (lock X% of profit)
        lock_amount = profit_points * (1 - pos.trail_percent / 100)
        
        if pos.side == 'LONG':
            new_sl = pos.entry_price + lock_amount
            if new_sl > pos.current_sl:
                self._update_sl(pos, new_sl, f"AUTO_TRAIL_{pos.trail_percent}%")
        else:
            new_sl = pos.entry_price - lock_amount
            if new_sl < pos.current_sl:
                self._update_sl(pos, new_sl, f"AUTO_TRAIL_{pos.trail_percent}%")
    
    # ==================== CORE SL UPDATE ====================
    
    def _update_sl(self, pos: TrailingPosition, new_sl: float, 
                   reason: str) -> dict:
        """Actually modify the SL order"""
        try:
            # Round to tick size (0.05 for options)
            new_sl = round(new_sl / 0.05) * 0.05
            
            # Modify the SL order
            result = self.client.modify_order(
                order_id=pos.sl_order_id,
                trigger_price=str(new_sl),
                validity="DAY"
            )
            
            old_sl = pos.current_sl
            pos.current_sl = new_sl
            pos.trail_history.append({
                'time': time.time(),
                'old_sl': old_sl,
                'new_sl': new_sl,
                'reason': reason
            })
            
            print(f"[TRAIL] {pos.symbol}: SL {old_sl} → {new_sl} ({reason})")
            
            # Notify
            if self.sound:
                self.sound.play('order_placed')
            
            if self.on_sl_updated:
                self.on_sl_updated(pos.symbol, old_sl, new_sl, reason)
            
            return {'success': True, 'new_sl': new_sl, 'order_id': pos.sl_order_id}
            
        except Exception as e:
            print(f"[TRAIL] Failed: {e}")
            return {'success': False, 'error': str(e)}
    
    def get_position_info(self, symbol: str) -> Optional[dict]:
        """Get trailing position info for GUI"""
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos:
                return None
            
            return {
                'symbol': pos.symbol,
                'entry': pos.entry_price,
                'current_sl': pos.current_sl,
                'side': pos.side,
                'mode': pos.trail_mode.value,
                'cost_locked': pos.cost_locked,
                'highest_profit': pos.highest_profit,
                'trail_count': len(pos.trail_history)
            }
```

### GUI Integration for Trailing SL

Add these to the Positions table in `main_window.py`:

```python
# In create_positions_panel - add Trail column to actions

def create_position_actions(self, position: dict, row_idx: int) -> QWidget:
    """Create action buttons for position row"""
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(2, 2, 2, 2)
    layout.setSpacing(4)
    
    symbol = position.get('symbol', '')
    current_sl = float(position.get('sl_price', 0))
    
    # === SL PRICE INPUT (Editable) ===
    sl_input = QLineEdit()
    sl_input.setPlaceholderText("SL")
    sl_input.setText(f"{current_sl:.2f}" if current_sl > 0 else "")
    sl_input.setMaximumWidth(70)
    sl_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
    sl_input.setStyleSheet("""
        QLineEdit {
            background-color: #1a1a2e;
            border: 1px solid #444466;
            border-radius: 3px;
            color: #ffaa00;
            font-weight: bold;
        }
        QLineEdit:focus {
            border: 1px solid #ff6600;
        }
    """)
    # Update SL on Enter key
    sl_input.returnPressed.connect(
        lambda: self.update_sl_from_input(symbol, sl_input.text())
    )
    layout.addWidget(sl_input)
    
    # Store reference for updates
    self.sl_inputs[symbol] = sl_input
    
    # === SET SL BUTTON (applies the input value) ===
    set_sl_btn = QPushButton("SET")
    set_sl_btn.setToolTip("Set SL to entered price")
    set_sl_btn.setMaximumWidth(35)
    set_sl_btn.setStyleSheet("background-color: #663300;")
    set_sl_btn.clicked.connect(
        lambda: self.update_sl_from_input(symbol, sl_input.text())
    )
    layout.addWidget(set_sl_btn)
    
    # Separator
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.VLine)
    sep.setMaximumWidth(2)
    layout.addWidget(sep)
    
    # === QUICK TRAIL BUTTONS ===
    
    # Trail to Cost (Breakeven)
    trail_cost_btn = QPushButton("⚡BE")
    trail_cost_btn.setToolTip("Trail SL to Breakeven (Cost)")
    trail_cost_btn.setMaximumWidth(35)
    trail_cost_btn.setStyleSheet("background-color: #004466;")
    trail_cost_btn.clicked.connect(
        lambda: self.trail_to_cost(symbol)
    )
    layout.addWidget(trail_cost_btn)
    
    # Trail +10 points
    trail_10_btn = QPushButton("+10")
    trail_10_btn.setToolTip("Trail SL by +10 points")
    trail_10_btn.setMaximumWidth(35)
    trail_10_btn.setStyleSheet("background-color: #005544;")
    trail_10_btn.clicked.connect(
        lambda: self.trail_by_points(symbol, 10)
    )
    layout.addWidget(trail_10_btn)
    
    # Trail +25 points
    trail_25_btn = QPushButton("+25")
    trail_25_btn.setToolTip("Trail SL by +25 points")
    trail_25_btn.setMaximumWidth(35)
    trail_25_btn.setStyleSheet("background-color: #006633;")
    trail_25_btn.clicked.connect(
        lambda: self.trail_by_points(symbol, 25)
    )
    layout.addWidget(trail_25_btn)
    
    # Trail to LTP-20
    trail_ltp_btn = QPushButton("📍-20")
    trail_ltp_btn.setToolTip("Trail SL to LTP minus 20")
    trail_ltp_btn.setMaximumWidth(45)
    trail_ltp_btn.setStyleSheet("background-color: #664400;")
    trail_ltp_btn.clicked.connect(
        lambda: self.trail_to_ltp_minus(symbol, 20)
    )
    layout.addWidget(trail_ltp_btn)
    
    # Separator
    layout.addWidget(self.create_separator())
    
    # === EXIT BUTTONS ===
    exit_50 = QPushButton("50%")
    exit_50.setMaximumWidth(40)
    exit_50.clicked.connect(lambda: self.exit_position_partial(position, 50))
    layout.addWidget(exit_50)
    
    exit_full = QPushButton("EXIT")
    exit_full.setStyleSheet("background-color: #880000; color: white;")
    exit_full.setMaximumWidth(50)
    exit_full.clicked.connect(lambda: self.exit_position_partial(position, 100))
    layout.addWidget(exit_full)
    
    return widget

# In __init__, add:
self.sl_inputs: Dict[str, QLineEdit] = {}  # Store SL input references

# Manual SL update method
def update_sl_from_input(self, symbol: str, price_str: str):
    """Update SL to manually entered price"""
    try:
        new_sl = float(price_str.strip())
        if new_sl <= 0:
            self.log_message.emit(f"[SL] Invalid price: {price_str}")
            return
        
        result = self.trail_mgr.trail_to_price(symbol, new_sl)
        
        if result['success']:
            self.log_message.emit(f"[SL] {symbol}: SL → {new_sl:.2f} ✓")
            self.sound.play('order_placed')
            
            # Update input field styling to show success
            if symbol in self.sl_inputs:
                self.sl_inputs[symbol].setStyleSheet("""
                    QLineEdit {
                        background-color: #1a3a1a;
                        border: 1px solid #00aa00;
                        border-radius: 3px;
                        color: #00ff00;
                        font-weight: bold;
                    }
                """)
                # Reset style after 1 second
                QTimer.singleShot(1000, lambda: self._reset_sl_input_style(symbol))
        else:
            self.log_message.emit(f"[SL] {symbol}: Failed - {result['error']}")
            # Show error styling
            if symbol in self.sl_inputs:
                self.sl_inputs[symbol].setStyleSheet("""
                    QLineEdit {
                        background-color: #3a1a1a;
                        border: 1px solid #aa0000;
                        color: #ff4444;
                    }
                """)
                QTimer.singleShot(1000, lambda: self._reset_sl_input_style(symbol))
                
    except ValueError:
        self.log_message.emit(f"[SL] Invalid price format: {price_str}")

def _reset_sl_input_style(self, symbol: str):
    """Reset SL input to default style"""
    if symbol in self.sl_inputs:
        self.sl_inputs[symbol].setStyleSheet("""
            QLineEdit {
                background-color: #1a1a2e;
                border: 1px solid #444466;
                border-radius: 3px;
                color: #ffaa00;
                font-weight: bold;
            }
        """)

# Update SL inputs when positions refresh
def update_positions_table(self, positions):
    """Update positions table with data"""
    # ... existing code ...
    
    # Update SL input fields with current values
    for pos in positions:
        symbol = pos.get('symbol', '')
        if symbol in self.sl_inputs:
            trail_info = self.trail_mgr.get_position_info(symbol)
            if trail_info:
                current_sl = trail_info.get('current_sl', 0)
                self.sl_inputs[symbol].setText(f"{current_sl:.2f}")

# Trail action methods
def trail_to_cost(self, symbol: str):
    """Trail SL to breakeven"""
    result = self.trail_mgr.trail_to_cost(symbol)
    if result['success']:
        self.log_message.emit(f"[TRAIL] {symbol}: SL moved to COST ({result['new_sl']:.2f}) ✓")
        self.sound.play('order_placed')
        # Update the input field
        if symbol in self.sl_inputs:
            self.sl_inputs[symbol].setText(f"{result['new_sl']:.2f}")
    else:
        self.log_message.emit(f"[TRAIL] {symbol}: Failed - {result['error']}")

def trail_by_points(self, symbol: str, points: float):
    """Trail SL by points"""
    result = self.trail_mgr.trail_by_points(symbol, points)
    if result['success']:
        self.log_message.emit(f"[TRAIL] {symbol}: SL → {result['new_sl']:.2f} (+{points}pts) ✓")
        self.sound.play('order_placed')
        # Update the input field
        if symbol in self.sl_inputs:
            self.sl_inputs[symbol].setText(f"{result['new_sl']:.2f}")
    else:
        self.log_message.emit(f"[TRAIL] {symbol}: Failed - {result['error']}")

def trail_to_ltp_minus(self, symbol: str, buffer: float):
    """Trail SL to LTP minus buffer"""
    # Get current LTP from position
    pos = self.get_position_by_symbol(symbol)
    if pos:
        ltp = float(pos.get('ltp', 0))
        result = self.trail_mgr.trail_to_ltp_minus(symbol, buffer, ltp)
        if result['success']:
            self.log_message.emit(f"[TRAIL] {symbol}: SL → {result['new_sl']:.2f} (LTP-{buffer}) ✓")
            self.sound.play('order_placed')
            # Update the input field
            if symbol in self.sl_inputs:
                self.sl_inputs[symbol].setText(f"{result['new_sl']:.2f}")
        else:
            self.log_message.emit(f"[TRAIL] {symbol}: {result['error']}")
```

### Keyboard Shortcuts for Trailing

```python
# Add to setup_shortcuts()
QShortcut(QKeySequence("T"), self, self.trail_selected_to_cost)      # Trail to cost
QShortcut(QKeySequence("Shift+T"), self, self.trail_selected_plus_10) # Trail +10
QShortcut(QKeySequence("Ctrl+T"), self, self.focus_sl_input)          # Focus SL input

def trail_selected_to_cost(self):
    """Trail selected position to cost"""
    selected = self.positions_table.currentRow()
    if selected >= 0:
        symbol = self.positions_table.item(selected, 0).text()
        self.trail_to_cost(symbol)

def trail_selected_plus_10(self):
    """Trail selected position +10 points"""
    selected = self.positions_table.currentRow()
    if selected >= 0:
        symbol = self.positions_table.item(selected, 0).text()
        self.trail_by_points(symbol, 10)

def focus_sl_input(self):
    """Focus SL input of selected position for manual entry"""
    selected = self.positions_table.currentRow()
    if selected >= 0:
        symbol = self.positions_table.item(selected, 0).text()
        if symbol in self.sl_inputs:
            self.sl_inputs[symbol].setFocus()
            self.sl_inputs[symbol].selectAll()
```

### Updated Position Row Display

```
┌─ POSITIONS ────────────────────────────────────────────────────────────────────────────────────────┐
│ Symbol              Qty   Avg     LTP     P&L      %     SL Input      Trail           Exit       │
│ ─────────────────────────────────────────────────────────────────────────────────────────────────  │
│ NIFTY25JAN24000CE   +75  180.00  210.00  +2250  +16.7%  [190.00][SET] [⚡BE][+10][+25][📍] [50%][EXIT] │
│ BANKNIFTY25JAN52PE  -15  220.00  195.00  +375   +11.4%  [210.00][SET] [⚡BE][+10][+25][📍] [50%][EXIT] │
│ ─────────────────────────────────────────────────────────────────────────────────────────────────  │
│ TOTAL P&L: ₹2,625                                                                                  │
└────────────────────────────────────────────────────────────────────────────────────────────────────┘

SL Update Methods:
1. Type price in [190.00] box → Press Enter OR click [SET]
2. Click [⚡BE] to trail to breakeven
3. Click [+10] or [+25] to trail by points
4. Click [📍-20] to trail to LTP minus 20
5. Press Ctrl+T to focus input, type price, Enter

Visual Feedback:
- Green border flash = SL updated successfully
- Red border flash = Update failed
- Orange text = Current SL value
```

### Module 9: OCO Position Monitor (`core/oco_monitor.py`)

```python
"""
Monitors positions with SL/Target orders.
Auto-cancels opposite leg when one is hit.
Since NEO doesn't have native OCO, we simulate it.
"""

import threading
import time
from typing import Dict, List, Optional
from dataclasses import dataclass

@dataclass
class OCOPair:
    """Represents an OCO pair (SL + Target)"""
    position_symbol: str
    sl_order_id: str
    target_order_id: str
    sl_trigger: float
    target_price: float
    quantity: int
    side: str  # 'LONG' or 'SHORT'
    created_at: float

class OCOMonitor:
    def __init__(self, neo_client, telegram: Optional['TelegramNotifier'] = None,
                 sound: Optional['SoundAlertManager'] = None):
        self.client = neo_client
        self.telegram = telegram
        self.sound = sound
        
        self.oco_pairs: Dict[str, OCOPair] = {}  # keyed by position symbol
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._check_interval = 1  # seconds
    
    def add_oco_pair(self, position_symbol: str, sl_order_id: str, 
                     target_order_id: str, sl_trigger: float,
                     target_price: float, quantity: int, side: str):
        """Register an OCO pair for monitoring"""
        with self._lock:
            pair = OCOPair(
                position_symbol=position_symbol,
                sl_order_id=sl_order_id,
                target_order_id=target_order_id,
                sl_trigger=sl_trigger,
                target_price=target_price,
                quantity=quantity,
                side=side,
                created_at=time.time()
            )
            self.oco_pairs[position_symbol] = pair
            print(f"[OCO] Registered: {position_symbol} SL:{sl_order_id} TGT:{target_order_id}")
    
    def remove_oco_pair(self, position_symbol: str):
        """Remove OCO pair from monitoring"""
        with self._lock:
            if position_symbol in self.oco_pairs:
                del self.oco_pairs[position_symbol]
                print(f"[OCO] Removed: {position_symbol}")
    
    def start(self):
        """Start monitoring thread"""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        print("[OCO] Monitor started")
    
    def stop(self):
        """Stop monitoring thread"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        print("[OCO] Monitor stopped")
    
    def _monitor_loop(self):
        """Main monitoring loop"""
        while self._running:
            try:
                self._check_orders()
            except Exception as e:
                print(f"[OCO] Error in monitor loop: {e}")
            
            time.sleep(self._check_interval)
    
    def _check_orders(self):
        """Check status of all OCO pairs"""
        if not self.oco_pairs:
            return
        
        try:
            # Get all orders
            order_report = self.client.order_report()
            orders = {o['nOrdNo']: o for o in order_report.get('data', [])}
            
            with self._lock:
                pairs_to_remove = []
                
                for symbol, pair in self.oco_pairs.items():
                    sl_order = orders.get(pair.sl_order_id)
                    target_order = orders.get(pair.target_order_id)
                    
                    sl_status = sl_order.get('ordSt', '').lower() if sl_order else 'unknown'
                    target_status = target_order.get('ordSt', '').lower() if target_order else 'unknown'
                    
                    # Check if SL hit
                    if sl_status in ['complete', 'traded']:
                        self._handle_sl_hit(pair, sl_order)
                        pairs_to_remove.append(symbol)
                    
                    # Check if Target hit
                    elif target_status in ['complete', 'traded']:
                        self._handle_target_hit(pair, target_order)
                        pairs_to_remove.append(symbol)
                    
                    # Check if either cancelled externally
                    elif sl_status == 'cancelled' and target_status not in ['complete', 'traded']:
                        print(f"[OCO] SL cancelled for {symbol}, keeping target")
                        pairs_to_remove.append(symbol)
                    
                    elif target_status == 'cancelled' and sl_status not in ['complete', 'traded']:
                        print(f"[OCO] Target cancelled for {symbol}, keeping SL")
                        pairs_to_remove.append(symbol)
                
                for symbol in pairs_to_remove:
                    del self.oco_pairs[symbol]
                    
        except Exception as e:
            print(f"[OCO] Check error: {e}")
    
    def _handle_sl_hit(self, pair: OCOPair, sl_order: dict):
        """Handle SL order hit - cancel target"""
        print(f"[OCO] SL HIT for {pair.position_symbol}")
        
        # Cancel target order
        try:
            self.client.cancel_order(order_id=pair.target_order_id)
            print(f"[OCO] Cancelled target order {pair.target_order_id}")
        except Exception as e:
            print(f"[OCO] Failed to cancel target: {e}")
        
        # Notify
        fill_price = float(sl_order.get('avgPrc', pair.sl_trigger))
        
        if self.sound:
            self.sound.play('sl_hit')
        
        if self.telegram:
            # Calculate P&L (would need entry price)
            self.telegram.notify_sl_hit(
                symbol=pair.position_symbol,
                exit_price=fill_price,
                pnl=0,  # Would need entry price to calculate
                pnl_pct=0
            )
    
    def _handle_target_hit(self, pair: OCOPair, target_order: dict):
        """Handle target order hit - cancel SL"""
        print(f"[OCO] TARGET HIT for {pair.position_symbol}")
        
        # Cancel SL order
        try:
            self.client.cancel_order(order_id=pair.sl_order_id)
            print(f"[OCO] Cancelled SL order {pair.sl_order_id}")
        except Exception as e:
            print(f"[OCO] Failed to cancel SL: {e}")
        
        # Notify
        fill_price = float(target_order.get('avgPrc', pair.target_price))
        
        if self.sound:
            self.sound.play('target_hit')
        
        if self.telegram:
            self.telegram.notify_target_hit(
                symbol=pair.position_symbol,
                exit_price=fill_price,
                pnl=0,
                pnl_pct=0
            )
    
    def get_active_pairs(self) -> List[dict]:
        """Get list of active OCO pairs"""
        with self._lock:
            return [
                {
                    'symbol': p.position_symbol,
                    'sl_order': p.sl_order_id,
                    'target_order': p.target_order_id,
                    'sl_price': p.sl_trigger,
                    'target_price': p.target_price,
                }
                for p in self.oco_pairs.values()
            ]
```

### Module 9: WebSocket Handler (`core/websocket_handler.py`)

```python
"""
Handles live market data and order updates via WebSocket.
"""

from typing import Callable, List, Dict
import threading

class WebSocketHandler:
    def __init__(self, neo_client):
        self.client = neo_client
        self.subscribed_tokens = []
        self.callbacks = {
            'ltp_update': [],
            'order_update': [],
            'position_update': []
        }
        self._running = False
        
    def setup_callbacks(self):
        """Setup NEO websocket callbacks"""
        self.client.on_message = self._on_message
        self.client.on_error = self._on_error
        self.client.on_close = self._on_close
        self.client.on_open = self._on_open
    
    def subscribe_ltp(self, tokens: List[Dict]) -> bool:
        """
        Subscribe to live LTP feed for tokens.
        
        Args:
            tokens: List of {'instrument_token': str, 'exchange_segment': str}
        """
        try:
            self.client.subscribe(instrument_tokens=tokens, isIndex=False, isDepth=False)
            self.subscribed_tokens.extend(tokens)
            return True
        except Exception as e:
            print(f"Subscription failed: {e}")
            return False
    
    def subscribe_order_feed(self):
        """Subscribe to order status updates"""
        try:
            self.client.subscribe_to_orderfeed()
            return True
        except Exception as e:
            print(f"Order feed subscription failed: {e}")
            return False
    
    def register_callback(self, event_type: str, callback: Callable):
        """Register callback for specific event type"""
        if event_type in self.callbacks:
            self.callbacks[event_type].append(callback)
    
    def _on_message(self, message):
        """Handle incoming websocket message"""
        # Parse message type and route to appropriate callbacks
        if 'ltp' in message:
            for cb in self.callbacks['ltp_update']:
                cb(message)
        elif 'order' in message:
            for cb in self.callbacks['order_update']:
                cb(message)
    
    def _on_error(self, error):
        print(f"WebSocket error: {error}")
    
    def _on_close(self, message):
        print(f"WebSocket closed: {message}")
        self._running = False
    
    def _on_open(self, message):
        print(f"WebSocket connected: {message}")
        self._running = True
    
    def unsubscribe_all(self):
        """Unsubscribe from all feeds"""
        if self.subscribed_tokens:
            self.client.un_subscribe(instrument_tokens=self.subscribed_tokens)
            self.subscribed_tokens = []
```

---

## GUI Design (PyQt6)

### Main Window Layout

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│  NEO TRADE TERMINAL                                           [_] [□] [X]            │
├──────────────────────────────────────────────────────────────────────────────────────┤
│ ┌─ STATUS BAR ────────────────────────────────────────────────────────────────────┐  │
│ │ 🟢 CONNECTED │ Margin: ₹2,45,000 │ Day P&L: +₹3,200 │ Time: 09:35:22          │  │
│ └─────────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                      │
│ ┌─ QUICK ENTRY PANEL ─────────────────────────────────────────────────────────────┐  │
│ │                                                                                 │  │
│ │  Symbol: [NIFTY25JAN24000CE________________] [SEARCH]  Lots: [1] Qty: 75       │  │
│ │                                                                                 │  │
│ │  Action: (●) BUY  ( ) SELL     Product: [MIS ▼]     Type: [LIMIT ▼]           │  │
│ │                                                                                 │  │
│ │  Price: [250.00____]   SL: [225.00____]   Target: [300.00____]                │  │
│ │                                                                                 │  │
│ │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐                │  │
│ │  │   🟢 BUY NOW    │  │   🔴 SELL NOW   │  │  📦 ADD TO      │                │  │
│ │  │   (Ctrl+B)      │  │   (Ctrl+S)      │  │     BASKET      │                │  │
│ │  └─────────────────┘  └─────────────────┘  └─────────────────┘                │  │
│ │                                                                                 │  │
│ │  [ ] Bracket Order (SL+Target auto)    [PREVIEW MARGIN: ₹12,500]              │  │
│ │                                                                                 │  │
│ └─────────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                      │
│ ┌─ POSITIONS ──────────────────────────────────────────────────────────────────────┐ │
│ │ Symbol              Qty    Avg      LTP      P&L       %     Actions            │ │
│ │ ─────────────────────────────────────────────────────────────────────────────── │ │
│ │ NIFTY25JAN24000CE   +75   180.00   195.50   +1162   +8.6%   [25%][50%][EXIT]   │ │
│ │ BANKNIFTY25JAN52PE  -15   220.00   205.00   +225    +6.8%   [SL/T][MOD][EXIT]  │ │
│ │ ─────────────────────────────────────────────────────────────────────────────── │ │
│ │ TOTAL P&L: ₹1,387                                    [EXIT ALL] [REFRESH]      │ │
│ └────────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                      │
│ ┌─ BASKET ORDER ─────────────────────────┐ ┌─ ORDER BOOK ─────────────────────────┐ │
│ │                                        │ │                                      │ │
│ │ Leg 1: NIFTY CE BUY  75 @ 180         │ │ ID      Symbol      Status    Time   │ │
│ │ Leg 2: NIFTY PE SELL 75 @ 95          │ │ 12345   NIFTY CE    COMPLETE  09:31  │ │
│ │                                        │ │ 12346   NIFTY PE    PENDING   09:32  │ │
│ │ Net Premium: ₹85 (Credit)              │ │ 12347   BANK PE     REJECTED  09:33  │ │
│ │ Max Margin: ₹45,000                    │ │                                      │ │
│ │                                        │ │                                      │ │
│ │ [ADD LEG] [CLEAR] [EXECUTE BASKET]     │ │ [CANCEL PENDING] [VIEW HISTORY]     │ │
│ └────────────────────────────────────────┘ └──────────────────────────────────────┘ │
│                                                                                      │
│ ┌─ QUICK ACTIONS ──────────────────────────────────────────────────────────────────┐ │
│ │  [F1: NIFTY CE ATM] [F2: NIFTY PE ATM] [F3: BANK CE ATM] [F4: BANK PE ATM]     │ │
│ │  [F5: +1 LOT]       [F6: -1 LOT]       [F9: EXIT ALL]    [F10: CANCEL ALL]     │ │
│ └──────────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                      │
│ ┌─ LOG ────────────────────────────────────────────────────────────────────────────┐ │
│ │ 09:35:22 [ORDER] NIFTY25JAN24000CE BUY 75 @ 180.00 → Order ID: 12345 ✓         │ │
│ │ 09:35:23 [FILL]  Order 12345 executed @ 180.25                                  │ │
│ │ 09:36:01 [INFO]  Position P&L updated: +₹1,162                                  │ │
│ └──────────────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

### Module 6: GUI Implementation (`gui/main_window.py`)

```python
"""
Main GUI window using PyQt6.
Clean, dark theme optimized for trading.
"""

import sys
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox, QTableWidget,
    QTableWidgetItem, QGroupBox, QTextEdit, QRadioButton, QButtonGroup,
    QFrame, QSplitter, QStatusBar, QHeaderView, QSpinBox
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread
from PyQt6.QtGui import QFont, QColor, QKeySequence, QShortcut

class TradingTerminal(QMainWindow):
    # Signals for thread-safe GUI updates
    position_updated = pyqtSignal(list)
    order_updated = pyqtSignal(dict)
    log_message = pyqtSignal(str)
    
    def __init__(self, session_mgr, order_mgr, symbol_mapper, config):
        super().__init__()
        self.session = session_mgr
        self.orders = order_mgr
        self.mapper = symbol_mapper
        self.config = config
        
        self.basket_legs = []  # For multi-leg orders
        
        self.init_ui()
        self.setup_shortcuts()
        self.setup_timers()
        self.connect_signals()
        
    def init_ui(self):
        self.setWindowTitle("NEO Trade Terminal v1.0")
        self.setMinimumSize(1200, 800)
        self.setStyleSheet(self.get_dark_theme())
        
        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Status bar at top
        layout.addWidget(self.create_status_bar())
        
        # Quick entry panel
        layout.addWidget(self.create_quick_entry_panel())
        
        # Splitter for positions and order book
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.create_positions_panel())
        
        # Right side: basket + order book
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.addWidget(self.create_basket_panel())
        right_layout.addWidget(self.create_order_book_panel())
        splitter.addWidget(right_widget)
        splitter.setSizes([700, 500])
        
        layout.addWidget(splitter)
        
        # Quick action buttons
        layout.addWidget(self.create_quick_actions_panel())
        
        # Log panel
        layout.addWidget(self.create_log_panel())
        
    def create_status_bar(self) -> QFrame:
        """Create top status bar with connection, margin, P&L info"""
        frame = QFrame()
        frame.setFrameStyle(QFrame.Shape.StyledPanel)
        frame.setMaximumHeight(40)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 5, 10, 5)
        
        # Connection status
        self.conn_status = QLabel("🟢 CONNECTED")
        self.conn_status.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        layout.addWidget(self.conn_status)
        
        layout.addWidget(self.create_separator())
        
        # Margin display
        layout.addWidget(QLabel("Margin:"))
        self.margin_label = QLabel("₹0")
        self.margin_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.margin_label.setStyleSheet("color: #00ff88;")
        layout.addWidget(self.margin_label)
        
        layout.addWidget(self.create_separator())
        
        # Day P&L
        layout.addWidget(QLabel("Day P&L:"))
        self.pnl_label = QLabel("₹0")
        self.pnl_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        layout.addWidget(self.pnl_label)
        
        layout.addWidget(self.create_separator())
        
        # Time
        self.time_label = QLabel("00:00:00")
        layout.addWidget(self.time_label)
        
        layout.addStretch()
        
        return frame
    
    def create_quick_entry_panel(self) -> QGroupBox:
        """Create the main order entry panel"""
        group = QGroupBox("QUICK ENTRY")
        layout = QVBoxLayout(group)
        
        # Row 1: Symbol input
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Symbol:"))
        self.symbol_input = QLineEdit()
        self.symbol_input.setPlaceholderText("Paste Kite symbol: NIFTY25JAN24000CE")
        self.symbol_input.setMinimumWidth(350)
        self.symbol_input.returnPressed.connect(self.on_symbol_entered)
        row1.addWidget(self.symbol_input)
        
        self.search_btn = QPushButton("🔍 SEARCH")
        self.search_btn.clicked.connect(self.on_search_symbol)
        row1.addWidget(self.search_btn)
        
        row1.addWidget(self.create_separator())
        
        row1.addWidget(QLabel("Lots:"))
        self.lots_spin = QSpinBox()
        self.lots_spin.setRange(1, 100)
        self.lots_spin.setValue(self.config['trading_defaults']['default_lots'])
        self.lots_spin.valueChanged.connect(self.update_quantity)
        row1.addWidget(self.lots_spin)
        
        row1.addWidget(QLabel("Qty:"))
        self.qty_label = QLabel("0")
        self.qty_label.setMinimumWidth(50)
        self.qty_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        row1.addWidget(self.qty_label)
        
        row1.addStretch()
        layout.addLayout(row1)
        
        # Row 2: Action, Product, Type
        row2 = QHBoxLayout()
        
        row2.addWidget(QLabel("Action:"))
        self.action_group = QButtonGroup()
        self.buy_radio = QRadioButton("BUY")
        self.buy_radio.setChecked(True)
        self.buy_radio.setStyleSheet("QRadioButton { color: #00ff88; font-weight: bold; }")
        self.sell_radio = QRadioButton("SELL")
        self.sell_radio.setStyleSheet("QRadioButton { color: #ff4444; font-weight: bold; }")
        self.action_group.addButton(self.buy_radio)
        self.action_group.addButton(self.sell_radio)
        row2.addWidget(self.buy_radio)
        row2.addWidget(self.sell_radio)
        
        row2.addWidget(self.create_separator())
        
        row2.addWidget(QLabel("Product:"))
        self.product_combo = QComboBox()
        self.product_combo.addItems(["MIS", "NRML"])
        row2.addWidget(self.product_combo)
        
        row2.addWidget(self.create_separator())
        
        row2.addWidget(QLabel("Type:"))
        self.order_type_combo = QComboBox()
        self.order_type_combo.addItems(["LIMIT", "MARKET", "SL", "SL-M"])
        self.order_type_combo.currentTextChanged.connect(self.on_order_type_changed)
        row2.addWidget(self.order_type_combo)
        
        row2.addStretch()
        layout.addLayout(row2)
        
        # Row 3: Price, SL, Target
        row3 = QHBoxLayout()
        
        row3.addWidget(QLabel("Price:"))
        self.price_input = QLineEdit()
        self.price_input.setPlaceholderText("0.00")
        self.price_input.setMaximumWidth(100)
        row3.addWidget(self.price_input)
        
        row3.addWidget(QLabel("SL:"))
        self.sl_input = QLineEdit()
        self.sl_input.setPlaceholderText("Optional")
        self.sl_input.setMaximumWidth(100)
        row3.addWidget(self.sl_input)
        
        row3.addWidget(QLabel("Target:"))
        self.target_input = QLineEdit()
        self.target_input.setPlaceholderText("Optional")
        self.target_input.setMaximumWidth(100)
        row3.addWidget(self.target_input)
        
        row3.addStretch()
        layout.addLayout(row3)
        
        # Row 4: Action buttons
        row4 = QHBoxLayout()
        
        self.buy_btn = QPushButton("🟢 BUY NOW\n(Ctrl+B)")
        self.buy_btn.setMinimumSize(150, 50)
        self.buy_btn.setStyleSheet("""
            QPushButton {
                background-color: #006644;
                color: white;
                font-weight: bold;
                font-size: 14px;
                border-radius: 5px;
            }
            QPushButton:hover { background-color: #008855; }
            QPushButton:pressed { background-color: #004433; }
        """)
        self.buy_btn.clicked.connect(lambda: self.place_quick_order('B'))
        row4.addWidget(self.buy_btn)
        
        self.sell_btn = QPushButton("🔴 SELL NOW\n(Ctrl+S)")
        self.sell_btn.setMinimumSize(150, 50)
        self.sell_btn.setStyleSheet("""
            QPushButton {
                background-color: #880000;
                color: white;
                font-weight: bold;
                font-size: 14px;
                border-radius: 5px;
            }
            QPushButton:hover { background-color: #aa0000; }
            QPushButton:pressed { background-color: #660000; }
        """)
        self.sell_btn.clicked.connect(lambda: self.place_quick_order('S'))
        row4.addWidget(self.sell_btn)
        
        self.basket_btn = QPushButton("📦 ADD TO\nBASKET")
        self.basket_btn.setMinimumSize(150, 50)
        self.basket_btn.setStyleSheet("""
            QPushButton {
                background-color: #444488;
                color: white;
                font-weight: bold;
                font-size: 14px;
                border-radius: 5px;
            }
            QPushButton:hover { background-color: #5555aa; }
        """)
        self.basket_btn.clicked.connect(self.add_to_basket)
        row4.addWidget(self.basket_btn)
        
        row4.addStretch()
        
        # Bracket order checkbox
        self.bracket_check = QCheckBox("Bracket Order (SL+Target auto)")
        row4.addWidget(self.bracket_check)
        
        # Margin preview
        row4.addWidget(QLabel("Est. Margin:"))
        self.margin_preview = QLabel("₹0")
        self.margin_preview.setStyleSheet("color: #ffaa00; font-weight: bold;")
        row4.addWidget(self.margin_preview)
        
        layout.addLayout(row4)
        
        return group
    
    def create_positions_panel(self) -> QGroupBox:
        """Create positions table with action buttons"""
        group = QGroupBox("POSITIONS")
        layout = QVBoxLayout(group)
        
        # Positions table
        self.positions_table = QTableWidget()
        self.positions_table.setColumnCount(8)
        self.positions_table.setHorizontalHeaderLabels([
            "Symbol", "Qty", "Avg", "LTP", "P&L", "%", "Actions", ""
        ])
        self.positions_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.positions_table.setAlternatingRowColors(True)
        layout.addWidget(self.positions_table)
        
        # Bottom row
        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("TOTAL P&L:"))
        self.total_pnl = QLabel("₹0")
        self.total_pnl.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        bottom.addWidget(self.total_pnl)
        bottom.addStretch()
        
        self.exit_all_btn = QPushButton("🚫 EXIT ALL")
        self.exit_all_btn.setStyleSheet("background-color: #aa0000; color: white; font-weight: bold;")
        self.exit_all_btn.clicked.connect(self.exit_all_positions)
        bottom.addWidget(self.exit_all_btn)
        
        self.refresh_btn = QPushButton("🔄 REFRESH")
        self.refresh_btn.clicked.connect(self.refresh_positions)
        bottom.addWidget(self.refresh_btn)
        
        layout.addLayout(bottom)
        
        return group
    
    def create_basket_panel(self) -> QGroupBox:
        """Create basket order builder for multi-leg strategies"""
        group = QGroupBox("BASKET ORDER")
        group.setMaximumHeight(200)
        layout = QVBoxLayout(group)
        
        # Basket legs table
        self.basket_table = QTableWidget()
        self.basket_table.setColumnCount(5)
        self.basket_table.setHorizontalHeaderLabels(["Leg", "Symbol", "Action", "Qty", "Price"])
        self.basket_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.basket_table)
        
        # Summary row
        summary = QHBoxLayout()
        summary.addWidget(QLabel("Net Premium:"))
        self.net_premium = QLabel("₹0")
        self.net_premium.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        summary.addWidget(self.net_premium)
        
        summary.addWidget(QLabel("Max Margin:"))
        self.basket_margin = QLabel("₹0")
        summary.addWidget(self.basket_margin)
        
        summary.addStretch()
        
        self.clear_basket_btn = QPushButton("🗑️ CLEAR")
        self.clear_basket_btn.clicked.connect(self.clear_basket)
        summary.addWidget(self.clear_basket_btn)
        
        self.execute_basket_btn = QPushButton("⚡ EXECUTE BASKET")
        self.execute_basket_btn.setStyleSheet("background-color: #006644; color: white; font-weight: bold;")
        self.execute_basket_btn.clicked.connect(self.execute_basket)
        summary.addWidget(self.execute_basket_btn)
        
        layout.addLayout(summary)
        
        return group
    
    def create_order_book_panel(self) -> QGroupBox:
        """Create order book display"""
        group = QGroupBox("ORDER BOOK")
        layout = QVBoxLayout(group)
        
        self.orders_table = QTableWidget()
        self.orders_table.setColumnCount(5)
        self.orders_table.setHorizontalHeaderLabels(["ID", "Symbol", "Type", "Status", "Time"])
        self.orders_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.orders_table)
        
        # Action buttons
        actions = QHBoxLayout()
        self.cancel_pending_btn = QPushButton("❌ CANCEL PENDING")
        self.cancel_pending_btn.clicked.connect(self.cancel_pending_orders)
        actions.addWidget(self.cancel_pending_btn)
        
        actions.addStretch()
        layout.addLayout(actions)
        
        return group
    
    def create_quick_actions_panel(self) -> QFrame:
        """Create quick action buttons with keyboard shortcuts"""
        frame = QFrame()
        frame.setFrameStyle(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(frame)
        
        # Preset buttons
        self.f1_btn = QPushButton("F1: NIFTY CE ATM")
        self.f1_btn.clicked.connect(lambda: self.load_preset('NIFTY', 'CE', 'ATM'))
        layout.addWidget(self.f1_btn)
        
        self.f2_btn = QPushButton("F2: NIFTY PE ATM")
        self.f2_btn.clicked.connect(lambda: self.load_preset('NIFTY', 'PE', 'ATM'))
        layout.addWidget(self.f2_btn)
        
        self.f3_btn = QPushButton("F3: BANK CE ATM")
        self.f3_btn.clicked.connect(lambda: self.load_preset('BANKNIFTY', 'CE', 'ATM'))
        layout.addWidget(self.f3_btn)
        
        self.f4_btn = QPushButton("F4: BANK PE ATM")
        self.f4_btn.clicked.connect(lambda: self.load_preset('BANKNIFTY', 'PE', 'ATM'))
        layout.addWidget(self.f4_btn)
        
        layout.addWidget(self.create_separator())
        
        self.f5_btn = QPushButton("F5: +1 LOT")
        self.f5_btn.clicked.connect(lambda: self.adjust_lots(1))
        layout.addWidget(self.f5_btn)
        
        self.f6_btn = QPushButton("F6: -1 LOT")
        self.f6_btn.clicked.connect(lambda: self.adjust_lots(-1))
        layout.addWidget(self.f6_btn)
        
        layout.addWidget(self.create_separator())
        
        self.f9_btn = QPushButton("F9: EXIT ALL")
        self.f9_btn.setStyleSheet("background-color: #aa0000; color: white;")
        self.f9_btn.clicked.connect(self.exit_all_positions)
        layout.addWidget(self.f9_btn)
        
        self.f10_btn = QPushButton("F10: CANCEL ALL")
        self.f10_btn.clicked.connect(self.cancel_all_orders)
        layout.addWidget(self.f10_btn)
        
        return frame
    
    def create_log_panel(self) -> QGroupBox:
        """Create log display panel"""
        group = QGroupBox("LOG")
        group.setMaximumHeight(120)
        layout = QVBoxLayout(group)
        
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 9))
        layout.addWidget(self.log_text)
        
        return group
    
    def setup_shortcuts(self):
        """Setup keyboard shortcuts"""
        # Buy/Sell shortcuts
        QShortcut(QKeySequence("Ctrl+B"), self, self.buy_btn.click)
        QShortcut(QKeySequence("Ctrl+S"), self, self.sell_btn.click)
        
        # Function keys
        QShortcut(QKeySequence("F1"), self, lambda: self.load_preset('NIFTY', 'CE', 'ATM'))
        QShortcut(QKeySequence("F2"), self, lambda: self.load_preset('NIFTY', 'PE', 'ATM'))
        QShortcut(QKeySequence("F3"), self, lambda: self.load_preset('BANKNIFTY', 'CE', 'ATM'))
        QShortcut(QKeySequence("F4"), self, lambda: self.load_preset('BANKNIFTY', 'PE', 'ATM'))
        QShortcut(QKeySequence("F5"), self, lambda: self.adjust_lots(1))
        QShortcut(QKeySequence("F6"), self, lambda: self.adjust_lots(-1))
        QShortcut(QKeySequence("F9"), self, self.exit_all_positions)
        QShortcut(QKeySequence("F10"), self, self.cancel_all_orders)
        
        # Quick focus
        QShortcut(QKeySequence("Ctrl+L"), self, self.symbol_input.setFocus)
    
    def setup_timers(self):
        """Setup refresh timers"""
        # Position refresh every 2 seconds
        self.position_timer = QTimer()
        self.position_timer.timeout.connect(self.refresh_positions)
        self.position_timer.start(2000)
        
        # Time update every second
        self.time_timer = QTimer()
        self.time_timer.timeout.connect(self.update_time)
        self.time_timer.start(1000)
        
        # Margin refresh every 30 seconds
        self.margin_timer = QTimer()
        self.margin_timer.timeout.connect(self.refresh_margin)
        self.margin_timer.start(30000)
    
    def connect_signals(self):
        """Connect internal signals"""
        self.position_updated.connect(self.update_positions_table)
        self.order_updated.connect(self.update_orders_table)
        self.log_message.connect(self.append_log)
    
    # Event handlers
    def on_symbol_entered(self):
        """Handle symbol input - map to NEO"""
        symbol = self.symbol_input.text().strip().upper()
        if not symbol:
            return
        
        try:
            mapping = self.mapper.map_to_neo(symbol)
            self.current_mapping = mapping
            
            # Update lot size and quantity
            lot_size = mapping['lot_size']
            self.qty_label.setText(str(lot_size * self.lots_spin.value()))
            
            self.log_message.emit(f"[MAP] {symbol} → Token: {mapping['instrument_token']}, Lot: {lot_size}")
            
        except Exception as e:
            self.log_message.emit(f"[ERROR] Symbol mapping failed: {str(e)}")
    
    def on_order_type_changed(self, order_type):
        """Enable/disable price field based on order type"""
        self.price_input.setEnabled(order_type in ["LIMIT", "SL"])
    
    def place_quick_order(self, action: str):
        """Place order from quick entry panel"""
        if not hasattr(self, 'current_mapping'):
            self.log_message.emit("[ERROR] No symbol selected. Enter symbol first.")
            return
        
        try:
            price = float(self.price_input.text()) if self.price_input.text() else 0
            order_type = self.order_type_combo.currentText()
            
            # Map order type to NEO format
            type_map = {'LIMIT': 'L', 'MARKET': 'MKT', 'SL': 'SL', 'SL-M': 'SL-M'}
            
            from core.order_manager import OrderParams
            params = OrderParams(
                symbol=self.current_mapping['trading_symbol'],
                exchange_segment=self.current_mapping['exchange_segment'],
                instrument_token=self.current_mapping['instrument_token'],
                transaction_type=action,
                quantity=int(self.qty_label.text()),
                product=self.product_combo.currentText(),
                order_type=type_map.get(order_type, 'L'),
                price=price if order_type == 'LIMIT' else None,
            )
            
            # Check if bracket order
            if self.bracket_check.isChecked():
                sl = float(self.sl_input.text()) if self.sl_input.text() else 0
                target = float(self.target_input.text()) if self.target_input.text() else 0
                if sl and target:
                    from core.order_manager import BracketOrderParams
                    bracket_params = BracketOrderParams(
                        entry=params,
                        stop_loss_points=abs(price - sl),
                        target_points=abs(target - price)
                    )
                    result = self.orders.place_bracket_order(bracket_params)
                else:
                    self.log_message.emit("[ERROR] SL and Target required for bracket order")
                    return
            else:
                result = self.orders.place_order(params)
            
            action_str = "BUY" if action == 'B' else "SELL"
            self.log_message.emit(
                f"[ORDER] {action_str} {params.symbol} {params.quantity} @ {price} → ID: {result.get('nOrdNo', 'N/A')} ✓"
            )
            
            # Set SL/Target if provided but not bracket
            if not self.bracket_check.isChecked():
                sl = float(self.sl_input.text()) if self.sl_input.text() else 0
                target = float(self.target_input.text()) if self.target_input.text() else 0
                if sl or target:
                    self.log_message.emit("[INFO] SL/Target orders will be placed after fill")
            
            self.refresh_positions()
            
        except Exception as e:
            self.log_message.emit(f"[ERROR] Order failed: {str(e)}")
    
    def add_to_basket(self):
        """Add current order to basket"""
        if not hasattr(self, 'current_mapping'):
            return
        
        action = 'B' if self.buy_radio.isChecked() else 'S'
        price = float(self.price_input.text()) if self.price_input.text() else 0
        qty = int(self.qty_label.text())
        
        leg = {
            'symbol': self.current_mapping['trading_symbol'],
            'mapping': self.current_mapping,
            'action': action,
            'qty': qty,
            'price': price
        }
        
        self.basket_legs.append(leg)
        self.update_basket_table()
    
    def update_basket_table(self):
        """Update basket table display"""
        self.basket_table.setRowCount(len(self.basket_legs))
        
        net_premium = 0
        for i, leg in enumerate(self.basket_legs):
            self.basket_table.setItem(i, 0, QTableWidgetItem(f"Leg {i+1}"))
            self.basket_table.setItem(i, 1, QTableWidgetItem(leg['symbol']))
            self.basket_table.setItem(i, 2, QTableWidgetItem("BUY" if leg['action'] == 'B' else "SELL"))
            self.basket_table.setItem(i, 3, QTableWidgetItem(str(leg['qty'])))
            self.basket_table.setItem(i, 4, QTableWidgetItem(str(leg['price'])))
            
            # Calculate net premium
            if leg['action'] == 'B':
                net_premium -= leg['price'] * leg['qty']
            else:
                net_premium += leg['price'] * leg['qty']
        
        self.net_premium.setText(f"₹{net_premium:,.0f}")
        color = "#00ff88" if net_premium > 0 else "#ff4444"
        self.net_premium.setStyleSheet(f"color: {color}; font-weight: bold;")
    
    def execute_basket(self):
        """Execute all legs in basket simultaneously"""
        if not self.basket_legs:
            return
        
        try:
            from core.order_manager import OrderParams
            
            params_list = []
            for leg in self.basket_legs:
                params = OrderParams(
                    symbol=leg['mapping']['trading_symbol'],
                    exchange_segment=leg['mapping']['exchange_segment'],
                    instrument_token=leg['mapping']['instrument_token'],
                    transaction_type=leg['action'],
                    quantity=leg['qty'],
                    product=self.product_combo.currentText(),
                    order_type='L',
                    price=leg['price']
                )
                params_list.append(params)
            
            results = self.orders.place_multi_leg_order(params_list)
            
            self.log_message.emit(f"[BASKET] Executed {len(results)} legs ✓")
            self.clear_basket()
            self.refresh_positions()
            
        except Exception as e:
            self.log_message.emit(f"[ERROR] Basket execution failed: {str(e)}")
    
    def clear_basket(self):
        """Clear basket"""
        self.basket_legs = []
        self.basket_table.setRowCount(0)
        self.net_premium.setText("₹0")
    
    def exit_all_positions(self):
        """Exit all open positions"""
        try:
            results = self.orders.exit_all_positions()
            for r in results:
                status = "✓" if r['status'] == 'success' else "✗"
                self.log_message.emit(f"[EXIT] {r['symbol']} {status}")
            self.refresh_positions()
        except Exception as e:
            self.log_message.emit(f"[ERROR] Exit all failed: {str(e)}")
    
    def refresh_positions(self):
        """Refresh positions from API"""
        try:
            client = self.session.get_client()
            positions = client.positions()
            self.position_updated.emit(positions.get('data', []))
        except Exception as e:
            self.log_message.emit(f"[ERROR] Position refresh failed: {str(e)}")
    
    def update_positions_table(self, positions):
        """Update positions table with data"""
        self.positions_table.setRowCount(len(positions))
        total_pnl = 0
        
        for i, pos in enumerate(positions):
            qty = int(pos.get('qty', 0))
            if qty == 0:
                continue
            
            avg = float(pos.get('avgPrice', 0))
            ltp = float(pos.get('ltp', avg))
            pnl = (ltp - avg) * qty
            pnl_pct = (pnl / (avg * abs(qty)) * 100) if avg else 0
            
            self.positions_table.setItem(i, 0, QTableWidgetItem(pos.get('symbol', '')))
            self.positions_table.setItem(i, 1, QTableWidgetItem(str(qty)))
            self.positions_table.setItem(i, 2, QTableWidgetItem(f"{avg:.2f}"))
            self.positions_table.setItem(i, 3, QTableWidgetItem(f"{ltp:.2f}"))
            
            pnl_item = QTableWidgetItem(f"₹{pnl:,.0f}")
            pnl_item.setForeground(QColor("#00ff88" if pnl >= 0 else "#ff4444"))
            self.positions_table.setItem(i, 4, pnl_item)
            
            pct_item = QTableWidgetItem(f"{pnl_pct:+.1f}%")
            pct_item.setForeground(QColor("#00ff88" if pnl >= 0 else "#ff4444"))
            self.positions_table.setItem(i, 5, pct_item)
            
            # Action buttons - create widget with buttons
            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(2, 2, 2, 2)
            
            exit_25 = QPushButton("25%")
            exit_25.setMaximumWidth(40)
            exit_25.clicked.connect(lambda checked, p=pos: self.exit_position_partial(p, 25))
            actions_layout.addWidget(exit_25)
            
            exit_50 = QPushButton("50%")
            exit_50.setMaximumWidth(40)
            exit_50.clicked.connect(lambda checked, p=pos: self.exit_position_partial(p, 50))
            actions_layout.addWidget(exit_50)
            
            exit_full = QPushButton("EXIT")
            exit_full.setStyleSheet("background-color: #880000; color: white;")
            exit_full.setMaximumWidth(50)
            exit_full.clicked.connect(lambda checked, p=pos: self.exit_position_partial(p, 100))
            actions_layout.addWidget(exit_full)
            
            self.positions_table.setCellWidget(i, 6, actions)
            
            total_pnl += pnl
        
        # Update total P&L
        self.total_pnl.setText(f"₹{total_pnl:,.0f}")
        self.total_pnl.setStyleSheet(f"color: {'#00ff88' if total_pnl >= 0 else '#ff4444'}; font-weight: bold;")
        self.pnl_label.setText(f"₹{total_pnl:,.0f}")
        self.pnl_label.setStyleSheet(f"color: {'#00ff88' if total_pnl >= 0 else '#ff4444'};")
    
    def exit_position_partial(self, position, percent):
        """Exit position by percentage"""
        try:
            result = self.orders.exit_position(position, percent)
            self.log_message.emit(f"[EXIT] {position.get('symbol')} {percent}% → ID: {result.get('nOrdNo')}")
            self.refresh_positions()
        except Exception as e:
            self.log_message.emit(f"[ERROR] Exit failed: {str(e)}")
    
    def refresh_margin(self):
        """Refresh margin info"""
        try:
            client = self.session.get_client()
            limits = client.limits(segment="ALL", exchange="ALL", product="ALL")
            # Parse margin from response
            available = float(limits.get('Net', 0))
            self.margin_label.setText(f"₹{available:,.0f}")
        except:
            pass
    
    def update_time(self):
        """Update time display"""
        from datetime import datetime
        self.time_label.setText(datetime.now().strftime("%H:%M:%S"))
    
    def load_preset(self, underlying, opt_type, strike_type):
        """Load preset symbol (ATM option)"""
        # This would need live spot price to calculate ATM
        # For now, just set the underlying
        self.symbol_input.setText(f"{underlying}25JAN")
        self.log_message.emit(f"[PRESET] {underlying} {opt_type} {strike_type} - Complete the symbol")
        self.symbol_input.setFocus()
    
    def adjust_lots(self, delta):
        """Adjust lot count"""
        current = self.lots_spin.value()
        self.lots_spin.setValue(max(1, current + delta))
    
    def update_quantity(self, lots):
        """Update quantity when lots change"""
        if hasattr(self, 'current_mapping'):
            lot_size = self.current_mapping['lot_size']
            self.qty_label.setText(str(lot_size * lots))
    
    def cancel_pending_orders(self):
        """Cancel all pending orders"""
        try:
            client = self.session.get_client()
            orders = client.order_report()
            for order in orders.get('data', []):
                if order.get('ordSt') in ['open', 'pending', 'trigger pending']:
                    client.cancel_order(order_id=order.get('nOrdNo'))
                    self.log_message.emit(f"[CANCEL] Order {order.get('nOrdNo')} cancelled")
        except Exception as e:
            self.log_message.emit(f"[ERROR] Cancel failed: {str(e)}")
    
    def cancel_all_orders(self):
        """Cancel all orders"""
        self.cancel_pending_orders()
    
    def append_log(self, message):
        """Append message to log"""
        from datetime import datetime
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"{timestamp} {message}")
    
    def create_separator(self):
        """Create vertical separator"""
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        return sep
    
    def get_dark_theme(self) -> str:
        """Return dark theme stylesheet"""
        return """
            QMainWindow, QWidget {
                background-color: #1a1a2e;
                color: #eaeaea;
                font-family: 'Segoe UI', Arial;
            }
            QGroupBox {
                font-weight: bold;
                border: 1px solid #333355;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                color: #8888ff;
            }
            QLineEdit, QComboBox, QSpinBox {
                background-color: #252540;
                border: 1px solid #333355;
                border-radius: 3px;
                padding: 5px;
                color: #ffffff;
            }
            QLineEdit:focus, QComboBox:focus {
                border: 1px solid #6666ff;
            }
            QPushButton {
                background-color: #333355;
                border: none;
                border-radius: 3px;
                padding: 8px 15px;
                color: white;
            }
            QPushButton:hover {
                background-color: #444477;
            }
            QPushButton:pressed {
                background-color: #222244;
            }
            QTableWidget {
                background-color: #252540;
                gridline-color: #333355;
                border: none;
            }
            QTableWidget::item {
                padding: 5px;
            }
            QTableWidget::item:selected {
                background-color: #444477;
            }
            QHeaderView::section {
                background-color: #333355;
                padding: 5px;
                border: none;
                font-weight: bold;
            }
            QTextEdit {
                background-color: #252540;
                border: 1px solid #333355;
                color: #aaaaaa;
            }
            QRadioButton {
                spacing: 5px;
            }
            QRadioButton::indicator {
                width: 15px;
                height: 15px;
            }
            QCheckBox {
                spacing: 5px;
            }
            QFrame {
                background-color: #252540;
            }
            QScrollBar:vertical {
                background-color: #252540;
                width: 12px;
            }
            QScrollBar::handle:vertical {
                background-color: #333355;
                border-radius: 6px;
            }
        """


# Entry point
def main():
    import yaml
    
    # Load config
    with open('config/settings.yaml', 'r') as f:
        config = yaml.safe_load(f)
    
    # Initialize components
    from core.session_manager import SessionManager
    from core.symbol_mapper import SymbolMapper
    from core.order_manager import OrderManager
    
    session = SessionManager(config)
    success, msg = session.auto_login()
    print(msg)
    
    if not success:
        print("Login failed. Exiting.")
        return
    
    mapper = SymbolMapper(session.get_client())
    success, msg = mapper.initialize()
    print(msg)
    
    orders = OrderManager(session.get_client(), config, mapper)
    
    # Start GUI
    app = QApplication(sys.argv)
    window = TradingTerminal(session, orders, mapper, config)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
```

---

## Project Structure

```
neo_trade_terminal/
├── config/
│   ├── settings.yaml           # Main configuration
│   └── presets.yaml            # Quick action presets (F1-F4)
├── core/
│   ├── __init__.py
│   ├── session_manager.py      # Auto-TOTP login
│   ├── symbol_mapper.py        # Kite → NEO mapping
│   ├── order_manager.py        # Order execution + safety
│   ├── websocket_handler.py    # Live feeds
│   ├── kite_spot.py            # Kite spot price for ATM calc
│   ├── sound_alerts.py         # Audio notifications
│   ├── telegram_notifier.py    # Telegram push notifications
│   ├── trailing_sl.py          # Trailing SL manager ⭐ NEW
│   ├── oco_monitor.py          # SL/Target OCO simulation
│   └── signal_watcher.py       # Future: file-based signals
├── gui/
│   ├── __init__.py
│   ├── main_window.py          # Main trading window
│   ├── dialogs/
│   │   ├── sl_target_dialog.py # SL/Target setter
│   │   ├── trail_dialog.py     # Custom trail dialog ⭐ NEW
│   │   ├── symbol_search.py    # Symbol search popup
│   │   └── gtt_dialog.py       # GTT order creation
│   └── widgets/
│       ├── position_row.py     # Position with actions
│       ├── order_row.py        # Order display
│       └── strike_selector.py  # Strike picker for ATM
├── assets/
│   └── sounds/                 # Alert sound files
│       ├── click.wav
│       ├── success.wav
│       ├── error.wav
│       ├── alert.wav
│       └── cash.wav
├── data/
│   ├── scrip_master/           # Cached scrip masters
│   └── session.json            # Session cache
├── signals/                    # Signal files (future)
│   └── signals_YYYYMMDD.csv
├── logs/
│   └── trades.log              # Trade audit log
├── main.py                     # Application entry
├── requirements.txt
└── README.md
```

---

## Requirements

```txt
# requirements.txt
# NEO API
git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git@v2.0.1#egg=neo_api_client

# Kite for spot prices
kiteconnect>=5.0.0

# GUI
PyQt6>=6.5.0

# Auto TOTP
pyotp>=2.8.0

# Data handling
pandas>=2.0.0
pyyaml>=6.0

# File watching (future)
watchdog>=3.0.0

# HTTP requests (Telegram)
requests>=2.28.0

# Sound (optional - install one)
# Windows: winsound (built-in)
# Cross-platform options:
# playsound>=1.3.0
# simpleaudio>=1.0.4
```

---

## Implementation Phases

### Phase 1: Core Foundation (Day 1-2)
- [ ] Session Manager with auto-TOTP
- [ ] Symbol Mapper (Kite → NEO)
- [ ] Basic Order Manager (place, cancel)
- [ ] Config loader

**Deliverable:** Can login and place orders via CLI

### Phase 2: Basic GUI (Day 3-4)
- [ ] Main window layout
- [ ] Quick entry panel
- [ ] Positions table (manual refresh)
- [ ] Order book display
- [ ] Status bar

**Deliverable:** Working GUI with manual refresh

### Phase 3: Live Updates (Day 5)
- [ ] WebSocket integration
- [ ] Live LTP on positions
- [ ] Order status updates
- [ ] Auto-refresh positions

**Deliverable:** Real-time position updates

### Phase 4: Advanced Features (Day 6-7)
- [ ] Kite spot fetcher for ATM
- [ ] F1-F4 preset buttons with ATM
- [ ] Sound alerts
- [ ] Telegram notifications
- [ ] Keyboard shortcuts

**Deliverable:** Full-featured terminal

### Phase 5: OCO & Safety (Day 8)
- [ ] OCO monitor for SL/Target
- [ ] Circuit breaker
- [ ] Duplicate order prevention
- [ ] Trade audit log

**Deliverable:** Production-ready with safety

### Phase 6: Multi-leg & Polish (Day 9-10)
- [ ] Basket order builder
- [ ] Multi-leg execution
- [ ] GTT dialog
- [ ] Signal file watcher (prep for future)

**Deliverable:** Complete terminal

---

## Quick Reference: Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+B` | Buy (current symbol) |
| `Ctrl+S` | Sell (current symbol) |
| `Ctrl+L` | Focus symbol input |
| `F1` | Load NIFTY CE ATM |
| `F2` | Load NIFTY PE ATM |
| `F3` | Load BANKNIFTY CE ATM |
| `F4` | Load BANKNIFTY PE ATM |
| `F5` | +1 Lot |
| `F6` | -1 Lot |
| `F9` | Exit all positions |
| `F10` | Cancel all orders |
| `T` | Trail selected position to COST |
| `Shift+T` | Trail selected +10 points |
| `Ctrl+T` | Focus SL input (type price, Enter) |
| `Esc` | Clear inputs |

---

## Data Flow Diagram

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   KITE ALERT    │     │   TERMINAL      │     │    NEO API      │
│   (Copy Symbol) │────▶│   QUICK ENTRY   │────▶│   PLACE ORDER   │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                               │                        │
                               │                        ▼
┌─────────────────┐            │               ┌─────────────────┐
│   KITE API      │            │               │   WEBSOCKET     │
│   (Spot Price)  │────────────┤               │   (Order Feed)  │
└─────────────────┘            │               └─────────────────┘
        │                      │                        │
        ▼                      ▼                        ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   ATM STRIKE    │     │   POSITIONS     │◀────│   ORDER STATUS  │
│   CALCULATION   │     │   DISPLAY       │     │   UPDATE        │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                               │                        │
                               ▼                        ▼
                        ┌─────────────────┐     ┌─────────────────┐
                        │   OCO MONITOR   │     │   TELEGRAM      │
                        │   (SL/Target)   │────▶│   NOTIFICATION  │
                        └─────────────────┘     └─────────────────┘
```

---

## Future Enhancements (Post v1.0)

1. **Signal File Automation**
   - File watcher for `signals_YYYYMMDD.csv`
   - Auto-process NEW signals
   - Status tracking (PROCESSING → EXECUTED/ERROR)

2. **Strategy Integration**
   - Connect to Bouncer/Momentum signals
   - Auto-derive strike from signal type

3. **Advanced Order Types**
   - Trailing SL
   - Time-based exits
   - Partial profit booking rules

4. **Analytics Dashboard**
   - Trade history visualization
   - P&L charts
   - Win rate tracking

5. **Multi-Account Support**
   - Multiple NEO accounts
   - Copy trading across accounts

---

## Notes for Claude Code Implementation

When building with Claude Code, create files in this order:

1. `config/settings.yaml` - Configuration template
2. `core/session_manager.py` - Login first
3. `core/symbol_mapper.py` - Symbol mapping
4. `core/order_manager.py` - Order execution
5. `core/kite_spot.py` - ATM calculation
6. `core/sound_alerts.py` - Audio alerts
7. `core/telegram_notifier.py` - Notifications
8. `core/oco_monitor.py` - OCO simulation
9. `gui/main_window.py` - GUI (last, largest)
10. `main.py` - Entry point

Test each module independently before GUI integration.

---

*Document Version: 1.1*
*Last Updated: January 2026*
*Author: Claude (for Raja)*

### 1. **One-Time Daily Setup**
- Auto-TOTP login on terminal start
- Scrip master download and caching
- Session persistence (no re-login if reopened same day)

### 2. **Quick Entry (1Cliq Style)**
- Paste Kite symbol → Auto-maps to NEO
- Keyboard shortcuts: Ctrl+B (Buy), Ctrl+S (Sell)
- F1-F4: Preset ATM options
- F5/F6: Adjust lots
- F9: Exit all, F10: Cancel all

### 3. **Position Management**
- Live P&L display (2-second refresh)
- Partial exit buttons: 25%, 50%, 100%
- Color-coded profit/loss
- Set SL/Target on existing positions

### 4. **Multi-leg Support**
- Basket order builder
- Execute all legs simultaneously
- Net premium calculation
- Spread margin display

### 5. **Risk Management (Config-driven)**
- Daily loss limit circuit breaker
- Max position limit
- Duplicate order prevention
- All configurable in YAML

### 6. **Order Types**
- Limit, Market, SL, SL-M
- Bracket Orders (if NEO supports)
- Manual SL+Target fallback

---

## Next Steps for Implementation

1. **Phase 1**: Core modules (session, mapper, orders)
2. **Phase 2**: Basic GUI without websocket
3. **Phase 3**: Add websocket for live LTP
4. **Phase 4**: Basket orders and multi-leg
5. **Phase 5**: Polish and keyboard shortcuts

---

## Confirmed Features

| Feature | Status | Notes |
|---------|--------|-------|
| ATM Strike Calculation | ✅ YES | Fetch NIFTY/BANKNIFTY spot from Kite API |
| Sound Alerts | ✅ YES | Beep on fill, different tone on rejection |
| Telegram Integration | ✅ YES | Push to existing Telegram bot |
| OCO Monitoring | ✅ YES | Auto-cancel opposite leg on SL/Target hit |
| NRML Support | ✅ YES | For positional trades |
| GTT Orders | ✅ YES | Create from terminal |

## Signal File Integration (Future-Ready)

### Phase 1 (Current): Manual Mode
- Paste symbol from Kite alert manually
- Terminal processes and places order

### Phase 2 (Future): File Watcher Mode
Signal file format: `signals/signals_YYYYMMDD.csv`

```csv
timestamp,symbol,action,price,sl,target,status,processed_at,order_id,notes
2026-01-16 09:35:00,NIFTY25JAN24000CE,BUY,250,225,300,NEW,,, 
2026-01-16 09:40:00,BANKNIFTY25JAN52000PE,SELL,180,200,150,NEW,,,
```

**Status Flow:**
```
NEW → PROCESSING → EXECUTED → FILLED/REJECTED
                → SKIPPED (if filters fail)
                → ERROR (if API fails)
```

**File Watcher Module (Ready for Phase 2):**

```python
# core/signal_watcher.py
"""
Watches signal file for new entries.
Processes NEW signals and updates status.
"""

import os
import pandas as pd
from datetime import datetime, date
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import threading
import time

class SignalFileHandler(FileSystemEventHandler):
    def __init__(self, terminal_callback):
        self.callback = terminal_callback
        self.signal_dir = "signals"
        self.current_file = None
        self._last_processed_row = 0
        
    def get_today_file(self) -> str:
        """Get today's signal file path"""
        today = date.today().strftime("%Y%m%d")
        return os.path.join(self.signal_dir, f"signals_{today}.csv")
    
    def on_modified(self, event):
        """Called when signal file is modified"""
        if event.src_path == self.get_today_file():
            self.process_new_signals()
    
    def process_new_signals(self):
        """Process any NEW signals in file"""
        filepath = self.get_today_file()
        if not os.path.exists(filepath):
            return
        
        try:
            df = pd.read_csv(filepath)
            
            # Find NEW signals
            new_signals = df[df['status'] == 'NEW']
            
            for idx, row in new_signals.iterrows():
                # Mark as processing
                df.at[idx, 'status'] = 'PROCESSING'
                df.at[idx, 'processed_at'] = datetime.now().isoformat()
                df.to_csv(filepath, index=False)
                
                # Send to terminal for execution
                result = self.callback(row.to_dict())
                
                # Update status based on result
                if result['success']:
                    df.at[idx, 'status'] = 'EXECUTED'
                    df.at[idx, 'order_id'] = result.get('order_id', '')
                else:
                    df.at[idx, 'status'] = 'ERROR'
                    df.at[idx, 'notes'] = result.get('error', '')
                
                df.to_csv(filepath, index=False)
                
        except Exception as e:
            print(f"Signal processing error: {e}")
    
    def create_today_file(self):
        """Create today's signal file if not exists"""
        os.makedirs(self.signal_dir, exist_ok=True)
        filepath = self.get_today_file()
        
        if not os.path.exists(filepath):
            df = pd.DataFrame(columns=[
                'timestamp', 'symbol', 'action', 'price', 
                'sl', 'target', 'status', 'processed_at', 
                'order_id', 'notes'
            ])
            df.to_csv(filepath, index=False)
        
        return filepath


class SignalWatcher:
    def __init__(self, terminal_callback):
        self.handler = SignalFileHandler(terminal_callback)
        self.observer = Observer()
        self._running = False
    
    def start(self):
        """Start watching signal directory"""
        signal_dir = self.handler.signal_dir
        os.makedirs(signal_dir, exist_ok=True)
        
        self.handler.create_today_file()
        self.observer.schedule(self.handler, signal_dir, recursive=False)
        self.observer.start()
        self._running = True
        print(f"[WATCHER] Monitoring {signal_dir} for signals...")
    
    def stop(self):
        """Stop watching"""
        self.observer.stop()
        self.observer.join()
        self._running = False
    
    def add_signal_manually(self, symbol: str, action: str, 
                           price: float = 0, sl: float = 0, 
                           target: float = 0) -> bool:
        """Add signal to file manually (for GUI integration)"""
        filepath = self.handler.get_today_file()
        
        try:
            df = pd.read_csv(filepath)
            new_row = {
                'timestamp': datetime.now().isoformat(),
                'symbol': symbol,
                'action': action,
                'price': price,
                'sl': sl,
                'target': target,
                'status': 'NEW',
                'processed_at': '',
                'order_id': '',
                'notes': ''
            }
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            df.to_csv(filepath, index=False)
            return True
        except Exception as e:
            print(f"Failed to add signal: {e}")
            return False
```

**GUI Integration for Signal Mode Toggle:**

```python
# In main_window.py - add to status bar
self.signal_mode_btn = QPushButton("📁 SIGNAL MODE: OFF")
self.signal_mode_btn.setCheckable(True)
self.signal_mode_btn.clicked.connect(self.toggle_signal_mode)

def toggle_signal_mode(self, enabled):
    if enabled:
        self.signal_watcher.start()
        self.signal_mode_btn.setText("📁 SIGNAL MODE: ON")
        self.signal_mode_btn.setStyleSheet("background-color: #006644;")
        self.log_message.emit("[WATCHER] Signal file monitoring ENABLED")
    else:
        self.signal_watcher.stop()
        self.signal_mode_btn.setText("📁 SIGNAL MODE: OFF")
        self.signal_mode_btn.setStyleSheet("")
        self.log_message.emit("[WATCHER] Signal file monitoring DISABLED")
```
