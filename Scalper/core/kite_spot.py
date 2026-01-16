"""
NEO Trade Terminal - Kite Spot Price Fetcher

Fetches live NIFTY/BANKNIFTY spot prices from Kite for ATM calculation.
Uses existing Kite credentials.
"""

from kiteconnect import KiteConnect
import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger(__name__)


class KiteSpotFetcher:
    """Fetches spot prices from Kite for ATM strike calculation."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.kite: Optional[KiteConnect] = None
        self._connected = False

        # Index tokens (NSE)
        self.spot_tokens = {
            'NIFTY': 256265,      # NIFTY 50 token
            'NIFTY 50': 256265,
            'BANKNIFTY': 260105,  # BANK NIFTY token
            'NIFTY BANK': 260105,
            'FINNIFTY': 257801,   # FINNIFTY token
            'NIFTY FIN SERVICE': 257801,
            'MIDCPNIFTY': 288009, # MIDCAP NIFTY token
            'NIFTY MIDCAP SELECT': 288009,
        }

        # Strike gaps for each index
        indices_config = config.get('indices', {})
        self.strike_gaps = {
            'NIFTY': indices_config.get('NIFTY', {}).get('strike_gap', 50),
            'BANKNIFTY': indices_config.get('BANKNIFTY', {}).get('strike_gap', 100),
            'FINNIFTY': indices_config.get('FINNIFTY', {}).get('strike_gap', 50),
            'SENSEX': indices_config.get('SENSEX', {}).get('strike_gap', 100),
            'BANKEX': indices_config.get('BANKEX', {}).get('strike_gap', 100),
            'MIDCPNIFTY': indices_config.get('MIDCPNIFTY', {}).get('strike_gap', 50),
        }

        # BSE index tokens (for SENSEX/BANKEX)
        self.bse_tokens = {
            'SENSEX': 265,
            'BANKEX': 274,
        }

    def connect(self) -> Tuple[bool, str]:
        """
        Connect to Kite using existing credentials.

        Returns:
            Tuple of (success: bool, message: str)
        """
        try:
            kite_creds = self.config.get('kite_credentials') or {}
            api_key = kite_creds.get('api_key') if kite_creds else None
            access_token = kite_creds.get('access_token') if kite_creds else None

            # Try to read from existing Kite session file (shared across BOTS projects)
            if not api_key or not access_token:
                paths_config = self.config.get('paths', {})

                # Try primary path first, then backup
                token_paths = [
                    paths_config.get('kite_token', '../data/kite_access_token.json'),
                    paths_config.get('kite_token_backup', ''),
                    '../data/kite_access_token.json',  # Relative fallback
                    'C:/Users/mail2/Documents/Projects/BOTS/data/kite_access_token.json',  # Absolute fallback
                ]

                for token_path in token_paths:
                    if not token_path:
                        continue
                    token_path = os.path.expanduser(token_path)
                    if os.path.exists(token_path):
                        try:
                            with open(token_path) as f:
                                session = json.load(f)
                                api_key = session.get('api_key')
                                access_token = session.get('access_token')
                                if api_key and access_token:
                                    logger.info(f"Loaded Kite token from: {token_path}")
                                    break
                        except Exception as e:
                            logger.warning(f"Failed to read {token_path}: {e}")

            if not api_key or not access_token:
                return False, "Kite credentials not found - check ../data/kite_access_token.json"

            self.kite = KiteConnect(api_key=api_key)
            self.kite.set_access_token(access_token)

            # Test connection
            profile = self.kite.profile()
            if profile:
                self._connected = True
                user_name = profile.get('user_name', 'User')
                return True, f"Kite connected: {user_name}"

            return False, "Kite connection failed - no profile returned"

        except Exception as e:
            logger.error(f"Kite connection failed: {e}", exc_info=True)
            return False, f"Kite connection failed: {str(e)}"

    def is_connected(self) -> bool:
        """Check if Kite is connected."""
        return self._connected and self.kite is not None

    def get_spot_price(self, underlying: str) -> float:
        """
        Get current spot price for underlying index.

        Args:
            underlying: Index name (NIFTY, BANKNIFTY, etc.)

        Returns:
            Current spot price

        Raises:
            Exception if not connected or index not found
        """
        if not self.kite:
            raise Exception("Kite not connected")

        underlying = underlying.upper().replace(' ', '')

        # Map common names
        name_map = {
            'NIFTY50': 'NIFTY',
            'NIFTYBANK': 'BANKNIFTY',
            'BANKNIFTY': 'BANKNIFTY',
        }
        underlying = name_map.get(underlying, underlying)

        # Check if BSE index
        if underlying in self.bse_tokens:
            exchange = "BSE"
            instrument = f"BSE:{underlying}"
        else:
            exchange = "NSE"
            # Get proper name for Kite
            kite_names = {
                'NIFTY': 'NIFTY 50',
                'BANKNIFTY': 'NIFTY BANK',
                'FINNIFTY': 'NIFTY FIN SERVICE',
                'MIDCPNIFTY': 'NIFTY MIDCAP SELECT',
            }
            kite_name = kite_names.get(underlying, underlying)
            instrument = f"NSE:{kite_name}"

        try:
            quote = self.kite.quote([instrument])
            return quote[instrument]['last_price']
        except Exception:
            # Fallback to LTP
            try:
                ltp = self.kite.ltp([instrument])
                return ltp[instrument]['last_price']
            except Exception as e:
                logger.error(f"Failed to get spot price for {underlying}: {e}")
                raise

    def get_atm_strike(self, underlying: str) -> int:
        """
        Calculate ATM strike for underlying.

        Args:
            underlying: Index name

        Returns:
            ATM strike price
        """
        spot = self.get_spot_price(underlying)
        underlying = underlying.upper().replace(' ', '')

        # Normalize name
        name_map = {'NIFTY50': 'NIFTY', 'NIFTYBANK': 'BANKNIFTY'}
        underlying = name_map.get(underlying, underlying)

        gap = self.strike_gaps.get(underlying, 50)

        # Round to nearest strike
        atm = round(spot / gap) * gap
        return int(atm)

    def get_option_symbol(self, underlying: str, opt_type: str,
                          strike_offset: int = 0, expiry_date: datetime = None,
                          use_monthly: bool = True) -> str:
        """
        Generate Kite-format option symbol.

        Args:
            underlying: NIFTY, BANKNIFTY, etc.
            opt_type: CE or PE
            strike_offset: 0 for ATM, +1 for OTM1, -1 for ITM1, etc.
            expiry_date: Specific expiry date (None = auto based on use_monthly)
            use_monthly: True for monthly expiry (26JAN), False for weekly (26116)

        Returns:
            Symbol like NIFTY26JAN24000CE (monthly) or NIFTY2611624000CE (weekly)
        """
        underlying = underlying.upper().replace(' ', '')
        opt_type = opt_type.upper()

        # Normalize name
        name_map = {'NIFTY50': 'NIFTY', 'NIFTYBANK': 'BANKNIFTY'}
        underlying = name_map.get(underlying, underlying)

        atm = self.get_atm_strike(underlying)
        gap = self.strike_gaps.get(underlying, 50)
        strike = atm + (strike_offset * gap)

        # Indices with only monthly expiry (SEBI rule: only 1 index per exchange has weekly)
        # NSE: NIFTY has weekly, others monthly only
        # BSE: SENSEX has weekly, others monthly only
        monthly_only_indices = ['BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'BANKEX']
        weekly_indices = ['NIFTY', 'SENSEX']

        # Force monthly for indices that don't have weekly
        if underlying in monthly_only_indices:
            use_monthly = True

        # Get expiry date
        if expiry_date is None:
            if use_monthly:
                expiry_date = self.get_monthly_expiry(0)
                # If monthly expiry already passed this month, get next month
                if expiry_date.date() < datetime.now().date():
                    expiry_date = self.get_monthly_expiry(1)
            else:
                expiry_date = self._get_weekly_expiry(underlying)

        # Format expiry based on type
        if use_monthly:
            # Monthly format: NIFTY26JAN24000CE (YYMMMM)
            expiry_str = expiry_date.strftime("%y%b").upper()
        else:
            # Weekly format: NIFTY2511624000CE (YY M DD)
            # Where month is single digit for Jan-Sep, O/N/D for Oct/Nov/Dec
            year_str = expiry_date.strftime("%y")
            month = expiry_date.month
            if month <= 9:
                month_str = str(month)
            else:
                month_str = ['O', 'N', 'D'][month - 10]
            day_str = expiry_date.strftime("%d")
            expiry_str = f"{year_str}{month_str}{day_str}"

        return f"{underlying}{expiry_str}{strike}{opt_type}"

    def _get_weekly_expiry(self, underlying: str) -> datetime:
        """
        Get current weekly expiry date.
        NSE indices expire on Thursday (or previous trading day if holiday).

        Args:
            underlying: Index name

        Returns:
            Expiry datetime
        """
        today = datetime.now()

        # Find next Thursday
        days_until_thursday = (3 - today.weekday()) % 7

        # If it's Thursday after market hours, get next week
        if days_until_thursday == 0 and today.hour >= 15:
            days_until_thursday = 7

        expiry = today + timedelta(days=days_until_thursday)
        return expiry

    def get_monthly_expiry(self, month_offset: int = 0) -> datetime:
        """
        Get monthly expiry date (last Thursday of month).

        Args:
            month_offset: 0 for current month, 1 for next month, etc.

        Returns:
            Expiry datetime
        """
        today = datetime.now()

        # Calculate target month
        year = today.year
        month = today.month + month_offset

        while month > 12:
            month -= 12
            year += 1

        # Find last Thursday of target month
        # Start from end of month and go backwards
        if month == 12:
            next_month = datetime(year + 1, 1, 1)
        else:
            next_month = datetime(year, month + 1, 1)

        last_day = next_month - timedelta(days=1)

        # Find last Thursday
        days_since_thursday = (last_day.weekday() - 3) % 7
        last_thursday = last_day - timedelta(days=days_since_thursday)

        return last_thursday

    def get_option_chain_strikes(self, underlying: str,
                                  range_count: int = 5) -> Dict[str, Any]:
        """
        Get range of strikes around ATM.

        Args:
            underlying: Index name
            range_count: Number of strikes on each side of ATM

        Returns:
            Dict with spot, atm, strikes list, and symbol lists
        """
        underlying = underlying.upper().replace(' ', '')
        name_map = {'NIFTY50': 'NIFTY', 'NIFTYBANK': 'BANKNIFTY'}
        underlying = name_map.get(underlying, underlying)

        spot = self.get_spot_price(underlying)
        atm = self.get_atm_strike(underlying)
        gap = self.strike_gaps.get(underlying, 50)

        strikes = []
        ce_symbols = []
        pe_symbols = []

        expiry = self._get_weekly_expiry(underlying)
        year_str = expiry.strftime("%y")
        month = expiry.month
        if month <= 9:
            month_str = str(month)
        else:
            month_str = ['O', 'N', 'D'][month - 10]
        day_str = expiry.strftime("%d")
        expiry_str = f"{year_str}{month_str}{day_str}"

        for i in range(-range_count, range_count + 1):
            strike = atm + (i * gap)
            strikes.append(strike)

            ce_symbols.append(f"{underlying}{expiry_str}{strike}CE")
            pe_symbols.append(f"{underlying}{expiry_str}{strike}PE")

        return {
            'spot': spot,
            'atm': atm,
            'strikes': strikes,
            'ce_symbols': ce_symbols,
            'pe_symbols': pe_symbols,
            'expiry': expiry.strftime('%Y-%m-%d'),
            'expiry_str': expiry_str
        }

    def get_option_ltp(self, symbol: str) -> Optional[float]:
        """
        Get LTP for an option symbol.

        Args:
            symbol: Option symbol (Kite format)

        Returns:
            LTP or None if not found
        """
        if not self.kite:
            return None

        try:
            # Determine exchange based on underlying
            underlying = symbol[:5] if symbol.startswith('NIFTY') else symbol[:9]
            if underlying.startswith('SENSEX') or underlying.startswith('BANKEX'):
                instrument = f"BFO:{symbol}"
            else:
                instrument = f"NFO:{symbol}"

            ltp_data = self.kite.ltp([instrument])
            return ltp_data[instrument]['last_price']

        except Exception as e:
            logger.warning(f"Failed to get LTP for {symbol}: {e}")
            return None

    def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get full quote for a symbol.

        Args:
            symbol: Symbol (Kite format)

        Returns:
            Quote dict or None
        """
        if not self.kite:
            return None

        try:
            # Determine exchange
            if symbol.startswith('SENSEX') or symbol.startswith('BANKEX'):
                instrument = f"BFO:{symbol}"
            elif 'CE' in symbol or 'PE' in symbol or 'FUT' in symbol:
                instrument = f"NFO:{symbol}"
            else:
                instrument = f"NSE:{symbol}"

            quotes = self.kite.quote([instrument])
            return quotes.get(instrument)

        except Exception as e:
            logger.warning(f"Failed to get quote for {symbol}: {e}")
            return None
