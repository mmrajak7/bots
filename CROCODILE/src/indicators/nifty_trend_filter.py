"""NIFTY Weekly Trend Filter"""

from typing import Tuple, Optional
from loguru import logger

from src.indicators.nifty_data_fetcher import NiftyDataFetcher
from src.indicators.supertrend_calculator import SuperTrendCalculator


class NiftyTrendFilter:
    """
    NIFTY weekly trend filter for market conditions

    Entry Rule: Only take trades when NIFTY weekly close > NIFTY weekly SuperTrend
    """

    def __init__(self):
        """Initialize NIFTY filter"""
        self.nifty_fetcher = NiftyDataFetcher()
        self.st_calculator = SuperTrendCalculator()

        logger.info("NIFTY Trend Filter initialized")

    def is_nifty_weekly_bullish(self) -> bool:
        """
        Check if NIFTY weekly is in bullish trend

        Returns:
            True if Weekly NIFTY Close > Weekly NIFTY SuperTrend
            False if bearish OR if data fetch fails (safe default)
        """
        is_bullish, error = self.check_nifty_trend()

        if error:
            # Data fetch failed - default to False (don't trade)
            logger.error(f"NIFTY filter check failed: {error}")
            return False

        return is_bullish

    def check_nifty_trend(self) -> Tuple[bool, Optional[str]]:
        """
        Check NIFTY weekly trend with detailed error reporting

        Returns:
            (is_bullish, error_message)
            - (True, None) = Bullish trend, trades allowed
            - (False, None) = Bearish trend (valid signal, no error)
            - (False, "error msg") = Data fetch failed, trades blocked
        """
        try:
            # === FETCH NIFTY WEEKLY DATA ===
            weekly_df = self.nifty_fetcher.fetch_weekly_data(use_cache=True)

            # Validation 1: Empty dataframe
            if weekly_df is None or weekly_df.empty:
                error_msg = "No NIFTY weekly data available (empty dataframe)"
                logger.error(error_msg)

                # Send Telegram alert for data fetch failure
                self._send_nifty_alert(
                    "🚨 **NIFTY Filter Data Failure**\n"
                    "Failed to fetch NIFTY weekly data\n"
                    "Result: Empty dataframe\n"
                    "**Impact: All new trades blocked until resolved**"
                )

                return False, error_msg

            # Validation 2: Insufficient data points
            if len(weekly_df) < 20:
                error_msg = f"Insufficient NIFTY data: {len(weekly_df)} candles (need >=20 for SuperTrend)"
                logger.error(error_msg)

                self._send_nifty_alert(
                    f"🚨 **NIFTY Filter Data Failure**\n"
                    f"Only {len(weekly_df)} weekly candles\n"
                    f"Need: >=20 for SuperTrend calculation\n"
                    f"**Impact: All new trades blocked until resolved**"
                )

                return False, error_msg

            # === CALCULATE SUPERTREND ===
            try:
                df_with_st = self.st_calculator.calculate_supertrend(weekly_df)
            except Exception as e:
                error_msg = f"SuperTrend calculation failed: {e}"
                logger.error(error_msg)

                self._send_nifty_alert(
                    f"🚨 **NIFTY Filter Calculation Failure**\n"
                    f"SuperTrend calculation error: {str(e)[:100]}\n"
                    f"**Impact: All new trades blocked until resolved**"
                )

                return False, error_msg

            # Validation 3: SuperTrend result has data
            if df_with_st.empty or 'supertrend' not in df_with_st.columns:
                error_msg = "SuperTrend calculation returned invalid data"
                logger.error(error_msg)

                self._send_nifty_alert(
                    "🚨 **NIFTY Filter Calculation Failure**\n"
                    "SuperTrend result missing or invalid\n"
                    "**Impact: All new trades blocked until resolved**"
                )

                return False, error_msg

            # === GET VALUES ===
            # Use completed candle only (ignore current incomplete candle during market hours)
            # For weekly data: use previous completed week's SuperTrend
            try:
                if len(df_with_st) >= 2:
                    # Use previous completed candle's SuperTrend (stable reference)
                    latest_close = float(df_with_st['Close'].iloc[-1])  # Current price for comparison
                    latest_supertrend = float(df_with_st['supertrend'].iloc[-2])  # Previous completed candle's ST
                    latest_trend = int(df_with_st['trend'].iloc[-2])  # Previous completed candle's trend
                else:
                    # Fallback if insufficient data
                    latest_close = float(df_with_st['Close'].iloc[-1])
                    latest_supertrend = float(df_with_st['supertrend'].iloc[-1])
                    latest_trend = int(df_with_st['trend'].iloc[-1])
            except (KeyError, IndexError, ValueError) as e:
                error_msg = f"Failed to extract NIFTY trend values: {e}"
                logger.error(error_msg)

                self._send_nifty_alert(
                    f"🚨 **NIFTY Filter Data Extraction Failure**\n"
                    f"Error: {str(e)[:100]}\n"
                    f"**Impact: All new trades blocked until resolved**"
                )

                return False, error_msg

            # === CHECK TREND ===
            # trend = -1 means price > SuperTrend (bullish)
            # trend = 1 means price < SuperTrend (bearish)
            is_bullish = latest_trend == -1 and latest_close > latest_supertrend

            logger.info(
                f"NIFTY Weekly Filter: Close={latest_close:.2f}, "
                f"ST={latest_supertrend:.2f} [completed candle], "
                f"Trend={'BULLISH' if is_bullish else 'BEARISH'}"
            )

            # Return result with NO error (None means data is valid)
            return is_bullish, None

        except Exception as e:
            # Unexpected error - catch all
            error_msg = f"Unexpected NIFTY filter error: {str(e)}"
            logger.error(error_msg)

            self._send_nifty_alert(
                f"🚨 **NIFTY Filter Critical Error**\n"
                f"Unexpected error: {str(e)[:150]}\n"
                f"**Impact: All new trades blocked until resolved**"
            )

            return False, error_msg

    def _send_nifty_alert(self, message: str):
        """Send Telegram alert for NIFTY filter issues (non-blocking)"""
        try:
            from src.reporting.telegram_client import telegram
            telegram.send_alert(message, critical=True)
        except Exception as e:
            # Don't fail on alert sending
            logger.error(f"Failed to send NIFTY filter alert: {e}")


# Singleton instance
nifty_filter = NiftyTrendFilter()
