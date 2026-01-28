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

    def execute_command(self, command: str, **kwargs) -> None:
        """Execute a command with optional parameters"""
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
            elif command == 'sync':
                self._cmd_sync()
            elif command == 'import':
                script = kwargs.get('script')
                if script:
                    self._cmd_import(script)
                else:
                    telegram.send_alert("Usage: /import SCRIPT")
            elif command == 'report':
                report_type = kwargs.get('report_type')
                self._cmd_report(report_type)
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
        """List all open positions with current P&L in a clean table format"""
        session = get_session()
        try:
            positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()

            if not positions:
                telegram.send_alert("No open positions")
                return

            # Get Kite client for LTP
            kite = None
            try:
                from src.api.dual_kite_client import get_kite_client
                kite = get_kite_client()
            except Exception as e:
                logger.warning(f"Could not get Kite client: {e}")

            # Build table header
            lines = [
                "<b>Open Positions</b>",
                "<code>Script       Qty   Entry     LTP      SL    P&L   Age</code>",
                "<code>─────────────────────────────────────────────────────</code>"
            ]

            total_deployed = 0
            total_unrealized = 0

            for pos in positions:
                ltp = None
                unrealized_pnl = 0

                # Try to get current LTP
                if kite is not None:
                    try:
                        instrument_token = kite.get_instrument_token(pos.script)
                        if instrument_token:
                            ltp = kite.get_instrument_ltp(instrument_token)
                            if ltp is not None:
                                unrealized_pnl = (ltp - pos.entry_price) * pos.quantity
                                total_unrealized += unrealized_pnl
                    except Exception as e:
                        logger.debug(f"Could not get LTP for {pos.script}: {e}")

                # Format script name (truncate/pad to 12 chars)
                script_name = pos.script[:12].ljust(12)

                # Format age
                if pos.days_held == 0:
                    age_str = "Today"
                elif pos.days_held == 1:
                    age_str = "  1d"
                else:
                    age_str = f"{pos.days_held:3d}d"

                # Format P&L
                if ltp is not None:
                    pnl_str = f"{unrealized_pnl:+5.0f}"
                    ltp_str = f"{ltp:7.2f}"
                else:
                    pnl_str = "  N/A"
                    ltp_str = "    N/A"

                # Build row
                lines.append(
                    f"<code>{script_name} {pos.quantity:3d} {pos.entry_price:7.2f} {ltp_str} {pos.current_sl:7.2f} {pnl_str} {age_str}</code>"
                )
                total_deployed += pos.capital_deployed

            # Summary
            lines.append("<code>─────────────────────────────────────────────────────</code>")
            lines.append(f"<b>Deployed:</b> {total_deployed:,.0f}")

            pnl_sign = "+" if total_unrealized >= 0 else ""
            pnl_emoji = "" if total_unrealized >= 0 else ""
            lines.append(f"<b>Unrealized:</b> {pnl_sign}{total_unrealized:,.0f} {pnl_emoji}")

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

    def _cmd_report(self, report_type: str = None) -> None:
        """
        Generate report - either show menu or generate specific type.

        Args:
            report_type: 'daily', 'weekly', 'monthly', 'overall', or None for menu
        """
        if report_type is None:
            # Show interactive menu with buttons
            self._show_report_menu()
        else:
            self._generate_report(report_type)

    def _show_report_menu(self) -> None:
        """Show interactive report type selection"""
        buttons = [
            [
                {'text': 'Daily', 'callback_data': 'report_daily'},
                {'text': 'Weekly', 'callback_data': 'report_weekly'}
            ],
            [
                {'text': 'Monthly', 'callback_data': 'report_monthly'},
                {'text': 'Overall', 'callback_data': 'report_overall'}
            ]
        ]
        reply_markup = telegram.create_inline_keyboard(buttons)

        telegram.send_message(
            "<b>Select Report Type</b>\n\n"
            "Daily - Today's summary\n"
            "Weekly - Last 7 days\n"
            "Monthly - This month\n"
            "Overall - All-time stats",
            reply_markup=reply_markup
        )

    def _generate_report(self, report_type: str) -> None:
        """Generate and send the specified report type"""
        try:
            from src.telegram.report_generator import report_generator

            # Generate report based on type
            if report_type == 'daily':
                filepath = report_generator.generate_daily_report()
                type_name = "Daily"
            elif report_type == 'weekly':
                filepath = report_generator.generate_weekly_report()
                type_name = "Weekly"
            elif report_type == 'monthly':
                filepath = report_generator.generate_monthly_report()
                type_name = "Monthly"
            elif report_type == 'overall':
                filepath = report_generator.generate_overall_report()
                type_name = "Overall"
            else:
                telegram.send_alert(f"Unknown report type: {report_type}")
                return

            if not filepath:
                telegram.send_alert(f"Failed to generate {type_name} report")
                return

            # Try to convert to image first
            image_path = report_generator.html_to_image(filepath)

            if image_path:
                # Send image
                success = telegram.send_photo(image_path, caption=f"<b>FIFTY {type_name} Report</b>")
                # Delete both image and HTML
                report_generator.delete_report(image_path)
                report_generator.delete_report(filepath)

                if success:
                    logger.info(f"{type_name} report image sent successfully")
                else:
                    # Fallback to HTML
                    telegram.send_alert(f"Failed to send image, report saved at {filepath}")
            else:
                # Send HTML document
                success = telegram.send_html_report(filepath, report_type=type_name)
                if success:
                    logger.info(f"{type_name} HTML report sent successfully")
                else:
                    telegram.send_alert(f"Failed to send {type_name} report")

        except Exception as e:
            logger.error(f"Error generating {report_type} report: {e}")
            telegram.send_alert(f"Report error: {str(e)}")

    def _cmd_report_html(self) -> None:
        """Generate and send HTML daily report (legacy method)"""
        self._generate_report('daily')

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

    def _cmd_import(self, script: str) -> None:
        """
        Import an existing Zerodha position into FIFTY's database.

        Args:
            script: Trading symbol (e.g., BALRAMCHIN)
        """
        from src.utils.timezone_helper import today_ist
        from src.models.database import ist_now_naive

        session = get_session()
        try:
            script = script.upper().strip()

            # Check if already in DB
            existing = session.query(OpenPosition).filter(
                OpenPosition.script == script,
                OpenPosition.status == PositionStatus.OPEN
            ).first()

            if existing:
                telegram.send_alert(
                    f"<b>{script}</b> already exists in DB\n"
                    f"Entry: {existing.entry_price:,.2f}\n"
                    f"Qty: {existing.quantity}\n"
                    f"SL: {existing.current_sl:,.2f}"
                )
                return

            # Fetch from Zerodha
            try:
                from src.api.dual_kite_client import get_kite_client
                kite = get_kite_client()
                zerodha_positions = kite.get_positions()
                net_positions = zerodha_positions.get('net', [])
            except Exception as e:
                logger.error(f"Could not fetch Zerodha positions: {e}")
                telegram.send_alert(f"Error fetching Zerodha: {str(e)}")
                return

            # Find the position
            zerodha_pos = None
            for p in net_positions:
                if p.get('tradingsymbol') == script and p.get('quantity', 0) > 0:
                    zerodha_pos = p
                    break

            if not zerodha_pos:
                telegram.send_alert(
                    f"<b>{script}</b> not found in Zerodha positions\n"
                    f"(or quantity is 0)"
                )
                return

            # Extract position details
            quantity = zerodha_pos.get('quantity', 0)
            avg_price = zerodha_pos.get('average_price', 0)
            exchange = zerodha_pos.get('exchange', 'NSE')

            if quantity <= 0 or avg_price <= 0:
                telegram.send_alert(f"Invalid position data for {script}")
                return

            # Calculate SL (20% below entry)
            sl_percent = config.get('trading.initial_sl_percent', 20)
            initial_sl = round(avg_price * (1 - sl_percent / 100), 2)
            capital_deployed = avg_price * quantity

            # Create position record
            position = OpenPosition(
                signal_id=0,  # No signal - manual import
                script=script,
                entry_date=today_ist(),
                entry_price=avg_price,
                quantity=quantity,
                capital_deployed=capital_deployed,
                initial_sl=initial_sl,
                current_sl=initial_sl,
                highest_sl=initial_sl,
                sl_movements=0,
                status=PositionStatus.OPEN,
                days_held=0,
                gtt_verified=False,
                created_at=ist_now_naive(),
                updated_at=ist_now_naive()
            )
            session.add(position)
            session.flush()  # Get position.id without committing yet

            position_id = position.id
            logger.info(f"Imported position {script}: {quantity} @ {avg_price}, SL: {initial_sl}")

            # Place SL GTT
            telegram.send_alert(
                f"Importing {script}...\n"
                f"Qty: {quantity} @ {avg_price:,.2f}\n"
                f"SL: {initial_sl:,.2f} (-{sl_percent}%)"
            )

            try:
                from src.core.exit_manager import exit_manager
                # FIX: Pass position object, not position_id
                gtt_id = exit_manager.place_sl_gtt(position, session=session)

                if gtt_id:
                    position.gtt_verified = True
                    session.commit()
                    telegram.send_alert(
                        f"<b>{script} Imported</b>\n\n"
                        f"Entry: {avg_price:,.2f} x {quantity}\n"
                        f"SL: {initial_sl:,.2f}\n"
                        f"GTT: {gtt_id}"
                    )
                else:
                    session.commit()  # Commit position even if GTT failed
                    telegram.send_alert(
                        f"<b>WARNING: {script} imported but SL GTT FAILED</b>\n\n"
                        f"Position is UNPROTECTED!\n"
                        f"Run recovery or place SL manually.",
                        critical=True
                    )

            except Exception as e:
                logger.error(f"Failed to place SL GTT for imported {script}: {e}")
                telegram.send_alert(
                    f"<b>WARNING: {script} imported but SL GTT FAILED</b>\n\n"
                    f"Error: {str(e)}\n"
                    f"Position is UNPROTECTED!",
                    critical=True
                )

        except Exception as e:
            logger.error(f"Import error for {script}: {e}")
            telegram.send_alert(f"Import failed: {str(e)}")
            session.rollback()
        finally:
            session.close()

    def _cmd_sync(self) -> None:
        """
        Sync positions from Zerodha.
        Shows Zerodha positions vs DB positions and allows import.
        """
        session = get_session()
        try:
            # Get DB positions
            db_positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()
            db_scripts = {p.script for p in db_positions}

            # Get Zerodha positions
            try:
                from src.api.dual_kite_client import get_kite_client
                kite = get_kite_client()
                zerodha_positions = kite.get_positions()
                net_positions = zerodha_positions.get('net', [])
            except Exception as e:
                logger.error(f"Could not fetch Zerodha positions: {e}")
                telegram.send_alert(f"Error fetching Zerodha positions: {str(e)}")
                return

            # Filter for actual holdings (quantity > 0)
            zerodha_holdings = [
                p for p in net_positions
                if p.get('quantity', 0) > 0 and p.get('exchange') == 'NSE'
            ]

            lines = ["<b>Position Sync Status</b>\n"]

            # Show DB positions
            lines.append(f"<b>DB Positions:</b> {len(db_positions)}")
            for p in db_positions:
                lines.append(f"  - {p.script}: {p.quantity} @ {p.entry_price:,.2f}")

            lines.append("")

            # Show Zerodha positions
            lines.append(f"<b>Zerodha Positions:</b> {len(zerodha_holdings)}")
            missing_from_db = []
            for p in zerodha_holdings:
                symbol = p.get('tradingsymbol', 'UNKNOWN')
                qty = p.get('quantity', 0)
                avg_price = p.get('average_price', 0)
                in_db = "DB" if symbol in db_scripts else "NOT IN DB"

                if symbol not in db_scripts:
                    missing_from_db.append({
                        'script': symbol,
                        'quantity': qty,
                        'avg_price': avg_price
                    })

                lines.append(f"  - {symbol}: {qty} @ {avg_price:,.2f} [{in_db}]")

            # Show import candidates
            if missing_from_db:
                lines.append("")
                lines.append("<b>Can Import:</b>")
                for m in missing_from_db:
                    lines.append(
                        f"  {m['script']}: {m['quantity']} @ {m['avg_price']:,.2f}"
                    )
                lines.append("")
                lines.append("Use /import SCRIPT to add to tracking")
            else:
                lines.append("")
                lines.append("All Zerodha positions are tracked")

            telegram.send_alert('\n'.join(lines))

        except Exception as e:
            logger.error(f"Sync error: {e}")
            telegram.send_alert(f"Sync error: {str(e)}")
        finally:
            session.close()


# Singleton instance
command_handler = CommandHandler()
