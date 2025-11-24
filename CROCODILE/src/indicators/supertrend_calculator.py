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

from src.api.kite_trade_client import KiteTradeClient
from src.utils.timezone_helper import now_ist


class SuperTrendCalculator:
    """SuperTrend calculation and signal verification engine"""

    def __init__(self, config_path: str = "config/config.yaml"):
        """Initialize SuperTrend calculator"""
        self.config = self._load_config(config_path)
        self.kite_client = KiteTradeClient(config_path)
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
        use_completed_candle_only: bool = False
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

            # Get values - always use current close for price comparison
            latest_close = float(df_with_st['Close'].iloc[-1])
            reference_supertrend = float(df_with_st['supertrend'].iloc[st_index])
            reference_trend = int(df_with_st['trend'].iloc[st_index])

            # Verify signal: Price should be above SuperTrend (bullish)
            # trend == -1 means UP/bullish in SuperTrend convention
            is_valid = reference_trend == -1 and latest_close > reference_supertrend

            metadata = {
                "script": script,
                "timeframe": timeframe,
                "signal_date": signal_date,
                "latest_close": latest_close,
                "supertrend_value": reference_supertrend,
                "trend": "UP" if reference_trend == -1 else "DOWN",
                "candle_reference": candle_type,
                "use_completed_candle_only": use_completed_candle_only,
                "calculation_time": now_ist().isoformat()
            }

            if is_valid:
                logger.info(f"Signal VERIFIED for {script} ({timeframe}): Close={latest_close:.2f}, ST={reference_supertrend:.2f} [{candle_type}]")
                return True, reference_supertrend, metadata
            else:
                logger.info(f"Signal NOT verified for {script} ({timeframe}): Close={latest_close:.2f}, ST={reference_supertrend:.2f}, Trend={metadata['trend']} [{candle_type}]")
                return False, None, metadata

        except Exception as e:
            logger.error(f"Signal verification failed for {script}: {e}")
            return False, None, {"error": str(e)}

    def validate_connection(self) -> bool:
        """Validate API connection"""
        return self.kite_client.validate_connection()