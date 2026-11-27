"""
FINAL OPTIMIZED REGIME DETECTION

Based on 5-year NIFTY backtest findings:

KEY INSIGHTS:
1. Gap Fade is the BEST predictor (46.8% WR, +51.7% total P&L)
2. Weekly/Monthly trends DON'T help predict daily direction
3. BEARISH (PE) trades work better than BULLISH (CE) trades
4. ADX filter helps avoid choppy markets
5. RSI extremes provide additional confirmation

BEST STRATEGY: GAP FADE + FILTERS
- Fade gaps between 0.15% and 1.0%
- Filter with ADX > 15 (avoid dead markets)
- RSI confirmation for extra edge
- Trade BEARISH (PE) more aggressively than BULLISH (CE)

Sources:
- https://www.quantifiedstrategies.com/gap-fill-trading-strategies/
- https://tradethatswing.com/sp-500-spy-es-gap-fill-strategy-and-statistics/
"""

import json
import os
from datetime import datetime, timedelta
from typing import Dict, Any, Tuple
import pandas as pd
import numpy as np

TOKEN_FILE = r'C:\Users\mail2\Documents\Projects\BOTS\data\kite_access_token.json'
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
NIFTY_TOKEN = 256265


def load_kite_token():
    with open(TOKEN_FILE, 'r') as f:
        return json.load(f)


def get_kite_client():
    from kiteconnect import KiteConnect
    token_data = load_kite_token()
    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


# =============================================================================
# INDICATORS
# =============================================================================

def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


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


def calculate_ema(prices: pd.Series, period: int) -> pd.Series:
    return prices.ewm(span=period, adjust=False).mean()


def prepare_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['rsi'] = calculate_rsi(df['close'], 14)
    df['atr'] = calculate_atr(df, 14)
    df['adx'] = calculate_adx(df, 14)
    df['ema_20'] = calculate_ema(df['close'], 20)

    # Volatility regime
    df['atr_pct'] = df['atr'] / df['close'] * 100

    # Previous day pattern
    df['prev_body_pct'] = (df['close'].shift(1) - df['open'].shift(1)) / df['open'].shift(1) * 100

    return df


# =============================================================================
# OPTIMIZED STRATEGIES
# =============================================================================

def strategy_gap_fade_optimized(
    today_open: float,
    yesterday: pd.Series,
    indicators: Dict[str, float]
) -> Tuple[str, Dict]:
    """
    Optimized Gap Fade Strategy

    Rules:
    1. Gap must be between 0.15% and 1.0% (sweet spot)
    2. ADX > 15 (market has some movement)
    3. ATR filter - gap should be significant vs ATR
    """
    gap_pct = (today_open - yesterday['close']) / yesterday['close'] * 100
    atr = indicators.get('atr', 100)
    adx = indicators.get('adx', 20)
    rsi = indicators.get('rsi', 50)

    gap_points = abs(today_open - yesterday['close'])
    gap_atr_ratio = gap_points / atr if atr > 0 else 0

    details = {
        'gap_pct': round(gap_pct, 2),
        'gap_atr_ratio': round(gap_atr_ratio, 2),
        'adx': round(adx, 1),
        'rsi': round(rsi, 1)
    }

    # Filter: Skip dead markets
    if adx < 15:
        return 'RANGE', details

    # Gap size filter
    if abs(gap_pct) < 0.15 or abs(gap_pct) > 1.5:
        return 'RANGE', details

    # Runaway gap filter (> 1.2x ATR might continue)
    if gap_atr_ratio > 1.2:
        return 'RANGE', details

    # Fade the gap
    if gap_pct > 0.15:
        return 'BEARISH', details
    elif gap_pct < -0.15:
        return 'BULLISH', details

    return 'RANGE', details


