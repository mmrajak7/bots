"""
Data loader for historical market data.
Fetches from Kite Connect API with proper pagination.
"""

import json
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Optional, Dict
import pandas as pd

try:
    from kiteconnect import KiteConnect
except ImportError:
    KiteConnect = None

from ..core.bar import Bar, DayData
from ..utils.time_utils import get_bar_number, MARKET_OPEN, MARKET_CLOSE


# Paths - resolve to absolute paths
SCRIPT_DIR = Path(__file__).resolve().parent
CONVICTBAR_DIR = SCRIPT_DIR.parent.parent
BOTS_DIR = CONVICTBAR_DIR.parent
DATA_DIR = BOTS_DIR / 'data'
TOKEN_FILE = DATA_DIR / 'kite_access_token.json'
CACHE_DIR = CONVICTBAR_DIR / 'data'


class DataLoader:
    """
    Loads historical OHLCV data from Kite Connect API.
    Handles pagination for long date ranges (60-day limit per request).
    """

    # Kite API limits
    MAX_DAYS_PER_REQUEST = 60
    MIN_REQUEST_INTERVAL = 0.35  # 3 requests per second

    def __init__(self, use_cache: bool = True):
        """
        Initialize data loader.

        Args:
            use_cache: Whether to use cached data if available
        """
        self.use_cache = use_cache
        self.kite = None
        self._last_request_time = 0

        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _init_kite(self) -> bool:
        """Initialize Kite Connect with saved token."""
        if KiteConnect is None:
            print("ERROR: kiteconnect not installed. Run: pip install kiteconnect")
            return False

        if not TOKEN_FILE.exists():
            print(f"ERROR: Token file not found: {TOKEN_FILE}")
            print("Run the Kite auth script first to generate token.")
            return False

        try:
            with open(TOKEN_FILE) as f:
                token_data = json.load(f)

            api_key = token_data['api_key']
            access_token = token_data['access_token']

            self.kite = KiteConnect(api_key=api_key)
            self.kite.set_access_token(access_token)

            # Validate token
            self.kite.profile()
            print(f"Kite connected: {token_data['user_id']}")
            return True

        except Exception as e:
            print(f"ERROR initializing Kite: {e}")
            return False

    def _rate_limit(self) -> None:
        """Ensure we don't exceed API rate limits."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.MIN_REQUEST_INTERVAL:
            time.sleep(self.MIN_REQUEST_INTERVAL - elapsed)
        self._last_request_time = time.time()

    def _get_cache_path(
        self,
        instrument_token: int,
        timeframe: str,
        start_date: date,
        end_date: date
    ) -> Path:
        """Get cache file path for given parameters."""
        filename = f"{instrument_token}_{timeframe}_{start_date}_{end_date}.csv"
        return CACHE_DIR / filename

    def _load_from_cache(
        self,
        instrument_token: int,
        timeframe: str,
        start_date: date,
        end_date: date
    ) -> Optional[pd.DataFrame]:
        """Load data from cache if available."""
        if not self.use_cache:
            return None

        cache_path = self._get_cache_path(instrument_token, timeframe, start_date, end_date)
        if cache_path.exists():
            try:
                df = pd.read_csv(cache_path, parse_dates=['datetime'])
                print(f"Loaded from cache: {cache_path.name}")
                return df
            except Exception as e:
                print(f"Cache read error: {e}")

        return None

    def _save_to_cache(
        self,
        df: pd.DataFrame,
        instrument_token: int,
        timeframe: str,
        start_date: date,
        end_date: date
    ) -> None:
        """Save data to cache."""
        cache_path = self._get_cache_path(instrument_token, timeframe, start_date, end_date)
        try:
            df.to_csv(cache_path, index=False)
            print(f"Saved to cache: {cache_path.name}")
        except Exception as e:
            print(f"Cache write error: {e}")

    def fetch_historical_data(
        self,
        instrument_token: int,
        start_date: date,
        end_date: date,
        timeframe: str = "5minute"
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV data from Kite.

        Handles pagination for date ranges > 60 days.

        Args:
            instrument_token: Kite instrument token
            start_date: Start date
            end_date: End date
            timeframe: Candle timeframe (5minute, 15minute, day, etc.)

        Returns:
            DataFrame with columns: datetime, open, high, low, close, volume
        """
        # Check cache first
        cached = self._load_from_cache(instrument_token, timeframe, start_date, end_date)
        if cached is not None:
            return cached

        # Initialize Kite if needed
        if self.kite is None:
            if not self._init_kite():
                raise RuntimeError("Could not initialize Kite connection")

        # Calculate date chunks (60 days max per request)
        all_data = []
        current_start = start_date

        while current_start <= end_date:
            current_end = min(
                current_start + timedelta(days=self.MAX_DAYS_PER_REQUEST - 1),
                end_date
            )

            print(f"Fetching {current_start} to {current_end}...")
            self._rate_limit()

            try:
                data = self.kite.historical_data(
                    instrument_token=instrument_token,
                    from_date=current_start,
                    to_date=current_end,
                    interval=timeframe,
                )
                all_data.extend(data)
                print(f"  Got {len(data)} candles")

            except Exception as e:
                print(f"ERROR fetching data: {e}")
                raise

            current_start = current_end + timedelta(days=1)

        # Convert to DataFrame
        if not all_data:
            print("WARNING: No data returned")
            return pd.DataFrame()

        df = pd.DataFrame(all_data)
        df = df.rename(columns={'date': 'datetime'})
        df['datetime'] = pd.to_datetime(df['datetime'])

        # Remove duplicates and sort
        df = df.drop_duplicates(subset=['datetime'])
        df = df.sort_values('datetime').reset_index(drop=True)

        print(f"Total candles: {len(df)}")

        # Save to cache
        self._save_to_cache(df, instrument_token, timeframe, start_date, end_date)

        return df

    def validate_data(self, df: pd.DataFrame) -> Dict[str, any]:
        """
        Validate data quality.

        Checks:
        - No missing bars during market hours
        - Valid OHLC relationships
        - No extreme price moves

        Args:
            df: DataFrame to validate

        Returns:
            Dict with validation results
        """
        issues = []

        # Check OHLC relationship
        invalid_ohlc = df[(df['low'] > df['open']) | (df['low'] > df['close']) |
                         (df['high'] < df['open']) | (df['high'] < df['close'])]
        if len(invalid_ohlc) > 0:
            issues.append(f"Invalid OHLC: {len(invalid_ohlc)} bars")

        # Check for negative/zero prices
        invalid_prices = df[(df['open'] <= 0) | (df['high'] <= 0) |
                           (df['low'] <= 0) | (df['close'] <= 0)]
        if len(invalid_prices) > 0:
            issues.append(f"Invalid prices: {len(invalid_prices)} bars")

        # Check for extreme ranges (> 200 points in 5 min)
        df_copy = df.copy()
        df_copy['range'] = df_copy['high'] - df_copy['low']
        extreme_ranges = df_copy[df_copy['range'] > 200]
        if len(extreme_ranges) > 0:
            issues.append(f"Extreme ranges (>200): {len(extreme_ranges)} bars")

        return {
            'is_valid': len(issues) == 0,
            'total_bars': len(df),
            'issues': issues,
            'date_range': (df['datetime'].min(), df['datetime'].max()) if len(df) > 0 else None,
        }

    def convert_to_bars(self, df: pd.DataFrame) -> List[Bar]:
        """
        Convert DataFrame to list of Bar objects.

        Filters to market hours and assigns bar numbers.

        Args:
            df: DataFrame with OHLCV data

        Returns:
            List of Bar objects
        """
        bars = []

        for _, row in df.iterrows():
            dt = row['datetime']

            # Filter to market hours
            if not (MARKET_OPEN <= dt.time() <= MARKET_CLOSE):
                continue

            bar_num = get_bar_number(dt)

            try:
                bar = Bar(
                    datetime=dt,
                    open=float(row['open']),
                    high=float(row['high']),
                    low=float(row['low']),
                    close=float(row['close']),
                    volume=int(row.get('volume', 0)),
                    bar_number=bar_num,
                )
                bars.append(bar)
            except ValueError as e:
                print(f"Skipping invalid bar at {dt}: {e}")

        return bars

    def group_by_day(self, bars: List[Bar]) -> List[DayData]:
        """
        Group bars by trading day.

        Args:
            bars: List of Bar objects

        Returns:
            List of DayData objects
        """
        # Group by date
        days_dict: Dict[date, List[Bar]] = {}

        for bar in bars:
            day = bar.datetime.date()
            if day not in days_dict:
                days_dict[day] = []
            days_dict[day].append(bar)

        # Sort bars within each day
        for day in days_dict:
            days_dict[day].sort(key=lambda b: b.datetime)

        # Create DayData objects with previous close
        sorted_dates = sorted(days_dict.keys())
        day_data_list = []
        prev_close = None

        for day in sorted_dates:
            day_bars = days_dict[day]

            day_data = DayData(
                date=datetime.combine(day, datetime.min.time()),
                bars=day_bars,
                previous_close=prev_close,
            )
            day_data_list.append(day_data)

            # Update prev_close for next day
            if day_bars:
                prev_close = day_bars[-1].close

        return day_data_list

    def load_data(
        self,
        instrument_token: int,
        start_date: date,
        end_date: date,
        timeframe: str = "5minute"
    ) -> List[DayData]:
        """
        Complete data loading pipeline.

        Fetches data, validates, converts to bars, and groups by day.

        Args:
            instrument_token: Kite instrument token
            start_date: Start date
            end_date: End date
            timeframe: Candle timeframe

        Returns:
            List of DayData objects ready for backtesting
        """
        # Fetch raw data
        df = self.fetch_historical_data(instrument_token, start_date, end_date, timeframe)

        if df.empty:
            return []

        # Validate
        validation = self.validate_data(df)
        print(f"Data validation: {validation}")

        if not validation['is_valid']:
            print(f"WARNING: Data has issues: {validation['issues']}")

        # Convert to bars
        bars = self.convert_to_bars(df)
        print(f"Converted {len(bars)} bars")

        # Group by day
        days = self.group_by_day(bars)
        print(f"Grouped into {len(days)} trading days")

        return days


def load_sample_data(csv_path: Path) -> List[DayData]:
    """
    Load data from a CSV file (for testing without Kite).

    Expected columns: datetime, open, high, low, close, volume

    Args:
        csv_path: Path to CSV file

    Returns:
        List of DayData objects
    """
    loader = DataLoader(use_cache=False)
    df = pd.read_csv(csv_path, parse_dates=['datetime'])
    bars = loader.convert_to_bars(df)
    return loader.group_by_day(bars)
