"""
SNAIL Chart Generation Utilities

Generates P&L charts for Telegram display.

@file        charts.py
@description P&L chart generation using matplotlib
@author      SNAIL Development Team
@created     2025-12-15
@version     1.1.0
"""

import io
from datetime import datetime, date, time as dt_time, timedelta
from typing import List, Optional, Tuple
from loguru import logger

try:
    import matplotlib
    matplotlib.use('Agg')  # Non-interactive backend for headless servers
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.patches import Rectangle
    import numpy as np
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    logger.warning("matplotlib not available - charts will be disabled")

# Market hours (IST)
MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)


def generate_pnl_chart(
    timestamps: List[datetime],
    pnl_values: List[float],
    title: str = "P&L Progress",
    profit_target: Optional[float] = None,
    stop_loss: Optional[float] = None
) -> Optional[bytes]:
    """
    Generate a P&L chart as PNG bytes.

    Args:
        timestamps: List of datetime timestamps
        pnl_values: List of P&L values corresponding to timestamps
        title: Chart title
        profit_target: Optional profit target line
        stop_loss: Optional stop loss line

    Returns:
        PNG image bytes or None if chart cannot be generated
    """
    if not MATPLOTLIB_AVAILABLE:
        logger.warning("Cannot generate chart - matplotlib not installed")
        return None

    if len(timestamps) < 2:
        logger.warning("Not enough data points for chart (need at least 2)")
        return None

    try:
        # Create figure with dark theme
        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(12, 6), dpi=100)

        # Set background color
        fig.patch.set_facecolor('#0d1117')
        ax.set_facecolor('#0d1117')

        # Determine color based on final P&L
        final_pnl = pnl_values[-1]
        line_color = '#00ff88' if final_pnl >= 0 else '#ff5555'
        fill_color = line_color

        # Find unique trading days
        trading_days = sorted(set(ts.date() for ts in timestamps))

        # Create sequential x-axis (market hours only)
        # Map each timestamp to a sequential position
        x_positions = []
        x_labels = []
        day_boundaries = []

        current_pos = 0
        prev_day = None

        for i, ts in enumerate(timestamps):
            day = ts.date()

            # Add day boundary marker
            if prev_day is not None and day != prev_day:
                day_boundaries.append(current_pos - 0.5)

            x_positions.append(current_pos)
            current_pos += 1
            prev_day = day

        # Plot main P&L line
        ax.plot(x_positions, pnl_values, color=line_color, linewidth=2.5,
                label=f'P&L', zorder=5)

        # Fill area under curve - green for positive, red for negative
        pnl_array = np.array(pnl_values)
        x_array = np.array(x_positions)
        ax.fill_between(x_array, pnl_array, 0, where=(pnl_array >= 0),
                       alpha=0.3, color='#00ff88', zorder=2)
        ax.fill_between(x_array, pnl_array, 0, where=(pnl_array < 0),
                       alpha=0.3, color='#ff5555', zorder=2)

        # Add zero line
        ax.axhline(y=0, color='#555555', linestyle='-', linewidth=1, zorder=3)

        # Add target line if provided
        if profit_target is not None:
            ax.axhline(y=profit_target, color='#00ff88', linestyle='--',
                      linewidth=2, alpha=0.8, label=f'Target: +₹{profit_target:,.0f}', zorder=4)

        # Add stop loss line if provided
        if stop_loss is not None:
            ax.axhline(y=stop_loss, color='#ff5555', linestyle='--',
                      linewidth=2, alpha=0.8, label=f'Stop: ₹{stop_loss:,.0f}', zorder=4)

        # Add day boundary vertical lines
        for boundary in day_boundaries:
            ax.axvline(x=boundary, color='#333333', linestyle='-', linewidth=2, zorder=1)

        # Calculate Y-axis limits based on actual data (not max profit/loss)
        pnl_min = min(pnl_values)
        pnl_max = max(pnl_values)

        # Include target and stop in range if they're close to data
        y_values_for_range = list(pnl_values)
        if profit_target is not None and profit_target < pnl_max * 3:
            y_values_for_range.append(profit_target)
        if stop_loss is not None and stop_loss > pnl_min * 3:
            y_values_for_range.append(stop_loss)

        y_min = min(y_values_for_range)
        y_max = max(y_values_for_range)
        y_padding = (y_max - y_min) * 0.15 if y_max != y_min else abs(y_max) * 0.3 or 1000

        ax.set_ylim(y_min - y_padding, y_max + y_padding)

        # X-axis: Show day labels at day boundaries
        if len(trading_days) == 1:
            # Single day - show time labels
            n_labels = min(8, len(timestamps))
            label_indices = [int(i * (len(timestamps) - 1) / (n_labels - 1)) for i in range(n_labels)]
            ax.set_xticks([x_positions[i] for i in label_indices])
            ax.set_xticklabels([timestamps[i].strftime('%H:%M') for i in label_indices],
                             fontsize=10, color='#aaaaaa')
            ax.set_xlabel('Time', fontsize=11, color='#aaaaaa')
        else:
            # Multi-day - show day labels
            day_centers = []
            day_labels = []

            start_idx = 0
            for i, day in enumerate(trading_days):
                day_timestamps = [j for j, ts in enumerate(timestamps) if ts.date() == day]
                if day_timestamps:
                    center = (x_positions[day_timestamps[0]] + x_positions[day_timestamps[-1]]) / 2
                    day_centers.append(center)
                    day_labels.append(day.strftime('%d %b'))

            ax.set_xticks(day_centers)
            ax.set_xticklabels(day_labels, fontsize=11, color='#aaaaaa')
            ax.set_xlabel('Trading Days', fontsize=11, color='#aaaaaa')

        # Format y-axis for currency
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'₹{x:,.0f}'))
        ax.tick_params(axis='y', colors='#aaaaaa', labelsize=10)

        # Title
        ax.set_title(title, fontsize=14, fontweight='bold', color='white', pad=15)
        ax.set_ylabel('P&L (₹)', fontsize=11, color='#aaaaaa')

        # Grid - horizontal only for cleaner look
        ax.grid(True, axis='y', alpha=0.15, linestyle='-', color='#555555')
        ax.grid(False, axis='x')

        # Legend - positioned better
        legend = ax.legend(loc='upper left', fontsize=10, framealpha=0.9,
                          facecolor='#1a1a2e', edgecolor='#333333')
        for text in legend.get_texts():
            text.set_color('white')

        # Add current P&L annotation with better styling
        final_x = x_positions[-1]
        pnl_sign = '+' if final_pnl >= 0 else ''

        # Annotation box
        bbox_color = '#00aa55' if final_pnl >= 0 else '#cc3333'
        ax.annotate(
            f'{pnl_sign}₹{final_pnl:,.0f}',
            xy=(final_x, final_pnl),
            xytext=(15, 0),
            textcoords='offset points',
            fontsize=13,
            fontweight='bold',
            color='white',
            bbox=dict(boxstyle='round,pad=0.4', facecolor=bbox_color, edgecolor='none', alpha=0.9),
            zorder=10
        )

        # Add a dot at the final point
        ax.scatter([final_x], [final_pnl], color=line_color, s=80, zorder=6, edgecolors='white', linewidths=2)

        # Remove spines for cleaner look
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.spines['left'].set_color('#333333')
        ax.spines['bottom'].set_color('#333333')

        # Tight layout
        plt.tight_layout()

        # Save to BytesIO
        buf = io.BytesIO()
        fig.savefig(buf, format='png', bbox_inches='tight',
                   facecolor='#0d1117', edgecolor='none', dpi=120)
        buf.seek(0)
        plt.close(fig)

        return buf.getvalue()

    except Exception as e:
        logger.error(f"Error generating P&L chart: {e}")
        import traceback
        traceback.print_exc()
        return None


