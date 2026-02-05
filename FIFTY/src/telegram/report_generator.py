"""
HTML Report Generator for FIFTY Bot - Landscape Modern Design

Generates professional, landscape-oriented HTML reports optimized for
Telegram display with improved readability and larger fonts.
All reports filter stale data and calculate metrics fresh from database.
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


REPORTS_DIR = Path(__file__).parent.parent.parent / "reports"


def ensure_reports_dir() -> Path:
    """Ensure reports directory exists"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    return REPORTS_DIR


class ReportGenerator:
    """Generates landscape HTML reports for FIFTY bot"""

    def __init__(self):
        self.reports_dir = ensure_reports_dir()

    def _get_compact_styles(self) -> str:
        """
        Landscape CSS optimized for Telegram image display.

        Color philosophy:
        - GREEN (#22c55e): Positive P&L, wins, profits ONLY
        - RED (#ef4444): Negative P&L, losses ONLY
        - CYAN (#06b6d4): Section headers, accents (neutral)
        - SLATE (#64748b, #94a3b8): Labels, secondary text
        - WHITE (#fff, #e8e8e8): Primary text, values
        """
        return """
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: 'Segoe UI', Roboto, Arial, sans-serif;
                background: #1a1a2e;
                color: #e8e8e8;
                padding: 40px;
                font-size: 32px;
                line-height: 1.5;
                min-width: 1400px;
            }
            .header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 24px 40px;
                background: linear-gradient(90deg, #0f3460, #16213e);
                border-radius: 16px;
                margin-bottom: 32px;
                border-left: 8px solid #06b6d4;
            }
            .header h1 {
                font-size: 48px;
                color: #fff;
                font-weight: 700;
            }
            .header .date {
                font-size: 28px;
                color: rgba(255,255,255,0.8);
                background: rgba(255,255,255,0.1);
                padding: 8px 24px;
                border-radius: 8px;
            }
            .grid-2 {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 32px;
                margin-bottom: 32px;
            }
            .grid-3 {
                display: grid;
                grid-template-columns: 1fr 1fr 1fr;
                gap: 32px;
                margin-bottom: 32px;
            }
            .section {
                background: rgba(255,255,255,0.05);
                border-radius: 16px;
                padding: 32px;
                margin-bottom: 32px;
                border: 2px solid rgba(255,255,255,0.08);
            }
            .section-title {
                font-size: 26px;
                color: #06b6d4;
                text-transform: uppercase;
                letter-spacing: 3px;
                margin-bottom: 24px;
                font-weight: 600;
            }
            .metrics {
                display: flex;
                gap: 24px;
                flex-wrap: wrap;
            }
            .metric {
                flex: 1;
                min-width: 200px;
                background: rgba(0,0,0,0.3);
                padding: 24px 32px;
                border-radius: 12px;
                text-align: center;
            }
            .metric-value {
                font-size: 56px;
                font-weight: 700;
                color: #fff;
            }
            .metric-label {
                font-size: 22px;
                color: #94a3b8;
                margin-top: 8px;
                text-transform: uppercase;
                letter-spacing: 1px;
            }
            .row {
                display: flex;
                justify-content: space-between;
                padding: 16px 0;
                border-bottom: 2px solid rgba(255,255,255,0.05);
            }
            .row:last-child { border-bottom: none; }
            .row-label { color: #94a3b8; font-size: 30px; }
            .row-value { font-weight: 600; font-size: 30px; color: #e8e8e8; }
            .positive { color: #22c55e; }
            .negative { color: #ef4444; }
            table {
                width: 100%;
                border-collapse: collapse;
                font-size: 28px;
            }
            th {
                background: rgba(6,182,212,0.1);
                color: #06b6d4;
                font-weight: 600;
                padding: 20px 24px;
                text-align: left;
                font-size: 24px;
                text-transform: uppercase;
            }
            td {
                padding: 20px 24px;
                border-bottom: 2px solid rgba(255,255,255,0.05);
            }
            .badge {
                display: inline-block;
                padding: 8px 20px;
                border-radius: 8px;
                font-size: 22px;
                font-weight: 600;
            }
            .badge-win { background: rgba(34,197,94,0.2); color: #22c55e; }
            .badge-loss { background: rgba(239,68,68,0.2); color: #ef4444; }
            .badge-pending { background: rgba(6,182,212,0.2); color: #06b6d4; }
            .footer {
                text-align: center;
                color: #64748b;
                font-size: 24px;
                padding-top: 24px;
                margin-top: 16px;
                border-top: 2px solid rgba(255,255,255,0.05);
            }
            .progress-bar {
                height: 12px;
                background: rgba(255,255,255,0.1);
                border-radius: 6px;
                margin-top: 16px;
            }
            .progress-fill {
                height: 100%;
                border-radius: 6px;
                background: linear-gradient(90deg, #06b6d4, #22c55e);
            }
            .empty { color: #64748b; text-align: center; padding: 32px; font-style: italic; }
        </style>
        """

    def _get_position_scripts(self, session) -> set:
        """Get set of scripts that have open positions"""
        positions = session.query(OpenPosition).filter(
            OpenPosition.status == PositionStatus.OPEN
        ).all()
        return {p.script for p in positions}

    def _filter_stale_orders(self, orders: List, position_scripts: set) -> List:
        """Filter out orders for scripts that already have positions"""
        return [o for o in orders if o.script not in position_scripts]

    def _calculate_deployed_capital(self, open_positions: List) -> float:
        """Calculate deployed capital from open positions"""
        return sum(p.capital_deployed for p in open_positions)

    def generate_daily_report(self) -> Optional[str]:
        """Generate compact daily report"""
        session = get_session()
        try:
            today = today_ist()

            # Get data
            open_positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()
            position_scripts = {p.script for p in open_positions}

            all_orders = session.query(OpenOrder).filter(
                OpenOrder.status == OrderStatus.PENDING
            ).all()
            pending_orders = self._filter_stale_orders(all_orders, position_scripts)

            signals_today = session.query(SignalQueue).filter(
                SignalQueue.signal_date == today
            ).all()

            trades_closed_today = session.query(ClosedPosition).filter(
                ClosedPosition.exit_date == today
            ).all()

            # Calculate fresh
            initial_capital = config.get('trading.initial_capital', 100000)
            deployed = self._calculate_deployed_capital(open_positions)
            available = initial_capital - deployed
            utilization = (deployed / initial_capital * 100) if initial_capital > 0 else 0

            today_pnl = sum(t.net_pnl for t in trades_closed_today) if trades_closed_today else 0
            all_trades = session.query(ClosedPosition).all()
            total_pnl = sum(t.net_pnl for t in all_trades) if all_trades else 0

            # Unrealized P&L
            unrealized = self._get_unrealized_pnl(open_positions)

            html = f"""
            <!DOCTYPE html>
            <html><head>
                <meta charset="UTF-8">
                <title>FIFTY Daily</title>
                {self._get_compact_styles()}
            </head><body>
                <div class="header">
                    <h1>FIFTY Daily Report</h1>
                    <div class="date">{today.strftime('%d %b %Y')}</div>
                </div>

                <div class="grid-2">
                    <div class="section">
                        <div class="section-title">Overview</div>
                        <div class="metrics">
                            <div class="metric">
                                <div class="metric-value">{len(open_positions)}</div>
                                <div class="metric-label">Positions</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value">{len(pending_orders)}</div>
                                <div class="metric-label">GTT Pending</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value">{len(signals_today)}</div>
                                <div class="metric-label">Signals</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value">{len(trades_closed_today)}</div>
                                <div class="metric-label">Closed</div>
                            </div>
                        </div>
                    </div>

                    <div class="section">
                        <div class="section-title">P&L Summary</div>
                        <div class="metrics">
                            <div class="metric">
                                <div class="metric-value {'positive' if today_pnl >= 0 else 'negative'}">{'+' if today_pnl >= 0 else ''}{today_pnl:,.0f}</div>
                                <div class="metric-label">Today</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value {'positive' if unrealized >= 0 else 'negative'}">{'+' if unrealized >= 0 else ''}{unrealized:,.0f}</div>
                                <div class="metric-label">Unrealized</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value {'positive' if total_pnl >= 0 else 'negative'}">{'+' if total_pnl >= 0 else ''}{total_pnl:,.0f}</div>
                                <div class="metric-label">Total</div>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="section" style="margin-bottom: 16px;">
                    <div class="section-title">Capital Utilization</div>
                    <div class="row">
                        <span class="row-label">Deployed</span>
                        <span class="row-value">{deployed:,.0f} ({utilization:.0f}%)</span>
                    </div>
                    <div class="row">
                        <span class="row-label">Available</span>
                        <span class="row-value">{available:,.0f}</span>
                    </div>
                    <div class="progress-bar">
                        <div class="progress-fill" style="width: {min(100, utilization):.0f}%"></div>
                    </div>
                </div>

                {self._positions_table(open_positions)}
                {self._orders_table(pending_orders)}

                <div class="footer">Generated {now_ist().strftime('%H:%M IST')} | FIFTY Bot</div>
            </body></html>
            """

            filepath = self.reports_dir / f"daily_{today.strftime('%Y%m%d')}.html"
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(html)

            logger.info(f"Daily report: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"Daily report error: {e}")
            return None
        finally:
            session.close()

    def generate_weekly_report(self) -> Optional[str]:
        """Generate compact weekly report"""
        session = get_session()
        try:
            today = today_ist()
            week_start = today - timedelta(days=7)

            week_trades = session.query(ClosedPosition).filter(
                ClosedPosition.exit_date >= week_start
            ).all()

            all_trades = session.query(ClosedPosition).all()
            open_positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()

            # Week stats
            week_count = len(week_trades)
            week_pnl = sum(t.net_pnl for t in week_trades) if week_trades else 0
            week_winners = sum(1 for t in week_trades if t.net_pnl > 0)
            week_win_rate = (week_winners / week_count * 100) if week_count > 0 else 0

            # All-time stats
            total_count = len(all_trades)
            total_pnl = sum(t.net_pnl for t in all_trades) if all_trades else 0
            total_winners = sum(1 for t in all_trades if t.net_pnl > 0)
            total_win_rate = (total_winners / total_count * 100) if total_count > 0 else 0

            initial_capital = config.get('trading.initial_capital', 100000)
            deployed = self._calculate_deployed_capital(open_positions)
            deployed_pct = (deployed / initial_capital * 100) if initial_capital > 0 else 0
            unrealized_pnl = self._get_unrealized_pnl(open_positions)
            unrealized_pct = (unrealized_pnl / deployed * 100) if deployed > 0 else 0

            html = f"""
            <!DOCTYPE html>
            <html><head>
                <meta charset="UTF-8">
                <title>FIFTY Weekly</title>
                {self._get_compact_styles()}
            </head><body>
                <div class="header">
                    <h1>FIFTY Weekly Report</h1>
                    <div class="date">{week_start.strftime('%d %b')} - {today.strftime('%d %b %Y')}</div>
                </div>

                <div class="grid-2">
                    <div class="section">
                        <div class="section-title">This Week</div>
                        <div class="metrics">
                            <div class="metric">
                                <div class="metric-value">{week_count}</div>
                                <div class="metric-label">Trades</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value {'positive' if week_pnl >= 0 else 'negative'}">{'+' if week_pnl >= 0 else ''}{week_pnl:,.0f}</div>
                                <div class="metric-label">P&L</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value">{week_win_rate:.0f}%</div>
                                <div class="metric-label">Win Rate</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value">{week_winners}/{week_count}</div>
                                <div class="metric-label">W/L</div>
                            </div>
                        </div>
                    </div>

                    <div class="section">
                        <div class="section-title">All-Time Performance</div>
                        <div class="metrics">
                            <div class="metric">
                                <div class="metric-value">{total_count}</div>
                                <div class="metric-label">Trades</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value {'positive' if total_pnl >= 0 else 'negative'}">{'+' if total_pnl >= 0 else ''}{total_pnl:,.0f}</div>
                                <div class="metric-label">P&L</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value">{total_win_rate:.0f}%</div>
                                <div class="metric-label">Win Rate</div>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="section">
                    <div class="section-title">Current Status</div>
                    <div class="row">
                        <span class="row-label">Open Positions</span>
                        <span class="row-value">{len(open_positions)}</span>
                    </div>
                    <div class="row">
                        <span class="row-label">Deployed Capital</span>
                        <span class="row-value">{deployed:,.0f} ({deployed_pct:.0f}%)</span>
                    </div>
                    <div class="row">
                        <span class="row-label">Unrealized P&L</span>
                        <span class="row-value {'positive' if unrealized_pnl >= 0 else 'negative'}">{unrealized_pnl:+,.0f} ({'+' if unrealized_pct >= 0 else ''}{unrealized_pct:.1f}%)</span>
                    </div>
                </div>

                {self._positions_table(open_positions)}
                {self._closed_trades_table(week_trades, "Week's Trades")}

                <div class="footer">Generated {now_ist().strftime('%H:%M IST')} | FIFTY Bot</div>
            </body></html>
            """

            filepath = self.reports_dir / f"weekly_{today.strftime('%Y%m%d')}.html"
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(html)

            logger.info(f"Weekly report: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"Weekly report error: {e}")
            return None
        finally:
            session.close()

    def generate_monthly_report(self) -> Optional[str]:
        """Generate compact monthly report"""
        session = get_session()
        try:
            today = today_ist()
            month_start = today.replace(day=1)
            month_name = today.strftime('%B %Y')

            month_trades = session.query(ClosedPosition).filter(
                ClosedPosition.exit_date >= month_start
            ).all()

            all_trades = session.query(ClosedPosition).all()
            open_positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()

            # Month stats
            month_count = len(month_trades)
            month_pnl = sum(t.net_pnl for t in month_trades) if month_trades else 0
            month_winners = sum(1 for t in month_trades if t.net_pnl > 0)
            month_win_rate = (month_winners / month_count * 100) if month_count > 0 else 0

            # All-time
            total_count = len(all_trades)
            total_pnl = sum(t.net_pnl for t in all_trades) if all_trades else 0
            total_win_rate = (sum(1 for t in all_trades if t.net_pnl > 0) / total_count * 100) if total_count > 0 else 0

            initial_capital = config.get('trading.initial_capital', 100000)
            deployed = self._calculate_deployed_capital(open_positions)
            month_roi = (month_pnl / initial_capital * 100) if initial_capital > 0 else 0
            total_roi = (total_pnl / initial_capital * 100) if initial_capital > 0 else 0

            html = f"""
            <!DOCTYPE html>
            <html><head>
                <meta charset="UTF-8">
                <title>FIFTY Monthly</title>
                {self._get_compact_styles()}
            </head><body>
                <div class="header">
                    <h1>FIFTY Monthly Report</h1>
                    <div class="date">{month_name}</div>
                </div>

                <div class="grid-2">
                    <div class="section">
                        <div class="section-title">This Month</div>
                        <div class="metrics">
                            <div class="metric">
                                <div class="metric-value">{month_count}</div>
                                <div class="metric-label">Trades</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value {'positive' if month_pnl >= 0 else 'negative'}">{'+' if month_pnl >= 0 else ''}{month_pnl:,.0f}</div>
                                <div class="metric-label">P&L</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value">{month_win_rate:.0f}%</div>
                                <div class="metric-label">Win Rate</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value {'positive' if month_roi >= 0 else 'negative'}">{'+' if month_roi >= 0 else ''}{month_roi:.1f}%</div>
                                <div class="metric-label">ROI</div>
                            </div>
                        </div>
                    </div>

                    <div class="section">
                        <div class="section-title">All-Time Performance</div>
                        <div class="metrics">
                            <div class="metric">
                                <div class="metric-value">{total_count}</div>
                                <div class="metric-label">Trades</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value {'positive' if total_pnl >= 0 else 'negative'}">{'+' if total_pnl >= 0 else ''}{total_pnl:,.0f}</div>
                                <div class="metric-label">P&L</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value">{total_win_rate:.0f}%</div>
                                <div class="metric-label">Win Rate</div>
                            </div>
                            <div class="metric">
                                <div class="metric-value {'positive' if total_roi >= 0 else 'negative'}">{'+' if total_roi >= 0 else ''}{total_roi:.1f}%</div>
                                <div class="metric-label">ROI</div>
                            </div>
                        </div>
                    </div>
                </div>

                {self._positions_table(open_positions)}
                {self._closed_trades_table(month_trades, "Month's Trades")}

                <div class="footer">Generated {now_ist().strftime('%H:%M IST')} | FIFTY Bot</div>
            </body></html>
            """

            filepath = self.reports_dir / f"monthly_{today.strftime('%Y%m%d')}.html"
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(html)

            logger.info(f"Monthly report: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"Monthly report error: {e}")
            return None
        finally:
            session.close()

    def generate_overall_report(self) -> Optional[str]:
        """Generate compact overall/lifetime report"""
        session = get_session()
        try:
            today = today_ist()

            all_trades = session.query(ClosedPosition).order_by(
                ClosedPosition.exit_date.desc()
            ).all()

            open_positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()

            # Stats
            total_count = len(all_trades)
            total_pnl = sum(t.net_pnl for t in all_trades) if all_trades else 0
            total_winners = sum(1 for t in all_trades if t.net_pnl > 0)
            total_losers = total_count - total_winners
            win_rate = (total_winners / total_count * 100) if total_count > 0 else 0

            avg_winner = sum(t.net_pnl for t in all_trades if t.net_pnl > 0) / total_winners if total_winners > 0 else 0
            avg_loser = sum(t.net_pnl for t in all_trades if t.net_pnl <= 0) / total_losers if total_losers > 0 else 0
            avg_days = sum(t.days_held for t in all_trades) / total_count if total_count > 0 else 0

            gross_profit = sum(t.net_pnl for t in all_trades if t.net_pnl > 0)
            gross_loss = abs(sum(t.net_pnl for t in all_trades if t.net_pnl <= 0))
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

            initial_capital = config.get('trading.initial_capital', 100000)
            deployed = self._calculate_deployed_capital(open_positions)
            deployed_pct = (deployed / initial_capital * 100) if initial_capital > 0 else 0
            total_roi = (total_pnl / initial_capital * 100) if initial_capital > 0 else 0
            unrealized = self._get_unrealized_pnl(open_positions)
            unrealized_pct = (unrealized / deployed * 100) if deployed > 0 else 0

            best = max(all_trades, key=lambda t: t.net_pnl) if all_trades else None
            worst = min(all_trades, key=lambda t: t.net_pnl) if all_trades else None

            html = f"""
            <!DOCTYPE html>
            <html><head>
                <meta charset="UTF-8">
                <title>FIFTY Overall</title>
                {self._get_compact_styles()}
            </head><body>
                <div class="header">
                    <h1>FIFTY Overall Performance</h1>
                    <div class="date">Lifetime Report</div>
                </div>

                <div class="section">
                    <div class="section-title">Performance Summary</div>
                    <div class="metrics">
                        <div class="metric">
                            <div class="metric-value">{total_count}</div>
                            <div class="metric-label">Total Trades</div>
                        </div>
                        <div class="metric">
                            <div class="metric-value {'positive' if total_pnl >= 0 else 'negative'}">{'+' if total_pnl >= 0 else ''}{total_pnl:,.0f}</div>
                            <div class="metric-label">Net P&L</div>
                        </div>
                        <div class="metric">
                            <div class="metric-value">{win_rate:.0f}%</div>
                            <div class="metric-label">Win Rate</div>
                        </div>
                        <div class="metric">
                            <div class="metric-value {'positive' if total_roi >= 0 else 'negative'}">{'+' if total_roi >= 0 else ''}{total_roi:.1f}%</div>
                            <div class="metric-label">ROI</div>
                        </div>
                    </div>
                </div>

                <div class="grid-2">
                    <div class="section">
                        <div class="section-title">Statistics</div>
                        <div class="row">
                            <span class="row-label">Win/Loss Record</span>
                            <span class="row-value">{total_winners}W / {total_losers}L</span>
                        </div>
                        <div class="row">
                            <span class="row-label">Avg Winner</span>
                            <span class="row-value {'positive' if total_winners > 0 else ''}">{f'+{avg_winner:,.0f}' if total_winners > 0 else 'N/A'}</span>
                        </div>
                        <div class="row">
                            <span class="row-label">Avg Loser</span>
                            <span class="row-value {'negative' if total_losers > 0 else ''}">{f'{avg_loser:,.0f}' if total_losers > 0 else 'N/A'}</span>
                        </div>
                        <div class="row">
                            <span class="row-label">Profit Factor</span>
                            <span class="row-value">{profit_factor:.2f}x</span>
                        </div>
                        <div class="row">
                            <span class="row-label">Avg Hold Period</span>
                            <span class="row-value">{avg_days:.0f} days</span>
                        </div>
                    </div>

                    <div class="section">
                        <div class="section-title">Current Status</div>
                        <div class="row">
                            <span class="row-label">Open Positions</span>
                            <span class="row-value">{len(open_positions)}</span>
                        </div>
                        <div class="row">
                            <span class="row-label">Deployed Capital</span>
                            <span class="row-value">{deployed:,.0f} ({deployed_pct:.0f}%)</span>
                        </div>
                        <div class="row">
                            <span class="row-label">Unrealized P&L</span>
                            <span class="row-value {'positive' if unrealized >= 0 else 'negative'}">{'+' if unrealized >= 0 else ''}{unrealized:,.0f} ({'+' if unrealized_pct >= 0 else ''}{unrealized_pct:.1f}%)</span>
                        </div>
                        {f'''<div class="row">
                            <span class="row-label">Best Trade</span>
                            <span class="row-value {'positive' if best.net_pnl >= 0 else 'negative'}">{best.script}: {'+' if best.net_pnl >= 0 else ''}{best.net_pnl:,.0f}</span>
                        </div>
                        <div class="row">
                            <span class="row-label">Worst Trade</span>
                            <span class="row-value {'positive' if worst.net_pnl >= 0 else 'negative'}">{worst.script}: {'+' if worst.net_pnl >= 0 else ''}{worst.net_pnl:,.0f}</span>
                        </div>''' if best and worst else ''}
                    </div>
                </div>

                {self._positions_table(open_positions)}
                {self._closed_trades_table(all_trades[:5], "Recent Trades")}

                <div class="footer">Generated {now_ist().strftime('%H:%M IST')} | FIFTY Bot</div>
            </body></html>
            """

            filepath = self.reports_dir / f"overall_{today.strftime('%Y%m%d')}.html"
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(html)

            logger.info(f"Overall report: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"Overall report error: {e}")
            return None
        finally:
            session.close()

    def html_to_image(self, html_path: str) -> Optional[str]:
        """Convert HTML to JPEG image (landscape, optimized for Telegram <10MB)"""
        try:
            import imgkit
            from pathlib import Path

            html_file = Path(html_path)
            image_path = html_file.with_suffix('.jpg')

            # Optimized for Telegram (must be under 10MB)
            options = {
                'format': 'jpg',
                'width': 1400,  # Landscape width
                'quality': 85,  # Good quality, smaller file
                'enable-local-file-access': None,
                'quiet': '',
            }

            imgkit.from_file(str(html_file), str(image_path), options=options)

            if image_path.exists() and image_path.stat().st_size > 0:
                size_mb = image_path.stat().st_size / (1024 * 1024)
                logger.info(f"Image created: {image_path} ({size_mb:.1f}MB)")

                # If still too large, reduce quality further
                if size_mb > 9:
                    logger.warning(f"Image too large ({size_mb:.1f}MB), reducing quality")
                    options['quality'] = 60
                    imgkit.from_file(str(html_file), str(image_path), options=options)
                    size_mb = image_path.stat().st_size / (1024 * 1024)
                    logger.info(f"Reduced image: {size_mb:.1f}MB")

                return str(image_path)
            return None

        except ImportError:
            logger.warning("imgkit not installed")
            return None
        except Exception as e:
            logger.warning(f"Image conversion failed: {e}")
            return None

    def _get_unrealized_pnl(self, positions: List) -> float:
        """Calculate unrealized P&L for positions"""
        unrealized = 0
        try:
            from src.api.dual_kite_client import get_kite_client
            kite = get_kite_client()
            for pos in positions:
                try:
                    token = kite.get_instrument_token(pos.script)
                    if token:
                        ltp = kite.get_instrument_ltp(token)
                        if ltp:
                            unrealized += (ltp - pos.entry_price) * pos.quantity
                except Exception:
                    pass
        except Exception:
            pass
        return unrealized

    def _positions_table(self, positions: List) -> str:
        """Compact positions table with dynamic days_held and unrealized P&L"""
        if not positions:
            return '<div class="section"><div class="empty">No open positions</div></div>'

        # Fetch LTPs for unrealized P&L
        ltp_map: dict = {}
        try:
            from src.api.dual_kite_client import get_kite_client
            kite = get_kite_client()
            for p in positions:
                try:
                    token = kite.get_instrument_token(p.script)
                    if token:
                        ltp = kite.get_instrument_ltp(token)
                        if ltp:
                            ltp_map[p.script] = ltp
                except Exception:
                    pass
        except Exception:
            pass

        today = today_ist()
        initial_capital = config.get('trading.initial_capital', 100000)
        rows = ''
        for p in positions:
            days = (today - p.entry_date).days if p.entry_date else 0
            ltp = ltp_map.get(p.script)
            # Capital % of total
            cap_pct = (p.capital_deployed / initial_capital * 100) if initial_capital > 0 else 0
            if ltp:
                unrealized = (ltp - p.entry_price) * p.quantity
                pnl_pct = (unrealized / p.capital_deployed * 100) if p.capital_deployed > 0 else 0
                pnl_class = 'positive' if unrealized >= 0 else 'negative'
                pnl_sign = '+' if pnl_pct >= 0 else ''
                pnl_str = f'<span class="{pnl_class}">{unrealized:+,.0f} ({pnl_sign}{pnl_pct:.1f}%)</span>'
            else:
                pnl_str = '-'
            rows += (
                f'<tr><td><b>{p.script}</b></td><td>{p.entry_price:,.0f}</td><td>{p.quantity}</td>'
                f'<td>{p.current_sl:,.0f}</td><td>{pnl_str}</td><td>{cap_pct:.0f}%</td><td>{days}d</td></tr>'
            )

        return f'''
        <div class="section">
            <div class="section-title">Open Positions ({len(positions)})</div>
            <table>
                <tr><th>Script</th><th>Entry</th><th>Qty</th><th>SL</th><th>Unrl P&L</th><th>Cap%</th><th>Days</th></tr>
                {rows}
            </table>
        </div>
        '''

    def _orders_table(self, orders: List) -> str:
        """Compact pending orders table"""
        if not orders:
            return ''

        rows = ''.join([
            f'<tr><td><b>{o.script}</b></td><td>{o.trigger_price:,.0f}</td><td>{o.quantity}</td>'
            f'<td>{o.capital_deployed:,.0f}</td><td><span class="badge badge-pending">PENDING</span></td></tr>'
            for o in orders
        ])

        return f'''
        <div class="section">
            <div class="section-title">GTT Orders ({len(orders)})</div>
            <table>
                <tr><th>Script</th><th>Trigger</th><th>Qty</th><th>Capital</th><th>Status</th></tr>
                {rows}
            </table>
        </div>
        '''

    def _closed_trades_table(self, trades: List, title: str) -> str:
        """Compact closed trades table"""
        if not trades:
            return ''

        rows = ''.join([
            f'<tr><td><b>{t.script}</b></td><td>{t.entry_price:,.0f}</td><td>{t.exit_price:,.0f}</td>'
            f'<td class="{"positive" if t.net_pnl >= 0 else "negative"}">{("+" if t.net_pnl >= 0 else "")}{t.net_pnl:,.0f}</td>'
            f'<td class="{"positive" if t.pnl_percent >= 0 else "negative"}">{("+" if t.pnl_percent >= 0 else "")}{t.pnl_percent:.1f}%</td>'
            f'<td><span class="badge {"badge-win" if t.net_pnl >= 0 else "badge-loss"}">{t.exit_reason}</span></td></tr>'
            for t in trades
        ])

        return f'''
        <div class="section">
            <div class="section-title">{title} ({len(trades)})</div>
            <table>
                <tr><th>Script</th><th>Entry</th><th>Exit</th><th>P&L</th><th>%</th><th>Exit</th></tr>
                {rows}
            </table>
        </div>
        '''

    def delete_report(self, filepath: str) -> bool:
        """Delete a report file"""
        try:
            path = Path(filepath)
            if path.exists():
                path.unlink()
                return True
        except Exception:
            pass
        return False

    def cleanup_old_reports(self, days: int = 7) -> int:
        """Delete reports older than specified days"""
        deleted = 0
        cutoff = datetime.now() - timedelta(days=days)
        try:
            for f in self.reports_dir.glob("*.*"):
                if f.stat().st_mtime < cutoff.timestamp():
                    f.unlink()
                    deleted += 1
        except Exception:
            pass
        return deleted


# Singleton
report_generator = ReportGenerator()
