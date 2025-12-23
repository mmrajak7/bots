"""
Chart Generator for Expiry Trading System

Generates P&L charts for Telegram updates.

@file        chart_generator.py
@description Dark theme P&L chart generation
"""

import json
import os
import tempfile
from datetime import datetime, date
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass, field
from loguru import logger

try:
    import matplotlib
    matplotlib.use('Agg')  # Non-interactive backend
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
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
            # Try different style names for compatibility across matplotlib versions
            for style in ['seaborn-v0_8-darkgrid', 'seaborn-darkgrid', 'dark_background']:
                try:
                    plt.style.use(style)
                    break
                except OSError:
                    continue

    def _get_history_file_path(self, for_date: Optional[date] = None) -> Path:
        """Get path to P&L history file for a specific date."""
        if for_date is None:
            for_date = date.today()
        return self.output_dir / f"pnl_history_{for_date.isoformat()}.json"

    def save_pnl_history(self, chart_data: ChartData) -> bool:
        """
        Save P&L history to file atomically for persistence across script invocations.

        Uses write-to-temp-then-rename pattern to prevent corruption.

        Args:
            chart_data: Chart data with P&L history

        Returns:
            True if saved successfully
        """
        try:
            history_path = self._get_history_file_path()

            # Convert to serializable format
            history_data = {
                "date": date.today().isoformat(),
                "entry_time": chart_data.entry_time.isoformat(),
                "spot_at_entry": chart_data.spot_at_entry,
                "wing_distance": chart_data.wing_distance,
                "target_pnl": chart_data.target_pnl,
                "stop_loss_pnl": chart_data.stop_loss_pnl,
                "total_credit": chart_data.total_credit,
                "pnl_history": [
                    {
                        "timestamp": p.timestamp.isoformat(),
                        "pnl": p.pnl,
                        "spot": p.spot
                    }
                    for p in chart_data.pnl_history
                ]
            }

            # Atomic write: temp file then rename
            temp_fd, temp_path = tempfile.mkstemp(
                dir=self.output_dir,
                prefix='.pnl_history_',
                suffix='.tmp'
            )
            try:
                with os.fdopen(temp_fd, 'w') as f:
                    json.dump(history_data, f, indent=2)

                # Atomic rename (Windows needs target removed first)
                if os.name == 'nt' and history_path.exists():
                    os.remove(history_path)
                os.rename(temp_path, history_path)

                logger.debug(f"P&L history saved: {len(chart_data.pnl_history)} points")
                return True
            except Exception:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise

        except Exception as e:
            logger.error(f"Failed to save P&L history: {e}")
            return False

    def load_pnl_history(self, for_date: Optional[date] = None) -> Optional[ChartData]:
        """
        Load P&L history from file.

        Args:
            for_date: Date to load history for (defaults to today)

        Returns:
            ChartData with loaded history, or None if not found
        """
        try:
            history_path = self._get_history_file_path(for_date)

            if not history_path.exists():
                logger.debug(f"No P&L history file found: {history_path}")
                return None

            with open(history_path, 'r') as f:
                data = json.load(f)

            # Verify it's for today
            if data.get("date") != date.today().isoformat():
                logger.debug("P&L history is from a different day, ignoring")
                return None

            # Reconstruct ChartData
            chart_data = ChartData(
                entry_time=datetime.fromisoformat(data["entry_time"]),
                spot_at_entry=data["spot_at_entry"],
                wing_distance=data["wing_distance"],
                target_pnl=data["target_pnl"],
                stop_loss_pnl=data["stop_loss_pnl"],
                total_credit=data["total_credit"],
                pnl_history=[]
            )

            # Reconstruct P&L points
            for point in data.get("pnl_history", []):
                chart_data.pnl_history.append(PnLPoint(
                    timestamp=datetime.fromisoformat(point["timestamp"]),
                    pnl=point["pnl"],
                    spot=point["spot"]
                ))

            logger.info(f"Loaded P&L history: {len(chart_data.pnl_history)} points")
            return chart_data

        except Exception as e:
            logger.error(f"Failed to load P&L history: {e}")
            return None

    def add_pnl_point(
        self,
        chart_data: ChartData,
        pnl: float,
        spot: float,
        timestamp: Optional[datetime] = None,
        auto_save: bool = True
    ) -> ChartData:
        """
        Add P&L data point and optionally persist to file.

        Prevents duplicate points for the same minute (handles multiple
        script invocations in same minute).

        Args:
            chart_data: Existing chart data
            pnl: Current P&L
            spot: Current spot price
            timestamp: Timestamp (defaults to now)
            auto_save: Whether to save history after adding point

        Returns:
            Updated chart data
        """
        if timestamp is None:
            timestamp = datetime.now()

        # Check for duplicate - same minute already exists
        current_minute = timestamp.strftime("%H:%M")
        for existing in chart_data.pnl_history:
            if existing.timestamp.strftime("%H:%M") == current_minute:
                # Update existing point instead of adding duplicate
                existing.pnl = pnl
                existing.spot = spot
                existing.timestamp = timestamp
                logger.debug(f"Updated existing P&L point for {current_minute}")
                if auto_save:
                    self.save_pnl_history(chart_data)
                return chart_data

        # No duplicate, add new point
        chart_data.pnl_history.append(PnLPoint(
            timestamp=timestamp,
            pnl=pnl,
            spot=spot
        ))

        # Persist to file for next invocation
        if auto_save:
            self.save_pnl_history(chart_data)

        return chart_data

    def generate_pnl_chart(
        self,
        chart_data: ChartData,
        current_pnl: float,
        current_spot: float
    ) -> Optional[Path]:
        """
        Generate P&L chart with smart Y-axis scaling.

        Design:
        - Top: P&L chart with auto-scaled Y-axis (not forced by target/SL)
        - Middle: Progress gauge showing position relative to SL/Target
        - Bottom: NIFTY spot with wing boundaries

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
            # Create figure with 3 sections: P&L chart, gauge, spot chart
            fig = plt.figure(figsize=(10, 9))
            gs = fig.add_gridspec(3, 1, height_ratios=[3, 0.4, 1.2], hspace=0.3)
            ax1 = fig.add_subplot(gs[0])  # P&L chart
            ax_gauge = fig.add_subplot(gs[1])  # Progress gauge
            ax2 = fig.add_subplot(gs[2])  # Spot chart

            # Apply dark theme
            fig.patch.set_facecolor(self.colors['background'])
            for ax in [ax1, ax_gauge, ax2]:
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

            # ========== P&L Chart (top) ==========
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
                color=self.colors['profit'], alpha=0.3
            )
            ax1.fill_between(
                times, 0, pnls,
                where=[p < 0 for p in pnls],
                color=self.colors['loss'], alpha=0.3
            )

            # Auto-scale Y-axis based on ACTUAL P&L data, not target/SL
            pnl_min, pnl_max = min(pnls), max(pnls)
            pnl_range = max(abs(pnl_max), abs(pnl_min), 100)  # Minimum range of 100
            y_padding = pnl_range * 0.3  # 30% padding
            ax1.set_ylim(-pnl_range - y_padding, pnl_range + y_padding)

            # Zero line
            ax1.axhline(y=0, color=self.colors['text'], linestyle='-', linewidth=1, alpha=0.5)

            # Current P&L marker
            marker_color = self.colors['profit'] if pnls[-1] >= 0 else self.colors['loss']
            ax1.scatter(
                [times[-1]], [pnls[-1]],
                color=marker_color, s=120, zorder=5, edgecolors='white', linewidth=2
            )

            # Current P&L annotation (prominent)
            pnl_sign = "+" if current_pnl >= 0 else ""
            ax1.annotate(
                f'Rs.{current_pnl:,.0f}',
                xy=(times[-1], pnls[-1]),
                xytext=(15, 0), textcoords='offset points',
                fontsize=14, fontweight='bold',
                color=marker_color,
                bbox=dict(boxstyle='round,pad=0.3', facecolor=self.colors['background'],
                         edgecolor=marker_color, alpha=0.9)
            )

            ax1.set_ylabel('P&L (Rs.)', fontsize=10, color=self.colors['text'])
            ax1.grid(True, color=self.colors['grid'], alpha=0.3)
            ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            ax1.xaxis.set_major_locator(mdates.MinuteLocator(interval=30))

            # ========== Progress Gauge (middle) ==========
            ax_gauge.set_xlim(0, 1)
            ax_gauge.set_ylim(0, 1)
            ax_gauge.axis('off')

            # Calculate position on gauge (SL = 0, Entry = 0.5, Target = 1)
            total_range = chart_data.target_pnl - chart_data.stop_loss_pnl
            if total_range > 0:
                # Position: 0 = SL, 0.5 = breakeven, 1 = target
                normalized_pos = (current_pnl - chart_data.stop_loss_pnl) / total_range
                normalized_pos = max(0, min(1, normalized_pos))  # Clamp to [0, 1]
            else:
                normalized_pos = 0.5

            # Draw gauge background (gradient from red to green)
            from matplotlib.patches import Rectangle, FancyBboxPatch

            # SL zone (red)
            ax_gauge.add_patch(Rectangle((0.05, 0.3), 0.4, 0.4,
                                         facecolor=self.colors['loss'], alpha=0.3,
                                         edgecolor='none'))
            # Profit zone (green)
            ax_gauge.add_patch(Rectangle((0.55, 0.3), 0.4, 0.4,
                                         facecolor=self.colors['profit'], alpha=0.3,
                                         edgecolor='none'))
            # Neutral zone around zero
            ax_gauge.add_patch(Rectangle((0.4, 0.3), 0.2, 0.4,
                                         facecolor=self.colors['text'], alpha=0.1,
                                         edgecolor='none'))

            # Gauge outline
            ax_gauge.add_patch(FancyBboxPatch((0.05, 0.3), 0.9, 0.4,
                                              boxstyle="round,pad=0.02",
                                              facecolor='none',
                                              edgecolor=self.colors['grid'], linewidth=2))

            # Current position marker (triangle)
            marker_x = 0.05 + normalized_pos * 0.9
            marker_color = self.colors['profit'] if current_pnl >= 0 else self.colors['loss']
            ax_gauge.plot([marker_x], [0.5], marker='v', markersize=15,
                         color=marker_color, markeredgecolor='white', markeredgewidth=2)
            ax_gauge.plot([marker_x, marker_x], [0.3, 0.7], color=marker_color,
                         linewidth=3, alpha=0.8)

            # Labels
            ax_gauge.text(0.05, 0.15, f'SL: Rs.{chart_data.stop_loss_pnl:,.0f}',
                         fontsize=9, color=self.colors['loss'], ha='left', fontweight='bold')
            ax_gauge.text(0.5, 0.15, '0', fontsize=9, color=self.colors['text'],
                         ha='center', alpha=0.7)
            ax_gauge.text(0.95, 0.15, f'Target: Rs.{chart_data.target_pnl:,.0f}',
                         fontsize=9, color=self.colors['profit'], ha='right', fontweight='bold')

            # Percentage of target
            pct_of_target = (current_pnl / chart_data.target_pnl * 100) if chart_data.target_pnl > 0 else 0
            ax_gauge.text(0.5, 0.85, f'{pct_of_target:.1f}% of Target',
                         fontsize=10, color=self.colors['text'], ha='center', fontweight='bold')

            # ========== Spot Chart (bottom) ==========
            ax2.plot(times, spots, color=self.colors['neutral'], linewidth=2)

            # Wing lines - now using actual short strikes
            atm = round(chart_data.spot_at_entry / 50) * 50
            short_ce = atm + chart_data.wing_distance  # Short CE strike
            short_pe = atm - chart_data.wing_distance  # Short PE strike

            ax2.axhline(y=short_ce, color=self.colors['warning'], linestyle='--',
                       linewidth=1.5, alpha=0.8)
            ax2.axhline(y=short_pe, color=self.colors['warning'], linestyle='--',
                       linewidth=1.5, alpha=0.8)
            ax2.axhline(y=atm, color=self.colors['text'], linestyle=':',
                       linewidth=1, alpha=0.4)

            # Wing labels on right side
            ax2.text(times[-1], short_ce, f'  {short_ce:.0f}',
                    color=self.colors['warning'], fontsize=9, va='center', fontweight='bold')
            ax2.text(times[-1], short_pe, f'  {short_pe:.0f}',
                    color=self.colors['warning'], fontsize=9, va='center', fontweight='bold')

            # Current spot marker
            ax2.scatter([times[-1]], [spots[-1]], color=self.colors['neutral'],
                       s=80, zorder=5, edgecolors='white', linewidth=2)

            ax2.set_ylabel('NIFTY', fontsize=10, color=self.colors['text'])
            ax2.set_xlabel('Time', fontsize=10, color=self.colors['text'])
            ax2.grid(True, color=self.colors['grid'], alpha=0.3)
            ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            ax2.xaxis.set_major_locator(mdates.MinuteLocator(interval=30))

            # Auto-scale spot chart to show wings properly
            spot_min = min(min(spots), short_pe) - 50
            spot_max = max(max(spots), short_ce) + 50
            ax2.set_ylim(spot_min, spot_max)

            plt.tight_layout()

            # Save chart
            timestamp = datetime.now().strftime('%H%M%S')
            output_path = self.output_dir / f"pnl_chart_{timestamp}.png"
            fig.savefig(output_path, dpi=150, facecolor=self.colors['background'])
            plt.close(fig)

            logger.info(f"Chart saved: {output_path}")
            return output_path

        except Exception as e:
            logger.exception(f"Chart generation failed: {e}")
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
        Remove old chart and history files.

        Args:
            keep_days: Days to keep charts

        Returns:
            Number of files removed
        """
        import time

        removed = 0
        cutoff = time.time() - (keep_days * 24 * 60 * 60)

        # Clean up chart images
        for chart_file in self.output_dir.glob("*.png"):
            if chart_file.stat().st_mtime < cutoff:
                chart_file.unlink()
                removed += 1

        # Clean up P&L history files
        for history_file in self.output_dir.glob("pnl_history_*.json"):
            if history_file.stat().st_mtime < cutoff:
                history_file.unlink()
                removed += 1

        if removed > 0:
            logger.info(f"Cleaned up {removed} old chart/history files")

        return removed
