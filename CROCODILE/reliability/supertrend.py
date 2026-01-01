"""
SuperTrend Indicator Calculator
================================
Calculates SuperTrend(10,3) for CROCODILE reliability testing.

SuperTrend Formula:
- ATR = Average True Range (14 periods)
- Basic Upper Band = (High + Low) / 2 + (Multiplier * ATR)
- Basic Lower Band = (High + Low) / 2 - (Multiplier * ATR)
- Final bands adjust based on previous values and close
- Trend direction determined by close vs bands
"""

import pandas as pd
import numpy as np
from typing import Tuple


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Calculate Average True Range (ATR).

    True Range = max(
        High - Low,
        abs(High - Previous Close),
        abs(Low - Previous Close)
    )
    """
    high = df['high']
    low = df['low']
    close = df['close']

    # True Range components
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))

    # True Range is max of the three
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # ATR is rolling mean of TR
    atr = tr.rolling(window=period, min_periods=1).mean()

    return atr


def calculate_supertrend(
    df: pd.DataFrame,
    period: int = 10,
    multiplier: float = 3.0,
    atr_period: int = 10  # TradingView default (was 14)
) -> pd.DataFrame:
    """
    Calculate SuperTrend indicator.

    Args:
        df: DataFrame with columns: date, open, high, low, close, volume
        period: SuperTrend period (default 10)
        multiplier: ATR multiplier (default 3.0)
        atr_period: ATR calculation period (default 14)

    Returns:
        DataFrame with additional columns:
        - atr: Average True Range
        - supertrend: SuperTrend value
        - direction: 1 for uptrend, -1 for downtrend
    """
    df = df.copy()

    # Calculate ATR
    df['atr'] = calculate_atr(df, atr_period)

    # Calculate basic bands
    hl2 = (df['high'] + df['low']) / 2
    df['basic_upper'] = hl2 + (multiplier * df['atr'])
    df['basic_lower'] = hl2 - (multiplier * df['atr'])

    # Initialize final bands and direction
    df['final_upper'] = df['basic_upper']
    df['final_lower'] = df['basic_lower']
    df['supertrend'] = 0.0
    df['direction'] = 1  # 1 = uptrend, -1 = downtrend

    # Calculate SuperTrend iteratively
    for i in range(1, len(df)):
        # Final Upper Band
        if df['basic_upper'].iloc[i] < df['final_upper'].iloc[i-1] or df['close'].iloc[i-1] > df['final_upper'].iloc[i-1]:
            df.loc[df.index[i], 'final_upper'] = df['basic_upper'].iloc[i]
        else:
            df.loc[df.index[i], 'final_upper'] = df['final_upper'].iloc[i-1]

        # Final Lower Band
        if df['basic_lower'].iloc[i] > df['final_lower'].iloc[i-1] or df['close'].iloc[i-1] < df['final_lower'].iloc[i-1]:
            df.loc[df.index[i], 'final_lower'] = df['basic_lower'].iloc[i]
        else:
            df.loc[df.index[i], 'final_lower'] = df['final_lower'].iloc[i-1]

        # SuperTrend and Direction
        if df['direction'].iloc[i-1] == 1:  # Was in uptrend
            if df['close'].iloc[i] < df['final_lower'].iloc[i]:
                # Trend reversal to downtrend
                df.loc[df.index[i], 'direction'] = -1
                df.loc[df.index[i], 'supertrend'] = df['final_upper'].iloc[i]
            else:
                # Continue uptrend
                df.loc[df.index[i], 'direction'] = 1
                df.loc[df.index[i], 'supertrend'] = df['final_lower'].iloc[i]
        else:  # Was in downtrend
            if df['close'].iloc[i] > df['final_upper'].iloc[i]:
                # Trend reversal to uptrend
                df.loc[df.index[i], 'direction'] = 1
                df.loc[df.index[i], 'supertrend'] = df['final_lower'].iloc[i]
            else:
                # Continue downtrend
                df.loc[df.index[i], 'direction'] = -1
                df.loc[df.index[i], 'supertrend'] = df['final_upper'].iloc[i]

    # Set first row SuperTrend
    df.loc[df.index[0], 'supertrend'] = df['final_lower'].iloc[0]

    # Clean up intermediate columns
    df = df.drop(columns=['basic_upper', 'basic_lower', 'final_upper', 'final_lower'])

    return df


def find_supertrend_touches(
    df: pd.DataFrame,
    touch_threshold_pct: float = 1.5,
    fresh_touch_lookback: int = 10
) -> pd.DataFrame:
    """
    Find SuperTrend touch events in uptrend.

    A "touch" occurs when:
    1. Direction is uptrend (1)
    2. Low touches or crosses below SuperTrend (within threshold)
    3. Close remains above SuperTrend (didn't break down)

    Args:
        df: DataFrame with SuperTrend calculated
        touch_threshold_pct: % threshold for touch detection (default 1.5%)
        fresh_touch_lookback: Candles to check for fresh touch (default 10)

    Returns:
        DataFrame with touch events marked
    """
    df = df.copy()

    # Initialize touch columns
    df['is_touch'] = False
    df['is_fresh_touch'] = False
    df['touch_distance_pct'] = 0.0

    for i in range(fresh_touch_lookback, len(df)):
        # Only check uptrend periods
        if df['direction'].iloc[i] != 1:
            continue

        st = df['supertrend'].iloc[i]
        low = df['low'].iloc[i]
        close = df['close'].iloc[i]

        # Calculate distance from low to SuperTrend
        if st > 0:
            distance_pct = (low - st) / st * 100
            df.loc[df.index[i], 'touch_distance_pct'] = distance_pct

            # Touch: low within threshold of ST or below, but close above ST
            if distance_pct <= touch_threshold_pct and close > st:
                df.loc[df.index[i], 'is_touch'] = True

                # Fresh touch: previous N candles had low > ST (gap existed)
                lookback_slice = df.iloc[i-fresh_touch_lookback:i]
                if len(lookback_slice) >= fresh_touch_lookback:
                    all_above = all(
                        lookback_slice['low'] > lookback_slice['supertrend'] * 1.005  # 0.5% buffer
                    )
                    df.loc[df.index[i], 'is_fresh_touch'] = all_above

    return df


if __name__ == "__main__":
    # Test with sample data
    import sys
    from pathlib import Path

    # Try to load a sample file
    sample_file = Path(__file__).parent.parent.parent / "Bouncer" / "data" / "historical" / "RELIANCE_daily.csv"

    if sample_file.exists():
        print(f"Loading: {sample_file}")
        df = pd.read_csv(sample_file)
        df['date'] = pd.to_datetime(df['date'])

        # Calculate SuperTrend
        df = calculate_supertrend(df, period=10, multiplier=3.0)

        # Find touches
        df = find_supertrend_touches(df)

        # Summary
        total_touches = df['is_touch'].sum()
        fresh_touches = df['is_fresh_touch'].sum()

        print(f"\nSuperTrend(10,3) Analysis for RELIANCE:")
        print(f"  Total candles: {len(df)}")
        print(f"  Uptrend candles: {(df['direction'] == 1).sum()}")
        print(f"  Total touches: {total_touches}")
        print(f"  Fresh touches: {fresh_touches}")

        # Show last 5 touches
        touches = df[df['is_touch']].tail(5)
        print(f"\nLast 5 touches:")
        print(touches[['date', 'close', 'supertrend', 'touch_distance_pct', 'is_fresh_touch']].to_string())
    else:
        print(f"Sample file not found: {sample_file}")
