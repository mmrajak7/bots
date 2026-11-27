"""
Regime Prediction Analysis - WITHOUT FORWARD BIAS

This script:
1. Downloads 5 years of NIFTY daily data
2. For each trading day, calculates the REGIME using ONLY data available at that point (no forward bias)
3. Compares predicted regime to actual day outcome
4. Calculates accuracy metrics

Regime Detection Logic (from option_buy.py):
- BEARISH: gap_down OR (bearish_days >= 2 AND not intraday_bullish)
- BULLISH: gap_up OR (bullish_days >= 2 AND intraday_bullish)
- RANGE: everything else

The prediction is made at market open using:
- Previous day's close
- Today's open (for gap detection)
- Last 3 days direction count
"""

import json
import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple
import pandas as pd
import numpy as np

# Token file path (shared with CROCODILE/SNAIL)
TOKEN_FILE = r'C:\Users\mail2\Documents\Projects\BOTS\data\kite_access_token.json'
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))

# NIFTY 50 instrument token
NIFTY_TOKEN = 256265  # NSE:NIFTY 50 index token


def load_kite_token() -> Dict[str, Any]:
    """Load access token from shared JSON file"""
    if not os.path.exists(TOKEN_FILE):
        raise FileNotFoundError(f"Token file not found: {TOKEN_FILE}")

    with open(TOKEN_FILE, 'r') as f:
        return json.load(f)


def get_kite_client():
    """Get authenticated KiteConnect instance"""
    try:
        from kiteconnect import KiteConnect
    except ImportError:
        raise ImportError("kiteconnect library required. Install with: pip install kiteconnect")

    token_data = load_kite_token()
    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    print(f"Connected to Kite API as user: {token_data['user_id']}")
    return kite


def download_nifty_daily_data(kite, years: int = 5) -> pd.DataFrame:
    """Download NIFTY daily data for specified years"""
    import time

    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=years * 365 + 30)  # Extra buffer for holidays

    print(f"Downloading NIFTY daily data from {start_date} to {end_date}...")

    all_data = []

    # Download year by year (API limitation)
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
                print(f"  Downloaded {len(data)} candles for {current_start.year}")
        except Exception as e:
            print(f"  Error for {current_start} to {current_end}: {e}")

        current_start = current_end + timedelta(days=1)
        time.sleep(0.5)  # Rate limiting

    if not all_data:
        raise ValueError("No data downloaded")

    df = pd.DataFrame(all_data)
    df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
    df = df.sort_values('date').reset_index(drop=True)
    df = df.drop_duplicates(subset=['date'], keep='first')

    print(f"Total: {len(df)} daily candles from {df['date'].min().date()} to {df['date'].max().date()}")
    return df


def download_nifty_hourly_data(kite, start_date: datetime, end_date: datetime) -> pd.DataFrame:
    """Download NIFTY hourly data for a date range (optional, for intraday analysis)"""
    import time

    print(f"Downloading NIFTY hourly data from {start_date.date()} to {end_date.date()}...")

    all_data = []

    # Download in 60-day chunks (API limitation for intraday)
    current_start = start_date
    while current_start < end_date:
        current_end = min(current_start + timedelta(days=60), end_date)

        try:
            data = kite.historical_data(
                instrument_token=NIFTY_TOKEN,
                from_date=current_start.strftime('%Y-%m-%d'),
                to_date=current_end.strftime('%Y-%m-%d'),
                interval='60minute'
            )
            if data:
                all_data.extend(data)
                print(f"  Downloaded {len(data)} hourly candles")
        except Exception as e:
            print(f"  Error for {current_start} to {current_end}: {e}")

        current_start = current_end + timedelta(days=1)
        time.sleep(0.5)

    if not all_data:
        return pd.DataFrame()

    df = pd.DataFrame(all_data)
    df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
    df = df.sort_values('date').reset_index(drop=True)

    return df


