"""
SNAIL Telegram Bot (Polling)

Telegram bot with polling for receiving user commands and responses.
Implements hybrid architecture: alerts via HTTP, commands via polling.

@file        telegram_bot.py
@description Telegram bot with polling and command handlers
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 7
"""

import os
import threading
import time
import json
from datetime import datetime
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass
from enum import Enum
import requests
from loguru import logger

from src.api.telegram_alerts import TelegramAlerts, get_telegram


# =============================================================================
# CONSTANTS
# =============================================================================

TELEGRAM_API = "https://api.telegram.org/bot"
DEFAULT_POLL_INTERVAL = 2  # seconds
DEFAULT_TIMEOUT = 30  # long polling timeout
RESPONSE_TIMEOUT_MINUTES = 15  # timeout for user response
MAX_PROCESSED_CALLBACKS = 1000  # max callbacks to track for deduplication


# =============================================================================
# DATA CLASSES
# =============================================================================

class CallbackAction(Enum):
    """Callback action types for inline keyboard buttons."""
    HOLD = "hold"
    EXIT = "exit"
    ADJUST = "adjust"
    CONFIRM_YES = "confirm_yes"
    CONFIRM_NO = "confirm_no"


@dataclass
class TelegramUpdate:
    """Represents a Telegram update."""
    update_id: int
    message_id: Optional[int] = None
    chat_id: Optional[int] = None
    text: Optional[str] = None
    callback_query_id: Optional[str] = None
    callback_data: Optional[str] = None
    from_user_id: Optional[int] = None
    from_username: Optional[str] = None


@dataclass
class UserResponse:
    """User response to an alert."""
    action: CallbackAction
    alert_type: str
    timestamp: datetime
    position_id: Optional[int] = None
    user_id: Optional[int] = None


@dataclass
class PendingDecision:
    """
    Tracks a pending decision awaiting user response.

    Attributes:
        alert_type: Type of alert (stop_loss, friday, vix_warning, etc.)
        position_id: Associated position ID
        message_id: Telegram message ID with buttons
        created_at: When the decision was sent
        timeout_minutes: Timeout in minutes
    """
    alert_type: str
    position_id: Optional[int]
    message_id: int
    created_at: datetime
    timeout_minutes: int = RESPONSE_TIMEOUT_MINUTES

    def is_expired(self) -> bool:
        """Check if this decision has timed out."""
        from datetime import timedelta
        elapsed = datetime.now() - self.created_at
        return elapsed > timedelta(minutes=self.timeout_minutes)


# =============================================================================
# TELEGRAM BOT CLASS
# =============================================================================

