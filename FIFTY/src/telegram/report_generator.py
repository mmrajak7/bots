"""
HTML Report Generator for FIFTY Bot

Generates professional HTML reports for:
- Daily summary
- Weekly summary
- Position details
- Trade history

Reports are saved to reports/ folder, sent via Telegram, then deleted.
"""

import os
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import List, Dict, Any, Optional
from loguru import logger

from src.models.database import (
    get_session, OpenPosition, ClosedPosition, OpenOrder, SignalQueue,
    CapitalLedger, PositionStatus, OrderStatus, SignalStatus
)
from src.utils.timezone_helper import today_ist, now_ist
from src.utils.config_manager import config


# Report output directory
REPORTS_DIR = Path(__file__).parent.parent.parent / "reports"


def ensure_reports_dir() -> Path:
    """Ensure reports directory exists"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    return REPORTS_DIR


class ReportGenerator:
    """Generates HTML reports for FIFTY bot"""

    def __init__(self):
        """Initialize report generator"""
        self.reports_dir = ensure_reports_dir()

    # =========================================================================
    # CSS STYLES
    # =========================================================================

    def _get_base_styles(self) -> str:
        """Get common CSS styles for all reports"""
        return """
        <style>
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
                background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
                color: #e8e8e8;
                padding: 20px;
                min-height: 100vh;
            }
            .container {
                max-width: 800px;
                margin: 0 auto;
            }
            .header {
                text-align: center;
                padding: 30px 0;
                border-bottom: 2px solid #0f3460;
                margin-bottom: 30px;
            }
            .header h1 {
                font-size: 2.5em;
                color: #e94560;
                margin-bottom: 10px;
                text-transform: uppercase;
                letter-spacing: 3px;
            }
            .header .subtitle {
                color: #94a3b8;
                font-size: 1.1em;
            }
            .card {
                background: rgba(255, 255, 255, 0.05);
                border-radius: 12px;
                padding: 25px;
                margin-bottom: 20px;
                border: 1px solid rgba(255, 255, 255, 0.1);
                backdrop-filter: blur(10px);
            }
            .card-title {
                color: #e94560;
                font-size: 1.3em;
                margin-bottom: 20px;
                padding-bottom: 10px;
                border-bottom: 1px solid rgba(255, 255, 255, 0.1);
            }
            .metric-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
                gap: 15px;
            }
            .metric {
                background: rgba(0, 0, 0, 0.2);
                padding: 15px;
                border-radius: 8px;
                text-align: center;
            }
            .metric-value {
                font-size: 1.8em;
                font-weight: bold;
                color: #fff;
            }
            .metric-label {
                font-size: 0.85em;
                color: #94a3b8;
                margin-top: 5px;
            }
            .positive { color: #22c55e; }
            .negative { color: #ef4444; }
            .neutral { color: #f59e0b; }
            table {
                width: 100%;
                border-collapse: collapse;
                margin-top: 15px;
            }
            th, td {
                padding: 12px 15px;
                text-align: left;
                border-bottom: 1px solid rgba(255, 255, 255, 0.1);
            }
            th {
                background: rgba(233, 69, 96, 0.2);
                color: #e94560;
                font-weight: 600;
                text-transform: uppercase;
                font-size: 0.85em;
                letter-spacing: 1px;
            }
            tr:hover {
                background: rgba(255, 255, 255, 0.05);
            }
            .badge {
                display: inline-block;
                padding: 4px 10px;
                border-radius: 4px;
                font-size: 0.8em;
                font-weight: 600;
            }
            .badge-success { background: rgba(34, 197, 94, 0.2); color: #22c55e; }
            .badge-danger { background: rgba(239, 68, 68, 0.2); color: #ef4444; }
            .badge-warning { background: rgba(245, 158, 11, 0.2); color: #f59e0b; }
            .badge-info { background: rgba(59, 130, 246, 0.2); color: #3b82f6; }
            .footer {
                text-align: center;
                padding: 20px;
                color: #64748b;
                font-size: 0.85em;
                margin-top: 30px;
                border-top: 1px solid rgba(255, 255, 255, 0.1);
            }
            .pnl-positive { color: #22c55e; }
            .pnl-negative { color: #ef4444; }
            .progress-bar {
                height: 8px;
                background: rgba(255, 255, 255, 0.1);
                border-radius: 4px;
                overflow: hidden;
                margin-top: 5px;
            }
            .progress-fill {
                height: 100%;
                border-radius: 4px;
                transition: width 0.3s ease;
            }
            .summary-row {
                display: flex;
                justify-content: space-between;
                padding: 10px 0;
                border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            }
            .summary-row:last-child {
                border-bottom: none;
            }
        </style>
        """

    # =========================================================================
    # DAILY REPORT
    # =========================================================================

    def generate_daily_report(self) -> Optional[str]:
        """
        Generate daily summary HTML report.

        Returns:
            Path to generated HTML file, or None on error
        """
        session = get_session()
        try:
            today = today_ist()
            report_date = today.strftime('%Y-%m-%d')

            # Gather data
            open_positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()

            pending_orders = session.query(OpenOrder).filter(
                OpenOrder.status == OrderStatus.PENDING
            ).all()

            signals_today = session.query(SignalQueue).filter(
                SignalQueue.signal_date == today
            ).all()

            trades_closed_today = session.query(ClosedPosition).filter(
                ClosedPosition.exit_date == today
            ).all()

            # Capital
            ledger = session.query(CapitalLedger).filter(
                CapitalLedger.date == today
            ).first()

            initial_capital = config.get('trading.initial_capital', 100000)
            deployed = ledger.deployed_capital if ledger else sum(p.capital_deployed for p in open_positions)
            free = ledger.free_capital if ledger else (initial_capital - deployed)
            realized_pnl = ledger.realized_pnl if ledger else 0

            # Today's P&L from closed trades
            today_pnl = sum(t.net_pnl for t in trades_closed_today) if trades_closed_today else 0

            # Get LTP for unrealized P&L
            unrealized_pnl = 0
            try:
                from src.api.dual_kite_client import get_kite_client
                kite = get_kite_client()
                for pos in open_positions:
                    try:
                        token = kite.get_instrument_token(pos.script)
                        if token:
                            ltp = kite.get_instrument_ltp(token)
                            if ltp:
                                unrealized_pnl += (ltp - pos.entry_price) * pos.quantity
                    except Exception:
                        pass
            except Exception:
                pass

            # Build HTML
            html = f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>FIFTY Daily Report - {report_date}</title>
                {self._get_base_styles()}
            </head>
            <body>
                <div class="container">
                    <div class="header">
                        <h1>FIFTY Bot</h1>
                        <div class="subtitle">Daily Report | {today.strftime('%B %d, %Y')}</div>
                    </div>

                    <!-- Summary Metrics -->
                    <div class="card">
                        <div class="card-title">Today's Summary</div>
                        <div class="metric-grid">
                            <div class="metric">
                                <div class="metric-value">{len(open_positions)}</div>
                                <div class="metric-label">Open Positions</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value">{len(pending_orders)}</div>
                                <div class="metric-label">Pending Orders</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value">{len(signals_today)}</div>
                                <div class="metric-label">Signals Today</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value">{len(trades_closed_today)}</div>
                                <div class="metric-label">Trades Closed</div>
                            </div>
                        </div>
                    </div>

                    <!-- P&L Card -->
                    <div class="card">
                        <div class="card-title">Profit & Loss</div>
                        <div class="metric-grid">
                            <div class="metric">
                                <div class="metric-value {'pnl-positive' if today_pnl >= 0 else 'pnl-negative'}">
                                    {'+' if today_pnl >= 0 else ''}{today_pnl:,.0f}
                                </div>
                                <div class="metric-label">Today's Realized</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value {'pnl-positive' if unrealized_pnl >= 0 else 'pnl-negative'}">
                                    {'+' if unrealized_pnl >= 0 else ''}{unrealized_pnl:,.0f}
                                </div>
                                <div class="metric-label">Unrealized P&L</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value {'pnl-positive' if realized_pnl >= 0 else 'pnl-negative'}">
                                    {'+' if realized_pnl >= 0 else ''}{realized_pnl:,.0f}
                                </div>
                                <div class="metric-label">Total Realized</div>
                            </div>
                        </div>
                    </div>

                    <!-- Capital Allocation -->
                    <div class="card">
                        <div class="card-title">Capital Allocation</div>
                        <div class="summary-row">
                            <span>Initial Capital</span>
                            <span>{initial_capital:,.0f}</span>
                        </div>
                        <div class="summary-row">
                            <span>Deployed</span>
                            <span>{deployed:,.0f}</span>
                        </div>
                        <div class="summary-row">
                            <span>Available</span>
                            <span>{free:,.0f}</span>
                        </div>
                        <div class="progress-bar">
                            <div class="progress-fill" style="width: {min(100, deployed / initial_capital * 100):.1f}%; background: linear-gradient(90deg, #e94560, #0f3460);"></div>
                        </div>
                        <div style="text-align: right; font-size: 0.85em; color: #94a3b8; margin-top: 5px;">
                            {deployed / initial_capital * 100:.1f}% deployed
                        </div>
                    </div>

                    <!-- Open Positions -->
                    {self._generate_positions_table(open_positions)}

                    <!-- Pending Orders -->
                    {self._generate_orders_table(pending_orders)}

                    <!-- Trades Closed Today -->
                    {self._generate_closed_trades_table(trades_closed_today, "Today's Closed Trades")}

                    <div class="footer">
                        Generated at {now_ist().strftime('%H:%M:%S IST')} | FIFTY Trading Bot
                    </div>
                </div>
            </body>
            </html>
            """

            # Save to file
            filename = f"daily_report_{report_date}.html"
            filepath = self.reports_dir / filename

            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(html)

            logger.info(f"Daily report generated: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"Error generating daily report: {e}")
            return None
        finally:
            session.close()

    # =========================================================================
    # WEEKLY REPORT
    # =========================================================================

    def generate_weekly_report(self) -> Optional[str]:
        """
        Generate weekly summary HTML report.

        Returns:
            Path to generated HTML file, or None on error
        """
        session = get_session()
        try:
            today = today_ist()
            week_start = today - timedelta(days=7)
            report_date = today.strftime('%Y-%m-%d')

            # Get week's closed trades
            week_trades = session.query(ClosedPosition).filter(
                ClosedPosition.exit_date >= week_start
            ).all()

            # All-time stats
            all_trades = session.query(ClosedPosition).all()

            # Current positions
            open_positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()

            # Calculate stats
            week_count = len(week_trades)
            week_pnl = sum(t.net_pnl for t in week_trades) if week_trades else 0
            week_winners = sum(1 for t in week_trades if t.net_pnl > 0)
            week_win_rate = (week_winners / week_count * 100) if week_count > 0 else 0

            total_count = len(all_trades)
            total_pnl = sum(t.net_pnl for t in all_trades) if all_trades else 0
            total_winners = sum(1 for t in all_trades if t.net_pnl > 0)
            total_win_rate = (total_winners / total_count * 100) if total_count > 0 else 0

            # Average metrics
            avg_winner = sum(t.net_pnl for t in all_trades if t.net_pnl > 0) / total_winners if total_winners > 0 else 0
            avg_loser = sum(t.net_pnl for t in all_trades if t.net_pnl <= 0) / (total_count - total_winners) if (total_count - total_winners) > 0 else 0
            avg_days = sum(t.days_held for t in all_trades) / total_count if total_count > 0 else 0

            # Capital
            initial_capital = config.get('trading.initial_capital', 100000)
            deployed = sum(p.capital_deployed for p in open_positions)

            # Build HTML
            html = f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>FIFTY Weekly Report - {report_date}</title>
                {self._get_base_styles()}
            </head>
            <body>
                <div class="container">
                    <div class="header">
                        <h1>FIFTY Bot</h1>
                        <div class="subtitle">Weekly Report | {week_start.strftime('%b %d')} - {today.strftime('%b %d, %Y')}</div>
                    </div>

                    <!-- This Week Summary -->
                    <div class="card">
                        <div class="card-title">This Week</div>
                        <div class="metric-grid">
                            <div class="metric">
                                <div class="metric-value">{week_count}</div>
                                <div class="metric-label">Trades Closed</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value {'pnl-positive' if week_pnl >= 0 else 'pnl-negative'}">
                                    {'+' if week_pnl >= 0 else ''}{week_pnl:,.0f}
                                </div>
                                <div class="metric-label">Week P&L</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value">{week_winners}/{week_count}</div>
                                <div class="metric-label">Winners</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value">{week_win_rate:.1f}%</div>
                                <div class="metric-label">Win Rate</div>
                            </div>
                        </div>
                    </div>

                    <!-- All-Time Stats -->
                    <div class="card">
                        <div class="card-title">All-Time Performance</div>
                        <div class="metric-grid">
                            <div class="metric">
                                <div class="metric-value">{total_count}</div>
                                <div class="metric-label">Total Trades</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value {'pnl-positive' if total_pnl >= 0 else 'pnl-negative'}">
                                    {'+' if total_pnl >= 0 else ''}{total_pnl:,.0f}
                                </div>
                                <div class="metric-label">Total P&L</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value">{total_win_rate:.1f}%</div>
                                <div class="metric-label">Win Rate</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value">{avg_days:.1f}</div>
                                <div class="metric-label">Avg Days Held</div>
                            </div>
                        </div>
                    </div>

                    <!-- Trade Metrics -->
                    <div class="card">
                        <div class="card-title">Trade Metrics</div>
                        <div class="summary-row">
                            <span>Average Winner</span>
                            <span class="pnl-positive">+{avg_winner:,.0f}</span>
                        </div>
                        <div class="summary-row">
                            <span>Average Loser</span>
                            <span class="pnl-negative">{avg_loser:,.0f}</span>
                        </div>
                        <div class="summary-row">
                            <span>Winners / Losers</span>
                            <span>{total_winners} / {total_count - total_winners}</span>
                        </div>
                        <div class="summary-row">
                            <span>Profit Factor</span>
                            <span>{abs(avg_winner / avg_loser):.2f}x</span>
                        </div>
                    </div>

                    <!-- Current Status -->
                    <div class="card">
                        <div class="card-title">Current Status</div>
                        <div class="summary-row">
                            <span>Open Positions</span>
                            <span>{len(open_positions)}</span>
                        </div>
                        <div class="summary-row">
                            <span>Capital Deployed</span>
                            <span>{deployed:,.0f}</span>
                        </div>
                        <div class="summary-row">
                            <span>Utilization</span>
                            <span>{deployed / initial_capital * 100:.1f}%</span>
                        </div>
                    </div>

                    <!-- Week's Trades -->
                    {self._generate_closed_trades_table(week_trades, "This Week's Trades")}

                    <div class="footer">
                        Generated at {now_ist().strftime('%H:%M:%S IST')} | FIFTY Trading Bot
                    </div>
                </div>
            </body>
            </html>
            """

            # Save to file
            filename = f"weekly_report_{report_date}.html"
            filepath = self.reports_dir / filename

            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(html)

            logger.info(f"Weekly report generated: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"Error generating weekly report: {e}")
            return None
        finally:
            session.close()

    # =========================================================================
    # HELPER METHODS
    # =========================================================================

    def _generate_positions_table(self, positions: List) -> str:
        """Generate HTML table for open positions"""
        if not positions:
            return """
            <div class="card">
                <div class="card-title">Open Positions</div>
                <p style="color: #94a3b8; text-align: center; padding: 20px;">No open positions</p>
            </div>
            """

        rows = []
        for pos in positions:
            rows.append(f"""
                <tr>
                    <td><strong>{pos.script}</strong></td>
                    <td>{pos.entry_price:,.2f}</td>
                    <td>{pos.quantity}</td>
                    <td>{pos.current_sl:,.2f}</td>
                    <td>{pos.capital_deployed:,.0f}</td>
                    <td>{pos.days_held}d</td>
                </tr>
            """)

        return f"""
        <div class="card">
            <div class="card-title">Open Positions ({len(positions)})</div>
            <table>
                <thead>
                    <tr>
                        <th>Script</th>
                        <th>Entry</th>
                        <th>Qty</th>
                        <th>SL</th>
                        <th>Capital</th>
                        <th>Days</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows)}
                </tbody>
            </table>
        </div>
        """

    def _generate_orders_table(self, orders: List) -> str:
        """Generate HTML table for pending orders"""
        if not orders:
            return ""

        rows = []
        for order in orders:
            rows.append(f"""
                <tr>
                    <td><strong>{order.script}</strong></td>
                    <td>{order.trigger_price:,.2f}</td>
                    <td>{order.quantity}</td>
                    <td>{order.capital_deployed:,.0f}</td>
                    <td><span class="badge badge-warning">PENDING</span></td>
                </tr>
            """)

        return f"""
        <div class="card">
            <div class="card-title">Pending Entry Orders ({len(orders)})</div>
            <table>
                <thead>
                    <tr>
                        <th>Script</th>
                        <th>Trigger</th>
                        <th>Qty</th>
                        <th>Capital</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows)}
                </tbody>
            </table>
        </div>
        """

    def _generate_closed_trades_table(self, trades: List, title: str) -> str:
        """Generate HTML table for closed trades"""
        if not trades:
            return ""

        rows = []
        for trade in trades:
            pnl_class = 'pnl-positive' if trade.net_pnl >= 0 else 'pnl-negative'
            pnl_sign = '+' if trade.net_pnl >= 0 else ''
            badge_class = 'badge-success' if trade.net_pnl >= 0 else 'badge-danger'

            rows.append(f"""
                <tr>
                    <td><strong>{trade.script}</strong></td>
                    <td>{trade.entry_price:,.2f}</td>
                    <td>{trade.exit_price:,.2f}</td>
                    <td class="{pnl_class}">{pnl_sign}{trade.net_pnl:,.0f}</td>
                    <td class="{pnl_class}">{pnl_sign}{trade.pnl_percent:.1f}%</td>
                    <td>{trade.days_held}d</td>
                    <td><span class="badge {badge_class}">{trade.exit_reason}</span></td>
                </tr>
            """)

        return f"""
        <div class="card">
            <div class="card-title">{title} ({len(trades)})</div>
            <table>
                <thead>
                    <tr>
                        <th>Script</th>
                        <th>Entry</th>
                        <th>Exit</th>
                        <th>P&L</th>
                        <th>%</th>
                        <th>Days</th>
                        <th>Reason</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows)}
                </tbody>
            </table>
        </div>
        """

    def cleanup_old_reports(self, days: int = 7) -> int:
        """
        Delete reports older than specified days.

        Args:
            days: Delete reports older than this

        Returns:
            Number of files deleted
        """
        deleted = 0
        cutoff = datetime.now() - timedelta(days=days)

        try:
            for filepath in self.reports_dir.glob("*.html"):
                if filepath.stat().st_mtime < cutoff.timestamp():
                    filepath.unlink()
                    deleted += 1
                    logger.debug(f"Deleted old report: {filepath.name}")
        except Exception as e:
            logger.error(f"Error cleaning up reports: {e}")

        return deleted

    def delete_report(self, filepath: str) -> bool:
        """
        Delete a specific report file.

        Args:
            filepath: Path to report file

        Returns:
            True if deleted successfully
        """
        try:
            path = Path(filepath)
            if path.exists():
                path.unlink()
                logger.debug(f"Deleted report: {filepath}")
                return True
        except Exception as e:
            logger.warning(f"Could not delete report {filepath}: {e}")
        return False


# Singleton instance
report_generator = ReportGenerator()
