"""
Broker Adapter - Abstract interface for broker API operations

Provides a broker-agnostic interface for FIFTY bot.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
import pandas as pd


class BrokerAdapter(ABC):
    """Abstract interface for broker API operations"""

    # =========================================================================
    # CONNECTION & VALIDATION
    # =========================================================================

    @abstractmethod
    def validate_connection(self) -> bool:
        """Validate API connection and token"""
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
        """Fetch historical OHLC data for given instrument"""
        pass

    @abstractmethod
    def get_historical_data_sampled(
        self,
        instrument_token: str,
        timeframe: str,
        years_back: float
    ) -> pd.DataFrame:
        """Fetch daily data and resample to monthly/weekly timeframes"""
        pass

    @abstractmethod
    def get_instrument_ltp(self, instrument_token: str) -> float:
        """Get Last Traded Price for an instrument"""
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
        """Place order on Zerodha"""
        pass

    @abstractmethod
    def cancel_order(self, order_id: str, variety: str = "regular", verify: bool = True) -> bool:
        """Cancel a regular order"""
        pass

    @abstractmethod
    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """Get status of a specific order"""
        pass

    @abstractmethod
    def get_all_orders(self) -> List[Dict[str, Any]]:
        """Get all orders placed today"""
        pass

    # =========================================================================
    # GTT OPERATIONS
    # =========================================================================

    @abstractmethod
    def place_gtt_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Place GTT (Good Till Triggered) order"""
        pass

    @abstractmethod
    def cancel_gtt_order(self, gtt_id: str) -> bool:
        """Cancel GTT order"""
        pass

    @abstractmethod
    def get_gtt_orders(self) -> List[Dict[str, Any]]:
        """Get all active GTT orders"""
        pass

    @abstractmethod
    def get_gtt_status(self, gtt_id: str) -> Optional[Dict[str, Any]]:
        """Get status of a specific GTT"""
        pass

    # =========================================================================
    # PORTFOLIO & MARGIN
    # =========================================================================

    @abstractmethod
    def get_positions(self) -> Dict[str, List[Dict[str, Any]]]:
        """Get current positions"""
        pass

    @abstractmethod
    def get_margins(self) -> Dict[str, Any]:
        """Fetch margin data"""
        pass

    # =========================================================================
    # INSTRUMENTS
    # =========================================================================

    @abstractmethod
    def get_instrument_token(self, script: str) -> str:
        """Get instrument token for a given script symbol"""
        pass

    @abstractmethod
    def get_tradingsymbol(self, instrument_token: str) -> Optional[str]:
        """Get trading symbol for given instrument token"""
        pass

    # =========================================================================
    # PROPERTIES
    # =========================================================================

    @property
    @abstractmethod
    def order_tag(self) -> str:
        """Get the order tag used by this bot"""
        pass
