"""
NEO Trade Terminal - Kite Spot Price Fetcher

Fetches live NIFTY/BANKNIFTY spot prices from Kite for ATM calculation.
Uses existing Kite credentials.
Also provides Kite WebSocket (KiteTicker) for real-time LTP streaming.
"""

from kiteconnect import KiteConnect, KiteTicker
import os
import json
import logging
import threading
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple, Callable

from core.supertrend import calculate_supertrend, SupertrendResult

logger = logging.getLogger(__name__)


class KiteSpotFetcher:
    """Fetches spot prices from Kite for ATM strike calculation."""

    def __init__(self, config: Dict[str, Any], symbol_mapper=None):
        self.config = config
        self.kite: Optional[KiteConnect] = None
        self._connected = False
        self.symbol_mapper = symbol_mapper  # For getting actual expiry dates from scrip master

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
                    str(Path(__file__).resolve().parent.parent.parent / 'data' / 'kite_access_token.json'),  # S38: Derived absolute fallback
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

    def set_symbol_mapper(self, symbol_mapper):
        """Set symbol mapper for expiry date lookups."""
        self.symbol_mapper = symbol_mapper

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
                          use_monthly: bool = True, min_dte: int = 0) -> str:
        """
        Generate Kite-format option symbol.

        Args:
            underlying: NIFTY, BANKNIFTY, etc.
            opt_type: CE or PE
            strike_offset: 0 for ATM, +1 for OTM1, -1 for ITM1, etc.
            expiry_date: Specific expiry date (None = auto based on use_monthly)
            use_monthly: True for monthly expiry (26JAN), False for weekly (26116)
            min_dte: Minimum days to expiry (default 0). If expiry is closer, select next expiry.

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

        # Get expiry date - prefer actual dates from scrip master
        if expiry_date is None:
            # Try to get actual expiry from scrip master (primary source)
            actual_expiry = None
            if self.symbol_mapper:
                try:
                    actual_expiry = self.symbol_mapper.get_nearest_expiry(underlying, use_monthly)
                    if actual_expiry:
                        expiry_date = datetime.combine(actual_expiry, datetime.min.time())
                        logger.debug(f"Got expiry from scrip master: {actual_expiry} for {underlying}")
                except Exception as e:
                    logger.warning(f"Failed to get expiry from scrip master: {e}")

            # Fallback to calculated expiry if scrip master failed
            if expiry_date is None:
                logger.debug(f"Using calculated expiry for {underlying} (scrip master unavailable)")
                if use_monthly:
                    expiry_date = self.get_monthly_expiry(0, underlying)
                    # If monthly expiry already passed this month, get next month
                    if expiry_date.date() < datetime.now().date():
                        expiry_date = self.get_monthly_expiry(1, underlying)
                else:
                    expiry_date = self._get_weekly_expiry(underlying)

        # S35: Check minimum DTE and get next expiry if too close
        if min_dte > 0 and expiry_date is not None:
            today = datetime.now().date()
            dte = (expiry_date.date() - today).days

            if dte < min_dte:
                logger.info(f"[DTE] {underlying} expiry {expiry_date.date()} has {dte} DTE < {min_dte}, getting next expiry")

                if use_monthly:
                    # Get next month's expiry
                    next_expiry = self.get_monthly_expiry(1, underlying)
                    if next_expiry.date() <= expiry_date.date():
                        next_expiry = self.get_monthly_expiry(2, underlying)
                    expiry_date = next_expiry
                else:
                    # Get next week's expiry
                    next_expiry = self._get_weekly_expiry(underlying)
                    next_expiry = next_expiry + timedelta(days=7)
                    expiry_date = next_expiry

                new_dte = (expiry_date.date() - today).days
                logger.info(f"[DTE] Selected next expiry: {expiry_date.date()} ({new_dte} DTE)")

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
        NSE indices (NIFTY) expire on Thursday.
        BSE indices (SENSEX) expire on Friday.

        Args:
            underlying: Index name

        Returns:
            Expiry datetime
        """
        today = datetime.now()

        # SENSEX expires on Friday (weekday=4), NIFTY on Thursday (weekday=3)
        if underlying.upper() in ['SENSEX', 'BANKEX']:
            expiry_weekday = 4  # Friday
        else:
            expiry_weekday = 3  # Thursday

        # Find next expiry day
        days_until_expiry = (expiry_weekday - today.weekday()) % 7

        # If it's expiry day after market hours (15:30), get next week
        if days_until_expiry == 0 and (today.hour > 15 or (today.hour == 15 and today.minute >= 30)):
            days_until_expiry = 7

        expiry = today + timedelta(days=days_until_expiry)
        return expiry

    def get_monthly_expiry(self, month_offset: int = 0, underlying: str = None) -> datetime:
        """
        Get monthly expiry date.
        NSE indices: Last Thursday of month
        BSE indices (SENSEX/BANKEX): Last Friday of month

        Args:
            month_offset: 0 for current month, 1 for next month, etc.
            underlying: Index name (to determine NSE vs BSE expiry day)

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

        # BSE indices expire on Friday (weekday=4), NSE on Thursday (weekday=3)
        if underlying and underlying.upper() in ['SENSEX', 'BANKEX']:
            expiry_weekday = 4  # Friday
        else:
            expiry_weekday = 3  # Thursday

        # Find last expiry day of target month
        # Start from end of month and go backwards
        if month == 12:
            next_month = datetime(year + 1, 1, 1)
        else:
            next_month = datetime(year, month + 1, 1)

        last_day = next_month - timedelta(days=1)

        # Find last occurrence of expiry_weekday
        days_since_expiry_day = (last_day.weekday() - expiry_weekday) % 7
        last_expiry = last_day - timedelta(days=days_since_expiry_day)

        return last_expiry

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
            # S31: Determine exchange based on symbol type
            # - BFO: SENSEX/BANKEX options
            # - NFO: NIFTY/BANKNIFTY/FINNIFTY options (contains CE/PE/FUT)
            # - NSE: Equity symbols (ICICIBANK, RELIANCE, etc.)
            if symbol.startswith('SENSEX') or symbol.startswith('BANKEX'):
                instrument = f"BFO:{symbol}"
            elif 'CE' in symbol or 'PE' in symbol or 'FUT' in symbol:
                instrument = f"NFO:{symbol}"
            else:
                # Equity symbol - strip -EQ suffix if present
                clean_symbol = symbol.replace('-EQ', '')
                instrument = f"NSE:{clean_symbol}"

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

    def get_quotes_batch(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Get quotes for multiple symbols in a single API call.
        Much faster than calling get_quote() for each symbol.

        Args:
            symbols: List of symbols (Kite format)

        Returns:
            Dict mapping symbol to quote data
        """
        if not self.kite or not symbols:
            return {}

        try:
            # Build instrument list
            instruments = []
            symbol_to_instrument = {}

            for symbol in symbols:
                if symbol.startswith('SENSEX') or symbol.startswith('BANKEX'):
                    instrument = f"BFO:{symbol}"
                elif 'CE' in symbol or 'PE' in symbol or 'FUT' in symbol:
                    instrument = f"NFO:{symbol}"
                else:
                    # Equity symbol - strip -EQ suffix if present
                    clean_symbol = symbol.replace('-EQ', '')
                    instrument = f"NSE:{clean_symbol}"
                instruments.append(instrument)
                symbol_to_instrument[symbol] = instrument

            # Batch fetch quotes
            quotes = self.kite.quote(instruments)

            # Map back to symbols
            result = {}
            for symbol, instrument in symbol_to_instrument.items():
                if instrument in quotes:
                    result[symbol] = quotes[instrument]

            return result

        except Exception as e:
            logger.warning(f"Failed to batch fetch quotes: {e}")
            return {}

    def get_historical_data(
        self,
        instrument_token: int,
        interval: str,
        from_date: datetime,
        to_date: datetime
    ) -> List[Dict[str, Any]]:
        """
        Fetch historical OHLC candles from Kite.

        Args:
            instrument_token: Kite instrument token
            interval: Candle interval ('5minute', '10minute', '15minute', '30minute', '60minute', 'day')
            from_date: Start date
            to_date: End date

        Returns:
            List of candle dicts with 'date', 'open', 'high', 'low', 'close', 'volume'
        """
        if not self.kite:
            logger.warning("Kite not connected - cannot fetch historical data")
            return []

        try:
            data = self.kite.historical_data(
                instrument_token=instrument_token,
                from_date=from_date,
                to_date=to_date,
                interval=interval
            )
            return data
        except Exception as e:
            logger.error(f"Failed to fetch historical data for token {instrument_token}: {e}")
            return []

    def _aggregate_candles(self, candles: List[Dict], factor: int, base_minutes: int = 5) -> List[Dict]:
        """
        Aggregate candles by a factor with proper time boundary alignment.

        Args:
            candles: List of OHLC candles with 'date' as datetime
            factor: Number of candles to combine (2 for 10m from 5m)
            base_minutes: Base candle interval in minutes (default 5)

        Returns:
            Aggregated candles aligned to proper time boundaries
        """
        if not candles or factor <= 1:
            return candles

        target_minutes = base_minutes * factor  # e.g., 5 * 2 = 10 minutes

        # Group candles by their target timeframe bucket
        buckets: Dict[datetime, List[Dict]] = {}

        for candle in candles:
            candle_time = candle.get('date')
            if not candle_time:
                continue

            # Handle both datetime and string formats
            if isinstance(candle_time, str):
                try:
                    candle_time = datetime.strptime(candle_time, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue

            # Calculate the bucket start time (align to target_minutes boundary)
            # For 10m: 09:15->09:10, 09:17->09:10, 09:25->09:20
            # For 15m: 09:15->09:15, 09:20->09:15, 09:30->09:30
            # For 30m: 09:15->09:00, 09:45->09:30
            # For 60m: 09:15->09:00, 10:30->10:00
            # For 120m: 09:15->09:00, 11:30->11:00

            total_minutes = candle_time.hour * 60 + candle_time.minute
            bucket_minutes = (total_minutes // target_minutes) * target_minutes
            bucket_hour = bucket_minutes // 60
            bucket_minute = bucket_minutes % 60

            bucket_time = candle_time.replace(hour=bucket_hour, minute=bucket_minute, second=0, microsecond=0)

            if bucket_time not in buckets:
                buckets[bucket_time] = []
            buckets[bucket_time].append(candle)

        # Convert buckets to aggregated candles
        aggregated = []
        for bucket_time in sorted(buckets.keys()):
            group = buckets[bucket_time]
            if not group:
                continue

            # Only include complete candles (have all expected sub-candles)
            # Exception: include current forming candle (last bucket)
            is_last_bucket = bucket_time == max(buckets.keys())

            if len(group) < factor and not is_last_bucket:
                # Incomplete historical candle - skip
                continue

            agg_candle = {
                'date': bucket_time,
                'open': group[0]['open'],
                'high': max(c['high'] for c in group),
                'low': min(c['low'] for c in group),
                'close': group[-1]['close'],
                'volume': sum(c.get('volume', 0) for c in group),
            }
            aggregated.append(agg_candle)

        return aggregated

    def get_index_supertrends_optimized(
        self,
        index_name: str,
        period: int = 10,
        multiplier: float = 3.0
    ) -> Dict[int, Optional[SupertrendResult]]:
        """
        Get Supertrend for ALL timeframes with a SINGLE API call.

        Fetches 5-minute candles and aggregates locally to create larger timeframes.
        This reduces API calls from 6 to 1 per index.

        Args:
            index_name: 'NIFTY', 'BANKNIFTY', or 'SENSEX'
            period: Supertrend period (default 10)
            multiplier: Supertrend multiplier (default 3.0)

        Returns:
            Dict mapping timeframe (minutes) to SupertrendResult
            {5: result, 10: result, 15: result, 30: result, 60: result, 120: result}
        """
        timeframes = [5, 10, 15, 30, 60, 120]
        results = {tf: None for tf in timeframes}

        # Get instrument token
        index_upper = index_name.upper()
        if index_upper in self.bse_tokens:
            token = self.bse_tokens[index_upper]
        elif index_upper in self.spot_tokens:
            token = self.spot_tokens[index_upper]
        else:
            logger.warning(f"Unknown index: {index_name}")
            return results

        # Calculate how many 5m candles we need
        # For 2H (120m) with period=10: need 10 * 24 = 240 5m candles minimum
        # Fetch 15 days to be safe (~75 candles/day × 15 = 1125 candles)
        to_date = datetime.now()
        from_date = to_date - timedelta(days=15)

        # Single API call - fetch 5-minute candles
        candles_5m = self.get_historical_data(token, '5minute', from_date, to_date)

        if not candles_5m:
            logger.warning(f"No 5m candles for {index_name}")
            return results

        logger.debug(f"[ST] {index_name}: Fetched {len(candles_5m)} 5m candles")

        # Aggregation factors from 5m base
        # 5m → Xm requires factor of X/5
        aggregation_factors = {
            5: 1,    # No aggregation
            10: 2,   # 2 × 5m = 10m
            15: 3,   # 3 × 5m = 15m
            30: 6,   # 6 × 5m = 30m
            60: 12,  # 12 × 5m = 60m (1H)
            120: 24, # 24 × 5m = 120m (2H)
        }

        min_candles_needed = period + 5  # Need period + buffer for reliable ST

        for tf in timeframes:
            try:
                factor = aggregation_factors[tf]

                if factor == 1:
                    # 5m - use directly
                    candles = candles_5m
                else:
                    # Aggregate to target timeframe
                    candles = self._aggregate_candles(candles_5m, factor)

                if not candles or len(candles) < min_candles_needed:
                    logger.warning(f"[ST] {index_name} {tf}m: Not enough candles ({len(candles) if candles else 0})")
                    continue

                # Calculate supertrend
                results[tf] = calculate_supertrend(candles, period, multiplier)

            except Exception as e:
                logger.error(f"[ST] {index_name} {tf}m calculation failed: {e}")

        return results

    def get_index_supertrend(
        self,
        index_name: str,
        interval_minutes: int,
        period: int = 10,
        multiplier: float = 3.0
    ) -> Optional[SupertrendResult]:
        """
        Calculate Supertrend for an index at a specific timeframe.

        Args:
            index_name: 'NIFTY', 'BANKNIFTY', or 'SENSEX'
            interval_minutes: Candle interval in minutes (5, 10, 15, 30, 60, 120)
            period: Supertrend period (default 10)
            multiplier: Supertrend multiplier (default 3.0)

        Returns:
            SupertrendResult or None if calculation fails
        """
        # Map interval minutes to Kite interval string
        # Note: Kite only supports up to 60minute, no 2hour
        # For 2H, we'll use 60minute data and calculate ST from it
        interval_map = {
            5: '5minute',
            10: '10minute',
            15: '15minute',
            30: '30minute',
            60: '60minute',
            120: '60minute',  # Use 60m candles for 2H calculation
        }

        interval_str = interval_map.get(interval_minutes)
        if not interval_str:
            logger.warning(f"Invalid interval: {interval_minutes} minutes")
            return None

        # Get instrument token
        index_upper = index_name.upper()
        if index_upper in self.bse_tokens:
            token = self.bse_tokens[index_upper]
        elif index_upper in self.spot_tokens:
            token = self.spot_tokens[index_upper]
        else:
            logger.warning(f"Unknown index: {index_name}")
            return None

        # Calculate date range - need enough candles for ATR + some buffer
        # For period=10, we need at least 20-30 candles
        to_date = datetime.now()

        # Calculate from_date based on interval
        if interval_minutes <= 15:
            days_back = 3
        elif interval_minutes <= 60:
            days_back = 7
        else:
            days_back = 15

        from_date = to_date - timedelta(days=days_back)

        # Fetch historical data
        candles = self.get_historical_data(token, interval_str, from_date, to_date)

        if not candles:
            logger.warning(f"No historical data for {index_name} @ {interval_minutes}m")
            return None

        # For 2H timeframe, aggregate 60m candles into 2H candles
        if interval_minutes == 120:
            candles = self._aggregate_candles(candles, 2)
            if not candles:
                logger.warning(f"Not enough candles to aggregate for {index_name} @ 2H")
                return None

        # Calculate Supertrend
        result = calculate_supertrend(candles, period, multiplier)

        if result:
            logger.debug(f"[ST] {index_name} {interval_minutes}m: {result.direction}")

        return result

    def get_all_index_supertrends(
        self,
        indices: List[str] = None,
        timeframes: List[int] = None,
        period: int = 10,
        multiplier: float = 3.0
    ) -> Dict[str, Dict[int, str]]:
        """
        Get Supertrend directions for multiple indices and timeframes.

        Args:
            indices: List of index names (default: ['NIFTY', 'BANKNIFTY', 'SENSEX'])
            timeframes: List of intervals in minutes (default: [5, 10, 15, 30, 60, 120])
            period: Supertrend period
            multiplier: Supertrend multiplier

        Returns:
            Nested dict: {index_name: {timeframe_minutes: direction}}
            Example: {'NIFTY': {5: 'UP', 10: 'DOWN', ...}, ...}
        """
        if indices is None:
            indices = ['NIFTY', 'BANKNIFTY', 'SENSEX']

        if timeframes is None:
            timeframes = [5, 10, 15, 30, 60, 120]

        results = {}

        for index_name in indices:
            results[index_name] = {}

            for tf in timeframes:
                try:
                    st_result = self.get_index_supertrend(index_name, tf, period, multiplier)
                    if st_result:
                        results[index_name][tf] = st_result.direction
                    else:
                        results[index_name][tf] = None
                except Exception as e:
                    logger.error(f"ST calc failed for {index_name} {tf}m: {e}")
                    results[index_name][tf] = None

        return results


class KiteWebSocket:
    """
    Kite WebSocket (KiteTicker) for real-time LTP streaming.
    More reliable than NEO WebSocket for market data.
    """

    # Index tokens for Kite (instrument_token values)
    INDEX_TOKENS = {
        256265: 'NIFTY',      # NIFTY 50
        260105: 'BANKNIFTY',  # NIFTY BANK
        257801: 'FINNIFTY',   # NIFTY FIN SERVICE
        265: 'SENSEX',        # BSE SENSEX
        274: 'BANKEX',        # BSE BANKEX
    }

    # Reverse mapping for subscription
    INDEX_NAMES = {v: k for k, v in INDEX_TOKENS.items()}

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.ticker: Optional[KiteTicker] = None
        self._api_key: Optional[str] = None
        self._access_token: Optional[str] = None
        self._connected = False
        self._lock = threading.Lock()

        # Callbacks
        self._on_ltp_callback: Optional[Callable[[int, float], None]] = None
        self._on_connect_callback: Optional[Callable[[], None]] = None
        self._on_disconnect_callback: Optional[Callable[[], None]] = None

        # Subscribed tokens
        self._subscribed_tokens: set = set()

        # LTP cache
        self._ltp_cache: Dict[int, float] = {}

    def set_credentials(self, api_key: str, access_token: str):
        """Set Kite credentials for WebSocket."""
        self._api_key = api_key
        self._access_token = access_token

    def load_credentials(self) -> bool:
        """Load Kite credentials from config or token file."""
        try:
            kite_creds = self.config.get('kite_credentials') or {}
            api_key = kite_creds.get('api_key')
            access_token = kite_creds.get('access_token')

            # Try to read from existing Kite session file
            if not api_key or not access_token:
                paths_config = self.config.get('paths', {})
                token_paths = [
                    paths_config.get('kite_token', '../data/kite_access_token.json'),
                    '../data/kite_access_token.json',
                    'C:/Users/mail2/Documents/Projects/BOTS/data/kite_access_token.json',
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
                                    break
                        except Exception:
                            pass

            if api_key and access_token:
                self._api_key = api_key
                self._access_token = access_token
                return True

            return False

        except Exception as e:
            logger.error(f"Failed to load Kite credentials: {e}")
            return False

    def connect(self) -> bool:
        """Connect to Kite WebSocket."""
        if not self._api_key or not self._access_token:
            if not self.load_credentials():
                logger.error("Kite credentials not available")
                return False

        try:
            self.ticker = KiteTicker(self._api_key, self._access_token)

            # Set callbacks
            self.ticker.on_ticks = self._on_ticks
            self.ticker.on_connect = self._on_connect
            self.ticker.on_close = self._on_close
            self.ticker.on_error = self._on_error
            self.ticker.on_reconnect = self._on_reconnect

            # Connect in background thread (non-blocking)
            self.ticker.connect(threaded=True)
            logger.info("[KITE WS] Connecting...")
            return True

        except Exception as e:
            logger.error(f"[KITE WS] Connection failed: {e}")
            return False

    def disconnect(self):
        """Disconnect from Kite WebSocket."""
        if self.ticker:
            try:
                self.ticker.close()
            except Exception:
                pass
        with self._lock:
            self._connected = False

    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        with self._lock:
            return self._connected

    def subscribe_indices(self, indices: List[str] = None):
        """
        Subscribe to index LTP feeds.

        Args:
            indices: List of index names like ['NIFTY', 'BANKNIFTY', 'SENSEX']
                    If None, subscribes to all indices.
        """
        if indices is None:
            indices = ['NIFTY', 'BANKNIFTY', 'SENSEX']

        tokens = []
        for idx in indices:
            idx_upper = idx.upper()
            if idx_upper in self.INDEX_NAMES:
                tokens.append(self.INDEX_NAMES[idx_upper])

        if tokens:
            self._subscribe_tokens(tokens)

    def subscribe_tokens(self, tokens: List[int]):
        """
        Subscribe to specific instrument tokens for LTP.

        Args:
            tokens: List of instrument tokens
        """
        self._subscribe_tokens(tokens)

    def _subscribe_tokens(self, tokens: List[int]):
        """Internal method to subscribe to tokens."""
        if not self.ticker:
            return

        try:
            # Add to tracked set
            with self._lock:
                self._subscribed_tokens.update(tokens)

            # Subscribe with LTP mode (lighter than full quote)
            self.ticker.subscribe(tokens)
            self.ticker.set_mode(self.ticker.MODE_LTP, tokens)
            logger.info(f"[KITE WS] Subscribed to {len(tokens)} tokens: {tokens[:5]}...")

        except Exception as e:
            logger.error(f"[KITE WS] Subscribe failed: {e}")

    def unsubscribe_tokens(self, tokens: List[int]):
        """Unsubscribe from tokens."""
        if not self.ticker:
            return

        try:
            self.ticker.unsubscribe(tokens)
            with self._lock:
                self._subscribed_tokens.difference_update(tokens)
        except Exception:
            pass

    def set_on_ltp(self, callback: Callable[[int, float], None]):
        """Set callback for LTP updates: callback(token, ltp)"""
        self._on_ltp_callback = callback

    def set_on_connect(self, callback: Callable[[], None]):
        """Set callback for connection."""
        self._on_connect_callback = callback

    def set_on_disconnect(self, callback: Callable[[], None]):
        """Set callback for disconnection."""
        self._on_disconnect_callback = callback

    def get_cached_ltp(self, token: int) -> Optional[float]:
        """Get cached LTP for a token."""
        with self._lock:
            return self._ltp_cache.get(token)

    def get_index_ltp(self, index_name: str) -> Optional[float]:
        """Get cached LTP for an index by name."""
        token = self.INDEX_NAMES.get(index_name.upper())
        if token:
            return self.get_cached_ltp(token)
        return None

    # ==================== Internal Callbacks ====================

    def _on_ticks(self, ws, ticks):
        """Handle incoming ticks."""
        for tick in ticks:
            token = tick.get('instrument_token')
            ltp = tick.get('last_price')

            if token is not None and ltp is not None:
                # Update cache
                with self._lock:
                    self._ltp_cache[token] = ltp

                # Fire callback
                if self._on_ltp_callback:
                    try:
                        self._on_ltp_callback(token, ltp)
                    except Exception as e:
                        logger.error(f"[KITE WS] LTP callback error: {e}")

    def _on_connect(self, ws, response):
        """Handle connection established."""
        logger.info(f"[KITE WS] Connected: {response}")
        with self._lock:
            self._connected = True

        # Resubscribe to previously subscribed tokens
        with self._lock:
            tokens = list(self._subscribed_tokens)
        if tokens:
            try:
                self.ticker.subscribe(tokens)
                self.ticker.set_mode(self.ticker.MODE_LTP, tokens)
                logger.info(f"[KITE WS] Resubscribed to {len(tokens)} tokens")
            except Exception as e:
                logger.error(f"[KITE WS] Resubscribe failed: {e}")

        if self._on_connect_callback:
            try:
                self._on_connect_callback()
            except Exception:
                pass

    def _on_close(self, ws, code, reason):
        """Handle connection closed."""
        logger.info(f"[KITE WS] Disconnected: {code} - {reason}")
        with self._lock:
            self._connected = False

        if self._on_disconnect_callback:
            try:
                self._on_disconnect_callback()
            except Exception:
                pass

    def _on_error(self, ws, code, reason):
        """Handle WebSocket error."""
        logger.error(f"[KITE WS] Error: {code} - {reason}")

    def _on_reconnect(self, ws, attempts_count):
        """Handle reconnection attempt."""
        logger.info(f"[KITE WS] Reconnecting... attempt {attempts_count}")