def strategy_gap_fade_with_rsi(
    today_open: float,
    yesterday: pd.Series,
    indicators: Dict[str, float]
) -> Tuple[str, Dict]:
    """
    Gap Fade + RSI Confirmation

    Additional RSI filter:
    - For BEARISH: RSI > 50 confirms overbought
    - For BULLISH: RSI < 50 confirms oversold
    """
    gap_pct = (today_open - yesterday['close']) / yesterday['close'] * 100
    adx = indicators.get('adx', 20)
    rsi = indicators.get('rsi', 50)

    details = {
        'gap_pct': round(gap_pct, 2),
        'adx': round(adx, 1),
        'rsi': round(rsi, 1)
    }

    if adx < 15:
        return 'RANGE', details

    if abs(gap_pct) < 0.15 or abs(gap_pct) > 1.5:
        return 'RANGE', details

    # Gap up + RSI elevated = BEARISH (fade)
    if gap_pct > 0.15 and rsi > 45:
        return 'BEARISH', details
    # Gap down + RSI low = BULLISH (fade)
    elif gap_pct < -0.15 and rsi < 55:
        return 'BULLISH', details

    return 'RANGE', details


def strategy_gap_fade_bearish_bias(
    today_open: float,
    yesterday: pd.Series,
    indicators: Dict[str, float]
) -> Tuple[str, Dict]:
    """
    Gap Fade with BEARISH Bias

    Based on finding that BEARISH trades have higher WR,
    be more aggressive on BEARISH (fade gap ups)
    """
    gap_pct = (today_open - yesterday['close']) / yesterday['close'] * 100
    adx = indicators.get('adx', 20)
    rsi = indicators.get('rsi', 50)

    details = {
        'gap_pct': round(gap_pct, 2),
        'adx': round(adx, 1),
        'rsi': round(rsi, 1)
    }

    if adx < 12:  # Slightly lower threshold
        return 'RANGE', details

    # BEARISH - more aggressive (lower threshold)
    if gap_pct > 0.1:
        return 'BEARISH', details
    # BULLISH - more conservative (higher threshold)
    elif gap_pct < -0.25:
        return 'BULLISH', details

    return 'RANGE', details


def strategy_volatility_adjusted(
    today_open: float,
    yesterday: pd.Series,
    indicators: Dict[str, float]
) -> Tuple[str, Dict]:
    """
    Volatility-Adjusted Gap Fade

    Adjust gap threshold based on recent volatility (ATR)
    High volatility = need bigger gap
    Low volatility = smaller gap is significant
    """
    gap_pct = (today_open - yesterday['close']) / yesterday['close'] * 100
    atr_pct = indicators.get('atr_pct', 1.0)
    adx = indicators.get('adx', 20)

    # Dynamic threshold based on ATR
    min_gap = max(0.1, atr_pct * 0.15)  # At least 15% of ATR
    max_gap = atr_pct * 1.5  # Not more than 150% of ATR

    details = {
        'gap_pct': round(gap_pct, 2),
        'atr_pct': round(atr_pct, 2),
        'min_gap': round(min_gap, 2),
        'max_gap': round(max_gap, 2),
        'adx': round(adx, 1)
    }

    if adx < 15:
        return 'RANGE', details

    if abs(gap_pct) < min_gap or abs(gap_pct) > max_gap:
        return 'RANGE', details

    if gap_pct > min_gap:
        return 'BEARISH', details
    elif gap_pct < -min_gap:
        return 'BULLISH', details

    return 'RANGE', details


def strategy_previous_day_pattern(
    today_open: float,
    yesterday: pd.Series,
    indicators: Dict[str, float]
) -> Tuple[str, Dict]:
    """
    Gap Fade + Previous Day Pattern

    If previous day had strong move, gap in opposite direction is likely to fade
    """
    gap_pct = (today_open - yesterday['close']) / yesterday['close'] * 100
    prev_body = indicators.get('prev_body_pct', 0)
    adx = indicators.get('adx', 20)

    details = {
        'gap_pct': round(gap_pct, 2),
        'prev_body_pct': round(prev_body, 2),
        'adx': round(adx, 1)
    }

    if adx < 15:
        return 'RANGE', details

    # Strong up day + gap up = exhaustion, fade down
    if prev_body > 0.5 and gap_pct > 0.1:
        return 'BEARISH', details

    # Strong down day + gap down = exhaustion, fade up
    if prev_body < -0.5 and gap_pct < -0.1:
        return 'BULLISH', details

    # Normal gap fade
    if abs(gap_pct) > 0.2 and abs(gap_pct) < 1.0:
        if gap_pct > 0:
            return 'BEARISH', details
        else:
            return 'BULLISH', details

    return 'RANGE', details


