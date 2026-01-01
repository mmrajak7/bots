"""
Test SuperTrend Touch Reliability - Trade by Trade Output
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path


def calculate_supertrend(df, period=10, multiplier=3.0, atr_period=10):
    """
    Calculate SuperTrend - TradingView compatible.
    Uses ATR_PERIOD=10 (TradingView default).
    """
    df = df.copy()

    high = df['high']
    low = df['low']
    close = df['close']

    # ATR calculation
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=atr_period, min_periods=1).mean()
    df['atr'] = atr

    # Basic bands
    hl2 = (high + low) / 2
    basic_upper = hl2 + (multiplier * atr)
    basic_lower = hl2 - (multiplier * atr)

    # Initialize
    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()
    supertrend = pd.Series(0.0, index=df.index)
    direction = pd.Series(1, index=df.index)

    # Calculate iteratively
    for i in range(1, len(df)):
        # Final Upper Band
        if basic_upper.iloc[i] < final_upper.iloc[i-1] or close.iloc[i-1] > final_upper.iloc[i-1]:
            final_upper.iloc[i] = basic_upper.iloc[i]
        else:
            final_upper.iloc[i] = final_upper.iloc[i-1]

        # Final Lower Band
        if basic_lower.iloc[i] > final_lower.iloc[i-1] or close.iloc[i-1] < final_lower.iloc[i-1]:
            final_lower.iloc[i] = basic_lower.iloc[i]
        else:
            final_lower.iloc[i] = final_lower.iloc[i-1]

        # Direction and SuperTrend
        if direction.iloc[i-1] == 1:  # Was uptrend
            if close.iloc[i] < final_lower.iloc[i]:
                direction.iloc[i] = -1
                supertrend.iloc[i] = final_upper.iloc[i]
            else:
                direction.iloc[i] = 1
                supertrend.iloc[i] = final_lower.iloc[i]
        else:  # Was downtrend
            if close.iloc[i] > final_upper.iloc[i]:
                direction.iloc[i] = 1
                supertrend.iloc[i] = final_lower.iloc[i]
            else:
                direction.iloc[i] = -1
                supertrend.iloc[i] = final_upper.iloc[i]

    supertrend.iloc[0] = final_lower.iloc[0]

    df['supertrend'] = supertrend
    df['direction'] = direction
    return df


def find_touches(df, threshold_pct=1.5):
    """Find touch events: low within threshold of ST in uptrend, close above ST"""
    touches = []
    min_distance = -0.5  # Max allowed undershoot (if lower, it's a breakdown not touch)

    for i in range(1, len(df)):
        if df['direction'].iloc[i] != 1:  # Must be uptrend
            continue

        st = df['supertrend'].iloc[i]
        low = df['low'].iloc[i]
        close = df['close'].iloc[i]

        dist_pct = (low - st) / st * 100

        # Touch: low within -0.5% to +1.5% of ST, close above ST
        # If low goes more than 0.5% below ST, it's a breakdown not a touch
        if dist_pct >= min_distance and dist_pct <= threshold_pct and close > st:
            touches.append(i)

    return touches


def simulate_trade(df, entry_idx, target_mult=1.5, max_days=20):
    """Simulate trade from touch candle"""
    entry_price = df['close'].iloc[entry_idx]
    entry_date = df['date'].iloc[entry_idx]
    entry_st = df['supertrend'].iloc[entry_idx]
    atr = df['atr'].iloc[entry_idx]
    target_price = entry_price + (atr * target_mult)

    result = {
        'entry_date': entry_date.strftime('%Y-%m-%d'),
        'entry_price': entry_price,
        'target': target_price,
        'initial_sl': entry_st,
        'exit_date': None,
        'exit_price': None,
        'exit_reason': None,
        'pnl_pct': None,
        'days': None
    }

    for i in range(entry_idx + 1, min(entry_idx + max_days + 1, len(df))):
        high = df['high'].iloc[i]
        low = df['low'].iloc[i]
        close = df['close'].iloc[i]
        current_st = df['supertrend'].iloc[i]
        direction = df['direction'].iloc[i]

        days_held = (df['date'].iloc[i] - entry_date).days

        # Target hit
        if high >= target_price:
            result['exit_date'] = df['date'].iloc[i].strftime('%Y-%m-%d')
            result['exit_price'] = target_price
            result['exit_reason'] = 'TARGET'
            result['pnl_pct'] = ((target_price - entry_price) / entry_price) * 100
            result['days'] = days_held
            return result

        # Stop loss (trend reversal or low below ST)
        if direction == -1 or low < current_st:
            exit_price = min(close, current_st)
            result['exit_date'] = df['date'].iloc[i].strftime('%Y-%m-%d')
            result['exit_price'] = exit_price
            result['exit_reason'] = 'SL'
            result['pnl_pct'] = ((exit_price - entry_price) / entry_price) * 100
            result['days'] = days_held
            return result

    # Timeout
    last_idx = min(entry_idx + max_days, len(df) - 1)
    result['exit_date'] = df['date'].iloc[last_idx].strftime('%Y-%m-%d')
    result['exit_price'] = df['close'].iloc[last_idx]
    result['exit_reason'] = 'TIMEOUT'
    result['pnl_pct'] = ((result['exit_price'] - entry_price) / entry_price) * 100
    result['days'] = max_days
    return result


def test_stock(symbol, lookback_days=365):
    """Test single stock and return trade results"""
    data_path = Path(__file__).parent.parent.parent / "Bouncer" / "data" / "historical"
    file_path = data_path / f"{symbol}_daily.csv"

    if not file_path.exists():
        print(f"{symbol}: File not found")
        return None

    df = pd.read_csv(file_path)
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)

    # Filter to lookback (keep some warmup data for ST calculation)
    cutoff = datetime.now() - timedelta(days=lookback_days + 50)
    df = df[df['date'] >= cutoff].reset_index(drop=True)

    if len(df) < 50:
        print(f"{symbol}: Insufficient data")
        return None

    # Calculate SuperTrend (ATR=10 to match TradingView)
    df = calculate_supertrend(df, period=10, multiplier=3.0, atr_period=10)

    # Filter to actual test period
    test_cutoff = datetime.now() - timedelta(days=lookback_days)
    df_test = df[df['date'] >= test_cutoff].reset_index(drop=True)

    # Find touches (1.5% threshold)
    touch_indices = find_touches(df_test, threshold_pct=1.5)

    if not touch_indices:
        print(f"{symbol}: No touches found")
        return None

    # Simulate trades (non-overlapping)
    trades = []
    last_exit_idx = -1

    for idx in touch_indices:
        if idx <= last_exit_idx:
            continue

        result = simulate_trade(df_test, idx)
        if result['exit_date']:
            trades.append(result)
            # Find exit index
            exit_date = datetime.strptime(result['exit_date'], '%Y-%m-%d')
            for j in range(idx, len(df_test)):
                if df_test['date'].iloc[j] >= exit_date:
                    last_exit_idx = j
                    break

    return trades


def print_trades(symbol, trades):
    """Print trade-by-trade results"""
    if not trades:
        print(f"\n{symbol}: No trades")
        return

    wins = sum(1 for t in trades if t['exit_reason'] == 'TARGET')
    losses = sum(1 for t in trades if t['exit_reason'] == 'SL')
    timeouts = sum(1 for t in trades if t['exit_reason'] == 'TIMEOUT')

    print(f"\n{'=' * 50}")
    print(f"{symbol}")
    print(f"{'=' * 50}")
    print(f"Total: {len(trades)} | Wins: {wins} | Losses: {losses} | Timeouts: {timeouts}")
    print(f"Win Rate: {wins/len(trades)*100:.1f}%")
    print()

    header = f"{'Entry':<12} {'Exit':<12} {'Entry':>9} {'Target':>9} {'SL':>9} {'ExitPr':>9} {'P&L':>7} {'Days':>5} {'Result':>8}"
    print(header)
    print("-" * len(header))

    for t in trades:
        print(f"{t['entry_date']:<12} {t['exit_date']:<12} {t['entry_price']:>9.2f} {t['target']:>9.2f} {t['initial_sl']:>9.2f} {t['exit_price']:>9.2f} {t['pnl_pct']:>+6.2f}% {t['days']:>5} {t['exit_reason']:>8}")


if __name__ == "__main__":
    print("=" * 100)
    print("SUPERTREND TOUCH RELIABILITY - TRADE BY TRADE RESULTS")
    print("Parameters: ST(10,3), ATR=10, Touch threshold=1.5%, Target=1.5x ATR, Max hold=20 days")
    print("=" * 100)

    # Test 3 stocks
    test_symbols = ['APOLLOHOSP', 'RELIANCE', 'HDFCBANK']

    for symbol in test_symbols:
        trades = test_stock(symbol)
        print_trades(symbol, trades)
