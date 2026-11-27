"""
MULTI-TIMEFRAME REGIME DETECTION

Combines:
1. Daily signals (entry timing)
2. Weekly trend (bias direction)
3. Monthly trend (major direction)

Research shows:
- Trade in direction of higher timeframe trend
- Use lower timeframe for entry

Weekly Analysis:
- Previous week's candle pattern
- Weekly RSI/trend

Monthly Analysis:
- Monthly trend direction
- Monthly RSI for major overbought/oversold
"""

import json
import os
from datetime import datetime, timedelta
from typing import Dict, Any, Tuple
import pandas as pd
import numpy as np

# Token file path
TOKEN_FILE = r'C:\Users\mail2\Documents\Projects\BOTS\data\kite_access_token.json'
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
NIFTY_TOKEN = 256265


def load_kite_token() -> Dict[str, Any]:
    with open(TOKEN_FILE, 'r') as f:
        return json.load(f)


def get_kite_client():
    from kiteconnect import KiteConnect
    token_data = load_kite_token()
    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


# =============================================================================
# INDICATOR CALCULATIONS
# =============================================================================

def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def calculate_ema(prices: pd.Series, period: int) -> pd.Series:
    return prices.ewm(span=period, adjust=False).mean()


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df['high']
    low = df['low']
    close = df['close']
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
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
    return dx.rolling(window=period).mean()


# =============================================================================
# TIMEFRAME RESAMPLING
# =============================================================================

def resample_to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Resample daily data to weekly OHLC"""
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])
    df.set_index('date', inplace=True)

    weekly = df.resample('W-FRI').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna()

    weekly.reset_index(inplace=True)
    return weekly


def resample_to_monthly(df: pd.DataFrame) -> pd.DataFrame:
    """Resample daily data to monthly OHLC"""
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])
    df.set_index('date', inplace=True)

    monthly = df.resample('M').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna()

    monthly.reset_index(inplace=True)
    return monthly


def prepare_timeframe_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add indicators to any timeframe"""
    df = df.copy()
    df['rsi'] = calculate_rsi(df['close'], 14)
    df['ema_10'] = calculate_ema(df['close'], 10)
    df['ema_20'] = calculate_ema(df['close'], 20)
    df['atr'] = calculate_atr(df, 14)
    df['adx'] = calculate_adx(df, 14)

    # Candle patterns
    df['body_pct'] = (df['close'] - df['open']) / df['open'] * 100
    df['is_green'] = df['close'] > df['open']

    # Trend
    df['trend'] = 'NEUTRAL'
    df.loc[(df['close'] > df['ema_10']) & (df['ema_10'] > df['ema_20']), 'trend'] = 'BULLISH'
    df.loc[(df['close'] < df['ema_10']) & (df['ema_10'] < df['ema_20']), 'trend'] = 'BEARISH'

    return df


# =============================================================================
# WEEKLY ANALYSIS
# =============================================================================