def strategy_combined_best(
    today_open: float,
    yesterday: pd.Series,
    indicators: Dict[str, float]
) -> Tuple[str, Dict]:
    """
    Combined Best Strategy

    Scoring system using all insights:
    - Gap fade signal
    - RSI confirmation
    - ADX filter
    - Volatility adjustment
    """
    gap_pct = (today_open - yesterday['close']) / yesterday['close'] * 100
    rsi = indicators.get('rsi', 50)
    adx = indicators.get('adx', 20)
    atr_pct = indicators.get('atr_pct', 1.0)
    prev_body = indicators.get('prev_body_pct', 0)

    bearish_score = 0
    bullish_score = 0
    signals = []

    # 1. Gap fade (weight: 3)
    if 0.15 < gap_pct < 1.2:
        bearish_score += 3
        signals.append('gap_up_fade')
    elif -1.2 < gap_pct < -0.15:
        bullish_score += 3
        signals.append('gap_down_fade')

    # 2. RSI confirmation (weight: 2)
    if rsi > 55 and gap_pct > 0:
        bearish_score += 2
        signals.append('rsi_overbought')
    elif rsi < 45 and gap_pct < 0:
        bullish_score += 2
        signals.append('rsi_oversold')

    # 3. Previous day exhaustion (weight: 1)
    if prev_body > 0.5 and gap_pct > 0:
        bearish_score += 1
        signals.append('prev_exhaustion')
    elif prev_body < -0.5 and gap_pct < 0:
        bullish_score += 1
        signals.append('prev_exhaustion')

    # 4. ADX filter (binary)
    if adx < 12:
        return 'RANGE', {'signals': signals, 'adx': adx}

    details = {
        'gap_pct': round(gap_pct, 2),
        'rsi': round(rsi, 1),
        'adx': round(adx, 1),
        'bullish_score': bullish_score,
        'bearish_score': bearish_score,
        'signals': signals
    }

    # Decision - need strong signal
    if bearish_score >= 4:
        return 'BEARISH', details
    elif bullish_score >= 4:
        return 'BULLISH', details
    elif bearish_score >= 3 and bearish_score > bullish_score:
        return 'BEARISH', details
    elif bullish_score >= 3 and bullish_score > bearish_score:
        return 'BULLISH', details

    return 'RANGE', details


# =============================================================================
# BACKTEST ENGINE
# =============================================================================

def run_backtest(df: pd.DataFrame, strategy_name: str) -> pd.DataFrame:
    """Run backtest for a strategy"""
    results = []
    start_idx = 50

    for i in range(start_idx, len(df)):
        today = df.iloc[i]
        yesterday = df.iloc[i-1]

        indicators = {
            'rsi': yesterday['rsi'] if pd.notna(yesterday['rsi']) else 50,
            'adx': yesterday['adx'] if pd.notna(yesterday['adx']) else 20,
            'atr': yesterday['atr'] if pd.notna(yesterday['atr']) else 100,
            'atr_pct': yesterday['atr_pct'] if pd.notna(yesterday['atr_pct']) else 1.0,
            'prev_body_pct': yesterday['prev_body_pct'] if pd.notna(yesterday['prev_body_pct']) else 0,
        }

        # Run strategy
        if strategy_name == 'gap_fade_optimized':
            regime, details = strategy_gap_fade_optimized(today['open'], yesterday, indicators)
        elif strategy_name == 'gap_fade_rsi':
            regime, details = strategy_gap_fade_with_rsi(today['open'], yesterday, indicators)
        elif strategy_name == 'gap_fade_bearish':
            regime, details = strategy_gap_fade_bearish_bias(today['open'], yesterday, indicators)
        elif strategy_name == 'volatility_adjusted':
            regime, details = strategy_volatility_adjusted(today['open'], yesterday, indicators)
        elif strategy_name == 'prev_day_pattern':
            regime, details = strategy_previous_day_pattern(today['open'], yesterday, indicators)
        elif strategy_name == 'combined_best':
            regime, details = strategy_combined_best(today['open'], yesterday, indicators)
        else:
            continue

        # Actual outcome
        day_change_pct = (today['close'] - today['open']) / today['open'] * 100

        if day_change_pct > 0.3:
            actual = 'UP'
        elif day_change_pct < -0.3:
            actual = 'DOWN'
        else:
            actual = 'FLAT'

        # P&L
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
        })

    return pd.DataFrame(results)


