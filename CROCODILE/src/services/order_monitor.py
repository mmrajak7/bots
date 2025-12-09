"""Order Fill Monitor - Detect filled orders, create positions, and monitor GTT status"""

from datetime import datetime, date
from typing import Dict, Optional, List
from loguru import logger
from sqlalchemy.orm import Session

from src.models.database import (
    OpenOrder, OpenPosition, TransactionHistory, CapitalLedger, ProcessedSignal,
    OrderStatus, PositionStatus, TransactionType, get_session
)
from src.models.kite_constants import KiteOrderStatus
from src.services.exit_manager import exit_manager
from src.api.broker_factory import get_broker
from src.utils.config_manager import config
from src.utils.timezone_helper import ist_now_naive
from src.utils.cost_calculator import cost_calculator
from src.reporting.telegram_client import telegram


class OrderMonitor:
    """
    Monitor pending orders and handle fills

    Workflow:
    1. Query all PENDING orders from database
    2. Check status with Zerodha API
    3. If FILLED → Create position + Place dummy GTT
    4. If CANCELLED/REJECTED → Update status
    """

    def __init__(self):
        """Initialize order monitor"""
        self.kite_client = get_broker()
        logger.info("Order Monitor initialized")

    def check_order_status(self, open_order: OpenOrder, session: Session) -> bool:
        """
        Check status of a single order

        Args:
            open_order: OpenOrder object
            session: Database session

        Returns:
            True if order filled (position created)
        """
        try:
            # Fetch order status from Zerodha
            order_details = self.kite_client.get_order_status(open_order.order_id)

            status = order_details.get('status', '')
            filled_qty = order_details.get('filled_quantity', 0)
            avg_price = order_details.get('average_price', 0)
            zerodha_symbol = order_details.get('tradingsymbol', open_order.script)

            logger.info(
                f"Order {open_order.order_id} ({open_order.script}): "
                f"Status={status}, FilledQty={filled_qty}/{open_order.quantity}, AvgPrice={avg_price}"
            )

            # Check if order was cancelled or rejected (handle first to avoid processing)
            if status == KiteOrderStatus.CANCELLED:
                capital_released = open_order.capital_deployed

                open_order.status = OrderStatus.CANCELLED
                open_order.rejection_reason = order_details.get('status_message', 'Cancelled by user or system')

                # Release capital and update signal status
                self._release_order_capital(open_order, 0, session)
                self._update_processed_signal_expired(open_order, session)

                session.commit()

                logger.warning(
                    f"Order {open_order.order_id} ({open_order.script}) CANCELLED: "
                    f"{open_order.rejection_reason} | Capital released: Rs.{capital_released:.2f}"
                )

                # Send Telegram alert for manual cancellations
                telegram.send_alert(
                    f"⚠️ **Order Cancelled**\n\n"
                    f"📊 *{open_order.script} ({open_order.timeframe})*\n"
                    f"❌ Order {open_order.order_id} was cancelled\n"
                    f"💰 Capital released: Rs.{capital_released:.2f}\n"
                    f"Reason: {open_order.rejection_reason}",
                    critical=False
                )

                return False

            elif status == KiteOrderStatus.REJECTED:
                capital_released = open_order.capital_deployed

                open_order.status = OrderStatus.REJECTED
                open_order.rejection_reason = order_details.get('status_message', 'Rejected by exchange')

                # Release capital and update signal status
                self._release_order_capital(open_order, 0, session)
                self._update_processed_signal_expired(open_order, session)

                session.commit()

                logger.warning(
                    f"Order {open_order.order_id} ({open_order.script}) REJECTED: "
                    f"{open_order.rejection_reason} | Capital released: Rs.{capital_released:.2f}"
                )

                # Send Telegram alert for rejections
                telegram.send_alert(
                    f"🔴 **Order Rejected**\n\n"
                    f"📊 *{open_order.script} ({open_order.timeframe})*\n"
                    f"❌ Order {open_order.order_id} rejected by exchange\n"
                    f"💰 Capital released: Rs.{capital_released:.2f}\n"
                    f"Reason: {open_order.rejection_reason}",
                    critical=False
                )

                return False

            # Check if order has any fills (handles both partial and complete fills)
            if filled_qty > 0 and filled_qty > open_order.filled_qty:
                # New fills detected!
                previous_filled = open_order.filled_qty
                new_fills = filled_qty - previous_filled

                logger.info(
                    f"New fills detected: {open_order.script} - "
                    f"Previous: {previous_filled}, Current: {filled_qty}, New: {new_fills}"
                )

                # Update filled quantity
                open_order.filled_qty = filled_qty
                open_order.fill_price = avg_price

                # Only create position once (when position_created is False)
                if not open_order.position_created:
                    return self._handle_order_fill(open_order, avg_price, filled_qty, zerodha_symbol, session)
                else:
                    # Position already created, just log the additional fills
                    logger.warning(
                        f"Order {open_order.order_id} has additional fills ({new_fills} shares) "
                        f"but position already created. Total filled: {filled_qty}/{open_order.quantity}"
                    )

                    # Mark as FILLED if fully filled
                    if filled_qty >= open_order.quantity:
                        open_order.status = OrderStatus.FILLED
                        open_order.filled_at = ist_now_naive()

                    session.commit()
                    return False

            # Check if order is fully filled
            elif status == KiteOrderStatus.COMPLETE and filled_qty >= open_order.quantity:
                # Fully filled but position not created yet (shouldn't happen, but handle it)
                if not open_order.position_created:
                    logger.warning(
                        f"Order {open_order.order_id} marked COMPLETE but position not created - "
                        f"handling now"
                    )
                    return self._handle_order_fill(open_order, avg_price, filled_qty, zerodha_symbol, session)
                else:
                    # Already handled
                    open_order.status = OrderStatus.FILLED
                    session.commit()
                    return False

            else:
                # Still pending (OPEN, TRIGGER PENDING, etc.) or no new fills
                logger.debug(f"Order {open_order.order_id} still pending - status: {status}")
                return False

        except Exception as e:
            logger.error(f"Failed to check order status for {open_order.order_id}: {e}")
            return False

    def _handle_order_fill(
        self,
        open_order: OpenOrder,
        fill_price: float,
        filled_qty: int,
        zerodha_symbol: str,
        session: Session
    ) -> bool:
        """
        Handle order fill - create position and place dummy GTT

        Args:
            open_order: OpenOrder that got filled
            fill_price: Actual fill price
            filled_qty: Filled quantity (actual filled, handles partial fills)
            zerodha_symbol: Trading symbol from Zerodha (correct symbol without special chars)
            session: Database session

        Returns:
            Success status
        """
        try:
            logger.info(
                f"🎯 Order FILLED: {open_order.script} {open_order.timeframe} - "
                f"{filled_qty} shares @ Rs.{fill_price:.2f}"
            )

            # Mark position as created to prevent duplicate position creation
            open_order.position_created = True

            # Update order status only if fully filled
            if filled_qty >= open_order.quantity:
                open_order.status = OrderStatus.FILLED
            # Otherwise keep as PENDING (partial fill)

            # Set filled_at timestamp if not already set
            if open_order.filled_at is None:
                open_order.filled_at = ist_now_naive()

            open_order.fill_price = fill_price

            # Calculate capital deployed (for the filled quantity)
            capital_deployed = fill_price * filled_qty

            # Create OpenPosition (use Zerodha symbol to avoid issues with special chars)
            position = OpenPosition(
                bot_instance_id=config.get_bot_instance_id(),
                script=zerodha_symbol,  # Use correct Zerodha symbol, not signals.csv symbol
                timeframe=open_order.timeframe,
                entry_date=date.today(),
                entry_price=fill_price,
                quantity=filled_qty,
                capital_deployed=capital_deployed,
                current_sl=0.0,  # Will be set by dummy GTT
                initial_sl=0.0,  # Will be set by dummy GTT
                status=PositionStatus.OPEN,
                days_held=0
            )

            session.add(position)
            session.flush()  # Get position.id

            # Create transaction history entry
            transaction = TransactionHistory(
                bot_instance_id=config.get_bot_instance_id(),
                transaction_type=TransactionType.ENTRY,
                script=zerodha_symbol,  # Use Zerodha symbol for consistency
                timeframe=open_order.timeframe,
                transaction_date=date.today(),
                price=fill_price,
                quantity=filled_qty,
                value=capital_deployed,
                order_id=open_order.order_id,
                position_id=position.id,
                notes=f"Entry order filled @ Rs.{fill_price:.2f}"
            )

            session.add(transaction)
            session.commit()

            # Place dummy protective GTT (using Zerodha symbol)
            logger.info(f"Placing dummy GTT for {position.script}")
            success, gtt_id, error = exit_manager.place_dummy_gtt(position, fill_price, session, zerodha_symbol)

            if not success:
                # CRITICAL: Position created but no GTT!
                critical_msg = (
                    f"🔴 **CRITICAL: Position UNPROTECTED!**\n"
                    f"📊 {zerodha_symbol} {position.timeframe}\n"
                    f"💰 {filled_qty} shares @ Rs.{fill_price:.2f}\n"
                    f"❌ Dummy GTT placement FAILED\n"
                    f"Error: {error}\n"
                    f"⚠️ **Manual intervention required - place stop loss manually!**"
                )
                logger.critical(critical_msg)

                # Send critical Telegram alert
                telegram.send_alert(critical_msg, critical=True)

            else:
                # Check if verification passed or failed (error will have message if verification failed)
                verification_status = "GTT verified ✓" if error is None else "GTT verification pending ⚠️"

                logger.info(
                    f"✅ Position created with protective GTT: {zerodha_symbol} - "
                    f"GTT ID: {gtt_id}, SL: Rs.{position.current_sl:.2f}"
                )

                # Send detailed success message
                success_msg = (
                    f"✅ **Order Filled & Position Created**\n\n"
                    f"📊 *{zerodha_symbol} ({position.timeframe})*\n"
                    f"• Order filled detected\n"
                    f"• Position created @ Rs.{fill_price:.2f}\n"
                    f"• Qty: {filled_qty} shares\n"
                    f"• GTT placed: SL @ Rs.{position.current_sl:.2f}\n"
                    f"• {verification_status}\n"
                    f"• Capital deployed: Rs.{fill_price * filled_qty:.2f}"
                )
                telegram.send_alert(success_msg, critical=False)

            # Update capital ledger
            self._update_capital_ledger(capital_deployed, session)

            return True

        except Exception as e:
            logger.error(f"Failed to handle order fill for {open_order.order_id}: {e}")
            session.rollback()
            return False

    def _update_capital_ledger(self, capital_deployed: float, session: Session):
        """Update capital ledger with new deployment"""
        try:
            today = date.today()
            ledger = session.query(CapitalLedger).filter_by(date=today).first()

            if ledger:
                ledger.deployed_capital += capital_deployed
                ledger.free_capital -= capital_deployed
                ledger.num_open_positions += 1
                ledger.num_entries_today += 1
                ledger.num_trades_today += 1
                session.commit()

                logger.debug(
                    f"Capital ledger updated: Deployed +Rs.{capital_deployed:.2f}, "
                    f"Total deployed=Rs.{ledger.deployed_capital:.2f}"
                )

        except Exception as e:
            logger.warning(f"Failed to update capital ledger: {e}")

    def monitor_all_pending_orders(self, session: Optional[Session] = None) -> Dict[str, int]:
        """
        Check all pending orders and handle fills

        Returns:
            Stats dict with counts
        """
        close_session = False
        if session is None:
            session = get_session()
            close_session = True

        stats = {
            'pending': 0,
            'filled': 0,
            'cancelled': 0,
            'still_pending': 0
        }

        try:
            # Get all pending orders
            pending_orders = session.query(OpenOrder).filter_by(
                status=OrderStatus.PENDING
            ).all()

            stats['pending'] = len(pending_orders)

            if not pending_orders:
                logger.debug("No pending orders to monitor")
                return stats

            logger.info(f"Monitoring {len(pending_orders)} pending orders")

            for order in pending_orders:
                filled = self.check_order_status(order, session)

                if filled:
                    stats['filled'] += 1
                elif order.status in [OrderStatus.CANCELLED, OrderStatus.REJECTED]:
                    stats['cancelled'] += 1
                else:
                    stats['still_pending'] += 1

            logger.info(
                f"Order monitoring complete: Pending={stats['pending']}, "
                f"Filled={stats['filled']}, Cancelled={stats['cancelled']}, "
                f"StillPending={stats['still_pending']}"
            )

            return stats

        except Exception as e:
            logger.error(f"Error monitoring orders: {e}")
            if close_session:
                session.rollback()  # Rollback any uncommitted changes
            raise
        finally:
            if close_session:
                session.close()

    def check_gtt_triggered_positions(self, session: Optional[Session] = None) -> Dict[str, any]:
        """
        Check all open positions for triggered GTTs (SL hit) and close them immediately.

        This runs every 5 minutes during market hours to detect SL hits in real-time,
        instead of waiting for Same-Day Recovery at 4 PM.

        Returns:
            Stats dict with:
            - positions_checked: Total open positions checked
            - sl_hits_detected: Positions where SL was hit
            - positions_closed: Successfully closed positions
            - errors: List of errors encountered
        """
        close_session = False
        if session is None:
            session = get_session()
            close_session = True

        stats = {
            'positions_checked': 0,
            'sl_hits_detected': 0,
            'positions_closed': 0,
            'errors': []
        }

        try:
            # Get all open positions for this bot
            open_positions = session.query(OpenPosition).filter_by(
                bot_instance_id=config.get_bot_instance_id(),
                status=PositionStatus.OPEN
            ).all()

            stats['positions_checked'] = len(open_positions)

            if not open_positions:
                logger.debug("No open positions to check for GTT status")
                return stats

            logger.info(f"Checking GTT status for {len(open_positions)} open positions...")

            # Fetch all active GTTs from Zerodha (single API call)
            try:
                zerodha_gtts = self.kite_client.get_gtt_orders()
                logger.debug(f"Fetched {len(zerodha_gtts)} GTTs from Zerodha")
            except Exception as e:
                error_msg = f"Failed to fetch GTTs from Zerodha: {e}"
                logger.error(error_msg)
                stats['errors'].append(error_msg)
                return stats

            # Build GTT lookup: {gtt_id: gtt_data}
            gtt_lookup = {str(gtt.get('id')): gtt for gtt in zerodha_gtts}

            # Check each position's GTT status
            for position in open_positions:
                position_key = f"{position.script} {position.timeframe}"

                # Skip if position has no GTT ID
                if not position.current_gtt_id:
                    logger.warning(f"{position_key}: No GTT ID - skipping status check")
                    # Alert: Position has no SL protection!
                    telegram.send_message(
                        f"⚠️ *UNPROTECTED POSITION*\n"
                        f"{position_key} has NO GTT/SL!\n"
                        f"Entry: ₹{position.entry_price:.2f}\n"
                        f"Qty: {position.quantity}"
                    )
                    continue

                # Check if GTT exists in Zerodha
                gtt = gtt_lookup.get(str(position.current_gtt_id))

                if gtt is None:
                    # GTT not found - might have been triggered and removed
                    logger.warning(f"{position_key}: GTT {position.current_gtt_id} not found in Zerodha")
                    # Check if there's a completed SELL order for this position
                    sl_hit = self._check_for_sell_order(position)
                    if sl_hit:
                        stats['sl_hits_detected'] += 1
                        closed = self._close_position_on_sl_hit(position, session)
                        if closed:
                            stats['positions_closed'] += 1
                    else:
                        # GTT missing and no sell order - position is unprotected!
                        telegram.send_message(
                            f"⚠️ *GTT MISSING*\n"
                            f"{position_key}\n"
                            f"GTT {position.current_gtt_id} not found!\n"
                            f"No sell order detected.\n"
                            f"Position may be UNPROTECTED!"
                        )
                    continue

                # Check GTT status
                gtt_status = gtt.get('status', '').lower()

                if gtt_status == 'triggered':
                    # GTT was triggered - SL HIT!
                    logger.warning(
                        f"🔴 SL HIT DETECTED: {position_key} - GTT {position.current_gtt_id} triggered"
                    )
                    stats['sl_hits_detected'] += 1

                    # Close the position immediately
                    closed = self._close_position_on_sl_hit(position, session, gtt)
                    if closed:
                        stats['positions_closed'] += 1
                    else:
                        stats['errors'].append(f"Failed to close {position_key}")

                elif gtt_status != 'active':
                    # GTT exists but not active (cancelled, disabled, etc.)
                    logger.warning(
                        f"{position_key}: GTT {position.current_gtt_id} status is '{gtt_status}' (not active)"
                    )
                    # Alert: GTT is not active - position may be unprotected
                    telegram.send_message(
                        f"⚠️ *GTT NOT ACTIVE*\n"
                        f"{position_key}\n"
                        f"GTT {position.current_gtt_id} status: {gtt_status}\n"
                        f"Position may be UNPROTECTED!"
                    )

            # Log summary
            if stats['sl_hits_detected'] > 0:
                logger.info(
                    f"GTT status check complete: {stats['sl_hits_detected']} SL hits detected, "
                    f"{stats['positions_closed']} positions closed"
                )
            else:
                logger.info(f"GTT status check complete: No SL hits detected ({stats['positions_checked']} positions checked)")

            return stats

        except Exception as e:
            logger.error(f"Error checking GTT triggered positions: {e}")
            stats['errors'].append(str(e))
            if close_session:
                session.rollback()
            return stats
        finally:
            if close_session:
                session.close()

    def check_pending_sl_orders(self, session: Optional[Session] = None) -> Dict[str, any]:
        """
        Check for pending SELL orders from GTT triggers that haven't filled.

        When a GTT triggers, it places a LIMIT sell order. If the price drops
        quickly, this LIMIT order may not fill. This function:
        1. Finds pending SELL LIMIT orders
        2. Checks if LTP has moved > 0.5% below the order price
        3. If so, converts the LIMIT order to MARKET for immediate execution

        Returns:
            Stats dict with orders_checked, orders_converted, errors
        """
        close_session = False
        if session is None:
            session = get_session()
            close_session = True

        stats = {
            'orders_checked': 0,
            'orders_converted': 0,
            'orders_filled': 0,
            'errors': []
        }

        # Threshold for converting LIMIT to MARKET (0.5% below trigger price)
        PRICE_DRIFT_THRESHOLD = 0.005  # 0.5%

        try:
            # Get all orders from Zerodha
            all_orders = self.kite_client.get_all_orders()

            # Filter for pending SELL LIMIT orders (likely from GTT triggers)
            pending_sells = [
                o for o in all_orders
                if o.get('transaction_type') == 'SELL'
                and o.get('status') in ['OPEN', 'PENDING', 'TRIGGER PENDING']
                and o.get('order_type') == 'LIMIT'
                and o.get('product') == 'CNC'
            ]

            if not pending_sells:
                logger.debug("No pending SELL LIMIT orders found")
                return stats

            logger.info(f"Checking {len(pending_sells)} pending SELL LIMIT orders...")
            stats['orders_checked'] = len(pending_sells)

            for order in pending_sells:
                symbol = order.get('tradingsymbol', '')
                order_id = order.get('order_id')
                order_price = order.get('price', 0)
                quantity = order.get('quantity', 0)

                # Get current LTP
                try:
                    ltp = self._get_ltp(symbol)
                    if ltp is None:
                        logger.warning(f"Could not get LTP for {symbol}")
                        continue
                except Exception as e:
                    logger.error(f"Error getting LTP for {symbol}: {e}")
                    continue

                # Calculate price drift
                price_drift = (order_price - ltp) / order_price if order_price > 0 else 0

                logger.info(
                    f"Pending SELL: {symbol} - Order Price: {order_price:.2f}, "
                    f"LTP: {ltp:.2f}, Drift: {price_drift*100:.2f}%"
                )

                # If LTP has drifted more than threshold below order price, convert to MARKET
                if price_drift > PRICE_DRIFT_THRESHOLD:
                    logger.warning(
                        f"🔴 Price drifted {price_drift*100:.2f}% below LIMIT price for {symbol}! "
                        f"Converting to MARKET order..."
                    )

                    try:
                        # Modify order to MARKET type
                        kite = self.kite_client._get_kite()
                        kite.modify_order(
                            variety='regular',
                            order_id=order_id,
                            order_type='MARKET'
                        )

                        stats['orders_converted'] += 1
                        logger.info(f"✅ Converted {symbol} SELL order {order_id} to MARKET")

                        # Send Telegram alert
                        telegram.send_alert(
                            f"⚠️ **SL Order Converted to MARKET**\n\n"
                            f"Stock: {symbol}\n"
                            f"Original LIMIT: ₹{order_price:.2f}\n"
                            f"Current LTP: ₹{ltp:.2f}\n"
                            f"Price drift: {price_drift*100:.2f}%\n\n"
                            f"*Converted to MARKET for immediate exit*",
                            critical=True
                        )

                    except Exception as e:
                        error_msg = f"Failed to convert {symbol} order to MARKET: {e}"
                        logger.error(error_msg)
                        stats['errors'].append(error_msg)

                        # Alert about failed conversion
                        telegram.send_alert(
                            f"🚨 **CRITICAL: SL Order Stuck**\n\n"
                            f"Stock: {symbol}\n"
                            f"LIMIT Price: ₹{order_price:.2f}\n"
                            f"Current LTP: ₹{ltp:.2f}\n"
                            f"Drift: {price_drift*100:.2f}%\n\n"
                            f"*Failed to convert to MARKET: {e}*\n"
                            f"**MANUAL INTERVENTION REQUIRED**",
                            critical=True
                        )

            return stats

        except Exception as e:
            logger.error(f"Error checking pending SL orders: {e}", exc_info=True)
            stats['errors'].append(str(e))
            return stats
        finally:
            if close_session:
                session.close()

    def _get_ltp(self, symbol: str) -> Optional[float]:
        """Get Last Traded Price for a symbol"""
        try:
            kite = self.kite_client._get_kite()
            # Try NSE first, then BSE
            for exchange in ['NSE', 'BSE']:
                try:
                    quote = kite.ltp(f"{exchange}:{symbol}")
                    if quote:
                        key = f"{exchange}:{symbol}"
                        if key in quote:
                            return quote[key].get('last_price')
                except:
                    continue
            return None
        except Exception as e:
            logger.error(f"Error fetching LTP for {symbol}: {e}")
            return None

    def _check_for_sell_order(self, position: OpenPosition) -> bool:
        """
        Check if there's a completed SELL order for a position (indicates SL hit)

        Args:
            position: OpenPosition to check

        Returns:
            True if SELL order found (SL was hit)
        """
        try:
            all_orders = self.kite_client.get_all_orders()

            for order in all_orders:
                if (order.get('tradingsymbol') == position.script and
                    order.get('transaction_type') == 'SELL' and
                    order.get('status') == 'COMPLETE' and
                    order.get('quantity') == position.quantity):
                    logger.info(
                        f"Found SELL order for {position.script}: "
                        f"Order ID {order.get('order_id')}, Price Rs.{order.get('average_price'):.2f}"
                    )
                    return True

            return False

        except Exception as e:
            logger.error(f"Error checking for SELL order: {e}")
            return False

    def _close_position_on_sl_hit(
        self,
        position: OpenPosition,
        session: Session,
        gtt: Optional[Dict] = None
    ) -> bool:
        """
        Close a position when SL is hit (GTT triggered)

        Args:
            position: OpenPosition to close
            session: Database session
            gtt: GTT data from Zerodha (optional)

        Returns:
            True if position closed successfully
        """
        try:
            position_key = f"{position.script} {position.timeframe}"
            logger.info(f"Closing position {position_key} - SL hit detected")

            # Find actual exit price from completed SELL order
            exit_price = None
            exit_source = 'UNKNOWN'

            try:
                all_orders = self.kite_client.get_all_orders()

                for order in all_orders:
                    if (order.get('tradingsymbol') == position.script and
                        order.get('transaction_type') == 'SELL' and
                        order.get('status') == 'COMPLETE' and
                        order.get('quantity') == position.quantity):
                        exit_price = order.get('average_price')
                        exit_source = 'SELL_ORDER'
                        logger.info(f"Exit price from SELL order: Rs.{exit_price:.2f}")
                        break

            except Exception as e:
                logger.warning(f"Failed to fetch orders for exit price: {e}")

            # Fallback to current_sl if no order found
            if exit_price is None:
                exit_price = position.current_sl
                exit_source = 'FALLBACK_SL'
                logger.warning(f"Using fallback exit price (current_sl): Rs.{exit_price:.2f}")

            # Calculate P&L
            pnl_details = cost_calculator.calculate_pnl(
                entry_price=position.entry_price,
                exit_price=exit_price,
                quantity=position.quantity
            )

            # Update position status
            position.status = PositionStatus.CLOSED_SL
            position.exit_date = date.today()
            position.exit_price = exit_price
            position.exit_reason = 'SL_HIT'

            # Create closed position record
            from src.models.database import ClosedPosition

            days_held = (date.today() - position.entry_date).days

            closed = ClosedPosition(
                script=position.script,
                timeframe=position.timeframe,
                entry_date=position.entry_date,
                entry_price=position.entry_price,
                quantity=position.quantity,
                capital_deployed=position.capital_deployed,
                exit_date=date.today(),
                exit_price=exit_price,
                exit_reason='SL_HIT',
                gross_pnl=pnl_details['gross_pnl'],
                transaction_costs=pnl_details['total_cost'],
                cost_breakdown=pnl_details['cost_breakdown'],
                net_pnl=pnl_details['net_pnl'],
                pnl_percent=pnl_details['pnl_percent'],
                days_held=days_held,
                sl_movements=position.sl_movements,
                highest_sl_achieved=position.highest_sl
            )

            # Create transaction history
            transaction = TransactionHistory(
                bot_instance_id=config.get_bot_instance_id(),
                transaction_type=TransactionType.EXIT_SL,
                script=position.script,
                timeframe=position.timeframe,
                transaction_date=date.today(),
                price=exit_price,
                quantity=position.quantity,
                value=exit_price * position.quantity,
                position_id=position.id,
                notes=f"SL hit - GTT triggered (source: {exit_source})"
            )

            session.add(closed)
            session.add(transaction)

            # Update capital ledger - free up the position slot and update realized P&L
            self._update_capital_ledger_on_close(position.capital_deployed, pnl_details['net_pnl'], session)

            session.commit()

            logger.info(
                f"✅ Position closed: {position_key} - "
                f"Exit @ Rs.{exit_price:.2f}, P&L: Rs.{pnl_details['net_pnl']:.2f} ({pnl_details['pnl_percent']:.2f}%)"
            )

            # Send Telegram alert
            pnl_emoji = "🟢" if pnl_details['net_pnl'] >= 0 else "🔴"
            telegram.send_alert(
                f"🔴 **SL HIT - Position Closed**\n\n"
                f"📊 *{position_key}*\n"
                f"• Entry: Rs.{position.entry_price:.2f}\n"
                f"• Exit: Rs.{exit_price:.2f}\n"
                f"• Qty: {position.quantity} shares\n"
                f"• {pnl_emoji} P&L: Rs.{pnl_details['net_pnl']:.2f} ({pnl_details['pnl_percent']:.2f}%)\n"
                f"• Days held: {days_held}\n"
                f"• Exit source: {exit_source}\n\n"
                f"_Position slot freed - ready for new signals_",
                critical=False
            )

            return True

        except Exception as e:
            logger.error(f"Failed to close position {position.script}: {e}")
            session.rollback()
            return False

    def _update_capital_ledger_on_close(self, capital_released: float, net_pnl: float, session: Session):
        """Update capital ledger when position is closed (free up capital, update P&L)"""
        try:
            today = date.today()
            ledger = session.query(CapitalLedger).filter_by(date=today).first()

            if ledger:
                ledger.deployed_capital -= capital_released
                ledger.free_capital += capital_released
                ledger.num_open_positions -= 1
                ledger.num_exits_today += 1
                ledger.realized_pnl_today += net_pnl

                logger.info(
                    f"Capital ledger updated on close: Released Rs.{capital_released:.2f}, "
                    f"P&L: Rs.{net_pnl:+.2f}, Today's realized: Rs.{ledger.realized_pnl_today:+.2f}, "
                    f"Open positions: {ledger.num_open_positions}"
                )
            else:
                logger.warning("No capital ledger found for today - cannot update on close")

        except Exception as e:
            logger.warning(f"Failed to update capital ledger on close: {e}")

    def manage_stale_pending_orders(self, session: Optional[Session] = None) -> Dict[str, any]:
        """
        Auto-cancel stale pending orders to free up capital for higher-probability trades.

        Stale Order Criteria (from config):
        1. Time-based: Order pending > max_pending_hours (default: 2 hours)
        2. Price-based: LTP moved > threshold_pct above limit price (default: 2%)

        This runs every 5 minutes as part of the order monitoring workflow.
        When cancelled: Capital released, Telegram alert sent, ProcessedSignal updated.

        Returns:
            Stats dict with:
            - pending_checked: Total pending orders checked
            - stale_by_time: Orders cancelled due to time
            - stale_by_price: Orders cancelled due to price distance
            - capital_released: Total capital freed
            - errors: List of errors encountered
        """
        close_session = False
        if session is None:
            session = get_session()
            close_session = True

        stats = {
            'pending_checked': 0,
            'stale_by_time': 0,
            'stale_by_price': 0,
            'capital_released': 0.0,
            'cancelled_orders': [],
            'errors': []
        }

        try:
            # Check if stale order management is enabled
            pom_config = config.get('risk_management.pending_order_management', {})
            if not pom_config.get('enabled', False):
                logger.debug("Stale pending order management is disabled")
                return stats

            # Get config values
            max_pending_hours = pom_config.get('max_pending_hours', 2)
            price_cancel_enabled = pom_config.get('price_distance_cancel', {}).get('enabled', True)
            price_threshold_pct = pom_config.get('price_distance_cancel', {}).get('threshold_pct', 2.0)

            # Get all pending orders for this bot
            pending_orders = session.query(OpenOrder).filter_by(
                status=OrderStatus.PENDING,
                bot_instance_id=config.get_bot_instance_id()
            ).all()

            stats['pending_checked'] = len(pending_orders)

            if not pending_orders:
                logger.debug("No pending orders to check for staleness")
                return stats

            logger.info(f"Checking {len(pending_orders)} pending orders for staleness...")

            current_time = ist_now_naive()

            for order in pending_orders:
                should_cancel = False
                cancel_reason = ""
                cancel_type = ""

                order_key = f"{order.script} {order.timeframe}"

                try:
                    # Check 1: Time-based staleness
                    hours_pending = (current_time - order.placed_at).total_seconds() / 3600

                    if hours_pending > max_pending_hours:
                        should_cancel = True
                        cancel_reason = f"Pending {hours_pending:.1f}h (max: {max_pending_hours}h)"
                        cancel_type = "time"
                        logger.info(f"  {order_key}: STALE (time) - {cancel_reason}")

                    # Check 2: Price-based staleness (only if not already flagged by time)
                    if not should_cancel and price_cancel_enabled and order.order_price:
                        try:
                            # Add delay before LTP fetch to respect API rate limits
                            import time
                            time.sleep(1.0)  # 1 second delay between LTP checks

                            # Get instrument token first, then fetch LTP
                            instrument_token = self.kite_client.get_instrument_token(order.script)
                            current_ltp = self.kite_client.get_instrument_ltp(instrument_token)

                            if current_ltp and current_ltp > 0:
                                # Calculate distance: how far LTP is above limit price
                                distance_pct = ((current_ltp - order.order_price) / order.order_price) * 100

                                if distance_pct > price_threshold_pct:
                                    should_cancel = True
                                    cancel_reason = f"LTP Rs.{current_ltp:.2f} is {distance_pct:.1f}% above order @ Rs.{order.order_price:.2f} (threshold: {price_threshold_pct}%)"
                                    cancel_type = "price"
                                    logger.info(f"  {order_key}: STALE (price) - {cancel_reason}")
                                else:
                                    logger.debug(
                                        f"  {order_key}: OK - LTP Rs.{current_ltp:.2f} "
                                        f"({distance_pct:.1f}% from order), pending {hours_pending:.1f}h"
                                    )

                        except Exception as e:
                            logger.warning(f"  {order_key}: Failed to get LTP for price check: {e}")

                    # Cancel if stale
                    if should_cancel:
                        result = self._cancel_stale_order(order, cancel_reason, session)

                        if result['success']:
                            if cancel_type == "time":
                                stats['stale_by_time'] += 1
                            else:
                                stats['stale_by_price'] += 1

                            stats['capital_released'] += result['capital_released']
                            stats['cancelled_orders'].append({
                                'script': order.script,
                                'timeframe': order.timeframe,
                                'reason': cancel_reason,
                                'capital': result['capital_released']
                            })

                            # Send compact Telegram alert
                            self._send_stale_order_alert(order, cancel_reason, result['capital_released'], cancel_type)

                        else:
                            stats['errors'].append(f"{order_key}: {result.get('error', 'Unknown error')}")

                except Exception as e:
                    logger.error(f"Error checking order {order.order_id}: {e}")
                    stats['errors'].append(f"{order_key}: {str(e)}")

            # Log summary
            total_cancelled = stats['stale_by_time'] + stats['stale_by_price']
            if total_cancelled > 0:
                logger.info(
                    f"Stale order cleanup: Checked={stats['pending_checked']}, "
                    f"Cancelled={total_cancelled} (Time={stats['stale_by_time']}, Price={stats['stale_by_price']}), "
                    f"Capital released=Rs.{stats['capital_released']:.2f}"
                )
            else:
                logger.debug(f"Stale order check: {stats['pending_checked']} orders checked, none stale")

            return stats

        except Exception as e:
            logger.error(f"Error in manage_stale_pending_orders: {e}")
            stats['errors'].append(str(e))
            if close_session:
                session.rollback()
            return stats
        finally:
            if close_session:
                session.close()

    def _cancel_stale_order(self, order: OpenOrder, reason: str, session: Session) -> Dict:
        """
        Cancel a single stale pending order

        Args:
            order: OpenOrder to cancel
            reason: Cancellation reason
            session: Database session

        Returns:
            Dict with success status and details
        """
        try:
            order_key = f"{order.script} {order.timeframe}"
            logger.info(f"Cancelling stale order: {order_key} - {reason}")

            # Get order variety from Zerodha
            order_details = self.kite_client.get_order_status(order.order_id)
            order_variety = order_details.get('variety', 'regular')
            zerodha_status = order_details.get('status', '')

            # Check if already cancelled/rejected on Zerodha
            if zerodha_status in [KiteOrderStatus.CANCELLED, KiteOrderStatus.REJECTED]:
                logger.info(f"  Order already {zerodha_status} on Zerodha - syncing status")
                order.status = OrderStatus.CANCELLED
                order.rejection_reason = f"Stale order (already {zerodha_status}): {reason}"
                self._update_processed_signal_expired(order, session)
                session.commit()
                return {'success': True, 'capital_released': order.capital_deployed}

            # Cancel on Zerodha if still open
            if zerodha_status == KiteOrderStatus.OPEN:
                try:
                    cancelled = self.kite_client.cancel_order(order.order_id, variety=order_variety)
                    if cancelled:
                        logger.info(f"  Order cancelled and verified on Zerodha")
                    else:
                        logger.warning(f"  Order could not be cancelled (may be filled/rejected)")
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"  Failed to cancel on Zerodha: {error_msg}")
                    # Send alert if verification failed (order still OPEN)
                    if "cancellation failed at exchange" in error_msg.lower():
                        alert_msg = (
                            f"🚨 STALE ORDER CANCEL FAILED\n\n"
                            f"Order: {order.order_id}\n"
                            f"Stock: {order.script} ({order.timeframe})\n"
                            f"Reason: {reason}\n\n"
                            f"⚠️ Order may still be OPEN. Cancel manually."
                        )
                        try:
                            telegram.send_alert(alert_msg, critical=True)
                        except Exception:
                            pass
                    return {'success': False, 'error': error_msg}

            # Update database
            capital_released = order.capital_deployed
            order.status = OrderStatus.CANCELLED
            order.rejection_reason = f"Auto-cancelled (stale): {reason}"

            # Update ProcessedSignal
            self._update_processed_signal_expired(order, session)

            # Release capital (update tracking)
            self._release_order_capital(order, 0, session)

            session.commit()

            logger.info(f"  Stale order cancelled: {order_key}, Capital released: Rs.{capital_released:.2f}")
            return {'success': True, 'capital_released': capital_released}

        except Exception as e:
            logger.error(f"Failed to cancel stale order {order.order_id}: {e}")
            session.rollback()
            return {'success': False, 'error': str(e)}

    def _send_stale_order_alert(self, order: OpenOrder, reason: str, capital_released: float, cancel_type: str):
        """Send compact Telegram alert for stale order cancellation"""
        try:
            # Short, compact message
            icon = "⏰" if cancel_type == "time" else "📈"
            type_text = "Time" if cancel_type == "time" else "Price"

            message = (
                f"{icon} **Stale Order Cancelled**\n"
                f"📊 {order.script} ({order.timeframe})\n"
                f"❌ {type_text}: {reason}\n"
                f"💰 Rs.{capital_released:,.0f} freed"
            )

            telegram.send_alert(message, critical=False)

        except Exception as e:
            logger.warning(f"Failed to send stale order alert: {e}")

    def cancel_unfulfilled_orders(self, session: Optional[Session] = None) -> Dict[str, any]:
        """
        Cancel all pending orders after market close (3:30 PM)

        This function:
        1. Finds all PENDING orders for this bot
        2. Checks their current status on Zerodha
        3. Handles partial fills (creates position for filled quantity)
        4. Cancels remaining unfulfilled orders on Zerodha
        5. Updates database status to CANCELLED
        6. Releases reserved capital
        7. Updates ProcessedSignal status to ORDER_EXPIRED
        8. Sends summary Telegram alert

        Returns:
            Stats dict with cancellation results
        """
        close_session = False
        if session is None:
            session = get_session()
            close_session = True

        stats = {
            'pending_found': 0,
            'already_filled': 0,
            'already_cancelled': 0,
            'partial_fills': 0,
            'cancelled': 0,
            'cancel_failed': 0,
            'capital_released': 0.0,
            'errors': []
        }

        try:
            logger.info("=" * 60)
            logger.info("EOD ORDER CLEANUP: Cancelling unfulfilled orders after market close")
            logger.info("=" * 60)

            # Get all pending orders for this bot
            pending_orders = session.query(OpenOrder).filter_by(
                status=OrderStatus.PENDING,
                bot_instance_id=config.get_bot_instance_id()
            ).all()

            stats['pending_found'] = len(pending_orders)

            if not pending_orders:
                logger.info("No pending orders to cancel")
                return stats

            logger.info(f"Found {len(pending_orders)} pending orders to process")

            for order in pending_orders:
                try:
                    result = self._process_unfulfilled_order(order, session)

                    if result['status'] == 'FILLED':
                        stats['already_filled'] += 1
                    elif result['status'] == 'ALREADY_CANCELLED':
                        stats['already_cancelled'] += 1
                    elif result['status'] == 'PARTIAL_FILL':
                        stats['partial_fills'] += 1
                        stats['capital_released'] += result.get('capital_released', 0)
                    elif result['status'] == 'CANCELLED':
                        stats['cancelled'] += 1
                        stats['capital_released'] += result.get('capital_released', 0)
                    elif result['status'] == 'CANCEL_FAILED':
                        stats['cancel_failed'] += 1
                        stats['errors'].append(result.get('error', 'Unknown error'))

                except Exception as e:
                    logger.error(f"Error processing order {order.order_id}: {e}")
                    stats['errors'].append(f"{order.script}: {str(e)}")

            # Log summary
            logger.info(
                f"EOD Order Cleanup Complete: "
                f"Found={stats['pending_found']}, Cancelled={stats['cancelled']}, "
                f"PartialFills={stats['partial_fills']}, AlreadyFilled={stats['already_filled']}, "
                f"Failed={stats['cancel_failed']}, CapitalReleased=Rs.{stats['capital_released']:.2f}"
            )

            # Send Telegram summary if any orders were processed
            if stats['cancelled'] > 0 or stats['partial_fills'] > 0:
                self._send_eod_cleanup_alert(stats)

            return stats

        except Exception as e:
            logger.error(f"Error in cancel_unfulfilled_orders: {e}")
            stats['errors'].append(str(e))
            if close_session:
                session.rollback()
            return stats
        finally:
            if close_session:
                session.close()

    def _process_unfulfilled_order(self, order: OpenOrder, session: Session) -> Dict:
        """
        Process a single unfulfilled order

        Args:
            order: OpenOrder to process
            session: Database session

        Returns:
            Dict with status and details
        """
        order_key = f"{order.script} {order.timeframe} (Order: {order.order_id})"
        logger.info(f"Processing unfulfilled order: {order_key}")

        try:
            # Step 1: Get current status from Zerodha
            order_details = self.kite_client.get_order_status(order.order_id)
            zerodha_status = order_details.get('status', '')
            filled_qty = order_details.get('filled_quantity', 0)
            avg_price = order_details.get('average_price', 0)
            order_variety = order_details.get('variety', 'regular')
            zerodha_symbol = order_details.get('tradingsymbol', order.script)

            logger.info(
                f"  Zerodha status: {zerodha_status}, Filled: {filled_qty}/{order.quantity}, "
                f"Variety: {order_variety}"
            )

            # Step 2: Check if already completed on Zerodha
            if zerodha_status == KiteOrderStatus.COMPLETE:
                # Order fully filled - handle as fill
                logger.info(f"  Order already COMPLETE on Zerodha - handling as fill")
                if not order.position_created:
                    self._handle_order_fill(order, avg_price, filled_qty, zerodha_symbol, session)
                order.status = OrderStatus.FILLED
                session.commit()
                return {'status': 'FILLED'}

            if zerodha_status == KiteOrderStatus.CANCELLED:
                # Already cancelled on Zerodha - sync status
                logger.info(f"  Order already CANCELLED on Zerodha - syncing status")
                order.status = OrderStatus.CANCELLED
                order.rejection_reason = "Cancelled on Zerodha (EOD sync)"
                self._update_processed_signal_expired(order, session)
                self._release_order_capital(order, 0, session)  # Release full capital
                session.commit()
                return {'status': 'ALREADY_CANCELLED', 'capital_released': order.capital_deployed}

            if zerodha_status == KiteOrderStatus.REJECTED:
                # Rejected on Zerodha - sync status
                logger.info(f"  Order REJECTED on Zerodha - syncing status")
                order.status = OrderStatus.REJECTED
                order.rejection_reason = order_details.get('status_message', 'Rejected by exchange')
                self._update_processed_signal_expired(order, session)
                self._release_order_capital(order, 0, session)
                session.commit()
                return {'status': 'ALREADY_CANCELLED', 'capital_released': order.capital_deployed}

            # Step 3: Handle partial fills
            if filled_qty > 0 and filled_qty < order.quantity:
                logger.info(f"  Partial fill detected: {filled_qty}/{order.quantity} shares")

                # Create position for filled quantity if not already created
                if not order.position_created:
                    self._handle_order_fill(order, avg_price, filled_qty, zerodha_symbol, session)

                # Calculate capital for unfilled portion
                unfilled_qty = order.quantity - filled_qty
                capital_for_unfilled = (order.capital_deployed / order.quantity) * unfilled_qty

                # Cancel remaining on Zerodha (only if still OPEN)
                if zerodha_status == KiteOrderStatus.OPEN:
                    try:
                        cancelled = self.kite_client.cancel_order(order.order_id, variety=order_variety)
                        if cancelled:
                            logger.info(f"  Cancelled remaining {unfilled_qty} shares on Zerodha")
                        else:
                            logger.warning(f"  Order could not be cancelled (may be filled/rejected)")
                    except Exception as e:
                        error_msg = str(e)
                        logger.warning(f"  Failed to cancel on Zerodha: {error_msg}")
                        # For partial fills, still proceed (position created is the priority)
                        if "cancellation failed at exchange" in error_msg.lower():
                            alert_msg = (
                                f"⚠️ PARTIAL FILL - Cancel Failed\n\n"
                                f"Order: {order.order_id}\n"
                                f"Stock: {order.script}\n"
                                f"Filled: {filled_qty}/{order.quantity}\n\n"
                                f"Remaining qty may still be OPEN. Check manually."
                            )
                            try:
                                telegram.send_alert(alert_msg, critical=True)
                            except Exception:
                                pass

                # Update order status
                order.status = OrderStatus.FILLED  # Position was created for filled portion
                order.rejection_reason = f"Partial fill at market close ({filled_qty}/{order.quantity})"

                # Release capital for unfilled portion only
                self._release_order_capital(order, filled_qty, session)

                session.commit()
                return {'status': 'PARTIAL_FILL', 'capital_released': capital_for_unfilled}

            # Step 4: Fully unfulfilled order - cancel it
            if zerodha_status == KiteOrderStatus.OPEN:
                logger.info(f"  Cancelling unfulfilled order on Zerodha...")

                # Skip AMO orders (they're for next day)
                if order_variety == 'amo':
                    logger.info(f"  Skipping AMO order (for next trading day)")
                    return {'status': 'SKIPPED_AMO'}

                try:
                    # cancel_order now includes verification (verify=True by default)
                    cancelled = self.kite_client.cancel_order(order.order_id, variety=order_variety)
                    if cancelled:
                        logger.info(f"  Order cancelled and verified on Zerodha")
                    else:
                        # Order was already filled/rejected - not actually cancelled
                        logger.warning(f"  Order could not be cancelled (may be filled/rejected)")
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"  Failed to cancel order on Zerodha: {error_msg}")

                    # Check if this is a verification failure (order still OPEN after cancel request)
                    if "cancellation failed at exchange" in error_msg.lower():
                        # Send critical Telegram alert for manual intervention
                        alert_msg = (
                            f"🚨 ORDER CANCELLATION FAILED - MANUAL ACTION REQUIRED\n\n"
                            f"Order: {order.order_id}\n"
                            f"Stock: {order.script} ({order.timeframe})\n"
                            f"Qty: {order.quantity} @ Rs.{order.order_price:.2f}\n\n"
                            f"Error: {error_msg}\n\n"
                            f"⚠️ Order may still be OPEN on Zerodha. Please cancel manually."
                        )
                        try:
                            telegram.send_alert(alert_msg, critical=True)
                        except Exception as tg_err:
                            logger.error(f"  Failed to send Telegram alert: {tg_err}")

                    return {'status': 'CANCEL_FAILED', 'error': error_msg}

            # Update database
            order.status = OrderStatus.CANCELLED
            order.rejection_reason = "Market close - order unfulfilled"

            # Update ProcessedSignal status
            self._update_processed_signal_expired(order, session)

            # Release full capital
            capital_released = order.capital_deployed
            self._release_order_capital(order, 0, session)

            session.commit()

            logger.info(f"  Order cancelled, capital released: Rs.{capital_released:.2f}")
            return {'status': 'CANCELLED', 'capital_released': capital_released}

        except Exception as e:
            logger.error(f"  Error processing order {order.order_id}: {e}")
            session.rollback()
            return {'status': 'CANCEL_FAILED', 'error': str(e)}

    def _update_processed_signal_expired(self, order: OpenOrder, session: Session):
        """Update ProcessedSignal status to ORDER_EXPIRED for cancelled orders"""
        try:
            # Find the ProcessedSignal that created this order
            signal = session.query(ProcessedSignal).filter_by(
                order_id=order.order_id,
                bot_instance_id=config.get_bot_instance_id()
            ).first()

            if signal:
                signal.processing_status = 'ORDER_EXPIRED'
                signal.last_error = "Order cancelled at market close - unfulfilled"
                logger.debug(f"Updated ProcessedSignal {signal.id} to ORDER_EXPIRED")
            else:
                logger.debug(f"No ProcessedSignal found for order {order.order_id}")

        except Exception as e:
            logger.warning(f"Failed to update ProcessedSignal: {e}")

    def _release_order_capital(self, order: OpenOrder, filled_qty: int, session: Session):
        """
        Release capital reserved for a cancelled/partial order

        Args:
            order: The OpenOrder being cancelled
            filled_qty: Quantity that was filled (0 for full cancellation)
            session: Database session
        """
        try:
            # Calculate capital to release
            if filled_qty == 0:
                # Full cancellation - release all reserved capital
                capital_to_release = order.capital_deployed
            else:
                # Partial fill - release capital for unfilled portion
                unfilled_qty = order.quantity - filled_qty
                capital_to_release = (order.capital_deployed / order.quantity) * unfilled_qty

            # Note: Capital ledger tracks deployed capital from POSITIONS, not pending orders
            # Pending order capital is tracked separately via OpenOrder.capital_deployed
            # When order is cancelled, it's automatically excluded from capital calculations
            # because it's no longer in PENDING status

            logger.info(
                f"Capital released for cancelled order {order.script}: Rs.{capital_to_release:.2f} "
                f"(Filled: {filled_qty}/{order.quantity})"
            )

        except Exception as e:
            logger.warning(f"Error calculating released capital: {e}")

    def _send_eod_cleanup_alert(self, stats: Dict):
        """Send Telegram alert summarizing EOD order cleanup"""
        try:
            message = (
                f"⏰ **EOD Order Cleanup Complete**\n\n"
                f"📊 Pending orders found: {stats['pending_found']}\n"
            )

            if stats['cancelled'] > 0:
                message += f"❌ Cancelled (unfulfilled): {stats['cancelled']}\n"

            if stats['partial_fills'] > 0:
                message += f"⚠️ Partial fills (position created): {stats['partial_fills']}\n"

            if stats['already_filled'] > 0:
                message += f"✅ Already filled: {stats['already_filled']}\n"

            if stats['already_cancelled'] > 0:
                message += f"🔄 Already cancelled: {stats['already_cancelled']}\n"

            if stats['cancel_failed'] > 0:
                message += f"🔴 Cancel failed: {stats['cancel_failed']}\n"

            message += f"\n💰 Capital released: Rs.{stats['capital_released']:.2f}"

            if stats['errors']:
                message += f"\n\n⚠️ Errors: {len(stats['errors'])}"

            telegram.send_alert(message, critical=False)

        except Exception as e:
            logger.warning(f"Failed to send EOD cleanup alert: {e}")


# Singleton instance
order_monitor = OrderMonitor()
