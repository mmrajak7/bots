"""
Broker Adapter - Abstract interface for broker API operations

This module provides a broker-agnostic interface allowing CROCODILE to work with
different authentication methods (enctoken, Kite API) without changing business logic.

Usage:
    from src.api.broker_adapter import get_broker_adapter

    broker = get_broker_adapter()  # Returns appropriate adapter based on config
    broker.place_order(...)        # Same interface regardless of auth method
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
import pandas as pd


class BrokerAdapter(ABC):
    """
    Abstract interface for broker API operations

    All broker implementations must implement these methods.
    This allows seamless switching between enctoken and Kite API methods.
    """

    # =========================================================================
    # CONNECTION & VALIDATION
    # =========================================================================

    @abstractmethod
    def validate_connection(self) -> bool:
        """
        Validate API connection and token

        Returns:
            True if connection is valid, False otherwise
        """
        pass

    # =========================================================================
    # HISTORICAL DATA
    # =========================================================================

    @abstractmethod
    def get_historical_data(
        self,
        instrument_token: str,
        start_date: str,
        end_date: str,
        interval: str = 'day'
    ) -> pd.DataFrame:
        """
        Fetch historical OHLC data for given instrument

        Args:
            instrument_token: Zerodha instrument token
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
            interval: Timeframe (day, minute, 2minute, 3minute, etc.)

        Returns:
            DataFrame with Date, Open, High, Low, Close, Volume columns
        """
        pass

    @abstractmethod
    def get_historical_data_sampled(
        self,
        instrument_token: str,
        timeframe: str,
        years_back: float
    ) -> pd.DataFrame:
        """
        Fetch daily data and resample to monthly/weekly timeframes

        Args:
            instrument_token: Zerodha instrument token
            timeframe: 'monthly', 'weekly', or 'daily'
            years_back: Number of years to look back

        Returns:
            DataFrame with resampled OHLC data
        """
        pass

    @abstractmethod
    def get_instrument_ltp(self, instrument_token: str) -> float:
        """
        Get Last Traded Price for an instrument

        Args:
            instrument_token: Zerodha instrument token

        Returns:
            Last traded price
        """
        pass

    # =========================================================================
    # ORDER OPERATIONS
    # =========================================================================

    @abstractmethod
    def place_order(
        self,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,
        quantity: int,
        order_type: str,
        price: Optional[float] = None,
        product: str = "CNC",
        validity: str = "DAY",
        tag: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Place order on Zerodha

        Args:
            tradingsymbol: Trading symbol (e.g., "RELIANCE")
            exchange: Exchange (e.g., "NSE")
            transaction_type: "BUY" or "SELL"
            quantity: Number of shares
            order_type: "LIMIT", "MARKET", "SL", "SL-M"
            price: Price for LIMIT orders
            product: "CNC" (delivery), "MIS" (intraday), "NRML"
            validity: "DAY", "IOC"
            tag: Order tag for identification

        Returns:
            Dict with order response (contains order_id)
        """
        pass

    @abstractmethod
    def cancel_order(self, order_id: str, variety: str = "regular", verify: bool = True) -> bool:
        """
        Cancel a regular order

        Args:
            order_id: Order ID to cancel
            variety: Order variety
            verify: If True, verify order status after cancellation

        Returns:
            True if cancelled successfully
        """
        pass

    @abstractmethod
    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """
        Get status of a specific order

        Args:
            order_id: Order ID

        Returns:
            Order status dict
        """
        pass

    @abstractmethod
    def get_all_orders(self) -> List[Dict[str, Any]]:
        """
        Get all orders placed today

        Returns:
            List of all orders
        """
        pass

    @abstractmethod
    def get_bot_orders(self, tag: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get orders placed by this bot (filtered by tag)

        Args:
            tag: Tag to filter by

        Returns:
            List of orders with matching tag
        """
        pass

    # =========================================================================
    # GTT OPERATIONS
    # =========================================================================

    @abstractmethod
    def place_gtt_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Place GTT (Good Till Triggered) order

        Args:
            payload: GTT order payload with condition, orders, type, expires_at

        Returns:
            Dict with GTT response (contains trigger_id)
        """
        pass

    @abstractmethod
    def cancel_gtt_order(self, gtt_id: str) -> bool:
        """
        Cancel GTT order

        Args:
            gtt_id: GTT trigger ID

        Returns:
            Success status
        """
        pass

    @abstractmethod
    def get_gtt_orders(self) -> List[Dict[str, Any]]:
        """
        Get all active GTT orders

        Returns:
            List of GTT orders
        """
        pass

    # =========================================================================
    # PORTFOLIO & MARGIN
    # =========================================================================

    @abstractmethod
    def get_positions(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Get current positions

        Returns:
            Dict with 'net' and 'day' positions
        """
        pass

    @abstractmethod
    def get_margins(self) -> Dict[str, Any]:
        """
        Fetch margin data

        Returns:
            Dict with margin details
        """
        pass

    # =========================================================================
    # INSTRUMENTS
    # =========================================================================

    @abstractmethod
    def get_instrument_token(self, script: str) -> str:
        """
        Get instrument token for a given script symbol

        Args:
            script: Trading symbol (e.g., "VOLTAS", "M&M")

        Returns:
            Instrument token as string
        """
        pass

    @abstractmethod
    def fetch_instruments_data(self) -> bool:
        """
        Fetch instruments data from API and cache locally

        Returns:
            True if successful
        """
        pass

    @abstractmethod
    def load_instruments_cache(self) -> bool:
        """
        Load instruments data from local cache

        Returns:
            True if successful
        """
        pass

    # =========================================================================
    # CACHE MANAGEMENT
    # =========================================================================

    @abstractmethod
    def clear_cache(self) -> None:
        """Clear the data cache"""
        pass

    @abstractmethod
    def clear_instruments_cache(self) -> None:
        """Clear the instruments cache"""
        pass

    # =========================================================================
    # PROPERTIES
    # =========================================================================

    @property
    @abstractmethod
    def order_tag(self) -> str:
        """Get the order tag used by this bot"""
        pass

    @property
    @abstractmethod
    def trade_method(self) -> str:
        """Get the trade method being used ('enctoken' or 'kite_api')"""
        pass