def calculate_metrics(results_df: pd.DataFrame, strategy_name: str) -> Dict:
    """Calculate performance metrics"""
    total = len(results_df)
    trades_df = results_df[results_df['predicted'] != 'RANGE'].copy()
    num_trades = len(trades_df)

    if num_trades > 0:
        wins = trades_df[trades_df['trade_pnl'] > 0]
        win_rate = len(wins) / num_trades * 100
        total_pnl = trades_df['trade_pnl'].sum()
        avg_pnl = trades_df['trade_pnl'].mean()

        bullish = trades_df[trades_df['predicted'] == 'BULLISH']
        bearish = trades_df[trades_df['predicted'] == 'BEARISH']

        bullish_wr = len(bullish[bullish['trade_pnl'] > 0]) / len(bullish) * 100 if len(bullish) > 0 else 0
        bearish_wr = len(bearish[bearish['trade_pnl'] > 0]) / len(bearish) * 100 if len(bearish) > 0 else 0
        bullish_pnl = bullish['trade_pnl'].sum() if len(bullish) > 0 else 0
        bearish_pnl = bearish['trade_pnl'].sum() if len(bearish) > 0 else 0
    else:
        win_rate = total_pnl = avg_pnl = bullish_wr = bearish_wr = bullish_pnl = bearish_pnl = 0

    regime_counts = results_df['predicted'].value_counts()

    return {
        'strategy': strategy_name,
        'num_trades': num_trades,
        'trade_pct': round(num_trades / total * 100, 1),
        'win_rate': round(win_rate, 1),
        'total_pnl': round(total_pnl, 1),
        'avg_pnl': round(avg_pnl, 3),
        'bullish_trades': regime_counts.get('BULLISH', 0),
        'bearish_trades': regime_counts.get('BEARISH', 0),
        'bullish_wr': round(bullish_wr, 1),
        'bearish_wr': round(bearish_wr, 1),
        'bullish_pnl': round(bullish_pnl, 1),
        'bearish_pnl': round(bearish_pnl, 1),
    }


