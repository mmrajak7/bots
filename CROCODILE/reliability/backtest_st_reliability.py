"""
SuperTrend Touch Reliability Backtest
=====================================
Tests how reliably each stock respects SuperTrend touches.

Methodology:
1. Calculate SuperTrend(10,3) on daily data
2. Find "touch" events (low touches ST in uptrend, closes above)
3. Simulate long trade from touch candle close
4. Track outcome: Hit 1.5x ATR target vs stopped out

Success Criteria:
- Entry: Close of touch candle
- Target: Entry + (ATR * 1.5)
- Stop Loss: SuperTrend value (trailing)
- Win: Target hit before SL

Output: st_reliability.json with win rates per stock
"""

import json
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, asdict

# Import SuperTrend calculator from same folder
from supertrend import calculate_supertrend, find_supertrend_touches, calculate_atr


@dataclass
class TradeResult:
    """Result of a simulated trade."""
    entry_date: str
    entry_price: float
    target_price: float
    initial_sl: float
    exit_date: str
    exit_price: float
    exit_reason: str  # 'target', 'sl', 'timeout'
    pnl_pct: float
    days_held: int


@dataclass
class StockReliability:
    """Reliability metrics for a single stock."""
    symbol: str
    total_touches: int
    wins: int
    losses: int
    timeouts: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    avg_days_held: float
    trades: List[TradeResult]


# Configuration
HISTORICAL_DATA_PATH = Path(__file__).parent.parent.parent / "Bouncer" / "data" / "historical"
OUTPUT_PATH = Path(__file__).parent / "st_reliability.json"
LOOKBACK_DAYS = 365  # Test only past 1 year

# Trade Parameters
ATR_TARGET_MULTIPLIER = 1.5
MAX_HOLD_DAYS = 20  # Timeout after 20 days
TOUCH_THRESHOLD_PCT = 0.5  # % proximity to ST for touch detection
FRESH_TOUCH_LOOKBACK = 10  # Candles gap for "fresh" touch


def load_historical_data(symbol: str) -> Optional[pd.DataFrame]:
    """Load historical daily data for a symbol."""
    file_path = HISTORICAL_DATA_PATH / f"{symbol}_daily.csv"

    if not file_path.exists():
        return None

    try:
        df = pd.read_csv(file_path)
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        return df
    except Exception as e:
        print(f"  Error loading {symbol}: {e}")
        return None


