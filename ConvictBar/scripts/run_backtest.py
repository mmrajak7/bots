#!/usr/bin/env python3
"""
ConvictBar Backtest Runner

Run backtest on historical NIFTY data using the ConvictBar strategy.

Usage:
    python scripts/run_backtest.py
    python scripts/run_backtest.py --start 2025-07-01 --end 2026-01-16
    python scripts/run_backtest.py --days 90
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.config import load_config
from src.backtest.data_loader import DataLoader
from src.backtest.runner import BacktestRunner, run_backtest
from src.backtest.analytics import (
    BacktestAnalytics,
    export_trades_csv,
    export_summary_json,
    print_summary,
)


def main():
    parser = argparse.ArgumentParser(description='Run ConvictBar backtest')
    parser.add_argument('--start', type=str, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, help='End date (YYYY-MM-DD)')
    parser.add_argument('--days', type=int, default=180, help='Days to backtest (default: 180)')
    parser.add_argument('--no-cache', action='store_true', help='Disable data cache')
    parser.add_argument('--output-dir', type=str, default='output', help='Output directory')

    args = parser.parse_args()

    print("=" * 60)
    print("CONVICTBAR BACKTEST")
    print("=" * 60)

    # Determine date range
    if args.end:
        end_date = date.fromisoformat(args.end)
    else:
        end_date = date.today()

    if args.start:
        start_date = date.fromisoformat(args.start)
    else:
        start_date = end_date - timedelta(days=args.days)

    print(f"\nDate Range: {start_date} to {end_date}")
    print(f"Duration: {(end_date - start_date).days} days")

    # Load config
    try:
        config = load_config()
        print(f"\nSymbol: {config.instrument.symbol}")
        print(f"Token: {config.instrument.instrument_token}")
    except Exception as e:
        print(f"ERROR loading config: {e}")
        return 1

    # Load data
    print("\n--- LOADING DATA ---")
    try:
        loader = DataLoader(use_cache=not args.no_cache)
        days = loader.load_data(
            instrument_token=config.instrument.instrument_token,
            start_date=start_date,
            end_date=end_date,
            timeframe="5minute",
        )
    except Exception as e:
        print(f"ERROR loading data: {e}")
        return 1

    if not days:
        print("No data loaded. Check your Kite connection and token.")
        return 1

    print(f"Loaded {len(days)} trading days")

    # Run backtest
    print("\n--- RUNNING BACKTEST ---")
    try:
        runner = BacktestRunner(config)
        result = runner.run(days)
        result.config = {
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat(),
            'symbol': config.instrument.symbol,
            'instrument_token': config.instrument.instrument_token,
        }
    except Exception as e:
        print(f"ERROR during backtest: {e}")
        import traceback
        traceback.print_exc()
        return 1

    # Calculate analytics
    print("\n--- CALCULATING ANALYTICS ---")
    analytics = BacktestAnalytics(result.trade_results)
    metrics = analytics.calculate_metrics()
    monthly_stats = analytics.calculate_monthly_stats()

    # Print summary
    print_summary(metrics)

    # Monthly breakdown
    if monthly_stats:
        print("\n--- MONTHLY BREAKDOWN ---")
        print(f"{'Month':<10} {'Trades':>8} {'Winners':>8} {'PnL':>10} {'Win Rate':>10}")
        print("-" * 48)
        for m in monthly_stats:
            print(f"{m['month']:<10} {m['trades']:>8} {m['winners']:>8} {m['pnl_points']:>10.2f} {m['win_rate']:>9.2f}%")

    # Export results
    output_dir = Path(args.output_dir)

    # Trades CSV
    trades_path = output_dir / 'trades' / f"trades_{start_date}_{end_date}.csv"
    export_trades_csv(result.trade_results, trades_path)

    # Summary JSON
    summary_path = output_dir / 'reports' / f"summary_{start_date}_{end_date}.json"
    export_summary_json(metrics, monthly_stats, result.config, summary_path)

    print(f"\n--- OUTPUT FILES ---")
    print(f"Trades:  {trades_path}")
    print(f"Summary: {summary_path}")

    print("\n" + "=" * 60)
    print("BACKTEST COMPLETE")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
