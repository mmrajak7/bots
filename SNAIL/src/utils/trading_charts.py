"""
SNAIL Trading Charts Module

Generate performance charts from actual trading data for Telegram reports.

@file        trading_charts.py
@description Chart generation from snail.db trading data
@author      SNAIL Development Team
@created     2025-12-20
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from loguru import logger

try:
    import matplotlib
    matplotlib.use('Agg')  # Non-interactive backend for server use
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    plt = None  # type: ignore[assignment]
    mdates = None  # type: ignore[assignment]
    Axes = None  # type: ignore[assignment,misc]
    Figure = None  # type: ignore[assignment,misc]
    logger.warning("matplotlib not installed - charts will not be generated")


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class TradeData:
    """Single trade record from database."""
    position_id: int
    entry_time: datetime
    exit_time: Optional[datetime]
    entry_day: str  # Monday, Tuesday, etc.
    net_pnl: float
    pnl_percent: float
    max_profit: float
    max_loss: float
    margin_deployed: float
    exit_reason: Optional[str]


@dataclass
class ChartPaths:
    """Paths to generated chart files."""
    cumulative_pnl: Optional[Path] = None
    drawdown: Optional[Path] = None
    capital_growth: Optional[Path] = None
    monthly_metrics: Optional[Path] = None
    yearly_metrics: Optional[Path] = None


# =============================================================================
# TRADING CHARTS CLASS
# =============================================================================

class TradingCharts:
    """
    Generate trading performance charts from database data.

    Charts generated:
    1. Cumulative P&L over time
    2. Drawdown chart
    3. Capital growth (starting capital + P&L)
    4. Monthly P&L metrics (bar chart)
    5. Yearly P&L metrics (bar chart)
    """

    def __init__(self, db_path: Optional[Path] = None, output_dir: Optional[Path] = None):
        """
        Initialize chart generator.

        Args:
            db_path: Path to snail.db (auto-detected if None)
            output_dir: Directory for chart output (default: data/charts)
        """
        if not MATPLOTLIB_AVAILABLE:
            raise ImportError("matplotlib is required for chart generation")

        if db_path is None:
            project_root = Path(__file__).parent.parent.parent
            db_path = project_root / "data" / "snail.db"

        self.db_path = db_path

        if output_dir is None:
            self.output_dir = db_path.parent / "charts"
        else:
            self.output_dir = output_dir

        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Chart styling
        plt.style.use('seaborn-v0_8-darkgrid')
        self.colors = {
            'profit': '#2ecc71',
            'loss': '#e74c3c',
            'neutral': '#3498db',
            'background': '#1a1a2e',
            'text': '#ffffff',
            'grid': '#404040'
        }

        logger.info(f"TradingCharts initialized. DB: {db_path}, Output: {self.output_dir}")

    # =========================================================================
    # DATA LOADING
    # =========================================================================

    def _load_closed_trades(self) -> List[TradeData]:
        """Load all closed trades from database."""
        if not self.db_path.exists():
            logger.warning(f"Database not found: {self.db_path}")
            return []

        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute('''
                SELECT id, entry_time, exit_time, net_pnl, pnl_percent,
                       max_profit, max_loss, margin_deployed, exit_reason
                FROM positions
                WHERE status = 'closed' AND net_pnl IS NOT NULL
                ORDER BY entry_time
            ''')

            trades: List[TradeData] = []
            for row in cursor.fetchall():
                try:
                    entry_time = datetime.fromisoformat(row[1])
                    exit_time = datetime.fromisoformat(row[2]) if row[2] else None
                    entry_day = entry_time.strftime('%A')

                    trades.append(TradeData(
                        position_id=row[0],
                        entry_time=entry_time,
                        exit_time=exit_time,
                        entry_day=entry_day,
                        net_pnl=float(row[3] or 0),
                        pnl_percent=float(row[4] or 0),
                        max_profit=float(row[5] or 0),
                        max_loss=float(row[6] or 0),
                        margin_deployed=float(row[7] or 100000),
                        exit_reason=row[8]
                    ))
                except (ValueError, TypeError) as e:
                    logger.warning(f"Skipping malformed trade row {row[0]}: {e}")
                    continue

            logger.info(f"Loaded {len(trades)} closed trades from database")
            return trades

        except sqlite3.Error as e:
            logger.error(f"Database error loading trades: {e}")
            return []
        except Exception as e:
            logger.error(f"Error loading trades: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def _calculate_metrics(self, trades: List[TradeData]) -> Dict[str, Any]:
        """Calculate trading metrics from trade data."""
        if not trades:
            return {}

        pnls: List[float] = [t.net_pnl for t in trades]
        cumulative_pnl: List[float] = []
        running_total: float = 0.0
        for pnl in pnls:
            running_total += pnl
            cumulative_pnl.append(running_total)

        # Calculate drawdown (from equity peak, not just positive P&L)
        peak: float = 0.0
        drawdowns: List[float] = []
        for cum_pnl in cumulative_pnl:
            if cum_pnl > peak:
                peak = cum_pnl
            # Drawdown is always the drop from peak (even if peak is 0)
            dd = peak - cum_pnl
            drawdowns.append(dd)

        # Monthly breakdown
        monthly: Dict[str, Dict[str, Any]] = {}
        for trade in trades:
            month_key = trade.entry_time.strftime('%Y-%m')
            if month_key not in monthly:
                monthly[month_key] = {'pnl': 0.0, 'trades': 0, 'wins': 0}
            monthly[month_key]['pnl'] += trade.net_pnl
            monthly[month_key]['trades'] += 1
            if trade.net_pnl > 0:
                monthly[month_key]['wins'] += 1

        # Yearly breakdown
        yearly: Dict[str, Dict[str, Any]] = {}
        for trade in trades:
            year_key = str(trade.entry_time.year)
            if year_key not in yearly:
                yearly[year_key] = {'pnl': 0.0, 'trades': 0, 'wins': 0}
            yearly[year_key]['pnl'] += trade.net_pnl
            yearly[year_key]['trades'] += 1
            if trade.net_pnl > 0:
                yearly[year_key]['wins'] += 1

        return {
            'trades': trades,
            'pnls': pnls,
            'cumulative_pnl': cumulative_pnl,
            'drawdowns': drawdowns,
            'monthly': monthly,
            'yearly': yearly,
            'total_pnl': sum(pnls),
            'max_drawdown': max(drawdowns) if drawdowns else 0.0,
            'win_rate': sum(1 for p in pnls if p > 0) / len(pnls) * 100 if pnls else 0.0
        }

    # =========================================================================
    # CHART GENERATION
    # =========================================================================

    def _setup_dark_theme(self, fig: Any, ax: Any) -> None:
        """Apply dark theme to chart."""
        fig.patch.set_facecolor(self.colors['background'])
        ax.set_facecolor(self.colors['background'])
        ax.tick_params(colors=self.colors['text'])
        ax.xaxis.label.set_color(self.colors['text'])
        ax.yaxis.label.set_color(self.colors['text'])
        ax.title.set_color(self.colors['text'])
        for spine in ax.spines.values():
            spine.set_color(self.colors['grid'])
        ax.grid(True, color=self.colors['grid'], alpha=0.3)

    def generate_cumulative_pnl_chart(
        self,
        trades: List[TradeData],
        metrics: Dict[str, Any]
    ) -> Optional[Path]:
        """Generate cumulative P&L chart."""
        if not trades or not metrics.get('cumulative_pnl'):
            logger.warning("No data for cumulative P&L chart")
            return None

        fig, ax = plt.subplots(figsize=(12, 6))
        self._setup_dark_theme(fig, ax)

        dates = [t.exit_time or t.entry_time for t in trades]
        cum_pnl = metrics['cumulative_pnl']

        ax.plot(dates, cum_pnl, color=self.colors['neutral'], linewidth=2, alpha=0.8)
        ax.fill_between(dates, 0, cum_pnl,
                       where=[p >= 0 for p in cum_pnl],
                       color=self.colors['profit'], alpha=0.3)
        ax.fill_between(dates, 0, cum_pnl,
                       where=[p < 0 for p in cum_pnl],
                       color=self.colors['loss'], alpha=0.3)

        ax.axhline(y=0, color=self.colors['text'], linestyle='--', alpha=0.5)

        ax.set_title('SNAIL - Cumulative P&L', fontsize=14, fontweight='bold')
        ax.set_xlabel('Date')
        ax.set_ylabel('P&L (INR)')

        # Format x-axis dates
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %Y'))
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        plt.xticks(rotation=45)

        # Add summary text
        total_pnl = metrics['total_pnl']
        win_rate = metrics['win_rate']
        summary = f"Total: {'+'if total_pnl >= 0 else ''}₹{total_pnl:,.0f} | Win Rate: {win_rate:.1f}%"
        ax.text(0.02, 0.98, summary, transform=ax.transAxes, fontsize=10,
               verticalalignment='top', color=self.colors['text'],
               bbox=dict(boxstyle='round', facecolor=self.colors['background'], alpha=0.8))

        plt.tight_layout()

        output_path = self.output_dir / "cumulative_pnl.png"
        fig.savefig(output_path, dpi=150, facecolor=self.colors['background'])
        plt.close(fig)

        logger.info(f"Cumulative P&L chart saved: {output_path}")
        return output_path

    def generate_drawdown_chart(
        self,
        trades: List[TradeData],
        metrics: Dict[str, Any]
    ) -> Optional[Path]:
        """Generate drawdown chart."""
        if not trades or not metrics.get('drawdowns'):
            logger.warning("No data for drawdown chart")
            return None

        fig, ax = plt.subplots(figsize=(12, 6))
        self._setup_dark_theme(fig, ax)

        dates = [t.exit_time or t.entry_time for t in trades]
        drawdowns = [-d for d in metrics['drawdowns']]  # Negative for display

        ax.fill_between(dates, 0, drawdowns, color=self.colors['loss'], alpha=0.6)
        ax.plot(dates, drawdowns, color=self.colors['loss'], linewidth=1.5)

        ax.axhline(y=0, color=self.colors['text'], linestyle='-', alpha=0.5)

        ax.set_title('SNAIL - Drawdown', fontsize=14, fontweight='bold')
        ax.set_xlabel('Date')
        ax.set_ylabel('Drawdown (INR)')

        ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %Y'))
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        plt.xticks(rotation=45)

        max_dd = metrics['max_drawdown']
        ax.text(0.02, 0.02, f"Max Drawdown: ₹{max_dd:,.0f}", transform=ax.transAxes,
               fontsize=10, color=self.colors['text'],
               bbox=dict(boxstyle='round', facecolor=self.colors['background'], alpha=0.8))

        plt.tight_layout()

        output_path = self.output_dir / "drawdown.png"
        fig.savefig(output_path, dpi=150, facecolor=self.colors['background'])
        plt.close(fig)

        logger.info(f"Drawdown chart saved: {output_path}")
        return output_path

    def generate_capital_growth_chart(
        self,
        trades: List[TradeData],
        metrics: Dict[str, Any],
        starting_capital: float = 100000
    ) -> Optional[Path]:
        """Generate capital growth chart."""
        if not trades or not metrics.get('cumulative_pnl'):
            logger.warning("No data for capital growth chart")
            return None

        if starting_capital <= 0:
            logger.warning("Invalid starting capital, using default 100000")
            starting_capital = 100000.0

        fig, ax = plt.subplots(figsize=(12, 6))
        self._setup_dark_theme(fig, ax)

        dates = [t.exit_time or t.entry_time for t in trades]
        capital = [starting_capital + p for p in metrics['cumulative_pnl']]

        ax.plot(dates, capital, color=self.colors['neutral'], linewidth=2)
        ax.fill_between(dates, starting_capital, capital,
                       where=[c >= starting_capital for c in capital],
                       color=self.colors['profit'], alpha=0.3)
        ax.fill_between(dates, starting_capital, capital,
                       where=[c < starting_capital for c in capital],
                       color=self.colors['loss'], alpha=0.3)

        ax.axhline(y=starting_capital, color=self.colors['text'], linestyle='--', alpha=0.5)

        ax.set_title('SNAIL - Capital Growth', fontsize=14, fontweight='bold')
        ax.set_xlabel('Date')
        ax.set_ylabel('Capital (INR)')

        ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %Y'))
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        plt.xticks(rotation=45)

        final_capital = capital[-1] if capital else starting_capital
        growth_pct = ((final_capital / starting_capital) - 1) * 100
        summary = f"Start: ₹{starting_capital:,.0f} | Current: ₹{final_capital:,.0f} | Growth: {growth_pct:+.1f}%"
        ax.text(0.02, 0.98, summary, transform=ax.transAxes, fontsize=10,
               verticalalignment='top', color=self.colors['text'],
               bbox=dict(boxstyle='round', facecolor=self.colors['background'], alpha=0.8))

        plt.tight_layout()

        output_path = self.output_dir / "capital_growth.png"
        fig.savefig(output_path, dpi=150, facecolor=self.colors['background'])
        plt.close(fig)

        logger.info(f"Capital growth chart saved: {output_path}")
        return output_path

    def generate_monthly_metrics_chart(
        self,
        metrics: Dict[str, Any]
    ) -> Optional[Path]:
        """Generate monthly P&L bar chart."""
        monthly = metrics.get('monthly', {})
        if not monthly:
            logger.warning("No monthly data for chart")
            return None

        fig, ax = plt.subplots(figsize=(14, 6))
        self._setup_dark_theme(fig, ax)

        months = list(monthly.keys())
        pnls = [monthly[m]['pnl'] for m in months]

        colors = [self.colors['profit'] if p >= 0 else self.colors['loss'] for p in pnls]
        bars = ax.bar(months, pnls, color=colors, alpha=0.8, edgecolor='white', linewidth=0.5)

        ax.axhline(y=0, color=self.colors['text'], linestyle='-', alpha=0.5)

        ax.set_title('SNAIL - Monthly P&L', fontsize=14, fontweight='bold')
        ax.set_xlabel('Month')
        ax.set_ylabel('P&L (INR)')

        plt.xticks(rotation=45, ha='right')

        # Add value labels on bars
        for bar, pnl in zip(bars, pnls):
            height = bar.get_height()
            va = 'bottom' if height >= 0 else 'top'
            offset = 100 if height >= 0 else -100
            ax.annotate(f'₹{pnl:,.0f}',
                       xy=(bar.get_x() + bar.get_width() / 2, height),
                       xytext=(0, offset / 50),
                       textcoords="offset points",
                       ha='center', va=va, fontsize=8, color=self.colors['text'])

        plt.tight_layout()

        output_path = self.output_dir / "monthly_metrics.png"
        fig.savefig(output_path, dpi=150, facecolor=self.colors['background'])
        plt.close(fig)

        logger.info(f"Monthly metrics chart saved: {output_path}")
        return output_path

    def generate_yearly_metrics_chart(
        self,
        metrics: Dict[str, Any]
    ) -> Optional[Path]:
        """Generate yearly P&L bar chart with win rate."""
        yearly = metrics.get('yearly', {})
        if not yearly:
            logger.warning("No yearly data for chart")
            return None

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
        self._setup_dark_theme(fig, ax1)
        self._setup_dark_theme(fig, ax2)

        years = list(yearly.keys())
        pnls = [yearly[y]['pnl'] for y in years]
        win_rates = [(yearly[y]['wins'] / yearly[y]['trades'] * 100) if yearly[y]['trades'] > 0 else 0
                    for y in years]
        trade_counts = [yearly[y]['trades'] for y in years]

        # P&L chart
        colors = [self.colors['profit'] if p >= 0 else self.colors['loss'] for p in pnls]
        bars1 = ax1.bar(years, pnls, color=colors, alpha=0.8, edgecolor='white', linewidth=0.5)
        ax1.axhline(y=0, color=self.colors['text'], linestyle='-', alpha=0.5)
        ax1.set_title('Yearly P&L', fontsize=12, fontweight='bold')
        ax1.set_ylabel('P&L (INR)')

        for bar, pnl in zip(bars1, pnls):
            height = bar.get_height()
            va = 'bottom' if height >= 0 else 'top'
            ax1.annotate(f'₹{pnl:,.0f}',
                        xy=(bar.get_x() + bar.get_width() / 2, height),
                        xytext=(0, 3 if height >= 0 else -3),
                        textcoords="offset points",
                        ha='center', va=va, fontsize=9, color=self.colors['text'])

        # Win rate chart
        bars2 = ax2.bar(years, win_rates, color=self.colors['neutral'], alpha=0.8)
        ax2.axhline(y=50, color=self.colors['text'], linestyle='--', alpha=0.5)
        ax2.set_title('Win Rate & Trade Count', fontsize=12, fontweight='bold')
        ax2.set_ylabel('Win Rate (%)')
        ax2.set_ylim(0, 100)

        for bar, wr, tc in zip(bars2, win_rates, trade_counts):
            ax2.annotate(f'{wr:.0f}%\n({tc} trades)',
                        xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
                        xytext=(0, 3),
                        textcoords="offset points",
                        ha='center', va='bottom', fontsize=9, color=self.colors['text'])

        fig.suptitle('SNAIL - Yearly Performance', fontsize=14, fontweight='bold',
                    color=self.colors['text'])
        plt.tight_layout()

        output_path = self.output_dir / "yearly_metrics.png"
        fig.savefig(output_path, dpi=150, facecolor=self.colors['background'])
        plt.close(fig)

        logger.info(f"Yearly metrics chart saved: {output_path}")
        return output_path

    # =========================================================================
    # MAIN GENERATION
    # =========================================================================

    def generate_all_charts(self, starting_capital: float = 100000) -> ChartPaths:
        """
        Generate all trading charts.

        Args:
            starting_capital: Starting capital for growth chart

        Returns:
            ChartPaths with paths to generated files
        """
        trades = self._load_closed_trades()

        if not trades:
            logger.warning("No closed trades found - generating empty charts notification")
            return ChartPaths()

        metrics = self._calculate_metrics(trades)

        return ChartPaths(
            cumulative_pnl=self.generate_cumulative_pnl_chart(trades, metrics),
            drawdown=self.generate_drawdown_chart(trades, metrics),
            capital_growth=self.generate_capital_growth_chart(trades, metrics, starting_capital),
            monthly_metrics=self.generate_monthly_metrics_chart(metrics),
            yearly_metrics=self.generate_yearly_metrics_chart(metrics)
        )

    def get_chart_paths(self) -> List[Path]:
        """
        Get list of all existing chart file paths.

        Returns:
            List of Path objects for existing charts
        """
        paths = []
        chart_files = [
            "cumulative_pnl.png",
            "drawdown.png",
            "capital_growth.png",
            "monthly_metrics.png",
            "yearly_metrics.png"
        ]

        for filename in chart_files:
            path = self.output_dir / filename
            if path.exists():
                paths.append(path)

        return paths


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def generate_friday_charts(
    db_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    starting_capital: float = 100000
) -> ChartPaths:
    """
    Generate all charts for Friday summary.

    Args:
        db_path: Path to database
        output_dir: Output directory for charts
        starting_capital: Starting capital for growth calculation

    Returns:
        ChartPaths with generated chart paths
    """
    try:
        charts = TradingCharts(db_path, output_dir)
        return charts.generate_all_charts(starting_capital)
    except Exception as e:
        logger.error(f"Failed to generate Friday charts: {e}")
        return ChartPaths()


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    import sys
    from dotenv import load_dotenv

    load_dotenv()

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    print("\n" + "=" * 60)
    print("SNAIL Trading Charts Generator")
    print("=" * 60)

    try:
        charts = TradingCharts()
        chart_paths = charts.generate_all_charts()

        print("\nGenerated Charts:")
        if chart_paths.cumulative_pnl:
            print(f"  1. Cumulative P&L: {chart_paths.cumulative_pnl}")
        if chart_paths.drawdown:
            print(f"  2. Drawdown: {chart_paths.drawdown}")
        if chart_paths.capital_growth:
            print(f"  3. Capital Growth: {chart_paths.capital_growth}")
        if chart_paths.monthly_metrics:
            print(f"  4. Monthly Metrics: {chart_paths.monthly_metrics}")
        if chart_paths.yearly_metrics:
            print(f"  5. Yearly Metrics: {chart_paths.yearly_metrics}")

        if not any([chart_paths.cumulative_pnl, chart_paths.drawdown,
                   chart_paths.capital_growth, chart_paths.monthly_metrics,
                   chart_paths.yearly_metrics]):
            print("\n  No charts generated - no closed trades in database")

        print("\n" + "=" * 60)

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