class TelegramBot:
    """
    Telegram bot with polling for receiving commands and button responses.

    Implements:
    - Long polling for updates
    - Command handlers (/status, /position, /exit, /help)
    - Inline keyboard buttons for decisions
    - Callback query handling

    Attributes:
        bot_token: Telegram bot token
        chat_id: Authorized chat ID
        poll_interval: Polling interval in seconds
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        poll_interval: int = DEFAULT_POLL_INTERVAL
    ):
        """
        Initialize Telegram bot.

        Args:
            bot_token: Bot token (defaults to env var)
            chat_id: Authorized chat ID (defaults to env var)
            poll_interval: Seconds between polls
        """
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.poll_interval = poll_interval

        if not self.bot_token or not self.chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required")

        self.base_url = f"{TELEGRAM_API}{self.bot_token}"
        self._last_update_id = 0
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None

        # Command handlers registry
        self._command_handlers: Dict[str, Callable] = {}

        # Callback handlers registry
        self._callback_handlers: Dict[str, Callable] = {}

        # Pending responses (waiting for user action)
        self._pending_responses: Dict[int, Dict[str, Any]] = {}

        # Response queue (for main thread to consume)
        self._response_queue: List[UserResponse] = []
        self._queue_lock = threading.Lock()

        # Callback deduplication - track processed callback query IDs
        self._processed_callbacks: List[str] = []
        self._callbacks_lock = threading.Lock()

        # Pending decisions awaiting user response (for timeout tracking)
        self._pending_decisions: Dict[str, PendingDecision] = {}  # key = alert_type

        # Alerts client for sending messages
        self._alerts = get_telegram()

        # Register default commands
        self._register_default_commands()

    # =========================================================================
    # COMMAND REGISTRATION
    # =========================================================================

    def _register_default_commands(self):
        """Register default command handlers."""
        self.register_command("start", self._cmd_start)
        self.register_command("help", self._cmd_help)
        self.register_command("status", self._cmd_status)
        self.register_command("position", self._cmd_position)
        self.register_command("exit", self._cmd_exit)
        self.register_command("hold", self._cmd_hold)
        self.register_command("pnl", self._cmd_pnl)
        self.register_command("market", self._cmd_market)
        self.register_command("cooldown", self._cmd_cooldown)

    def register_command(self, command: str, handler: Callable):
        """
        Register a command handler.

        Args:
            command: Command name (without /)
            handler: Handler function
        """
        self._command_handlers[command.lower()] = handler
        logger.debug(f"Registered command handler: /{command}")

    def register_callback(self, action: str, handler: Callable):
        """
        Register a callback handler for inline buttons.

        Args:
            action: Callback data prefix
            handler: Handler function
        """
        self._callback_handlers[action] = handler
        logger.debug(f"Registered callback handler: {action}")

    # =========================================================================
    # POLLING
    # =========================================================================

    def start_polling(self) -> bool:
        """
        Start polling in a background thread.

        Returns:
            True if started successfully
        """
        if self._running:
            logger.warning("Polling already running")
            return False

        self._running = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="TelegramPollThread",
            daemon=True
        )
        self._poll_thread.start()
        logger.info("Telegram polling started")
        return True

    def stop_polling(self):
        """Stop polling thread."""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
            self._poll_thread = None
        logger.info("Telegram polling stopped")

    def _poll_loop(self):
        """Main polling loop (runs in background thread)."""
        logger.info("Telegram poll loop started")

        while self._running:
            try:
                updates = self._get_updates()

                for update in updates:
                    self._process_update(update)

            except Exception as e:
                logger.error(f"Error in poll loop: {e}")
                time.sleep(self.poll_interval)

            time.sleep(0.1)  # Small delay between polls

    def _get_updates(self) -> List[TelegramUpdate]:
        """
        Get updates from Telegram using long polling.

        Returns:
            List of TelegramUpdate objects
        """
        try:
            url = f"{self.base_url}/getUpdates"
            params = {
                "offset": self._last_update_id + 1,
                "timeout": DEFAULT_TIMEOUT,
                "allowed_updates": ["message", "callback_query"]
            }

            response = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT + 5)

            if response.status_code != 200:
                logger.error(f"getUpdates failed: HTTP {response.status_code}")
                return []

            data = response.json()

            if not data.get("ok"):
                logger.error(f"getUpdates error: {data.get('description')}")
                return []

            updates = []
            for raw_update in data.get("result", []):
                update = self._parse_update(raw_update)
                if update:
                    updates.append(update)
                    self._last_update_id = max(self._last_update_id, update.update_id)

            return updates

        except requests.exceptions.Timeout:
            # Normal for long polling
            return []
        except Exception as e:
            logger.error(f"Error getting updates: {e}")
            return []

    def _parse_update(self, raw: Dict) -> Optional[TelegramUpdate]:
        """Parse raw update into TelegramUpdate."""
        try:
            update_id = raw.get("update_id", 0)

            # Message update
            if "message" in raw:
                msg = raw["message"]
                return TelegramUpdate(
                    update_id=update_id,
                    message_id=msg.get("message_id"),
                    chat_id=msg.get("chat", {}).get("id"),
                    text=msg.get("text"),
                    from_user_id=msg.get("from", {}).get("id"),
                    from_username=msg.get("from", {}).get("username")
                )

            # Callback query update (button press)
            if "callback_query" in raw:
                cb = raw["callback_query"]
                return TelegramUpdate(
                    update_id=update_id,
                    callback_query_id=cb.get("id"),
                    callback_data=cb.get("data"),
                    message_id=cb.get("message", {}).get("message_id"),
                    chat_id=cb.get("message", {}).get("chat", {}).get("id"),
                    from_user_id=cb.get("from", {}).get("id"),
                    from_username=cb.get("from", {}).get("username")
                )

            return None

        except Exception as e:
            logger.error(f"Error parsing update: {e}")
            return None

    def _process_update(self, update: TelegramUpdate):
        """Process a single update."""
        # Verify chat ID for security
        if str(update.chat_id) != str(self.chat_id):
            logger.warning(f"Ignoring update from unauthorized chat: {update.chat_id}")
            return

        # Callback query (button press)
        if update.callback_query_id:
            self._handle_callback(update)
            return

        # Text message
        if update.text:
            self._handle_message(update)

    def _handle_message(self, update: TelegramUpdate):
        """Handle text message."""
        text = update.text.strip()

        # Check for command
        if text.startswith("/"):
            parts = text[1:].split()
            if not parts:
                self._send_reply("Empty command. Type /help for available commands.")
                return
            command = parts[0].lower()
            args = parts[1:] if len(parts) > 1 else []

            handler = self._command_handlers.get(command)
            if handler:
                try:
                    handler(update, args)
                except Exception as e:
                    logger.error(f"Error in command handler /{command}: {e}")
                    self._send_reply("Error processing command. Please try again.")
            else:
                self._send_reply(f"Unknown command: /{command}\n\nType /help for available commands.")

        # Check for text responses (EXIT, HOLD, ADJUST)
        else:
            self._handle_text_response(update, text)

    def _handle_text_response(self, update: TelegramUpdate, text: str):
        """Handle text responses like EXIT, HOLD, ADJUST, ENTER, SKIP."""
        # Normalize to uppercase for case-insensitive matching
        text_upper = text.upper().strip()

        # Handle standard callback actions
        if text_upper in ["EXIT", "HOLD", "ADJUST"]:
            action = CallbackAction[text_upper]
            self._queue_response(action, "text_command")
            self._send_reply(f"Received: {text_upper}\n\nProcessing your request...")

        # Handle ENTER/SKIP responses for pre-entry (write to shared queue)
        # Normalize various natural language inputs to standard responses
        else:
            # Aliases for ENTER (proceed with trade)
            enter_aliases = [
                "ENTER", "YES", "PROCEED", "Y", "OK", "OKAY", "GO", "SURE",
                "DO IT", "DOIT", "TRADE", "EXECUTE", "CONFIRM", "APPROVED",
                "GO AHEAD", "LET'S GO", "LETS GO", "YEP", "YA", "YEAH"
            ]
            # Aliases for SKIP (don't trade)
            skip_aliases = [
                "SKIP", "NO", "N", "NOPE", "CANCEL", "WAIT", "PASS",
                "NO THANKS", "NOTHANKS", "DONT", "DON'T", "STOP", "ABORT",
                "NOT NOW", "NOTNOW", "LATER", "NAH", "NA"
            ]

            normalized_response = None
            if text_upper in enter_aliases:
                normalized_response = "enter"
            elif text_upper in skip_aliases:
                normalized_response = "skip"

            if normalized_response:
                from src.api.response_handler import TelegramResponseHandler, RESPONSE_QUEUE_FILE
                TelegramResponseHandler.write_shared_response(
                    text=normalized_response,
                    chat_id=str(update.chat_id)
                )
                logger.info(f"Text response '{text_upper}' normalized to '{normalized_response}' and queued")
                # Send confirmation to user
                action_text = "ENTER ✅" if normalized_response == "enter" else "SKIP ⏭️"
                self._send_reply(f"✅ Received: {text_upper} → {action_text}")
            # Otherwise ignore non-command messages

    def _is_callback_processed(self, callback_query_id: str) -> bool:
        """
        Check if callback has already been processed (deduplication).

        Args:
            callback_query_id: Telegram callback query ID

        Returns:
            True if already processed
        """
        with self._callbacks_lock:
            if callback_query_id in self._processed_callbacks:
                return True

            # Add to processed list
            self._processed_callbacks.append(callback_query_id)

            # Trim list if too long (keep last N)
            if len(self._processed_callbacks) > MAX_PROCESSED_CALLBACKS:
                self._processed_callbacks = self._processed_callbacks[-MAX_PROCESSED_CALLBACKS:]

            return False

    def _handle_callback(self, update: TelegramUpdate):
        """Handle callback query from inline button."""
        try:
            # Answer callback to stop loading animation
            self._answer_callback(update.callback_query_id)

            # Deduplication check - prevent double-processing
            if self._is_callback_processed(update.callback_query_id):
                logger.debug(f"Ignoring duplicate callback: {update.callback_query_id}")
                return

            data = update.callback_data
            if not data:
                return

            # Parse callback data (format: action:alert_type:position_id)
            parts = data.split(":")
            if not parts or not parts[0]:
                logger.warning(f"Invalid callback data format: {data}")
                return
            action_str = parts[0]
            alert_type = parts[1] if len(parts) > 1 else "unknown"
            position_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None

            # Map to CallbackAction for in-memory queue (keep for backward compat)
            action_map = {
                "hold": CallbackAction.HOLD,
                "exit": CallbackAction.EXIT,
                "adjust": CallbackAction.ADJUST,
                "yes": CallbackAction.CONFIRM_YES,
                "no": CallbackAction.CONFIRM_NO
            }

            action = action_map.get(action_str.lower())
            if action:
                # Write to SHARED file queue (for monitor_workflow to read)
                from src.api.response_handler import TelegramResponseHandler
                TelegramResponseHandler.write_callback(
                    action=action_str.lower(),
                    alert_type=alert_type,
                    position_id=position_id,
                    chat_id=str(update.chat_id)
                )
                logger.info(f"Callback written to shared queue: {action_str}:{alert_type}:{position_id}")

                # Also queue in-memory for backward compatibility
                self._queue_response(action, alert_type, position_id, update.from_user_id)

                # Clear the pending decision for this alert type
                if alert_type in self._pending_decisions:
                    del self._pending_decisions[alert_type]
                    logger.debug(f"Cleared pending decision for {alert_type}")

                # Update message to show selection
                self._edit_message_reply_markup(update.chat_id, update.message_id, None)
                self._send_reply(f"✅ *{action_str.upper()}* received\n\nProcessing...")

        except Exception as e:
            logger.error(f"Error handling callback: {e}")

    def _queue_response(
        self,
        action: CallbackAction,
        alert_type: str,
        position_id: Optional[int] = None,
        user_id: Optional[int] = None
    ):
        """Add response to queue for main thread."""
        response = UserResponse(
            action=action,
            alert_type=alert_type,
            timestamp=datetime.now(),
            position_id=position_id,
            user_id=user_id
        )

        with self._queue_lock:
            self._response_queue.append(response)

        logger.info(f"Queued user response: {action.value} for {alert_type}")

    def get_pending_responses(self) -> List[UserResponse]:
        """
        Get and clear pending responses.

        Called by main thread to check for user actions.

        Returns:
            List of UserResponse objects
        """
        with self._queue_lock:
            responses = self._response_queue.copy()
            self._response_queue.clear()
        return responses

    def has_pending_responses(self) -> bool:
        """Check if there are pending responses."""
        with self._queue_lock:
            return len(self._response_queue) > 0

    # =========================================================================
    # DEFAULT COMMAND HANDLERS
    # =========================================================================

    def _cmd_start(self, update: TelegramUpdate, args: List[str]):
        """Handle /start command."""
        self._send_reply("""🐌 *SNAIL Trading Bot*

