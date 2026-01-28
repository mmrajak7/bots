"""
Exit Manager - Handles position exits and SL management

Responsibilities:
- Place protective SL GTT for new positions
- Trail SL to monthly LOW (last trading day only)
- Monitor for 30% drops and alert
- Handle emergency exits
- Process SL hits
- Verify GTT protection for all positions
"""

import re
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from loguru import logger

from src.api.dual_kite_client import get_kite_client
from src.models.database import (
    get_session, OpenPosition, ClosedPosition, GTTUpdateLog, CapitalLedger,
    PositionStatus, ist_now_naive
)
from src.telegram.bot import telegram
from src.utils.config_manager import config
from src.utils.timezone_helper import today_ist, now_ist
from src.utils.price_rounder import round_price_down, PriceRounder
from src.utils.cost_calculator import cost_calculator


# Constants for retry logic
GTT_RETRY_ATTEMPTS = 3
GTT_RETRY_BASE_DELAY = 2.0  # seconds


class ExitManager:
    """Manages position exits and SL for FIFTY bot"""

    def __init__(self):
        """Initialize exit manager"""
        self.kite = get_kite_client()

        logger.info("Exit manager initialized")

    def place_sl_gtt(
        self,
        position: OpenPosition,
        session=None,
        retry: bool = True
    ) -> Optional[str]:
        """
        Place protective SL GTT for a position with retry and verification.

        Args:
            position: OpenPosition record
            session: Optional existing session (for transaction integrity)
            retry: Whether to retry on failure

        Returns:
            GTT ID if successful and verified
        """
        own_session = session is None
        if own_session:
            session = get_session()

        try:
            script = position.script
            sl_price = position.current_sl
            quantity = position.quantity

            # Get instrument token
            instrument_token = self.kite.get_instrument_token(script)

            # Get current LTP
            try:
                ltp = self.kite.get_instrument_ltp(instrument_token)
            except Exception:
                ltp = position.entry_price

            # FIX TR-C1: Calculate limit price with buffer for gap-down protection
            # In a gap-down scenario, LIMIT at trigger price won't fill if stock opens lower
            # Use 2% buffer below trigger to increase fill probability
            sl_limit_buffer = config.get('risk.sl_gtt_limit_buffer_percent', 2.0) / 100
            sl_limit_price = round_price_down(sl_price * (1 - sl_limit_buffer))

            # Prepare GTT payload - Use LIMIT order (battle-tested in CROCODILE)
            # Trigger at sl_price, but limit order at sl_limit_price (2% lower)
            payload = {
                'type': 'single',
                'condition': {
                    'exchange': 'NSE',
                    'tradingsymbol': script,
                    'trigger_values': [sl_price],
                    'last_price': ltp
                },
                'orders': [{
                    'exchange': 'NSE',
                    'tradingsymbol': script,
                    'transaction_type': 'SELL',
                    'quantity': quantity,
                    'order_type': 'LIMIT',  # LIMIT order (matches CROCODILE)
                    'product': 'CNC',
                    'price': sl_limit_price  # FIX: 2% below trigger for gap-down protection
                }],
                'expires_at': self._get_gtt_expiry()
            }

            logger.debug(f"SL GTT for {script}: trigger={sl_price}, limit={sl_limit_price} ({sl_limit_buffer*100:.1f}% buffer)")

            # Place GTT with retry logic (includes "price too close" handling)
            gtt_id = None
            last_error = None
            max_attempts = GTT_RETRY_ATTEMPTS if retry else 1
            current_sl_price = sl_price  # May be adjusted if "too close" error

            for attempt in range(max_attempts):
                try:
                    # Update payload with current SL price (may have been adjusted)
                    # FIX TR-C3: Maintain gap-down buffer - limit price must be BELOW trigger
                    current_limit_price = round_price_down(current_sl_price * (1 - sl_limit_buffer))
                    payload['condition']['trigger_values'] = [current_sl_price]
                    payload['orders'][0]['price'] = current_limit_price

                    result = self.kite.place_gtt_order(payload)
                    gtt_id = str(result.get('trigger_id'))
                    logger.info(f"SL GTT placed for {script}: ID={gtt_id}, SL={current_sl_price} (attempt {attempt + 1})")
                    break
                except Exception as e:
                    last_error = e
                    error_str = str(e).lower()

                    # Check if error is "price too close to LTP" (Zerodha requires 0.25% gap)
                    price_too_close = (
                        "too close" in error_str or
                        "0.25%" in error_str or
                        "0.2%" in error_str
                    )

                    if price_too_close and attempt < max_attempts - 1:
                        # Apply 0.3% buffer (0.25% requirement + 0.05% margin)
                        current_sl_price = round_price_down(current_sl_price * 0.997)
                        logger.warning(
                            f"GTT rejected - trigger too close to LTP, retrying with buffer: "
                            f"SL={current_sl_price} (attempt {attempt + 1}/{max_attempts})"
                        )
                        time.sleep(1)  # Brief delay before retry
                        continue

                    # Check if error is tick size related (from CROCODILE)
                    if "tick size" in error_str and attempt < max_attempts - 1:
                        # Extract tick size from error message
                        tick_match = re.search(r'tick size.*?is\s+(0\.\d+)', str(e), re.IGNORECASE)

                        if tick_match:
                            required_tick = float(tick_match.group(1))
                            logger.warning(
                                f"SL GTT tick size error for {script}. "
                                f"Zerodha requires {required_tick}, retrying..."
                            )

                            # Re-round price with correct tick size
                            current_sl_price = PriceRounder.round_down_to_tick(
                                current_sl_price, tick_size=required_tick
                            )
                            logger.info(f"Corrected SL price for {script}: {current_sl_price}")
                            time.sleep(1)
                            continue

                    logger.warning(f"GTT placement attempt {attempt + 1}/{max_attempts} failed: {e}")
                    if attempt < max_attempts - 1:
                        delay = GTT_RETRY_BASE_DELAY * (2 ** attempt)
                        time.sleep(delay)

            if gtt_id is None:
                raise Exception(f"GTT placement failed after {max_attempts} attempts: {last_error}")

            # Verify GTT is actually active (TR-C1 fix)
            verified, verify_error = self._verify_gtt_active(gtt_id)

            # Update position (use adjusted price if "too close" retry occurred)
            position.gtt_id = gtt_id
            position.gtt_placed_at = ist_now_naive()
            position.gtt_verified = verified
            if current_sl_price != sl_price:
                # SL was adjusted due to "too close" error
                position.current_sl = current_sl_price
                logger.info(f"Position SL adjusted from {sl_price} to {current_sl_price} due to price proximity")

            if own_session:
                session.commit()

            # Log GTT update (use actual SL price placed)
            self._log_gtt_update(
                position_id=position.id,
                script=script,
                old_sl=None,
                new_sl=current_sl_price,  # Use actual placed SL
                old_gtt_id=None,
                new_gtt_id=gtt_id,
                status='SUCCESS' if verified else 'UNVERIFIED',
                reason='INITIAL'
            )

            if not verified:
                telegram.send_alert(
                    f"WARNING: SL GTT for {script} placed but NOT VERIFIED\n"
                    f"GTT ID: {gtt_id}\n"
                    f"Error: {verify_error}\n"
                    f"Will retry verification next run.",
                    critical=True
                )

            return gtt_id

        except Exception as e:
            logger.error(f"Failed to place SL GTT for {position.script}: {e}")
            if own_session:
                session.rollback()
            telegram.send_alert(
                f"CRITICAL: Failed to place SL GTT for {position.script}\n"
                f"Position UNPROTECTED!\n"
                f"Error: {str(e)}",
                critical=True
            )
            return None
        finally:
            if own_session:
                session.close()

    def _verify_gtt_active(self, gtt_id: str, max_wait: float = 10.0) -> Tuple[bool, Optional[str]]:
        """
        Verify that a GTT is actually active at Zerodha.

        FIX SYS-M2: Increased wait time and added retry with backoff.

        Args:
            gtt_id: GTT ID to verify
            max_wait: Maximum seconds to wait for verification (default 10s)

        Returns:
            (is_verified, error_message)
        """
        # FIX SYS-M2: Multiple verification attempts with increasing delays
        # GTT registration can take 2-5 seconds at Zerodha
        verification_delays = [2.0, 3.0, 5.0]  # Total: 10 seconds max

        for attempt, delay in enumerate(verification_delays):
            try:
                time.sleep(delay)

                # Query GTT status
                gtt_status = self.kite.get_gtt_status(gtt_id)

                if gtt_status is None:
                    if attempt < len(verification_delays) - 1:
                        logger.debug(f"GTT {gtt_id} not found yet, retrying... (attempt {attempt + 1})")
                        continue
                    return False, f"GTT {gtt_id} not found in active GTT list after {max_wait}s"

                status = gtt_status.get('status', '')

                if status == 'active':
                    logger.info(f"GTT {gtt_id} verified as active (attempt {attempt + 1})")
                    return True, None
                elif status in ('triggered', 'cancelled', 'rejected', 'disabled'):
                    return False, f"GTT {gtt_id} has unexpected status: {status}"
                else:
                    # Unknown status - retry if we have attempts left
                    if attempt < len(verification_delays) - 1:
                        logger.debug(f"GTT {gtt_id} has status '{status}', retrying...")
                        continue
                    return False, f"GTT {gtt_id} has unknown status: {status}"

            except Exception as e:
                if attempt < len(verification_delays) - 1:
                    logger.debug(f"GTT verification error, retrying: {e}")
                    continue
                logger.warning(f"GTT verification failed for {gtt_id}: {e}")
                return False, str(e)

        return False, f"GTT {gtt_id} verification timeout after {max_wait}s"

    def verify_all_gtt_protection(self) -> List[Dict[str, Any]]:
        """
        Verify all open positions have active SL GTT protection.
        Called during recovery checks. (TR-C2 fix)

        Returns:
            List of unprotected positions with details
        """
        unprotected = []
        session = get_session()

        try:
            positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()

            if not positions:
                return []

            # Get all active GTTs from Zerodha
            try:
                active_gtts = self.kite.get_gtt_orders()
                active_gtt_ids = {str(g.get('id')) for g in active_gtts if g.get('status') == 'active'}
            except Exception as e:
                logger.error(f"Failed to fetch GTT list for verification: {e}")
                telegram.send_alert(
                    f"CRITICAL: Cannot verify GTT protection - API error: {e}",
                    critical=True
                )
                return []

            for position in positions:
                gtt_id = position.gtt_id
                is_protected = False
                issue = None

                if not gtt_id:
                    issue = "No GTT ID recorded"
                elif gtt_id not in active_gtt_ids:
                    issue = f"GTT {gtt_id} not found in active GTTs"
                else:
                    is_protected = True
                    # Update verified status
                    if not position.gtt_verified:
                        position.gtt_verified = True
                        logger.info(f"{position.script}: GTT {gtt_id} now verified")

                if not is_protected:
                    unprotected.append({
                        'position_id': position.id,
                        'script': position.script,
                        'quantity': position.quantity,
                        'current_sl': position.current_sl,
                        'gtt_id': gtt_id,
                        'issue': issue
                    })
                    logger.warning(f"{position.script}: UNPROTECTED - {issue}")

            session.commit()

            # Attempt to recover unprotected positions
            for pos_info in unprotected:
                self._attempt_gtt_recovery(pos_info, session)

            return unprotected

        except Exception as e:
            logger.error(f"Error verifying GTT protection: {e}")
            session.rollback()
            return []
        finally:
            session.close()

    def _attempt_gtt_recovery(self, pos_info: Dict[str, Any], session) -> bool:
        """
        Attempt to recover GTT protection for an unprotected position.

        Args:
            pos_info: Position info dict
            session: Database session

        Returns:
            True if recovery successful
        """
        try:
            position = session.query(OpenPosition).filter(
                OpenPosition.id == pos_info['position_id']
            ).first()

            if not position or position.status != PositionStatus.OPEN:
                return False

            logger.info(f"Attempting GTT recovery for {position.script}")

            # Place new SL GTT
            new_gtt_id = self.place_sl_gtt(position, session=session, retry=True)

            if new_gtt_id:
                telegram.send_alert(
                    f"GTT RECOVERED: {position.script}\n"
                    f"New GTT ID: {new_gtt_id}\n"
                    f"SL: {position.current_sl:,.2f}"
                )
                return True
            else:
                telegram.send_alert(
                    f"CRITICAL: GTT RECOVERY FAILED for {position.script}\n"
                    f"Position remains UNPROTECTED!\n"
                    f"Manual intervention required.",
                    critical=True
                )
                return False

        except Exception as e:
            logger.error(f"GTT recovery failed for position {pos_info['position_id']}: {e}")
            return False

    def update_monthly_trailing_sl(self) -> List[Dict[str, Any]]:
        """
        Trail SL to monthly LOW for all positions

        Called on last trading day of month AFTER 3:30 PM (market close).

        Returns:
            List of updated positions
        """
        updates = []
        session = get_session()

        try:
            # FIX TR-C2: Verify we're past market close to ensure monthly candle is complete
            current_time = now_ist()
            market_close_time = current_time.replace(hour=15, minute=30, second=0, microsecond=0)

            if current_time < market_close_time:
                logger.warning(
                    "Monthly SL trail called before market close - monthly candle incomplete. "
                    "Skipping to avoid using intraday LOW."
                )
                return updates

            positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()

            for position in positions:
                try:
                    # FIX SYS-C1: Pass session to maintain transaction atomicity
                    update = self._trail_to_monthly_low(position, session=session)
                    if update:
                        updates.append(update)
                except Exception as e:
                    logger.error(f"Failed to trail SL for {position.script}: {e}")

            # Commit all updates in single transaction
            session.commit()

        except Exception as e:
            logger.error(f"Error in monthly SL trail: {e}")
            session.rollback()
        finally:
            session.close()

        return updates

    def _trail_to_monthly_low(
        self,
        position: OpenPosition,
        session=None
    ) -> Optional[Dict[str, Any]]:
        """
        Trail position SL to monthly LOW.

        Args:
            position: OpenPosition to update
            session: Existing DB session (for transaction atomicity)
        """
        # FIX SYS-C1: Use passed session for atomicity, only create if not provided
        own_session = session is None
        if own_session:
            session = get_session()

        try:
            script = position.script
            current_sl = position.current_sl

            # Get instrument token
            instrument_token = self.kite.get_instrument_token(script)

            # Get this month's data
            monthly_df = self.kite.get_historical_data_sampled(
                instrument_token=instrument_token,
                timeframe='monthly',
                years_back=0.5
            )

            if monthly_df is None or monthly_df.empty:
                logger.warning(f"No monthly data for {script}")
                return None

            # Get this month's LOW
            monthly_low = float(monthly_df['Low'].iloc[-1])
            new_sl = round_price_down(monthly_low)

            # Only trail upward (SL can only tighten)
            if new_sl <= current_sl:
                logger.debug(f"{script}: Monthly LOW {monthly_low:.2f} <= current SL {current_sl:.2f}, no update")
                return None

            logger.info(f"{script}: Trailing SL from {current_sl:.2f} to {new_sl:.2f}")

            # Cancel old GTT
            old_gtt_id = position.gtt_id
            if old_gtt_id:
                try:
                    self.kite.cancel_gtt_order(old_gtt_id)
                except Exception as e:
                    logger.warning(f"Failed to cancel old GTT {old_gtt_id}: {e}")

            # Place new GTT - FIX SYS-C1: Pass session for atomicity
            position.current_sl = new_sl
            new_gtt_id = self.place_sl_gtt(position, session=session)

            if new_gtt_id:
                # Update position (session commit handled by caller if own_session=False)
                position.highest_sl = max(position.highest_sl or 0, new_sl)
                position.sl_movements += 1
                position.last_sl_update = ist_now_naive()

                if own_session:
                    session.commit()

                # Log update
                self._log_gtt_update(
                    position_id=position.id,
                    script=script,
                    old_sl=current_sl,
                    new_sl=new_sl,
                    old_gtt_id=old_gtt_id,
                    new_gtt_id=new_gtt_id,
                    status='SUCCESS',
                    reason='MONTHLY_TRAIL'
                )

                return {
                    'script': script,
                    'old_sl': current_sl,
                    'new_sl': new_sl,
                    'monthly_low': monthly_low
                }

            return None

        except Exception as e:
            logger.error(f"Error trailing SL for {position.script}: {e}")
            if own_session:
                session.rollback()
            return None
        finally:
            if own_session:
                session.close()

    def check_for_drops(self) -> List[Dict[str, Any]]:
        """
        Monitor positions for 30% drops

        Returns:
            List of positions with alerts sent
        """
        alerts = []
        drop_threshold = config.get('risk.emergency_drop_percent', 30)

        session = get_session()
        try:
            positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN,
                OpenPosition.drop_alert_sent == False
            ).all()

            if not positions:
                return alerts

            # FIX ERR-H1: Track API failures to detect systemic issues
            api_failures = 0
            api_successes = 0

            for position in positions:
                try:
                    # Get current LTP
                    instrument_token = self.kite.get_instrument_token(position.script)
                    if not instrument_token:
                        logger.warning(f"No instrument token for {position.script}")
                        api_failures += 1
                        continue

                    ltp = self.kite.get_instrument_ltp(instrument_token)
                    if ltp is None or ltp <= 0:
                        logger.warning(f"Invalid LTP for {position.script}: {ltp}")
                        api_failures += 1
                        continue

                    api_successes += 1

                    # Calculate drop
                    entry_price = position.entry_price
                    drop_pct = ((entry_price - ltp) / entry_price) * 100

                    if drop_pct >= drop_threshold:
                        # Send alert
                        msg_id = telegram.send_drop_alert(
                            position_id=position.id,
                            script=position.script,
                            entry_price=entry_price,
                            current_price=ltp,
                            drop_percent=drop_pct
                        )

                        if msg_id:
                            position.drop_alert_sent = True
                            position.drop_alert_date = today_ist()
                            session.commit()

                            alerts.append({
                                'script': position.script,
                                'drop_pct': drop_pct,
                                'ltp': ltp
                            })

                            logger.warning(f"{position.script}: 30% drop alert sent (drop={drop_pct:.1f}%)")

                except Exception as e:
                    logger.error(f"Error checking drop for {position.script}: {e}")
                    api_failures += 1

            # FIX ERR-H1: Alert if ALL API calls failed (systemic issue)
            if api_failures > 0 and api_successes == 0:
                logger.error(f"ALL position drop checks failed ({api_failures} failures)")
                telegram.send_alert(
                    f"<b>WARNING: Position Monitoring Failed</b>\n\n"
                    f"Could not check {api_failures} positions for drops.\n"
                    f"API may be down. Check manually!",
                    critical=True
                )

        finally:
            session.close()

        return alerts

    def emergency_exit(self, position_id: int) -> bool:
        """
        Execute emergency market exit for a position

        Args:
            position_id: Position to exit

        Returns:
            True if successful
        """
        session = get_session()
        try:
            position = session.query(OpenPosition).filter(
                OpenPosition.id == position_id,
                OpenPosition.status == PositionStatus.OPEN
            ).first()

            if not position:
                logger.error(f"Position {position_id} not found or not open")
                return False

            script = position.script
            quantity = position.quantity

            logger.warning(f"Executing emergency exit: {script} x {quantity}")

            # Cancel SL GTT first
            if position.gtt_id:
                try:
                    self.kite.cancel_gtt_order(position.gtt_id)
                except Exception as e:
                    logger.warning(f"Failed to cancel SL GTT: {e}")

            # FIX SAFE-2: Verify order placement before proceeding
            # Place market sell order
            try:
                result = self.kite.place_order(
                    tradingsymbol=script,
                    exchange='NSE',
                    transaction_type='SELL',
                    quantity=quantity,
                    order_type='MARKET',
                    product='CNC'
                )
            except Exception as e:
                logger.error(f"Emergency exit order placement failed: {e}")
                telegram.send_alert(
                    f"<b>EMERGENCY EXIT ORDER FAILED</b>\n\n"
                    f"{script}: {str(e)}\n"
                    f"Position still has {quantity} shares!\n"
                    f"MANUAL ACTION REQUIRED!",
                    critical=True
                )
                return False

            # FIX SAFE-2: Verify we got a valid order_id
            order_id = result.get('order_id') if isinstance(result, dict) else None
            if not order_id:
                logger.error(f"Emergency exit returned no order_id: {result}")
                telegram.send_alert(
                    f"<b>EMERGENCY EXIT FAILED - NO ORDER ID</b>\n\n"
                    f"{script}\n"
                    f"Response: {result}\n"
                    f"MANUAL ACTION REQUIRED!",
                    critical=True
                )
                return False

            logger.info(f"Emergency exit order placed: {order_id}")

            # Wait and check for fill with retry
            import time
            max_checks = 5
            for check in range(max_checks):
                time.sleep(2)
                try:
                    order_status = self.kite.get_order_status(order_id)
                    status = order_status.get('status', 'UNKNOWN')

                    if status == 'COMPLETE':
                        exit_price = float(order_status.get('average_price', 0))

                        # Close position
                        self._close_position(position, exit_price, 'EMERGENCY_EXIT')

                        telegram.send_alert(
                            f"<b>Emergency Exit Complete</b>\n\n"
                            f"{script} @ {exit_price:,.2f}\n"
                            f"Qty: {quantity}"
                        )
                        return True

                    elif status in ['REJECTED', 'CANCELLED']:
                        reason = order_status.get('status_message', 'Unknown')
                        telegram.send_alert(
                            f"<b>EMERGENCY EXIT REJECTED</b>\n\n"
                            f"{script}\n"
                            f"Reason: {reason}\n"
                            f"MANUAL ACTION REQUIRED!",
                            critical=True
                        )
                        return False

                    # Still pending, continue checking
                    logger.info(f"Emergency exit check {check+1}/{max_checks}: status={status}")

                except Exception as e:
                    logger.warning(f"Error checking order status: {e}")

            # Exhausted retries
            telegram.send_alert(
                f"<b>Emergency exit order pending</b>\n\n"
                f"{script}\n"
                f"Order ID: {order_id}\n"
                f"Check Zerodha manually!",
                critical=True
            )
            return False

        except Exception as e:
            logger.error(f"Emergency exit failed: {e}")
            session.rollback()
            telegram.send_alert(
                f"EMERGENCY EXIT FAILED: {e}",
                critical=True
            )
            return False
        finally:
            session.close()

    def check_sl_hits(self) -> List[Dict[str, Any]]:
        """
        Check if any SL GTTs have been triggered

        Returns:
            List of closed positions
        """
        closed = []
        session = get_session()

        try:
            positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN,
                OpenPosition.gtt_id.isnot(None)
            ).all()

            # Get all GTTs
            try:
                gtts = self.kite.get_gtt_orders()
            except Exception as e:
                logger.error(f"Failed to fetch GTTs: {e}")
                return []

            gtt_map = {str(g.get('id')): g for g in gtts}

            for position in positions:
                gtt_id = position.gtt_id
                gtt = gtt_map.get(gtt_id)

                if gtt is None:
                    # GTT not found - might have been triggered
                    closed_pos = self._check_if_sl_triggered(position)
                    if closed_pos:
                        closed.append(closed_pos)
                    continue

                gtt_status = gtt.get('status', '')

                if gtt_status == 'triggered':
                    orders = gtt.get('orders', [])
                    if orders:
                        gtt_order = orders[0]
                        order_result = gtt_order.get('result', {})
                        order_id = order_result.get('order_id')

                        if order_id:
                            closed_pos = self._process_sl_trigger(position, order_id)
                            if closed_pos:
                                closed.append(closed_pos)

        finally:
            session.close()

        return closed

    def _check_if_sl_triggered(self, position: OpenPosition) -> Optional[Dict[str, Any]]:
        """Check if SL was triggered by looking at regular orders"""
        try:
            all_orders = self.kite.get_all_orders()

            for order in all_orders:
                if (order.get('tradingsymbol') == position.script and
                    order.get('transaction_type') == 'SELL' and
                    order.get('status') == 'COMPLETE'):

                    exit_price = float(order.get('average_price', 0))

                    # Check if this looks like an SL exit
                    if exit_price <= position.current_sl * 1.02:  # Within 2% of SL
                        return self._close_position(position, exit_price, 'SL_HIT')

            return None

        except Exception as e:
            logger.error(f"Error checking SL trigger: {e}")
            return None

    def _process_sl_trigger(self, position: OpenPosition, order_id: str) -> Optional[Dict[str, Any]]:
        """Process SL GTT trigger"""
        try:
            order_status = self.kite.get_order_status(order_id)

            if order_status.get('status') != 'COMPLETE':
                return None

            exit_price = float(order_status.get('average_price', position.current_sl))

            return self._close_position(position, exit_price, 'SL_HIT')

        except Exception as e:
            logger.error(f"Error processing SL trigger: {e}")
            return None

    def _close_position(
        self,
        position: OpenPosition,
        exit_price: float,
        exit_reason: str
    ) -> Dict[str, Any]:
        """Close position and create historical record"""
        session = get_session()
        try:
            script = position.script
            entry_price = position.entry_price
            quantity = position.quantity
            entry_date = position.entry_date
            exit_date = today_ist()

            # Calculate P&L
            pnl_data = cost_calculator.calculate_pnl(entry_price, exit_price, quantity)

            # Days held
            days_held = (exit_date - entry_date).days

            # Create closed position record
            closed = ClosedPosition(
                script=script,
                entry_date=entry_date,
                entry_price=entry_price,
                quantity=quantity,
                capital_deployed=position.capital_deployed,
                exit_date=exit_date,
                exit_price=exit_price,
                exit_reason=exit_reason,
                gross_pnl=pnl_data['gross_pnl'],
                transaction_costs=pnl_data['total_cost'],
                net_pnl=pnl_data['net_pnl'],
                pnl_percent=pnl_data['pnl_percent'],
                days_held=days_held,
                sl_movements=position.sl_movements,
                highest_sl_achieved=position.highest_sl,
                closed_at=ist_now_naive()
            )
            session.add(closed)

            # Update position status
            position.status = PositionStatus.CLOSED_SL if exit_reason == 'SL_HIT' else PositionStatus.CLOSED_MANUAL
            position.exit_date = exit_date
            position.exit_price = exit_price
            position.exit_reason = exit_reason

            session.commit()

            # Update CapitalLedger with realized P&L
            self._update_capital_ledger_pnl(pnl_data['net_pnl'], position.capital_deployed)

            logger.info(f"Position closed: {script} @ {exit_price:.2f}, P&L: {pnl_data['net_pnl']:.2f}")

            # Send alert
            pnl_sign = "+" if pnl_data['net_pnl'] >= 0 else ""
            telegram.send_alert(
                f"<b>Position Closed - {exit_reason}</b>\n\n"
                f"{script}\n"
                f"Entry: {entry_price:,.2f} | Exit: {exit_price:,.2f}\n"
                f"P&L: {pnl_sign}{pnl_data['net_pnl']:,.0f} ({pnl_sign}{pnl_data['pnl_percent']:.1f}%)\n"
                f"Days: {days_held} | SL Moves: {position.sl_movements}"
            )

            return {
                'script': script,
                'exit_price': exit_price,
                'exit_reason': exit_reason,
                'net_pnl': pnl_data['net_pnl'],
                'pnl_percent': pnl_data['pnl_percent']
            }

        except Exception as e:
            logger.error(f"Error closing position: {e}")
            session.rollback()
            return None
        finally:
            session.close()

    def _log_gtt_update(
        self,
        position_id: int,
        script: str,
        old_sl: Optional[float],
        new_sl: float,
        old_gtt_id: Optional[str],
        new_gtt_id: Optional[str],
        status: str,
        reason: str,
        error: str = None
    ) -> None:
        """Log GTT update to audit trail"""
        session = get_session()
        try:
            log_entry = GTTUpdateLog(
                position_id=position_id,
                script=script,
                update_date=today_ist(),
                old_sl=old_sl,
                new_sl=new_sl,
                old_gtt_id=old_gtt_id,
                new_gtt_id=new_gtt_id,
                status=status,
                error_message=error,
                update_reason=reason,
                created_at=ist_now_naive()
            )
            session.add(log_entry)
            session.commit()
        except Exception as e:
            logger.error(f"Failed to log GTT update: {e}")
            session.rollback()
        finally:
            session.close()

    def _get_gtt_expiry(self) -> str:
        """Get GTT expiry date (1 year from now)"""
        expiry = now_ist() + timedelta(days=365)
        return expiry.strftime('%Y-%m-%d %H:%M:%S')

    def _update_capital_ledger_pnl(self, net_pnl: float, capital_freed: float) -> None:
        """
        Update CapitalLedger when a position is closed.

        Args:
            net_pnl: Net P&L from closed position
            capital_freed: Capital deployed that is now freed
        """
        session = get_session()
        try:
            today = today_ist()

            # Get or create today's ledger entry
            ledger = session.query(CapitalLedger).filter(
                CapitalLedger.date == today
            ).first()

            if ledger:
                # Update existing entry
                ledger.realized_pnl = (ledger.realized_pnl or 0) + net_pnl
                ledger.monthly_pnl = (ledger.monthly_pnl or 0) + net_pnl
                ledger.deployed_capital = max(0, (ledger.deployed_capital or 0) - capital_freed)
                ledger.free_capital = (ledger.free_capital or 0) + capital_freed + net_pnl
                ledger.updated_at = ist_now_naive()
            else:
                # Create new entry with P&L
                initial_capital = config.get('trading.initial_capital', 100000)
                ledger = CapitalLedger(
                    date=today,
                    opening_capital=initial_capital,
                    deployed_capital=0,
                    free_capital=initial_capital + net_pnl,
                    realized_pnl=net_pnl,
                    monthly_pnl=net_pnl,
                    num_open_positions=0,
                    num_pending_orders=0
                )
                session.add(ledger)

            session.commit()
            logger.debug(f"CapitalLedger updated: realized_pnl={ledger.realized_pnl:.2f}")

        except Exception as e:
            logger.error(f"Failed to update CapitalLedger: {e}")
            session.rollback()
        finally:
            session.close()


# Singleton instance
exit_manager = ExitManager()
