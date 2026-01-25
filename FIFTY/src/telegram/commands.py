"""
Telegram Commands Handler - Handles slash commands for FIFTY bot

Commands:
- /positions - List all open positions
- /pending - List pending approvals and hold signals
- /stats - Win rate, avg P&L, total trades
- /capital - Show capital allocation
- /report - Generate and send today's summary (HTML)
- /weekly - Generate and send weekly report (HTML)
- /kill - Activate kill switch
- /resume - Deactivate kill switch
"""

from typing import Dict, Any, List
from datetime import datetime, timedelta
from loguru import logger

from src.telegram.bot import telegram
from src.models.database import (
    get_session, SignalQueue, OpenPosition, OpenOrder, ClosedPosition,
    CapitalLedger, SignalStatus, PositionStatus, OrderStatus,
    is_kill_switch_active, set_kill_switch
)
from src.utils.timezone_helper import today_ist, now_ist
from src.utils.config_manager import config


class CommandHandler:
    """Handles Telegram slash commands"""

    def execute_command(self, command: str) -> None:
        """Execute a command"""
        try:
            if command == 'positions':
                self._cmd_positions()
            elif command == 'pending':
                self._cmd_pending()
            elif command == 'stats':
                self._cmd_stats()
            elif command == 'capital':
                self._cmd_capital()
            elif command == 'report':
                self._cmd_report()
            elif command == 'weekly':
                self._cmd_weekly()
            elif command == 'kill':
                self._cmd_kill()
            elif command == 'resume':
                self._cmd_resume()
            else:
                telegram.send_alert(f"Unknown command: {command}")
        except Exception as e:
            logger.error(f"Error executing command '{command}': {e}")
            telegram.send_alert(f"Error executing /{command}: {str(e)}")

    def _cmd_positions(self) -> None:
        """List all open positions with current P&L"""
        session = get_session()
        try:
            positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()

            if not positions:
                telegram.send_alert("No open positions")
                return

            # Get Kite client for LTP
            try:
                from src.api.dual_kite_client import get_kite_client
                kite = get_kite_client()
            except Exception as e:
                logger.warning(f"Could not get Kite client: {e}")
                kite = None

            lines = ["<b>Open Positions</b>\n"]

            total_deployed = 0
            total_unrealized = 0

            for pos in positions:
                ltp = None
                unrealized_pnl = 0
                pnl_pct = 0

                # Try to get current LTP
                if kite is not None:
                    try:
                        instrument_token = kite.get_instrument_token(pos.script)
                        if instrument_token:
                            ltp = kite.get_instrument_ltp(instrument_token)
                            if ltp is not None:
                                unrealized_pnl = (ltp - pos.entry_price) * pos.quantity
                                pnl_pct = ((ltp - pos.entry_price) / pos.entry_price) * 100
                                total_unrealized += unrealized_pnl
                    except Exception as e:
                        logger.debug(f"Could not get LTP for {pos.script}: {e}")

                # Build position line
                pnl_str = ""
                if ltp is not None:
                    pnl_sign = "+" if unrealized_pnl >= 0 else ""
                    pnl_str = f"LTP: {ltp:,.2f} | P&L: {pnl_sign}{unrealized_pnl:,.0f} ({pnl_sign}{pnl_pct:.1f}%)\n"

                lines.append(
                    f"<b>{pos.script}</b>\n"
                    f"Entry: {pos.entry_price:,.2f} ({pos.entry_date})\n"
                    f"Qty: {pos.quantity} | SL: {pos.current_sl:,.2f}\n"
                    f"{pnl_str}"
                    f"SL Moves: {pos.sl_movements} | Days: {pos.days_held}\n"
                )
                total_deployed += pos.capital_deployed

            lines.append(f"\n<b>Total Deployed:</b> {total_deployed:,.0f}")
            if total_unrealized != 0:
                pnl_sign = "+" if total_unrealized >= 0 else ""
                lines.append(f"<b>Unrealized P&L:</b> {pnl_sign}{total_unrealized:,.0f}")

            telegram.send_alert('\n'.join(lines))

        finally:
            session.close()

    def _cmd_pending(self) -> None:
        """List pending approvals and hold signals"""
        session = get_session()
        try:
            pending_signals = session.query(SignalQueue).filter(
                SignalQueue.status.in_([
                    SignalStatus.PENDING,
                    SignalStatus.NOTIFIED,
                    SignalStatus.HOLD,
                    SignalStatus.AWAITING_PRICE
                ])
            ).all()

            pending_orders = session.query(OpenOrder).filter(
                OpenOrder.status == OrderStatus.PENDING
            ).all()

            lines = ["<b>Pending Items</b>\n"]

            if pending_signals:
                lines.append("<b>Signals:</b>")
                for sig in pending_signals:
                    status_emoji = {
                        SignalStatus.PENDING: "",
                        SignalStatus.NOTIFIED: "",
                        SignalStatus.HOLD: "",
                        SignalStatus.AWAITING_PRICE: ""
                    }.get(sig.status, "")

                    lines.append(
                        f"{status_emoji} {sig.script} @ {sig.signal_level:,.2f}\n"
                        f"   Status: {sig.status.value}"
                    )
                lines.append("")

            if pending_orders:
                lines.append("<b>Entry Orders:</b>")
                for order in pending_orders:
                    lines.append(
                        f" {order.script} @ {order.limit_price:,.2f}\n"
                        f"   Qty: {order.quantity}"
                    )
                lines.append("")

            if not pending_signals and not pending_orders:
                lines.append("No pending items")

            telegram.send_alert('\n'.join(lines))

        finally:
            session.close()

    def _cmd_stats(self) -> None:
        """Show trading statistics"""
        session = get_session()
        try:
            # Get closed positions for stats
            closed = session.query(ClosedPosition).all()

            if not closed:
                telegram.send_alert("No closed trades yet")
                return

            total_trades = len(closed)
            winners = sum(1 for c in closed if c.net_pnl > 0)
            losers = sum(1 for c in closed if c.net_pnl <= 0)
            win_rate = (winners / total_trades * 100) if total_trades > 0 else 0

            total_pnl = sum(c.net_pnl for c in closed)
            avg_pnl = total_pnl / total_trades if total_trades > 0 else 0

            avg_winner = sum(c.net_pnl for c in closed if c.net_pnl > 0) / winners if winners > 0 else 0
            avg_loser = sum(c.net_pnl for c in closed if c.net_pnl <= 0) / losers if losers > 0 else 0

            avg_days = sum(c.days_held for c in closed) / total_trades if total_trades > 0 else 0

            text = (
                "<b>Trading Statistics</b>\n\n"
                f"Total Trades: {total_trades}\n"
                f"Win Rate: {win_rate:.1f}%\n"
                f"Winners: {winners} | Losers: {losers}\n\n"
                f"<b>P&L</b>\n"
                f"Total: {total_pnl:+,.0f}\n"
                f"Average: {avg_pnl:+,.0f}\n"
                f"Avg Winner: {avg_winner:+,.0f}\n"
                f"Avg Loser: {avg_loser:+,.0f}\n\n"
                f"<b>Duration</b>\n"
                f"Avg Days Held: {avg_days:.1f}"
            )

            telegram.send_alert(text)

        finally:
            session.close()

    def _cmd_capital(self) -> None:
        """Show capital allocation"""
        session = get_session()
        try:
            # Get today's capital ledger
            today = today_ist()
            ledger = session.query(CapitalLedger).filter(
                CapitalLedger.date == today
            ).first()

            # Count positions and orders
            open_positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).count()

            pending_orders = session.query(OpenOrder).filter(
                OpenOrder.status == OrderStatus.PENDING
            ).count()

            # Get config values
            initial_capital = config.get('trading.initial_capital', 100000)
            per_trade = config.get('trading.per_trade_amount', 20000)
            max_positions = config.get('trading.max_positions', 5)

            if ledger:
                deployed = ledger.deployed_capital
                free = ledger.free_capital
                realized = ledger.realized_pnl
            else:
                # Calculate from positions
                positions = session.query(OpenPosition).filter(
                    OpenPosition.status == PositionStatus.OPEN
                ).all()
                deployed = sum(p.capital_deployed for p in positions)
                free = initial_capital - deployed
                realized = 0

            text = (
                "<b>Capital Allocation</b>\n\n"
                f"Initial Capital: {initial_capital:,.0f}\n"
                f"Per Trade: {per_trade:,.0f}\n"
                f"Max Positions: {max_positions}\n\n"
                f"<b>Current Status</b>\n"
                f"Deployed: {deployed:,.0f}\n"
                f"Free: {free:,.0f}\n"
                f"Realized P&L: {realized:+,.0f}\n\n"
                f"<b>Counts</b>\n"
                f"Open Positions: {open_positions}/{max_positions}\n"
                f"Pending Orders: {pending_orders}"
            )

            telegram.send_alert(text)

        finally:
            session.close()

    def _cmd_report(self, html: bool = True) -> None:
        """
        Generate today's summary report.

        Args:
            html: If True, generate and send HTML report. If False, send text summary.
        """
        if html:
            self._cmd_report_html()
        else:
            self._cmd_report_text()

    def _cmd_report_html(self) -> None:
        """Generate and send HTML daily report"""
        try:
            from src.telegram.report_generator import report_generator

            # Generate HTML report
            filepath = report_generator.generate_daily_report()

            if filepath:
                # Send via Telegram (deletes after send)
                success = telegram.send_html_report(filepath, report_type="Daily")
                if success:
                    logger.info("Daily HTML report sent successfully")
                else:
                    logger.warning("Failed to send HTML report, falling back to text")
                    self._cmd_report_text()
            else:
                logger.warning("Failed to generate HTML report, falling back to text")
                self._cmd_report_text()

        except Exception as e:
            logger.error(f"Error generating HTML report: {e}")
            self._cmd_report_text()

    def _cmd_report_text(self) -> None:
        """Generate text-based summary report (fallback)"""
        session = get_session()
        try:
            today = today_ist()

            # Count metrics
            open_positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).count()

            pending_orders = session.query(OpenOrder).filter(
                OpenOrder.status == OrderStatus.PENDING
            ).count()

            # Signals processed today
            signals_today = session.query(SignalQueue).filter(
                SignalQueue.signal_date == today
            ).count()

            # Trades closed today
            trades_today = session.query(ClosedPosition).filter(
                ClosedPosition.exit_date == today
            ).count()

            # Capital
            ledger = session.query(CapitalLedger).filter(
                CapitalLedger.date == today
            ).first()

            if ledger:
                deployed = ledger.deployed_capital
                free = ledger.free_capital
                realized = ledger.realized_pnl
            else:
                positions = session.query(OpenPosition).filter(
                    OpenPosition.status == PositionStatus.OPEN
                ).all()
                deployed = sum(p.capital_deployed for p in positions)
                free = config.get('trading.initial_capital', 100000) - deployed
                realized = 0

            # Kill switch status
            kill_status = "ACTIVE" if is_kill_switch_active() else "Inactive"

            telegram.send_daily_summary(
                date_str=today.strftime('%Y-%m-%d'),
                open_positions=open_positions,
                pending_orders=pending_orders,
                signals_processed=signals_today,
                trades_today=trades_today,
                realized_pnl=realized,
                deployed_capital=deployed,
                free_capital=free
            )

            # Add kill switch status
            if is_kill_switch_active():
                telegram.send_alert(f"Kill Switch: {kill_status}")

        finally:
            session.close()

    def _cmd_weekly(self) -> None:
        """Generate and send HTML weekly report"""
        try:
            from src.telegram.report_generator import report_generator

            # Generate HTML report
            filepath = report_generator.generate_weekly_report()

            if filepath:
                # Send via Telegram (deletes after send)
                success = telegram.send_html_report(filepath, report_type="Weekly")
                if success:
                    logger.info("Weekly HTML report sent successfully")
                else:
                    telegram.send_alert("Failed to send weekly HTML report")
            else:
                telegram.send_alert("Failed to generate weekly report")

        except Exception as e:
            logger.error(f"Error generating weekly report: {e}")
            telegram.send_alert(f"Error generating weekly report: {str(e)}")

    def _cmd_kill(self) -> None:
        """Activate kill switch"""
        if is_kill_switch_active():
            telegram.send_alert("Kill switch is already ACTIVE")
            return

        set_kill_switch(True)
        telegram.send_alert(
            "<b>KILL SWITCH ACTIVATED</b>\n\n"
            "All operations HALTED.\n"
            "Use /resume to deactivate."
        )
        logger.warning("Kill switch activated via Telegram command")

    def _cmd_resume(self) -> None:
        """Deactivate kill switch"""
        if not is_kill_switch_active():
            telegram.send_alert("Kill switch is not active")
            return

        set_kill_switch(False)
        telegram.send_alert(
            "<b>KILL SWITCH DEACTIVATED</b>\n\n"
            "Normal operations resumed."
        )
        logger.info("Kill switch deactivated via Telegram command")


# Singleton instance
command_handler = CommandHandler()