Welcome! I'm the SNAIL Iron Fly trading system.

*Commands:*
/status - System & position status
/position - Detailed position info
/pnl - Current P&L summary
/market - NIFTY & VIX data
/cooldown - Show/clear cooldowns
/exit - Request position exit
/hold - Confirm hold position
/help - Show this help

*Quick Actions:*
Type ENTER/SKIP for entry prompts
Type HOLD/EXIT for position alerts

_Buttons work too!_""")

    def _cmd_help(self, update: TelegramUpdate, args: List[str]):
        """Handle /help command."""
        self._cmd_start(update, args)

    def _cmd_status(self, update: TelegramUpdate, args: List[str]):
        """Handle /status command - shows market + position status."""
        try:
            from src.utils.db import get_active_position, get_latest_pnl_snapshot
            from src.utils.helpers import is_market_open

            position = get_active_position()

            if not position:
                market_status = "Open" if is_market_open() else "Closed"
                self._send_reply(f"""📊 *System Status*

• Position: None
• State: {'Waiting for entry' if is_market_open() else 'Idle'}
• Market: {market_status}

_Use /market for NIFTY & VIX data_""")
                return

            # Get P&L snapshot
            snapshot = get_latest_pnl_snapshot(position.id)

            if snapshot:
                pnl_emoji = "🟢" if snapshot.current_pnl >= 0 else "🔴"
                pnl_sign = "+" if snapshot.current_pnl >= 0 else ""

                self._send_reply(f"""📊 *System Status*

