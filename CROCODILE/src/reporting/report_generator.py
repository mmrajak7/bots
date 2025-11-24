"""Report Generator for Daily and Weekly Summaries"""

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional
from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.database import (
    CapitalLedger, OpenPosition, ClosedPosition, TransactionHistory,
    PositionStatus, get_session
)
from src.reporting.telegram_client import telegram
from src.reporting.report_formatter import fmt
from src.api.kite_trade_client import KiteTradeClient


class ReportGenerator:
    """Generate daily and weekly performance reports"""

    def __init__(self):
        """Initialize report generator"""
        self.kite_client = KiteTradeClient()

    def generate_daily_report(self, session: Optional[Session] = None) -> str:
        """
        Generate daily EOD report

        Returns:
            Formatted report string
        """
        close_session = False
        if session is None:
            session = get_session()
            close_session = True

        try:
            today = date.today()

            # Get today's capital ledger
            ledger = session.query(CapitalLedger).filter_by(date=today).first()

            if not ledger:
                error_msg = (
                    f"🚨 *DAILY REPORT GENERATION FAILED*\n\n"
                    f"📅 Date: {today.strftime('%d %b %Y')}\n"
                    f"❌ Error: No capital ledger data available\n\n"
                    f"**Possible causes:**\n"
                    f"- Capital ledger update failed today\n"
                    f"- Database connectivity issues\n"
                    f"- Bot not running during market hours\n\n"
                    f"**Action required:**\n"
                    f"- Check bot logs for errors\n"
                    f"- Verify database connection\n"
                    f"- Manually update capital ledger if needed"
                )

                # Send critical alert
                try:
                    telegram.send_alert(error_msg, critical=True)
                except Exception as e:
                    logger.error(f"Failed to send ledger missing alert: {e}")

                return error_msg

            # Get open positions with unrealized P&L
            open_positions = session.query(OpenPosition).filter_by(
                status=PositionStatus.OPEN
            ).all()

            # Calculate unrealized P&L
            total_unrealized = 0.0
            position_details = []
            ltp_fetch_failures = []  # Track positions where LTP fetch failed

            for pos in open_positions:
                try:
                    instrument_token = self.kite_client.get_instrument_token(pos.script)
                    current_ltp = self.kite_client.get_instrument_ltp(instrument_token)
                    unrealized = (current_ltp - pos.entry_price) * pos.quantity
                    total_unrealized += unrealized

                    position_details.append({
                        'script': pos.script,
                        'tf': pos.timeframe,
                        'entry': pos.entry_price,
                        'qty': pos.quantity,
                        'current_ltp': current_ltp,
                        'sl': pos.current_sl,
                        'days': pos.days_held,
                        'unrealized': unrealized,
                        'ltp_failed': False
                    })
                except Exception as e:
                    # CRITICAL: Don't exclude position! Include with fallback values
                    logger.error(f"Failed to get LTP for {pos.script}: {e}")

                    # Use entry price as fallback (P&L = 0)
                    fallback_ltp = pos.entry_price
                    unrealized_fallback = 0.0  # Can't calculate without real LTP

                    position_details.append({
                        'script': pos.script,
                        'tf': pos.timeframe,
                        'entry': pos.entry_price,
                        'qty': pos.quantity,
                        'current_ltp': fallback_ltp,
                        'sl': pos.current_sl,
                        'days': pos.days_held,
                        'unrealized': unrealized_fallback,
                        'ltp_failed': True,  # Flag for display
                        'ltp_error': str(e)[:50]  # Store error
                    })

                    # Track failure
                    ltp_fetch_failures.append(f"{pos.script} ({pos.timeframe}): {str(e)[:50]}")

            # Get today's closed positions
            today_exits = session.query(ClosedPosition).filter_by(
                exit_date=today
            ).all()

            # Get today's entries
            today_entries = session.query(TransactionHistory).filter_by(
                transaction_date=today,
                transaction_type='ENTRY'
            ).all()

            # ========== BUILD COMPACT REPORT ==========
            report = f"📊 *DAILY SUMMARY* | {today.strftime('%d %b %Y')}\n"
            report += fmt.THIN_LINE + "\n\n"

            # Warning banner if LTP failures
            if ltp_fetch_failures:
                report += f"⚠️ LTP fetch failed: {len(ltp_fetch_failures)} pos (P&L incomplete)\n\n"

            # === CAPITAL (single line) ===
            deployed_pct = (ledger.deployed_capital / ledger.total_capital * 100) if ledger.total_capital > 0 else 0
            report += f"💰 *CAPITAL*\n"
            report += f"{fmt.format_currency(ledger.total_capital)} | "
            report += f"Deployed: {fmt.format_currency(ledger.deployed_capital)} ({deployed_pct:.0f}%) | "
            report += f"Free: {fmt.format_currency(ledger.free_capital)}\n\n"

            # === TODAY'S P&L (compact) ===
            total_pnl = ledger.realized_pnl_today + total_unrealized
            pnl_emoji = fmt.pnl_emoji(total_pnl)
            report += f"📈 *TODAY* {pnl_emoji}\n"
            report += f"Realized: {fmt.format_currency(ledger.realized_pnl_today, show_sign=True)} | "
            report += f"Unrealized: {fmt.format_currency(total_unrealized, show_sign=True)}"
            if ltp_fetch_failures:
                report += " ⚠️"
            report += f"\nTrades: {ledger.num_trades_today} (Entry:{ledger.num_entries_today} Exit:{ledger.num_exits_today})\n\n"

            # === OPEN POSITIONS (compact table) ===
            report += f"📊 *POSITIONS* ({len(open_positions)})\n"
            if position_details:
                # Sort by unrealized P&L (worst first)
                sorted_positions = sorted(position_details, key=lambda x: x.get('unrealized', 0))
                for pos in sorted_positions[:6]:  # Show max 6
                    if pos.get('ltp_failed', False):
                        report += f"⚠️ {pos['script']}({pos['tf']}) {pos['qty']}@{pos['entry']:.0f} LTP:N/A\n"
                    else:
                        emoji = fmt.pnl_emoji(pos['unrealized'])
                        pnl_str = fmt.format_currency(pos['unrealized'], show_sign=True)
                        report += f"{emoji} {pos['script']}({pos['tf']}) {pos['qty']}@{pos['entry']:.0f} LTP:{pos['current_ltp']:.0f} SL:{pos['sl']:.0f} {pnl_str}\n"

                if len(position_details) > 6:
                    remaining = len(position_details) - 6
                    remaining_pnl = sum(p.get('unrealized', 0) for p in sorted_positions[6:])
                    report += f"   +{remaining} more ({fmt.format_currency(remaining_pnl, show_sign=True)})\n"
            else:
                report += "No open positions\n"
            report += "\n"

            # === TODAY'S ENTRIES (compact) ===
            if today_entries:
                report += f"✅ *ENTRIES* ({len(today_entries)})\n"
                for entry in today_entries[:4]:
                    report += f"  {entry.script}({entry.timeframe}) {entry.quantity}@{entry.price:.0f}\n"
                if len(today_entries) > 4:
                    report += f"  +{len(today_entries) - 4} more\n"
                report += "\n"

            # === TODAY'S EXITS (compact with P&L) ===
            if today_exits:
                total_exit_pnl = sum(e.net_pnl for e in today_exits)
                exit_emoji = fmt.pnl_emoji(total_exit_pnl)
                report += f"🚪 *EXITS* ({len(today_exits)}) {exit_emoji} {fmt.format_currency(total_exit_pnl, show_sign=True)}\n"
                for exit in today_exits[:4]:
                    emoji = fmt.pnl_emoji(exit.net_pnl)
                    report += f"{emoji} {exit.script}({exit.timeframe}) @{exit.exit_price:.0f} {fmt.format_currency(exit.net_pnl, show_sign=True)} ({exit.pnl_percent:+.1f}%) {exit.days_held}d\n"
                if len(today_exits) > 4:
                    report += f"  +{len(today_exits) - 4} more\n"
                report += "\n"

            # === RISK STATUS (single line) ===
            dd_emoji = "✅" if ledger.monthly_drawdown_pct < 5 else "⚠️" if ledger.monthly_drawdown_pct < 10 else "🔴"
            risk_status = "OK" if ledger.monthly_drawdown_pct < 5 else "CAUTION" if ledger.monthly_drawdown_pct < 10 else "HIGH"
            report += f"📉 *RISK* {dd_emoji} DD:{ledger.monthly_drawdown_pct:.1f}% | Status:{risk_status}\n"

            # === FOOTER ===
            report += fmt.THIN_LINE + "\n"
            report += f"🤖 Bot: OK | Next: {self._get_next_market_day()}"

            # === SEND SEPARATE ALERT IF LTP FETCH FAILURES ===
            # Send critical alert if multiple positions have LTP fetch failures
            if len(ltp_fetch_failures) > 0:
                alert_msg = (
                    f"⚠️ **DAILY REPORT - LTP FETCH FAILURES**\n\n"
                    f"Failed to fetch LTP for {len(ltp_fetch_failures)} position(s):\n\n"
                )

                for failure in ltp_fetch_failures:
                    alert_msg += f"• {failure}\n"

                alert_msg += (
                    f"\n**Impact:**\n"
                    f"- Unrealized P&L calculation incomplete\n"
                    f"- Position monitoring may be affected\n\n"
                    f"**Possible causes:**\n"
                    f"- Zerodha API rate limiting\n"
                    f"- Network connectivity issues\n"
                    f"- Invalid instrument tokens\n\n"
                    f"**Action:**\n"
                    f"- Check Zerodha API status\n"
                    f"- Verify instrument token cache\n"
                    f"- Review bot logs for details"
                )

                try:
                    # Send as critical if >50% of positions failed
                    is_critical = len(ltp_fetch_failures) > len(open_positions) / 2
                    telegram.send_alert(alert_msg, critical=is_critical)
                except Exception as e:
                    logger.error(f"Failed to send LTP failure alert: {e}")

            return report

        finally:
            if close_session:
                session.close()

    def generate_weekly_report(self, session: Optional[Session] = None) -> str:
        """
        Generate weekly summary report (sent on Fridays)

        Returns:
            Formatted report string
        """
        close_session = False
        if session is None:
            session = get_session()
            close_session = True

        try:
            today = date.today()

            # Get week's date range (Monday to Friday)
            days_since_monday = today.weekday()  # Monday = 0
            week_start = today - timedelta(days=days_since_monday)
            week_end = today

            # Get capital ledgers for the week
            ledgers = session.query(CapitalLedger).filter(
                CapitalLedger.date >= week_start,
                CapitalLedger.date <= week_end
            ).order_by(CapitalLedger.date).all()

            if not ledgers:
                error_msg = (
                    f"🚨 *WEEKLY REPORT GENERATION FAILED*\n\n"
                    f"📅 Week: {week_start.strftime('%d %b')} to {week_end.strftime('%d %b %Y')}\n"
                    f"❌ Error: No capital ledger data for this week\n\n"
                    f"**Possible causes:**\n"
                    f"- Bot was not running this week\n"
                    f"- Capital ledger updates failed\n"
                    f"- Database connectivity issues\n\n"
                    f"**Action required:**\n"
                    f"- Check bot logs for the week\n"
                    f"- Verify bot was running daily\n"
                    f"- Check database integrity"
                )

                # Send critical alert
                try:
                    telegram.send_alert(error_msg, critical=True)
                except Exception as e:
                    logger.error(f"Failed to send weekly report error alert: {e}")

                return error_msg

            # Validation: Check for missing days
            expected_days = 5  # Monday-Friday
            actual_days = len(ledgers)
            if actual_days < expected_days:
                logger.warning(
                    f"Weekly report: Only {actual_days}/{expected_days} days of data available"
                )

            week_start_capital = ledgers[0].opening_capital
            week_end_capital = ledgers[-1].total_capital
            net_pnl = week_end_capital - week_start_capital
            pnl_pct = (net_pnl / week_start_capital) * 100 if week_start_capital > 0 else 0

            # Get week's closed trades
            weekly_trades = session.query(ClosedPosition).filter(
                ClosedPosition.exit_date >= week_start,
                ClosedPosition.exit_date <= week_end
            ).all()

            # Calculate stats
            total_trades = len(weekly_trades)
            winning_trades = sum(1 for t in weekly_trades if t.net_pnl > 0)
            losing_trades = total_trades - winning_trades
            win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

            avg_win = sum(t.net_pnl for t in weekly_trades if t.net_pnl > 0) / winning_trades if winning_trades > 0 else 0
            avg_loss = sum(t.net_pnl for t in weekly_trades if t.net_pnl < 0) / losing_trades if losing_trades > 0 else 0

            # Build report
            report = f"📊 *CROCODILE - WEEKLY SUMMARY*\n"
            report += f"📅 Week: {week_start.strftime('%d %b')} to {week_end.strftime('%d %b %Y')}\n"

            # Add data quality warning if incomplete week data
            if actual_days < expected_days:
                report += f"\n⚠️ *DATA WARNING*\n"
                report += f"├─ Only {actual_days}/{expected_days} trading days of data\n"
                report += f"└─ Weekly metrics may be incomplete\n"

            report += "\n"

            # Weekly Performance
            report += f"💰 *WEEKLY PERFORMANCE*\n"
            pnl_emoji = "🟢" if net_pnl >= 0 else "🔴"
            report += f"├─ Starting Capital: Rs.{week_start_capital:,.2f}\n"
            report += f"├─ Ending Capital: Rs.{week_end_capital:,.2f}\n"
            report += f"└─ Net P&L: {pnl_emoji} Rs.{net_pnl:,.2f} ({pnl_pct:.2f}%)\n\n"

            # Trade Statistics
            report += f"📈 *TRADE STATISTICS*\n"
            report += f"├─ Total Trades: {total_trades}\n"
            report += f"├─ Winning Trades: {winning_trades} ({win_rate:.1f}%)\n"
            report += f"├─ Losing Trades: {losing_trades}\n"
            report += f"├─ Average Win: Rs.{avg_win:,.2f}\n"
            report += f"└─ Average Loss: Rs.{avg_loss:,.2f}\n\n"

            # Current Status
            open_positions = session.query(OpenPosition).filter_by(status=PositionStatus.OPEN).count()
            deployed = ledgers[-1].deployed_capital
            available = ledgers[-1].free_capital
            utilization = (deployed / (deployed + available) * 100) if (deployed + available) > 0 else 0

            report += f"📊 *CURRENT STATUS*\n"
            report += f"├─ Open Positions: {open_positions}\n"
            report += f"├─ Deployed Capital: Rs.{deployed:,.2f}\n"
            report += f"├─ Available Capital: Rs.{available:,.2f}\n"
            report += f"└─ Capital Utilization: {utilization:.1f}%\n\n"

            # Top Performers
            if weekly_trades:
                top_5 = sorted(weekly_trades, key=lambda x: x.net_pnl, reverse=True)[:5]
                worst_5 = sorted(weekly_trades, key=lambda x: x.net_pnl)[:5]

                report += f"🏆 *TOP PERFORMERS*\n"
                for i, trade in enumerate(top_5, 1):
                    report += f"{i}. {trade.script} ({trade.timeframe}): Rs.{trade.net_pnl:,.2f} ({trade.pnl_percent:.1f}%)\n"
                report += "\n"

                report += f"📉 *WORST PERFORMERS*\n"
                for i, trade in enumerate(worst_5, 1):
                    report += f"{i}. {trade.script} ({trade.timeframe}): Rs.{trade.net_pnl:,.2f} ({trade.pnl_percent:.1f}%)\n"
                report += "\n"

            # Footer
            report += "---\n"
            report += "🤖 Have a great weekend! 🎉\n"
            report += f"📊 Next report: Next Friday\n"

            return report

        finally:
            if close_session:
                session.close()

    def _get_next_market_day(self) -> str:
        """Get next market day (skip weekends)"""
        tomorrow = date.today() + timedelta(days=1)

        # Skip weekends
        while tomorrow.weekday() >= 5:  # 5=Saturday, 6=Sunday
            tomorrow += timedelta(days=1)

        return tomorrow.strftime('%d %b %Y (%A)')

    def send_daily_report(self):
        """Generate and send daily report via Telegram"""
        try:
            logger.info("Generating daily report...")
            report = self.generate_daily_report()
            success = telegram.send_formatted_report(report)

            if success:
                logger.info("✅ Daily report sent successfully")
            else:
                logger.warning("⚠️ Failed to send daily report")

        except Exception as e:
            logger.error(f"Failed to generate/send daily report: {e}")

    def send_weekly_report(self):
        """Generate and send weekly report via Telegram (Fridays only)"""
        try:
            if date.today().weekday() != 4:  # 4 = Friday
                logger.info("Not Friday, skipping weekly report")
                return

            logger.info("Generating weekly report...")
            report = self.generate_weekly_report()
            success = telegram.send_formatted_report(report)

            if success:
                logger.info("✅ Weekly report sent successfully")
            else:
                logger.warning("⚠️ Failed to send weekly report")

        except Exception as e:
            logger.error(f"Failed to generate/send weekly report: {e}")


# Singleton instance
report_generator = ReportGenerator()
