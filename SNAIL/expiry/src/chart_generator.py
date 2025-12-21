"""
Chart Generator for Expiry Trading System

Generates P&L charts for Telegram updates.

@file        chart_generator.py
@description Dark theme P&L chart generation
"""

from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from loguru import logger

try:
    import matplotlib
    matplotlib.use('Agg')  # Non-interactive backend
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.patches import Rectangle
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    logger.warning("matplotlib not installed - charts disabled")


@dataclass
class PnLPoint:
    """Single P&L data point."""
    timestamp: datetime
    pnl: float
    spot: float


@dataclass
class ChartData:
    """Data for chart generation."""
    entry_time: datetime
    spot_at_entry: float
    wing_distance: int
    target_pnl: float
    stop_loss_pnl: float
    total_credit: float
    pnl_history: List[PnLPoint] = field(default_factory=list)


class ChartGenerator:
    """
    Generate P&L charts with dark theme.

    Charts show:
    - Intraday P&L line (green/red)
    - Target line (dashed green)
    - Stop loss line (dashed red)
    - Current time marker
    - Wing distance indicators
    """

    def __init__(self, output_dir: Path):
        """
        Initialize chart generator.

        Args:
            output_dir: Directory for chart output
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Dark theme colors
        self.colors = {
            'background': '#1a1a2e',
            'text': '#ffffff',
            'grid': '#404040',
            'profit': '#2ecc71',
            'loss': '#e74c3c',
            'target': '#27ae60',
            'stoploss': '#c0392b',
            'neutral': '#3498db',
            'warning': '#f39c12'
        }

        if MATPLOTLIB_AVAILABLE:
            plt.style.use('seaborn-v0_8-darkgrid')

    def add_pnl_point(
        self,
        chart_data: ChartData,
        pnl: float,
        spot: float,
        timestamp: Optional[datetime] = None
    ) -> ChartData:
        """
        Add P&L data point.

        Args:
            chart_data: Existing chart data
            pnl: Current P&L
            spot: Current spot price
            timestamp: Timestamp (defaults to now)

        Returns:
            Updated chart data
        """
        if timestamp is None:
            timestamp = datetime.now()

        chart_data.pnl_history.append(PnLPoint(
            timestamp=timestamp,
            pnl=pnl,
            spot=spot
        ))

        return chart_data

    def generate_pnl_chart(
        self,
        chart_data: ChartData,
        current_pnl: float,
        current_spot: float
    ) -> Optional[Path]:
        """
        Generate P&L chart.

        Args:
            chart_data: Chart data with history
            current_pnl: Current P&L
            current_spot: Current spot price

        Returns:
            Path to generated chart image
        """
        if not MATPLOTLIB_AVAILABLE:
            logger.warning("matplotlib not available, skipping chart")
            return None

        if len(chart_data.pnl_history) < 2:
            logger.warning("Not enough data points for chart")
            return None

        try:
            fig, (ax1, ax2) = plt.subplots(
                2, 1, figsize=(10, 8),
                gridspec_kw={'height_ratios': [3, 1]}
            )

            # Apply dark theme
            fig.patch.set_facecolor(self.colors['background'])
            for ax in [ax1, ax2]:
                ax.set_facecolor(self.colors['background'])
                ax.tick_params(colors=self.colors['text'])
                ax.xaxis.label.set_color(self.colors['text'])
                ax.yaxis.label.set_color(self.colors['text'])
                for spine in ax.spines.values():
                    spine.set_color(self.colors['grid'])

            # Extract data
            times = [p.timestamp for p in chart_data.pnl_history]
            pnls = [p.pnl for p in chart_data.pnl_history]
            spots = [p.spot for p in chart_data.pnl_history]

            # P&L Chart (top)
            ax1.set_title(
                f"Expiry Iron Condor - {chart_data.entry_time.strftime('%d %b %Y')}",
                fontsize=14, fontweight='bold', color=self.colors['text']
            )

            # P&L line with color based on value
            for i in range(1, len(times)):
                color = self.colors['profit'] if pnls[i] >= 0 else self.colors['loss']
                ax1.plot(
                    [times[i-1], times[i]], [pnls[i-1], pnls[i]],
                    color=color, linewidth=2
                )

            # Fill area
            ax1.fill_between(
                times, 0, pnls,
                where=[p >= 0 for p in pnls],
                color=self.colors['profit'], alpha=0.2
            )
            ax1.fill_between(
                times, 0, pnls,
                where=[p < 0 for p in pnls],
                color=self.colors['loss'], alpha=0.2
            )

            # Target line
            ax1.axhline(
                y=chart_data.target_pnl, color=self.colors['target'],
                linestyle='--', linewidth=1.5, alpha=0.8,
                label=f'Target: Rs.{chart_data.target_pnl:,.0f}'
            )

            # Stop loss line
            ax1.axhline(
                y=chart_data.stop_loss_pnl, color=self.colors['stoploss'],
                linestyle='--', linewidth=1.5, alpha=0.8,
                label=f'SL: Rs.{chart_data.stop_loss_pnl:,.0f}'
            )

            # Zero line
            ax1.axhline(y=0, color=self.colors['text'], linestyle='-', linewidth=0.5, alpha=0.5)

            # Current P&L marker
            ax1.scatter(
                [times[-1]], [pnls[-1]],
                color=self.colors['profit'] if pnls[-1] >= 0 else self.colors['loss'],
                s=100, zorder=5, edgecolors='white', linewidth=2
            )

            # Current P&L annotation
            pnl_sign = "+" if current_pnl >= 0 else ""
            ax1.annotate(
                f'{pnl_sign}Rs.{current_pnl:,.0f}',
                xy=(times[-1], pnls[-1]),
                xytext=(10, 10), textcoords='offset points',
                fontsize=12, fontweight='bold',
                color=self.colors['profit'] if current_pnl >= 0 else self.colors['loss'],
                bbox=dict(boxstyle='round', facecolor=self.colors['background'], alpha=0.8)
            )

            ax1.set_ylabel('P&L (Rs.)', fontsize=10, color=self.colors['text'])
            ax1.legend(loc='upper left', facecolor=self.colors['background'], labelcolor=self.colors['text'])
            ax1.grid(True, color=self.colors['grid'], alpha=0.3)

            # Format x-axis
            ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            ax1.xaxis.set_major_locator(mdates.MinuteLocator(interval=30))

            # Spot Chart (bottom)
            ax2.plot(times, spots, color=self.colors['neutral'], linewidth=1.5)

            # Wing lines
            atm = chart_data.spot_at_entry
            ce_wing = atm + chart_data.wing_distance
            pe_wing = atm - chart_data.wing_distance

            ax2.axhline(y=ce_wing, color=self.colors['warning'], linestyle='--', linewidth=1, alpha=0.7)
            ax2.axhline(y=pe_wing, color=self.colors['warning'], linestyle='--', linewidth=1, alpha=0.7)
            ax2.axhline(y=atm, color=self.colors['text'], linestyle=':', linewidth=1, alpha=0.5)

            # Add wing labels
            ax2.text(
                times[0], ce_wing, f' CE Wing {ce_wing:.0f}',
                color=self.colors['warning'], fontsize=8, va='bottom'
            )
            ax2.text(
                times[0], pe_wing, f' PE Wing {pe_wing:.0f}',
                color=self.colors['warning'], fontsize=8, va='top'
            )

            ax2.set_ylabel('NIFTY', fontsize=10, color=self.colors['text'])
            ax2.set_xlabel('Time', fontsize=10, color=self.colors['text'])
            ax2.grid(True, color=self.colors['grid'], alpha=0.3)

            ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            ax2.xaxis.set_major_locator(mdates.MinuteLocator(interval=30))

            plt.tight_layout()

            # Save chart
            timestamp = datetime.now().strftime('%H%M%S')
            output_path = self.output_dir / f"pnl_chart_{timestamp}.png"
            fig.savefig(output_path, dpi=150, facecolor=self.colors['background'])
            plt.close(fig)

            logger.info(f"Chart saved: {output_path}")
            return output_path

        except Exception as e:
            logger.error(f"Chart generation failed: {e}")
            return None

    def generate_summary_chart(
        self,
        chart_data: ChartData,
        final_pnl: float,
        exit_reason: str
    ) -> Optional[Path]:
        """
        Generate final summary chart.

        Args:
            chart_data: Chart data with complete history
            final_pnl: Final P&L
            exit_reason: Reason for exit

        Returns:
            Path to generated chart image
        """
        if not MATPLOTLIB_AVAILABLE:
            return None

        if len(chart_data.pnl_history) < 2:
            return None

        try:
            fig, ax = plt.subplots(figsize=(12, 6))

            # Apply dark theme
            fig.patch.set_facecolor(self.colors['background'])
            ax.set_facecolor(self.colors['background'])
            ax.tick_params(colors=self.colors['text'])
            for spine in ax.spines.values():
                spine.set_color(self.colors['grid'])

            # Extract data
            times = [p.timestamp for p in chart_data.pnl_history]
            pnls = [p.pnl for p in chart_data.pnl_history]

            # P&L line
            color = self.colors['profit'] if final_pnl >= 0 else self.colors['loss']
            ax.plot(times, pnls, color=color, linewidth=2.5)

            # Fill
            ax.fill_between(
                times, 0, pnls,
                where=[p >= 0 for p in pnls],
                color=self.colors['profit'], alpha=0.3
            )
            ax.fill_between(
                times, 0, pnls,
                where=[p < 0 for p in pnls],
                color=self.colors['loss'], alpha=0.3
            )

            # Target and SL lines
            ax.axhline(y=chart_data.target_pnl, color=self.colors['target'], linestyle='--', linewidth=1.5)
            ax.axhline(y=chart_data.stop_loss_pnl, color=self.colors['stoploss'], linestyle='--', linewidth=1.5)
            ax.axhline(y=0, color=self.colors['text'], linestyle='-', linewidth=0.5, alpha=0.5)

            # Title with result
            pnl_sign = "+" if final_pnl >= 0 else ""
            result_emoji = "WIN" if final_pnl >= 0 else "LOSS"
            ax.set_title(
                f"Expiry Iron Condor - {result_emoji} | {pnl_sign}Rs.{final_pnl:,.0f} | {exit_reason}",
                fontsize=14, fontweight='bold', color=color
            )

            ax.set_ylabel('P&L (Rs.)', fontsize=10, color=self.colors['text'])
            ax.set_xlabel('Time', fontsize=10, color=self.colors['text'])
            ax.grid(True, color=self.colors['grid'], alpha=0.3)

            ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            ax.xaxis.set_major_locator(mdates.MinuteLocator(interval=30))

            plt.tight_layout()

            # Save
            output_path = self.output_dir / f"summary_chart_{datetime.now().strftime('%Y%m%d')}.png"
            fig.savefig(output_path, dpi=150, facecolor=self.colors['background'])
            plt.close(fig)

            logger.info(f"Summary chart saved: {output_path}")
            return output_path

        except Exception as e:
            logger.error(f"Summary chart generation failed: {e}")
            return None

    def cleanup_old_charts(self, keep_days: int = 7) -> int:
        """
        Remove old chart files.

        Args:
            keep_days: Days to keep charts

        Returns:
            Number of files removed
        """
        import time

        removed = 0
        cutoff = time.time() - (keep_days * 24 * 60 * 60)

        for chart_file in self.output_dir.glob("*.png"):
            if chart_file.stat().st_mtime < cutoff:
                chart_file.unlink()
                removed += 1

        if removed > 0:
            logger.info(f"Cleaned up {removed} old chart files")

        return removed