• Position: Active Iron Fly
• ATM Strike: {position.atm_strike}
• Wings: ±{position.wing_distance}
• Expiry: {position.expiry_date}

{pnl_emoji} *P&L: {pnl_sign}₹{snapshot.current_pnl:,.0f}* ({snapshot.pnl_percent:+.1f}%)

• NIFTY: ₹{snapshot.nifty_spot:,.0f}
• VIX: {snapshot.vix:.2f}

_Last updated: {snapshot.timestamp.strftime('%H:%M:%S') if snapshot.timestamp else 'N/A'}_""")
            else:
                self._send_reply(f"""📊 *System Status*

• Position: Active Iron Fly
• ATM Strike: {position.atm_strike}
• Wings: ±{position.wing_distance}
• Expiry: {position.expiry_date}

_P&L data pending - monitor will update soon_""")

        except Exception as e:
            logger.error(f"Error in /status: {e}")
            self._send_reply(f"❌ Error fetching status: {str(e)[:100]}")

    def _cmd_position(self, update: TelegramUpdate, args: List[str]):
        """Handle /position command - shows detailed position info."""
        try:
            from src.utils.db import get_active_position, get_position_legs

            position = get_active_position()

            if not position:
                self._send_reply("📍 *No Active Position*\n\n_Waiting for entry opportunity._")
                return

            legs = get_position_legs(position.id)

            # Format legs
            legs_text = ""
            for leg in legs:
                side = "S" if leg.side == "SHORT" else "B"
                legs_text += f"• {side} {leg.tradingsymbol} @ ₹{leg.entry_price:.1f}\n"

            self._send_reply(f"""📍 *Position Details*