def get_weekly_bias(weekly_df: pd.DataFrame, current_date: pd.Timestamp) -> Dict[str, Any]:
    """
    Get weekly bias for a given date (using ONLY past data)

    Returns bias based on:
    - Previous week's close vs open (momentum)
    - Weekly RSI
    - Weekly trend (EMA alignment)
    """
    # Get weeks before current date
    past_weeks = weekly_df[weekly_df['date'] < current_date].copy()

    if len(past_weeks) < 3:
        return {'bias': 'NEUTRAL', 'confidence': 0}

    last_week = past_weeks.iloc[-1]
    prev_week = past_weeks.iloc[-2]

    signals = []

    # Previous week's candle
    if last_week['body_pct'] > 0.5:
        signals.append(('BULLISH', 1, 'weekly_green'))
    elif last_week['body_pct'] < -0.5:
        signals.append(('BEARISH', 1, 'weekly_red'))

    # Weekly RSI
    rsi = last_week['rsi'] if pd.notna(last_week['rsi']) else 50
    if rsi > 60:
        signals.append(('BULLISH', 1, 'weekly_rsi_high'))
    elif rsi < 40:
        signals.append(('BEARISH', 1, 'weekly_rsi_low'))

    # Weekly trend
    if last_week['trend'] == 'BULLISH':
        signals.append(('BULLISH', 2, 'weekly_trend_up'))
    elif last_week['trend'] == 'BEARISH':
        signals.append(('BEARISH', 2, 'weekly_trend_down'))

    # Two consecutive weeks same direction
    if last_week['is_green'] and prev_week['is_green']:
        signals.append(('BULLISH', 1, 'weekly_momentum'))
    elif not last_week['is_green'] and not prev_week['is_green']:
        signals.append(('BEARISH', 1, 'weekly_momentum'))

    # Aggregate
    bullish = sum(w for d, w, _ in signals if d == 'BULLISH')
    bearish = sum(w for d, w, _ in signals if d == 'BEARISH')

    if bullish >= 3 and bullish > bearish:
        return {'bias': 'BULLISH', 'confidence': bullish, 'rsi': round(rsi, 1), 'signals': [s[2] for s in signals]}
    elif bearish >= 3 and bearish > bullish:
        return {'bias': 'BEARISH', 'confidence': bearish, 'rsi': round(rsi, 1), 'signals': [s[2] for s in signals]}
    else:
        return {'bias': 'NEUTRAL', 'confidence': 0, 'rsi': round(rsi, 1), 'signals': [s[2] for s in signals]}


# =============================================================================
# MONTHLY ANALYSIS
# =============================================================================

def get_monthly_bias(monthly_df: pd.DataFrame, current_date: pd.Timestamp) -> Dict[str, Any]:
    """
    Get monthly bias for a given date (using ONLY past data)

    Monthly trend is the MAJOR direction filter
    """
    past_months = monthly_df[monthly_df['date'] < current_date].copy()

    if len(past_months) < 3:
        return {'bias': 'NEUTRAL', 'confidence': 0}

    last_month = past_months.iloc[-1]
    prev_month = past_months.iloc[-2]

    signals = []

    # Last month's candle
    if last_month['body_pct'] > 1.0:
        signals.append(('BULLISH', 2, 'monthly_strong_green'))
    elif last_month['body_pct'] > 0:
        signals.append(('BULLISH', 1, 'monthly_green'))
    elif last_month['body_pct'] < -1.0:
        signals.append(('BEARISH', 2, 'monthly_strong_red'))
    elif last_month['body_pct'] < 0:
        signals.append(('BEARISH', 1, 'monthly_red'))

    # Monthly RSI
    rsi = last_month['rsi'] if pd.notna(last_month['rsi']) else 50
    if rsi > 70:
        signals.append(('BEARISH', 1, 'monthly_overbought'))  # Mean reversion
    elif rsi < 30:
        signals.append(('BULLISH', 1, 'monthly_oversold'))  # Mean reversion
    elif rsi > 55:
        signals.append(('BULLISH', 1, 'monthly_rsi_strong'))
    elif rsi < 45:
        signals.append(('BEARISH', 1, 'monthly_rsi_weak'))

    # Monthly trend
    if last_month['trend'] == 'BULLISH':
        signals.append(('BULLISH', 2, 'monthly_trend_up'))
    elif last_month['trend'] == 'BEARISH':
        signals.append(('BEARISH', 2, 'monthly_trend_down'))

    # Aggregate
    bullish = sum(w for d, w, _ in signals if d == 'BULLISH')
    bearish = sum(w for d, w, _ in signals if d == 'BEARISH')

    if bullish >= 3 and bullish > bearish:
        return {'bias': 'BULLISH', 'confidence': bullish, 'rsi': round(rsi, 1)}
    elif bearish >= 3 and bearish > bullish:
        return {'bias': 'BEARISH', 'confidence': bearish, 'rsi': round(rsi, 1)}
    else:
        return {'bias': 'NEUTRAL', 'confidence': 0, 'rsi': round(rsi, 1)}


# =============================================================================
# DAILY REGIME DETECTION
# =============================================================================