def predict_regime_at_open(
    today_open: float,
    yesterday_close: float,
    last_3_days: pd.DataFrame,
    gap_threshold: float = 0.001  # 0.1%
) -> Tuple[str, Dict[str, Any]]:
    """
    Predict regime at market open using ONLY data available at that point.

    This is the PREDICTION made at 9:15 AM using:
    - Today's open price
    - Yesterday's close
    - Last 3 completed days' candles

    NO FORWARD BIAS - we don't know today's close yet!

    Returns:
        (regime, details_dict)
    """
    details = {}

    # Gap detection
    gap_pct = (today_open - yesterday_close) / yesterday_close
    gap_down = gap_pct < -gap_threshold
    gap_up = gap_pct > gap_threshold
    details['gap_pct'] = round(gap_pct * 100, 2)
    details['gap_down'] = gap_down
    details['gap_up'] = gap_up

    # Count bullish/bearish days in last 3 completed days
    if len(last_3_days) >= 3:
        recent = last_3_days.tail(3)
        bullish_days = (recent['close'] > recent['open']).sum()
        bearish_days = 3 - bullish_days
    else:
        bullish_days = 0
        bearish_days = 0

    details['bullish_days'] = int(bullish_days)
    details['bearish_days'] = int(bearish_days)

    # For intraday direction prediction, we use gap direction
    # (Since we can't know intraday_bullish at open, we use gap as proxy)
    intraday_bias_bullish = gap_up or (not gap_down and bullish_days >= 2)
    details['intraday_bias_bullish'] = intraday_bias_bullish

    # Regime prediction logic (matching option_buy.py)
    if gap_down or (bearish_days >= 2 and not intraday_bias_bullish):
        regime = 'BEARISH'
    elif gap_up or (bullish_days >= 2 and intraday_bias_bullish):
        regime = 'BULLISH'
    else:
        regime = 'RANGE'

    return regime, details


def calculate_actual_outcome(row: pd.Series) -> Tuple[str, float]:
    """
    Calculate what actually happened during the day.

    Returns:
        (actual_direction, day_change_pct)
    """
    day_change_pct = (row['close'] - row['open']) / row['open'] * 100

    # Define thresholds for significant move
    if day_change_pct > 0.3:  # Up more than 0.3%
        return 'UP', day_change_pct
    elif day_change_pct < -0.3:  # Down more than 0.3%
        return 'DOWN', day_change_pct
    else:
        return 'FLAT', day_change_pct


def is_prediction_correct(predicted_regime: str, actual_direction: str) -> bool:
    """
    Check if our regime prediction was correct.

    BEARISH prediction is correct if market went DOWN
    BULLISH prediction is correct if market went UP
    RANGE prediction is correct if market was FLAT (or we count any non-extreme move)
    """
    if predicted_regime == 'BEARISH':
        return actual_direction == 'DOWN'
    elif predicted_regime == 'BULLISH':
        return actual_direction == 'UP'
    else:  # RANGE
        return actual_direction == 'FLAT'


def is_trade_profitable(predicted_regime: str, actual_direction: str, day_change_pct: float) -> Tuple[bool, float]:
    """
    Simulate if our trade would be profitable.

    BEARISH -> Buy PE (profit if market DOWN)
    BULLISH -> Buy CE (profit if market UP)
    RANGE -> No trade

    Returns:
        (would_profit, simulated_pnl_pct)
    """
    if predicted_regime == 'RANGE':
        return False, 0.0  # No trade

    if predicted_regime == 'BEARISH':
        # Buy PE - profit if market down
        # Rough approximation: PE gains ~2-3x the index drop (depending on delta)
        delta_multiplier = 2.5
        if actual_direction == 'DOWN':
            return True, abs(day_change_pct) * delta_multiplier
        else:
            # Loss scenario
            return False, -abs(day_change_pct) * delta_multiplier

    elif predicted_regime == 'BULLISH':
        # Buy CE - profit if market up
        delta_multiplier = 2.5
        if actual_direction == 'UP':
            return True, abs(day_change_pct) * delta_multiplier
        else:
            return False, -abs(day_change_pct) * delta_multiplier

    return False, 0.0