*Iron Fly on NIFTY*
• ATM: {position.atm_strike}
• Wings: ±{position.wing_distance}
• Expiry: {position.expiry_date}
• Qty: {position.quantity} ({position.lot_size} per lot)

*Entry:*
• Net Credit: ₹{position.entry_premium:.1f}
• Max Profit: ₹{position.max_profit:,.0f}
• Max Loss: ₹{position.max_loss:,.0f}

*Legs:*
{legs_text}
_Entry: {position.entry_time.strftime('%Y-%m-%d %H:%M') if position.entry_time else 'N/A'}_""")

        except Exception as e:
            logger.error(f"Error in /position: {e}")
            self._send_reply(f"❌ Error: {str(e)[:100]}")

    def _cmd_pnl(self, update: TelegramUpdate, args: List[str]):
        """Handle /pnl command - shows current P&L with real-time quote."""
        try:
            from src.utils.db import get_active_position, get_position_legs, get_latest_pnl_snapshot

            position = get_active_position()

            if not position:
                self._send_reply("💰 *No Active Position*\n\n_No P&L to display._")
                return

            snapshot = get_latest_pnl_snapshot(position.id)

            if snapshot:
                pnl_emoji = "🟢" if snapshot.current_pnl >= 0 else "🔴"
                pnl_sign = "+" if snapshot.current_pnl >= 0 else ""

                # Calculate P&L percentage of max profit/loss
                if snapshot.current_pnl >= 0:
                    pct_of_target = (snapshot.current_pnl / position.max_profit * 100) if position.max_profit else 0
                    target_text = f"{pct_of_target:.1f}% of max profit"
                else:
                    pct_of_max_loss = (abs(snapshot.current_pnl) / position.max_loss * 100) if position.max_loss else 0
                    target_text = f"{pct_of_max_loss:.1f}% of max loss"

                self._send_reply(f"""💰 *P&L Summary*

{pnl_emoji} *Current: {pnl_sign}₹{snapshot.current_pnl:,.0f}*
📊 {snapshot.pnl_percent:+.1f}% ({target_text})

*Targets:*
• Max Profit: ₹{position.max_profit:,.0f}
• Max Loss: ₹{position.max_loss:,.0f}
• Exit at: 50% profit (₹{position.max_profit * 0.5:,.0f})

*Market:*
• NIFTY: ₹{snapshot.nifty_spot:,.0f}
• VIX: {snapshot.vix:.2f}

_Updated: {snapshot.timestamp.strftime('%H:%M:%S') if snapshot.timestamp else 'N/A'}_""")
            else:
                self._send_reply(f"""💰 *P&L Summary*

• Max Profit: ₹{position.max_profit:,.0f}
• Max Loss: ₹{position.max_loss:,.0f}

_Real-time P&L pending - monitor updating..._""")

        except Exception as e:
            logger.error(f"Error in /pnl: {e}")
            self._send_reply(f"❌ Error: {str(e)[:100]}")

    def _cmd_market(self, update: TelegramUpdate, args: List[str]):
        """Handle /market command - shows current market data."""
        try:
            from src.api.kite_client import get_kite_client
            from src.utils.config import load_config

            kite = get_kite_client(load_config())
            nifty = kite.get_nifty_spot()
            vix = kite.get_india_vix()

            if nifty and vix:
                # Determine market sentiment
                if vix < 12:
                    sentiment = "🟢 Low volatility"
                elif vix < 16:
                    sentiment = "🟡 Normal volatility"
                elif vix < 20:
                    sentiment = "🟠 Elevated volatility"
                else:
                    sentiment = "🔴 High volatility"

                self._send_reply(f"""📈 *Market Data*

*NIFTY 50:* ₹{nifty:,.2f}
*India VIX:* {vix:.2f}

{sentiment}

