"""
Same-Day Recovery Workflow - Comprehensive End-of-Day Reconciliation

Runs at 4:00 PM (after market close 3:30 PM and EOD GTT update 3:50 PM)

This workflow acts as a SAFETY NET to catch all failures that occurred during the day:
- Orders that filled but GTT not placed
- Positions without GTT protection
- Database vs Zerodha state mismatches
- Orphan GTTs without positions
- Quantity mismatches

CRITICAL: This is the last line of defense before overnight/weekend risk.
"""

import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from datetime import date
from loguru import logger
from sqlalchemy.orm import Session

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.models.database import (
    OpenPosition, OpenOrder, PositionStatus, OrderStatus, get_session
)
from src.api.broker_factory import get_broker
from src.services.exit_manager import exit_manager
from src.services.entry_manager import entry_manager
from src.reporting.telegram_client import telegram
from src.utils.config_manager import config
from src.utils.timezone_helper import ist_now_naive, today_ist
from src.reporting.report_formatter import fmt


class SameDayRecovery:
    """
    Comprehensive end-of-day reconciliation to catch all failures

    Acts as safety net for:
    - Missed order fills
    - GTT placement failures
    - Database sync issues
    - Zerodha vs DB mismatches
    """

    def __init__(self):
        """Initialize recovery manager"""
        self.kite_client = get_broker()
        self.auto_fix_enabled = config.get('recovery.auto_fix_enabled', True)
        self.alert_threshold = config.get('recovery.alert_threshold', 'any')  # 'any' or 'critical_only'

    def run_recovery(self, session: Optional[Session] = None) -> Dict[str, any]:
        """
        Run comprehensive same-day recovery checks

        Returns:
            Recovery report with all findings and actions taken
        """
        close_session = False
        if session is None:
            session = get_session()
            close_session = True

        try:
            logger.info("=" * 80)
            logger.info("SAME-DAY RECOVERY - 4:00 PM CHECK")
            logger.info("=" * 80)

            recovery_report = {
                'timestamp': ist_now_naive(),
                'date': today_ist(),
                'checks_performed': [],
                'issues_found': [],
                'actions_taken': [],
                'critical_alerts': [],
                'warnings': [],
                'summary': {}
            }

            # Check 1: Position-GTT Reconciliation
            logger.info("\n[CHECK 1] Position-GTT Reconciliation...")
            position_gtt_results = self._check_position_gtt_reconciliation(session)
            recovery_report['checks_performed'].append('position_gtt_reconciliation')
            recovery_report['issues_found'].extend(position_gtt_results['issues'])
            recovery_report['actions_taken'].extend(position_gtt_results['actions'])
            recovery_report['critical_alerts'].extend(position_gtt_results['critical_alerts'])

            # Check 2: Missed Order Fills
            logger.info("\n[CHECK 2] Missed Order Fill Detection...")
            missed_fills_results = self._check_missed_order_fills(session)
            recovery_report['checks_performed'].append('missed_order_fills')
            recovery_report['issues_found'].extend(missed_fills_results['issues'])
            recovery_report['actions_taken'].extend(missed_fills_results['actions'])
            recovery_report['critical_alerts'].extend(missed_fills_results['critical_alerts'])

            # Check 3: Orphan GTT Detection
            logger.info("\n[CHECK 3] Orphan GTT Detection...")
            orphan_gtt_results = self._check_orphan_gtts(session)
            recovery_report['checks_performed'].append('orphan_gtt_detection')
            recovery_report['issues_found'].extend(orphan_gtt_results['issues'])
            recovery_report['actions_taken'].extend(orphan_gtt_results['actions'])
            recovery_report['warnings'].extend(orphan_gtt_results['warnings'])

            # Check 4: Quantity Mismatches
            logger.info("\n[CHECK 4] Position-GTT Quantity Verification...")
            quantity_results = self._check_quantity_mismatches(session)
            recovery_report['checks_performed'].append('quantity_verification')
            recovery_report['issues_found'].extend(quantity_results['issues'])
            recovery_report['warnings'].extend(quantity_results['warnings'])

            # Check 5: Orphan Order Detection (orders marked PENDING in DB but actually dead in Zerodha)
            logger.info("\n[CHECK 5] Orphan Order Detection...")
            orphan_order_results = self._check_orphan_orders(session)
            recovery_report['checks_performed'].append('orphan_order_detection')
            recovery_report['issues_found'].extend(orphan_order_results['issues'])
            recovery_report['actions_taken'].extend(orphan_order_results['actions'])
            recovery_report['warnings'].extend(orphan_order_results['warnings'])

            # Generate summary
            recovery_report['summary'] = {
                'total_checks': len(recovery_report['checks_performed']),
                'total_issues': len(recovery_report['issues_found']),
                'critical_issues': len(recovery_report['critical_alerts']),
                'warnings': len(recovery_report['warnings']),
                'actions_taken': len(recovery_report['actions_taken']),
                'auto_fix_enabled': self.auto_fix_enabled
            }

            # Send alerts if needed
            self._send_recovery_alerts(recovery_report)

            # Log summary
            self._log_recovery_summary(recovery_report)

            logger.info("=" * 80)
            logger.info("SAME-DAY RECOVERY COMPLETE")
            logger.info("=" * 80)

            return recovery_report

        except Exception as e:
            logger.error(f"Error during same-day recovery: {e}")
            telegram.send_alert(
                f"🚨 **SAME-DAY RECOVERY ERROR**\n"
                f"Recovery check failed at 4 PM\n"
                f"Error: {str(e)}\n"
                f"**Manual review required!**"
            )
            raise

        finally:
            if close_session:
                session.close()

    def _check_position_gtt_reconciliation(self, session: Session) -> Dict:
        """
        Check 1: Verify every open position has exactly 1 active GTT

        Catches:
        - Positions without GTT (CRITICAL - unprotected)
        - Positions with inactive GTT
        - GTT ID mismatch
        """
        results = {
            'issues': [],
            'actions': [],
            'critical_alerts': []
        }

        try:
            # Get all open positions from database (for this bot)
            open_positions = session.query(OpenPosition).filter_by(
                bot_instance_id=config.get_bot_instance_id(),
                status=PositionStatus.OPEN
            ).all()

            if not open_positions:
                logger.info("No open positions to check")
                return results

            logger.info(f"Checking {len(open_positions)} open positions...")

            # Get all active GTTs from Zerodha
            try:
                zerodha_gtts = self.kite_client.get_gtt_orders()
                logger.info(f"Found {len(zerodha_gtts)} active GTTs in Zerodha")
            except Exception as e:
                error_msg = f"Failed to fetch GTTs from Zerodha: {e}"
                logger.error(f"{error_msg}\nAPI Response: {getattr(e, 'response', 'No response available')}")
                results['critical_alerts'].append(error_msg)
                return results

            # Build GTT lookup dict: {gtt_id: gtt_data}
            gtt_lookup = {str(gtt.get('id')): gtt for gtt in zerodha_gtts}

            # Check each position
            for position in open_positions:
                position_key = f"{position.script} {position.timeframe}"

                # Check if position has GTT ID in database
                if not position.current_gtt_id:
                    issue = f"Position {position_key} has NO GTT ID in database"
                    logger.error(f"⚠️ {issue}")
                    results['issues'].append(issue)
                    results['critical_alerts'].append(
                        f"🚨 **UNPROTECTED POSITION**\n"
                        f"Script: {position_key}\n"
                        f"Entry: Rs.{position.entry_price:.2f}\n"
                        f"Qty: {position.quantity}\n"
                        f"**No GTT ID in database - Position unprotected!**"
                    )

                    # Auto-fix: Try to place GTT
                    if self.auto_fix_enabled:
                        action = self._auto_fix_missing_gtt(position, session)
                        results['actions'].append(action)

                    continue

                # Check if GTT exists in Zerodha
                if str(position.current_gtt_id) not in gtt_lookup:
                    issue = f"Position {position_key} GTT {position.current_gtt_id} NOT FOUND in Zerodha"
                    logger.error(f"⚠️ {issue}")
                    results['issues'].append(issue)
                    results['critical_alerts'].append(
                        f"🚨 **GTT NOT FOUND**\n"
                        f"Script: {position_key}\n"
                        f"GTT ID: {position.current_gtt_id}\n"
                        f"Current SL: Rs.{position.current_sl:.2f}\n"
                        f"**GTT missing in Zerodha - Position unprotected!**"
                    )

                    # Auto-fix: Try to place new GTT
                    if self.auto_fix_enabled:
                        action = self._auto_fix_missing_gtt(position, session)
                        results['actions'].append(action)

                    continue

                # GTT exists - verify status
                gtt = gtt_lookup[str(position.current_gtt_id)]
                gtt_status = gtt.get('status', 'unknown').lower()

                if gtt_status == 'triggered':
                    # GTT was executed (SL hit) - Position should be closed
                    logger.warning(
                        f"⚠️ GTT TRIGGERED: {position_key} GTT {position.current_gtt_id} - "
                        f"SL hit, position should be closed"
                    )

                    # Auto-fix: Close the position in database
                    if self.auto_fix_enabled:
                        action = self._auto_fix_triggered_gtt(position, gtt, session)
                        results['actions'].append(action)
                    else:
                        issue = f"Position {position_key} GTT triggered but position still open in DB"
                        results['issues'].append(issue)
                        results['critical_alerts'].append(
                            f"⚠️ **GTT TRIGGERED - POSITION NOT CLOSED**\n"
                            f"Script: {position_key}\n"
                            f"GTT ID: {position.current_gtt_id}\n"
                            f"**Position should be closed - manual action needed**"
                        )

                elif gtt_status != 'active':
                    # Other non-active statuses (cancelled, disabled, etc.)
                    issue = f"Position {position_key} GTT {position.current_gtt_id} status: {gtt_status} (not active)"
                    logger.warning(f"⚠️ {issue}")
                    results['issues'].append(issue)
                    results['critical_alerts'].append(
                        f"⚠️ **GTT INACTIVE**\n"
                        f"Script: {position_key}\n"
                        f"GTT ID: {position.current_gtt_id}\n"
                        f"Status: {gtt_status}\n"
                        f"**Manual review required**"
                    )
                else:
                    # GTT is active - now check expiry date
                    logger.info(f"✅ {position_key}: GTT {position.current_gtt_id} status verified (active)")

                    # === EXPIRY VALIDATION ===
                    try:
                        expires_at_str = gtt.get('expires_at')

                        if expires_at_str:
                            # Parse expiry date (Zerodha format: "YYYY-MM-DD HH:MM:SS")
                            from datetime import datetime
                            from src.utils.timezone_helper import now_ist

                            # Parse as IST timezone
                            expires_at = datetime.strptime(expires_at_str, "%Y-%m-%d %H:%M:%S")
                            current_time = now_ist().replace(tzinfo=None)  # Remove tz for comparison

                            # Calculate days until expiry
                            time_until_expiry = expires_at - current_time
                            days_until_expiry = time_until_expiry.days

                            # CRITICAL: GTT expiring in < 7 days
                            if days_until_expiry < 7:
                                issue = (
                                    f"Position {position_key} GTT expiring in {days_until_expiry} days "
                                    f"(expires: {expires_at_str})"
                                )
                                logger.error(f"🚨 {issue}")
                                results['issues'].append(issue)
                                results['critical_alerts'].append(
                                    f"🚨 **GTT EXPIRING SOON - CRITICAL**\n"
                                    f"Script: {position_key}\n"
                                    f"GTT ID: {position.current_gtt_id}\n"
                                    f"Expires: {expires_at_str}\n"
                                    f"Days remaining: {days_until_expiry}\n"
                                    f"**GTT needs renewal to maintain SL protection!**"
                                )

                                # Auto-fix: Renew GTT (cancel old, place new)
                                if self.auto_fix_enabled:
                                    action = self._auto_fix_expiring_gtt(position, gtt, session)
                                    results['actions'].append(action)

                            # WARNING: GTT expiring in < 30 days
                            elif days_until_expiry < 30:
                                issue = (
                                    f"Position {position_key} GTT expiring in {days_until_expiry} days "
                                    f"(expires: {expires_at_str})"
                                )
                                logger.warning(f"⚠️ {issue}")
                                results['issues'].append(issue)
                                # Don't add to critical_alerts for warning level (30-7 days)
                                # Just log it - no immediate action needed

                            else:
                                # GTT has sufficient validity
                                logger.debug(
                                    f"{position_key}: GTT expires in {days_until_expiry} days "
                                    f"({expires_at_str})"
                                )

                        else:
                            # No expiry date in GTT data (shouldn't happen)
                            logger.warning(
                                f"⚠️ {position_key}: GTT {position.current_gtt_id} has no expiry date"
                            )

                    except Exception as e:
                        # Don't fail entire check on expiry parsing error
                        logger.error(f"Error checking GTT expiry for {position_key}: {e}")

            logger.info(
                f"Position-GTT check complete: {len(results['issues'])} issues, "
                f"{len(results['critical_alerts'])} critical"
            )

        except Exception as e:
            logger.error(f"Error in position-GTT reconciliation: {e}")
            results['issues'].append(f"Check failed: {str(e)}")

        return results

    def _check_missed_order_fills(self, session: Session) -> Dict:
        """
        Check 2: Detect orders that filled but GTT not placed

        Catches:
        - Orders marked PENDING in DB but filled in Zerodha
        - Orders filled late (near market close)
        - Order monitoring failures
        """
        results = {
            'issues': [],
            'actions': [],
            'critical_alerts': []
        }

        try:
            # Get all pending orders from database (for this bot)
            pending_orders = session.query(OpenOrder).filter_by(
                bot_instance_id=config.get_bot_instance_id(),
                status=OrderStatus.PENDING
            ).all()

            if not pending_orders:
                logger.info("No pending orders to check")
                return results

            logger.info(f"Checking {len(pending_orders)} pending orders for missed fills...")

            for order in pending_orders:
                order_key = f"{order.script} {order.timeframe}"

                try:
                    # Check order status in Zerodha
                    order_status_data = self.kite_client.get_order_status(order.order_id)
                    zerodha_status = order_status_data.get('status', '').upper()

                    # Check if filled
                    if zerodha_status == 'COMPLETE':
                        filled_qty = order_status_data.get('filled_quantity', 0)
                        avg_price = order_status_data.get('average_price', 0)

                        issue = f"MISSED FILL: {order_key} - Order filled but not detected by monitoring"
                        logger.error(f"🚨 {issue}")
                        results['issues'].append(issue)
                        results['critical_alerts'].append(
                            f"🚨 **MISSED ORDER FILL**\n"
                            f"Script: {order_key}\n"
                            f"Order ID: {order.order_id}\n"
                            f"Filled Qty: {filled_qty}\n"
                            f"Avg Price: Rs.{avg_price:.2f}\n"
                            f"**Position exists without GTT!**"
                        )

                        # Auto-fix: Process the fill
                        if self.auto_fix_enabled:
                            action = self._auto_fix_missed_fill(order, order_status_data, session)
                            results['actions'].append(action)

                    elif zerodha_status in ['REJECTED', 'CANCELLED']:
                        # Order not filled, update DB
                        logger.info(f"Order {order_key} status: {zerodha_status} - updating DB")
                        order.status = OrderStatus.REJECTED if zerodha_status == 'REJECTED' else OrderStatus.CANCELLED
                        session.commit()
                        results['actions'].append(f"Updated {order_key} status to {zerodha_status}")

                except Exception as e:
                    error_msg = f"Failed to check order {order_key}: {e}"
                    logger.error(error_msg)
                    results['issues'].append(error_msg)

            logger.info(
                f"Order fill check complete: {len(results['issues'])} missed fills detected"
            )

        except Exception as e:
            logger.error(f"Error in missed order fill check: {e}")
            results['issues'].append(f"Check failed: {str(e)}")

        return results

    def _check_orphan_gtts(self, session: Session) -> Dict:
        """
        Check 3: Detect ACTIVE GTTs without corresponding open positions

        IMPORTANT: Only ACTIVE GTTs without positions are true orphans.
        Triggered/Cancelled GTTs are NOT orphans - they're valid GTTs from closed positions.

        True Orphans (ACTIVE GTTs without position):
        - GTTs left over from manual position closes (forgot to cancel GTT)
        - Database sync issues
        - Duplicate GTTs placed by error

        NOT Orphans (skip these entirely):
        - Triggered GTTs: Position was closed via SL hit - this is normal
        - Cancelled GTTs: Already inactive
        - Disabled/Rejected GTTs: Already inactive
        """
        results = {
            'issues': [],
            'actions': [],
            'warnings': []
        }

        orphan_stats = {
            'active_orphans_found': 0,
            'cancelled_successfully': 0,
            'cancel_failed': 0,
            'skipped_triggered': 0,
            'skipped_inactive': 0
        }

        try:
            # Get all GTTs from Zerodha
            try:
                zerodha_gtts = self.kite_client.get_gtt_orders()
            except Exception as e:
                logger.error(f"Failed to fetch GTTs: {e}\nAPI Response: {getattr(e, 'response', 'No response available')}")
                return results

            # Get all open positions with GTT IDs (for this bot)
            open_positions = session.query(OpenPosition).filter_by(
                bot_instance_id=config.get_bot_instance_id(),
                status=PositionStatus.OPEN
            ).all()

            position_gtt_ids = {str(pos.current_gtt_id) for pos in open_positions if pos.current_gtt_id}

            logger.info(f"Checking {len(zerodha_gtts)} GTTs against {len(position_gtt_ids)} position GTT IDs...")

            # Check each GTT
            for gtt in zerodha_gtts:
                gtt_id = str(gtt.get('id'))
                script = gtt.get('condition', {}).get('tradingsymbol', 'UNKNOWN')
                gtt_status = gtt.get('status', 'unknown').lower()

                # Skip GTTs that belong to open positions
                if gtt_id in position_gtt_ids:
                    continue

                # GTT has no corresponding open position - check if it's a true orphan

                # SKIP triggered GTTs - these are VALID (position closed via SL hit)
                if gtt_status == 'triggered':
                    orphan_stats['skipped_triggered'] += 1
                    logger.debug(f"Skipping triggered GTT {gtt_id} ({script}) - valid SL execution")
                    continue

                # SKIP inactive GTTs - already cancelled/disabled
                if gtt_status in ['cancelled', 'disabled', 'rejected']:
                    orphan_stats['skipped_inactive'] += 1
                    logger.debug(f"Skipping inactive GTT {gtt_id} ({script}) - status: {gtt_status}")
                    continue

                # ACTIVE GTT without position = TRUE ORPHAN
                if gtt_status == 'active':
                    orphan_stats['active_orphans_found'] += 1
                    issue = f"ORPHAN GTT (active): {gtt_id} for {script}"
                    logger.warning(f"⚠️ {issue}")
                    results['issues'].append(issue)

                    # Auto-fix: Cancel orphan GTT
                    if self.auto_fix_enabled:
                        try:
                            self.kite_client.cancel_gtt_order(gtt_id)
                            orphan_stats['cancelled_successfully'] += 1
                            action = f"Cancelled orphan GTT: {gtt_id} for {script}"
                            logger.info(f"✅ {action}")
                            results['actions'].append(action)
                        except Exception as e:
                            orphan_stats['cancel_failed'] += 1
                            error_msg = str(e)
                            response_text = getattr(e, 'response', None)
                            if response_text:
                                try:
                                    response_text = response_text.text[:200]  # First 200 chars
                                except:
                                    response_text = str(response_text)[:200]

                            # Check if it's a server error (502, 503, etc.)
                            if '502' in error_msg or '503' in error_msg or '504' in error_msg:
                                logger.warning(
                                    f"  Server error cancelling GTT {gtt_id}: {error_msg} - "
                                    f"will retry next cycle"
                                )
                                results['warnings'].append(
                                    f"⚠️ **ORPHAN GTT (SERVER ERROR)**\n"
                                    f"GTT ID: {gtt_id}\n"
                                    f"Script: {script}\n"
                                    f"Error: Server unavailable\n"
                                    f"Response: {response_text or 'N/A'}\n"
                                    f"Will retry on next recovery cycle"
                                )
                            else:
                                logger.error(f"  Failed to cancel orphan GTT {gtt_id}: {error_msg}")
                                results['warnings'].append(
                                    f"⚠️ **ORPHAN GTT (CANCEL FAILED)**\n"
                                    f"GTT ID: {gtt_id}\n"
                                    f"Script: {script}\n"
                                    f"Error: {error_msg}\n"
                                    f"Response: {response_text or 'N/A'}\n"
                                    f"Manual cancellation may be required"
                                )
                    else:
                        # Auto-fix disabled
                        results['warnings'].append(
                            f"⚠️ **ORPHAN GTT**\n"
                            f"GTT ID: {gtt_id}\n"
                            f"Script: {script}\n"
                            f"Status: {gtt_status}\n"
                            f"No corresponding open position - manual review needed"
                        )

            # Log summary
            logger.info(
                f"Orphan GTT check complete: "
                f"ActiveOrphans={orphan_stats['active_orphans_found']}, "
                f"Cancelled={orphan_stats['cancelled_successfully']}, "
                f"CancelFailed={orphan_stats['cancel_failed']}, "
                f"SkippedTriggered={orphan_stats['skipped_triggered']}, "
                f"SkippedInactive={orphan_stats['skipped_inactive']}"
            )

            if orphan_stats['skipped_triggered'] > 0:
                logger.info(
                    f"  Note: {orphan_stats['skipped_triggered']} triggered GTTs skipped "
                    f"(valid SL executions from closed positions)"
                )

        except Exception as e:
            logger.error(f"Error in orphan GTT check: {e}")
            results['issues'].append(f"Check failed: {str(e)}")

        return results

    def _check_quantity_mismatches(self, session: Session) -> Dict:
        """
        Check 4: Verify GTT quantity matches position quantity

        Catches:
        - Partial fills with wrong GTT quantity
        - Manual modifications
        """
        results = {
            'issues': [],
            'warnings': []
        }

        try:
            open_positions = session.query(OpenPosition).filter_by(
                bot_instance_id=config.get_bot_instance_id(),
                status=PositionStatus.OPEN
            ).all()

            if not open_positions:
                return results

            # Get all GTTs
            try:
                zerodha_gtts = self.kite_client.get_gtt_orders()
                gtt_lookup = {str(gtt.get('id')): gtt for gtt in zerodha_gtts}
            except Exception as e:
                logger.error(f"Failed to fetch GTTs: {e}\nAPI Response: {getattr(e, 'response', 'No response available')}")
                return results

            for position in open_positions:
                if not position.current_gtt_id:
                    continue  # Already caught in Check 1

                gtt = gtt_lookup.get(str(position.current_gtt_id))
                if not gtt:
                    continue  # Already caught in Check 1

                gtt_qty = gtt.get('orders', [{}])[0].get('quantity', 0)

                if gtt_qty != position.quantity:
                    issue = f"Quantity mismatch: {position.script} {position.timeframe} - Position qty={position.quantity}, GTT qty={gtt_qty}"
                    logger.warning(f"⚠️ {issue}")
                    results['issues'].append(issue)
                    results['warnings'].append(
                        f"⚠️ **QUANTITY MISMATCH**\n"
                        f"Script: {position.script} {position.timeframe}\n"
                        f"Position Qty: {position.quantity}\n"
                        f"GTT Qty: {gtt_qty}\n"
                        f"**Manual review required**"
                    )

            logger.info(f"Quantity check complete: {len(results['issues'])} mismatches found")

        except Exception as e:
            logger.error(f"Error in quantity mismatch check: {e}")
            results['issues'].append(f"Check failed: {str(e)}")

        return results

    def _check_orphan_orders(self, session: Session) -> Dict:
        """
        Check 5: Detect orphan orders (marked PENDING in DB but actually cancelled/filled/expired in Zerodha)

        Catches:
        - Orders cancelled by exchange but still marked PENDING in DB
        - Orders expired but still marked PENDING in DB
        - Orders completed but not updated in DB (partial fill edge cases)

        This prevents duplicate signal rejection when order is actually dead.
        """
        results = {
            'issues': [],
            'actions': [],
            'warnings': []
        }

        try:
            # Get all pending orders from database (for this bot)
            pending_orders = session.query(OpenOrder).filter_by(
                bot_instance_id=config.get_bot_instance_id(),
                status=OrderStatus.PENDING
            ).all()

            if not pending_orders:
                logger.info("No pending orders to check")
                return results

            logger.info(f"Checking {len(pending_orders)} pending orders against Zerodha...")

            # Fetch all orders from Zerodha (today's orders)
            try:
                zerodha_orders = self.kite_client.get_all_orders()
                logger.info(f"Fetched {len(zerodha_orders)} orders from Zerodha")
            except Exception as e:
                error_msg = f"Failed to fetch orders from Zerodha: {e}"
                logger.error(f"{error_msg}\nAPI Response: {getattr(e, 'response', 'No response available')}")
                results['issues'].append(error_msg)
                return results

            # Build lookup: {order_id: order_data}
            zerodha_order_lookup = {str(order.get('order_id')): order for order in zerodha_orders}

            # Check each pending order
            for db_order in pending_orders:
                order_key = f"{db_order.script} {db_order.timeframe}"
                order_id = str(db_order.order_id)

                # Check if order exists in Zerodha
                if order_id not in zerodha_order_lookup:
                    # Order not found - might be old or already processed
                    issue = f"Order {order_id} ({order_key}) not found in Zerodha today's orders"
                    logger.warning(f"⚠️ {issue}")
                    results['warnings'].append(issue)
                    continue

                zerodha_order = zerodha_order_lookup[order_id]
                zerodha_status = zerodha_order.get('status', '').upper()

                # Check if order is actually dead in Zerodha
                dead_statuses = ['CANCELLED', 'REJECTED', 'COMPLETE']

                if zerodha_status in dead_statuses:
                    issue = (
                        f"ORPHAN ORDER: {order_id} ({order_key}) - "
                        f"DB status=PENDING, Zerodha status={zerodha_status}"
                    )
                    logger.warning(f"⚠️ {issue}")
                    results['issues'].append(issue)

                    # Check if position was created (partial fill scenario) (for this bot)
                    position_exists = session.query(OpenPosition).filter_by(
                        bot_instance_id=config.get_bot_instance_id(),
                        script=db_order.script,
                        timeframe=db_order.timeframe,
                        status=PositionStatus.OPEN
                    ).first()

                    # Auto-fix: Update order status
                    if self.auto_fix_enabled:
                        action_msg = self._auto_fix_orphan_order(
                            db_order,
                            zerodha_status,
                            position_exists,
                            session
                        )
                        results['actions'].append(action_msg)
                    else:
                        # Manual review needed
                        results['warnings'].append(
                            f"⚠️ **ORPHAN ORDER DETECTED**\n"
                            f"Order: {order_id} ({order_key})\n"
                            f"DB Status: PENDING\n"
                            f"Zerodha Status: {zerodha_status}\n"
                            f"Position exists: {'Yes' if position_exists else 'No'}\n"
                            f"**Manual review required**"
                        )

            logger.info(
                f"Orphan order check complete: {len(results['issues'])} orphan orders found"
            )

        except Exception as e:
            logger.error(f"Error in orphan order check: {e}")
            results['issues'].append(f"Check failed: {str(e)}")

        return results

    def _auto_fix_orphan_order(
        self,
        db_order: OpenOrder,
        zerodha_status: str,
        position_exists: bool,
        session: Session
    ) -> str:
        """
        Auto-fix: Update orphan order status in database

        Logic:
        - If position exists: Order was partially filled and cancelled → Mark as FILLED
        - If no position: Order was rejected/cancelled without fill → Mark as CANCELLED
        """
        try:
            order_key = f"{db_order.script} {db_order.timeframe}"
            logger.info(f"AUTO-FIX: Updating orphan order {db_order.order_id} ({order_key})")

            if position_exists:
                # Partial fill scenario - position was created
                new_status = OrderStatus.FILLED
                db_order.status = new_status
                session.commit()

                action = (
                    f"✅ AUTO-FIX: Updated order {db_order.order_id} ({order_key})\n"
                    f"  Status: PENDING → FILLED\n"
                    f"  Reason: Position exists (partial fill detected)"
                )
                logger.info(action)
                return action
            else:
                # No position - order was cancelled/rejected without fill
                if zerodha_status == 'CANCELLED':
                    new_status = OrderStatus.CANCELLED
                elif zerodha_status == 'REJECTED':
                    new_status = OrderStatus.REJECTED
                else:
                    new_status = OrderStatus.CANCELLED  # Default

                db_order.status = new_status
                session.commit()

                action = (
                    f"✅ AUTO-FIX: Updated order {db_order.order_id} ({order_key})\n"
                    f"  Status: PENDING → {new_status}\n"
                    f"  Zerodha status: {zerodha_status}\n"
                    f"  No position created (order dead without fill)"
                )
                logger.info(action)
                return action

        except Exception as e:
            action = f"❌ AUTO-FIX FAILED: Order {db_order.order_id}: {str(e)}"
            logger.error(action)
            return action

    def _auto_fix_missing_gtt(self, position: OpenPosition, session: Session) -> str:
        """Auto-fix: Place missing GTT for unprotected position"""
        try:
            logger.info(f"AUTO-FIX: Placing missing GTT for {position.script} {position.timeframe}...")

            # Use current SL if available, then ATR-based, then fixed 10% fallback
            if position.current_sl:
                sl_price = position.current_sl
            elif position.entry_atr:
                atr_mult = config.get('trading.sl_strategy.initial_atr_multiplier', 2.0)
                sl_price = position.entry_price - (atr_mult * position.entry_atr)
            else:
                sl_price = position.entry_price * 0.90

            # Place GTT with verification
            success, gtt_id, error = exit_manager.place_dummy_gtt(
                position=position,
                entry_price=position.entry_price,
                session=session
            )

            if success:
                action = f"✅ AUTO-FIX: Placed GTT {gtt_id} for {position.script} @ Rs.{sl_price:.2f}"
                logger.info(action)
                return action
            else:
                action = f"❌ AUTO-FIX FAILED: Could not place GTT for {position.script}: {error}"
                logger.error(action)
                return action

        except Exception as e:
            action = f"❌ AUTO-FIX ERROR: {position.script}: {str(e)}"
            logger.error(action)
            return action

    def _auto_fix_triggered_gtt(self, position: OpenPosition, gtt: Dict, session: Session) -> str:
        """
        Auto-fix: Close position when GTT triggered (SL hit)

        Extracts actual exit price from completed SELL order with comprehensive fallback:
        1. Try to find SELL order matching position (GTT-triggered or manual)
        2. Fallback to current_sl if no order found - Safety net

        This ensures accurate P&L calculation for both automated and manual position closes.
        """
        try:
            position_key = f"{position.script} {position.timeframe}"
            logger.info(f"AUTO-FIX: Closing {position_key} - GTT triggered (SL hit)")

            # Extract actual exit price from completed SELL order
            exit_price = None
            exit_source = None

            # Find SELL order matching this position
            try:
                all_orders = self.kite_client.get_all_orders()

                for order in all_orders:
                    if (order.get('tradingsymbol') == position.script and
                        order.get('transaction_type') == 'SELL' and
                        order.get('status') == 'COMPLETE' and
                        order.get('quantity') == position.quantity):

                        # Found SELL order
                        exit_price = order.get('average_price')
                        exit_source = 'ORDER'
                        logger.info(
                            f"Found SELL order for {position_key}: "
                            f"Order ID {order.get('order_id')}, Exit price Rs.{exit_price:.2f}"
                        )
                        break
            except Exception as e:
                logger.warning(f"Failed to fetch all orders: {e}")

            # Fallback to current SL if no order found
            if exit_price is None:
                exit_price = position.current_sl
                exit_source = 'FALLBACK_SL'
                logger.warning(
                    f"No SELL order found for {position_key}. "
                    f"Using fallback exit price = current_sl = Rs.{exit_price:.2f}"
                )

            # Close the position using exit_manager
            success = exit_manager.close_position(
                position=position,
                exit_price=exit_price,
                exit_reason='SL_HIT',
                session=session
            )

            if success:
                action = (
                    f"✅ AUTO-FIX: Closed {position_key} @ Rs.{exit_price:.2f} "
                    f"(Source: {exit_source})"
                )
                logger.info(action)

                # Send notification
                exit_source_label = {
                    'GTT_ORDER': 'GTT-triggered order',
                    'MANUAL_ORDER': 'Manual order',
                    'FALLBACK_SL': 'Fallback SL (order not found)'
                }.get(exit_source, exit_source)

                telegram.send_alert(
                    f"✅ **Position Auto-Closed (SL Hit)**\n"
                    f"Script: {position_key}\n"
                    f"Exit Price: Rs.{exit_price:.2f}\n"
                    f"Price Source: {exit_source_label}\n"
                    f"GTT ID: {position.current_gtt_id}\n"
                    f"Detected by Same-Day Recovery workflow"
                )

                return action
            else:
                action = f"❌ AUTO-FIX FAILED: Could not close {position_key}"
                logger.error(action)
                return action

        except Exception as e:
            action = f"❌ AUTO-FIX ERROR: Close position {position.script}: {str(e)}"
            logger.error(action)
            return action

    def _auto_fix_expiring_gtt(self, position: OpenPosition, gtt: Dict, session: Session) -> str:
        """
        Auto-fix: Renew GTT that is expiring soon

        Process:
        1. Cancel old GTT (expiring soon)
        2. Place new GTT with fresh 365-day expiry
        3. Update position record with new GTT ID
        """
        try:
            position_key = f"{position.script} {position.timeframe}"
            old_gtt_id = position.current_gtt_id
            expires_at = gtt.get('expires_at', 'unknown')

            logger.info(
                f"AUTO-FIX: Renewing expiring GTT for {position_key} "
                f"(current GTT {old_gtt_id} expires: {expires_at})"
            )

            # Step 1: Cancel old GTT
            try:
                cancel_success = exit_manager.cancel_gtt_order(old_gtt_id)
                if not cancel_success:
                    action = f"❌ AUTO-FIX FAILED: Could not cancel old GTT {old_gtt_id} for {position_key}"
                    logger.error(action)
                    return action
                logger.info(f"Cancelled old GTT {old_gtt_id}")
            except Exception as e:
                action = f"❌ AUTO-FIX FAILED: Error cancelling GTT {old_gtt_id}: {e}"
                logger.error(action)
                return action

            # Step 2: Place new GTT with current SL
            # Get current LTP for validation
            try:
                instrument_token = self.kite_client.get_instrument_token(position.script)
                current_ltp = self.kite_client.get_instrument_ltp(instrument_token)
            except Exception as e:
                action = f"❌ AUTO-FIX FAILED: Could not get LTP for {position.script}: {e}"
                logger.error(action)
                return action

            # Place new GTT using exit_manager's method (includes verification)
            from src.utils.price_rounder import round_price
            sl_price_rounded = round_price(position.current_sl)

            success, new_gtt_id, error = exit_manager._place_gtt_order(
                script=position.script,
                quantity=position.quantity,
                sl_price=sl_price_rounded,
                current_ltp=current_ltp
            )

            if not success:
                action = (
                    f"❌ AUTO-FIX FAILED: Could not place new GTT for {position_key}: {error}\n"
                    f"**WARNING: Old GTT {old_gtt_id} was cancelled! Position unprotected!**"
                )
                logger.error(action)

                # Send critical alert - position is now unprotected
                telegram.send_alert(
                    f"🚨 **CRITICAL: Position Unprotected!**\n"
                    f"Script: {position_key}\n"
                    f"Old GTT {old_gtt_id} cancelled\n"
                    f"New GTT placement failed: {error}\n"
                    f"**Manual intervention required immediately!**",
                    critical=True
                )
                return action

            # Step 3: Update position with new GTT ID
            position.current_gtt_id = new_gtt_id
            session.commit()

            action = (
                f"✅ AUTO-FIX: Renewed GTT for {position_key}\n"
                f"  Old GTT: {old_gtt_id} (cancelled)\n"
                f"  New GTT: {new_gtt_id} @ Rs.{sl_price_rounded:.2f}\n"
                f"  New expiry: 365 days from now"
            )
            logger.info(action)

            # Send notification
            telegram.send_alert(
                f"✅ **GTT Renewed Successfully**\n"
                f"Script: {position_key}\n"
                f"Old GTT: {old_gtt_id} (expiring: {expires_at})\n"
                f"New GTT: {new_gtt_id} @ Rs.{sl_price_rounded:.2f}\n"
                f"New expiry: 365 days\n"
                f"Auto-renewed by Same-Day Recovery"
            )

            return action

        except Exception as e:
            action = f"❌ AUTO-FIX ERROR: {position.script}: {str(e)}"
            logger.error(action)
            return action

    def _auto_fix_missed_fill(self, order: OpenOrder, order_data: Dict, session: Session) -> str:
        """
        Auto-fix: Process missed order fill

        NOTE: For safety, this marks the order as filled and sends alert for manual review.
        Creating positions automatically from missed fills is too risky (capital calculations, etc.)
        """
        try:
            order_key = f"{order.script} {order.timeframe}"
            filled_qty = order_data.get('filled_quantity', 0)
            avg_price = order_data.get('average_price', 0)

            logger.warning(
                f"AUTO-FIX LIMITATION: Missed fill for {order_key} - "
                f"Manual intervention required to create position"
            )

            # Update order status to prevent repeated alerts
            order.status = OrderStatus.FILLED
            order.filled_quantity = filled_qty
            order.fill_price = avg_price
            session.commit()

            action = (
                f"⚠️ MANUAL ACTION REQUIRED: {order_key} filled @ Rs.{avg_price:.2f} "
                f"(Qty: {filled_qty}) - Position needs to be created manually with GTT"
            )
            logger.warning(action)

            # Send detailed alert
            telegram.send_alert(
                f"⚠️ **MISSED FILL - MANUAL ACTION REQUIRED**\n"
                f"Script: {order_key}\n"
                f"Order ID: {order.order_id}\n"
                f"Filled Qty: {filled_qty}\n"
                f"Avg Price: Rs.{avg_price:.2f}\n"
                f"Value: Rs.{filled_qty * avg_price:,.2f}\n\n"
                f"**Action:** Manually create OpenPosition record and place GTT\n"
                f"Order marked as FILLED in database to prevent repeated alerts."
            )

            return action

        except Exception as e:
            action = f"❌ AUTO-FIX ERROR: {order.script}: {str(e)}"
            logger.error(action)
            return action

    def _send_recovery_alerts(self, report: Dict):
        """Send Telegram alerts for recovery findings - Compact colored format"""
        try:
            summary = report['summary']
            critical_count = summary['critical_issues']
            warning_count = summary['warnings']
            fixes_count = summary['actions_taken']
            checks_count = summary['total_checks']
            issues_count = summary['total_issues']

            # Build compact header
            date_str = report['date'].strftime('%d %b') if hasattr(report['date'], 'strftime') else str(report['date'])

            if critical_count == 0 and warning_count == 0:
                # All good - compact success message
                logger.info("No issues found - sending success confirmation")
                status_line = fmt.format_recovery_summary(checks_count, 0, 0, 0, fixes_count)
                telegram.send_alert(
                    f"<b>RECOVERY RUN</b> ✅\n"
                    f"{status_line}\n\n"
                    f"{fmt.CHECK} All positions verified & protected"
                )
                return

            # Build consolidated alert with all findings
            status_emoji = "🔴" if critical_count > 0 else "⚠️"
            lines = [f"<b>RECOVERY RUN</b> {status_emoji}"]
            lines.append("")

            # Status summary line
            status_line = fmt.format_recovery_summary(checks_count, issues_count, critical_count, warning_count, fixes_count)
            lines.append(status_line)

            # Critical issues - compact format
            if critical_count > 0:
                lines.append("")
                lines.append(f"{fmt.RED_CIRCLE} *Critical* ({critical_count}):")
                for i, alert in enumerate(report['critical_alerts'][:3]):
                    # Extract key info from alert (typically multi-line)
                    alert_lines = alert.strip().split('\n')
                    # Get script name and issue type from first lines
                    issue_summary = self._extract_issue_summary(alert_lines)
                    lines.append(f"  {fmt.CROSS} {issue_summary}")
                if critical_count > 3:
                    lines.append(f"  +{critical_count - 3} more critical")

            # Warnings - compact format
            if warning_count > 0 and self.alert_threshold == 'any':
                lines.append("")
                lines.append(f"{fmt.YELLOW_CIRCLE} *Warnings* ({warning_count}):")
                for i, warn in enumerate(report['warnings'][:3]):
                    warn_lines = warn.strip().split('\n')
                    warn_summary = self._extract_issue_summary(warn_lines)
                    lines.append(f"  {fmt.WARNING} {warn_summary}")
                if warning_count > 3:
                    lines.append(f"  +{warning_count - 3} more warnings")

            # Auto-fixes applied
            if fixes_count > 0:
                lines.append("")
                lines.append(f"{fmt.GREEN_CIRCLE} *Auto-Fixes* ({fixes_count}):")
                for action in report['actions_taken'][:3]:
                    # Extract action type and script
                    action_summary = action.split('\n')[0][:50]
                    if action_summary.startswith('✅'):
                        action_summary = action_summary[2:].strip()
                    lines.append(f"  {fmt.CHECK} {action_summary}")
                if fixes_count > 3:
                    lines.append(f"  +{fixes_count - 3} more fixes")

            # Footer
            lines.append("")
            auto_fix_status = f"{fmt.CHECK} Auto-fix ON" if self.auto_fix_enabled else f"{fmt.WARNING} Auto-fix OFF"
            lines.append(auto_fix_status)

            telegram.send_alert("\n".join(lines))

        except Exception as e:
            logger.error(f"Error sending recovery alerts: {e}")

    def _extract_issue_summary(self, alert_lines: list) -> str:
        """Extract compact summary from multi-line alert"""
        try:
            # Look for Script: line
            script = None
            issue_type = None

            for line in alert_lines:
                line = line.strip()
                if line.startswith('Script:'):
                    script = line.replace('Script:', '').strip()
                elif line.startswith('**') and line.endswith('**'):
                    issue_type = line.strip('*').strip()
                elif 'UNPROTECTED' in line.upper():
                    issue_type = 'No GTT'
                elif 'GTT NOT FOUND' in line.upper():
                    issue_type = 'GTT missing'
                elif 'EXPIRING' in line.upper():
                    issue_type = 'GTT expiring'
                elif 'TRIGGERED' in line.upper():
                    issue_type = 'SL hit'
                elif 'ORPHAN' in line.upper():
                    issue_type = 'Orphan'
                elif 'MISMATCH' in line.upper():
                    issue_type = 'Qty mismatch'

            if script and issue_type:
                return f"{script}: {issue_type}"
            elif script:
                return script
            elif issue_type:
                return issue_type
            else:
                # Fallback: return first meaningful line
                for line in alert_lines:
                    line = line.strip().strip('*').strip()
                    if line and not line.startswith('🚨') and not line.startswith('⚠'):
                        return line[:45] + '...' if len(line) > 45 else line
                return "Issue detected"
        except:
            return "Issue detected"

    def _log_recovery_summary(self, report: Dict):
        """Log comprehensive recovery summary"""
        logger.info("\n" + "=" * 80)
        logger.info("SAME-DAY RECOVERY SUMMARY")
        logger.info("=" * 80)
        logger.info(f"Date: {report['date']}")
        logger.info(f"Timestamp: {report['timestamp']}")
        logger.info(f"Checks Performed: {report['summary']['total_checks']}")
        logger.info(f"Total Issues: {report['summary']['total_issues']}")
        logger.info(f"  - Critical: {report['summary']['critical_issues']}")
        logger.info(f"  - Warnings: {report['summary']['warnings']}")
        logger.info(f"Actions Taken: {report['summary']['actions_taken']}")
        logger.info(f"Auto-Fix: {'Enabled' if self.auto_fix_enabled else 'Disabled'}")

        if report['issues_found']:
            logger.info("\nISSUES FOUND:")
            for issue in report['issues_found']:
                logger.info(f"  - {issue}")

        if report['actions_taken']:
            logger.info("\nACTIONS TAKEN:")
            for action in report['actions_taken']:
                logger.info(f"  - {action}")

        logger.info("=" * 80)


# Singleton instance
recovery_manager = SameDayRecovery()


def print_workflow_banner():
    """Print a clear banner to identify workflow start in logs"""
    banner = """
***************************************************************
*  SAME-DAY RECOVERY WORKFLOW - Comprehensive EOD Reconcile   *
***************************************************************"""
    logger.info(banner)


def main():
    """Main entry point for same-day recovery workflow"""
    try:
        print_workflow_banner()
        logger.info("Same-day recovery workflow started")
        report = recovery_manager.run_recovery()

        if report['summary']['critical_issues'] > 0:
            logger.error(f"Recovery complete with {report['summary']['critical_issues']} CRITICAL issues")
            sys.exit(1)
        else:
            logger.info("Recovery complete - All checks passed")
            sys.exit(0)

    except Exception as e:
        logger.error(f"Recovery workflow failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