def run_regime_analysis(df: pd.DataFrame) -> pd.DataFrame:
    """
    Run regime prediction analysis on historical data.

    For each day (starting from day 4), predict regime using only past data.
    """
    results = []

    for i in range(3, len(df)):  # Start from day 4 (need 3 days history)
        today = df.iloc[i]
        yesterday = df.iloc[i-1]
        last_3_days = df.iloc[i-3:i]  # 3 completed days before today

        # Predict regime at open (NO FORWARD BIAS)
        predicted_regime, details = predict_regime_at_open(
            today_open=today['open'],
            yesterday_close=yesterday['close'],
            last_3_days=last_3_days
        )

        # Calculate actual outcome (what really happened)
        actual_direction, day_change_pct = calculate_actual_outcome(today)

        # Check prediction accuracy
        correct = is_prediction_correct(predicted_regime, actual_direction)

        # Simulate trade
        would_profit, sim_pnl = is_trade_profitable(predicted_regime, actual_direction, day_change_pct)

        results.append({
            'date': today['date'],
            'open': today['open'],
            'high': today['high'],
            'low': today['low'],
            'close': today['close'],
            'day_change_pct': round(day_change_pct, 2),
            'predicted_regime': predicted_regime,
            'actual_direction': actual_direction,
            'gap_pct': details['gap_pct'],
            'bullish_days': details['bullish_days'],
            'bearish_days': details['bearish_days'],
            'prediction_correct': correct,
            'would_profit': would_profit if predicted_regime != 'RANGE' else None,
            'simulated_pnl': round(sim_pnl, 2) if predicted_regime != 'RANGE' else None
        })

    return pd.DataFrame(results)


