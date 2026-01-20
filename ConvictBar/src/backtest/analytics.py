"""
Analytics and reporting for backtest results.
Calculates performance metrics and generates reports.
"""

import csv
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
import statistics

from .simulator import TradeResult, ExitReason
from ..core.signals import Direction


@dataclass
class PerformanceMetrics:
    """Comprehensive performance metrics."""
    # Basic stats
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    breakeven_trades: int = 0
    win_rate: float = 0.0

    # P&L stats
    total_pnl_points: float = 0.0
    total_pnl_amount: float = 0.0
    average_pnl_points: float = 0.0
    average_winner: float = 0.0
    average_loser: float = 0.0
    largest_winner: float = 0.0
    largest_loser: float = 0.0

    # Risk metrics
    profit_factor: float = 0.0
    expectancy: float = 0.0
    max_drawdown_points: float = 0.0
    max_drawdown_pct: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0

    # Direction analysis
    long_trades: int = 0
    long_winners: int = 0
    long_win_rate: float = 0.0
    long_pnl: float = 0.0
    short_trades: int = 0
    short_winners: int = 0
    short_win_rate: float = 0.0
    short_pnl: float = 0.0

    # Exit analysis
    target_exits: int = 0
    sl_exits: int = 0
    ft_failure_exits: int = 0
    time_exits: int = 0

    # Time analysis
    avg_bars_held: float = 0.0
    avg_winner_bars: float = 0.0
    avg_loser_bars: float = 0.0

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)


class BacktestAnalytics:
    """
    Analyzes backtest results and generates metrics/reports.
    """

    def __init__(self, trade_results: List[TradeResult]):
        """
        Initialize analytics.

        Args:
            trade_results: List of completed trades
        """
        self.results = trade_results

    def calculate_metrics(self) -> PerformanceMetrics:
        """
        Calculate comprehensive performance metrics.

        Returns:
            PerformanceMetrics object
        """
        metrics = PerformanceMetrics()

        if not self.results:
            return metrics

        # Separate winners and losers
        winners = [r for r in self.results if r.pnl_points > 0]
        losers = [r for r in self.results if r.pnl_points < 0]
        breakeven = [r for r in self.results if r.pnl_points == 0]

        # Basic stats
        metrics.total_trades = len(self.results)
        metrics.winning_trades = len(winners)
        metrics.losing_trades = len(losers)
        metrics.breakeven_trades = len(breakeven)
        metrics.win_rate = (len(winners) / len(self.results) * 100) if self.results else 0.0

        # P&L stats
        metrics.total_pnl_points = sum(r.pnl_points for r in self.results)
        metrics.total_pnl_amount = sum(r.pnl_amount for r in self.results)
        metrics.average_pnl_points = statistics.mean([r.pnl_points for r in self.results]) if self.results else 0.0

        if winners:
            metrics.average_winner = statistics.mean([r.pnl_points for r in winners])
            metrics.largest_winner = max(r.pnl_points for r in winners)
        if losers:
            metrics.average_loser = statistics.mean([r.pnl_points for r in losers])
            metrics.largest_loser = min(r.pnl_points for r in losers)

        # Profit factor
        gross_profit = sum(r.pnl_points for r in winners) if winners else 0
        gross_loss = abs(sum(r.pnl_points for r in losers)) if losers else 0
        metrics.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0

        # Expectancy
        if metrics.total_trades > 0:
            win_rate_decimal = metrics.winning_trades / metrics.total_trades
            avg_win = metrics.average_winner if metrics.average_winner else 0
            avg_loss = abs(metrics.average_loser) if metrics.average_loser else 0
            metrics.expectancy = (win_rate_decimal * avg_win) - ((1 - win_rate_decimal) * avg_loss)

        # Drawdown
        metrics.max_drawdown_points = self._calculate_max_drawdown()

        # Consecutive wins/losses
        metrics.max_consecutive_wins = self._calculate_max_consecutive(True)
        metrics.max_consecutive_losses = self._calculate_max_consecutive(False)

        # Direction analysis
        longs = [r for r in self.results if r.direction == Direction.LONG]
        shorts = [r for r in self.results if r.direction == Direction.SHORT]

        metrics.long_trades = len(longs)
        metrics.long_winners = len([r for r in longs if r.pnl_points > 0])
        metrics.long_win_rate = (metrics.long_winners / len(longs) * 100) if longs else 0.0
        metrics.long_pnl = sum(r.pnl_points for r in longs)

        metrics.short_trades = len(shorts)
        metrics.short_winners = len([r for r in shorts if r.pnl_points > 0])
        metrics.short_win_rate = (metrics.short_winners / len(shorts) * 100) if shorts else 0.0
        metrics.short_pnl = sum(r.pnl_points for r in shorts)

        # Exit analysis
        metrics.target_exits = len([r for r in self.results if r.exit_reason == ExitReason.TARGET])
        metrics.sl_exits = len([r for r in self.results if r.exit_reason == ExitReason.STOP_LOSS])
        metrics.ft_failure_exits = len([r for r in self.results if r.exit_reason == ExitReason.FT_FAILURE])
        metrics.time_exits = len([r for r in self.results if r.exit_reason == ExitReason.TIME_EXIT])

        # Time analysis
        all_bars = [r.bars_held for r in self.results]
        winner_bars = [r.bars_held for r in winners]
        loser_bars = [r.bars_held for r in losers]

        metrics.avg_bars_held = statistics.mean(all_bars) if all_bars else 0.0
        metrics.avg_winner_bars = statistics.mean(winner_bars) if winner_bars else 0.0
        metrics.avg_loser_bars = statistics.mean(loser_bars) if loser_bars else 0.0

        return metrics

    def _calculate_max_drawdown(self) -> float:
        """Calculate maximum peak-to-trough drawdown in points."""
        if not self.results:
            return 0.0

        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0

        for r in self.results:
            cumulative += r.pnl_points
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        return max_dd

    def _calculate_max_consecutive(self, is_win: bool) -> int:
        """Calculate maximum consecutive wins or losses."""
        if not self.results:
            return 0

        max_streak = 0
        current_streak = 0

        for r in self.results:
            is_winner = r.pnl_points > 0
            if is_winner == is_win:
                current_streak += 1
                max_streak = max(max_streak, current_streak)
            else:
                current_streak = 0

        return max_streak

    def calculate_monthly_stats(self) -> List[Dict]:
        """
        Calculate month-by-month performance.

        Returns:
            List of monthly stat dictionaries
        """
        if not self.results:
            return []

        # Group by month
        monthly: Dict[str, List[TradeResult]] = {}
        for r in self.results:
            month_key = r.date.strftime('%Y-%m')
            if month_key not in monthly:
                monthly[month_key] = []
            monthly[month_key].append(r)

        # Calculate stats for each month
        stats = []
        for month, trades in sorted(monthly.items()):
            winners = [t for t in trades if t.pnl_points > 0]
            pnl = sum(t.pnl_points for t in trades)
            win_rate = (len(winners) / len(trades) * 100) if trades else 0.0

            stats.append({
                'month': month,
                'trades': len(trades),
                'winners': len(winners),
                'pnl_points': round(pnl, 2),
                'win_rate': round(win_rate, 2),
            })

        return stats

    def get_equity_curve(self) -> List[Dict]:
        """
        Get equity curve data (cumulative P&L over time).

        Returns:
            List of {date, cumulative_pnl} dicts
        """
        if not self.results:
            return []

        cumulative = 0.0
        curve = []

        for r in self.results:
            cumulative += r.pnl_points
            curve.append({
                'date': r.date.strftime('%Y-%m-%d'),
                'trade_id': r.trade_id,
                'pnl': round(r.pnl_points, 2),
                'cumulative': round(cumulative, 2),
            })

        return curve