_VIX Range for entry: 10-16_""")
            else:
                self._send_reply("❌ Could not fetch market data. Market may be closed.")

        except Exception as e:
            logger.error(f"Error in /market: {e}")
            self._send_reply(f"❌ Error: {str(e)[:100]}")

    def _cmd_cooldown(self, update: TelegramUpdate, args: List[str]):
        """Handle /cooldown command - shows/clears cooldowns."""
        try:
            from src.utils.db import is_on_cooldown, get_cooldown_remaining, clear_cooldown

            # Check for clear argument
            if args and args[0].lower() == 'clear':
                cooldown_type = args[1] if len(args) > 1 else None
                count = clear_cooldown(cooldown_type)
                if count > 0:
                    self._send_reply(f"✅ Cleared {count} cooldown(s)")
                else:
                    self._send_reply("ℹ️ No active cooldowns to clear")
                return

            # Show cooldown status
            cooldown_types = ['entry', 'user_skip']
            status_lines = []
            any_active = False

            for ct in cooldown_types:
                remaining = get_cooldown_remaining(ct)
                # Escape underscores for Telegram Markdown
                ct_display = ct.replace('_', '\\_')
                if remaining is not None:
                    hours = remaining // 3600
                    minutes = (remaining % 3600) // 60
                    time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
                    status_lines.append(f"• {ct_display}: 🔴 ACTIVE ({time_str} left)")
                    any_active = True
                else:
                    status_lines.append(f"• {ct_display}: 🟢 inactive")

            status_text = "\n".join(status_lines)

            if any_active:
                self._send_reply(f"""⏰ *Cooldown Status*

{status_text}

To clear: /cooldown clear""")
            else:
                self._send_reply(f"""⏰ *Cooldown Status*

{status_text}

✅ No active cooldowns - entry allowed""")

        except Exception as e:
            import traceback
            logger.error(f"Error in /cooldown: {e}")
            logger.error(traceback.format_exc())
            self._send_reply(f"❌ Error: {str(e)[:200]}")

    def _cmd_exit(self, update: TelegramUpdate, args: List[str]):
        """Handle /exit command - confirm and queue exit request."""
        try:
            from src.utils.db import get_active_position

            position = get_active_position()
            if not position:
                self._send_reply("❌ *No Active Position*\n\n_Nothing to exit._")
                return

            # Send confirmation with inline keyboard
            self.send_with_keyboard(
                f"""⚠️ *Confirm Exit?*

Position: Iron Fly @ {position.atm_strike}
Expiry: {position.expiry_date}

This will close all 4 legs at market.""",
                [
                    [
                        {"text": "✅ Yes, Exit", "callback_data": f"yes:exit_confirm:{position.id}"},
                        {"text": "❌ Cancel", "callback_data": "no:exit_confirm"}
                    ]
                ]
            )

        except Exception as e:
            logger.error(f"Error in /exit: {e}")
            self._send_reply(f"❌ Error: {str(e)[:100]}")

    def _cmd_hold(self, update: TelegramUpdate, args: List[str]):
        """Handle /hold command - confirms hold decision."""
        from src.api.response_handler import TelegramResponseHandler
        TelegramResponseHandler.write_callback("hold", "manual_hold")
        self._send_reply("✋ *Hold confirmed* - monitoring continues")

    # =========================================================================
    # INLINE KEYBOARD METHODS
    # =========================================================================

    def send_with_keyboard(
        self,
        message: str,
        keyboard: List[List[Dict[str, str]]],
        parse_mode: str = "Markdown"
    ) -> Optional[int]:
        """
        Send message with inline keyboard.

        Args:
            message: Message text
            keyboard: Inline keyboard layout
            parse_mode: Message format

        Returns:
            Message ID if successful
        """
        try:
            url = f"{self.base_url}/sendMessage"

            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode,
                "reply_markup": {
                    "inline_keyboard": keyboard
                }
            }

            response = requests.post(url, json=payload, timeout=10)

            if response.status_code != 200:
                logger.error(f"sendMessage failed: HTTP {response.status_code}")
                return None

            data = response.json()
            if data.get("ok"):
                return data.get("result", {}).get("message_id")

            logger.error(f"sendMessage error: {data.get('description')}")
            return None

        except Exception as e:
            logger.error(f"Error sending keyboard message: {e}")
            return None

    def send_stop_loss_decision(
        self,
        current_pnl: float,
        loss_percent: float,
        claude_advice: str,
        position_id: int
    ) -> Optional[int]:
        """
        Send stop loss alert with HOLD/EXIT buttons.

        Args:
            current_pnl: Current P&L amount
            loss_percent: Percentage of max loss
            claude_advice: Claude's recommendation
            position_id: Position ID for callback

        Returns:
            Message ID if successful
        """
        message = f"""⚠️ *STOP LOSS: {loss_percent:.0f}% of Max*

