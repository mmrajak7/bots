"""
Orchestrator - Time-based task scheduler for FIFTY bot

Runs via cron every 5 minutes. Handles:
- Morning startup (9:00-9:05)
- Signal processing during market hours
- Hold signal re-notification (9:30-9:35)
- EOD GTT update (last trading day, 15:50-15:55)
- Recovery checks (16:00-16:05)
- Daily/Weekly/Monthly reports
"""

from datetime import date
from typing import Optional
from loguru import logger

from src.utils.config_manager import config
from src.utils.timezone_helper import (
    now_ist, today_ist, in_time_window, is_market_hours,
    is_market_day_ist, is_friday, get_month_end
)
from src.models.database import (
    is_kill_switch_active, get_bot_state, set_bot_state,
    get_session, OpenPosition, OpenOrder, SignalQueue, ClosedPosition,
    PositionStatus, OrderStatus, SignalStatus, ist_now_naive
)
from src.telegram.bot import telegram
from src.telegram.approval_handler import approval_handler
from src.telegram.commands import command_handler


class Orchestrator:
    """Main orchestrator for FIFTY bot operations"""

    def __init__(self):
        """Initialize orchestrator"""
        self.signal_processor = None
        self.order_manager = None
        self.exit_manager = None

        logger.info("Orchestrator initialized")

    def _lazy_load_processors(self):
        """Lazy load processors to avoid circular imports"""
        if self.signal_processor is None:
            from src.core.signal_processor import signal_processor
            self.signal_processor = signal_processor

        if self.order_manager is None:
            from src.core.order_manager import order_manager
            self.order_manager = order_manager

        if self.exit_manager is None:
            from src.core.exit_manager import exit_manager
            self.exit_manager = exit_manager

    def _ensure_token_ready(self) -> None:
        """
        Early token generation (8:50-9:00)

        Ensures the Kite API trade token is ready before market hours.
        This runs before the main morning startup to give time for token generation.
        """
        # Check if already done today
        last_token_gen = get_bot_state('last_token_generation')
        today_str = today_ist().isoformat()

        if last_token_gen == today_str:
            return

        logger.info("Running early token generation check")

        try:
            from src.utils.token_generator import ensure_valid_token

            token_valid, token_msg = ensure_valid_token()

            if token_valid:
                logger.info(f"Token ready: {token_msg}")
                set_bot_state('last_token_generation', today_str)
            else:
                logger.error(f"Token generation failed: {token_msg}")
                telegram.send_alert(
                    f"<b>FIFTY Token Alert</b>\n\n"
                    f"Failed to generate token: {token_msg}\n"
                    f"Will retry at 9:00 AM startup",
                    critical=True
                )

        except Exception as e:
            logger.error(f"Token generation error: {e}")
            telegram.send_alert(
                f"<b>FIFTY Token Error</b>\n\n{str(e)}",
                critical=True
            )

    def run(self) -> None:
        """Main run method - called by cron every 5 minutes"""
        try:
            current = now_ist()
            logger.info(f"Orchestrator run at {current.strftime('%H:%M:%S')}")

            # 1. Check kill switch
            if is_kill_switch_active():
                logger.warning("Kill switch active - skipping all operations")
                return

            # Check if market day
            if not is_market_day_ist():
                logger.info("Not a market day - skipping")
                return

            # Lazy load processors
            self._lazy_load_processors()

            # 2. Early token generation (8:50-9:00)
            # Ensures token is ready before market open
            if in_time_window(current, "08:50", "09:00"):
                self._ensure_token_ready()

            # 3. Morning startup (9:00-9:05)
            if in_time_window(current, "09:00", "09:05"):
                self._morning_startup()

            # 4. Process Telegram callbacks (always during market hours)
            if in_time_window(current, "09:00", "16:30"):
                self._process_telegram_callbacks()

            # 5. Process signals during market hours (9:15-15:30)
            if is_market_hours(current):
                self._process_signals()
                self._send_pending_notifications()
                self._monitor_orders()
                self._monitor_positions_for_drops()

            # 6. Hold signal re-notification (9:30-9:35)
            if in_time_window(current, "09:30", "09:35"):
                self._resend_hold_signals()

            # 7. EOD GTT Update (last trading day, 15:50-15:55)
            if in_time_window(current, "15:50", "15:55") and self._is_last_trading_day():
                self._update_monthly_trailing_sl()

            # 8. Recovery checks (16:00-16:05)
            if in_time_window(current, "16:00", "16:05"):
                self._run_recovery_checks()

            # 9. Daily report - DISABLED (user preference: weekly/monthly only)
            # if in_time_window(current, "16:10", "16:15"):
            #     self._send_daily_report()

            # 10. Weekly report (Friday 16:15-16:20)
            if is_friday() and in_time_window(current, "16:15", "16:20"):
                self._send_weekly_report()

            # 11. Monthly report (last trading day, 16:20-16:25) - BEFORE cleanup
            if in_time_window(current, "16:20", "16:25") and self._is_last_trading_day():
                self._send_monthly_report()

            # 12. Month-end cleanup (last trading day, 16:25-16:30)
            if in_time_window(current, "16:25", "16:30") and self._is_last_trading_day():
                self._month_end_cleanup()

            logger.info("Orchestrator run completed")

        except Exception as e:
            logger.error(f"Orchestrator error: {e}")
            telegram.send_alert(f"Orchestrator error: {str(e)}", critical=True)
            # FIX SYS-H4: Re-raise exception so cron job reports failure correctly
            # This ensures monitoring tools detect the error
            raise

    # =========================================================================
    # MORNING STARTUP
    # =========================================================================

    def _morning_startup(self) -> None:
        """Morning startup tasks"""
        # Check if already run today
        last_startup = get_bot_state('last_morning_startup')
        today_str = today_ist().isoformat()

        if last_startup == today_str:
            logger.debug("Morning startup already completed today")
            return

        logger.info("Running morning startup")

        try:
            # Step 1: Ensure we have a valid trade token (generates if needed)
            from src.utils.token_generator import ensure_valid_token
            token_valid, token_msg = ensure_valid_token()

            if not token_valid:
                error_msg = f"<b>FIFTY Token Generation Failed</b>\n\n{token_msg}"
                logger.error(error_msg)
                telegram.send_alert(error_msg, critical=True)
                return

            logger.info(f"Token status: {token_msg}")

            # Step 2: Initialize Kite client and validate connections
            from src.api.dual_kite_client import get_kite_client
            kite = get_kite_client()

            # Validate connections
            if kite.validate_read_connection():
                logger.info("Read connection validated")
            else:
                telegram.send_alert("WARNING: Read connection failed", critical=True)

            if kite.validate_trade_connection():
                logger.info("Trade connection validated")
            else:
                telegram.send_alert("WARNING: Trade connection failed", critical=True)

            # Update capital ledger
            self._update_capital_ledger()

            # Run reconciliation
            self._reconcile_positions()

            # Send professional startup message
            self._send_startup_message(kite)

            set_bot_state('last_morning_startup', today_str)

        except Exception as e:
            logger.error(f"Morning startup failed: {e}")
            telegram.send_alert(f"Morning startup FAILED: {str(e)}", critical=True)

    def _send_startup_message(self, kite) -> None:
        """Send professional startup message like CROCODILE"""
        try:
            session = get_session()
            try:
                today = today_ist()
                day_name = today.strftime('%A')
                date_str = today.strftime('%d %b %Y')

                # Get capital info
                initial_capital = config.get('trading.initial_capital', 100000)

                # Get open positions
                open_positions = session.query(OpenPosition).filter(
                    OpenPosition.status == PositionStatus.OPEN
                ).all()
                num_positions = len(open_positions)
                deployed_capital = sum(p.capital_deployed for p in open_positions)
                available_capital = initial_capital - deployed_capital

                # Get pending orders
                pending_orders = session.query(OpenOrder).filter(
                    OpenOrder.status == OrderStatus.PENDING
                ).count()

                # Calculate unrealized P&L
                unrealized_pnl = 0
                try:
                    for pos in open_positions:
                        token = kite.get_instrument_token(pos.script)
                        if token:
                            ltp = kite.get_instrument_ltp(token)
                            if ltp:
                                unrealized_pnl += (ltp - pos.entry_price) * pos.quantity
                except Exception:
                    pass

                # Get realized P&L from closed trades
                all_trades = session.query(ClosedPosition).all()
                realized_pnl = sum(t.net_pnl for t in all_trades)

                # Calculate total return
                total_pnl = realized_pnl + unrealized_pnl
                total_return_pct = (total_pnl / initial_capital * 100) if initial_capital > 0 else 0

                # Build compact message
                pnl_sign = "+" if total_pnl >= 0 else ""

                message = (
                    f"<b>Good Morning - FIFTY Started</b>\n\n"
                    f"{date_str} ({day_name})\n"
                    f"Capital: Rs.{available_capital:,.0f} / {initial_capital:,.0f}\n"
                    f"Positions: {num_positions} | Pending: {pending_orders}\n"
                    f"P&L: {pnl_sign}{total_pnl:,.0f} ({pnl_sign}{total_return_pct:.2f}%)\n"
                    f"Ready"
                )

                telegram.send_alert(message)
                logger.info("Startup message sent")

            finally:
                session.close()

        except Exception as e:
            logger.warning(f"Could not send startup message: {e}")
            # Fallback to simple message
            telegram.send_alert("FIFTY Bot: Morning startup complete")

    # =========================================================================
    # TELEGRAM CALLBACK PROCESSING
    # =========================================================================

    def _process_telegram_callbacks(self) -> None:
        """Process Telegram callbacks and execute resulting actions"""
        try:
            actions = approval_handler.process_updates()

            for action in actions:
                action_type = action.get('type')

                if action_type == 'signal_approved':
                    self._handle_signal_approved(action)

                elif action_type == 'emergency_exit':
                    self._handle_emergency_exit(action)

                elif action_type == 'command':
                    command = action.get('command')
                    # Handle commands with parameters
                    if command == 'import':
                        command_handler.execute_command(command, script=action.get('script'))
                    elif command == 'report':
                        command_handler.execute_command(command, report_type=action.get('report_type'))
                    else:
                        command_handler.execute_command(command)

        except Exception as e:
            logger.error(f"Error processing Telegram callbacks: {e}")

    def _handle_signal_approved(self, action: dict) -> None:
        """Handle approved signal - place entry order"""
        signal_id = action.get('signal_id')
        script = action.get('script')
        entry_price = action.get('entry_price')

        logger.info(f"Processing approved signal: {script} @ {entry_price}")

        try:
            self.order_manager.place_entry_gtt(signal_id, script, entry_price)
        except Exception as e:
            logger.error(f"Failed to place entry order for {script}: {e}")
            telegram.send_alert(f"Failed to place entry for {script}: {str(e)}", critical=True)

    def _handle_emergency_exit(self, action: dict) -> None:
        """Handle emergency exit request"""
        position_id = action.get('position_id')
        script = action.get('script')

        logger.warning(f"Processing emergency exit: {script}")

        try:
            self.exit_manager.emergency_exit(position_id)
        except Exception as e:
            logger.error(f"Failed emergency exit for {script}: {e}")
            telegram.send_alert(f"Failed emergency exit for {script}: {str(e)}", critical=True)

    # =========================================================================
    # SIGNAL PROCESSING
    # =========================================================================

    def _process_signals(self) -> None:
        """Process new CSP signals from CSV"""
        try:
            new_signals = self.signal_processor.process_new_signals()
            if new_signals:
                logger.info(f"Processed {len(new_signals)} new signals")
        except Exception as e:
            logger.error(f"Error processing signals: {e}")

    def _send_pending_notifications(self) -> None:
        """Send Telegram notifications for pending signals"""
        try:
            # Check if we can send notifications
            can_notify, reason = self.signal_processor.can_send_notification()
            if not can_notify:
                logger.info(f"Cannot send notifications: {reason}")
                return

            # Get pending signals
            pending = self.signal_processor.get_pending_notifications()

            if not pending:
                logger.debug("No pending signals to notify")
                return

            logger.info(f"Found {len(pending)} pending signals to notify")

            for signal in pending[:3]:  # Max 3 at a time
                self.signal_processor.send_signal_notification(signal['id'])

        except Exception as e:
            logger.error(f"Error sending notifications: {e}")

    def _resend_hold_signals(self) -> None:
        """Re-notify signals on HOLD"""
        # Check if already run today
        last_renotify = get_bot_state('last_hold_renotify')
        today_str = today_ist().isoformat()

        if last_renotify == today_str:
            return

        try:
            count = self.signal_processor.resend_hold_signals()
            if count > 0:
                logger.info(f"Reset {count} HOLD signals for re-notification")
            set_bot_state('last_hold_renotify', today_str)
        except Exception as e:
            logger.error(f"Error resending hold signals: {e}")

    # =========================================================================
    # ORDER MONITORING
    # =========================================================================

    def _monitor_orders(self) -> None:
        """Monitor pending orders for fills"""
        try:
            self.order_manager.check_order_fills()
        except Exception as e:
            logger.error(f"Error monitoring orders: {e}")

    # =========================================================================
    # POSITION MONITORING
    # =========================================================================

    def _monitor_positions_for_drops(self) -> None:
        """Monitor positions for 30% drops"""
        try:
            self.exit_manager.check_for_drops()
        except Exception as e:
            logger.error(f"Error monitoring drops: {e}")

    def _update_monthly_trailing_sl(self) -> None:
        """Update trailing SL to monthly LOW (last trading day only)"""
        # Check if already run today
        last_sl_update = get_bot_state('last_monthly_sl_update')
        today_str = today_ist().isoformat()

        if last_sl_update == today_str:
            return

        logger.info("Running monthly SL trail update")

        try:
            updates = self.exit_manager.update_monthly_trailing_sl()
            set_bot_state('last_monthly_sl_update', today_str)

            if updates:
                telegram.send_alert(f"Monthly SL Updated: {len(updates)} positions")
        except Exception as e:
            logger.error(f"Error updating monthly SL: {e}")
            telegram.send_alert(f"Monthly SL update FAILED: {str(e)}", critical=True)

    # =========================================================================
    # RECOVERY & RECONCILIATION
    # =========================================================================

    def _run_recovery_checks(self) -> None:
        """Run recovery checks including GTT protection verification"""
        # Check if already run today
        last_recovery = get_bot_state('last_recovery_check')
        today_str = today_ist().isoformat()

        if last_recovery == today_str:
            return

        logger.info("Running recovery checks")

        try:
            # Reconcile positions with Zerodha
            self._reconcile_positions()

            # Verify all positions have active SL GTT protection (TR-C2 fix)
            self._verify_gtt_protection()

            # FIX ARCH-C1: Clean up stale PLACING orders (crashed mid-placement)
            self._cleanup_stale_placing_orders()

            set_bot_state('last_recovery_check', today_str)
        except Exception as e:
            logger.error(f"Recovery check failed: {e}")

    def _verify_gtt_protection(self) -> None:
        """
        Verify all open positions have active SL GTT protection.
        Attempts recovery for any unprotected positions. (TR-C2 fix)
        """
        logger.info("Verifying GTT protection for all positions")

        try:
            unprotected = self.exit_manager.verify_all_gtt_protection()

            if unprotected:
                # Some positions couldn't be recovered
                scripts = [p['script'] for p in unprotected]
                logger.warning(f"Unprotected positions found: {scripts}")

                # Alert already sent by verify_all_gtt_protection
                # for positions that couldn't be recovered
            else:
                logger.info("All positions have verified SL GTT protection")

        except Exception as e:
            logger.error(f"Error verifying GTT protection: {e}")
            telegram.send_alert(f"GTT verification error: {e}", critical=True)

    def _reconcile_positions(self) -> None:
        """
        Reconcile DB positions with actual Zerodha holdings.

        Checks BOTH holdings() and positions() APIs:
        - holdings(): Shows T+1 settled and T+2+ shares (quantity + t1_quantity)
        - positions(): Shows T+0 day trades (bought today, not yet in holdings)

        If a DB position is not found in either API, it was manually closed
        or SL triggered while bot was offline. Auto-closes it in DB.
        """
        try:
            from src.api.dual_kite_client import get_kite_client
            kite = get_kite_client()

            # 1. Get holdings (T+1 and settled shares)
            holdings = kite.get_holdings()
            holdings_map = {}
            for h in holdings:
                symbol = h.get('tradingsymbol', '')
                qty = h.get('quantity', 0)
                t1_qty = h.get('t1_quantity', 0)
                total_qty = qty + t1_qty
                if total_qty > 0:
                    holdings_map[symbol] = total_qty

            # 2. Get day positions (bought today, pending settlement tomorrow)
            zerodha_positions = kite.get_positions()
            day_pos_map = {}
            for p in zerodha_positions.get('net', []):
                symbol = p.get('tradingsymbol', '')
                qty = p.get('quantity', 0)
                if qty > 0:
                    day_pos_map[symbol] = qty

            # 3. Get today's sell orders (for exit price of stopped out positions)
            try:
                all_orders = kite.get_all_orders()
                today_sells = {}
                for o in all_orders:
                    if (o.get('transaction_type') == 'SELL' and
                            o.get('status') == 'COMPLETE'):
                        symbol = o.get('tradingsymbol', '')
                        today_sells[symbol] = o.get('average_price', 0)
            except Exception as e:
                logger.warning(f"Could not fetch today's orders for exit prices: {e}")
                today_sells = {}

            # 4. Reconcile each DB position
            session = get_session()
            try:
                db_positions = session.query(OpenPosition).filter(
                    OpenPosition.status == PositionStatus.OPEN
                ).all()

                if not db_positions:
                    logger.info("No open positions in DB to reconcile")
                    return

                closed_count = 0
                kept_count = 0

                for position in db_positions:
                    total_hld = holdings_map.get(position.script, 0)
                    day_qty = day_pos_map.get(position.script, 0)

                    # Position is active if in holdings OR bought today (day positions)
                    if total_hld > 0 or day_qty > 0:
                        kept_count += 1
                        continue

                    # Position NOT in Zerodha - was manually closed or SL triggered offline
                    sold_today = position.script in today_sells

                    if sold_today:
                        exit_price = today_sells[position.script]
                        exit_reason = 'GTT_SL_TRIGGERED'
                    else:
                        # Sold on prior day - use entry price as fallback
                        exit_price = position.entry_price
                        exit_reason = 'STALE_POSITION_SYNC'

                    logger.warning(
                        f"RECONCILE: {position.script} not found in Zerodha "
                        f"(holdings={total_hld}, day_pos={day_qty}). "
                        f"Closing with reason={exit_reason}, exit_price={exit_price:.2f}"
                    )

                    # Close the position in DB
                    self._lazy_load_processors()
                    self.exit_manager._close_position(
                        position, exit_price, exit_reason, session=session
                    )
                    closed_count += 1

                session.commit()

                if closed_count > 0:
                    telegram.send_alert(
                        f"<b>Position Reconciliation</b>\n\n"
                        f"Closed {closed_count} position(s) not found in Zerodha\n"
                        f"Active: {kept_count}",
                        critical=True
                    )
                else:
                    logger.info(f"Reconciliation: All {kept_count} positions verified in Zerodha")

            finally:
                session.close()

        except Exception as e:
            logger.error(f"Reconciliation error: {e}")
            telegram.send_alert(f"Reconciliation error: {str(e)}", critical=True)

    def _cleanup_stale_placing_orders(self) -> None:
        """
        FIX ARCH-C1: Clean up orders stuck in PLACING status.

        If an order has been PLACING for > 5 minutes, it means the process crashed
        mid-placement. Check Zerodha to see if GTT was actually placed and either:
        - Update to PENDING if GTT exists
        - Mark as REJECTED if GTT doesn't exist
        """
        from datetime import timedelta
        from src.api.dual_kite_client import get_kite_client

        session = get_session()
        try:
            # Find orders stuck in PLACING status for > 5 minutes
            cutoff_time = ist_now_naive() - timedelta(minutes=5)

            stale_orders = session.query(OpenOrder).filter(
                OpenOrder.status == OrderStatus.PLACING,
                OpenOrder.placed_at < cutoff_time
            ).all()

            if not stale_orders:
                return

            logger.warning(f"Found {len(stale_orders)} stale PLACING orders")

            kite = get_kite_client()
            existing_gtts = kite.get_gtt_orders()

            for order in stale_orders:
                try:
                    # Check if GTT was actually placed on Zerodha
                    found_gtt = None
                    for gtt in existing_gtts:
                        # FIX LOG-1: Safe access to orders list (could be empty)
                        orders = gtt.get('orders') or []
                        transaction_type = orders[0].get('transaction_type') if orders else None

                        if (gtt.get('condition', {}).get('tradingsymbol') == order.script and
                            gtt.get('status') == 'active' and
                            transaction_type == 'BUY'):
                            found_gtt = gtt
                            break

                    if found_gtt:
                        # GTT exists - update order to PENDING
                        gtt_id = str(found_gtt.get('id'))
                        order.gtt_id = gtt_id
                        order.status = OrderStatus.PENDING
                        logger.info(
                            f"Recovered stale PLACING order {order.script}: "
                            f"GTT {gtt_id} exists, updated to PENDING"
                        )
                    else:
                        # GTT doesn't exist - mark as REJECTED
                        order.status = OrderStatus.REJECTED
                        order.last_error = "Stale PLACING - no GTT found on Zerodha"
                        logger.warning(
                            f"Stale PLACING order {order.script}: "
                            f"No GTT found, marked as REJECTED"
                        )
                        telegram.send_alert(
                            f"<b>Stale Order Cleaned Up</b>\n\n"
                            f"{order.script}: Entry GTT placement failed\n"
                            f"Please re-approve if needed"
                        )

                except Exception as e:
                    logger.error(f"Error cleaning up stale order {order.script}: {e}")

            session.commit()

        except Exception as e:
            logger.error(f"Error in stale PLACING cleanup: {e}")
            session.rollback()
        finally:
            session.close()

    def _update_capital_ledger(self) -> None:
        """Update today's capital ledger"""
        try:
            from src.models.database import CapitalLedger, ist_now_naive
            from src.api.dual_kite_client import get_kite_client

            session = get_session()
            try:
                today = today_ist()

                # Check if entry exists
                ledger = session.query(CapitalLedger).filter(
                    CapitalLedger.date == today
                ).first()

                # Get margins
                kite = get_kite_client()
                margins = kite.get_margins()
                equity_margin = margins.get('data', {}).get('equity', {})
                available = equity_margin.get('available', {}).get('live_balance', 0)

                # Get deployed capital
                positions = session.query(OpenPosition).filter(
                    OpenPosition.status == PositionStatus.OPEN
                ).all()
                deployed = sum(p.capital_deployed for p in positions)

                pending_orders = session.query(OpenOrder).filter(
                    OpenOrder.status == OrderStatus.PENDING
                ).count()

                if ledger:
                    ledger.deployed_capital = deployed
                    ledger.free_capital = available
                    ledger.num_open_positions = len(positions)
                    ledger.num_pending_orders = pending_orders
                    ledger.updated_at = ist_now_naive()
                else:
                    ledger = CapitalLedger(
                        date=today,
                        opening_capital=config.get('trading.initial_capital', 100000),
                        deployed_capital=deployed,
                        free_capital=available,
                        num_open_positions=len(positions),
                        num_pending_orders=pending_orders
                    )
                    session.add(ledger)

                session.commit()

            finally:
                session.close()

        except Exception as e:
            logger.error(f"Error updating capital ledger: {e}")

    # =========================================================================
    # REPORTS
    # =========================================================================

    def _send_daily_report(self) -> None:
        """Send daily summary report"""
        # Check if already sent today
        last_report = get_bot_state('last_daily_report')
        today_str = today_ist().isoformat()

        if last_report == today_str:
            return

        try:
            command_handler.execute_command('report')
            set_bot_state('last_daily_report', today_str)
        except Exception as e:
            logger.error(f"Error sending daily report: {e}")

    def _send_weekly_report(self) -> None:
        """Send weekly report with statistics (HTML)"""
        # Check if already sent this week
        last_report = get_bot_state('last_weekly_report')
        today_str = today_ist().isoformat()

        if last_report == today_str:
            return

        try:
            # Use HTML report generator
            command_handler.execute_command('weekly')
            set_bot_state('last_weekly_report', today_str)

        except Exception as e:
            logger.error(f"Error sending weekly report: {e}")
            # Fallback to text report
            self._send_weekly_report_text()

    def _send_weekly_report_text(self) -> None:
        """Send weekly report (text fallback)"""
        try:
            from src.models.database import ClosedPosition
            from datetime import timedelta

            session = get_session()
            try:
                today = today_ist()
                week_start = today - timedelta(days=7)

                # Get week's closed positions
                week_closed = session.query(ClosedPosition).filter(
                    ClosedPosition.exit_date >= week_start
                ).all()

                # Get all-time stats
                all_closed = session.query(ClosedPosition).all()

                # Calculate week stats
                week_trades = len(week_closed)
                week_pnl = sum(c.net_pnl for c in week_closed) if week_closed else 0
                week_winners = sum(1 for c in week_closed if c.net_pnl > 0)

                # Calculate all-time stats
                total_trades = len(all_closed)
                total_pnl = sum(c.net_pnl for c in all_closed) if all_closed else 0
                total_winners = sum(1 for c in all_closed if c.net_pnl > 0)
                win_rate = (total_winners / total_trades * 100) if total_trades > 0 else 0

                # Get current positions
                open_positions = session.query(OpenPosition).filter(
                    OpenPosition.status == PositionStatus.OPEN
                ).all()
                num_positions = len(open_positions)
                total_deployed = sum(p.capital_deployed for p in open_positions)

                # Get pending orders
                pending_orders = session.query(OpenOrder).filter(
                    OpenOrder.status == OrderStatus.PENDING
                ).count()

                # Build report
                week_pnl_sign = "+" if week_pnl >= 0 else ""
                total_pnl_sign = "+" if total_pnl >= 0 else ""

                report = (
                    f"<b>FIFTY Weekly Report</b>\n"
                    f"{week_start.strftime('%d %b')} - {today.strftime('%d %b %Y')}\n\n"
                    f"<b>This Week</b>\n"
                    f"Trades Closed: {week_trades}\n"
                    f"P&L: {week_pnl_sign}{week_pnl:,.0f}\n"
                    f"Winners: {week_winners}/{week_trades}\n\n"
                    f"<b>All Time</b>\n"
                    f"Total Trades: {total_trades}\n"
                    f"Total P&L: {total_pnl_sign}{total_pnl:,.0f}\n"
                    f"Win Rate: {win_rate:.1f}%\n\n"
                    f"<b>Current Status</b>\n"
                    f"Open Positions: {num_positions}\n"
                    f"Deployed: {total_deployed:,.0f}\n"
                    f"Pending Orders: {pending_orders}"
                )

                telegram.send_alert(report)

            finally:
                session.close()

        except Exception as e:
            logger.error(f"Error sending weekly text report: {e}")

    # =========================================================================
    # MONTHLY REPORT (Last Trading Day)
    # =========================================================================

    def _send_monthly_report(self) -> None:
        """Send monthly performance report on last trading day (before cleanup)"""
        # Check if already run today
        last_report = get_bot_state('last_monthly_report')
        today_str = today_ist().isoformat()

        if last_report == today_str:
            return

        logger.info("Sending monthly report")

        try:
            from src.telegram.report_generator import report_generator

            # Generate monthly HTML report
            filepath = report_generator.generate_monthly_report()

            if filepath:
                # Try to convert to image
                image_path = report_generator.html_to_image(filepath)

                if image_path:
                    telegram.send_photo(image_path, caption="<b>FIFTY Monthly Report</b>")
                    report_generator.delete_report(image_path)
                    report_generator.delete_report(filepath)
                    logger.info("Monthly report image sent successfully")
                else:
                    # Fallback to HTML document
                    telegram.send_html_report(filepath, report_type="Monthly")
                    logger.info("Monthly HTML report sent successfully")

            # Also send text summary
            self._send_monthly_text_report()

            set_bot_state('last_monthly_report', today_str)

        except Exception as e:
            logger.error(f"Error sending monthly report: {e}")
            # Try text report as fallback
            self._send_monthly_text_report()

    def _send_monthly_text_report(self) -> None:
        """Send text-based monthly report as backup"""
        try:
            session = get_session()
            try:
                today = today_ist()
                month_start = today.replace(day=1)
                month_name = today.strftime('%B %Y')

                # Month's trades
                month_trades_list = session.query(ClosedPosition).filter(
                    ClosedPosition.exit_date >= month_start
                ).all()
                month_trades = len(month_trades_list)
                month_pnl = sum(t.net_pnl for t in month_trades_list)
                month_winners = sum(1 for t in month_trades_list if t.net_pnl > 0)

                # All-time stats
                all_trades = session.query(ClosedPosition).all()
                total_trades = len(all_trades)
                total_pnl = sum(t.net_pnl for t in all_trades)
                total_winners = sum(1 for t in all_trades if t.net_pnl > 0)
                win_rate = (total_winners / total_trades * 100) if total_trades > 0 else 0

                # Current status
                open_positions = session.query(OpenPosition).filter(
                    OpenPosition.status == PositionStatus.OPEN
                ).all()
                num_positions = len(open_positions)
                total_deployed = sum(p.capital_deployed for p in open_positions)

                pending_orders = session.query(OpenOrder).filter(
                    OpenOrder.status == OrderStatus.PENDING
                ).count()

                # ROI
                initial_capital = config.get('trading.initial_capital', 100000)
                month_roi = (month_pnl / initial_capital * 100) if initial_capital > 0 else 0
                total_roi = (total_pnl / initial_capital * 100) if initial_capital > 0 else 0

                month_pnl_sign = '+' if month_pnl >= 0 else ''
                total_pnl_sign = '+' if total_pnl >= 0 else ''
                month_roi_sign = '+' if month_roi >= 0 else ''
                total_roi_sign = '+' if total_roi >= 0 else ''

                # Build positions table
                positions_table = ""
                if open_positions:
                    positions_table = "\n<code>Script       Qty   Entry      SL    Days</code>\n<code>───────────────────────────────────────</code>\n"
                    for pos in open_positions:
                        script_name = pos.script[:12].ljust(12)
                        positions_table += f"<code>{script_name} {pos.quantity:3d} {pos.entry_price:7.2f} {pos.current_sl:7.2f} {pos.days_held:4d}d</code>\n"

                report = (
                    f"<b>FIFTY Monthly Report - {month_name}</b>\n\n"
                    f"<b>This Month</b>\n"
                    f"Trades Closed: {month_trades}\n"
                    f"P&L: {month_pnl_sign}{month_pnl:,.0f}\n"
                    f"ROI: {month_roi_sign}{month_roi:.2f}%\n"
                    f"Winners: {month_winners}/{month_trades}\n\n"
                    f"<b>All Time</b>\n"
                    f"Total Trades: {total_trades}\n"
                    f"Total P&L: {total_pnl_sign}{total_pnl:,.0f}\n"
                    f"Total ROI: {total_roi_sign}{total_roi:.2f}%\n"
                    f"Win Rate: {win_rate:.1f}%\n\n"
                    f"<b>Current Status</b>\n"
                    f"Open Positions: {num_positions}\n"
                    f"Deployed: {total_deployed:,.0f}\n"
                    f"Pending Orders: {pending_orders}"
                    f"{positions_table}"
                )

                telegram.send_alert(report)

            finally:
                session.close()

        except Exception as e:
            logger.error(f"Error sending monthly text report: {e}")

    # =========================================================================
    # MONTH-END CLEANUP
    # =========================================================================

    def _month_end_cleanup(self) -> None:
        """Month-end cleanup tasks"""
        # Check if already run today
        last_cleanup = get_bot_state('last_month_cleanup')
        today_str = today_ist().isoformat()

        if last_cleanup == today_str:
            return

        logger.info("Running month-end cleanup")

        try:
            # Cancel unfilled entry GTTs
            cancelled = self.order_manager.cancel_unfilled_entry_gtts()

            # Invalidate pending signals
            invalidated = self._invalidate_pending_signals()

            telegram.send_alert(
                f"<b>Month-End Cleanup</b>\n\n"
                f"Entry GTTs Cancelled: {cancelled}\n"
                f"Signals Invalidated: {invalidated}"
            )

            set_bot_state('last_month_cleanup', today_str)

        except Exception as e:
            logger.error(f"Month-end cleanup failed: {e}")
            telegram.send_alert(f"Month-end cleanup FAILED: {str(e)}", critical=True)

    def _invalidate_pending_signals(self) -> int:
        """
        Invalidate pending/notified/hold signals at month end.

        Per design: Only invalidate if monthly_close < signal_level.
        Otherwise, mark as EXPIRED (signal still valid but month ended).
        """
        from src.api.dual_kite_client import get_kite_client

        session = get_session()
        try:
            signals = session.query(SignalQueue).filter(
                SignalQueue.status.in_([
                    SignalStatus.PENDING,
                    SignalStatus.NOTIFIED,
                    SignalStatus.HOLD
                ])
            ).all()

            if not signals:
                return 0

            kite = get_kite_client()
            invalidated_count = 0
            expired_count = 0

            for signal in signals:
                try:
                    # Get monthly close for this script
                    instrument_token = kite.get_instrument_token(signal.script)
                    if not instrument_token:
                        logger.warning(f"No token for {signal.script}, expiring signal")
                        signal.status = SignalStatus.EXPIRED
                        expired_count += 1
                        continue

                    monthly_df = kite.get_historical_data_sampled(
                        instrument_token=instrument_token,
                        timeframe='monthly',
                        years_back=0.5
                    )

                    if monthly_df is None or monthly_df.empty:
                        logger.warning(f"No monthly data for {signal.script}, expiring signal")
                        signal.status = SignalStatus.EXPIRED
                        expired_count += 1
                        continue

                    # FIX TR-C4: Get COMPLETED month's close, not current incomplete candle
                    # Monthly data uses 'ME' resampling - last row might be incomplete
                    # Use second-to-last row to ensure we have completed month data
                    if len(monthly_df) >= 2:
                        monthly_close = float(monthly_df['Close'].iloc[-2])
                        candle_date = monthly_df['Date'].iloc[-2]
                    else:
                        # Fallback: only one candle available
                        monthly_close = float(monthly_df['Close'].iloc[-1])
                        candle_date = monthly_df['Date'].iloc[-1]

                    logger.debug(f"{signal.script}: Using monthly close {monthly_close:.2f} from {candle_date}")
                    signal_level = signal.signal_level

                    if monthly_close < signal_level:
                        # Price closed below SuperTrend level - INVALIDATE
                        signal.status = SignalStatus.INVALIDATED
                        invalidated_count += 1
                        logger.info(
                            f"{signal.script}: INVALIDATED - "
                            f"monthly close {monthly_close:.2f} < level {signal_level:.2f}"
                        )
                        telegram.send_alert(
                            f"<b>Signal Invalidated</b>\n\n"
                            f"{signal.script}\n"
                            f"Monthly Close: {monthly_close:,.2f}\n"
                            f"Signal Level: {signal_level:,.2f}\n"
                            f"Reason: Close below SuperTrend"
                        )
                    else:
                        # Price still above level but month ended - just expire
                        signal.status = SignalStatus.EXPIRED
                        expired_count += 1
                        logger.info(
                            f"{signal.script}: Expired (still valid) - "
                            f"monthly close {monthly_close:.2f} >= level {signal_level:.2f}"
                        )

                except Exception as e:
                    logger.error(f"Error checking {signal.script}: {e}")
                    signal.status = SignalStatus.EXPIRED
                    expired_count += 1

            session.commit()

            logger.info(f"Month-end: {invalidated_count} invalidated, {expired_count} expired")
            return invalidated_count

        finally:
            session.close()

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _is_last_trading_day(self) -> bool:
        """
        Check if today is the last trading day of the month.

        FIX TR-H3: Now checks NSE holidays in addition to weekends.
        """
        from datetime import timedelta
        from src.utils.timezone_helper import is_market_day_ist

        today = today_ist()
        month_end = get_month_end(today)

        # Find last trading day (not weekend AND not NSE holiday)
        last_trading = month_end
        max_iterations = 10  # Safety limit
        iterations = 0

        while not is_market_day_ist(last_trading) and iterations < max_iterations:
            last_trading = last_trading - timedelta(days=1)
            iterations += 1

        return today == last_trading


# Singleton instance
orchestrator = Orchestrator()