def generate_daily_pnl_chart(
    position_id: int,
    target_date: Optional[date] = None,
    full_history: bool = True
) -> Tuple[Optional[bytes], str]:
    """
    Generate P&L chart for a position.

    Args:
        position_id: Position ID
        target_date: Date to chart (defaults to today, ignored if full_history=True)
        full_history: If True, show all data across multiple days (default)

    Returns:
        Tuple of (PNG bytes or None, status message)
    """
    from src.utils.db import get_pnl_snapshots_for_position, get_position_by_id
    from src.utils.config import get_trading_config

    # Get snapshots - full history or single day
    if full_history:
        snapshots = get_pnl_snapshots_for_position(position_id, target_date=None)
    else:
        if target_date is None:
            target_date = date.today()
        snapshots = get_pnl_snapshots_for_position(position_id, target_date)

    if not snapshots:
        return None, "No P&L data available"

    if len(snapshots) < 2:
        return None, "Not enough data points (need at least 2)"

    # Extract data
    timestamps = [s.timestamp for s in snapshots if s.timestamp]
    pnl_values = [s.current_pnl for s in snapshots]

    if len(timestamps) != len(pnl_values):
        return None, "Data mismatch in snapshots"

    # Get position details for targets
    position = get_position_by_id(position_id)
    profit_target = None
    stop_loss = None

    if position:
        # Calculate targets from config
        config = get_trading_config()
        profit_target_pct = config.get('exit', {}).get('profit_target_pct', 2.6)
        stop_loss_pct = config.get('exit', {}).get('stop_loss_pct', 50)

        if position.margin_deployed > 0:
            profit_target = position.margin_deployed * profit_target_pct / 100
        elif position.max_profit > 0:
            profit_target = position.max_profit * 0.5

        if position.max_loss > 0:
            stop_loss = -(position.max_loss * stop_loss_pct / 100)

    # Generate title based on date range
    if timestamps:
        first_date = timestamps[0].date()
        last_date = timestamps[-1].date()
        if first_date == last_date:
            title = f"P&L - {first_date.strftime('%d %b %Y')}"
        else:
            title = f"P&L - {first_date.strftime('%d %b')} to {last_date.strftime('%d %b %Y')}"
    else:
        title = "P&L Progress"

    # Generate chart
    chart_bytes = generate_pnl_chart(
        timestamps=timestamps,
        pnl_values=pnl_values,
        title=title,
        profit_target=profit_target,
        stop_loss=stop_loss
    )

    if chart_bytes:
        return chart_bytes, "OK"
    else:
        return None, "Failed to generate chart"


def is_chart_available() -> bool:
    """Check if chart generation is available."""
    return MATPLOTLIB_AVAILABLE