def export_trades_csv(results: List[TradeResult], output_path: Path) -> None:
    """
    Export trade results to CSV file.

    Args:
        results: List of trade results
        output_path: Path for output file
    """
    if not results:
        print("No trades to export")
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = list(results[0].to_dict().keys())

    with open(output_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(r.to_dict())

    print(f"Exported {len(results)} trades to {output_path}")


def export_summary_json(
    metrics: PerformanceMetrics,
    monthly_stats: List[Dict],
    config: Dict,
    output_path: Path
) -> None:
    """
    Export summary report to JSON file.

    Args:
        metrics: Performance metrics
        monthly_stats: Monthly breakdown
        config: Backtest configuration
        output_path: Path for output file
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    report = {
        'generated_at': datetime.now().isoformat(),
        'config': config,
        'performance_metrics': metrics.to_dict(),
        'monthly_breakdown': monthly_stats,
    }

    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2)

    print(f"Exported summary to {output_path}")


def print_summary(metrics: PerformanceMetrics) -> None:
    """
    Print formatted summary to console.

    Args:
        metrics: Performance metrics to display
    """
    print("\n" + "=" * 60)
    print("BACKTEST SUMMARY")
    print("=" * 60)

    print(f"\n--- BASIC STATS ---")
    print(f"Total Trades:        {metrics.total_trades}")
    print(f"Winning Trades:      {metrics.winning_trades}")
    print(f"Losing Trades:       {metrics.losing_trades}")
    print(f"Win Rate:            {metrics.win_rate:.2f}%")

    print(f"\n--- P&L ---")
    print(f"Total P&L (points):  {metrics.total_pnl_points:.2f}")
    print(f"Total P&L (INR):     {metrics.total_pnl_amount:.2f}")
    print(f"Avg P&L per Trade:   {metrics.average_pnl_points:.2f} points")
    print(f"Avg Winner:          {metrics.average_winner:.2f} points")
    print(f"Avg Loser:           {metrics.average_loser:.2f} points")
    print(f"Largest Winner:      {metrics.largest_winner:.2f} points")
    print(f"Largest Loser:       {metrics.largest_loser:.2f} points")

    print(f"\n--- RISK METRICS ---")
    print(f"Profit Factor:       {metrics.profit_factor:.2f}")
    print(f"Expectancy:          {metrics.expectancy:.2f} points/trade")
    print(f"Max Drawdown:        {metrics.max_drawdown_points:.2f} points")
    print(f"Max Consec. Wins:    {metrics.max_consecutive_wins}")
    print(f"Max Consec. Losses:  {metrics.max_consecutive_losses}")

    print(f"\n--- DIRECTION ANALYSIS ---")
    print(f"Long Trades:         {metrics.long_trades} (Win Rate: {metrics.long_win_rate:.2f}%, PnL: {metrics.long_pnl:.2f})")
    print(f"Short Trades:        {metrics.short_trades} (Win Rate: {metrics.short_win_rate:.2f}%, PnL: {metrics.short_pnl:.2f})")

    print(f"\n--- EXIT ANALYSIS ---")
    print(f"Target Exits:        {metrics.target_exits}")
    print(f"Stop Loss Exits:     {metrics.sl_exits}")
    print(f"FT Failure Exits:    {metrics.ft_failure_exits}")
    print(f"Time Exits:          {metrics.time_exits}")

    print(f"\n--- TIME ANALYSIS ---")
    print(f"Avg Bars Held:       {metrics.avg_bars_held:.1f}")
    print(f"Avg Winner Bars:     {metrics.avg_winner_bars:.1f}")
    print(f"Avg Loser Bars:      {metrics.avg_loser_bars:.1f}")

    print("\n" + "=" * 60)