def get_daily_signal(
    today_open: float,
    yesterday: pd.Series,
    day_before: pd.Series,
    atr: float
) -> Dict[str, Any]:
    """
    Daily entry signal - Gap fade with confirmation
    """
    gap_pct = (today_open - yesterday['close']) / yesterday['close'] * 100

    # Gap fade logic
    if 0.15 < abs(gap_pct) < 1.0:
        if gap_pct > 0.15:
            direction = 'BEARISH'
        else:
            direction = 'BULLISH'
    else:
        direction = 'NEUTRAL'

    # Yesterday's candle confirmation
    yesterday_body = (yesterday['close'] - yesterday['open']) / yesterday['open'] * 100

    return {
        'signal': direction,
        'gap_pct': round(gap_pct, 2),
        'yesterday_body': round(yesterday_body, 2)
    }


# =============================================================================
# MULTI-TIMEFRAME COMBINATION
# =============================================================================

def combine_timeframes(
    daily_signal: Dict,
    weekly_bias: Dict,
    monthly_bias: Dict
) -> Tuple[str, Dict]:
    """
    Combine signals from all timeframes

    Rules:
    1. Monthly trend is the MAJOR filter (don't fight it)
    2. Weekly trend confirms direction
    3. Daily signal provides entry

    Best setup:
    - Monthly BULLISH + Weekly BULLISH + Daily gap down -> Strong BULLISH (fade the gap)
    - Monthly BEARISH + Weekly BEARISH + Daily gap up -> Strong BEARISH (fade the gap)
    """
    details = {
        'daily': daily_signal,
        'weekly': weekly_bias,
        'monthly': monthly_bias
    }

    monthly = monthly_bias.get('bias', 'NEUTRAL')
    weekly = weekly_bias.get('bias', 'NEUTRAL')
    daily = daily_signal.get('signal', 'NEUTRAL')

    # Scoring system
    bullish_score = 0
    bearish_score = 0

    # Monthly (weight: 3)
    if monthly == 'BULLISH':
        bullish_score += 3
    elif monthly == 'BEARISH':
        bearish_score += 3

    # Weekly (weight: 2)
    if weekly == 'BULLISH':
        bullish_score += 2
    elif weekly == 'BEARISH':
        bearish_score += 2

    # Daily gap fade (weight: 2)
    if daily == 'BULLISH':  # Gap down -> fade up
        bullish_score += 2
    elif daily == 'BEARISH':  # Gap up -> fade down
        bearish_score += 2

    details['bullish_score'] = bullish_score
    details['bearish_score'] = bearish_score

    # Decision with alignment check
    if bullish_score >= 5 and bullish_score > bearish_score + 2:
        # Strong bullish alignment
        return 'BULLISH', details
    elif bearish_score >= 5 and bearish_score > bullish_score + 2:
        # Strong bearish alignment
        return 'BEARISH', details
    elif bullish_score >= 4 and monthly != 'BEARISH':
        return 'BULLISH', details
    elif bearish_score >= 4 and monthly != 'BULLISH':
        return 'BEARISH', details
    else:
        return 'RANGE', details


def combine_timeframes_v2(
    daily_signal: Dict,
    weekly_bias: Dict,
    monthly_bias: Dict
) -> Tuple[str, Dict]:
    """
    V2: More aggressive - trade with higher timeframe, use daily for entry only
    """
    details = {
        'daily': daily_signal,
        'weekly': weekly_bias,
        'monthly': monthly_bias
    }

    monthly = monthly_bias.get('bias', 'NEUTRAL')
    weekly = weekly_bias.get('bias', 'NEUTRAL')
    daily = daily_signal.get('signal', 'NEUTRAL')
    gap_pct = daily_signal.get('gap_pct', 0)

    # Rule 1: Strong monthly trend - follow it
    if monthly in ['BULLISH', 'BEARISH']:
        # Only trade in monthly direction
        if monthly == 'BULLISH' and gap_pct < -0.1:  # Gap down in bullish market
            return 'BULLISH', details
        elif monthly == 'BEARISH' and gap_pct > 0.1:  # Gap up in bearish market
            return 'BEARISH', details

    # Rule 2: Weekly trend with gap fade
    if weekly in ['BULLISH', 'BEARISH'] and monthly == 'NEUTRAL':
        if weekly == 'BULLISH' and gap_pct < -0.1:
            return 'BULLISH', details
        elif weekly == 'BEARISH' and gap_pct > 0.1:
            return 'BEARISH', details

    # Rule 3: Alignment of all timeframes
    if monthly == 'BULLISH' and weekly == 'BULLISH':
        return 'BULLISH', details
    elif monthly == 'BEARISH' and weekly == 'BEARISH':
        return 'BEARISH', details

    return 'RANGE', details


