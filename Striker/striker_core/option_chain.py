"""
Striker CLI - Option Chain Fetcher

Fetches option chain data using Kite API.
Provides strikes, premiums, bid-ask, OI, volume, and Greeks for strategy building.
"""

import sys
import os
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
import logging

# Add Scalper to path for reusing modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'Scalper'))

logger = logging.getLogger(__name__)


@dataclass
class OptionQuote:
    """Detailed option quote with bid-ask, OI, volume."""
    strike: int
    option_type: str  # 'CE' or 'PE'
    ltp: float
    bid: float
    ask: float
    bid_qty: int
    ask_qty: int
    oi: int
    volume: int
    trading_symbol: str
    token: Optional[str] = None

    # Calculated fields
    spread: float = 0
    spread_pct: float = 0
    iv: float = 0
    delta: float = 0
    theta: float = 0
    gamma: float = 0
    vega: float = 0

    def __post_init__(self):
        """Calculate spread metrics."""
        if self.bid > 0 and self.ask > 0:
            self.spread = self.ask - self.bid
            mid = (self.bid + self.ask) / 2
            self.spread_pct = (self.spread / mid * 100) if mid > 0 else 0

    @property
    def mid_price(self) -> float:
        """Get mid price between bid and ask."""
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.ltp

    @property
    def is_liquid(self) -> bool:
        """Check if option has reasonable liquidity."""
        return self.oi >= 1000 and self.volume >= 100 and self.spread_pct < 5

    @property
    def liquidity_score(self) -> str:
        """Get liquidity rating."""
        if self.oi >= 10000 and self.volume >= 1000 and self.spread_pct < 1:
            return "HIGH"
        elif self.oi >= 1000 and self.volume >= 100 and self.spread_pct < 3:
            return "MEDIUM"
        elif self.oi >= 100 and self.spread_pct < 10:
            return "LOW"
        else:
            return "ILLIQUID"


