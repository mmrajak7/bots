"""
SuperTrend Calculator for Signal Verification
Implements SuperTrend(10,3) calculation and signal verification logic
"""

from datetime import datetime, timedelta
from typing import Tuple, Optional, Dict, Any

import numpy as np
import pandas as pd
import yaml
from loguru import logger

from src.api.broker_factory import get_broker
from src.utils.timezone_helper import now_ist


class SuperTrendCalculator:
    """SuperTrend calculation and signal verification engine"""

    def __init__(self, config_path: str = "config/config.yaml"):
        """Initialize SuperTrend calculator"""
        self.config = self._load_config(config_path)
        self.kite_client = get_broker(config_path)
        self.period = self.config['supertrend']['period']
        self.multiplier = self.config['supertrend']['multiplier']

    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """Load configuration from YAML file"""
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    def calculate_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """
        Calculate Average True Range (ATR)

        Args:
            df: DataFrame with High, Low, Close columns
            period: ATR period

        Returns:
            Series with ATR values
        """
        if len(df) < period:
            raise ValueError(f"Insufficient data for ATR calculation. Need at least {period} candles, got {len(df)}")

        # True Range calculation
        df = df.copy()
        df['prev_close'] = df['Close'].shift(1)
        df['tr1'] = df['High'] - df['Low']
        df['tr2'] = abs(df['High'] - df['prev_close'])
        df['tr3'] = abs(df['Low'] - df['prev_close'])
        df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)

        # ATR using Wilder's smoothing
        atr = df['tr'].ewm(alpha=1/period, adjust=False).mean()
        return atr

    def calculate_supertrend(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Calculate SuperTrend indicator

        Args:
            df: DataFrame with Open, High, Low, Close columns

        Returns:
            DataFrame with SuperTrend columns added
        """
        if len(df) < max(self.period, 14):
            raise ValueError(f"Insufficient data for SuperTrend calculation. Need at least {max(self.period, 14)} candles")

        df = df.copy()

        # Calculate ATR
        atr = self.calculate_atr(df, self.period)

        # Calculate basic upper and lower bands
        hl2 = (df['High'] + df['Low']) / 2
        df['basic_ub'] = hl2 + (self.multiplier * atr)
        df['basic_lb'] = hl2 - (self.multiplier * atr)

        # Initialize final upper and lower bands
        df['final_ub'] = 0.0
        df['final_lb'] = 0.0
        df['supertrend'] = 0.0
        df['trend'] = 0

        # Calculate final bands
        for i in range(len(df)):
            if i == 0:
                df.iloc[i, df.columns.get_loc('final_ub')] = df.iloc[i]['basic_ub']
                df.iloc[i, df.columns.get_loc('final_lb')] = df.iloc[i]['basic_lb']
            else:
                # Final Upper Band
                if df.iloc[i]['basic_ub'] < df.iloc[i-1]['final_ub'] or df.iloc[i-1]['Close'] > df.iloc[i-1]['final_ub']:
                    df.iloc[i, df.columns.get_loc('final_ub')] = df.iloc[i]['basic_ub']
                else:
                    df.iloc[i, df.columns.get_loc('final_ub')] = df.iloc[i-1]['final_ub']

                # Final Lower Band
                if df.iloc[i]['basic_lb'] > df.iloc[i-1]['final_lb'] or df.iloc[i-1]['Close'] < df.iloc[i-1]['final_lb']:
                    df.iloc[i, df.columns.get_loc('final_lb')] = df.iloc[i]['basic_lb']
                else:
                    df.iloc[i, df.columns.get_loc('final_lb')] = df.iloc[i-1]['final_lb']

        # Calculate SuperTrend and Trend
        for i in range(len(df)):
            if i == 0:
                df.iloc[i, df.columns.get_loc('supertrend')] = df.iloc[i]['final_ub']
                df.iloc[i, df.columns.get_loc('trend')] = 1
            else:
                # Determine trend
                if df.iloc[i-1]['supertrend'] == df.iloc[i-1]['final_ub'] and df.iloc[i]['Close'] <= df.iloc[i]['final_ub']:
                    df.iloc[i, df.columns.get_loc('trend')] = 1
                elif df.iloc[i-1]['supertrend'] == df.iloc[i-1]['final_ub'] and df.iloc[i]['Close'] > df.iloc[i]['final_ub']:
                    df.iloc[i, df.columns.get_loc('trend')] = -1
                elif df.iloc[i-1]['supertrend'] == df.iloc[i-1]['final_lb'] and df.iloc[i]['Close'] >= df.iloc[i]['final_lb']:
                    df.iloc[i, df.columns.get_loc('trend')] = -1
                elif df.iloc[i-1]['supertrend'] == df.iloc[i-1]['final_lb'] and df.iloc[i]['Close'] < df.iloc[i]['final_lb']:
                    df.iloc[i, df.columns.get_loc('trend')] = 1
                else:
                    df.iloc[i, df.columns.get_loc('trend')] = df.iloc[i-1]['trend']

                # Set SuperTrend value
                if df.iloc[i]['trend'] == 1:
                    df.iloc[i, df.columns.get_loc('supertrend')] = df.iloc[i]['final_ub']
                else:
                    df.iloc[i, df.columns.get_loc('supertrend')] = df.iloc[i]['final_lb']

        return df

    def get_historical_data_for_timeframe(self, script: str, timeframe: str) -> pd.DataFrame:
        """
        Fetch historical data based on timeframe requirements using sampling

        Args:
            script: Stock symbol (e.g., "VOLTAS", "IRCTC")
            timeframe: "M" for Monthly, "W" for Weekly

        Returns:
            DataFrame with sufficient historical data
        """
        # Map timeframe to data requirements
        if timeframe.upper() == 'M':
            years = self.config['historical_data']['monthly_lookback_years']
            sampling_timeframe = 'monthly'
        elif timeframe.upper() == 'W':
            years = self.config['historical_data']['weekly_lookback_years']
            sampling_timeframe = 'weekly'
        elif timeframe.upper() == 'D':
            years = self.config['historical_data']['daily_lookback_years']
            sampling_timeframe = 'daily'
        else:
            raise ValueError(f"Unsupported timeframe: {timeframe}. Use 'M', 'W', or 'D'")

        # Get instrument token
        instrument_token = self._get_instrument_token(script)

        try:
            # Use sampled data method which fetches daily data and resamples
            df = self.kite_client.get_historical_data_sampled(
                instrument_token=instrument_token,
                timeframe=sampling_timeframe,
                years_back=years
            )

            if df.empty:
                raise Exception(f"No historical data available for {script}")

            logger.info(f"Fetched {len(df)} {sampling_timeframe} candles for {script} ({timeframe})")
            return df

        except Exception as e:
            logger.error(f"Failed to fetch historical data for {script}: {e}")
            raise

    def _get_instrument_token(self, script: str) -> str:
        """
        Get instrument token for script using Kite instruments API
        """
        try:
            return self.kite_client.get_instrument_token(script)
        except Exception as e:
            logger.error(f"Failed to get instrument token for {script}: {e}")
            raise ValueError(f"Instrument token not found for script: {script}") from e

    def verify_signal(
        self,
        script: str,
        timeframe: str,
        signal_date: str,
        use_completed_candle_only: bool = False,
        check_fresh_touch: bool = True
    ) -> Tuple[bool, Optional[float], Dict[str, Any]]:
        """
        Verify if signal is valid based on SuperTrend calculation

        Args:
            script: Stock symbol
            timeframe: "M", "W", or "D"
            signal_date: Date when signal was generated
            use_completed_candle_only: If True, use previous completed candle's SuperTrend
                                       (ignores current incomplete candle during market hours).
                                       Use for signal processing and order monitoring.
                                       Default False for after-hours operations (GTT, SL, recovery).
            check_fresh_touch: If True, validate that this is a "fresh touch"
                              (no touches in previous N candles). Default True.

        Returns:
            Tuple of (is_valid, buy_level, metadata)
        """
        try:
            # Fetch historical data
            df = self.get_historical_data_for_timeframe(script, timeframe)

            if df.empty:
                return False, None, {"error": "No historical data available"}

            # Calculate SuperTrend
            df_with_st = self.calculate_supertrend(df)

            # Determine which row to use for SuperTrend reference
            if use_completed_candle_only and len(df_with_st) >= 2:
                # Use previous completed candle's SuperTrend (ignores current incomplete candle)
                # This is used during market hours for signal processing/order monitoring
                st_index = -2
                candle_type = "completed"
            else:
                # Use current candle (default for after-hours when candle is complete)
                st_index = -1
                candle_type = "current"

            # CRITICAL: Always use current close (today's LTP) for reference
            # But use completed candle's ST when use_completed_candle_only=True
            latest_close = float(df_with_st['Close'].iloc[-1])  # Always today's LTP
            reference_supertrend = float(df_with_st['supertrend'].iloc[st_index])  # Yesterday's ST if use_completed=True
            reference_trend = int(df_with_st['trend'].iloc[st_index])  # Yesterday's trend if use_completed=True

            # Verify signal: Only check trend direction (UP/bullish)
            # trend == -1 means UP/bullish in SuperTrend convention
            # Price can be below ST (good entry!) or above ST - trend direction matters
            is_valid = reference_trend == -1

            metadata = {
                "script": script,
                "timeframe": timeframe,
                "signal_date": signal_date,
                "close_price": latest_close,
                "supertrend_value": reference_supertrend,
                "trend": "UP" if reference_trend == -1 else "DOWN",
                "candle_reference": candle_type,
                "candle_index": st_index,
                "use_completed_candle_only": use_completed_candle_only,
                "calculation_time": now_ist().isoformat(),
                "fresh_touch_checked": False,
                "fresh_touch_result": None,
                "fresh_touch_reason": None
            }

            # Early exit if trend direction check failed
            if not is_valid:
                logger.info(f"❌ Signal REJECTED for {script} ({timeframe}): Trend={metadata['trend']} (need UP), Close={latest_close:.2f}, ST={reference_supertrend:.2f} [{candle_type} candle]")
                return False, None, metadata

            # Fresh Touch Filter (if enabled)
            if check_fresh_touch:
                fresh_touch_enabled = self.config.get('supertrend', {}).get('fresh_touch', {}).get('enabled', True)
                lookback = self.config.get('supertrend', {}).get('fresh_touch', {}).get('lookback_candles', 10)

                if fresh_touch_enabled:
                    is_fresh, fresh_reason = self.is_fresh_touch(df_with_st, lookback)
                    metadata["fresh_touch_checked"] = True
                    metadata["fresh_touch_result"] = is_fresh
                    metadata["fresh_touch_reason"] = fresh_reason
                    metadata["fresh_touch_lookback"] = lookback

                    if not is_fresh:
                        logger.info(
                            f"❌ Signal REJECTED for {script} ({timeframe}): "
                            f"Not a fresh touch - {fresh_reason}"
                        )
                        return False, None, metadata
                    else:
                        logger.debug(f"Fresh touch PASSED for {script}: {fresh_reason}")

            # Price Proximity Filter (prevents chasing breakouts)
            # Reject if LTP is too far above SuperTrend - signal has "run away"
            proximity_config = self.config.get('supertrend', {}).get('price_proximity', {})
            proximity_enabled = proximity_config.get('enabled', True)
            max_distance_pct = proximity_config.get('max_distance_pct', 5.0)

            if proximity_enabled and reference_supertrend > 0:
                price_distance_pct = ((latest_close - reference_supertrend) / reference_supertrend) * 100

                metadata["price_proximity_checked"] = True
                metadata["price_distance_pct"] = price_distance_pct
                metadata["max_distance_pct"] = max_distance_pct

                if price_distance_pct > max_distance_pct:
                    logger.info(
                        f"❌ Signal REJECTED for {script} ({timeframe}): "
                        f"Price too far from ST - LTP={latest_close:.2f} is {price_distance_pct:.1f}% above ST={reference_supertrend:.2f} "
                        f"(max allowed: {max_distance_pct}%)"
                    )
                    metadata["rejection_reason"] = "price_proximity"
                    return False, None, metadata
                else:
                    position_desc = f"{price_distance_pct:.1f}% above" if price_distance_pct > 0 else f"{abs(price_distance_pct):.1f}% below"
                    logger.debug(
                        f"Price proximity PASSED for {script}: "
                        f"LTP={latest_close:.2f} is {position_desc} ST (max above: {max_distance_pct}%)"
                    )

            # All checks passed
            price_position = "above" if latest_close > reference_supertrend else "below"
            fresh_info = ""
            if metadata.get("fresh_touch_checked"):
                fresh_info = f", Fresh={metadata['fresh_touch_result']}"
            logger.info(f"✅ Signal VERIFIED for {script} ({timeframe}): Trend={metadata['trend']}, Close={latest_close:.2f} ({price_position} ST={reference_supertrend:.2f}) [{candle_type} candle]{fresh_info}")
            return True, reference_supertrend, metadata

        except Exception as e:
            logger.error(f"Signal verification failed for {script}: {e}")
            return False, None, {"error": str(e)}

    def is_fresh_touch(
        self,
        df_with_st: pd.DataFrame,
        lookback: int = 10
    ) -> Tuple[bool, str]:
        """
        Check if current touch is "fresh" (first time price touches ST in last N candles)

        A fresh touch means price APPROACHED SuperTrend from above, indicating a
        true pullback-to-support scenario rather than price grinding along ST line.

        Args:
            df_with_st: DataFrame with OHLCV + SuperTrend columns (from calculate_supertrend)
            lookback: Number of previous candles to check (default: 10)

        Returns:
            Tuple of (is_fresh, reason_string)

        Logic:
        - Previous N candles must have LOW > SuperTrend (strict, no tolerance)
        - Current candle touch detection uses existing Chartink tolerance (handled elsewhere)

        Edge Cases:
        - Insufficient history (<N candles): Returns True (allow entry, can't verify)
        - First touch after ST direction flip: Returns True (direction just changed = fresh)
        """
        if len(df_with_st) < 2:
            return True, "Insufficient data for fresh touch check"

        # Use completed candle for reference (index -2), current candle is -1
        # This matches the use_completed_candle_only=True logic in verify_signal
        current_idx = len(df_with_st) - 1

        # Edge case: Insufficient history for full lookback
        available_candles = current_idx  # How many candles before current
        if available_candles < lookback:
            logger.debug(
                f"Fresh touch: Only {available_candles} candles available "
                f"(need {lookback}), allowing entry"
            )
            return True, f"Insufficient history ({available_candles} < {lookback} candles)"

        # Edge case: Check for ST direction flip in lookback period
        # If trend changed from DOWN to UP within lookback, consider fresh
        current_trend = df_with_st['trend'].iloc[-1]
        for i in range(1, min(lookback + 1, available_candles + 1)):
            prev_trend = df_with_st['trend'].iloc[current_idx - i]
            if prev_trend != current_trend:
                # Trend flipped within lookback period - this is a fresh touch
                candles_since_flip = i
                logger.debug(
                    f"Fresh touch: ST direction flipped {candles_since_flip} candles ago, "
                    f"considering as fresh entry"
                )
                return True, f"ST direction flipped {candles_since_flip} candles ago"

        # Check previous N candles for any touch (strict: LOW <= ST)
        for i in range(1, lookback + 1):
            check_idx = current_idx - i
            if check_idx < 0:
                break

            candle_low = df_with_st['Low'].iloc[check_idx]
            candle_st = df_with_st['supertrend'].iloc[check_idx]

            # Strict check: LOW must be strictly above ST (no tolerance)
            if candle_low <= candle_st:
                # Found a previous touch - NOT a fresh touch
                candle_date = df_with_st.index[check_idx] if isinstance(df_with_st.index, pd.DatetimeIndex) else f"candle-{i}"
                logger.debug(
                    f"Fresh touch FAILED: Candle {i} back ({candle_date}) had "
                    f"LOW={candle_low:.2f} <= ST={candle_st:.2f}"
                )
                return False, f"Previous touch found {i} candles ago (LOW={candle_low:.2f} <= ST={candle_st:.2f})"

        # All previous N candles had gap (LOW > ST) - this is a fresh touch!
        return True, f"No touches in previous {lookback} candles"

    def validate_connection(self) -> bool:
        """Validate API connection"""
        return self.kite_client.validate_connection()