def print_analysis_report(results_df: pd.DataFrame):
    """Print comprehensive analysis report"""

    print("\n" + "="*80)
    print("REGIME PREDICTION ANALYSIS - NO FORWARD BIAS")
    print("="*80)

    total_days = len(results_df)
    print(f"\nTotal Trading Days Analyzed: {total_days}")
    print(f"Date Range: {results_df['date'].min().date()} to {results_df['date'].max().date()}")

    # Regime Distribution
    print("\n" + "-"*40)
    print("REGIME DISTRIBUTION (Predictions Made)")
    print("-"*40)
    regime_counts = results_df['predicted_regime'].value_counts()
    for regime in ['BEARISH', 'BULLISH', 'RANGE']:
        if regime in regime_counts:
            count = regime_counts[regime]
            pct = count / total_days * 100
            print(f"  {regime:8s}: {count:4d} days ({pct:5.1f}%)")

    # Actual Direction Distribution
    print("\n" + "-"*40)
    print("ACTUAL MARKET DIRECTION")
    print("-"*40)
    direction_counts = results_df['actual_direction'].value_counts()
    for direction in ['UP', 'DOWN', 'FLAT']:
        if direction in direction_counts:
            count = direction_counts[direction]
            pct = count / total_days * 100
            print(f"  {direction:4s}: {count:4d} days ({pct:5.1f}%)")

    # Overall Prediction Accuracy
    print("\n" + "-"*40)
    print("PREDICTION ACCURACY (Regime vs Actual Direction)")
    print("-"*40)
    correct_predictions = results_df['prediction_correct'].sum()
    accuracy = correct_predictions / total_days * 100
    print(f"  Overall Accuracy: {correct_predictions}/{total_days} = {accuracy:.1f}%")

    # Accuracy by Regime
    print("\n  By Regime:")
    for regime in ['BEARISH', 'BULLISH', 'RANGE']:
        regime_df = results_df[results_df['predicted_regime'] == regime]
        if len(regime_df) > 0:
            correct = regime_df['prediction_correct'].sum()
            accuracy = correct / len(regime_df) * 100
            print(f"    {regime:8s}: {correct:4d}/{len(regime_df):4d} = {accuracy:5.1f}%")

    # Trade Simulation (only non-RANGE predictions)
    print("\n" + "-"*40)
    print("TRADE SIMULATION (BEARISH->PE, BULLISH->CE)")
    print("-"*40)

    trades_df = results_df[results_df['predicted_regime'] != 'RANGE'].copy()
    total_trades = len(trades_df)
    winning_trades = trades_df['would_profit'].sum()
    win_rate = winning_trades / total_trades * 100 if total_trades > 0 else 0

    print(f"  Total Trades: {total_trades}")
    print(f"  Winning Trades: {winning_trades}")
    print(f"  Win Rate: {win_rate:.1f}%")

    # P&L Analysis
    total_pnl = trades_df['simulated_pnl'].sum()
    avg_pnl = trades_df['simulated_pnl'].mean()
    print(f"  Total Simulated P&L: {total_pnl:+.1f}%")
    print(f"  Average P&L/Trade: {avg_pnl:+.2f}%")

    # By Trade Direction
    print("\n  By Trade Direction:")
    for regime in ['BEARISH', 'BULLISH']:
        regime_trades = trades_df[trades_df['predicted_regime'] == regime]
        if len(regime_trades) > 0:
            wins = regime_trades['would_profit'].sum()
            total = len(regime_trades)
            wr = wins / total * 100
            pnl = regime_trades['simulated_pnl'].sum()
            avg = regime_trades['simulated_pnl'].mean()
            print(f"    {regime:8s} ({'PE' if regime == 'BEARISH' else 'CE'}): {wins}/{total} = {wr:5.1f}% WR, {pnl:+.1f}% Total, {avg:+.2f}%/trade")

    # Yearly Breakdown
    print("\n" + "-"*40)
    print("YEARLY BREAKDOWN")
    print("-"*40)

    results_df['year'] = results_df['date'].dt.year

    print(f"  {'Year':>6s} | {'Days':>5s} | {'BEAR':>5s} | {'BULL':>5s} | {'RANGE':>5s} | {'Accuracy':>8s} | {'Win Rate':>8s}")
    print("  " + "-"*62)

    for year in sorted(results_df['year'].unique()):
        year_df = results_df[results_df['year'] == year]
        days = len(year_df)
        bear = len(year_df[year_df['predicted_regime'] == 'BEARISH'])
        bull = len(year_df[year_df['predicted_regime'] == 'BULLISH'])
        range_c = len(year_df[year_df['predicted_regime'] == 'RANGE'])
        accuracy = year_df['prediction_correct'].sum() / days * 100

        year_trades = year_df[year_df['predicted_regime'] != 'RANGE']
        if len(year_trades) > 0:
            win_rate = year_trades['would_profit'].sum() / len(year_trades) * 100
        else:
            win_rate = 0

        print(f"  {year:>6d} | {days:>5d} | {bear:>5d} | {bull:>5d} | {range_c:>5d} | {accuracy:>7.1f}% | {win_rate:>7.1f}%")

    # Monthly Breakdown for recent data
    print("\n" + "-"*40)
    print("MONTHLY BREAKDOWN (Last 12 Months)")
    print("-"*40)

    recent_df = results_df[results_df['date'] >= results_df['date'].max() - timedelta(days=365)]
    recent_df['month'] = recent_df['date'].dt.to_period('M')

    print(f"  {'Month':>8s} | {'Days':>4s} | {'BEAR':>4s} | {'BULL':>4s} | {'Acc':>5s} | {'WR':>5s} | {'P&L':>6s}")
    print("  " + "-"*52)

    for month in sorted(recent_df['month'].unique())[-12:]:
        month_df = recent_df[recent_df['month'] == month]
        days = len(month_df)
        bear = len(month_df[month_df['predicted_regime'] == 'BEARISH'])
        bull = len(month_df[month_df['predicted_regime'] == 'BULLISH'])
        accuracy = month_df['prediction_correct'].sum() / days * 100

        month_trades = month_df[month_df['predicted_regime'] != 'RANGE']
        if len(month_trades) > 0:
            win_rate = month_trades['would_profit'].sum() / len(month_trades) * 100
            pnl = month_trades['simulated_pnl'].sum()
        else:
            win_rate = 0
            pnl = 0

        print(f"  {str(month):>8s} | {days:>4d} | {bear:>4d} | {bull:>4d} | {accuracy:>4.0f}% | {win_rate:>4.0f}% | {pnl:>+5.0f}%")

    # Gap Analysis
    print("\n" + "-"*40)
    print("GAP ANALYSIS (Impact on Predictions)")
    print("-"*40)

    gap_down_df = results_df[results_df['gap_pct'] < -0.1]
    gap_up_df = results_df[results_df['gap_pct'] > 0.1]
    no_gap_df = results_df[(results_df['gap_pct'] >= -0.1) & (results_df['gap_pct'] <= 0.1)]

    print(f"  Gap Down (>{0.1}%): {len(gap_down_df)} days")
    if len(gap_down_df) > 0:
        acc = gap_down_df['prediction_correct'].sum() / len(gap_down_df) * 100
        print(f"    Prediction Accuracy: {acc:.1f}%")
        print(f"    Market went DOWN: {len(gap_down_df[gap_down_df['actual_direction'] == 'DOWN'])} ({len(gap_down_df[gap_down_df['actual_direction'] == 'DOWN'])/len(gap_down_df)*100:.1f}%)")

    print(f"\n  Gap Up (>{0.1}%): {len(gap_up_df)} days")
    if len(gap_up_df) > 0:
        acc = gap_up_df['prediction_correct'].sum() / len(gap_up_df) * 100
        print(f"    Prediction Accuracy: {acc:.1f}%")
        print(f"    Market went UP: {len(gap_up_df[gap_up_df['actual_direction'] == 'UP'])} ({len(gap_up_df[gap_up_df['actual_direction'] == 'UP'])/len(gap_up_df)*100:.1f}%)")

    print(f"\n  No Significant Gap: {len(no_gap_df)} days")
    if len(no_gap_df) > 0:
        acc = no_gap_df['prediction_correct'].sum() / len(no_gap_df) * 100
        print(f"    Prediction Accuracy: {acc:.1f}%")

    # Key Insights
    print("\n" + "="*80)
    print("KEY INSIGHTS")
    print("="*80)

    # Best performing regime
    regime_performance = {}
    for regime in ['BEARISH', 'BULLISH']:
        regime_trades = trades_df[trades_df['predicted_regime'] == regime]
        if len(regime_trades) > 0:
            regime_performance[regime] = {
                'win_rate': regime_trades['would_profit'].sum() / len(regime_trades) * 100,
                'avg_pnl': regime_trades['simulated_pnl'].mean(),
                'trades': len(regime_trades)
            }

    if regime_performance:
        best_regime = max(regime_performance.keys(), key=lambda x: regime_performance[x]['win_rate'])
        print(f"\n1. Best Performing Regime: {best_regime}")
        print(f"   - Win Rate: {regime_performance[best_regime]['win_rate']:.1f}%")
        print(f"   - Avg P&L/Trade: {regime_performance[best_regime]['avg_pnl']:+.2f}%")

    # Gap prediction reliability
    if len(gap_down_df) > 0:
        gap_down_correct = len(gap_down_df[gap_down_df['actual_direction'] == 'DOWN']) / len(gap_down_df) * 100
        print(f"\n2. Gap Down -> Market Down: {gap_down_correct:.1f}% reliable")

    if len(gap_up_df) > 0:
        gap_up_correct = len(gap_up_df[gap_up_df['actual_direction'] == 'UP']) / len(gap_up_df) * 100
        print(f"   Gap Up -> Market Up: {gap_up_correct:.1f}% reliable")

    # 3-day trend reliability
    strong_bear = results_df[results_df['bearish_days'] >= 2]
    if len(strong_bear) > 0:
        bear_acc = len(strong_bear[strong_bear['actual_direction'] == 'DOWN']) / len(strong_bear) * 100
        print(f"\n3. After 2+ Bearish Days -> Market Down: {bear_acc:.1f}% ({len(strong_bear)} cases)")

    strong_bull = results_df[results_df['bullish_days'] >= 2]
    if len(strong_bull) > 0:
        bull_acc = len(strong_bull[strong_bull['actual_direction'] == 'UP']) / len(strong_bull) * 100
        print(f"   After 2+ Bullish Days -> Market Up: {bull_acc:.1f}% ({len(strong_bull)} cases)")


def main():
    """Main entry point"""
    print("="*80)
    print("REGIME PREDICTION ANALYSIS - NIFTY 50")
    print("Using ONLY past data at each point (NO FORWARD BIAS)")
    print("="*80)

    # Connect to Kite API
    kite = get_kite_client()

    # Download data
    df = download_nifty_daily_data(kite, years=5)

    # Save raw data
    raw_data_file = os.path.join(OUTPUT_DIR, 'nifty_daily_5years.csv')
    df.to_csv(raw_data_file, index=False)
    print(f"\nRaw data saved to: {raw_data_file}")

    # Run analysis
    print("\nRunning regime prediction analysis...")
    results_df = run_regime_analysis(df)

    # Save results
    results_file = os.path.join(OUTPUT_DIR, 'regime_prediction_results.csv')
    results_df.to_csv(results_file, index=False)
    print(f"Results saved to: {results_file}")

    # Print report
    print_analysis_report(results_df)

    print("\n" + "="*80)
    print("ANALYSIS COMPLETE")
    print("="*80)


if __name__ == '__main__':
    main()
