"""
SNAIL Chart Generation Utilities

Generates P&L charts for Telegram display.

@file        charts.py
@description P&L chart generation using matplotlib
@author      SNAIL Development Team
@created     2025-12-15
@version     1.0.0
"""

import io
from datetime import datetime, date
from typing import List, Optional, Tuple
from loguru import logger

try:
    import matplotlib
    matplotlib.use('Agg')  # Non-interactive backend for headless servers
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    logger.warning("matplotlib not available - charts will be disabled")


def generate_pnl_chart(
    timestamps: List[datetime],
    pnl_values: List[float],
    title: str = "P&L Progress",
    profit_target: Optional[float] = None,
    stop_loss: Optional[float] = None,
    max_profit: Optional[float] = None,
    max_loss: Optional[float] = None
) -> Optional[bytes]:
    """
    Generate a P&L chart as PNG bytes.

    Args:
        timestamps: List of datetime timestamps
        pnl_values: List of P&L values corresponding to timestamps
        title: Chart title
        profit_target: Optional profit target line
        stop_loss: Optional stop loss line
        max_profit: Optional max profit line
        max_loss: Optional max loss line

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
        # Create figure with dark theme for better Telegram visibility
        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(10, 5), dpi=100)

        # Determine color based on final P&L
        final_pnl = pnl_values[-1]
        line_color = '#00ff88' if final_pnl >= 0 else '#ff4444'
        fill_color = '#00ff8833' if final_pnl >= 0 else '#ff444433'

        # Plot main P&L line
        ax.plot(timestamps, pnl_values, color=line_color, linewidth=2, label='P&L')

        # Fill area under curve
        ax.fill_between(timestamps, pnl_values, 0, alpha=0.3, color=line_color)

        # Add zero line
        ax.axhline(y=0, color='white', linestyle='-', linewidth=0.5, alpha=0.5)

        # Add target/stop lines if provided
        if profit_target is not None:
            ax.axhline(y=profit_target, color='#00ff88', linestyle='--',
                      linewidth=1.5, alpha=0.7, label=f'Target: +₹{profit_target:,.0f}')

        if stop_loss is not None:
            ax.axhline(y=stop_loss, color='#ff4444', linestyle='--',
                      linewidth=1.5, alpha=0.7, label=f'Stop: -₹{abs(stop_loss):,.0f}')

        if max_profit is not None:
            ax.axhline(y=max_profit, color='#4488ff', linestyle=':',
                      linewidth=1, alpha=0.5, label=f'Max: +₹{max_profit:,.0f}')

        if max_loss is not None:
            ax.axhline(y=max_loss, color='#ff8844', linestyle=':',
                      linewidth=1, alpha=0.5, label=f'Max Loss: -₹{abs(max_loss):,.0f}')

        # Format x-axis based on date range
        if timestamps:
            date_range = (timestamps[-1] - timestamps[0]).days
            if date_range == 0:
                # Single day - show time only
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
                ax.xaxis.set_major_locator(mdates.HourLocator(interval=1))
            elif date_range <= 5:
                # Few days - show day and time
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%d %b\n%H:%M'))
                ax.xaxis.set_major_locator(mdates.HourLocator(interval=6))
            else:
                # Many days - show date only
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%d %b'))
                ax.xaxis.set_major_locator(mdates.DayLocator(interval=1))
        plt.xticks(rotation=45)

        # Format y-axis for currency
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'₹{x:,.0f}'))

        # Labels and title
        ax.set_xlabel('Time', fontsize=10, color='white')
        ax.set_ylabel('P&L (₹)', fontsize=10, color='white')
        ax.set_title(title, fontsize=12, fontweight='bold', color='white')

        # Grid
        ax.grid(True, alpha=0.2, linestyle='--')

        # Legend
        ax.legend(loc='upper left', fontsize=8, framealpha=0.7)

        # Add current P&L annotation
        final_time = timestamps[-1]
        pnl_sign = '+' if final_pnl >= 0 else ''
        ax.annotate(
            f'{pnl_sign}₹{final_pnl:,.0f}',
            xy=(final_time, final_pnl),
            xytext=(10, 10),
            textcoords='offset points',
            fontsize=11,
            fontweight='bold',
            color=line_color,
            bbox=dict(boxstyle='round,pad=0.3', facecolor='black', alpha=0.7)
        )

        # Tight layout
        plt.tight_layout()

        # Save to BytesIO
        buf = io.BytesIO()
        fig.savefig(buf, format='png', bbox_inches='tight',
                   facecolor='#1a1a2e', edgecolor='none')
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
    max_profit = None
    max_loss = None

    if position:
        max_profit = position.max_profit
        max_loss = -position.max_loss if position.max_loss else None

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
        stop_loss=stop_loss,
        max_profit=max_profit,
        max_loss=max_loss
    )

    if chart_bytes:
        return chart_bytes, "OK"
    else:
        return None, "Failed to generate chart"


def is_chart_available() -> bool:
    """Check if chart generation is available."""
    return MATPLOTLIB_AVAILABLE
