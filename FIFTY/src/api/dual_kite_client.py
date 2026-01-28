"""
Dual Kite Client - Handles both read and trade Kite accounts for FIFTY

Read account: Shared token from SNAIL (for market data, historical data)
Trade account: Dedicated account for FIFTY (for orders, positions)
"""

import json
import os
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

import pandas as pd
from loguru import logger

from src.api.broker_adapter import BrokerAdapter
from src.utils.config_manager import config
from src.utils.timezone_helper import now_ist, today_ist, IST
from src.utils.circuit_breaker import get_kite_breaker, CircuitBreakerOpenError

try:
    from kiteconnect import KiteConnect
    KITECONNECT_AVAILABLE = True
except ImportError:
    KITECONNECT_AVAILABLE = False
    logger.warning("kiteconnect library not installed. Run: pip install kiteconnect")


class DualKiteClient(BrokerAdapter):
    """
    Dual Kite Client for FIFTY Bot

    Uses two separate Kite accounts:
    - Read client: For market data (shared token from SNAIL)
    - Trade client: For trading operations (dedicated FIFTY account)
    """

    def __init__(self):
        """Initialize Dual Kite Client"""
        if not KITECONNECT_AVAILABLE:
            raise ImportError("kiteconnect library required. Install with: pip install kiteconnect")

        self._read_client: Optional[KiteConnect] = None
        self._trade_client: Optional[KiteConnect] = None

        # Configuration
        self._read_token_file = config.get('kite_read.token_file')
        self._trade_token_file = config.get('kite_trade.token_file')
        self._trade_credentials_file = config.get('kite_trade.credentials_file')
        self._order_tag = config.get('kite_trade.order_tag', 'fifty')
        self._rate_limit_delay = config.get('kite_trade.rate_limit_delay', 1.0)

        # Instruments cache
        self._instruments_cache: Dict[str, str] = {}  # tradingsymbol -> token
        self._token_to_symbol_cache: Dict[str, str] = {}  # token -> tradingsymbol
        self._instruments_file = config.get('instruments.cache_file', 'data/instruments.csv')

        # Data cache
        self._data_cache: Dict[str, Any] = {}

        # Circuit breaker for API resilience
        self._circuit_breaker = get_kite_breaker()

        logger.info("DualKiteClient initialized")

    def _load_token_data(self, token_file: str) -> Dict[str, Any]:
        """Load access token from JSON file"""
        if not os.path.exists(token_file):
            raise FileNotFoundError(f"Token file not found: {token_file}")

        with open(token_file, 'r') as f:
            return json.load(f)

    def _validate_token_date(self, token_data: Dict[str, Any]) -> bool:
        """Validate that token was generated after 6 AM IST today (or 6 AM yesterday if before 6 AM now)"""
        generated_at = token_data.get('generated_at', '')
        if not generated_at:
            logger.warning("Token missing generated_at timestamp")
            return False  # Require timestamp for validation

        try:
            token_time = datetime.fromisoformat(generated_at.replace('Z', '+00:00'))
            if token_time.tzinfo is None:
                token_time = token_time.replace(tzinfo=IST)

            current_time = now_ist()
            today_6am = current_time.replace(hour=6, minute=0, second=0, microsecond=0)

            if current_time < today_6am:
                valid_from = today_6am - timedelta(days=1)
            else:
                valid_from = today_6am

            return token_time >= valid_from
        except Exception as e:
            logger.warning(f"Could not validate token date: {e}")
            return True

    def _get_read_client(self) -> KiteConnect:
        """Get authenticated KiteConnect instance for reading data"""
        if self._read_client is None:
            token_data = self._load_token_data(self._read_token_file)

            if not self._validate_token_date(token_data):
                raise ValueError("Read token expired. SNAIL morning startup must run.")

            api_key = token_data.get('api_key')
            access_token = token_data.get('access_token')

            if not api_key or not access_token:
                raise ValueError("Invalid token file format")

            self._read_client = KiteConnect(api_key=api_key)
            self._read_client.set_access_token(access_token)
            logger.info(f"Read client initialized for user: {token_data.get('user_id')}")

        return self._read_client

    def _get_trade_client(self) -> KiteConnect:
        """Get authenticated KiteConnect instance for trading"""
        if self._trade_client is None:
            token_data = self._load_token_data(self._trade_token_file)

            if not self._validate_token_date(token_data):
                raise ValueError("Trade token expired. Run generate_token.py")

            api_key = token_data.get('api_key')
            access_token = token_data.get('access_token')

            if not api_key or not access_token:
                raise ValueError("Invalid token file format")

            self._trade_client = KiteConnect(api_key=api_key)
            self._trade_client.set_access_token(access_token)
            logger.info(f"Trade client initialized for user: {token_data.get('user_id')}")

        return self._trade_client

    def _rate_limit(self):
        """Apply rate limiting before API calls"""
        time.sleep(self._rate_limit_delay)

    # =========================================================================
    # CONNECTION & VALIDATION
    # =========================================================================

    def validate_connection(self) -> bool:
        """Validate both read and trade connections"""
        try:
            # Validate read client
            read_client = self._get_read_client()
            read_profile = read_client.profile()
            logger.info(f"Read connection validated: {read_profile.get('user_id')}")

            # Validate trade client
            trade_client = self._get_trade_client()
            trade_profile = trade_client.profile()
            logger.info(f"Trade connection validated: {trade_profile.get('user_id')}")

            return True
        except Exception as e:
            logger.error(f"Connection validation failed: {e}")
            return False

    def validate_read_connection(self) -> bool:
        """Validate read connection only"""
        try:
            read_client = self._get_read_client()
            read_profile = read_client.profile()
            logger.info(f"Read connection validated: {read_profile.get('user_id')}")
            return True
        except Exception as e:
            logger.error(f"Read connection validation failed: {e}")
            return False

    def validate_trade_connection(self) -> bool:
        """Validate trade connection only"""
        try:
            trade_client = self._get_trade_client()
            trade_profile = trade_client.profile()
            logger.info(f"Trade connection validated: {trade_profile.get('user_id')}")
            return True
        except Exception as e:
            logger.error(f"Trade connection validation failed: {e}")
            return False

    # =========================================================================
    # HISTORICAL DATA (uses read client)
    # =========================================================================

    def get_historical_data(
        self,
        instrument_token: str,
        start_date: str,
        end_date: str,
        interval: str = 'day'
    ) -> pd.DataFrame:
        """Fetch historical OHLC data using read client (circuit breaker protected)"""
        cache_key = f"{instrument_token}_{start_date}_{end_date}_{interval}"

        # Check cache
        cache_expiry = config.get('supertrend.cache_expiry_minutes', 60)
        if cache_key in self._data_cache:
            cache_time, cached_data = self._data_cache[cache_key]
            if now_ist() - cache_time < timedelta(minutes=cache_expiry):
                return cached_data

        # FIX SYS-H1: Check circuit breaker before API call
        if not self._circuit_breaker.can_execute():
            logger.warning(f"Circuit breaker OPEN - returning empty dataframe for {instrument_token}")
            return pd.DataFrame()

        self._rate_limit()

        try:
            kite = self._get_read_client()

            data = kite.historical_data(
                instrument_token=int(instrument_token),
                from_date=start_date,
                to_date=end_date,
                interval=interval,
                oi=True
            )

            self._circuit_breaker.record_success()

            if not data:
                return pd.DataFrame()

            df = pd.DataFrame(data)
            df = df.rename(columns={
                'date': 'Date',
                'open': 'Open',
                'high': 'High',
                'low': 'Low',
                'close': 'Close',
                'volume': 'Volume'
            })
            df = df[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']]

            # Cache result
            self._data_cache[cache_key] = (now_ist(), df.copy())

            return df

        except Exception as e:
            self._circuit_breaker.record_failure()
            logger.error(f"Historical data fetch failed for {instrument_token}: {e}")
            raise

    def get_historical_data_sampled(
        self,
        instrument_token: str,
        timeframe: str,
        years_back: float
    ) -> pd.DataFrame:
        """Fetch daily data and resample to monthly/weekly"""
        end_date = now_ist().date()
        start_date = end_date - timedelta(days=int(years_back * 365))

        # Collect daily data year by year
        daily_data_list = []
        for year in range(start_date.year, end_date.year + 1):
            year_start = datetime(year, 1, 1).date()
            year_end = datetime(year, 12, 31).date()

            if year_start < start_date:
                year_start = start_date
            if year_end > end_date:
                year_end = end_date

            try:
                year_df = self.get_historical_data(
                    instrument_token=instrument_token,
                    start_date=year_start.strftime('%Y-%m-%d'),
                    end_date=year_end.strftime('%Y-%m-%d'),
                    interval='day'
                )
                if not year_df.empty:
                    daily_data_list.append(year_df)
            except Exception as e:
                logger.warning(f"Failed to fetch data for year {year}: {e}")

            time.sleep(0.1)

        if not daily_data_list:
            return pd.DataFrame()

        daily_df = pd.concat(daily_data_list, ignore_index=True)
        daily_df['Date'] = pd.to_datetime(daily_df['Date'])
        daily_df.set_index('Date', inplace=True)
        daily_df = daily_df[~daily_df.index.duplicated(keep='first')]
        daily_df.sort_index(inplace=True)

        if timeframe == 'daily':
            daily_df.reset_index(inplace=True)
            return daily_df

        # Resample (use 'M' for monthly - compatible with all pandas versions)
        resample_freq = 'M' if timeframe == 'monthly' else 'W'
        resampled_df = daily_df.resample(resample_freq).agg({
            'Open': 'first',
            'High': 'max',
            'Low': 'min',
            'Close': 'last',
            'Volume': 'sum'
        })
        resampled_df.dropna(inplace=True)
        resampled_df.reset_index(inplace=True)

        return resampled_df

    def get_instrument_ltp(self, instrument_token: str) -> float:
        """Get Last Traded Price using read client (circuit breaker protected)"""
        # FIX SYS-H1: Check circuit breaker before API call
        if not self._circuit_breaker.can_execute():
            raise CircuitBreakerOpenError(
                f"Circuit breaker OPEN - cannot fetch LTP for {instrument_token}"
            )

        self._rate_limit()

        try:
            kite = self._get_read_client()

            # Get tradingsymbol for LTP query
            tradingsymbol = self.get_tradingsymbol(instrument_token)
            if tradingsymbol:
                try:
                    quotes = kite.ltp([f"NSE:{tradingsymbol}"])
                    if quotes:
                        key = list(quotes.keys())[0]
                        ltp = quotes[key].get('last_price')
                        if ltp and ltp > 0:
                            self._circuit_breaker.record_success()
                            return float(ltp)
                except Exception as e:
                    self._circuit_breaker.record_failure()
                    logger.debug(f"LTP query failed for {tradingsymbol}: {e}")

            # Fallback: Get from historical data (already circuit breaker protected)
            today = now_ist().strftime('%Y-%m-%d')
            df = self.get_historical_data(instrument_token, today, today, 'minute')

            if df is None or df.empty:
                end_date = now_ist().strftime('%Y-%m-%d')
                start_date = (now_ist() - timedelta(days=7)).strftime('%Y-%m-%d')
                df = self.get_historical_data(instrument_token, start_date, end_date, 'day')

            if df is None or df.empty:
                raise Exception(f"No LTP data available for {instrument_token}")

            return float(df['Close'].iloc[-1])

        except CircuitBreakerOpenError:
            raise
        except Exception as e:
            logger.error(f"LTP fetch failed for {instrument_token}: {e}")
            raise

    # =========================================================================
    # ORDER OPERATIONS (uses trade client)
    # =========================================================================

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
        """Place order using trade client (circuit breaker protected)"""
        # Check circuit breaker before attempting
        if not self._circuit_breaker.can_execute():
            raise CircuitBreakerOpenError(
                f"Kite API circuit breaker is OPEN - order placement blocked for {tradingsymbol}"
            )

        self._rate_limit()

        try:
            kite = self._get_trade_client()

            order_params = {
                'exchange': exchange,
                'tradingsymbol': tradingsymbol,
                'transaction_type': transaction_type,
                'quantity': quantity,
                'order_type': order_type,
                'product': product,
                'validity': validity,
            }

            if order_type == "LIMIT" and price:
                order_params['price'] = price

            if tag:
                order_params['tag'] = tag
            elif self._order_tag:
                order_params['tag'] = self._order_tag

            order_id = kite.place_order(variety='regular', **order_params)
            logger.info(f"Order placed: {transaction_type} {quantity} {tradingsymbol} @ {price or 'MARKET'} - ID: {order_id}")

            self._circuit_breaker.record_success()
            return {'order_id': order_id}

        except CircuitBreakerOpenError:
            raise
        except Exception as e:
            self._circuit_breaker.record_failure()
            logger.error(f"Order placement failed: {e}")
            raise

    def cancel_order(self, order_id: str, variety: str = "regular", verify: bool = True) -> bool:
        """Cancel order using trade client"""
        self._rate_limit()
        kite = self._get_trade_client()

        kite.cancel_order(variety=variety, order_id=order_id)
        logger.info(f"Cancel request sent for order: {order_id}")

        if verify:
            time.sleep(1)
            try:
                order_status = self.get_order_status(order_id)
                actual_status = order_status.get('status', '')
                if actual_status == 'CANCELLED':
                    return True
                elif actual_status in ['COMPLETE', 'REJECTED']:
                    return False
            except Exception:
                pass

        return True

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """Get order status using trade client"""
        self._rate_limit()
        kite = self._get_trade_client()

        order_history = kite.order_history(order_id=order_id)
        if not order_history:
            raise Exception(f"No order history found for: {order_id}")

        return order_history[-1]

    def get_all_orders(self) -> List[Dict[str, Any]]:
        """Get all orders using trade client"""
        self._rate_limit()
        kite = self._get_trade_client()
        return kite.orders()

    # =========================================================================
    # GTT OPERATIONS (uses trade client)
    # =========================================================================

    def place_gtt_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Place GTT order using trade client (circuit breaker protected)"""
        # Check circuit breaker before attempting
        if not self._circuit_breaker.can_execute():
            raise CircuitBreakerOpenError(
                f"Kite API circuit breaker is OPEN - GTT placement blocked"
            )

        self._rate_limit()

        try:
            kite = self._get_trade_client()

            condition = payload['condition']
            orders = payload['orders']
            gtt_type = payload['type']

            response = kite.place_gtt(
                trigger_type=gtt_type,
                tradingsymbol=condition['tradingsymbol'],
                exchange=condition['exchange'],
                trigger_values=condition['trigger_values'],
                last_price=condition['last_price'],
                orders=[{
                    'transaction_type': orders[0]['transaction_type'],
                    'quantity': orders[0]['quantity'],
                    'order_type': 'LIMIT',
                    'product': orders[0]['product'],
                    'price': orders[0]['price'],
                }]
            )

            if isinstance(response, dict):
                trigger_id = response.get('trigger_id', response)
            else:
                trigger_id = response

            self._circuit_breaker.record_success()
            logger.info(f"GTT placed: {trigger_id}")
            return {'trigger_id': trigger_id}

        except CircuitBreakerOpenError:
            raise
        except Exception as e:
            self._circuit_breaker.record_failure()
            logger.error(f"GTT placement failed: {e}")
            raise

    def cancel_gtt_order(self, gtt_id: str) -> bool:
        """Cancel GTT order using trade client"""
        self._rate_limit()
        kite = self._get_trade_client()

        if isinstance(gtt_id, dict):
            gtt_id = gtt_id.get('trigger_id', gtt_id)

        kite.delete_gtt(trigger_id=int(gtt_id))
        logger.info(f"GTT cancelled: {gtt_id}")
        return True

    def get_gtt_orders(self) -> List[Dict[str, Any]]:
        """Get all active GTT orders using trade client"""
        self._rate_limit()
        kite = self._get_trade_client()
        return kite.get_gtts() or []

    def get_gtt_status(self, gtt_id: str) -> Optional[Dict[str, Any]]:
        """Get status of a specific GTT"""
        gtts = self.get_gtt_orders()
        for gtt in gtts:
            if str(gtt.get('id')) == str(gtt_id):
                return gtt
        return None

    # =========================================================================
    # PORTFOLIO & MARGIN (uses trade client)
    # =========================================================================

    def get_positions(self) -> Dict[str, List[Dict[str, Any]]]:
        """Get positions using trade client"""
        self._rate_limit()
        kite = self._get_trade_client()
        return kite.positions()

    def get_margins(self) -> Dict[str, Any]:
        """Get margins using trade client"""
        self._rate_limit()
        kite = self._get_trade_client()
        margins = kite.margins()
        return {'data': margins}

    # =========================================================================
    # INSTRUMENTS
    # =========================================================================

    def get_instrument_token(self, script: str) -> str:
        """Get instrument token for script symbol"""
        if not self._instruments_cache:
            self._load_instruments_cache()

        script_upper = script.upper()
        variations = self._generate_symbol_variations(script_upper)

        for variation in variations:
            if variation in self._instruments_cache:
                return self._instruments_cache[variation]

        # Refresh cache and try again
        self._fetch_instruments()
        for variation in variations:
            if variation in self._instruments_cache:
                return self._instruments_cache[variation]

        raise ValueError(f"Instrument token not found for: {script}")

    def get_tradingsymbol(self, instrument_token: str) -> Optional[str]:
        """Get trading symbol for instrument token"""
        if not self._token_to_symbol_cache:
            self._load_instruments_cache()
        return self._token_to_symbol_cache.get(str(instrument_token))

    def _generate_symbol_variations(self, script: str) -> List[str]:
        """Generate possible symbol variations"""
        variations = [script]
        if '&' in script:
            variations.append(script.replace('&', '-'))
            variations.append(script.replace('&', ''))
        if '-' in script:
            variations.append(script.replace('-', ''))
        return list(dict.fromkeys(variations))

    def _load_instruments_cache(self) -> bool:
        """Load instruments from local cache"""
        try:
            if not os.path.exists(self._instruments_file):
                return self._fetch_instruments()

            # Check file age
            file_mtime = os.path.getmtime(self._instruments_file)
            file_time = datetime.fromtimestamp(file_mtime, tz=IST)
            file_age = now_ist() - file_time
            max_age = config.get('instruments.max_age_hours', 24)

            if file_age.total_seconds() > max_age * 3600:
                return self._fetch_instruments()

            df = pd.read_csv(self._instruments_file)
            if df.empty:
                return self._fetch_instruments()

            self._instruments_cache = {
                str(row['tradingsymbol']): str(row['instrument_token'])
                for _, row in df.iterrows()
                if pd.notna(row['tradingsymbol']) and pd.notna(row['instrument_token'])
            }
            self._token_to_symbol_cache = {
                str(row['instrument_token']): str(row['tradingsymbol'])
                for _, row in df.iterrows()
                if pd.notna(row['tradingsymbol']) and pd.notna(row['instrument_token'])
            }

            logger.info(f"Loaded {len(self._instruments_cache)} instruments from cache")
            return True

        except Exception as e:
            logger.error(f"Error loading instruments cache: {e}")
            return self._fetch_instruments()

    def _fetch_instruments(self) -> bool:
        """Fetch instruments data from API"""
        try:
            self._rate_limit()
            kite = self._get_read_client()

            instruments = kite.instruments("NSE")

            eq_instruments = [
                {
                    'instrument_token': str(i['instrument_token']),
                    'tradingsymbol': i['tradingsymbol'],
                    'name': i.get('name', ''),
                    'exchange': i['exchange'],
                    'instrument_type': i.get('instrument_type', '')
                }
                for i in instruments
                if i.get('instrument_type') == 'EQ'
            ]

            if not eq_instruments:
                return False

            # Save to file
            os.makedirs(os.path.dirname(self._instruments_file), exist_ok=True)
            df = pd.DataFrame(eq_instruments)
            df.to_csv(self._instruments_file, index=False)

            # Build caches
            self._instruments_cache = {
                row['tradingsymbol']: row['instrument_token']
                for row in eq_instruments
            }
            self._token_to_symbol_cache = {
                row['instrument_token']: row['tradingsymbol']
                for row in eq_instruments
            }

            logger.info(f"Fetched {len(eq_instruments)} instruments")
            return True

        except Exception as e:
            logger.error(f"Failed to fetch instruments: {e}")
            return False

    # =========================================================================
    # PROPERTIES
    # =========================================================================

    @property
    def order_tag(self) -> str:
        """Get the order tag"""
        return self._order_tag


# Singleton instance
_kite_client: Optional[DualKiteClient] = None


def get_kite_client() -> DualKiteClient:
    """Get singleton DualKiteClient instance"""
    global _kite_client
    if _kite_client is None:
        _kite_client = DualKiteClient()
    return _kite_client