def filter_to_lookback(df: pd.DataFrame, days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """Filter DataFrame to only include last N days."""
    cutoff_date = datetime.now() - timedelta(days=days)
    return df[df['date'] >= cutoff_date].reset_index(drop=True)


def simulate_trade(
    df: pd.DataFrame,
    entry_idx: int,
    atr_value: float
) -> Optional[TradeResult]:
    """
    Simulate a long trade from touch candle.

    Entry: Close of touch candle
    Target: Entry + (ATR * 1.5)
    Stop Loss: SuperTrend (trailing)
    """
    if entry_idx >= len(df) - 1:
        return None  # No room to trade

    entry_price = df['close'].iloc[entry_idx]
    entry_date = df['date'].iloc[entry_idx]
    target_price = entry_price + (atr_value * ATR_TARGET_MULTIPLIER)
    initial_sl = df['supertrend'].iloc[entry_idx]

    # Simulate day by day
    for i in range(entry_idx + 1, min(entry_idx + MAX_HOLD_DAYS + 1, len(df))):
        high = df['high'].iloc[i]
        low = df['low'].iloc[i]
        close = df['close'].iloc[i]
        current_st = df['supertrend'].iloc[i]
        direction = df['direction'].iloc[i]
        current_date = df['date'].iloc[i]
        days_held = (current_date - entry_date).days

        # Check target hit (using high of day)
        if high >= target_price:
            pnl_pct = ((target_price - entry_price) / entry_price) * 100
            return TradeResult(
                entry_date=entry_date.strftime('%Y-%m-%d'),
                entry_price=round(entry_price, 2),
                target_price=round(target_price, 2),
                initial_sl=round(initial_sl, 2),
                exit_date=current_date.strftime('%Y-%m-%d'),
                exit_price=round(target_price, 2),
                exit_reason='target',
                pnl_pct=round(pnl_pct, 2),
                days_held=days_held
            )

        # Check stop loss hit (trend reversal or low below ST)
        if direction == -1 or low < current_st:
            # Exit at ST or low, whichever is worse
            exit_price = min(close, current_st)
            pnl_pct = ((exit_price - entry_price) / entry_price) * 100
            return TradeResult(
                entry_date=entry_date.strftime('%Y-%m-%d'),
                entry_price=round(entry_price, 2),
                target_price=round(target_price, 2),
                initial_sl=round(initial_sl, 2),
                exit_date=current_date.strftime('%Y-%m-%d'),
                exit_price=round(exit_price, 2),
                exit_reason='sl',
                pnl_pct=round(pnl_pct, 2),
                days_held=days_held
            )

    # Timeout - exit at last available close
    last_idx = min(entry_idx + MAX_HOLD_DAYS, len(df) - 1)
    exit_price = df['close'].iloc[last_idx]
    exit_date = df['date'].iloc[last_idx]
    days_held = (exit_date - entry_date).days
    pnl_pct = ((exit_price - entry_price) / entry_price) * 100

    return TradeResult(
        entry_date=entry_date.strftime('%Y-%m-%d'),
        entry_price=round(entry_price, 2),
        target_price=round(target_price, 2),
        initial_sl=round(initial_sl, 2),
        exit_date=exit_date.strftime('%Y-%m-%d'),
        exit_price=round(exit_price, 2),
        exit_reason='timeout',
        pnl_pct=round(pnl_pct, 2),
        days_held=days_held
    )


def backtest_stock(symbol: str, use_fresh_only: bool = True) -> Optional[StockReliability]:
    """
    Run backtest for a single stock.

    Args:
        symbol: Stock symbol
        use_fresh_only: If True, only test "fresh" touches (10-candle gap)
    """
    # Load data
    df = load_historical_data(symbol)
    if df is None or len(df) < 50:
        return None

    # Filter to lookback period
    df = filter_to_lookback(df, LOOKBACK_DAYS)
    if len(df) < 30:
        return None

    # Calculate SuperTrend
    df = calculate_supertrend(df, period=10, multiplier=3.0, atr_period=14)

    # Find touches
    df = find_supertrend_touches(
        df,
        touch_threshold_pct=TOUCH_THRESHOLD_PCT,
        fresh_touch_lookback=FRESH_TOUCH_LOOKBACK
    )

    # Get touch indices
    if use_fresh_only:
        touch_mask = df['is_fresh_touch']
    else:
        touch_mask = df['is_touch']

    touch_indices = df[touch_mask].index.tolist()

    if not touch_indices:
        return None

    # Simulate trades
    trades: List[TradeResult] = []
    last_exit_idx = -1

    for idx in touch_indices:
        # Skip if still in a trade
        if idx <= last_exit_idx:
            continue

        atr_value = df['atr'].iloc[idx]
        result = simulate_trade(df, idx, atr_value)

        if result:
            trades.append(result)
            # Find exit index to avoid overlapping trades
            exit_date = datetime.strptime(result.exit_date, '%Y-%m-%d')
            exit_mask = df['date'] >= exit_date
            if exit_mask.any():
                last_exit_idx = df[exit_mask].index[0]

    if not trades:
        return None

    # Calculate metrics
    wins = sum(1 for t in trades if t.exit_reason == 'target')
    losses = sum(1 for t in trades if t.exit_reason == 'sl')
    timeouts = sum(1 for t in trades if t.exit_reason == 'timeout')

    total = len(trades)
    win_rate = wins / total if total > 0 else 0.0

    win_pnls = [t.pnl_pct for t in trades if t.exit_reason == 'target']
    loss_pnls = [t.pnl_pct for t in trades if t.exit_reason in ('sl', 'timeout')]

    avg_win_pct = np.mean(win_pnls) if win_pnls else 0.0
    avg_loss_pct = np.mean(loss_pnls) if loss_pnls else 0.0
    avg_days_held = np.mean([t.days_held for t in trades])

    return StockReliability(
        symbol=symbol,
        total_touches=total,
        wins=wins,
        losses=losses,
        timeouts=timeouts,
        win_rate=round(win_rate, 4),
        avg_win_pct=round(avg_win_pct, 2),
        avg_loss_pct=round(avg_loss_pct, 2),
        avg_days_held=round(avg_days_held, 1),
        trades=trades
    )


def get_available_stocks() -> List[str]:
    """Get list of stocks with historical data."""
    if not HISTORICAL_DATA_PATH.exists():
        return []

    stocks = []
    for f in HISTORICAL_DATA_PATH.glob("*_daily.csv"):
        symbol = f.stem.replace("_daily", "")
        stocks.append(symbol)

    return sorted(stocks)


def run_full_backtest(use_fresh_only: bool = True) -> Dict:
    """
    Run backtest on all available stocks.

    Returns:
        Dictionary with results and summary
    """
    stocks = get_available_stocks()
    print(f"Found {len(stocks)} stocks with historical data")
    print(f"Testing period: Last {LOOKBACK_DAYS} days")
    print(f"Touch type: {'Fresh only (10-candle gap)' if use_fresh_only else 'All touches'}")
    print(f"Target: ATR x {ATR_TARGET_MULTIPLIER}")
    print("-" * 60)

    results: Dict[str, StockReliability] = {}
    skipped = 0

    for i, symbol in enumerate(stocks, 1):
        print(f"[{i}/{len(stocks)}] Processing {symbol}...", end=" ")

        result = backtest_stock(symbol, use_fresh_only)

        if result:
            results[symbol] = result
            print(f"Touches: {result.total_touches}, Win Rate: {result.win_rate*100:.1f}%")
        else:
            skipped += 1
            print("SKIP (no data/touches)")

    print("-" * 60)
    print(f"Completed: {len(results)} stocks, Skipped: {skipped}")

    # Build output
    output = {
        "metadata": {
            "generated_at": datetime.now().isoformat(),
            "lookback_days": LOOKBACK_DAYS,
            "atr_target_multiplier": ATR_TARGET_MULTIPLIER,
            "max_hold_days": MAX_HOLD_DAYS,
            "touch_type": "fresh_only" if use_fresh_only else "all",
            "total_stocks": len(results)
        },
        "summary": {},
        "stocks": {}
    }

    # Calculate summary stats
    if results:
        all_win_rates = [r.win_rate for r in results.values()]
        all_trades = sum(r.total_touches for r in results.values())
        all_wins = sum(r.wins for r in results.values())

        output["summary"] = {
            "total_stocks_tested": len(results),
            "total_trades": all_trades,
            "total_wins": all_wins,
            "overall_win_rate": round(all_wins / all_trades, 4) if all_trades > 0 else 0,
            "avg_stock_win_rate": round(np.mean(all_win_rates), 4),
            "median_stock_win_rate": round(np.median(all_win_rates), 4),
            "best_stock": max(results.items(), key=lambda x: x[1].win_rate)[0],
            "worst_stock": min(results.items(), key=lambda x: x[1].win_rate)[0]
        }

    # Add stock-level results (without individual trades for cleaner output)
    for symbol, result in sorted(results.items(), key=lambda x: x[1].win_rate, reverse=True):
        output["stocks"][symbol] = {
            "total_touches": result.total_touches,
            "wins": result.wins,
            "losses": result.losses,
            "timeouts": result.timeouts,
            "win_rate": result.win_rate,
            "avg_win_pct": result.avg_win_pct,
            "avg_loss_pct": result.avg_loss_pct,
            "avg_days_held": result.avg_days_held
        }

    return output


def save_results(output: Dict):
    """Save results to JSON file."""
    with open(OUTPUT_PATH, 'w') as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to: {OUTPUT_PATH}")


def print_summary(output: Dict):
    """Print summary statistics."""
    summary = output.get("summary", {})
    stocks = output.get("stocks", {})

    print("\n" + "=" * 60)
    print("SUPERTREND TOUCH RELIABILITY - SUMMARY")
    print("=" * 60)

    print(f"\nOverall Statistics:")
    print(f"  Stocks tested: {summary.get('total_stocks_tested', 0)}")
    print(f"  Total trades: {summary.get('total_trades', 0)}")
    print(f"  Overall win rate: {summary.get('overall_win_rate', 0)*100:.1f}%")
    print(f"  Average stock win rate: {summary.get('avg_stock_win_rate', 0)*100:.1f}%")
    print(f"  Median stock win rate: {summary.get('median_stock_win_rate', 0)*100:.1f}%")

    print(f"\nBest Performers:")
    top_5 = list(stocks.items())[:5]
    for symbol, data in top_5:
        print(f"  {symbol}: {data['win_rate']*100:.1f}% ({data['wins']}/{data['total_touches']})")

    print(f"\nWorst Performers:")
    bottom_5 = list(stocks.items())[-5:]
    for symbol, data in reversed(bottom_5):
        print(f"  {symbol}: {data['win_rate']*100:.1f}% ({data['wins']}/{data['total_touches']})")

    # Distribution
    print(f"\nWin Rate Distribution:")
    bins = [(0.6, 1.0, "60%+"), (0.5, 0.6, "50-60%"), (0.4, 0.5, "40-50%"),
            (0.3, 0.4, "30-40%"), (0.0, 0.3, "<30%")]

    for low, high, label in bins:
        count = sum(1 for s in stocks.values() if low <= s['win_rate'] < high)
        print(f"  {label}: {count} stocks")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SuperTrend Touch Reliability Backtest")
    parser.add_argument("--all-touches", action="store_true",
                        help="Test all touches, not just fresh ones")
    parser.add_argument("--symbol", type=str,
                        help="Test single symbol only")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS,
                        help=f"Lookback days (default: {LOOKBACK_DAYS})")

    args = parser.parse_args()

    # Override lookback if specified
    if args.days != LOOKBACK_DAYS:
        LOOKBACK_DAYS = args.days

    use_fresh = not args.all_touches

    if args.symbol:
        # Single stock test
        print(f"Testing {args.symbol}...")
        result = backtest_stock(args.symbol, use_fresh)
        if result:
            print(f"\n{result.symbol} Results:")
            print(f"  Touches: {result.total_touches}")
            print(f"  Wins: {result.wins} | Losses: {result.losses} | Timeouts: {result.timeouts}")
            print(f"  Win Rate: {result.win_rate*100:.1f}%")
            print(f"  Avg Win: +{result.avg_win_pct:.2f}% | Avg Loss: {result.avg_loss_pct:.2f}%")
            print(f"  Avg Days Held: {result.avg_days_held:.1f}")
            print(f"\nTrades:")
            for t in result.trades:
                print(f"  {t.entry_date} -> {t.exit_date}: {t.exit_reason.upper()} ({t.pnl_pct:+.2f}%)")
        else:
            print("No trades found")
    else:
        # Full backtest
        output = run_full_backtest(use_fresh)
        save_results(output)
        print_summary(output)