P&L: *-₹{abs(current_pnl):,.0f}*

📋 {claude_advice}"""

        keyboard = [
            [
                {"text": "✋ HOLD", "callback_data": f"hold:stop_loss:{position_id}"},
                {"text": "🚪 EXIT", "callback_data": f"exit:stop_loss:{position_id}"},
                {"text": "🔧 ADJUST", "callback_data": f"adjust:stop_loss:{position_id}"}
            ]
        ]

        message_id = self.send_with_keyboard(message, keyboard)

        # Track pending decision for timeout handling
        if message_id:
            self._pending_decisions["stop_loss"] = PendingDecision(
                alert_type="stop_loss",
                position_id=position_id,
                message_id=message_id,
                created_at=datetime.now()
            )

        return message_id

    def send_wing_approach_decision(
        self,
        direction: str,
        proximity_percent: float,
        claude_advice: str,
        position_id: int
    ) -> Optional[int]:
        """
        Send wing approach alert with decision buttons.

        Args:
            direction: Wing direction (CE/PE)
            proximity_percent: Distance to wing as percentage
            claude_advice: Claude's recommendation
            position_id: Position ID

        Returns:
            Message ID if successful
        """
        message = f"""⚠️ *WING: {proximity_percent:.0f}% to {direction.upper()}*

📋 {claude_advice}"""

        keyboard = [
            [
                {"text": "✋ HOLD", "callback_data": f"hold:wing_approach:{position_id}"},
                {"text": "🚪 EXIT", "callback_data": f"exit:wing_approach:{position_id}"},
                {"text": "🔧 ADJUST", "callback_data": f"adjust:wing_approach:{position_id}"}
            ]
        ]

        message_id = self.send_with_keyboard(message, keyboard)

        # Track pending decision for timeout handling
        if message_id:
            self._pending_decisions["wing_approach"] = PendingDecision(
                alert_type="wing_approach",
                position_id=position_id,
                message_id=message_id,
                created_at=datetime.now()
            )

        return message_id

    def send_friday_decision(
        self,
        current_pnl: float,
        dte: int,
        claude_advice: str,
        position_id: int
    ) -> Optional[int]:
        """
        Send Friday exit decision with buttons.

        Args:
            current_pnl: Current P&L
            dte: Days to expiry
            claude_advice: Claude's recommendation
            position_id: Position ID

        Returns:
            Message ID if successful
        """
        emoji = "🟢" if current_pnl >= 0 else "🔴"
        pnl_sign = "+" if current_pnl >= 0 else ""

        message = f"""📅 *FRIDAY* - {emoji} P&L *{pnl_sign}₹{current_pnl:,.0f}* | DTE {dte}

📋 {claude_advice}"""

        keyboard = [
            [
                {"text": "✋ HOLD", "callback_data": f"hold:friday:{position_id}"},
                {"text": "🚪 EXIT", "callback_data": f"exit:friday:{position_id}"}
            ]
        ]

        message_id = self.send_with_keyboard(message, keyboard)

        # Track pending decision for timeout handling
        if message_id:
            self._pending_decisions["friday"] = PendingDecision(
                alert_type="friday",
                position_id=position_id,
                message_id=message_id,
                created_at=datetime.now()
            )

        return message_id

    def send_vix_warning_decision(
        self,
        current_vix: float,
        claude_advice: str,
        position_id: int
    ) -> Optional[int]:
        """
        Send VIX warning with decision buttons.

        Args:
            current_vix: Current VIX level
            claude_advice: Claude's recommendation
            position_id: Position ID

        Returns:
            Message ID if successful
        """
        message = f"""⚠️ *VIX: {current_vix:.1f}* - Hard exit at 20

