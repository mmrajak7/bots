"""
SNAIL Kite Client

Unified wrapper for Kite Connect API with automatic authentication.

@file        kite_client.py
@description Kite API client for SNAIL trading operations
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 2.2
"""

import os
import threading
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List, Any
from dataclasses import dataclass

from kiteconnect import KiteConnect
from loguru import logger

from src.api.kite_auth import KiteAuthenticator


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class Quote:
    """Standardized quote structure with bid-ask data."""
    bid: float
    ask: float
    ltp: float
    oi: int = 0
    volume: int = 0

    @property
    def spread(self) -> float:
        """Calculate bid-ask spread."""
        return self.ask - self.bid


@dataclass
class OrderResult:
    """Order execution result."""
    order_id: str
    status: str
    tradingsymbol: str
    transaction_type: str
    quantity: int
    price: float
    fill_price: Optional[float] = None
    slippage: Optional[float] = None


# =============================================================================
# EXCEPTIONS
# =============================================================================

class KiteClientError(Exception):
    """Exception raised for Kite client errors."""
    pass


class OrderExecutionError(KiteClientError):
    """Exception raised for order execution failures."""
    pass


# =============================================================================
# SNAIL KITE CLIENT
# =============================================================================

class SNAILKiteClient:
    """
    SNAIL-specific Kite API client.

    Provides unified interface for all Kite operations with:
    - Automatic authentication
    - Quote fetching with bid-ask
    - Order placement and management
    - Position and margin queries
    - Historical data access

    Attributes:
        config: Configuration dictionary
        api_key: Kite API key
        _kite: KiteConnect instance
        _authenticator: KiteAuthenticator instance
    """

    # Constants
    EXCHANGE_NFO = "NFO"
    EXCHANGE_NSE = "NSE"
    PRODUCT_NRML = "NRML"
    ORDER_TYPE_LIMIT = "LIMIT"
    ORDER_TYPE_MARKET = "MARKET"
    VARIETY_REGULAR = "regular"

    # NIFTY 50 instrument token
    NIFTY_INSTRUMENT_TOKEN = 256265

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Kite client.

        Args:
            config: Configuration dictionary with 'kite' section
        """
        self.config = config
        kite_config = config.get('api', {}).get('kite', config.get('kite', {}))

        self.api_key = kite_config.get('api_key') or os.getenv('ZERODHA_API_KEY')
        self._kite: Optional[KiteConnect] = None
        self._authenticator = KiteAuthenticator(kite_config)
        paper_config = config.get('paper_trading', {}).get('enabled', False)
        # Handle both bool and string values
        if isinstance(paper_config, str):
            self._paper_trading = paper_config.lower() in ('true', 'yes', '1', 'on')
        else:
            self._paper_trading = bool(paper_config)

        self._paper_orders: Dict[str, Dict[str, Any]] = {}  # Track paper trading orders

        # Log the paper trading status with source info for debugging
        env_value = os.getenv('SNAIL_PAPER_TRADING', 'not set')
        logger.info(f"Paper trading: enabled={self._paper_trading} (env={env_value}, config={paper_config})")

    def ensure_authenticated(self) -> 'SNAILKiteClient':
        """
        Ensure valid Kite session, auto-login if needed.

        Returns:
            Self for method chaining

        Raises:
            KiteAuthenticationError: If authentication fails
        """
        if self._kite:
            try:
                # Quick validation with profile call
                self._kite.profile()
                return self
            except Exception as e:
                logger.debug(f"Session validation failed: {e}")
                self._kite = None

        # Get valid token
        access_token = self._authenticator.ensure_valid_token()

        # Create KiteConnect instance
        self._kite = KiteConnect(api_key=self.api_key)
        self._kite.set_access_token(access_token)

        logger.info("Kite client authenticated")
        return self

    @property
    def kite(self) -> KiteConnect:
        """Get authenticated KiteConnect instance."""
        self.ensure_authenticated()
        return self._kite

    # =========================================================================
    # QUOTE OPERATIONS
    # =========================================================================

    def quote(self, instruments: List[str]) -> Dict[str, Quote]:
        """
        Get quotes with bid-ask data.

        Args:
            instruments: List of instrument strings (e.g., ["NFO:NIFTY24D0526100CE"])

        Returns:
            Dict mapping instrument to Quote object
        """
        raw_quotes = self.kite.quote(instruments)

        result = {}
        for inst, data in raw_quotes.items():
            depth = data.get('depth', {})
            buy_depth = depth.get('buy', [{}])
            sell_depth = depth.get('sell', [{}])

            result[inst] = Quote(
                bid=buy_depth[0].get('price', 0) if buy_depth else 0,
                ask=sell_depth[0].get('price', 0) if sell_depth else 0,
                ltp=data.get('last_price', 0),
                oi=data.get('oi', 0),
                volume=data.get('volume', 0)
            )

        return result

    def ltp(self, instruments: List[str]) -> Dict[str, float]:
        """
        Get last traded prices.

        Args:
            instruments: List of instrument strings

        Returns:
            Dict mapping instrument to LTP
        """
        result = self.kite.ltp(instruments)
        return {inst: data.get('last_price', 0) for inst, data in result.items()}

    def get_nifty_spot(self) -> Optional[float]:
        """
        Get current NIFTY spot price.

        Returns:
            NIFTY 50 last traded price, or None if unavailable
        """
        result = self.ltp(["NSE:NIFTY 50"])
        value = result.get("NSE:NIFTY 50", 0)
        if not value or value <= 0:
            logger.warning("get_nifty_spot() returned invalid value, returning None")
            return None
        return value

    def get_india_vix(self) -> Optional[float]:
        """
        Get current India VIX.

        Returns:
            India VIX value, or None if unavailable
        """
        result = self.ltp(["NSE:INDIA VIX"])
        value = result.get("NSE:INDIA VIX", 0)
        if not value or value <= 0:
            logger.warning("get_india_vix() returned invalid value, returning None")
            return None
        return value

    def get_nifty_futures_ltp(self, futures_symbol: str) -> float:
        """
        Get current NIFTY Futures LTP.

        Args:
            futures_symbol: Futures trading symbol (e.g., "NIFTY25DECFUT")

        Returns:
            NIFTY Futures last traded price, or 0 if unavailable
        """
        instrument = f"NFO:{futures_symbol}"
        result = self.ltp([instrument])
        return result.get(instrument, 0)

    # =========================================================================
    # ORDER OPERATIONS
    # =========================================================================

    def place_order(
        self,
        tradingsymbol: str,
        transaction_type: str,
        quantity: int,
        price: Optional[float] = None,
        order_type: str = "LIMIT"
    ) -> str:
        """
        Place an order.

        Args:
            tradingsymbol: Symbol without exchange prefix
            transaction_type: "BUY" or "SELL"
            quantity: Order quantity
            price: Limit price (None for MARKET orders)
            order_type: "LIMIT" or "MARKET"

        Returns:
            Order ID

        Raises:
            OrderExecutionError: If order placement fails
        """
        if self._paper_trading:
            order_id = f"PAPER_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
            # Store order details for later retrieval
            self._paper_orders[order_id] = {
                'order_id': order_id,
                'tradingsymbol': tradingsymbol,
                'transaction_type': transaction_type,
                'quantity': quantity,
                'price': price,
                'order_type': order_type,
                'status': 'COMPLETE',
                'filled_quantity': quantity,
                'average_price': price if price else 100.0  # Use order price as fill
            }
            logger.info(f"[PAPER] Order placed: {order_id} - {transaction_type} "
                       f"{quantity} {tradingsymbol} @ {price or 'MARKET'}")
            return order_id

        try:
            params = {
                'variety': self.VARIETY_REGULAR,
                'exchange': self.EXCHANGE_NFO,
                'tradingsymbol': tradingsymbol,
                'transaction_type': transaction_type,
                'quantity': quantity,
                'product': self.PRODUCT_NRML,
                'order_type': order_type,
            }

            if order_type == "LIMIT" and price is not None:
                params['price'] = price

            order_id = self.kite.place_order(**params)
            logger.info(f"Order placed: {order_id} - {transaction_type} "
                       f"{quantity} {tradingsymbol} @ {price or 'MARKET'}")
            return order_id

        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            raise OrderExecutionError(f"Failed to place order: {e}")

    def modify_order(
        self,
        order_id: str,
        price: Optional[float] = None,
        order_type: Optional[str] = None
    ) -> str:
        """
        Modify an existing order.

        Args:
            order_id: Order ID to modify
            price: New price
            order_type: New order type

        Returns:
            Modified order ID

        Raises:
            OrderExecutionError: If modification fails
        """
        if self._paper_trading:
            logger.info(f"[PAPER] Order modified: {order_id} -> price={price}, type={order_type}")
            return order_id

        try:
            params: Dict[str, Any] = {'variety': self.VARIETY_REGULAR, 'order_id': order_id}

            if price is not None:
                params['price'] = price
            if order_type is not None:
                params['order_type'] = order_type

            return self.kite.modify_order(**params)
        except Exception as e:
            logger.error(f"Order modification failed for {order_id}: {e}")
            raise OrderExecutionError(f"Failed to modify order {order_id}: {e}")

    def cancel_order(self, order_id: str) -> str:
        """
        Cancel an order.

        Args:
            order_id: Order ID to cancel

        Returns:
            Cancelled order ID

        Raises:
            OrderExecutionError: If cancellation fails
        """
        if self._paper_trading:
            logger.info(f"[PAPER] Order cancelled: {order_id}")
            return order_id

        try:
            return self.kite.cancel_order(variety=self.VARIETY_REGULAR, order_id=order_id)
        except Exception as e:
            logger.error(f"Order cancellation failed for {order_id}: {e}")
            raise OrderExecutionError(f"Failed to cancel order {order_id}: {e}")

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """
        Get order status.

        Args:
            order_id: Order ID

        Returns:
            Order details dictionary
        """
        if self._paper_trading:
            # Return stored order details if available
            if order_id in self._paper_orders:
                return self._paper_orders[order_id]
            # Fallback for unknown orders
            return {
                'order_id': order_id,
                'status': 'COMPLETE',
                'filled_quantity': 65,
                'average_price': 100.0
            }

        orders = self.kite.orders()
        for order in orders:
            if order.get('order_id') == order_id:
                return order

        raise KiteClientError(f"Order {order_id} not found")

    def orders(self) -> List[Dict]:
        """Get all orders for the day."""
        if self._paper_trading:
            return list(self._paper_orders.values())
        return self.kite.orders()

    # =========================================================================
    # POSITION & MARGIN OPERATIONS
    # =========================================================================

    def positions(self) -> Dict[str, List]:
        """
        Get current positions.

        Returns:
            Dictionary with 'net' and 'day' positions
        """
        if self._paper_trading:
            return {'net': [], 'day': []}
        return self.kite.positions()

    def margins(self) -> Dict[str, Any]:
        """
        Get account margins.

        Returns:
            Margins dictionary with 'equity' and 'commodity' sections
        """
        if self._paper_trading:
            return {
                'equity': {
                    'net': 200000,
                    'available': {'live_balance': 200000, 'opening_balance': 200000},
                    'utilised': {'span': 0, 'exposure': 0, 'option_premium': 0}
                }
            }
        return self.kite.margins()

    def get_available_margin(self) -> float:
        """
        Get available margin for trading.

        Returns:
            Available margin in INR
        """
        margins = self.margins()
        equity = margins.get('equity', {})
        return float(equity.get('net', 0))

    def get_margin_utilised(self) -> float:
        """
        Get utilised margin.

        Returns:
            Utilised margin in INR
        """
        margins = self.margins()
        equity = margins.get('equity', {})
        utilised = equity.get('utilised', {})

        return float(sum([
            utilised.get('span', 0),
            utilised.get('exposure', 0),
            utilised.get('option_premium', 0)
        ]))

    # =========================================================================
    # INSTRUMENTS
    # =========================================================================

    def instruments(self, exchange: str = "NFO") -> List[Dict]:
        """
        Get instruments list.

        Args:
            exchange: Exchange code (default NFO)

        Returns:
            List of instrument dictionaries
        """
        return self.kite.instruments(exchange)

    def get_nifty_options(self) -> List[Dict]:
        """
        Get NIFTY options instruments.

        Returns:
            List of NIFTY option instruments
        """
        all_instruments = self.instruments("NFO")
        return [
            i for i in all_instruments
            if i.get('name') == 'NIFTY' and i.get('instrument_type') in ('CE', 'PE')
        ]

    # =========================================================================
    # HISTORICAL DATA
    # =========================================================================

    def historical_data(
        self,
        instrument_token: int,
        from_date: str,
        to_date: str,
        interval: str = "day"
    ) -> List[Dict]:
        """
        Get historical OHLC data.

        Args:
            instrument_token: Instrument token
            from_date: Start date (YYYY-MM-DD)
            to_date: End date (YYYY-MM-DD)
            interval: Candle interval (minute, day, etc.)

        Returns:
            List of OHLC candle dictionaries
        """
        return self.kite.historical_data(
            instrument_token,
            from_date,
            to_date,
            interval
        )

    def get_nifty_historical(
        self,
        days: int = 365,
        interval: str = "day"
    ) -> List[Dict]:
        """
        Get NIFTY historical data.

        Args:
            days: Number of days of history
            interval: Candle interval

        Returns:
            List of OHLC candles
        """
        to_date = date.today() - timedelta(days=1)
        from_date = to_date - timedelta(days=days)

        return self.historical_data(
            self.NIFTY_INSTRUMENT_TOKEN,
            from_date.strftime('%Y-%m-%d'),
            to_date.strftime('%Y-%m-%d'),
            interval
        )

    # =========================================================================
    # PROFILE
    # =========================================================================

    def profile(self) -> Dict[str, Any]:
        """
        Get user profile.

        Returns:
            Profile dictionary
        """
        if self._paper_trading:
            return {
                'user_id': 'PAPER',
                'user_name': 'Paper Trading',
                'email': 'paper@trading.com'
            }
        return self.kite.profile()


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_kite_client: Optional[SNAILKiteClient] = None
_kite_client_lock = threading.Lock()


def get_kite_client(config: Optional[Dict] = None) -> SNAILKiteClient:
    """
    Get or create singleton Kite client (thread-safe).

    Args:
        config: Optional configuration dictionary

    Returns:
        SNAILKiteClient instance
    """
    global _kite_client

    # Always acquire lock first to ensure thread safety
    # Reading outside lock can see stale data due to instruction reordering
    with _kite_client_lock:
        if _kite_client is None:
            if config is None:
                from src.utils.config import load_config
                config = load_config()
            _kite_client = SNAILKiteClient(config)
        return _kite_client


def reset_kite_client() -> None:
    """Reset the singleton client (for testing)."""
    global _kite_client
    with _kite_client_lock:
        _kite_client = None


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    import sys
    from dotenv import load_dotenv

    load_dotenv()

    # Configure logging
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    print("\n" + "=" * 60)
    print("SNAIL Kite Client Test")
    print("=" * 60)

    try:
        config = {
            'api': {
                'kite': {
                    'api_key': os.getenv('ZERODHA_API_KEY'),
                    'api_secret': os.getenv('ZERODHA_API_SECRET'),
                    'user_id': os.getenv('ZERODHA_USER_ID'),
                    'password': os.getenv('ZERODHA_PASSWORD'),
                    'totp_secret': os.getenv('ZERODHA_TOTP_SECRET'),
                    'token_file': '../data/kite_access_token.json'  # Shared BOTS/data folder
                }
            },
            'paper_trading': {'enabled': False}
        }

        client = SNAILKiteClient(config)
        client.ensure_authenticated()

        # Test operations
        print("\n[1] Getting profile...")
        profile = client.profile()
        print(f"    User: {profile.get('user_name')} ({profile.get('user_id')})")

        print("\n[2] Getting NIFTY spot...")
        nifty = client.get_nifty_spot()
        print(f"    NIFTY: ₹{nifty:,.2f}")

        print("\n[3] Getting India VIX...")
        vix = client.get_india_vix()
        print(f"    VIX: {vix:.2f}")

        print("\n[4] Getting margins...")
        margin = client.get_available_margin()
        print(f"    Available: ₹{margin:,.2f}")

        print("\n" + "=" * 60)
        print("All tests passed!")
        print("=" * 60 + "\n")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
