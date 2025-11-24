"""
NIFTY Data Fetcher - Historical data retrieval for NIFTY trend analysis
Fetches 3 years monthly and 1.5 years weekly OHLC data for NIFTY trend filter
"""

import os
from datetime import datetime, timedelta
from typing import Dict, Optional

import pandas as pd
from loguru import logger

from src.api.kite_trade_client import KiteTradeClient
from src.utils.timezone_helper import now_ist, today_ist


class NiftyDataFetcher:
    """NIFTY historical data fetcher for trend analysis"""

    # NIFTY instrument token from architecture specification
    NIFTY_INSTRUMENT_TOKEN = "256265"

    def __init__(self, config_path: str = "config/config.yaml"):
        """Initialize NIFTY data fetcher"""
        self.kite_client = KiteTradeClient(config_path)
        self._cache = {}  # In-memory cache for NIFTY data

        # Historical data requirements from architecture
        self.monthly_years_back = 3.0  # 3 years for monthly data
        self.weekly_years_back = 1.5   # 1.5 years for weekly data

        logger.info("NIFTY data fetcher initialized")

    def fetch_monthly_data(self, use_cache: bool = True) -> pd.DataFrame:
        """
        Fetch 3 years of monthly OHLC data for NIFTY

        Args:
            use_cache: Whether to use cached data if available

        Returns:
            DataFrame with monthly OHLC data
        """
        cache_key = "nifty_monthly_3y"

        # Check cache first
        if use_cache and cache_key in self._cache:
            cache_time, cached_data = self._cache[cache_key]
            cache_age = now_ist() - cache_time

            # Cache daily refresh as per performance considerations
            if cache_age < timedelta(hours=24):
                logger.debug("Using cached NIFTY monthly data")
                return cached_data

        try:
            logger.info(f"Fetching {self.monthly_years_back} years of NIFTY monthly data")

            # Fetch monthly data using existing sampled method
            monthly_df = self.kite_client.get_historical_data_sampled(
                instrument_token=self.NIFTY_INSTRUMENT_TOKEN,
                timeframe='monthly',
                years_back=self.monthly_years_back
            )

            if monthly_df.empty:
                logger.warning("No NIFTY monthly data received")
                return pd.DataFrame()

            # Cache the result
            self._cache[cache_key] = (now_ist(), monthly_df.copy())

            logger.info(f"Fetched {len(monthly_df)} monthly NIFTY candles")
            return monthly_df

        except Exception as e:
            logger.error(f"Failed to fetch NIFTY monthly data: {e}")
            raise

    def fetch_weekly_data(self, use_cache: bool = True) -> pd.DataFrame:
        """
        Fetch 1.5 years of weekly OHLC data for NIFTY

        Includes retry logic for transient API failures

        Args:
            use_cache: Whether to use cached data if available

        Returns:
            DataFrame with weekly OHLC data
        """
        cache_key = "nifty_weekly_1_5y"

        # Check cache first
        if use_cache and cache_key in self._cache:
            cache_time, cached_data = self._cache[cache_key]
            cache_age = now_ist() - cache_time

            # Cache daily refresh as per performance considerations
            if cache_age < timedelta(hours=24):
                logger.debug("Using cached NIFTY weekly data")
                return cached_data

        # Retry logic for transient failures
        MAX_RETRIES = 3
        RETRY_DELAY = 2  # seconds

        last_error = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(
                    f"Fetching {self.weekly_years_back} years of NIFTY weekly data "
                    f"(attempt {attempt}/{MAX_RETRIES})"
                )

                # Fetch weekly data using existing sampled method
                weekly_df = self.kite_client.get_historical_data_sampled(
                    instrument_token=self.NIFTY_INSTRUMENT_TOKEN,
                    timeframe='weekly',
                    years_back=self.weekly_years_back
                )

                # Validation: Empty dataframe
                if weekly_df is None or weekly_df.empty:
                    logger.warning(f"NIFTY weekly data fetch returned empty (attempt {attempt})")

                    # If this is last attempt, send alert
                    if attempt == MAX_RETRIES:
                        self._send_data_alert(
                            "⚠️ **NIFTY Data Fetch Failed**\n"
                            f"All {MAX_RETRIES} attempts returned empty data\n"
                            "NIFTY filter will block trades\n"
                            "**Check Zerodha API status**"
                        )

                    # Retry on next attempt
                    last_error = "Empty dataframe"
                    if attempt < MAX_RETRIES:
                        import time
                        time.sleep(RETRY_DELAY)
                        continue
                    else:
                        return pd.DataFrame()  # All retries exhausted

                # Success - cache and return
                self._cache[cache_key] = (now_ist(), weekly_df.copy())

                logger.info(f"✅ Fetched {len(weekly_df)} weekly NIFTY candles")
                return weekly_df

            except Exception as e:
                last_error = str(e)
                logger.error(f"NIFTY weekly data fetch failed (attempt {attempt}/{MAX_RETRIES}): {e}")

                # If not last attempt, retry after delay
                if attempt < MAX_RETRIES:
                    import time
                    time.sleep(RETRY_DELAY)
                    continue
                else:
                    # All retries exhausted - send alert
                    self._send_data_alert(
                        f"🚨 **NIFTY Data Fetch Failed - All Retries Exhausted**\n"
                        f"Attempts: {MAX_RETRIES}\n"
                        f"Error: {str(e)[:150]}\n"
                        f"NIFTY filter will block trades\n"
                        f"**Check Zerodha API status**"
                    )
                    raise

        # Should not reach here, but defensive
        logger.error(f"NIFTY weekly data fetch failed after {MAX_RETRIES} attempts: {last_error}")
        return pd.DataFrame()

    def _send_data_alert(self, message: str):
        """Send Telegram alert for NIFTY data fetch issues (non-blocking)"""
        try:
            from src.reporting.telegram_client import telegram
            telegram.send_alert(message, critical=True)
        except Exception as e:
            # Don't fail on alert sending
            logger.error(f"Failed to send NIFTY data alert: {e}")

    def get_current_nifty_price(self) -> float:
        """
        Get current NIFTY price (LTP)

        Returns:
            Current NIFTY price as float
        """
        try:
            logger.debug("Fetching current NIFTY price")
            current_price = self.kite_client.get_instrument_ltp(self.NIFTY_INSTRUMENT_TOKEN)
            logger.debug(f"Current NIFTY price: {current_price}")
            return current_price

        except Exception as e:
            logger.error(f"Failed to get current NIFTY price: {e}")
            raise

    def validate_connection(self) -> bool:
        """
        Validate connection to Kite API using NIFTY data

        Returns:
            True if connection successful, False otherwise
        """
        try:
            logger.info("Validating NIFTY data fetcher connection")

            # Test connection using base client validation
            if not self.kite_client.validate_connection():
                logger.error("Base Kite client connection validation failed")
                return False

            # Test NIFTY-specific data fetch with minimal data
            today = now_ist().strftime('%Y-%m-%d')
            yesterday = (now_ist() - timedelta(days=1)).strftime('%Y-%m-%d')

            test_df = self.kite_client.get_historical_data(
                instrument_token=self.NIFTY_INSTRUMENT_TOKEN,
                start_date=yesterday,
                end_date=today,
                interval='day'
            )

            if test_df.empty:
                logger.warning("NIFTY test data fetch returned empty result")
                # This might be okay if markets are closed
                return True

            logger.info("NIFTY data fetcher connection validation successful")
            return True

        except Exception as e:
            logger.error(f"NIFTY data fetcher connection validation failed: {e}")
            return False

    def clear_cache(self) -> None:
        """Clear NIFTY data cache"""
        self._cache.clear()
        logger.info("NIFTY data cache cleared")

    def get_cache_stats(self) -> Dict[str, any]:
        """
        Get cache statistics

        Returns:
            Dictionary with cache information
        """
        stats = {
            "cache_entries": len(self._cache),
            "cached_datasets": list(self._cache.keys())
        }

        # Add cache ages
        for key, (cache_time, _) in self._cache.items():
            cache_age = now_ist() - cache_time
            stats[f"{key}_age_hours"] = cache_age.total_seconds() / 3600

        return stats