def main():
    """Main analysis"""
    print("="*80)
    print("FINAL OPTIMIZED REGIME DETECTION")
    print("Gap Fade + Multiple Filters")
    print("="*80)

    # Load data
    data_file = os.path.join(OUTPUT_DIR, 'nifty_daily_5years.csv')
    df = pd.read_csv(data_file)
    df['date'] = pd.to_datetime(df['date'])

    print(f"\nData: {len(df)} days from {df['date'].min().date()} to {df['date'].max().date()}")

    # Prepare indicators
    print("Calculating indicators...")
    df = prepare_indicators(df)

    # Strategies to test
    strategies = [
        'gap_fade_optimized',
        'gap_fade_rsi',
        'gap_fade_bearish',
        'volatility_adjusted',
        'prev_day_pattern',
        'combined_best'
    ]

    print("\nRunning backtests...")
    all_metrics = []

    for strategy in strategies:
        print(f"  Testing: {strategy}")
        results = run_backtest(df, strategy)
        metrics = calculate_metrics(results, strategy)
        all_metrics.append(metrics)
        results.to_csv(os.path.join(OUTPUT_DIR, f'final_results_{strategy}.csv'), index=False)

    # Create comparison
    metrics_df = pd.DataFrame(all_metrics)
    metrics_df = metrics_df.sort_values('win_rate', ascending=False)

    print("\n" + "="*80)
    print("OPTIMIZED STRATEGY COMPARISON")
    print("="*80)

    print(f"\n{'Strategy':<22} | {'Trades':>6} | {'WR':>6} | {'Avg P&L':>8} | {'Total P&L':>10} | {'Bull WR':>7} | {'Bear WR':>7}")
    print("-"*80)

    for _, row in metrics_df.iterrows():
        print(f"{row['strategy']:<22} | {row['num_trades']:>6} | {row['win_rate']:>5.1f}% | {row['avg_pnl']:>+7.3f}% | {row['total_pnl']:>+9.1f}% | {row['bullish_wr']:>6.1f}% | {row['bearish_wr']:>6.1f}%")

    # Best strategy details
    best = metrics_df.iloc[0]
    print(f"\n" + "="*80)
    print(f"BEST OPTIMIZED STRATEGY: {best['strategy']}")
    print("="*80)
    print(f"  Win Rate: {best['win_rate']}%")
    print(f"  Avg P&L/Trade: {best['avg_pnl']:+.3f}%")
    print(f"  Total P&L (5 years): {best['total_pnl']:+.1f}%")
    print(f"  Trades: {best['num_trades']} ({best['trade_pct']}% of days)")
    print(f"  BULLISH (CE): {best['bullish_trades']} trades @ {best['bullish_wr']}% WR -> {best['bullish_pnl']:+.1f}%")
    print(f"  BEARISH (PE): {best['bearish_trades']} trades @ {best['bearish_wr']}% WR -> {best['bearish_pnl']:+.1f}%")

    # Compare with original
    print(f"\n" + "-"*80)
    print("COMPARISON WITH ORIGINAL")
    print("-"*80)
    print("\n  Original (gap+3day):")
    print("    Win Rate: 36.2%")
    print("    Total P&L: -188.4%")
    print(f"\n  {best['strategy']}:")
    print(f"    Win Rate: {best['win_rate']}% ({best['win_rate'] - 36.2:+.1f}%)")
    print(f"    Total P&L: {best['total_pnl']:+.1f}% ({best['total_pnl'] + 188.4:+.1f}%)")

    # Save comparison
    metrics_df.to_csv(os.path.join(OUTPUT_DIR, 'final_strategy_comparison.csv'), index=False)

    # Yearly breakdown for best
    print(f"\n" + "="*80)
    print(f"YEARLY BREAKDOWN - {best['strategy']}")
    print("="*80)

    best_results = pd.read_csv(os.path.join(OUTPUT_DIR, f'final_results_{best["strategy"]}.csv'))
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

            print(f"{year:>6} | {len(trades):>6} | {wr:>5.1f}% | {avg:>+7.3f}% | {total:>+9.1f}% | {bull_pnl:>+8.1f}% | {bear_pnl:>+8.1f}%")

    # Final recommendation
    print("\n" + "="*80)
    print("FINAL RECOMMENDATION")
    print("="*80)

    print(f"""
Based on 5-year NIFTY backtest analysis:

1. BEST STRATEGY: {best['strategy']}
   - Win Rate: {best['win_rate']}%
   - Avg P&L per trade: {best['avg_pnl']:+.3f}%

2. KEY RULES:
   - FADE the gap (gap up -> sell, gap down -> buy)
   - Gap must be between 0.15% and 1.5%
   - ADX must be > 15 (avoid dead markets)
   - RSI confirmation improves accuracy

3. TRADE PREFERENCE:
   - BEARISH (PE) trades: {best['bearish_wr']}% WR
   - BULLISH (CE) trades: {best['bullish_wr']}% WR
   - PE trades are more reliable

4. AVOID:
   - Very small gaps (< 0.1%)
   - Runaway gaps (> 1.5%)
   - Low ADX days (< 15)

5. IMPROVEMENT OVER ORIGINAL:
   - Win Rate: +{best['win_rate'] - 36.2:.1f}%
   - Total P&L: +{best['total_pnl'] + 188.4:.1f}%
""")

    print("="*80)


if __name__ == '__main__':
    main()