class OptionChain:
    """Fetches and manages option chain data."""

    # Weekly expiry indices (Thursday expiry)
    WEEKLY_INDICES = {'NIFTY', 'SENSEX', 'BANKNIFTY', 'FINNIFTY'}

    # Strike gaps for different underlyings
    STRIKE_GAPS = {
        'NIFTY': 50,
        'BANKNIFTY': 100,
        'FINNIFTY': 50,
        'SENSEX': 100,
        'MIDCPNIFTY': 25,
        # Stock options typically have dynamic gaps based on price
    }

    # Lot sizes (fallback - should fetch from instruments)
    LOT_SIZES = {
        'NIFTY': 75,
        'BANKNIFTY': 30,
        'FINNIFTY': 40,
        'SENSEX': 10,
        'MIDCPNIFTY': 50,
    }

    def __init__(self, kite_client=None):
        """
        Initialize with Kite client.

        Args:
            kite_client: KiteConnect instance or compatible client
        """
        self.kite = kite_client
        self._instruments_cache = {}
        self._spot_cache = {}
        self._chain_cache = {}

    def set_kite_client(self, kite_client):
        """Set Kite client after initialization."""
        self.kite = kite_client

    def get_spot_price(self, symbol: str) -> Optional[float]:
        """
        Get spot/LTP for underlying.

        Args:
            symbol: Underlying symbol (NIFTY, RELIANCE, etc.)

        Returns:
            Spot price or None
        """
        if not self.kite:
            logger.error("Kite client not initialized")
            return None

        try:
            # Determine exchange
            if symbol.upper() in self.WEEKLY_INDICES:
                if symbol.upper() == 'SENSEX':
                    exchange = 'BSE'
                    trading_symbol = 'SENSEX'
                else:
                    exchange = 'NSE'
                    trading_symbol = f'NIFTY 50' if symbol.upper() == 'NIFTY' else symbol.upper()
            else:
                exchange = 'NSE'
                trading_symbol = symbol.upper()

            # Get LTP
            quote_key = f"{exchange}:{trading_symbol}"
            quotes = self.kite.ltp([quote_key])

            if quotes and quote_key in quotes:
                ltp = quotes[quote_key].get('last_price')
                self._spot_cache[symbol.upper()] = ltp
                return ltp

            return None

        except Exception as e:
            logger.error(f"Failed to get spot for {symbol}: {e}")
            return self._spot_cache.get(symbol.upper())

    def get_expiries(self, symbol: str, weekly: bool = None) -> List[datetime]:
        """
        Get available expiry dates for symbol.

        Args:
            symbol: Underlying symbol
            weekly: True for weekly, False for monthly, None for auto

        Returns:
            List of expiry dates sorted ascending
        """
        if not self.kite:
            return self._generate_expiries(symbol, weekly)

        try:
            # Fetch instruments
            instruments = self._get_instruments(symbol)

            expiries = set()
            for inst in instruments:
                if inst.get('expiry'):
                    exp_date = inst['expiry']
                    if isinstance(exp_date, str):
                        exp_date = datetime.strptime(exp_date, '%Y-%m-%d')
                    expiries.add(exp_date)

            expiries = sorted(list(expiries))

            # Filter based on weekly preference
            if weekly is not None:
                if weekly:
                    # Keep only weekly (non-monthly) expiries
                    expiries = [e for e in expiries if not self._is_monthly_expiry(e)]
                else:
                    # Keep only monthly expiries
                    expiries = [e for e in expiries if self._is_monthly_expiry(e)]

            return expiries

        except Exception as e:
            logger.error(f"Failed to get expiries: {e}")
            return self._generate_expiries(symbol, weekly)

    def _is_monthly_expiry(self, expiry: datetime) -> bool:
        """Check if expiry is a monthly expiry (last Thursday of month)."""
        # Find last Thursday of the month
        next_month = expiry.replace(day=28) + timedelta(days=4)
        last_day = next_month - timedelta(days=next_month.day)

        # Find last Thursday
        days_to_subtract = (last_day.weekday() - 3) % 7
        last_thursday = last_day - timedelta(days=days_to_subtract)

        return expiry.date() == last_thursday.date()

    def _generate_expiries(self, symbol: str, weekly: bool = None) -> List[datetime]:
        """Generate approximate expiries when API unavailable."""
        today = datetime.now()
        expiries = []

        # Generate next 8 Thursdays
        days_until_thursday = (3 - today.weekday()) % 7
        if days_until_thursday == 0 and today.hour >= 15:
            days_until_thursday = 7

        next_thursday = today + timedelta(days=days_until_thursday)

        for i in range(8):
            exp = next_thursday + timedelta(weeks=i)
            is_monthly = self._is_monthly_expiry(exp)

            if weekly is None:
                expiries.append(exp)
            elif weekly and not is_monthly:
                expiries.append(exp)
            elif not weekly and is_monthly:
                expiries.append(exp)

        return expiries

    def get_atm_strike(self, symbol: str, spot: float = None) -> int:
        """
        Get ATM strike for symbol.

        Args:
            symbol: Underlying symbol
            spot: Spot price (fetched if not provided)

        Returns:
            ATM strike price
        """
        if spot is None:
            spot = self.get_spot_price(symbol)

        if spot is None:
            raise ValueError(f"Cannot get spot price for {symbol}")

        # Get strike gap
        gap = self.STRIKE_GAPS.get(symbol.upper(), self._estimate_strike_gap(spot))

        # Round to nearest strike
        atm = round(spot / gap) * gap
        return int(atm)

    def _estimate_strike_gap(self, spot: float) -> int:
        """Estimate strike gap based on spot price."""
        if spot < 100:
            return 2.5
        elif spot < 500:
            return 5
        elif spot < 1000:
            return 10
        elif spot < 2500:
            return 25
        elif spot < 5000:
            return 50
        else:
            return 100

    def get_option_chain(self, symbol: str, expiry: datetime,
                         strikes_around_atm: int = 10) -> Dict[str, Any]:
        """
        Get option chain for symbol and expiry.

        Args:
            symbol: Underlying symbol
            expiry: Expiry date
            strikes_around_atm: Number of strikes above/below ATM

        Returns:
            Dict with 'spot', 'atm', 'calls', 'puts', 'expiry', 'dte'
        """
        spot = self.get_spot_price(symbol)
        if spot is None:
            raise ValueError(f"Cannot get spot price for {symbol}")

        atm = self.get_atm_strike(symbol, spot)
        gap = self.STRIKE_GAPS.get(symbol.upper(), self._estimate_strike_gap(spot))

        # Generate strikes
        strikes = []
        for i in range(-strikes_around_atm, strikes_around_atm + 1):
            strikes.append(atm + (i * gap))

        # Calculate DTE
        today = datetime.now()
        if isinstance(expiry, str):
            expiry = datetime.strptime(expiry, '%Y-%m-%d')
        dte = (expiry - today).days

        # Fetch option premiums
        calls = {}
        puts = {}

        try:
            instruments = self._get_instruments(symbol)

            # Collect all trading symbols for batch quote
            symbols_to_quote = []
            symbol_map = {}  # quote_key -> (strike, type, trading_symbol, token)

            for strike in strikes:
                for inst in instruments:
                    inst_expiry = inst.get('expiry')
                    if isinstance(inst_expiry, str):
                        inst_expiry = datetime.strptime(inst_expiry, '%Y-%m-%d')

                    if inst_expiry and inst_expiry.date() == expiry.date():
                        if inst.get('strike') == strike:
                            inst_type = inst.get('instrument_type', '')
                            trading_symbol = inst.get('tradingsymbol', '')
                            token = inst.get('instrument_token')

                            exchange = 'BFO' if symbol.upper() == 'SENSEX' else 'NFO'
                            quote_key = f"{exchange}:{trading_symbol}"
                            symbols_to_quote.append(quote_key)
                            symbol_map[quote_key] = (strike, inst_type, trading_symbol, token)

            # Fetch full quotes in batch (bid, ask, oi, volume)
            full_quotes = {}
            if symbols_to_quote and self.kite:
                try:
                    # Kite quote() returns full depth
                    full_quotes = self.kite.quote(symbols_to_quote)
                except Exception as e:
                    logger.warning(f"Full quote failed, falling back to LTP: {e}")
                    try:
                        ltp_quotes = self.kite.ltp(symbols_to_quote)
                        for k, v in ltp_quotes.items():
                            full_quotes[k] = {'last_price': v.get('last_price', 0)}
                    except:
                        pass

            # Process quotes
            for quote_key, (strike, inst_type, trading_symbol, token) in symbol_map.items():
                quote = full_quotes.get(quote_key, {})

                # Extract bid-ask from depth
                depth = quote.get('depth', {})
                buy_depth = depth.get('buy', [{}])
                sell_depth = depth.get('sell', [{}])

                bid = buy_depth[0].get('price', 0) if buy_depth else 0
                bid_qty = buy_depth[0].get('quantity', 0) if buy_depth else 0
                ask = sell_depth[0].get('price', 0) if sell_depth else 0
                ask_qty = sell_depth[0].get('quantity', 0) if sell_depth else 0

                ltp = quote.get('last_price', 0)
                oi = quote.get('oi', 0)
                volume = quote.get('volume', 0)

                # Create OptionQuote with all data
                option_quote = OptionQuote(
                    strike=strike,
                    option_type=inst_type,
                    ltp=ltp,
                    bid=bid,
                    ask=ask,
                    bid_qty=bid_qty,
                    ask_qty=ask_qty,
                    oi=oi,
                    volume=volume,
                    trading_symbol=trading_symbol,
                    token=token
                )

                # Calculate Greeks
                if dte > 0 and ltp > 0:
                    option_quote.delta, option_quote.theta = self._calculate_greeks(
                        spot, strike, dte, ltp, inst_type
                    )

                # Store as dict for backward compatibility
                option_data = {
                    'strike': strike,
                    'ltp': ltp,
                    'bid': bid,
                    'ask': ask,
                    'bid_qty': bid_qty,
                    'ask_qty': ask_qty,
                    'spread': option_quote.spread,
                    'spread_pct': option_quote.spread_pct,
                    'mid_price': option_quote.mid_price,
                    'oi': oi,
                    'volume': volume,
                    'trading_symbol': trading_symbol,
                    'token': token,
                    'delta': option_quote.delta,
                    'theta': option_quote.theta,
                    'liquidity': option_quote.liquidity_score,
                    'is_liquid': option_quote.is_liquid,
                    'quote': option_quote  # Full object for advanced use
                }

                if inst_type == 'CE':
                    calls[strike] = option_data
                elif inst_type == 'PE':
                    puts[strike] = option_data

        except Exception as e:
            logger.error(f"Failed to fetch option chain: {e}")
            # Return empty chain with strikes
            for strike in strikes:
                calls[strike] = self._empty_option_data(strike)
                puts[strike] = self._empty_option_data(strike)

        return {
            'symbol': symbol.upper(),
            'spot': spot,
            'atm': atm,
            'expiry': expiry,
            'dte': dte,
            'strikes': strikes,
            'calls': calls,
            'puts': puts,
            'lot_size': self.get_lot_size(symbol),
            'strike_gap': gap
        }

    def _empty_option_data(self, strike: int) -> Dict[str, Any]:
        """Return empty option data structure."""
        return {
            'strike': strike, 'ltp': 0, 'bid': 0, 'ask': 0,
            'bid_qty': 0, 'ask_qty': 0, 'spread': 0, 'spread_pct': 0,
            'mid_price': 0, 'oi': 0, 'volume': 0,
            'trading_symbol': '', 'token': None,
            'delta': 0, 'theta': 0, 'liquidity': 'UNKNOWN', 'is_liquid': False
        }

    def _calculate_greeks(self, spot: float, strike: int, dte: int,
                          premium: float, option_type: str) -> Tuple[float, float]:
        """
        Calculate approximate delta and theta.

        Uses simplified Black-Scholes approximations.
        For accurate Greeks, use a proper options pricing library.
        """
        try:
            # Time to expiry in years
            t = dte / 365.0
            if t <= 0:
                return (0.5 if option_type == 'CE' else -0.5, 0)

            # Approximate IV from premium (very rough)
            # Using ATM approximation: premium ≈ 0.4 * spot * sigma * sqrt(t)
            if premium > 0 and spot > 0:
                iv = premium / (0.4 * spot * math.sqrt(t))
                iv = min(max(iv, 0.1), 2.0)  # Clamp to reasonable range
            else:
                iv = 0.2  # Default 20% IV

            # Moneyness
            moneyness = math.log(spot / strike) if strike > 0 else 0

            # Simplified delta calculation
            # Delta ≈ N(d1) for calls, N(d1) - 1 for puts
            d1 = (moneyness + 0.5 * iv * iv * t) / (iv * math.sqrt(t)) if iv > 0 and t > 0 else 0

            # Approximate normal CDF
            def norm_cdf(x):
                return 0.5 * (1 + math.erf(x / math.sqrt(2)))

            delta = norm_cdf(d1) if option_type == 'CE' else norm_cdf(d1) - 1

            # Simplified theta calculation (daily decay)
            # Theta ≈ -premium / dte (very rough approximation)
            theta = -premium / dte if dte > 0 else 0

            return (round(delta, 3), round(theta, 2))

        except Exception as e:
            logger.debug(f"Greeks calculation error: {e}")
            return (0.5 if option_type == 'CE' else -0.5, 0)

    def get_lot_size(self, symbol: str) -> int:
        """Get lot size for symbol."""
        if symbol.upper() in self.LOT_SIZES:
            return self.LOT_SIZES[symbol.upper()]

        # Try to get from instruments
        try:
            instruments = self._get_instruments(symbol)
            if instruments:
                return instruments[0].get('lot_size', 1)
        except:
            pass

        return 1  # Default for stocks

    def _get_instruments(self, symbol: str) -> List[Dict[str, Any]]:
        """Get instruments for symbol from cache or API."""
        cache_key = symbol.upper()

        if cache_key in self._instruments_cache:
            return self._instruments_cache[cache_key]

        if not self.kite:
            return []

        try:
            # Determine exchange
            if symbol.upper() == 'SENSEX':
                exchange = 'BFO'
            else:
                exchange = 'NFO'

            # Fetch all instruments for exchange
            all_instruments = self.kite.instruments(exchange)

            # Filter for our symbol
            filtered = [
                i for i in all_instruments
                if i.get('name', '').upper() == symbol.upper()
            ]

            self._instruments_cache[cache_key] = filtered
            return filtered

        except Exception as e:
            logger.error(f"Failed to fetch instruments: {e}")
            return []

    def get_quote(self, trading_symbol: str, exchange: str = 'NFO') -> Dict[str, Any]:
        """
        Get full quote for a trading symbol.

        Args:
            trading_symbol: Option trading symbol
            exchange: Exchange (NFO, BFO)

        Returns:
            Quote dict with ltp, bid, ask, volume, oi, etc.
        """
        if not self.kite:
            return {}

        try:
            quote_key = f"{exchange}:{trading_symbol}"
            quotes = self.kite.quote([quote_key])

            if quote_key in quotes:
                return quotes[quote_key]
            return {}

        except Exception as e:
            logger.error(f"Failed to get quote: {e}")
            return {}

    def suggest_expiry(self, symbol: str, strategy_type: str = 'spread') -> datetime:
        """
        Suggest appropriate expiry based on symbol and strategy.

        Rules:
        - NIFTY/SENSEX: Weekly expiry (more liquid)
        - Stock options: Monthly (avoid gamma risk)
        - Avoid < 3 DTE for stocks

        Args:
            symbol: Underlying symbol
            strategy_type: Type of strategy

        Returns:
            Suggested expiry date
        """
        today = datetime.now()

        # Get available expiries
        is_index = symbol.upper() in self.WEEKLY_INDICES

        if is_index and symbol.upper() in {'NIFTY', 'SENSEX'}:
            # Use weekly for NIFTY/SENSEX
            expiries = self.get_expiries(symbol, weekly=True)
            if not expiries:
                expiries = self.get_expiries(symbol, weekly=None)
        else:
            # Use monthly for stocks and other indices
            expiries = self.get_expiries(symbol, weekly=False)
            if not expiries:
                expiries = self.get_expiries(symbol, weekly=None)

        if not expiries:
            # Generate default
            days_until_thursday = (3 - today.weekday()) % 7
            if days_until_thursday < 3:
                days_until_thursday += 7
            return today + timedelta(days=days_until_thursday)

        # Filter out expiries with < 3 DTE for stocks
        if not is_index:
            expiries = [e for e in expiries if (e - today).days >= 3]

        # Return nearest valid expiry
        for exp in expiries:
            if exp > today:
                return exp

        return expiries[-1] if expiries else today + timedelta(days=7)

    def clear_cache(self):
        """Clear all caches."""
        self._instruments_cache.clear()
        self._spot_cache.clear()
        self._chain_cache.clear()
