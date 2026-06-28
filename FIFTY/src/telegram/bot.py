"""
Telegram Bot for FIFTY - Interactive approval workflow with inline buttons

Uses Telegram Bot API directly (no external library needed).
Supports inline keyboards for approval workflow.
"""

import json
import requests
from typing import Optional, Dict, Any, List, Tuple
from loguru import logger

from src.utils.config_manager import config
from src.models.database import get_telegram_offset, set_telegram_offset


# Constants for failure tracking (SYS-H3 fix)
MAX_CONSECUTIVE_FAILURES = 5


class TelegramBot:
    """
    Interactive Telegram bot with inline keyboard support.

    Uses getUpdates API for polling (no webhooks needed).
    """

    def __init__(self):
        """Initialize Telegram bot"""
        self.enabled = config.get('telegram.enabled', True)
        self.bot_token = config.get('telegram.bot_token')
        self.chat_id = config.get('telegram.chat_id')
        self.retry_attempts = config.get('telegram.retry_attempts', 3)
        self.retry_delay = config.get('telegram.retry_delay', 5)

        # Failure tracking (SYS-H3 fix)
        self._consecutive_failures = 0
        self._last_failure_logged = False

        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram bot_token or chat_id not configured - bot disabled")
            self.enabled = False

        if self.enabled:
            self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
            logger.info(f"Telegram bot initialized - Chat ID: {self.chat_id}")

    def register_commands(self) -> bool:
        """
        Register bot commands with Telegram (appears in / menu).

        Returns:
            True if successful
        """
        if not self.enabled:
            return False

        commands = [
            # View commands
            {"command": "positions", "description": "Open positions with P&L"},
            {"command": "pending", "description": "Signals & GTT orders"},
            {"command": "stats", "description": "Trading statistics"},
            {"command": "capital", "description": "Capital allocation"},
            {"command": "report", "description": "Generate reports"},
            # Action commands
            {"command": "sync", "description": "Zerodha vs DB comparison"},
            {"command": "import", "description": "Import position (e.g. /import RELIANCE)"},
            {"command": "fix", "description": "Place missing SL GTT"},
            {"command": "exit", "description": "Exit position at market"},
            {"command": "release", "description": "Release HOLD signal"},
            {"command": "kill", "description": "Stop all operations"},
            {"command": "resume", "description": "Resume operations"},
            {"command": "help", "description": "Show all commands"},
        ]

        result = self._make_request('setMyCommands', {'commands': commands})
        if result is not None:
            logger.info(f"Registered {len(commands)} commands with Telegram")
            return True
        else:
            logger.warning("Failed to register commands with Telegram")
            return False

    def _make_request(
        self,
        method: str,
        payload: Dict[str, Any] = None,
        timeout: int = 30
    ) -> Optional[Dict[str, Any]]:
        """Make request to Telegram API"""
        if not self.enabled:
            return None

        url = f"{self.base_url}/{method}"

        for attempt in range(self.retry_attempts):
            try:
                if payload:
                    response = requests.post(url, json=payload, timeout=timeout)
                else:
                    response = requests.get(url, timeout=timeout)

                response.raise_for_status()
                result = response.json()

                if result.get('ok'):
                    return result.get('result')
                else:
                    logger.error(f"Telegram API error: {result}")
                    return None

            except Exception as e:
                logger.warning(f"Telegram {method} attempt {attempt + 1}/{self.retry_attempts} failed: {e}")
                if attempt < self.retry_attempts - 1:
                    import time
                    time.sleep(self.retry_delay)
                else:
                    logger.error(f"All Telegram attempts failed for {method}: {e}")
                    return None

        return None

    # =========================================================================
    # SENDING MESSAGES
    # =========================================================================

    def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False,
        reply_markup: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Send message to Telegram

        Args:
            text: Message text (supports HTML)
            parse_mode: "HTML" or "Markdown"
            disable_notification: Silent notification
            reply_markup: Inline keyboard markup

        Returns:
            Message dict with message_id if successful
        """
        # Add test mode prefix
        if config.is_test_mode():
            text = "[TEST] " + text

        payload = {
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': parse_mode,
            'disable_notification': disable_notification
        }

        if reply_markup:
            payload['reply_markup'] = reply_markup

        return self._make_request('sendMessage', payload)

    def send_alert(self, message: str, critical: bool = False) -> bool:
        """Send alert message"""
        result = self.send_message(
            text=message,
            disable_notification=not critical
        )
        return result is not None

    def edit_message(
        self,
        message_id: int,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Edit existing message"""
        payload = {
            'chat_id': self.chat_id,
            'message_id': message_id,
            'text': text,
            'parse_mode': parse_mode
        }

        if reply_markup:
            payload['reply_markup'] = reply_markup

        result = self._make_request('editMessageText', payload)
        return result is not None

    def delete_message(self, message_id: int) -> bool:
        """Delete a message"""
        payload = {
            'chat_id': self.chat_id,
            'message_id': message_id
        }
        result = self._make_request('deleteMessage', payload)
        return result is not None

    def send_document(
        self,
        file_path: str,
        caption: str = None,
        disable_notification: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        Send a document file to Telegram.

        Args:
            file_path: Path to file to send
            caption: Optional caption text (supports HTML)
            disable_notification: Silent notification

        Returns:
            Message dict if successful, None otherwise
        """
        if not self.enabled:
            return None

        url = f"{self.base_url}/sendDocument"

        try:
            with open(file_path, 'rb') as f:
                files = {'document': f}
                data = {
                    'chat_id': self.chat_id,
                    'disable_notification': disable_notification
                }
                if caption:
                    # Add test mode prefix
                    if config.is_test_mode():
                        caption = "[TEST] " + caption
                    data['caption'] = caption
                    data['parse_mode'] = 'HTML'

                for attempt in range(self.retry_attempts):
                    try:
                        response = requests.post(url, data=data, files=files, timeout=60)
                        response.raise_for_status()
                        result = response.json()

                        if result.get('ok'):
                            return result.get('result')
                        else:
                            logger.error(f"Telegram sendDocument error: {result}")
                            return None

                    except Exception as e:
                        logger.warning(f"sendDocument attempt {attempt + 1}/{self.retry_attempts} failed: {e}")
                        if attempt < self.retry_attempts - 1:
                            import time
                            time.sleep(self.retry_delay)
                            # Re-open file for retry
                            f.seek(0)
                        else:
                            logger.error(f"All sendDocument attempts failed: {e}")
                            return None

        except FileNotFoundError:
            logger.error(f"File not found: {file_path}")
            return None
        except Exception as e:
            logger.error(f"Error sending document: {e}")
            return None

        return None

    def send_photo(
        self,
        file_path: str,
        caption: str = None,
        disable_notification: bool = False
    ) -> bool:
        """
        Send a photo/image to Telegram.

        Args:
            file_path: Path to image file
            caption: Optional caption (supports HTML)
            disable_notification: Silent notification

        Returns:
            True if sent successfully
        """
        if not self.enabled:
            return False

        url = f"{self.base_url}/sendPhoto"

        try:
            with open(file_path, 'rb') as f:
                files = {'photo': f}
                data = {
                    'chat_id': self.chat_id,
                    'disable_notification': disable_notification
                }
                if caption:
                    if config.is_test_mode():
                        caption = "[TEST] " + caption
                    data['caption'] = caption
                    data['parse_mode'] = 'HTML'

                for attempt in range(self.retry_attempts):
                    try:
                        response = requests.post(url, data=data, files=files, timeout=60)
                        response.raise_for_status()
                        result = response.json()

                        if result.get('ok'):
                            return True
                        else:
                            logger.error(f"Telegram sendPhoto error: {result}")
                            return False

                    except Exception as e:
                        logger.warning(f"sendPhoto attempt {attempt + 1}/{self.retry_attempts} failed: {e}")
                        if attempt < self.retry_attempts - 1:
                            import time
                            time.sleep(self.retry_delay)
                            f.seek(0)
                        else:
                            logger.error(f"All sendPhoto attempts failed: {e}")
                            return False

        except FileNotFoundError:
            logger.error(f"Image file not found: {file_path}")
            return False
        except Exception as e:
            logger.error(f"Error sending photo: {e}")
            return False

        return False

    def send_html_report(
        self,
        file_path: str,
        report_type: str = "Report",
        delete_after: bool = True
    ) -> bool:
        """
        Send an HTML report file and optionally delete it after.

        Args:
            file_path: Path to HTML file
            report_type: Type of report for caption (e.g., "Daily", "Weekly")
            delete_after: Delete file after successful send

        Returns:
            True if sent successfully
        """
        from src.utils.timezone_helper import now_ist

        caption = f"<b>FIFTY {report_type} Report</b>\n{now_ist().strftime('%Y-%m-%d %H:%M IST')}"

        result = self.send_document(file_path, caption=caption)

        if result and delete_after:
            try:
                import os
                os.unlink(file_path)
                logger.debug(f"Deleted report after send: {file_path}")
            except Exception as e:
                logger.warning(f"Could not delete report {file_path}: {e}")

        return result is not None

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: str = None,
        show_alert: bool = False
    ) -> bool:
        """Answer a callback query (acknowledge button click)"""
        payload = {
            'callback_query_id': callback_query_id,
            'show_alert': show_alert
        }
        if text:
            payload['text'] = text

        result = self._make_request('answerCallbackQuery', payload)
        return result is not None

    # =========================================================================
    # INLINE KEYBOARDS
    # =========================================================================

    @staticmethod
    def create_inline_keyboard(buttons: List[List[Dict[str, str]]]) -> Dict[str, Any]:
        """
        Create inline keyboard markup

        Args:
            buttons: List of rows, each row is a list of button dicts
                    Each button: {'text': 'Button Text', 'callback_data': 'action_id'}

        Example:
            buttons = [
                [{'text': 'Approve', 'callback_data': 'approve_123'},
                 {'text': 'Reject', 'callback_data': 'reject_123'}],
                [{'text': 'Hold', 'callback_data': 'hold_123'},
                 {'text': 'Revise', 'callback_data': 'revise_123'}]
            ]
        """
        return {
            'inline_keyboard': [
                [{'text': btn['text'], 'callback_data': btn['callback_data']} for btn in row]
                for row in buttons
            ]
        }

    def send_signal_notification(
        self,
        signal_id: int,
        script: str,
        signal_level: float,
        ltp: float,
        quantity: int,
        position_value: float,
        available_capital: float,
        is_fno: bool = False,
        company_snapshot: Optional[str] = None,
        conviction: Optional[str] = None
    ) -> Optional[int]:
        """
        Send signal notification with approval buttons

        Returns:
            message_id if successful, None otherwise
        """
        distance_pct = ((ltp - signal_level) / signal_level) * 100 if signal_level > 0 else 0
        distance_sign = "+" if distance_pct >= 0 else ""

        fno_tag = " [NFO]" if is_fno else ""

        conviction_line = f"\n{conviction}" if conviction else ""

        text = (
            f"<b>{script}</b>{fno_tag} @ {signal_level:,.2f}\n"
            f"LTP: {ltp:,.2f} ({distance_sign}{distance_pct:.1f}%)\n"
            f"Qty: {quantity} / Value: {position_value:,.0f}{conviction_line}\n\n"
            f"Capital: {available_capital:,.0f}"
        )

        if company_snapshot:
            text += f"\n\n{company_snapshot}"

        # Create approval buttons
        buttons = [
            [
                {'text': 'Approve', 'callback_data': f'approve_{signal_id}'},
                {'text': 'Reject', 'callback_data': f'reject_{signal_id}'}
            ],
            [
                {'text': 'Hold', 'callback_data': f'hold_{signal_id}'},
                {'text': 'Revise', 'callback_data': f'revise_{signal_id}'}
            ],
            [
                {'text': '\U0001f4ca Full Report', 'callback_data': f'details_{signal_id}'}
            ]
        ]
        reply_markup = self.create_inline_keyboard(buttons)

        result = self.send_message(text, reply_markup=reply_markup)
        return result.get('message_id') if result else None

    def send_drop_alert(
        self,
        position_id: int,
        script: str,
        entry_price: float,
        current_price: float,
        drop_percent: float
    ) -> Optional[int]:
        """
        Send 30% drop alert with HODL/EXIT buttons

        Returns:
            message_id if successful
        """
        text = (
            f"<b>ALERT: Position Drop > 30%</b>\n\n"
            f"<b>{script}</b>\n"
            f"Entry: {entry_price:,.2f}\n"
            f"Current: {current_price:,.2f}\n"
            f"Drop: <b>-{drop_percent:.1f}%</b>"
        )

        buttons = [
            [
                {'text': 'HODL', 'callback_data': f'hodl_{position_id}'},
                {'text': 'EXIT NOW', 'callback_data': f'exit_{position_id}'}
            ]
        ]
        reply_markup = self.create_inline_keyboard(buttons)

        result = self.send_message(text, reply_markup=reply_markup)
        return result.get('message_id') if result else None

    def send_price_request(self, signal_id: int, script: str, current_level: float) -> Optional[int]:
        """
        Send price revision request

        Returns:
            message_id if successful
        """
        text = (
            f"<b>Enter revised price for {script}</b>\n\n"
            f"Current Level: {current_level:,.2f}\n\n"
            f"Reply with the price you want to use (e.g., 2450.50)"
        )

        # Add cancel button
        buttons = [[{'text': 'Cancel', 'callback_data': f'cancel_revise_{signal_id}'}]]
        reply_markup = self.create_inline_keyboard(buttons)

        result = self.send_message(text, reply_markup=reply_markup)
        return result.get('message_id') if result else None

    # =========================================================================
    # RECEIVING UPDATES
    # =========================================================================

    def get_updates(self, timeout: int = 0) -> Tuple[Optional[List[Dict[str, Any]]], bool]:
        """
        Get pending updates from Telegram (SYS-H3 fix)

        Uses long polling if timeout > 0

        Returns:
            Tuple of (updates_list, is_error)
            - ([], False) = Success, no new updates
            - ([...], False) = Success, updates found
            - (None, True) = Failure, could not fetch
        """
        if not self.enabled:
            return [], False

        offset = get_telegram_offset()

        payload = {
            'offset': offset + 1 if offset else None,
            'timeout': timeout,
            'allowed_updates': ['message', 'callback_query']
        }
        # Remove None values
        payload = {k: v for k, v in payload.items() if v is not None}

        result = self._make_request('getUpdates', payload, timeout=timeout + 10)

        if result is not None:
            # Success - reset failure counter
            if self._consecutive_failures > 0:
                logger.info(f"Telegram API recovered after {self._consecutive_failures} failures")
            self._consecutive_failures = 0
            self._last_failure_logged = False

            updates = result if isinstance(result, list) else []
            if updates:
                # Update offset to latest
                max_update_id = max(u['update_id'] for u in updates)
                set_telegram_offset(max_update_id)
            return updates, False

        # Failure case
        self._consecutive_failures += 1

        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            if not self._last_failure_logged:
                logger.error(
                    f"Telegram API failing repeatedly ({self._consecutive_failures} times). "
                    f"User button clicks may be lost!"
                )
                self._last_failure_logged = True

        return None, True

    def get_updates_simple(self, timeout: int = 0) -> List[Dict[str, Any]]:
        """
        Backward-compatible wrapper for get_updates.
        Returns empty list on failure (loses error distinction).
        """
        updates, is_error = self.get_updates(timeout)
        return updates if updates is not None else []

    def is_api_healthy(self) -> bool:
        """Check if Telegram API is responding normally"""
        return self._consecutive_failures < MAX_CONSECUTIVE_FAILURES

    def process_callback_query(self, callback_query: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse callback query into structured format

        Returns:
            {
                'callback_id': str,
                'action': str,  # 'approve', 'reject', 'hold', 'revise', 'hodl', 'exit'
                'target_id': int,  # signal_id or position_id
                'message_id': int,
                'user_id': int
            }
        """
        callback_data = callback_query.get('data', '')
        parts = callback_data.split('_')

        action = parts[0] if parts else ''
        target_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

        return {
            'callback_id': callback_query.get('id'),
            'action': action,
            'target_id': target_id,
            'message_id': callback_query.get('message', {}).get('message_id'),
            'user_id': callback_query.get('from', {}).get('id'),
            'raw_data': callback_data
        }

    def process_text_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse text message

        Returns:
            {
                'message_id': int,
                'text': str,
                'user_id': int,
                'date': int
            }
        """
        return {
            'message_id': message.get('message_id'),
            'text': message.get('text', ''),
            'user_id': message.get('from', {}).get('id'),
            'date': message.get('date'),
            'chat_id': message.get('chat', {}).get('id')
        }

    # =========================================================================
    # FORMATTED REPORTS
    # =========================================================================

    def send_daily_summary(
        self,
        date_str: str,
        open_positions: int,
        pending_orders: int,
        signals_processed: int,
        trades_today: int,
        realized_pnl: float,
        deployed_capital: float,
        free_capital: float
    ) -> bool:
        """Send daily summary report"""
        text = (
            f"<b>FIFTY Daily Summary - {date_str}</b>\n"
            f"{'─' * 25}\n\n"
            f"Open Positions: {open_positions}\n"
            f"Pending Orders: {pending_orders}\n"
            f"Signals Today: {signals_processed}\n"
            f"Trades Today: {trades_today}\n\n"
            f"<b>Capital</b>\n"
            f"Deployed: {deployed_capital:,.0f}\n"
            f"Free: {free_capital:,.0f}\n"
            f"Realized P&L: {realized_pnl:+,.0f}"
        )
        return self.send_alert(text, critical=False)

    def send_position_list(self, positions: List[Dict[str, Any]]) -> bool:
        """Send list of open positions"""
        if not positions:
            return self.send_alert("No open positions")

        lines = ["<b>Open Positions</b>\n"]
        for pos in positions:
            pnl_sign = "+" if pos.get('unrealized_pnl', 0) >= 0 else ""
            lines.append(
                f"<b>{pos['script']}</b> @ {pos['entry_price']:,.2f}\n"
                f"Qty: {pos['quantity']} | SL: {pos['current_sl']:,.2f}\n"
                f"P&L: {pnl_sign}{pos.get('unrealized_pnl', 0):,.0f} ({pnl_sign}{pos.get('pnl_pct', 0):.1f}%)\n"
            )

        text = '\n'.join(lines)
        return self.send_alert(text, critical=False)

    def send_pending_list(self, signals: List[Dict[str, Any]]) -> bool:
        """Send list of pending signals and hold signals"""
        if not signals:
            return self.send_alert("No pending signals")

        lines = ["<b>Pending Signals</b>\n"]
        for sig in signals:
            status_emoji = "" if sig['status'] == 'hold' else ""
            lines.append(
                f"{status_emoji} <b>{sig['script']}</b> @ {sig['signal_level']:,.2f}\n"
                f"Status: {sig['status'].upper()}\n"
            )

        text = '\n'.join(lines)
        return self.send_alert(text, critical=False)


# Singleton instance
telegram = TelegramBot()
