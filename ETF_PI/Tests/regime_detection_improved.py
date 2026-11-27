"""
IMPROVED REGIME DETECTION - Based on Research & Backtesting

Key Insights from Research:
1. Gaps have ~60-70% fill rate - so gap direction often REVERSES (mean reversion)
2. RSI extremes (>70, <30) predict reversal with ~97% accuracy in volatile periods
3. ADX > 25 indicates strong trend, ADX < 20 indicates ranging/choppy
4. Price vs VWAP/MA indicates bias
5. Combining indicators improves accuracy significantly

New Regime Detection Logic:
- Uses RSI, ADX, Moving Averages, Bollinger Bands, ATR
- Mean reversion logic for gaps (fade the gap)
- Trend strength filtering with ADX
- Multiple confirmation signals

Sources:
- https://www.quantifiedstrategies.com/macd-and-rsi-strategy/
- https://www.quantifiedstrategies.com/gap-fill-trading-strategies/
- https://tradethatswing.com/sp-500-spy-es-gap-fill-strategy-and-statistics/
"""

import json
import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple, List
import pandas as pd
import numpy as np

# Token file path
TOKEN_FILE = r'C:\Users\mail2\Documents\Projects\BOTS\data\kite_access_token.json'
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))

# NIFTY 50 instrument token
NIFTY_TOKEN = 256265


def load_kite_token() -> Dict[str, Any]:
    """Load access token from shared JSON file"""
    with open(TOKEN_FILE, 'r') as f:
        return json.load(f)


def get_kite_client():
    """Get authenticated KiteConnect instance"""
    from kiteconnect import KiteConnect
    token_data = load_kite_token()
    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


# =============================================================================
# TECHNICAL INDICATORS
# =============================================================================

def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Relative Strength Index"""
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Average Directional Index (trend strength)"""
    high = df['high']
    low = df['low']
    close = df['close']

    plus_dm = high.diff()
    minus_dm = low.diff()

    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm > 0] = 0
    minus_dm = abs(minus_dm)

    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.rolling(window=period).mean()
    plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr)

    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    adx = dx.rolling(window=period).mean()

    return adx


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Average True Range (volatility)"""
    high = df['high']
    low = df['low']
    close = df['close']

    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    return tr.rolling(window=period).mean()


def calculate_bollinger_bands(prices: pd.Series, period: int = 20, std_dev: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Calculate Bollinger Bands"""
    sma = prices.rolling(window=period).mean()
    std = prices.rolling(window=period).std()
    upper = sma + (std * std_dev)
    lower = sma - (std * std_dev)
    return upper, sma, lower


def calculate_ema(prices: pd.Series, period: int) -> pd.Series:
    """Calculate Exponential Moving Average"""
    return prices.ewm(span=period, adjust=False).mean()