def combine_timeframes_v3(
    daily_signal: Dict,
    weekly_bias: Dict,
    monthly_bias: Dict
) -> Tuple[str, Dict]:
    """
    V3: Conservative - only trade when all align + gap fade opportunity
    """
    details = {
        'daily': daily_signal,
        'weekly': weekly_bias,
        'monthly': monthly_bias
    }

    monthly = monthly_bias.get('bias', 'NEUTRAL')
    weekly = weekly_bias.get('bias', 'NEUTRAL')
    gap_pct = daily_signal.get('gap_pct', 0)

    # Only trade when monthly and weekly align
    if monthly == 'BULLISH' and weekly == 'BULLISH':
        # In bullish trend, buy gaps down
        if gap_pct < -0.2:
            return 'BULLISH', details
    elif monthly == 'BEARISH' and weekly == 'BEARISH':
        # In bearish trend, sell gaps up
        if gap_pct > 0.2:
            return 'BEARISH', details

    return 'RANGE', details


# =============================================================================
# BACKTEST ENGINE
# =============================================================================

def run_multi_timeframe_backtest(
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    strategy_name: str
) -> pd.DataFrame:
    """Run multi-timeframe strategy backtest"""
    results = []

    # Prepare indicators
    daily_df = prepare_timeframe_indicators(daily_df.copy())
    weekly_df = prepare_timeframe_indicators(weekly_df.copy())
    monthly_df = prepare_timeframe_indicators(monthly_df.copy())

    # Start after enough data
    start_idx = 60

    for i in range(start_idx, len(daily_df)):
        today = daily_df.iloc[i]
        yesterday = daily_df.iloc[i-1]
        day_before = daily_df.iloc[i-2]

        current_date = pd.to_datetime(today['date'])

        # Get higher timeframe biases
        weekly_bias = get_weekly_bias(weekly_df, current_date)
        monthly_bias = get_monthly_bias(monthly_df, current_date)

        # Get daily signal
        atr = yesterday['atr'] if pd.notna(yesterday['atr']) else 100
        daily_signal = get_daily_signal(today['open'], yesterday, day_before, atr)

        # Combine timeframes
        if strategy_name == 'mtf_v1':
            regime, details = combine_timeframes(daily_signal, weekly_bias, monthly_bias)
        elif strategy_name == 'mtf_v2':
            regime, details = combine_timeframes_v2(daily_signal, weekly_bias, monthly_bias)
        elif strategy_name == 'mtf_v3':
            regime, details = combine_timeframes_v3(daily_signal, weekly_bias, monthly_bias)
        elif strategy_name == 'weekly_only':
            # Use only weekly bias
            bias = weekly_bias.get('bias', 'NEUTRAL')
            if bias == 'BULLISH':
                regime = 'BULLISH'
            elif bias == 'BEARISH':
                regime = 'BEARISH'
            else:
                regime = 'RANGE'
            details = weekly_bias
        elif strategy_name == 'monthly_only':
            # Use only monthly bias
            bias = monthly_bias.get('bias', 'NEUTRAL')
            if bias == 'BULLISH':
                regime = 'BULLISH'
            elif bias == 'BEARISH':
                regime = 'BEARISH'
            else:
                regime = 'RANGE'
            details = monthly_bias
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

        # Trade P&L
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
            'day_change_pct': round(day_change_pct, 2),
            'predicted': regime,
            'actual': actual,
            'correct': correct,
            'trade_pnl': round(trade_pnl, 2) if regime != 'RANGE' else None,
            'weekly_bias': weekly_bias.get('bias'),
            'monthly_bias': monthly_bias.get('bias'),
            'gap_pct': daily_signal.get('gap_pct')
        })

    return pd.DataFrame(results)