📋 {claude_advice}"""

        keyboard = [
            [
                {"text": "✋ HOLD", "callback_data": f"hold:vix_warning:{position_id}"},
                {"text": "🚪 EXIT", "callback_data": f"exit:vix_warning:{position_id}"}
            ]
        ]

        message_id = self.send_with_keyboard(message, keyboard)

        # Track pending decision for timeout handling
        if message_id:
            self._pending_decisions["vix_warning"] = PendingDecision(
                alert_type="vix_warning",
                position_id=position_id,
                message_id=message_id,
                created_at=datetime.now()
            )

        return message_id

    # =========================================================================
    # TIMEOUT HANDLING
    # =========================================================================

    def check_pending_timeouts(self) -> List[PendingDecision]:
        """
        Check for timed out pending decisions.

        Returns list of expired decisions and handles them by:
        - Logging the timeout
        - Sending a notification to user
        - Removing keyboard from original message
        - Continuing to hold (per configuration)

        Returns:
            List of expired PendingDecision objects
        """
        expired = []

        for alert_type, decision in list(self._pending_decisions.items()):
            if decision.is_expired():
                expired.append(decision)

                # Log the timeout
                logger.warning(
                    f"Decision timeout for {alert_type} "
                    f"(position_id={decision.position_id}, "
                    f"waited {RESPONSE_TIMEOUT_MINUTES} minutes)"
                )

                # Send notification about timeout
                self._send_reply(
                    f"⏰ *Decision Timeout*\n\n"
                    f"No response received for *{alert_type.replace('_', ' ').title()}* alert "
                    f"after {RESPONSE_TIMEOUT_MINUTES} minutes.\n\n"
                    f"*Action: Continuing to HOLD position*\n\n"
                    f"_Send /status to check current position._"
                )

                # Remove keyboard from original message
                self._edit_message_reply_markup(
                    self.chat_id,
                    decision.message_id,
                    None
                )

                # Remove from pending
                del self._pending_decisions[alert_type]

        return expired

    def get_pending_decisions(self) -> Dict[str, PendingDecision]:
        """Get all pending decisions awaiting response."""
        return self._pending_decisions.copy()

    def has_pending_decision(self, alert_type: str) -> bool:
        """Check if there's a pending decision of given type."""
        return alert_type in self._pending_decisions

    def clear_pending_decision(self, alert_type: str) -> bool:
        """
        Clear a pending decision (e.g., when handled externally).

        Returns:
            True if decision was cleared
        """
        if alert_type in self._pending_decisions:
            del self._pending_decisions[alert_type]
            return True
        return False

    # =========================================================================
    # UTILITY METHODS
    # =========================================================================

    def _send_reply(self, message: str, parse_mode: str = "Markdown") -> bool:
        """Send a simple reply message."""
        return self._alerts.send(message, parse_mode)

    def _answer_callback(self, callback_query_id: str, text: str = None):
        """Answer a callback query to stop loading animation."""
        try:
            url = f"{self.base_url}/answerCallbackQuery"
            payload = {"callback_query_id": callback_query_id}
            if text:
                payload["text"] = text

            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            logger.error(f"Error answering callback: {e}")

    def _edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        keyboard: Optional[List[List[Dict]]]
    ):
        """Edit message to update/remove inline keyboard."""
        try:
            url = f"{self.base_url}/editMessageReplyMarkup"
            payload = {
                "chat_id": chat_id,
                "message_id": message_id
            }

            if keyboard:
                payload["reply_markup"] = {"inline_keyboard": keyboard}
            else:
                payload["reply_markup"] = {"inline_keyboard": []}

            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            logger.error(f"Error editing message markup: {e}")

    @property
    def is_running(self) -> bool:
        """Check if polling is running."""
        return self._running


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_bot: Optional[TelegramBot] = None


def get_telegram_bot() -> TelegramBot:
    """Get or create Telegram bot singleton."""
    global _bot
    if _bot is None:
        _bot = TelegramBot()
    return _bot


def reset_telegram_bot() -> None:
    """Reset singleton (for testing)."""
    global _bot
    if _bot and _bot.is_running:
        _bot.stop_polling()
    _bot = None


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    import sys
    from dotenv import load_dotenv
    from pathlib import Path

    PROJECT_ROOT = Path(__file__).parent.parent.parent
    load_dotenv(PROJECT_ROOT / '.env')

    print("\n" + "=" * 60)
    print("SNAIL Telegram Bot Test (Polling)")
    print("=" * 60)

    try:
        bot = TelegramBot()

        print("\n[1] Testing bot info...")
        alerts = get_telegram()
        bot_info = alerts.get_bot_info()
        if bot_info:
            print(f"    Bot: @{bot_info.get('username')}")
        else:
            print("    Failed to get bot info")
            sys.exit(1)

        print("\n[2] Starting polling...")
        bot.start_polling()
        print("    Polling started. Send commands to your bot!")
        print("    Press Ctrl+C to stop.\n")

        # Keep running and check for responses
        try:
            while True:
                responses = bot.get_pending_responses()
                for resp in responses:
                    print(f"    Received: {resp.action.value} for {resp.alert_type}")
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\n[3] Stopping...")
            bot.stop_polling()

        print("\n" + "=" * 60)
        print("Bot test complete!")
        print("=" * 60 + "\n")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