def calculate_macd(prices: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Calculate MACD"""
    ema_fast = calculate_ema(prices, fast)
    ema_slow = calculate_ema(prices, slow)
    macd_line = ema_fast - ema_slow
    signal_line = calculate_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calculate_stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> Tuple[pd.Series, pd.Series]:
    """Calculate Stochastic Oscillator"""
    low_min = df['low'].rolling(window=k_period).min()
    high_max = df['high'].rolling(window=k_period).max()

    stoch_k = 100 * (df['close'] - low_min) / (high_max - low_min)
    stoch_d = stoch_k.rolling(window=d_period).mean()

    return stoch_k, stoch_d


# =============================================================================
# REGIME DETECTION STRATEGIES
# =============================================================================

def strategy_original(today_open: float, yesterday_close: float, last_3_days: pd.DataFrame) -> Tuple[str, Dict]:
    """Original regime detection (for comparison)"""
    gap_pct = (today_open - yesterday_close) / yesterday_close
    gap_down = gap_pct < -0.001
    gap_up = gap_pct > 0.001

    if len(last_3_days) >= 3:
        recent = last_3_days.tail(3)
        bullish_days = (recent['close'] > recent['open']).sum()
        bearish_days = 3 - bullish_days
    else:
        bullish_days = bearish_days = 0

    intraday_bias_bullish = gap_up or (not gap_down and bullish_days >= 2)

    if gap_down or (bearish_days >= 2 and not intraday_bias_bullish):
        regime = 'BEARISH'
    elif gap_up or (bullish_days >= 2 and intraday_bias_bullish):
        regime = 'BULLISH'
    else:
        regime = 'RANGE'

    return regime, {'gap_pct': gap_pct * 100}


def strategy_gap_fade(today_open: float, yesterday_close: float, yesterday_low: float, yesterday_high: float, atr: float) -> Tuple[str, Dict]:
    """
    Gap Fade Strategy - Based on ~60-70% gap fill rate research

    Logic: Gaps tend to fill, so FADE the gap direction
    - Gap up > 0.3% -> expect DOWN (gap fill)
    - Gap down < -0.3% -> expect UP (gap fill)
    - Small gap -> RANGE (no edge)

    Filter: Only fade if gap is within 1.5x ATR (avoid runaway gaps)
    """
    gap_pct = (today_open - yesterday_close) / yesterday_close * 100
    gap_points = today_open - yesterday_close

    # ATR-normalized gap
    gap_atr_ratio = abs(gap_points) / atr if atr > 0 else 0

    details = {
        'gap_pct': round(gap_pct, 2),
        'gap_atr_ratio': round(gap_atr_ratio, 2)
    }

    # Only fade reasonable gaps (not runaway gaps > 1.5x ATR)
    if gap_atr_ratio > 1.5:
        # Large gap - trend continuation more likely
        if gap_pct > 0:
            return 'BULLISH', details
        else:
            return 'BEARISH', details

    # Fade the gap
    if gap_pct > 0.3:
        return 'BEARISH', details  # Gap up -> fade down
    elif gap_pct < -0.3:
        return 'BULLISH', details  # Gap down -> fade up
    else:
        return 'RANGE', details


def strategy_rsi_extreme(rsi: float, prev_rsi: float) -> Tuple[str, Dict]:
    """
    RSI Extreme Strategy - Based on mean reversion at extremes

    RSI > 70 and turning down -> BEARISH
    RSI < 30 and turning up -> BULLISH
    Otherwise -> RANGE
    """
    details = {'rsi': round(rsi, 1), 'prev_rsi': round(prev_rsi, 1)}

    if rsi > 70 and rsi < prev_rsi:  # Overbought and turning
        return 'BEARISH', details
    elif rsi < 30 and rsi > prev_rsi:  # Oversold and turning
        return 'BULLISH', details
    elif rsi > 60:  # Bullish momentum
        return 'BULLISH', details
    elif rsi < 40:  # Bearish momentum
        return 'BEARISH', details
    else:
        return 'RANGE', details


def strategy_trend_following(
    close: float,
    ema_20: float,
    ema_50: float,
    adx: float
) -> Tuple[str, Dict]:
    """
    Trend Following Strategy with ADX filter

    ADX > 25 = Strong trend, follow it
    ADX < 20 = Weak/range, don't trade

    Price > EMA20 > EMA50 = Bullish trend
    Price < EMA20 < EMA50 = Bearish trend
    """
    details = {
        'adx': round(adx, 1),
        'price_vs_ema20': round((close - ema_20) / ema_20 * 100, 2),
        'ema20_vs_ema50': round((ema_20 - ema_50) / ema_50 * 100, 2)
    }

    # Trend strength filter
    if adx < 20:
        return 'RANGE', details

    # Strong trend - follow it
    if close > ema_20 and ema_20 > ema_50:
        return 'BULLISH', details
    elif close < ema_20 and ema_20 < ema_50:
        return 'BEARISH', details
    else:
        return 'RANGE', details


def strategy_bollinger_reversion(
    close: float,
    bb_upper: float,
    bb_middle: float,
    bb_lower: float,
    rsi: float
) -> Tuple[str, Dict]:
    """
    Bollinger Bands Mean Reversion Strategy

    Price near upper band + RSI > 70 -> BEARISH (expect reversion)
    Price near lower band + RSI < 30 -> BULLISH (expect reversion)
    """
    bb_position = (close - bb_lower) / (bb_upper - bb_lower) if (bb_upper - bb_lower) > 0 else 0.5

    details = {
        'bb_position': round(bb_position, 2),
        'rsi': round(rsi, 1)
    }

    if bb_position > 0.9 and rsi > 65:  # Near upper band + overbought
        return 'BEARISH', details
    elif bb_position < 0.1 and rsi < 35:  # Near lower band + oversold
        return 'BULLISH', details
    elif bb_position > 0.7:
        return 'BULLISH', details  # Upper half - momentum up
    elif bb_position < 0.3:
        return 'BEARISH', details  # Lower half - momentum down
    else:
        return 'RANGE', details


def strategy_macd_momentum(macd_hist: float, prev_macd_hist: float, macd_line: float) -> Tuple[str, Dict]:
    """
    MACD Momentum Strategy

    MACD histogram increasing -> momentum building
    MACD histogram decreasing -> momentum fading
    """
    details = {
        'macd_hist': round(macd_hist, 2),
        'macd_hist_change': round(macd_hist - prev_macd_hist, 2),
        'macd_line': round(macd_line, 2)
    }

    hist_change = macd_hist - prev_macd_hist

    if macd_line > 0 and hist_change > 0:  # Bullish and accelerating
        return 'BULLISH', details
    elif macd_line < 0 and hist_change < 0:  # Bearish and accelerating
        return 'BEARISH', details
    elif macd_line > 0 and hist_change < 0:  # Bullish but decelerating
        return 'RANGE', details
    elif macd_line < 0 and hist_change > 0:  # Bearish but decelerating
        return 'RANGE', details
    else:
        return 'RANGE', details


def strategy_composite_v1(
    today_open: float,
    yesterday: pd.Series,
    indicators: Dict[str, float]
) -> Tuple[str, Dict]:
    """
    Composite Strategy V1 - Weighted voting system

    Combines:
    1. Gap fade (weight: 2)
    2. RSI momentum (weight: 2)
    3. Trend (EMA alignment) (weight: 1)
    4. ADX filter (binary: trade or not)
    """
    gap_pct = (today_open - yesterday['close']) / yesterday['close'] * 100

    bullish_score = 0
    bearish_score = 0
    signals = []

    # 1. Gap fade logic (weight: 2)
    if 0.1 < abs(gap_pct) < 1.0:  # Reasonable gap to fade
        if gap_pct > 0.1:
            bearish_score += 2
            signals.append('gap_fade_bear')
        elif gap_pct < -0.1:
            bullish_score += 2
            signals.append('gap_fade_bull')

    # 2. RSI momentum (weight: 2)
    rsi = indicators.get('rsi', 50)
    if rsi > 60:
        bullish_score += 2
        signals.append('rsi_bull')
    elif rsi < 40:
        bearish_score += 2
        signals.append('rsi_bear')

    # 3. Trend alignment (weight: 1)
    close = yesterday['close']
    ema_20 = indicators.get('ema_20', close)
    ema_50 = indicators.get('ema_50', close)

    if close > ema_20 > ema_50:
        bullish_score += 1
        signals.append('trend_bull')
    elif close < ema_20 < ema_50:
        bearish_score += 1
        signals.append('trend_bear')

    # 4. ADX filter
    adx = indicators.get('adx', 20)
    if adx < 15:
        # Very weak trend - prefer RANGE
        return 'RANGE', {'score': 0, 'signals': signals, 'adx': adx}

    details = {
        'bullish_score': bullish_score,
        'bearish_score': bearish_score,
        'signals': signals,
        'gap_pct': round(gap_pct, 2),
        'rsi': round(rsi, 1),
        'adx': round(adx, 1)
    }

    # Decision
    if bullish_score >= 3 and bullish_score > bearish_score:
        return 'BULLISH', details
    elif bearish_score >= 3 and bearish_score > bullish_score:
        return 'BEARISH', details
    else:
        return 'RANGE', details


def strategy_composite_v2(
    today_open: float,
    yesterday: pd.Series,
    indicators: Dict[str, float],
    prev_indicators: Dict[str, float]
) -> Tuple[str, Dict]:
    """
    Composite Strategy V2 - Mean reversion focus with trend filter

    Key insight: We're looking for INTRADAY direction prediction
    - At open, if market is extended, expect reversion
    - If market is trending, follow the trend
    """
    gap_pct = (today_open - yesterday['close']) / yesterday['close'] * 100

    rsi = indicators.get('rsi', 50)
    prev_rsi = prev_indicators.get('rsi', 50)
    adx = indicators.get('adx', 20)
    bb_position = indicators.get('bb_position', 0.5)
    macd_hist = indicators.get('macd_hist', 0)
    prev_macd_hist = prev_indicators.get('macd_hist', 0)

    signals = []
    confidence = 0

    # MEAN REVERSION SIGNALS (higher weight in ranging markets)
    if adx < 25:  # Ranging market - mean reversion works better
        # RSI extreme reversal
        if rsi > 70 and rsi < prev_rsi:
            signals.append(('BEARISH', 3, 'rsi_overbought_reversal'))
            confidence += 3
        elif rsi < 30 and rsi > prev_rsi:
            signals.append(('BULLISH', 3, 'rsi_oversold_reversal'))
            confidence += 3

        # Bollinger band touch
        if bb_position > 0.95:
            signals.append(('BEARISH', 2, 'bb_upper_touch'))
        elif bb_position < 0.05:
            signals.append(('BULLISH', 2, 'bb_lower_touch'))

        # Gap fade (only in ranging market)
        if 0.2 < gap_pct < 0.8:
            signals.append(('BEARISH', 2, 'gap_fade'))
        elif -0.8 < gap_pct < -0.2:
            signals.append(('BULLISH', 2, 'gap_fade'))

    # TREND FOLLOWING SIGNALS (higher weight in trending markets)
    else:  # adx >= 25 - trending market
        # MACD momentum
        if macd_hist > 0 and macd_hist > prev_macd_hist:
            signals.append(('BULLISH', 2, 'macd_momentum'))
        elif macd_hist < 0 and macd_hist < prev_macd_hist:
            signals.append(('BEARISH', 2, 'macd_momentum'))

        # RSI momentum (not extreme)
        if 50 < rsi < 70:
            signals.append(('BULLISH', 1, 'rsi_momentum'))
        elif 30 < rsi < 50:
            signals.append(('BEARISH', 1, 'rsi_momentum'))

        # Large gap continuation (in trending market)
        if gap_pct > 0.5:
            signals.append(('BULLISH', 1, 'gap_continuation'))
        elif gap_pct < -0.5:
            signals.append(('BEARISH', 1, 'gap_continuation'))

    # Aggregate signals
    bullish_weight = sum(w for d, w, _ in signals if d == 'BULLISH')
    bearish_weight = sum(w for d, w, _ in signals if d == 'BEARISH')

    details = {
        'gap_pct': round(gap_pct, 2),
        'rsi': round(rsi, 1),
        'adx': round(adx, 1),
        'bb_position': round(bb_position, 2),
        'bullish_weight': bullish_weight,
        'bearish_weight': bearish_weight,
        'signals': [s[2] for s in signals],
        'market_type': 'trending' if adx >= 25 else 'ranging'
    }

    # Need at least 2 weight difference for confidence
    if bullish_weight >= 3 and bullish_weight - bearish_weight >= 2:
        return 'BULLISH', details
    elif bearish_weight >= 3 and bearish_weight - bullish_weight >= 2:
        return 'BEARISH', details
    else:
        return 'RANGE', details


def strategy_momentum_continuation(
    yesterday: pd.Series,
    day_before: pd.Series,
    indicators: Dict[str, float]
) -> Tuple[str, Dict]:
    """
    Momentum Continuation Strategy

    If yesterday was strong (>0.5% move) and momentum indicators confirm,
    expect continuation
    """
    yesterday_change = (yesterday['close'] - yesterday['open']) / yesterday['open'] * 100
    day_before_change = (day_before['close'] - day_before['open']) / day_before['open'] * 100

    rsi = indicators.get('rsi', 50)
    adx = indicators.get('adx', 20)

    details = {
        'yesterday_change': round(yesterday_change, 2),
        'day_before_change': round(day_before_change, 2),
        'rsi': round(rsi, 1),
        'adx': round(adx, 1)
    }

    # Two consecutive strong days in same direction
    if yesterday_change > 0.5 and day_before_change > 0.3 and rsi > 55 and adx > 20:
        return 'BULLISH', details
    elif yesterday_change < -0.5 and day_before_change < -0.3 and rsi < 45 and adx > 20:
        return 'BEARISH', details

    # Strong reversal day
    if yesterday_change > 0.8 and rsi > 65:
        return 'BULLISH', details
    elif yesterday_change < -0.8 and rsi < 35:
        return 'BEARISH', details

    return 'RANGE', details


# =============================================================================
# ANALYSIS ENGINE
# =============================================================================

def prepare_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate all indicators for the dataframe"""
    df = df.copy()

    # RSI
    df['rsi'] = calculate_rsi(df['close'], 14)

    # ADX
    df['adx'] = calculate_adx(df, 14)

    # ATR
    df['atr'] = calculate_atr(df, 14)

    # EMAs
    df['ema_20'] = calculate_ema(df['close'], 20)
    df['ema_50'] = calculate_ema(df['close'], 50)

    # Bollinger Bands
    df['bb_upper'], df['bb_middle'], df['bb_lower'] = calculate_bollinger_bands(df['close'], 20, 2.0)
    df['bb_position'] = (df['close'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower'])

    # MACD
    df['macd_line'], df['macd_signal'], df['macd_hist'] = calculate_macd(df['close'])

    # Stochastic
    df['stoch_k'], df['stoch_d'] = calculate_stochastic(df)

    return df


def run_strategy_backtest(df: pd.DataFrame, strategy_name: str, strategy_func) -> pd.DataFrame:
    """Run backtest for a single strategy"""
    results = []

    # Need enough history for indicators
    start_idx = 50

    for i in range(start_idx, len(df)):
        today = df.iloc[i]
        yesterday = df.iloc[i-1]
        day_before = df.iloc[i-2]
        last_3_days = df.iloc[i-3:i]

        # Get indicators
        indicators = {
            'rsi': yesterday['rsi'] if pd.notna(yesterday['rsi']) else 50,
            'adx': yesterday['adx'] if pd.notna(yesterday['adx']) else 20,
            'atr': yesterday['atr'] if pd.notna(yesterday['atr']) else 100,
            'ema_20': yesterday['ema_20'] if pd.notna(yesterday['ema_20']) else yesterday['close'],
            'ema_50': yesterday['ema_50'] if pd.notna(yesterday['ema_50']) else yesterday['close'],
            'bb_upper': yesterday['bb_upper'] if pd.notna(yesterday['bb_upper']) else yesterday['close'] * 1.02,
            'bb_middle': yesterday['bb_middle'] if pd.notna(yesterday['bb_middle']) else yesterday['close'],
            'bb_lower': yesterday['bb_lower'] if pd.notna(yesterday['bb_lower']) else yesterday['close'] * 0.98,
            'bb_position': yesterday['bb_position'] if pd.notna(yesterday['bb_position']) else 0.5,
            'macd_line': yesterday['macd_line'] if pd.notna(yesterday['macd_line']) else 0,
            'macd_hist': yesterday['macd_hist'] if pd.notna(yesterday['macd_hist']) else 0,
            'stoch_k': yesterday['stoch_k'] if pd.notna(yesterday['stoch_k']) else 50,
        }

        prev_indicators = {
            'rsi': day_before['rsi'] if pd.notna(day_before['rsi']) else 50,
            'macd_hist': day_before['macd_hist'] if pd.notna(day_before['macd_hist']) else 0,
        }

        # Run strategy based on type
        if strategy_name == 'original':
            regime, details = strategy_original(today['open'], yesterday['close'], last_3_days)
        elif strategy_name == 'gap_fade':
            regime, details = strategy_gap_fade(
                today['open'], yesterday['close'], yesterday['low'], yesterday['high'],
                indicators['atr']
            )
        elif strategy_name == 'rsi_extreme':
            regime, details = strategy_rsi_extreme(indicators['rsi'], prev_indicators['rsi'])
        elif strategy_name == 'trend_following':
            regime, details = strategy_trend_following(
                yesterday['close'], indicators['ema_20'], indicators['ema_50'], indicators['adx']
            )
        elif strategy_name == 'bollinger_reversion':
            regime, details = strategy_bollinger_reversion(
                yesterday['close'], indicators['bb_upper'], indicators['bb_middle'],
                indicators['bb_lower'], indicators['rsi']
            )
        elif strategy_name == 'macd_momentum':
            regime, details = strategy_macd_momentum(
                indicators['macd_hist'], prev_indicators['macd_hist'], indicators['macd_line']
            )
        elif strategy_name == 'composite_v1':
            regime, details = strategy_composite_v1(today['open'], yesterday, indicators)
        elif strategy_name == 'composite_v2':
            regime, details = strategy_composite_v2(today['open'], yesterday, indicators, prev_indicators)
        elif strategy_name == 'momentum_continuation':
            regime, details = strategy_momentum_continuation(yesterday, day_before, indicators)
        else:
            continue

        # Calculate actual outcome
        day_change_pct = (today['close'] - today['open']) / today['open'] * 100

        if day_change_pct > 0.3:
            actual = 'UP'
        elif day_change_pct < -0.3:
            actual = 'DOWN'
        else:
            actual = 'FLAT'

        # Check correctness
        if regime == 'BEARISH':
            correct = actual == 'DOWN'
            trade_pnl = -day_change_pct * 2.5 if actual != 'DOWN' else abs(day_change_pct) * 2.5
        elif regime == 'BULLISH':
            correct = actual == 'UP'
            trade_pnl = day_change_pct * 2.5 if actual == 'UP' else -abs(day_change_pct) * 2.5
        else:
            correct = actual == 'FLAT'
            trade_pnl = 0

        results.append({
            'date': today['date'],
            'open': today['open'],
            'close': today['close'],
            'day_change_pct': round(day_change_pct, 2),
            'predicted': regime,
            'actual': actual,
            'correct': correct,
            'trade_pnl': round(trade_pnl, 2) if regime != 'RANGE' else None,
            **{f'ind_{k}': round(v, 2) if isinstance(v, float) else v for k, v in indicators.items()}
        })

    return pd.DataFrame(results)


def calculate_strategy_metrics(results_df: pd.DataFrame, strategy_name: str) -> Dict:
    """Calculate performance metrics for a strategy"""
    total = len(results_df)

    # Prediction accuracy
    correct = results_df['correct'].sum()
    accuracy = correct / total * 100

    # Trade metrics (excluding RANGE)
    trades_df = results_df[results_df['predicted'] != 'RANGE'].copy()
    num_trades = len(trades_df)

    if num_trades > 0:
        wins = trades_df[trades_df['trade_pnl'] > 0]
        win_rate = len(wins) / num_trades * 100
        total_pnl = trades_df['trade_pnl'].sum()
        avg_pnl = trades_df['trade_pnl'].mean()

        # By direction
        bullish_trades = trades_df[trades_df['predicted'] == 'BULLISH']
        bearish_trades = trades_df[trades_df['predicted'] == 'BEARISH']

        bullish_wr = len(bullish_trades[bullish_trades['trade_pnl'] > 0]) / len(bullish_trades) * 100 if len(bullish_trades) > 0 else 0
        bearish_wr = len(bearish_trades[bearish_trades['trade_pnl'] > 0]) / len(bearish_trades) * 100 if len(bearish_trades) > 0 else 0
    else:
        win_rate = 0
        total_pnl = 0
        avg_pnl = 0
        bullish_wr = 0
        bearish_wr = 0

    # Regime distribution
    regime_counts = results_df['predicted'].value_counts()

    return {
        'strategy': strategy_name,
        'total_days': total,
        'accuracy': round(accuracy, 1),
        'num_trades': num_trades,
        'trade_pct': round(num_trades / total * 100, 1),
        'win_rate': round(win_rate, 1),
        'total_pnl': round(total_pnl, 1),
        'avg_pnl': round(avg_pnl, 2),
        'bullish_trades': regime_counts.get('BULLISH', 0),
        'bearish_trades': regime_counts.get('BEARISH', 0),
        'range_trades': regime_counts.get('RANGE', 0),
        'bullish_wr': round(bullish_wr, 1),
        'bearish_wr': round(bearish_wr, 1),
    }


def main():
    """Main analysis"""
    print("="*80)
    print("IMPROVED REGIME DETECTION - STRATEGY COMPARISON")
    print("="*80)

    # Load data
    data_file = os.path.join(OUTPUT_DIR, 'nifty_daily_5years.csv')

    if os.path.exists(data_file):
        print(f"\nLoading data from: {data_file}")
        df = pd.read_csv(data_file)
        df['date'] = pd.to_datetime(df['date'])
    else:
        print("\nDownloading data...")
        kite = get_kite_client()
        from regime_prediction_analysis import download_nifty_daily_data
        df = download_nifty_daily_data(kite, years=5)

    print(f"Data: {len(df)} days from {df['date'].min().date()} to {df['date'].max().date()}")

    # Prepare indicators
    print("\nCalculating technical indicators...")
    df = prepare_indicators(df)

    # Strategies to test
    strategies = [
        'original',
        'gap_fade',
        'rsi_extreme',
        'trend_following',
        'bollinger_reversion',
        'macd_momentum',
        'composite_v1',
        'composite_v2',
        'momentum_continuation'
    ]

    # Run backtests
    print("\nRunning strategy backtests...")
    all_metrics = []

    for strategy in strategies:
        print(f"  Testing: {strategy}")
        results = run_strategy_backtest(df, strategy, None)
        metrics = calculate_strategy_metrics(results, strategy)
        all_metrics.append(metrics)

        # Save detailed results for best strategies
        if strategy in ['composite_v2', 'original']:
            results.to_csv(os.path.join(OUTPUT_DIR, f'regime_results_{strategy}.csv'), index=False)

    # Create comparison table
    metrics_df = pd.DataFrame(all_metrics)
    metrics_df = metrics_df.sort_values('win_rate', ascending=False)

    print("\n" + "="*80)
    print("STRATEGY COMPARISON - SORTED BY WIN RATE")
    print("="*80)

    print(f"\n{'Strategy':<22} | {'Trades':>6} | {'WR':>6} | {'Avg P&L':>8} | {'Total P&L':>10} | {'Bull WR':>7} | {'Bear WR':>7}")
    print("-"*80)

    for _, row in metrics_df.iterrows():
        print(f"{row['strategy']:<22} | {row['num_trades']:>6} | {row['win_rate']:>5.1f}% | {row['avg_pnl']:>+7.2f}% | {row['total_pnl']:>+9.1f}% | {row['bullish_wr']:>6.1f}% | {row['bearish_wr']:>6.1f}%")

    # Best strategy analysis
    best = metrics_df.iloc[0]
    print(f"\n" + "="*80)
    print(f"BEST STRATEGY: {best['strategy']}")
    print("="*80)
    print(f"  Win Rate: {best['win_rate']}%")
    print(f"  Avg P&L/Trade: {best['avg_pnl']:+.2f}%")
    print(f"  Total P&L: {best['total_pnl']:+.1f}%")
    print(f"  Trades: {best['num_trades']} ({best['trade_pct']}% of days)")
    print(f"  BULLISH trades: {best['bullish_trades']} ({best['bullish_wr']}% WR)")
    print(f"  BEARISH trades: {best['bearish_trades']} ({best['bearish_wr']}% WR)")

    # Original vs Best comparison
    print(f"\n" + "-"*80)
    print("ORIGINAL vs BEST STRATEGY")
    print("-"*80)

    orig = metrics_df[metrics_df['strategy'] == 'original'].iloc[0]
    print(f"\n  Original:")
    print(f"    Win Rate: {orig['win_rate']}%")
    print(f"    Avg P&L: {orig['avg_pnl']:+.2f}%")
    print(f"    Total P&L: {orig['total_pnl']:+.1f}%")

    print(f"\n  {best['strategy']}:")
    print(f"    Win Rate: {best['win_rate']}% ({best['win_rate'] - orig['win_rate']:+.1f}% improvement)")
    print(f"    Avg P&L: {best['avg_pnl']:+.2f}% ({best['avg_pnl'] - orig['avg_pnl']:+.2f}% improvement)")
    print(f"    Total P&L: {best['total_pnl']:+.1f}% ({best['total_pnl'] - orig['total_pnl']:+.1f}% improvement)")

    # Save summary
    metrics_df.to_csv(os.path.join(OUTPUT_DIR, 'strategy_comparison.csv'), index=False)
    print(f"\nResults saved to: strategy_comparison.csv")

    # Yearly breakdown for best strategy
    print(f"\n" + "="*80)
    print(f"YEARLY BREAKDOWN - {best['strategy']}")
    print("="*80)

    best_results = pd.read_csv(os.path.join(OUTPUT_DIR, f'regime_results_{best["strategy"]}.csv'))
    best_results['date'] = pd.to_datetime(best_results['date'])
    best_results['year'] = best_results['date'].dt.year

    print(f"\n{'Year':>6} | {'Trades':>6} | {'WR':>6} | {'Avg P&L':>8} | {'Total P&L':>10}")
    print("-"*50)

    for year in sorted(best_results['year'].unique()):
        year_df = best_results[best_results['year'] == year]
        trades = year_df[year_df['predicted'] != 'RANGE']
        if len(trades) > 0:
            wr = len(trades[trades['trade_pnl'] > 0]) / len(trades) * 100
            avg = trades['trade_pnl'].mean()
            total = trades['trade_pnl'].sum()
            print(f"{year:>6} | {len(trades):>6} | {wr:>5.1f}% | {avg:>+7.2f}% | {total:>+9.1f}%")

    print("\n" + "="*80)
    print("ANALYSIS COMPLETE")
    print("="*80)


if __name__ == '__main__':
    main()