def calculate_metrics(results_df: pd.DataFrame, strategy_name: str) -> Dict:
    """Calculate strategy metrics"""
    total = len(results_df)

    trades_df = results_df[results_df['predicted'] != 'RANGE'].copy()
    num_trades = len(trades_df)

    if num_trades > 0:
        wins = trades_df[trades_df['trade_pnl'] > 0]
        win_rate = len(wins) / num_trades * 100
        total_pnl = trades_df['trade_pnl'].sum()
        avg_pnl = trades_df['trade_pnl'].mean()

        bullish_trades = trades_df[trades_df['predicted'] == 'BULLISH']
        bearish_trades = trades_df[trades_df['predicted'] == 'BEARISH']

        bullish_wr = len(bullish_trades[bullish_trades['trade_pnl'] > 0]) / len(bullish_trades) * 100 if len(bullish_trades) > 0 else 0
        bearish_wr = len(bearish_trades[bearish_trades['trade_pnl'] > 0]) / len(bearish_trades) * 100 if len(bearish_trades) > 0 else 0

        bullish_pnl = bullish_trades['trade_pnl'].sum() if len(bullish_trades) > 0 else 0
        bearish_pnl = bearish_trades['trade_pnl'].sum() if len(bearish_trades) > 0 else 0
    else:
        win_rate = total_pnl = avg_pnl = bullish_wr = bearish_wr = bullish_pnl = bearish_pnl = 0

    regime_counts = results_df['predicted'].value_counts()

    return {
        'strategy': strategy_name,
        'total_days': total,
        'num_trades': num_trades,
        'trade_pct': round(num_trades / total * 100, 1),
        'win_rate': round(win_rate, 1),
        'total_pnl': round(total_pnl, 1),
        'avg_pnl': round(avg_pnl, 2),
        'bullish_trades': regime_counts.get('BULLISH', 0),
        'bearish_trades': regime_counts.get('BEARISH', 0),
        'range_days': regime_counts.get('RANGE', 0),
        'bullish_wr': round(bullish_wr, 1),
        'bearish_wr': round(bearish_wr, 1),
        'bullish_pnl': round(bullish_pnl, 1),
        'bearish_pnl': round(bearish_pnl, 1),
    }


