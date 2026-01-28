"""
Approval Handler - Processes Telegram callbacks for signal approval workflow

Handles:
- Approve/Reject/Hold/Revise actions for signals
- HODL/EXIT actions for 30% drop alerts
- Price revision text messages
"""

from typing import Optional, Dict, Any, List
from loguru import logger

from src.telegram.bot import telegram
from src.models.database import (
    get_session, SignalQueue, OpenPosition, TelegramCallback,
    SignalStatus, PositionStatus, ist_now_naive
)
from src.api.dual_kite_client import get_kite_client


class ApprovalHandler:
    """Handles Telegram approval workflow callbacks"""

    def __init__(self):
        """Initialize approval handler"""
        # FIX SYS-C2: Changed mapping direction to support multiple signals
        # In-memory cache - also loaded from DB on demand (SYS-H5 fix)
        # Mapping: signal_id -> chat_id (allows multiple signals awaiting price)
        self._signal_awaiting_price: Dict[int, int] = {}  # signal_id -> chat_id
        # Also keep reverse mapping for text message handling
        self._chat_awaiting_signal: Dict[int, int] = {}  # chat_id -> signal_id (most recent)
        self._loaded_from_db = False

    def process_updates(self) -> List[Dict[str, Any]]:
        """
        Fetch and process all pending Telegram updates (non-blocking)

        Returns:
            List of processed actions for further handling
        """
        return self._process_updates_internal(timeout=0)

    def process_updates_long_poll(self, timeout: int = 30) -> List[Dict[str, Any]]:
        """
        Fetch and process Telegram updates with long-polling.

        This method blocks for up to `timeout` seconds waiting for new updates.
        Used in daemon mode for instant response.

        Args:
            timeout: Long-polling timeout in seconds (default 30)

        Returns:
            List of processed actions for further handling
        """
        return self._process_updates_internal(timeout=timeout)

    def _process_updates_internal(self, timeout: int) -> List[Dict[str, Any]]:
        """
        Internal method to fetch and process Telegram updates.

        Args:
            timeout: Polling timeout (0 for non-blocking, >0 for long-polling)

        Returns:
            List of processed actions for further handling
        """
        actions = []

        # Load awaiting_price state from DB on every call for crash recovery
        # This ensures signals in AWAITING_PRICE status are recovered if bot restarts
        self._load_awaiting_price_from_db()

        try:
            updates, is_error = telegram.get_updates(timeout=timeout)

            # Handle API failure (SYS-H3 fix)
            if is_error:
                logger.warning("Telegram API failed - user responses may be delayed")
                # Don't process None updates
                return actions

            if updates is None:
                return actions

            for update in updates:
                update_id = update.get('update_id')

                # Check if already processed
                if self._is_update_processed(update_id):
                    continue

                # Process callback query (button clicks)
                if 'callback_query' in update:
                    action = self._handle_callback(update['callback_query'])
                    if action:
                        actions.append(action)

                # Process text message (price revision)
                elif 'message' in update:
                    action = self._handle_message(update['message'])
                    if action:
                        actions.append(action)

                # Mark as processed
                self._mark_update_processed(update_id, update)

        except Exception as e:
            logger.error(f"Error processing Telegram updates: {e}")

        return actions

    def _load_awaiting_price_from_db(self) -> None:
        """
        Load signals with AWAITING_PRICE status from DB (SYS-H5 fix).
        This recovers state after crash/restart.

        FIX SYS-C2: Now properly loads ALL signals awaiting price.
        """
        try:
            session = get_session()
            try:
                # Find all signals awaiting price input
                signals = session.query(SignalQueue).filter(
                    SignalQueue.status == SignalStatus.AWAITING_PRICE
                ).all()

                chat_id = int(telegram.chat_id) if telegram.chat_id else 0

                for signal in signals:
                    # FIX SYS-C2: Store both mappings
                    self._signal_awaiting_price[signal.id] = chat_id
                    self._chat_awaiting_signal[chat_id] = signal.id  # Last one wins for text reply
                    logger.info(f"Recovered awaiting_price state for signal {signal.id} ({signal.script})")

                self._loaded_from_db = True

            finally:
                session.close()

        except Exception as e:
            logger.error(f"Error loading awaiting_price from DB: {e}")
            self._loaded_from_db = True  # Don't keep retrying

    def _is_update_processed(self, update_id: int) -> bool:
        """Check if update was already processed"""
        session = get_session()
        try:
            existing = session.query(TelegramCallback).filter(
                TelegramCallback.update_id == update_id
            ).first()
            return existing is not None
        finally:
            session.close()

    def _mark_update_processed(self, update_id: int, update: Dict[str, Any]) -> None:
        """Mark update as processed in database"""
        session = get_session()
        try:
            callback = TelegramCallback(
                update_id=update_id,
                callback_data=update.get('callback_query', {}).get('data'),
                message_text=update.get('message', {}).get('text'),
                processed_at=ist_now_naive()
            )
            session.add(callback)
            session.commit()
        except Exception as e:
            logger.error(f"Failed to mark update as processed: {e}")
            session.rollback()
        finally:
            session.close()

    def _handle_callback(self, callback_query: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle callback query (button click)"""
        parsed = telegram.process_callback_query(callback_query)
        action = parsed['action']
        target_id = parsed['target_id']
        message_id = parsed['message_id']
        callback_id = parsed['callback_id']

        logger.info(f"Processing callback: {action}_{target_id}")

        # Acknowledge the callback
        telegram.answer_callback_query(callback_id, "Processing...")

        # Handle signal approval actions
        if action in ['approve', 'reject', 'hold', 'revise']:
            return self._handle_signal_action(action, target_id, message_id)

        # Handle position actions (30% drop)
        elif action in ['hodl', 'exit']:
            return self._handle_position_action(action, target_id, message_id)

        # Handle report type selection
        elif action == 'report':
            # target_id here is actually the report type from callback_data "report_daily"
            report_type = parsed['raw_data'].split('_')[1] if '_' in parsed['raw_data'] else None
            if report_type:
                telegram.edit_message(message_id, f"Generating {report_type} report...")
                return {'type': 'command', 'command': 'report', 'report_type': report_type}
            return None

        # Handle cancel revise
        elif action == 'cancel':
            # FIX SYS-C2: Clear awaiting price state using new mapping
            if target_id in self._signal_awaiting_price:
                chat_id = self._signal_awaiting_price.pop(target_id)
                # Clear reverse mapping if it points to this signal
                if self._chat_awaiting_signal.get(chat_id) == target_id:
                    del self._chat_awaiting_signal[chat_id]
            telegram.edit_message(message_id, "Price revision cancelled.")
            return None

        return None

    def _handle_signal_action(
        self,
        action: str,
        signal_id: int,
        message_id: int
    ) -> Optional[Dict[str, Any]]:
        """Handle signal approval action"""
        session = get_session()
        try:
            signal = session.query(SignalQueue).filter(
                SignalQueue.id == signal_id
            ).first()

            if not signal:
                telegram.edit_message(message_id, f"Signal #{signal_id} not found.")
                return None

            script = signal.script
            signal_level = signal.signal_level

            if action == 'approve':
                signal.status = SignalStatus.APPROVED
                session.commit()

                # Update message
                telegram.edit_message(
                    message_id,
                    f"<b>{script}</b> APPROVED @ {signal_level:,.2f}\n"
                    f"Entry order will be placed."
                )

                return {
                    'type': 'signal_approved',
                    'signal_id': signal_id,
                    'script': script,
                    'entry_price': signal.user_price or signal_level
                }

            elif action == 'reject':
                signal.status = SignalStatus.REJECTED
                session.commit()

                telegram.edit_message(
                    message_id,
                    f"<b>{script}</b> REJECTED @ {signal_level:,.2f}"
                )
                return None

            elif action == 'hold':
                signal.status = SignalStatus.HOLD
                session.commit()

                telegram.edit_message(
                    message_id,
                    f"<b>{script}</b> on HOLD @ {signal_level:,.2f}\n"
                    f"Will re-ask tomorrow."
                )
                return None

            elif action == 'revise':
                signal.status = SignalStatus.AWAITING_PRICE
                session.commit()

                # Send price request message
                new_msg_id = telegram.send_price_request(signal_id, script, signal_level)

                # FIX SYS-C2: Store awaiting state in both mappings
                chat_id = int(telegram.chat_id)
                self._signal_awaiting_price[signal_id] = chat_id
                self._chat_awaiting_signal[chat_id] = signal_id

                # Update original message
                telegram.edit_message(
                    message_id,
                    f"<b>{script}</b> - Awaiting revised price..."
                )

                return None

        except Exception as e:
            logger.error(f"Error handling signal action: {e}")
            session.rollback()
            return None
        finally:
            session.close()

    def _handle_position_action(
        self,
        action: str,
        position_id: int,
        message_id: int
    ) -> Optional[Dict[str, Any]]:
        """Handle position action (HODL/EXIT for 30% drop)"""
        session = get_session()
        try:
            position = session.query(OpenPosition).filter(
                OpenPosition.id == position_id,
                OpenPosition.status == PositionStatus.OPEN
            ).first()

            if not position:
                telegram.edit_message(message_id, f"Position #{position_id} not found or closed.")
                return None

            script = position.script

            if action == 'hodl':
                # Just acknowledge - don't alert again today
                position.drop_alert_sent = True
                position.drop_alert_date = ist_now_naive().date()
                session.commit()

                telegram.edit_message(
                    message_id,
                    f"<b>{script}</b> - HODL confirmed.\n"
                    f"No more alerts today."
                )
                return None

            elif action == 'exit':
                telegram.edit_message(
                    message_id,
                    f"<b>{script}</b> - EXIT initiated.\n"
                    f"Placing market sell order..."
                )

                return {
                    'type': 'emergency_exit',
                    'position_id': position_id,
                    'script': script,
                    'quantity': position.quantity
                }

        except Exception as e:
            logger.error(f"Error handling position action: {e}")
            session.rollback()
            return None
        finally:
            session.close()

    def _handle_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle text message (for price revision)"""
        parsed = telegram.process_text_message(message)
        text = parsed['text'].strip()
        chat_id = parsed['chat_id']

        # FIX: Check for commands FIRST - commands should always work
        # regardless of awaiting_price state
        if text.startswith('/'):
            return self._handle_command(text, parsed['message_id'])

        # FIX SYS-C2: Check if we're awaiting a price for this chat using new mapping
        if chat_id not in self._chat_awaiting_signal:
            return None

        signal_id = self._chat_awaiting_signal[chat_id]

        # Try to parse price
        try:
            # Remove commas and parse
            price_str = text.replace(',', '').replace(' ', '')
            price = float(price_str)

            if price <= 0:
                telegram.send_alert("Invalid price. Please enter a positive number.")
                return None

            # Update signal with user price
            session = get_session()
            try:
                signal = session.query(SignalQueue).filter(
                    SignalQueue.id == signal_id
                ).first()

                if signal:
                    signal.user_price = price
                    signal.status = SignalStatus.APPROVED
                    session.commit()

                    # FIX SYS-C2: Clear both mappings
                    if signal_id in self._signal_awaiting_price:
                        del self._signal_awaiting_price[signal_id]
                    if chat_id in self._chat_awaiting_signal:
                        del self._chat_awaiting_signal[chat_id]

                    telegram.send_alert(
                        f"<b>{signal.script}</b> APPROVED @ {price:,.2f}\n"
                        f"(Revised from {signal.signal_level:,.2f})"
                    )

                    return {
                        'type': 'signal_approved',
                        'signal_id': signal_id,
                        'script': signal.script,
                        'entry_price': price
                    }

            finally:
                session.close()

        except ValueError:
            telegram.send_alert(f"Could not parse '{text}' as price. Please enter a number.")

        return None

    def _handle_command(self, command: str, message_id: int) -> Optional[Dict[str, Any]]:
        """Handle slash commands"""
        cmd = command.lower().split()[0]

        if cmd == '/positions':
            return {'type': 'command', 'command': 'positions'}
        elif cmd == '/pending':
            return {'type': 'command', 'command': 'pending'}
        elif cmd == '/stats':
            return {'type': 'command', 'command': 'stats'}
        elif cmd == '/capital':
            return {'type': 'command', 'command': 'capital'}
        elif cmd == '/report':
            return {'type': 'command', 'command': 'report'}
        elif cmd == '/weekly':
            return {'type': 'command', 'command': 'weekly'}
        elif cmd == '/kill':
            return {'type': 'command', 'command': 'kill'}
        elif cmd == '/resume':
            return {'type': 'command', 'command': 'resume'}
        elif cmd == '/sync':
            return {'type': 'command', 'command': 'sync'}
        elif cmd.startswith('/import'):
            # Extract script name from command (e.g., "/import BALRAMCHIN")
            parts = command.strip().split()
            if len(parts) >= 2:
                script = parts[1].upper()
                return {'type': 'command', 'command': 'import', 'script': script}
            else:
                telegram.send_alert("Usage: /import SCRIPT\nExample: /import BALRAMCHIN")
                return None
        elif cmd.startswith('/fix'):
            # Extract script name from command (e.g., "/fix BALRAMCHIN")
            parts = command.strip().split()
            if len(parts) >= 2:
                script = parts[1].upper()
                return {'type': 'command', 'command': 'fix', 'script': script}
            else:
                telegram.send_alert("Usage: /fix SCRIPT\nFixes unprotected position by placing SL GTT")
                return None
        elif cmd == '/help':
            self._send_help()
            return None

        return None

    def _send_help(self) -> None:
        """Send help message"""
        text = (
            "📚 <b>FIFTY Commands</b>\n\n"
            "📊 <b>View</b>\n"
            "/positions - Open positions + P&L\n"
            "/pending - Signals & GTT orders\n"
            "/stats - Trading statistics\n"
            "/capital - Capital allocation\n"
            "/report - Reports menu\n\n"
            "⚡ <b>Actions</b>\n"
            "/sync - Zerodha vs DB diff\n"
            "/import SCRIPT - Add position\n"
            "/fix SCRIPT - Place missing SL\n"
            "/kill - Stop operations\n"
            "/resume - Resume operations"
        )
        telegram.send_alert(text)

    def set_awaiting_price(self, chat_id: int, signal_id: int) -> None:
        """Set awaiting price state (for external use)"""
        # FIX SYS-C2: Update both mappings
        self._signal_awaiting_price[signal_id] = chat_id
        self._chat_awaiting_signal[chat_id] = signal_id

    def clear_awaiting_price(self, chat_id: int) -> None:
        """Clear awaiting price state"""
        # FIX SYS-C2: Clear from both mappings
        if chat_id in self._chat_awaiting_signal:
            signal_id = self._chat_awaiting_signal.pop(chat_id)
            if signal_id in self._signal_awaiting_price:
                del self._signal_awaiting_price[signal_id]

    def clear_awaiting_price_for_signal(self, signal_id: int) -> None:
        """Clear awaiting price state for a specific signal"""
        if signal_id in self._signal_awaiting_price:
            chat_id = self._signal_awaiting_price.pop(signal_id)
            if self._chat_awaiting_signal.get(chat_id) == signal_id:
                del self._chat_awaiting_signal[chat_id]


# Singleton instance
approval_handler = ApprovalHandler()
