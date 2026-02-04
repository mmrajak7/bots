"""
Telegram Commands Handler - Handles slash commands for FIFTY bot

Commands:
- /positions - List all open positions with P&L
- /pending - List pending approvals and hold signals
- /stats - Win rate, avg P&L, total trades
- /capital - Show capital allocation
- /report - Generate reports (Daily/Weekly/Monthly/Overall)
- /sync - Compare Zerodha vs DB positions
- /import SCRIPT - Import position from Zerodha
- /fix SCRIPT - Fix unprotected position (place SL GTT)
- /exit SCRIPT - Exit position at market price
- /release SCRIPT - Release signal from HOLD (or /release ALL)
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
                report_type = kwargs.get('report_type')
                self._cmd_report(report_type)
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
            elif command == 'release':
                script = kwargs.get('script')
                if script:
                    self._cmd_release(script)
                else:
                    telegram.send_alert("Usage: /release SCRIPT or /release ALL")
            elif command == 'fix':
                script = kwargs.get('script')
                if script:
                    self._cmd_fix(script)
                else:
                    telegram.send_alert("Usage: /fix SCRIPT")
            elif command == 'exit':
                script = kwargs.get('script')
                if script:
                    self._cmd_exit(script)
                else:
                    telegram.send_alert("Usage: /exit SCRIPT")
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
                telegram.send_alert("📊 No open positions")
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
                "<code>Script       Qty  Entry    LTP     SL   P&L  Age</code>",
                "<code>────────────────────────────────────────────────</code>"
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
                    ltp_str = f"{ltp:6.0f}"
                else:
                    pnl_str = "  N/A"
                    ltp_str = "   N/A"

                # Build row
                lines.append(
                    f"<code>{script_name} {pos.quantity:3d} {pos.entry_price:5.0f} {ltp_str} {pos.current_sl:6.0f} {pnl_str} {age_str}</code>"
                )
                total_deployed += pos.capital_deployed

            # Summary
            lines.append("<code>────────────────────────────────────────────────</code>")
            lines.append(f"<b>Deployed:</b> {total_deployed:,.0f}")

            pnl_sign = "+" if total_unrealized >= 0 else ""
            pnl_emoji = "" if total_unrealized >= 0 else ""
            lines.append(f"<b>Unrealized:</b> {pnl_sign}{total_unrealized:,.0f} {pnl_emoji}")

            telegram.send_alert('\n'.join(lines))

        finally:
            session.close()

    def _cmd_pending(self) -> None:
        """List pending signals, watching signals, and GTT entry orders"""
        session = get_session()
        try:
            # Get scripts that already have positions (to filter out stale orders)
            position_scripts = {p.script for p in session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()}

            # Signals watching for price (LTP too far from entry)
            watching_signals = session.query(SignalQueue).filter(
                SignalQueue.status == SignalStatus.WATCHING
            ).all()

            # Awaiting approval (pending/notified/hold)
            awaiting_signals = session.query(SignalQueue).filter(
                SignalQueue.status.in_([
                    SignalStatus.PENDING,  # Ready for notification
                    SignalStatus.NOTIFIED,
                    SignalStatus.HOLD,
                    SignalStatus.AWAITING_PRICE
                ])
            ).all()

            # Pending GTT entry orders (exclude scripts with positions)
            pending_orders = session.query(OpenOrder).filter(
                OpenOrder.status == OrderStatus.PENDING
            ).all()
            pending_orders = [o for o in pending_orders if o.script not in position_scripts]

            lines = ["📋 <b>Pending</b>"]

            # Get Kite client for LTP info
            kite = None
            try:
                from src.api.dual_kite_client import get_kite_client
                kite = get_kite_client()
            except Exception as e:
                logger.debug(f"Could not get Kite client for LTP: {e}")

            # Signals watching for price
            if watching_signals:
                lines.append("")
                lines.append(f"👁 <b>Watching Price ({len(watching_signals)})</b>")
                for sig in watching_signals:
                    ltp_info = ""
                    if kite is not None:
                        try:
                            token = kite.get_instrument_token(sig.script)
                            if token:
                                ltp = kite.get_instrument_ltp(token)
                                if ltp is not None and sig.signal_level > 0:
                                    dist_pct = ((ltp / sig.signal_level) - 1) * 100
                                    ltp_info = f" | LTP: {ltp:,.0f} ({dist_pct:+.1f}%)"
                        except Exception:
                            pass
                    lines.append(f"  • {sig.script} @ {sig.signal_level:,.0f}{ltp_info}")

            # Signals awaiting response
            if awaiting_signals:
                lines.append("")
                lines.append(f"🔔 <b>Awaiting Response ({len(awaiting_signals)})</b>")
                for sig in awaiting_signals:
                    status_tag = {
                        SignalStatus.PENDING: "QUEUE",
                        SignalStatus.NOTIFIED: "NEW",
                        SignalStatus.HOLD: "HOLD",
                        SignalStatus.AWAITING_PRICE: "PRICE?"
                    }.get(sig.status, "")
                    lines.append(f"  • {sig.script} @ {sig.signal_level:,.0f} [{status_tag}]")

            # GTT orders waiting to fill
            if pending_orders:
                lines.append("")
                lines.append(f"⏳ <b>GTT Orders ({len(pending_orders)})</b>")

                for order in pending_orders:
                    ltp_info = ""
                    if kite is not None:
                        try:
                            token = kite.get_instrument_token(order.script)
                            if token:
                                ltp = kite.get_instrument_ltp(token)
                                if ltp is not None and order.limit_price > 0:
                                    dist_pct = ((ltp - order.limit_price) / order.limit_price) * 100
                                    ltp_info = f" | LTP: {ltp:,.0f} ({dist_pct:+.1f}%)"
                        except Exception:
                            pass
                    lines.append(f"  • {order.script} @ {order.limit_price:,.0f} x{order.quantity}{ltp_info}")

            if not watching_signals and not awaiting_signals and not pending_orders:
                lines.append("\n✅ No pending items")

            telegram.send_alert('\n'.join(lines))

        finally:
            session.close()

    def _cmd_stats(self) -> None:
        """Show trading statistics"""
        session = get_session()
        try:
            closed = session.query(ClosedPosition).all()

            if not closed:
                telegram.send_alert("📊 No closed trades yet")
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

            pnl_emoji = "📈" if total_pnl >= 0 else "📉"

            text = (
                f"📊 <b>Stats</b>\n\n"
                f"🎯 {total_trades} trades | {win_rate:.0f}% win rate\n"
                f"✅ {winners}W / ❌ {losers}L\n\n"
                f"{pnl_emoji} <b>P&L:</b> {total_pnl:+,.0f}\n"
                f"   Avg: {avg_pnl:+,.0f} | Win: {avg_winner:+,.0f} | Loss: {avg_loser:+,.0f}\n\n"
                f"⏱️ Avg hold: {avg_days:.0f} days"
            )

            telegram.send_alert(text)

        finally:
            session.close()

    def _cmd_capital(self) -> None:
        """Show capital allocation"""
        session = get_session()
        try:
            # Get config values
            initial_capital = config.get('trading.initial_capital', 100000)
            per_trade = config.get('trading.per_trade_amount', 20000)
            max_positions = config.get('trading.max_positions', 5)

            # Get positions
            positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()
            position_scripts = {p.script for p in positions}
            num_positions = len(positions)
            deployed = sum(p.capital_deployed for p in positions)
            free = initial_capital - deployed

            # Get pending GTT orders (exclude scripts with positions)
            all_orders = session.query(OpenOrder).filter(
                OpenOrder.status == OrderStatus.PENDING
            ).all()
            pending_orders = len([o for o in all_orders if o.script not in position_scripts])

            # Get realized P&L
            all_trades = session.query(ClosedPosition).all()
            realized = sum(t.net_pnl for t in all_trades)

            pnl_emoji = "📈" if realized >= 0 else "📉"
            utilization = (deployed / initial_capital * 100) if initial_capital > 0 else 0

            text = (
                f"💰 <b>Capital</b>\n\n"
                f"💵 {initial_capital:,.0f} @ {per_trade:,.0f}/trade\n"
                f"🔒 {deployed:,.0f} deployed ({utilization:.0f}%)\n"
                f"✅ {free:,.0f} available\n\n"
                f"📊 {num_positions}/{max_positions} positions | {pending_orders} GTT pending\n"
                f"{pnl_emoji} Realized: {realized:+,.0f}"
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
        """Generate and send weekly report as PNG image"""
        self._generate_report('weekly')

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
                # Check if position has SL GTT - if not, place it
                if not existing.gtt_id or not existing.gtt_verified:
                    telegram.send_alert(f"⚠️ {script} unprotected, placing SL...")
                    try:
                        from src.core.exit_manager import exit_manager
                        gtt_id = exit_manager.place_sl_gtt(existing, session=session)
                        if gtt_id:
                            existing.gtt_verified = True
                            session.commit()
                            telegram.send_alert(f"🛡️ {script} protected | GTT: {gtt_id}")
                        else:
                            telegram.send_alert(f"🔴 {script} SL FAILED - UNPROTECTED!", critical=True)
                    except Exception as e:
                        telegram.send_alert(f"🔴 {script} SL error: {str(e)}", critical=True)
                    return
                else:
                    telegram.send_alert(
                        f"✅ {script} already tracked\n"
                        f"   📊 {existing.quantity} x {existing.entry_price:,.0f} | SL: {existing.current_sl:,.0f}"
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
                telegram.send_alert(f"❌ {script} not in Zerodha (or qty=0)")
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
            telegram.send_alert(f"⏳ Importing {script}...")

            try:
                from src.core.exit_manager import exit_manager
                gtt_id = exit_manager.place_sl_gtt(position, session=session)

                if gtt_id:
                    position.gtt_verified = True
                    session.commit()
                    telegram.send_alert(
                        f"✅ <b>{script} Imported</b>\n"
                        f"   📊 {quantity} x {avg_price:,.0f} | 🛡️ SL: {initial_sl:,.0f} | GTT: {gtt_id}"
                    )
                else:
                    session.commit()
                    telegram.send_alert(f"🔴 {script} imported but SL FAILED - UNPROTECTED!", critical=True)

            except Exception as e:
                session.commit()
                logger.error(f"Failed to place SL GTT for imported {script}: {e}")
                telegram.send_alert(f"🔴 {script} imported but SL error: {str(e)}", critical=True)

        except Exception as e:
            logger.error(f"Import error for {script}: {e}")
            telegram.send_alert(f"Import failed: {str(e)}")
            session.rollback()
        finally:
            session.close()

    def _cmd_release(self, script: str) -> None:
        """Release signal(s) from HOLD back to PENDING for re-notification"""
        session = get_session()
        try:
            script = script.upper().strip()

            if script == 'ALL':
                hold_signals = session.query(SignalQueue).filter(
                    SignalQueue.status == SignalStatus.HOLD
                ).all()
            else:
                hold_signals = session.query(SignalQueue).filter(
                    SignalQueue.script == script,
                    SignalQueue.status == SignalStatus.HOLD
                ).all()

            if not hold_signals:
                if script == 'ALL':
                    telegram.send_alert("No signals on HOLD")
                else:
                    telegram.send_alert(f"No HOLD signal for {script}")
                return

            released = []
            for signal in hold_signals:
                signal.status = SignalStatus.PENDING
                signal.telegram_msg_id = None
                released.append(signal.script)

            session.commit()

            if len(released) == 1:
                telegram.send_alert(f"{released[0]} released from HOLD\nWill re-notify next cycle.")
            else:
                scripts_str = ', '.join(released)
                telegram.send_alert(f"Released {len(released)} signals: {scripts_str}\nWill re-notify next cycle.")

            logger.info(f"Released {len(released)} HOLD signals via /release: {released}")

        except Exception as e:
            logger.error(f"Release error: {e}")
            telegram.send_alert(f"Release failed: {str(e)}")
            session.rollback()
        finally:
            session.close()

    def _cmd_fix(self, script: str) -> None:
        """Fix unprotected position by placing SL GTT"""
        session = get_session()
        try:
            script = script.upper().strip()

            position = session.query(OpenPosition).filter(
                OpenPosition.script == script,
                OpenPosition.status == PositionStatus.OPEN
            ).first()

            if not position:
                telegram.send_alert(f"❌ No position for {script}")
                return

            if position.gtt_id and position.gtt_verified:
                telegram.send_alert(f"🛡️ {script} already protected | GTT: {position.gtt_id}")
                return

            telegram.send_alert(f"⏳ Placing SL for {script}...")

            try:
                from src.core.exit_manager import exit_manager
                gtt_id = exit_manager.place_sl_gtt(position, session=session)

                if gtt_id:
                    position.gtt_verified = True
                    session.commit()
                    telegram.send_alert(f"🛡️ {script} protected | SL: {position.current_sl:,.0f} | GTT: {gtt_id}")
                else:
                    telegram.send_alert(f"🔴 {script} SL FAILED - UNPROTECTED!", critical=True)

            except Exception as e:
                logger.error(f"Failed to place SL GTT for {script}: {e}")
                telegram.send_alert(f"🔴 {script} SL error: {str(e)}", critical=True)

        except Exception as e:
            logger.error(f"Fix error for {script}: {e}")
            telegram.send_alert(f"🔴 Fix failed: {str(e)}")
        finally:
            session.close()

    def _cmd_exit(self, script: str) -> None:
        """Exit position at market price"""
        try:
            script = script.upper().strip()

            # First check if position exists
            session = get_session()
            try:
                position = session.query(OpenPosition).filter(
                    OpenPosition.script == script,
                    OpenPosition.status == PositionStatus.OPEN
                ).first()

                if not position:
                    telegram.send_alert(f"❌ No open position for {script}")
                    return

                position_id = position.id
                quantity = position.quantity
                entry_price = position.entry_price
            finally:
                session.close()

            telegram.send_alert(f"⏳ Exiting {script} at MARKET...")

            # Use exit_manager to execute market exit (notify=False, we'll send our own)
            from src.core.exit_manager import exit_manager
            success = exit_manager.emergency_exit(position_id, notify=False)

            if success:
                # Fetch closed position from fresh session for P&L details
                session = get_session()
                try:
                    closed = session.query(ClosedPosition).filter(
                        ClosedPosition.script == script
                    ).order_by(ClosedPosition.closed_at.desc()).first()

                    if closed:
                        pnl_class = '🟢' if closed.net_pnl >= 0 else '🔴'
                        pnl_sign = '+' if closed.net_pnl >= 0 else ''
                        telegram.send_alert(
                            f"✅ <b>{script} EXITED</b>\n\n"
                            f"📊 {quantity} @ {entry_price:,.2f} → {closed.exit_price:,.2f}\n"
                            f"{pnl_class} P&L: {pnl_sign}{closed.net_pnl:,.0f} ({pnl_sign}{closed.pnl_percent:.1f}%)"
                        )
                    else:
                        telegram.send_alert(f"✅ {script} exit order filled")
                finally:
                    session.close()
            # Note: emergency_exit already sends alerts for failures

        except Exception as e:
            logger.error(f"Exit command error for {script}: {e}")
            telegram.send_alert(f"🔴 Exit error: {str(e)}")

    def _cmd_sync(self) -> None:
        """
        Sync positions from Zerodha.
        Checks BOTH holdings() and positions() APIs to get complete picture:
        - holdings(): T+1 settled and T+2+ shares
        - positions(): T+0 day trades (bought today)
        Shows comparison and detects manually closed positions.
        """
        session = get_session()
        try:
            # Get DB positions
            db_positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()
            db_scripts = {p.script for p in db_positions}

            # Get Zerodha holdings + positions
            try:
                from src.api.dual_kite_client import get_kite_client
                kite = get_kite_client()

                # Holdings (T+1 onwards)
                holdings = kite.get_holdings()
                holdings_map = {}
                for h in holdings:
                    symbol = h.get('tradingsymbol', '')
                    qty = h.get('quantity', 0)
                    t1_qty = h.get('t1_quantity', 0)
                    total_qty = qty + t1_qty
                    if total_qty > 0:
                        holdings_map[symbol] = {
                            'quantity': total_qty,
                            'avg_price': h.get('average_price', 0)
                        }

                # Day positions (T+0, bought today)
                zerodha_positions = kite.get_positions()
                day_pos_map = {}
                for p in zerodha_positions.get('net', []):
                    symbol = p.get('tradingsymbol', '')
                    qty = p.get('quantity', 0)
                    if qty > 0 and p.get('exchange') == 'NSE':
                        day_pos_map[symbol] = {
                            'quantity': qty,
                            'avg_price': p.get('average_price', 0)
                        }

            except Exception as e:
                logger.error(f"Could not fetch Zerodha data: {e}")
                telegram.send_alert(f"Error fetching Zerodha data: {str(e)}")
                return

            # Merge holdings + day positions (holdings take precedence)
            zerodha_all = {}
            for symbol, data in day_pos_map.items():
                zerodha_all[symbol] = data
            for symbol, data in holdings_map.items():
                zerodha_all[symbol] = data  # Overwrite day pos if in holdings too

            zerodha_scripts = set(zerodha_all.keys())

            lines = ["<b>Sync</b>"]

            # Categorize
            missing_from_db = []
            missing_from_zerodha = []

            for symbol, data in zerodha_all.items():
                if symbol not in db_scripts:
                    source = "holdings" if symbol in holdings_map else "day pos"
                    missing_from_db.append({
                        'script': symbol,
                        'quantity': data['quantity'],
                        'avg_price': data['avg_price'],
                        'source': source
                    })

            for p in db_positions:
                if p.script not in zerodha_scripts:
                    missing_from_zerodha.append(p)

            # Summary
            lines.append(f"\nDB: {len(db_positions)} | Zerodha: {len(zerodha_all)}")
            lines.append(f"(Holdings: {len(holdings_map)} | Day Positions: {len(day_pos_map)})")

            # Show what's tracked and verified
            if db_positions:
                lines.append("\n<b>Tracked:</b>")
                for p in db_positions:
                    in_zerodha = p.script in zerodha_scripts
                    gtt_icon = "Y" if p.gtt_verified else "N"
                    status_icon = "OK" if in_zerodha else "MISSING"
                    lines.append(f"  [{status_icon}] {p.script} x{p.quantity} GTT:{gtt_icon}")

            # Show positions in DB but NOT in Zerodha (manually closed)
            if missing_from_zerodha:
                lines.append("\n<b>In DB but NOT in Zerodha (manually closed?):</b>")
                for p in missing_from_zerodha:
                    lines.append(f"  {p.script} x{p.quantity} @ {p.entry_price:,.0f}")
                lines.append("\n<i>These will be auto-closed at next reconciliation</i>")

            # Show positions in Zerodha but NOT in DB
            if missing_from_db:
                lines.append("\n<b>Not Tracked (in Zerodha):</b>")
                for m in missing_from_db:
                    lines.append(
                        f"  {m['script']} x{m['quantity']} @ {m['avg_price']:,.0f} ({m['source']})"
                    )
                lines.append("\n<i>/import SCRIPT to add</i>")

            if not missing_from_db and not missing_from_zerodha:
                lines.append("\nAll synced")

            telegram.send_alert('\n'.join(lines))

        except Exception as e:
            logger.error(f"Sync error: {e}")
            telegram.send_alert(f"Sync error: {str(e)}")
        finally:
            session.close()


# Singleton instance
command_handler = CommandHandler()