def main():
    """Main analysis"""
    print("="*80)
    print("MULTI-TIMEFRAME REGIME DETECTION")
    print("Daily + Weekly + Monthly Analysis")
    print("="*80)

    # Load daily data
    data_file = os.path.join(OUTPUT_DIR, 'nifty_daily_5years.csv')

    if os.path.exists(data_file):
        print(f"\nLoading data from: {data_file}")
        daily_df = pd.read_csv(data_file)
        daily_df['date'] = pd.to_datetime(daily_df['date'])
    else:
        print("\nDownloading data...")
        kite = get_kite_client()
        import time
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=5 * 365 + 30)

        all_data = []
        current_start = start_date
        while current_start < end_date:
            current_end = min(current_start + timedelta(days=365), end_date)
            try:
                data = kite.historical_data(
                    instrument_token=NIFTY_TOKEN,
                    from_date=current_start.strftime('%Y-%m-%d'),
                    to_date=current_end.strftime('%Y-%m-%d'),
                    interval='day'
                )
                if data:
                    all_data.extend(data)
            except Exception as e:
                print(f"  Error: {e}")
            current_start = current_end + timedelta(days=1)
            time.sleep(0.5)

        daily_df = pd.DataFrame(all_data)
        daily_df['date'] = pd.to_datetime(daily_df['date']).dt.tz_localize(None)
        daily_df = daily_df.sort_values('date').reset_index(drop=True)
        daily_df.to_csv(data_file, index=False)

    print(f"Daily data: {len(daily_df)} candles")

    # Create weekly and monthly data
    print("Resampling to weekly and monthly...")
    weekly_df = resample_to_weekly(daily_df)
    monthly_df = resample_to_monthly(daily_df)

    print(f"Weekly data: {len(weekly_df)} candles")
    print(f"Monthly data: {len(monthly_df)} candles")

    # Save resampled data
    weekly_df.to_csv(os.path.join(OUTPUT_DIR, 'nifty_weekly_5years.csv'), index=False)
    monthly_df.to_csv(os.path.join(OUTPUT_DIR, 'nifty_monthly_5years.csv'), index=False)

    # Strategies to test
    strategies = [
        'weekly_only',
        'monthly_only',
        'mtf_v1',
        'mtf_v2',
        'mtf_v3'
    ]

    print("\nRunning multi-timeframe backtests...")
    all_metrics = []

    for strategy in strategies:
        print(f"  Testing: {strategy}")
        results = run_multi_timeframe_backtest(daily_df, weekly_df, monthly_df, strategy)
        metrics = calculate_metrics(results, strategy)
        all_metrics.append(metrics)

        # Save detailed results
        results.to_csv(os.path.join(OUTPUT_DIR, f'mtf_results_{strategy}.csv'), index=False)

    # Create comparison table
    metrics_df = pd.DataFrame(all_metrics)
    metrics_df = metrics_df.sort_values('win_rate', ascending=False)

    print("\n" + "="*80)
    print("MULTI-TIMEFRAME STRATEGY COMPARISON")
    print("="*80)

    print(f"\n{'Strategy':<15} | {'Trades':>6} | {'WR':>6} | {'Avg P&L':>8} | {'Total P&L':>10} | {'Bull WR':>7} | {'Bear WR':>7}")
    print("-"*75)

    for _, row in metrics_df.iterrows():
        print(f"{row['strategy']:<15} | {row['num_trades']:>6} | {row['win_rate']:>5.1f}% | {row['avg_pnl']:>+7.2f}% | {row['total_pnl']:>+9.1f}% | {row['bullish_wr']:>6.1f}% | {row['bearish_wr']:>6.1f}%")

    # Best strategy
    best = metrics_df.iloc[0]
    print(f"\n" + "="*80)
    print(f"BEST MULTI-TIMEFRAME STRATEGY: {best['strategy']}")
    print("="*80)
    print(f"  Win Rate: {best['win_rate']}%")
    print(f"  Avg P&L/Trade: {best['avg_pnl']:+.2f}%")
    print(f"  Total P&L: {best['total_pnl']:+.1f}%")
    print(f"  Trades: {best['num_trades']} ({best['trade_pct']}% of days)")
    print(f"  BULLISH: {best['bullish_trades']} trades ({best['bullish_wr']}% WR) -> P&L: {best['bullish_pnl']:+.1f}%")
    print(f"  BEARISH: {best['bearish_trades']} trades ({best['bearish_wr']}% WR) -> P&L: {best['bearish_pnl']:+.1f}%")

    # Compare with gap_fade (best daily-only strategy)
    print(f"\n" + "-"*80)
    print("COMPARISON WITH DAILY-ONLY STRATEGIES")
    print("-"*80)

    # Load gap_fade results
    gap_fade_file = os.path.join(OUTPUT_DIR, 'strategy_comparison.csv')
    if os.path.exists(gap_fade_file):
        daily_strategies = pd.read_csv(gap_fade_file)
        gap_fade = daily_strategies[daily_strategies['strategy'] == 'gap_fade'].iloc[0]
        print(f"\n  Gap Fade (Daily only):")
        print(f"    Win Rate: {gap_fade['win_rate']}%")
        print(f"    Total P&L: {gap_fade['total_pnl']:+.1f}%")

        print(f"\n  {best['strategy']} (Multi-TF):")
        print(f"    Win Rate: {best['win_rate']}% ({best['win_rate'] - gap_fade['win_rate']:+.1f}%)")
        print(f"    Total P&L: {best['total_pnl']:+.1f}% ({best['total_pnl'] - gap_fade['total_pnl']:+.1f}%)")

    # Yearly breakdown
    print(f"\n" + "="*80)
    print(f"YEARLY BREAKDOWN - {best['strategy']}")
    print("="*80)

    best_results = pd.read_csv(os.path.join(OUTPUT_DIR, f'mtf_results_{best["strategy"]}.csv'))
    best_results['date'] = pd.to_datetime(best_results['date'])
    best_results['year'] = best_results['date'].dt.year

    print(f"\n{'Year':>6} | {'Trades':>6} | {'WR':>6} | {'Avg P&L':>8} | {'Total P&L':>10} | {'Bull P&L':>9} | {'Bear P&L':>9}")
    print("-"*75)

    for year in sorted(best_results['year'].unique()):
        year_df = best_results[best_results['year'] == year]
        trades = year_df[year_df['predicted'] != 'RANGE']
        if len(trades) > 0:
            wr = len(trades[trades['trade_pnl'] > 0]) / len(trades) * 100
            avg = trades['trade_pnl'].mean()
            total = trades['trade_pnl'].sum()

            bull = trades[trades['predicted'] == 'BULLISH']
            bear = trades[trades['predicted'] == 'BEARISH']
            bull_pnl = bull['trade_pnl'].sum() if len(bull) > 0 else 0
            bear_pnl = bear['trade_pnl'].sum() if len(bear) > 0 else 0

            print(f"{year:>6} | {len(trades):>6} | {wr:>5.1f}% | {avg:>+7.2f}% | {total:>+9.1f}% | {bull_pnl:>+8.1f}% | {bear_pnl:>+8.1f}%")

    # Save summary
    metrics_df.to_csv(os.path.join(OUTPUT_DIR, 'mtf_strategy_comparison.csv'), index=False)

    # Analyze weekly/monthly trend accuracy
    print(f"\n" + "="*80)
    print("WEEKLY & MONTHLY TREND ANALYSIS")
    print("="*80)

    # Weekly accuracy
    print("\nWEEKLY TREND vs DAILY OUTCOME:")
    weekly_results = pd.read_csv(os.path.join(OUTPUT_DIR, 'mtf_results_weekly_only.csv'))

    for bias in ['BULLISH', 'BEARISH']:
        subset = weekly_results[weekly_results['weekly_bias'] == bias]
        if len(subset) > 0:
            up_days = len(subset[subset['actual'] == 'UP'])
            down_days = len(subset[subset['actual'] == 'DOWN'])
            flat_days = len(subset[subset['actual'] == 'FLAT'])
            total = len(subset)
            print(f"  When Weekly is {bias}: {total} days")
            print(f"    UP: {up_days} ({up_days/total*100:.1f}%), DOWN: {down_days} ({down_days/total*100:.1f}%), FLAT: {flat_days} ({flat_days/total*100:.1f}%)")

    # Monthly accuracy
    print("\nMONTHLY TREND vs DAILY OUTCOME:")
    monthly_results = pd.read_csv(os.path.join(OUTPUT_DIR, 'mtf_results_monthly_only.csv'))

    for bias in ['BULLISH', 'BEARISH']:
        subset = monthly_results[monthly_results['monthly_bias'] == bias]
        if len(subset) > 0:
            up_days = len(subset[subset['actual'] == 'UP'])
            down_days = len(subset[subset['actual'] == 'DOWN'])
            flat_days = len(subset[subset['actual'] == 'FLAT'])
            total = len(subset)
            print(f"  When Monthly is {bias}: {total} days")
            print(f"    UP: {up_days} ({up_days/total*100:.1f}%), DOWN: {down_days} ({down_days/total*100:.1f}%), FLAT: {flat_days} ({flat_days/total*100:.1f}%)")

    print("\n" + "="*80)
    print("ANALYSIS COMPLETE")
    print("="*80)


if __name__ == '__main__':
    main()
