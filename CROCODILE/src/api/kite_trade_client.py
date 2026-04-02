"""
Kite Trade Client - Token-based API wrapper for historical data and market operations
Based on enc token authentication pattern from option_buy implementation
"""

import json
import os
import random
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import pandas as pd
import requests
import yaml
import pyotp
from loguru import logger
from dotenv import load_dotenv

from src.core.api_resilience import critical_api_call, standard_api_call, non_critical_api_call
from src.utils.timezone_helper import now_ist, today_ist

# Load environment variables from .env file (if it exists)
# Try loading from config/cred.env first, then fallback to project root .env
import os
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(project_root, 'config', 'cred.env'))  # User's credentials location
load_dotenv()  # Fallback to project root .env

# Handle kite_trade import gracefully for development/testing
try:
    import kite_trade as app
except ImportError:
    # Mock kite_trade for development/testing
    class MockKiteTrade:
        @staticmethod
        def get_enctoken(userid, password, twofa):
            return f"mock_token_{userid}_{twofa}"

    app = MockKiteTrade()


class KiteTradeClient:
    """Token-based Kite API client for SuperTrend calculation"""

    def __init__(self, config_path: str = "config/config.yaml"):
        """Initialize Kite client with configuration"""
        self.config = self._load_config(config_path)
        self.token_file = self.config['kite']['token_file']
        self.rate_limit_delay = self.config['kite']['rate_limit_delay']
        self.order_tag = self.config['kite'].get('order_tag', 'croc')  # Default tag for bot's orders
        self.base_url = "https://kite.zerodha.com"
        self._token = None
        self._user_id = self._get_user_id()  # Load user_id immediately
        self._cache = {}  # Simple in-memory cache
        self._instruments_cache = {}  # Instruments data cache
        self.instruments_file = self.config.get('instruments', {}).get('cache_file', 'data/instruments.csv')

    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """Load configuration from YAML file"""
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    def _get_user_id(self) -> str:
        """Get Zerodha user_id from environment or config"""
        userid = os.getenv('ZERODHA_USER_ID')
        if not userid:
            credentials = self.config.get('kite', {}).get('credentials', {})
            userid = credentials.get('userid')
        return userid

    def _get_token(self) -> str:
        """Read enc token from file, generate if missing or expired"""
        try:
            # Load user_id if not already loaded
            if not self._user_id:
                self._user_id = self._get_user_id()

            # Try to read existing token
            if os.path.exists(self.token_file):
                with open(self.token_file, 'r') as f:
                    token = f.read().strip()
                if token:
                    return token

            # Token doesn't exist or is empty, generate new one
            logger.info("Token file missing or empty, generating new token")
            return self._generate_token()

        except Exception as e:
            logger.error(f"Error reading token: {e}")
            # Try to generate new token as fallback
            return self._generate_token()

    def _generate_token(self) -> str:
        """
        Generate new enc token using credentials and TOTP

        Credentials are loaded in priority order:
        1. Environment variables (.env file) - RECOMMENDED
        2. config.yaml file - FALLBACK (less secure)
        """
        try:
            # Try to load from environment variables first (SECURE)
            userid = os.getenv('ZERODHA_USER_ID')
            password = os.getenv('ZERODHA_PASSWORD')
            totp_key = os.getenv('ZERODHA_TOTP_KEY')

            credential_source = "environment variables (.env)"

            # Fallback to config.yaml if env vars not set (LEGACY SUPPORT)
            if not all([userid, password, totp_key]):
                logger.warning(
                    "Credentials not found in environment variables, falling back to config.yaml. "
                    "For better security, move credentials to .env file (see .env.template)"
                )
                credentials = self.config.get('kite', {}).get('credentials', {})
                userid = credentials.get('userid')
                password = credentials.get('password')
                totp_key = credentials.get('totp_key')
                credential_source = "config.yaml (less secure)"

            if not all([userid, password, totp_key]):
                raise ValueError(
                    "Missing Zerodha credentials! Set them in either:\n"
                    "1. .env file (RECOMMENDED - see .env.template)\n"
                    "2. config.yaml under kite.credentials (less secure)"
                )

            # Generate TOTP
            totp = pyotp.TOTP(totp_key)
            pin = totp.now()
            twoFA = f"{int(pin):06d}" if len(pin) <= 5 else pin

            logger.info(f"Generating token for user: {userid} (loaded from {credential_source})")
            enctoken = app.get_enctoken(userid=userid, password=password, twofa=twoFA)

            if not enctoken:
                raise Exception("Failed to generate token - check credentials")

            # Store user_id for order placement
            self._user_id = userid

            # Save token to file
            os.makedirs(os.path.dirname(self.token_file), exist_ok=True)
            with open(self.token_file, 'w') as f:
                f.write(enctoken)

            logger.info("Token generated and saved successfully")
            return enctoken

        except Exception as e:
            logger.error(f"Token generation failed: {e}")
            raise Exception(f"Unable to generate token: {e}")

    def _get_headers(self) -> Dict[str, str]:
        """Get authorization headers for API requests"""
        if not self._token:
            self._token = self._get_token()

        return {
            'authority': 'kite.zerodha.com',
            'cache-control': 'private, max-age=0, no-cache',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.163 Safari/537.36',
            'accept': '*/*',
            'authorization': f'enctoken {self._token}',
            'accept-encoding': 'gzip, deflate',
            'accept-language': 'en-US,en;q=0.9,tr-TR;q=0.8,tr;q=0.7',
        }

    def _make_api_request(self, url: str) -> Dict[str, Any]:
        """Make API request with rate limiting and error handling"""
        # Rate limiting as per AC requirement
        time.sleep(self.rate_limit_delay)

        headers = self._get_headers()
        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            if response.status_code == 403:
                logger.warning("Token expired or invalid, regenerating token...")
                # Clear current token and regenerate
                self._token = None
                self._token = self._generate_token()
                # Retry request with new token
                headers = self._get_headers()
                response = requests.get(url, headers=headers, timeout=30)
                response.raise_for_status()
                return response.json()
            raise Exception(f"HTTP error {response.status_code}: {e}")
        except requests.exceptions.RequestException as e:
            raise Exception(f"Request failed: {e}")
        except json.JSONDecodeError as e:
            raise Exception(f"Invalid JSON response: {e}")

    def validate_connection(self) -> bool:
        """Validate API connection and token"""
        try:
            # Test with a simple request - get user profile
            test_url = f"{self.base_url}/oms/user/profile/full"
            response = self._make_api_request(test_url)
            logger.info("Connection validation successful")
            return True
        except Exception as e:
            logger.error(f"Connection validation failed: {e}")
            return False

    @standard_api_call("Get Historical Data")
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
        cache_key = f"{instrument_token}_{start_date}_{end_date}_{interval}"

        # Check cache first
        if cache_key in self._cache:
            cache_time, cached_data = self._cache[cache_key]
            if now_ist() - cache_time < timedelta(minutes=self.config['supertrend']['cache_expiry_minutes']):
                logger.debug(f"Using cached data for {instrument_token}")
                return cached_data

        random_id = random.randint(1000000, 9999999)
        url = f"{self.base_url}/oms/instruments/historical/{instrument_token}/{interval}?oi=1&from={start_date}&to={end_date}&ciqrandom={random_id}"

        data = self._make_api_request(url)

        if 'data' not in data or 'candles' not in data['data']:
            raise Exception("Invalid response format from API")

        candles = data['data']['candles']
        if not candles:
            logger.warning(f"No data found for {instrument_token} from {start_date} to {end_date}")
            return pd.DataFrame()

        # Convert to DataFrame (API returns 7 columns with OI)
        df = pd.DataFrame(candles)

        # Set proper column names (handle both 6 and 7 column responses)
        if len(df.columns) == 7:
            df.columns = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume', 'OI']
            df = df[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']]  # Drop OI
        elif len(df.columns) == 6:
            df.columns = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume']
        else:
            raise Exception(f"Unexpected number of columns: {len(df.columns)}")

        # Cache the result
        self._cache[cache_key] = (now_ist(), df.copy())

        logger.info(f"Fetched {len(df)} candles for instrument {instrument_token}")
        return df

    @standard_api_call("Get Instrument LTP")
    def get_instrument_ltp(self, instrument_token: str) -> float:
        """
        Get Last Traded Price for an instrument

        Returns validated LTP with sanity checks.
        Falls back to last available close price if market is not open.
        """
        # Try current day's last candle as LTP
        today = now_ist().strftime('%Y-%m-%d')
        df = self.get_historical_data(instrument_token, today, today, 'minute')

        if df is None or df.empty:
            # Market not open yet - fallback to last available close from past week
            logger.debug(f"No intraday data for {instrument_token}, fetching last close from historical data")
            end_date = now_ist().strftime('%Y-%m-%d')
            start_date = (now_ist() - timedelta(days=7)).strftime('%Y-%m-%d')
            df = self.get_historical_data(instrument_token, start_date, end_date, 'day')

            if df is None or df.empty:
                raise Exception(f"No LTP data available for instrument {instrument_token}")

        ltp = float(df['Close'].iloc[-1])

        # Validate LTP is reasonable
        if ltp <= 0:
            raise Exception(
                f"Invalid LTP (≤0) for instrument {instrument_token}: {ltp}"
            )

        if ltp < 0.05:
            raise Exception(
                f"LTP below minimum tick size for instrument {instrument_token}: {ltp}"
            )

        logger.debug(f"LTP for {instrument_token}: Rs.{ltp:.2f}")
        return ltp

    def regenerate_token(self) -> bool:
        """
        Regenerate token - placeholder for actual implementation
        This would typically involve re-authentication flow
        """
        logger.warning("Token regeneration not implemented - manual intervention required")
        return False

    def get_historical_data_sampled(
        self,
        instrument_token: str,
        timeframe: str,
        years_back: float
    ) -> pd.DataFrame:
        """
        Fetch daily data and resample to monthly/weekly timeframes
        Based on extract_data.py implementation

        Args:
            instrument_token: Zerodha instrument token
            timeframe: 'monthly' or 'weekly'
            years_back: Number of years to look back

        Returns:
            DataFrame with resampled OHLC data
        """
        try:
            # Calculate date range
            end_date = now_ist().date()
            if timeframe == 'monthly':
                start_date = end_date - timedelta(days=int(years_back * 365))
            elif timeframe == 'weekly':
                start_date = end_date - timedelta(days=int(years_back * 365))
            elif timeframe == 'daily':
                start_date = end_date - timedelta(days=int(years_back * 365))
            else:
                raise ValueError(f"Invalid timeframe: {timeframe}. Use 'monthly', 'weekly', or 'daily'")

            logger.info(f"Fetching {timeframe} data for {instrument_token} from {start_date} to {end_date}")

            # Collect daily data year by year to handle large date ranges
            daily_data_list = []
            for year in range(start_date.year, end_date.year + 1):
                year_start = datetime(year, 1, 1).date()
                year_end = datetime(year, 12, 31).date()

                # Ensure we don't go beyond our target date range
                if year_start < start_date:
                    year_start = start_date
                if year_end > end_date:
                    year_end = end_date

                try:
                    # Fetch daily data for this year
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
                    continue

                # Add small delay between year requests
                time.sleep(0.1)

            if not daily_data_list:
                logger.warning(f"No data available for {instrument_token}")
                return pd.DataFrame()

            # Combine all years
            daily_df = pd.concat(daily_data_list, ignore_index=True)

            # Convert Date column to datetime and set as index
            daily_df['Date'] = pd.to_datetime(daily_df['Date'])
            daily_df.set_index('Date', inplace=True)

            # Remove duplicates and sort
            daily_df = daily_df[~daily_df.index.duplicated(keep='first')]
            daily_df.sort_index(inplace=True)

            # For daily timeframe, return data as-is without resampling
            if timeframe == 'daily':
                daily_df.reset_index(inplace=True)
                logger.info(f"Fetched {len(daily_df)} daily candles")
                return daily_df

            # Resample to desired timeframe (monthly or weekly)
            resample_freq = 'M' if timeframe == 'monthly' else 'W'
            resampled_df = daily_df.resample(resample_freq).agg({
                'Open': 'first',
                'High': 'max',
                'Low': 'min',
                'Close': 'last',
                'Volume': 'sum'
            })

            # Remove rows with NaN values
            resampled_df.dropna(inplace=True)

            # Reset index to get Date as column
            resampled_df.reset_index(inplace=True)

            logger.info(f"Resampled {len(daily_df)} daily candles to {len(resampled_df)} {timeframe} candles")
            return resampled_df

        except Exception as e:
            logger.error(f"Failed to fetch sampled historical data: {e}")
            raise

    def fetch_instruments_data(self) -> bool:
        """
        Fetch instruments data from Kite API and cache locally

        Returns:
            True if successful, False otherwise
        """
        try:
            logger.info("Fetching instruments data from Kite API")

            # Make API request to instruments endpoint
            instruments_url = "https://api.kite.trade/instruments"
            headers = self._get_headers()

            # Apply rate limiting
            time.sleep(self.rate_limit_delay)

            response = requests.get(instruments_url, headers=headers, timeout=60)
            response.raise_for_status()

            # Process response using shared method (DRY principle)
            return self._process_instruments_response(response.text)

        except requests.exceptions.HTTPError as e:
            if hasattr(e, 'response') and e.response.status_code == 403:
                logger.warning("Token expired during instruments fetch, regenerating token...")
                # Clear current token and regenerate
                self._token = None
                self._token = self._generate_token()
                # Retry once with new token
                try:
                    headers = self._get_headers()
                    time.sleep(self.rate_limit_delay)
                    response = requests.get(instruments_url, headers=headers, timeout=60)
                    response.raise_for_status()
                    # Process response using shared method
                    return self._process_instruments_response(response.text)
                except Exception as retry_e:
                    logger.error(f"Retry failed for instruments fetch: {retry_e}")
                    return False
            else:
                logger.error(f"HTTP error fetching instruments: {e}")
                return False
        except Exception as e:
            logger.error(f"Failed to fetch instruments data: {e}")
            return False

    def _process_instruments_response(self, response_text: str) -> bool:
        """Helper method to process instruments API response"""
        try:
            instruments_data = []
            lines = response_text.strip().split('\n')

            if not lines:
                return False

            headers_line = lines[0].split(',')

            for line in lines[1:]:
                fields = line.split(',')
                if len(fields) >= len(headers_line):
                    row_data = dict(zip(headers_line, fields))

                    if (row_data.get('exchange') == 'NSE' and
                        row_data.get('instrument_type') == 'EQ'):
                        instruments_data.append({
                            'instrument_token': row_data.get('instrument_token', ''),
                            'tradingsymbol': row_data.get('tradingsymbol', ''),
                            'name': row_data.get('name', ''),
                            'exchange': row_data.get('exchange', ''),
                            'instrument_type': row_data.get('instrument_type', '')
                        })

            if not instruments_data:
                return False

            # Save and cache
            os.makedirs(os.path.dirname(self.instruments_file), exist_ok=True)
            df = pd.DataFrame(instruments_data)
            df.to_csv(self.instruments_file, index=False)

            self._instruments_cache = {
                row['tradingsymbol']: row['instrument_token']
                for row in instruments_data
            }

            logger.info(f"Processed {len(instruments_data)} instruments in retry")
            return True

        except Exception as e:
            logger.error(f"Error processing instruments response: {e}")
            return False

    def load_instruments_cache(self) -> bool:
        """
        Load instruments data from local file into memory cache

        Includes comprehensive validation:
        - File age check with timezone-aware comparison
        - CSV structure validation (required columns)
        - Row count sanity check (NSE has 2000+ instruments)
        - Auto-recovery on corruption (re-fetch from API)
        - Telegram alerts for critical failures

        Returns:
            True if successful, False otherwise
        """
        try:
            # === FILE EXISTENCE CHECK ===
            if not os.path.exists(self.instruments_file):
                logger.warning(f"Instruments file not found: {self.instruments_file}")
                logger.info("Attempting to fetch fresh instruments data from API")
                return self.fetch_instruments_data()

            # === FILE AGE CHECK (Timezone-aware) ===
            file_mtime = os.path.getmtime(self.instruments_file)
            # Convert file mtime to IST-aware datetime for accurate comparison
            from src.utils.timezone_helper import IST
            file_time_ist = datetime.fromtimestamp(file_mtime, tz=IST)
            current_time_ist = now_ist()
            file_age = current_time_ist - file_time_ist
            max_age_hours = self.config.get('instruments', {}).get('max_age_hours', 24)

            if file_age.total_seconds() > max_age_hours * 3600:
                logger.warning(
                    f"Instruments file is {file_age.total_seconds()/3600:.1f} hours old "
                    f"(max: {max_age_hours}h), fetching fresh data"
                )
                return self.fetch_instruments_data()

            # === CSV LOAD & VALIDATION ===
            logger.debug(f"Loading instruments from: {self.instruments_file}")
            df = pd.read_csv(self.instruments_file)

            # Validation 1: Empty file
            if df.empty:
                logger.error("Instruments file is empty - attempting re-fetch")
                self._send_instruments_alert(
                    "⚠️ **Instruments Cache Corrupted**\n"
                    "Cache file is empty\n"
                    "Auto-recovery: Re-fetching from API"
                )
                return self.fetch_instruments_data()

            # Validation 2: Required columns exist
            required_columns = {'tradingsymbol', 'instrument_token'}
            missing_columns = required_columns - set(df.columns)

            if missing_columns:
                logger.error(f"Instruments file missing required columns: {missing_columns}")
                self._send_instruments_alert(
                    f"🚨 **Instruments Cache Corrupted**\n"
                    f"Missing columns: {', '.join(missing_columns)}\n"
                    f"Auto-recovery: Re-fetching from API"
                )
                return self.fetch_instruments_data()

            # Validation 3: Row count sanity check
            # NSE has 2000+ equity instruments
            # If we have < 500, the cache is likely corrupted
            MIN_EXPECTED_INSTRUMENTS = 500
            if len(df) < MIN_EXPECTED_INSTRUMENTS:
                logger.error(
                    f"Instruments file has only {len(df)} rows "
                    f"(expected at least {MIN_EXPECTED_INSTRUMENTS}) - likely corrupted"
                )
                self._send_instruments_alert(
                    f"🚨 **Instruments Cache Corrupted**\n"
                    f"Only {len(df)} instruments found\n"
                    f"Expected: >{MIN_EXPECTED_INSTRUMENTS}\n"
                    f"Auto-recovery: Re-fetching from API"
                )
                return self.fetch_instruments_data()

            # === BUILD IN-MEMORY CACHE ===
            # Filter out rows with missing critical data
            valid_rows = df[
                df['tradingsymbol'].notna() &
                df['instrument_token'].notna()
            ]

            self._instruments_cache = {
                str(row['tradingsymbol']): str(row['instrument_token'])
                for _, row in valid_rows.iterrows()
            }

            # Validation 4: Check cache build success
            if len(self._instruments_cache) == 0:
                logger.error("Failed to build instruments cache - no valid rows")
                self._send_instruments_alert(
                    "🚨 **Instruments Cache Build Failed**\n"
                    "No valid instrument rows found\n"
                    "Auto-recovery: Re-fetching from API"
                )
                return self.fetch_instruments_data()

            # Success
            invalid_rows = len(df) - len(valid_rows)
            logger.info(
                f"✅ Loaded {len(self._instruments_cache)} instruments from cache "
                f"(age: {file_age.total_seconds()/3600:.1f}h, invalid rows: {invalid_rows})"
            )

            # Alert if too many invalid rows (>10%)
            if len(df) > 0 and (invalid_rows / len(df)) > 0.1:
                logger.warning(f"High invalid row rate in instruments cache: {invalid_rows}/{len(df)}")

            return True

        except pd.errors.ParserError as e:
            # CSV parsing error - file is corrupted
            logger.error(f"CSV parsing error - instruments file corrupted: {e}")
            self._send_instruments_alert(
                f"🚨 **Instruments Cache Corrupted**\n"
                f"CSV parsing failed: {str(e)[:100]}\n"
                f"Auto-recovery: Re-fetching from API"
            )
            return self.fetch_instruments_data()

        except KeyError as e:
            # Missing column error (shouldn't happen due to validation, but defensive)
            logger.error(f"Column access error in instruments file: {e}")
            self._send_instruments_alert(
                f"🚨 **Instruments Cache Error**\n"
                f"Column error: {e}\n"
                f"Auto-recovery: Re-fetching from API"
            )
            return self.fetch_instruments_data()

        except Exception as e:
            # Unexpected error - try re-fetch
            logger.error(f"Unexpected error loading instruments cache: {e}")
            self._send_instruments_alert(
                f"🚨 **Instruments Cache Load Failed**\n"
                f"Error: {str(e)[:100]}\n"
                f"Auto-recovery: Re-fetching from API"
            )
            return self.fetch_instruments_data()

    def _send_instruments_alert(self, message: str):
        """Send Telegram alert for instruments cache issues (non-blocking)"""
        try:
            from src.reporting.telegram_client import telegram
            telegram.send_alert(message, critical=True)
        except Exception as e:
            # Don't fail on alert sending
            logger.error(f"Failed to send instruments cache alert: {e}")

    def _generate_symbol_variations(self, script: str) -> List[str]:
        """
        Generate possible NSE symbol variations for lookup

        Handles common symbol format differences:
        - M&M → tries: M&M, M-M, MM
        - BAJAJ-AUTO → tries: BAJAJ-AUTO, BAJAJAUTO
        - ARE&M → tries: ARE&M, ARE-M, AREM

        Args:
            script: Original script symbol

        Returns:
            List of possible symbol variations to try
        """
        script_upper = script.upper()
        variations = [script_upper]  # Start with original

        # Try replacing & with - (hyphen)
        if '&' in script_upper:
            variations.append(script_upper.replace('&', '-'))

        # Try replacing & with nothing
        if '&' in script_upper:
            variations.append(script_upper.replace('&', ''))

        # Try replacing - with nothing
        if '-' in script_upper:
            variations.append(script_upper.replace('-', ''))

        # Remove all duplicates while preserving order
        seen = set()
        unique_variations = []
        for var in variations:
            if var not in seen:
                seen.add(var)
                unique_variations.append(var)

        return unique_variations

    def get_instrument_token(self, script: str) -> str:
        """
        Get instrument token for a given script symbol

        Automatically handles symbol format variations:
        - M&M, M-M, MM (tries all variations)
        - BAJAJ-AUTO, BAJAJAUTO (tries all variations)

        Args:
            script: Trading symbol (e.g., "VOLTAS", "M&M", "BAJAJ-AUTO")

        Returns:
            Instrument token as string

        Raises:
            ValueError: If instrument token not found for any variation
        """
        # Ensure cache is loaded
        if not self._instruments_cache:
            if not self.load_instruments_cache():
                # If loading from file fails, try fetching fresh data
                if not self.fetch_instruments_data():
                    raise ValueError("Unable to load instruments data")

        # Generate symbol variations to try
        variations = self._generate_symbol_variations(script)

        # Try each variation
        token = None
        matched_symbol = None

        for variation in variations:
            token = self._instruments_cache.get(variation)
            if token:
                matched_symbol = variation
                break

        if not token:
            # Try refreshing cache once
            logger.warning(f"Instrument token not found for {script} (tried: {variations}), refreshing instruments data")
            if self.fetch_instruments_data():
                # Try all variations again with fresh cache
                for variation in variations:
                    token = self._instruments_cache.get(variation)
                    if token:
                        matched_symbol = variation
                        break

            if not token:
                available_scripts = list(self._instruments_cache.keys())[:10]  # Show first 10 for debugging
                raise ValueError(f"Instrument token not found for script: {script}. "
                               f"Tried variations: {variations}. "
                               f"Available scripts (sample): {available_scripts}")

        if matched_symbol != script.upper():
            logger.info(f"Symbol mapping: {script} → {matched_symbol} (token: {token})")
        else:
            logger.debug(f"Found instrument token for {script}: {token}")

        return token

    def get_instruments_stats(self) -> Dict[str, Any]:
        """
        Get statistics about cached instruments data

        Returns:
            Dictionary with cache statistics
        """
        stats = {
            "cache_size": len(self._instruments_cache),
            "file_exists": os.path.exists(self.instruments_file),
            "file_age_hours": 0
        }

        if stats["file_exists"]:
            file_age = now_ist() - datetime.fromtimestamp(os.path.getmtime(self.instruments_file))
            stats["file_age_hours"] = file_age.total_seconds() / 3600

        return stats

    @standard_api_call("Get Margins")
    def get_margins(self) -> Dict[str, Any]:
        """
        Fetch margin data from Zerodha API

        Returns:
            Dict with margin details from API response
        """
        url = f"{self.base_url}/oms/user/margins"
        response = self._make_api_request(url)

        logger.debug(f"Fetched margins: {response}")
        return response

    @standard_api_call("Place Order")
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
            tag: Order tag for identification (default: bot's configured tag)

        Returns:
            Dict with order response (contains order_id)
        """
        # Ensure user_id is loaded
        if not self._user_id:
            self._user_id = self._get_user_id()

        # Build order payload matching Zerodha's exact format
        payload = {
            "variety": "regular",
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "order_type": order_type,
            "quantity": quantity,
            "product": product,
            "validity": validity,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "squareoff": 0,
            "stoploss": 0,
            "trailing_stoploss": 0,
            "user_id": self._user_id
        }

        # Add price for LIMIT orders
        if order_type == "LIMIT" and price:
            payload["price"] = price

        # Apr 2026: market_protection mandatory for MARKET/SL-M orders
        if order_type in ("MARKET", "SL-M"):
            payload["market_protection"] = -1

        # Make POST request to correct endpoint
        url = f"{self.base_url}/oms/orders/regular"
        headers = self._get_headers()
        headers['Content-Type'] = 'application/x-www-form-urlencoded'

        # Debug logging
        logger.debug(f"Order URL: {url}")
        logger.debug(f"Order payload: {payload}")

        # Apply rate limiting before API call
        time.sleep(self.rate_limit_delay)

        response = requests.post(url, data=payload, headers=headers, timeout=30)

        # Log response for debugging
        logger.debug(f"Response status: {response.status_code}")
        logger.debug(f"Response text: {response.text[:500]}")

        # Handle error responses from Zerodha
        if response.status_code != 200:
            try:
                error_data = response.json()
                error_msg = error_data.get('message', 'Unknown error')

                # Create detailed error message
                detailed_error = (
                    f"Zerodha Error: {error_msg}\n"
                    f"Symbol: {tradingsymbol}\n"
                    f"Order Type: {order_type}\n"
                    f"Price: {price if price else 'MARKET'}"
                )

                logger.error(detailed_error)

                # Raise exception with clear message
                raise Exception(detailed_error)
            except (ValueError, KeyError):
                # If response is not JSON or missing expected fields
                logger.error(f"Zerodha API returned status {response.status_code}: {response.text[:200]}")
                response.raise_for_status()

        result = response.json()

        logger.info(
            f"Order placed: {transaction_type} {quantity} {tradingsymbol} @ "
            f"{price if price else 'MARKET'} - Order ID: {result.get('data', {}).get('order_id')}"
        )

        return result.get('data', {})

    @critical_api_call("Place GTT Order")
    def place_gtt_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Place GTT (Good Till Triggered) order

        Args:
            payload: GTT order payload with condition, orders, type, expires_at

        Returns:
            Dict with GTT response (contains trigger_id)
        """
        import json

        url = f"{self.base_url}/oms/gtt/triggers"
        headers = self._get_headers()
        # Don't set Content-Type - let requests handle form data encoding

        # Convert to form data format (Zerodha expects form data, not JSON)
        form_data = {
            'condition': json.dumps(payload['condition']),
            'orders': json.dumps(payload['orders']),
            'type': payload['type'],
            'expires_at': payload['expires_at']
        }

        # Apply rate limiting before API call
        time.sleep(self.rate_limit_delay)

        response = requests.post(url, data=form_data, headers=headers, timeout=30)

        # Log response for debugging
        if response.status_code != 200:
            logger.error(f"GTT API Error Response: {response.text}")

        response.raise_for_status()

        result = response.json()

        logger.info(f"GTT placed: {result.get('data', {}).get('trigger_id')}")
        return result.get('data', {})

    @critical_api_call("Cancel GTT Order")
    def cancel_gtt_order(self, gtt_id: str) -> bool:
        """
        Cancel GTT order

        Args:
            gtt_id: GTT trigger ID

        Returns:
            Success status
        """
        # Handle case where gtt_id might be a dict {'trigger_id': <int>}
        if isinstance(gtt_id, dict):
            gtt_id = gtt_id.get('trigger_id', gtt_id)

        url = f"{self.base_url}/oms/gtt/triggers/{gtt_id}"
        headers = self._get_headers()

        # Apply rate limiting before API call
        time.sleep(self.rate_limit_delay)

        response = requests.delete(url, headers=headers, timeout=30)
        response.raise_for_status()

        logger.info(f"GTT cancelled: {gtt_id}")
        return True

    @critical_api_call("Cancel Regular Order")
    def cancel_order(self, order_id: str, variety: str = "regular", verify: bool = True) -> bool:
        """
        Cancel a regular order on Zerodha

        Args:
            order_id: Order ID to cancel
            variety: Order variety - "regular", "amo", "co", "iceberg", "auction"
            verify: If True, verify order status after cancellation (recommended)

        Returns:
            True if cancelled successfully and verified

        Raises:
            Exception: If cancellation fails or verification fails
        """
        url = f"{self.base_url}/oms/orders/{variety}/{order_id}"
        headers = self._get_headers()

        # Apply rate limiting before API call
        time.sleep(self.rate_limit_delay)

        response = requests.delete(url, headers=headers, timeout=30)

        if response.status_code != 200:
            error_msg = response.json().get('message', 'Unknown error')
            logger.error(f"Failed to cancel order {order_id}: {error_msg}")
            raise Exception(f"Order cancellation failed: {error_msg}")

        response.raise_for_status()

        logger.info(f"Cancel request sent for order: {order_id} (variety: {variety})")

        # Verify cancellation if requested (handles edge case where API returns 200 but exchange rejects)
        if verify:
            time.sleep(1)  # Brief delay for status to propagate
            try:
                order_status = self.get_order_status(order_id)
                actual_status = order_status.get('status', '')
                status_message = order_status.get('status_message', '')

                if actual_status == 'CANCELLED':
                    logger.info(f"Order cancellation verified: {order_id} is CANCELLED")
                    return True
                elif actual_status in ['COMPLETE', 'REJECTED']:
                    # Order was already filled or rejected - not an error, just can't cancel
                    logger.warning(f"Order {order_id} cannot be cancelled - status is {actual_status}")
                    return False
                else:
                    # Order still OPEN or other status - cancellation may have failed at exchange level
                    logger.error(
                        f"Order cancellation verification FAILED: {order_id} still has status '{actual_status}'. "
                        f"Message: {status_message}"
                    )
                    raise Exception(
                        f"Order cancellation failed at exchange level. "
                        f"Order {order_id} still {actual_status}. {status_message}"
                    )
            except Exception as e:
                if "cancellation failed at exchange" in str(e):
                    raise  # Re-raise our verification failure
                # If we can't verify (e.g., API error), log warning but don't fail
                logger.warning(f"Could not verify order cancellation for {order_id}: {e}")
                return True  # Assume success since DELETE returned 200

        return True

    @standard_api_call("Get GTT Orders")
    def get_gtt_orders(self) -> List[Dict[str, Any]]:
        """
        Get all active GTT orders

        Returns:
            List of GTT orders
        """
        url = f"{self.base_url}/oms/gtt/triggers"
        response = self._make_api_request(url)

        return response.get('data', [])

    @standard_api_call("Get Order Status")
    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """
        Get status of a specific order

        Note: Kite API returns order history as an array. Each modification/fill
        creates a new entry. We return the latest (last) entry.

        Args:
            order_id: Order ID

        Returns:
            Latest order status (last entry in order history)

        Raises:
            Exception: If no order history found
        """
        url = f"{self.base_url}/oms/orders/{order_id}"
        response = self._make_api_request(url)

        # API returns array of order history entries
        order_history = response.get('data', [])

        if not order_history:
            raise Exception(f"No order history found for order_id: {order_id}")

        # Return latest status (last entry in history)
        latest_order = order_history[-1]

        logger.debug(
            f"Order {order_id} status: {latest_order.get('status')}, "
            f"filled: {latest_order.get('filled_quantity', 0)}/{latest_order.get('quantity', 0)}"
        )

        return latest_order

    @standard_api_call("Get Positions")
    def get_positions(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Get current positions from Zerodha

        Returns:
            Dict with 'net' and 'day' positions
        """
        url = f"{self.base_url}/oms/portfolio/positions"
        response = self._make_api_request(url)

        return response.get('data', {})

    def get_all_orders(self) -> List[Dict[str, Any]]:
        """
        Get all orders (today's orders, no filtering)

        Returns:
            List of all orders placed today
        """
        try:
            url = f"{self.base_url}/oms/orders"
            response = self._make_api_request(url)

            all_orders = response.get('data', [])

            logger.info(f"Fetched {len(all_orders)} orders from Zerodha")

            return all_orders

        except Exception as e:
            logger.error(f"Failed to fetch all orders: {e}")
            raise

    def get_bot_orders(self, tag: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all orders placed by this bot (filtered by tag)

        Args:
            tag: Tag to filter by (default: bot's configured tag)

        Returns:
            List of orders with matching tag
        """
        try:
            # Use bot's default tag if not provided
            if tag is None:
                tag = self.order_tag

            # Fetch all orders
            all_orders = self.get_all_orders()

            # Filter by tag
            bot_orders = [order for order in all_orders if order.get('tag') == tag]

            logger.info(
                f"Found {len(bot_orders)} bot orders (tag='{tag}') out of {len(all_orders)} total orders"
            )

            return bot_orders

        except Exception as e:
            logger.error(f"Failed to fetch bot orders: {e}")
            raise

    def clear_cache(self) -> None:
        """Clear the data cache"""
        self._cache.clear()
        logger.info("Cache cleared")

    def clear_instruments_cache(self) -> None:
        """Clear the instruments cache"""
        self._instruments_cache.clear()
        logger.info("Instruments cache cleared")