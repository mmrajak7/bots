"""
Telegram alerts for ConvictBar forward test.
Sends real-time notifications for signals, entries, exits.
"""

import logging
from datetime import datetime
from typing import Optional
from pathlib import Path
import json

try:
    import requests
except ImportError:
    requests = None

from ..core.bar import Bar
from ..core.signals import Direction, PendingOrder, Position
from ..backtest.simulator import TradeResult, ExitReason


# Paths
SCRIPT_DIR = Path(__file__).resolve().parent
CONVICTBAR_DIR = SCRIPT_DIR.parent.parent
BOTS_DIR = CONVICTBAR_DIR.parent
DATA_DIR = BOTS_DIR / 'data'
TELEGRAM_CONFIG_FILE = DATA_DIR / 'telegram_config.json'

logger = logging.getLogger(__name__)


class TelegramAlerts:
    """
    Sends trading alerts to Telegram.

    Alert types:
    - CONVICTION_DETECTED: Signal bar identified
    - ENTRY_SIGNAL: Follow-through confirmed, order placed
    - ENTRY_FILLED: Position opened
    - TRAILING_UPDATE: Stop loss moved
    - EXIT: Position closed (SL/TGT/Time/FT_Failure)
    - DAILY_SUMMARY: End of day summary
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        enabled: bool = True,
    ):
        """
        Initialize Telegram alerts.

        Args:
            bot_token: Telegram bot token (loads from config if None)
            chat_id: Telegram chat ID (loads from config if None)
            enabled: Enable/disable alerts
        """
        if requests is None:
            logger.warning("requests library not installed, alerts disabled")
            self.enabled = False
            return

        self.enabled = enabled
        self.bot_token = bot_token
        self.chat_id = chat_id

        # Load from config if not provided
        if (self.bot_token is None or self.chat_id is None) and TELEGRAM_CONFIG_FILE.exists():
            try:
                with open(TELEGRAM_CONFIG_FILE) as f:
                    config = json.load(f)
                    self.bot_token = self.bot_token or config.get('bot_token')
                    self.chat_id = self.chat_id or config.get('chat_id')
            except Exception as e:
                logger.error(f"Failed to load Telegram config: {e}")

        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram credentials not configured, alerts disabled")
            self.enabled = False

    def _send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """
        Send message to Telegram.

        Args:
            text: Message text
            parse_mode: HTML or Markdown

        Returns:
            True if sent successfully
        """
        if not self.enabled:
            return False

        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            response = requests.post(url, json=payload, timeout=10)

            if response.status_code == 200:
                return True
            else:
                logger.error(f"Telegram API error: {response.status_code} - {response.text}")
                return False

        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    def _format_direction(self, direction: Direction) -> str:
        """Format direction with emoji."""
        if direction == Direction.LONG:
            return "🟢 LONG"
        else:
            return "🔴 SHORT"

    def _format_price(self, price: float) -> str:
        """Format price with 2 decimals."""
        return f"{price:,.2f}"

    def _format_pnl(self, pnl: float) -> str:
        """Format PnL with color indicator."""
        if pnl >= 0:
            return f"+{pnl:,.2f} pts ✅"
        else:
            return f"{pnl:,.2f} pts ❌"

    def send_conviction_detected(
        self,
        bar: Bar,
        conviction_type: str,
        details: str = "",
    ) -> bool:
        """
        Alert for conviction bar detection.

        Args:
            bar: The conviction bar
            conviction_type: BULLISH or BEARISH
            details: Additional details
        """
        direction_emoji = "🟢" if conviction_type == "BULLISH" else "🔴"

        message = f"""
<b>📊 CONVICTION DETECTED</b>

{direction_emoji} <b>{conviction_type}</b>

<b>Bar Details:</b>
• Time: {bar.datetime.strftime('%H:%M')}
• O: {self._format_price(bar.open)}
• H: {self._format_price(bar.high)}
• L: {self._format_price(bar.low)}
• C: {self._format_price(bar.close)}
• Range: {bar.range:.2f} pts
• Body Ratio: {bar.body_ratio:.1%}
• Close Position: {bar.close_position:.1%}

<i>Awaiting follow-through confirmation...</i>
"""
        return self._send_message(message.strip())

    def send_entry_signal(
        self,
        order: PendingOrder,
        ft_confirmation: str = "",
    ) -> bool:
        """
        Alert for entry signal (follow-through confirmed).

        Args:
            order: The pending order
            ft_confirmation: Confirmation level (STRONG/MODERATE)
        """
        direction = self._format_direction(order.direction)
        risk = abs(order.entry_price - order.stop_loss)
        reward = abs(order.target - order.entry_price)
        rr_ratio = reward / risk if risk > 0 else 0

        message = f"""
<b>🎯 ENTRY SIGNAL</b>

{direction}

<b>Order Details:</b>
• Entry: {self._format_price(order.entry_price)}
• Stop Loss: {self._format_price(order.stop_loss)}
• Target: {self._format_price(order.target)}
• Risk: {risk:.2f} pts
• Reward: {reward:.2f} pts
• R:R: 1:{rr_ratio:.2f}

<b>Signal Bar:</b>
• Time: {order.signal_bar.datetime.strftime('%H:%M')}
• Range: {order.signal_bar.range:.2f} pts

<b>FT Confirmation:</b> {ft_confirmation}

<i>Order valid for {order.valid_until_bar - order.created_at_bar} bars</i>
"""
        return self._send_message(message.strip())

    def send_entry_filled(
        self,
        position: Position,
        fill_bar: Bar,
    ) -> bool:
        """
        Alert for position entry.

        Args:
            position: The opened position
            fill_bar: Bar where entry was filled
        """
        direction = self._format_direction(position.direction)
        risk = abs(position.entry_price - position.initial_stop_loss)

        message = f"""
<b>✅ ENTRY FILLED</b>

{direction}

<b>Position Details:</b>
• Entry Price: {self._format_price(position.entry_price)}
• Stop Loss: {self._format_price(position.current_stop_loss)}
• Target: {self._format_price(position.target)}
• Risk: {risk:.2f} pts

<b>Fill Time:</b> {fill_bar.datetime.strftime('%H:%M')}
<b>Fill Bar:</b> O:{self._format_price(fill_bar.open)} H:{self._format_price(fill_bar.high)} L:{self._format_price(fill_bar.low)} C:{self._format_price(fill_bar.close)}

<i>Position active, monitoring for exit...</i>
"""
        return self._send_message(message.strip())

    def send_trailing_update(
        self,
        position: Position,
        old_sl: float,
        new_sl: float,
        current_price: float,
        reason: str = "",
    ) -> bool:
        """
        Alert for trailing stop update.

        Args:
            position: Current position
            old_sl: Previous stop loss
            new_sl: New stop loss
            current_price: Current market price
            reason: Reason for update (BE/TRAIL)
        """
        direction = self._format_direction(position.direction)

        # Calculate locked profit
        if position.direction == Direction.LONG:
            locked_profit = new_sl - position.entry_price
        else:
            locked_profit = position.entry_price - new_sl

        message = f"""
<b>🔄 TRAILING STOP UPDATE</b>

{direction}

<b>Stop Loss Moved:</b>
• Old SL: {self._format_price(old_sl)}
• New SL: {self._format_price(new_sl)}
• Reason: {reason}

<b>Current Status:</b>
• Entry: {self._format_price(position.entry_price)}
• Current Price: {self._format_price(current_price)}
• Locked Profit: {locked_profit:+.2f} pts
"""
        return self._send_message(message.strip())

    def send_exit(
        self,
        result: TradeResult,
        exit_bar: Bar,
    ) -> bool:
        """
        Alert for position exit.

        Args:
            result: Trade result
            exit_bar: Bar where exit occurred
        """
        direction = self._format_direction(result.direction)

        # Exit reason emoji
        reason_emoji = {
            ExitReason.STOP_LOSS: "🛑",
            ExitReason.TARGET: "🎯",
            ExitReason.TIME_EXIT: "⏰",
            ExitReason.FT_FAILURE: "❌",
        }.get(result.exit_reason, "📤")

        # Result emoji
        result_emoji = "✅" if result.pnl_points >= 0 else "❌"

        message = f"""
<b>{reason_emoji} POSITION CLOSED</b>

{direction}

<b>Exit Details:</b>
• Exit Reason: {result.exit_reason.value}
• Exit Price: {self._format_price(result.exit_price)}
• Exit Time: {exit_bar.datetime.strftime('%H:%M')}

<b>Trade Result:</b>
• Entry: {self._format_price(result.entry_price)}
• Exit: {self._format_price(result.exit_price)}
• P&L: {self._format_pnl(result.pnl_points)} {result_emoji}
• Duration: {result.bars_held} bars

<b>Stop Loss:</b> {self._format_price(result.stop_loss)}
<b>Target:</b> {self._format_price(result.target)}
"""
        return self._send_message(message.strip())

    def send_order_expired(
        self,
        order: PendingOrder,
        current_bar: Bar,
    ) -> bool:
        """
        Alert for expired order.

        Args:
            order: The expired order
            current_bar: Current bar
        """
        direction = self._format_direction(order.direction)

        message = f"""
<b>⏳ ORDER EXPIRED</b>

{direction}

<b>Order Details:</b>
• Entry Level: {self._format_price(order.entry_price)}
• Not reached within {order.valid_until_bar - order.created_at_bar} bars

<b>Current Market:</b>
• Price: {self._format_price(current_bar.close)}
• Time: {current_bar.datetime.strftime('%H:%M')}

<i>Looking for next signal...</i>
"""
        return self._send_message(message.strip())

    def send_trade_skipped(
        self,
        reason: str,
        bar: Bar,
    ) -> bool:
        """
        Alert for skipped trade.

        Args:
            reason: Skip reason
            bar: Current bar
        """
        message = f"""
<b>⚠️ TRADE SKIPPED</b>

<b>Reason:</b> {reason}
<b>Time:</b> {bar.datetime.strftime('%H:%M')}

<i>Risk management limit reached</i>
"""
        return self._send_message(message.strip())

    def send_day_skipped(
        self,
        reason: str,
        bar: Bar,
    ) -> bool:
        """
        Alert for skipped trading day.

        Args:
            reason: Skip reason (gap, Bar 1 filter, etc.)
            bar: First bar of day
        """
        message = f"""
<b>🚫 DAY SKIPPED</b>

<b>Date:</b> {bar.datetime.strftime('%Y-%m-%d')}
<b>Reason:</b> {reason}

<i>No trades today</i>
"""
        return self._send_message(message.strip())

    def send_daily_summary(
        self,
        date: datetime,
        trades: list,
        total_pnl: float,
        signals_detected: int = 0,
        orders_expired: int = 0,
    ) -> bool:
        """
        Send end of day summary.

        Args:
            date: Trading date
            trades: List of trade results
            total_pnl: Total P&L for the day
            signals_detected: Number of signals
            orders_expired: Number of expired orders
        """
        wins = sum(1 for t in trades if t.pnl_points > 0)
        losses = sum(1 for t in trades if t.pnl_points < 0)

        result_emoji = "✅" if total_pnl >= 0 else "❌"

        # Trade details
        trade_lines = []
        for t in trades:
            dir_emoji = "🟢" if t.direction == Direction.LONG else "🔴"
            pnl_emoji = "✅" if t.pnl_points >= 0 else "❌"
            trade_lines.append(
                f"• {dir_emoji} {t.exit_reason.value}: {t.pnl_points:+.2f} pts {pnl_emoji}"
            )

        trades_text = "\n".join(trade_lines) if trade_lines else "No trades"

        message = f"""
<b>📊 DAILY SUMMARY</b>

<b>Date:</b> {date.strftime('%Y-%m-%d (%A)')}

<b>Statistics:</b>
• Signals Detected: {signals_detected}
• Orders Expired: {orders_expired}
• Trades Executed: {len(trades)}
• Wins: {wins}
• Losses: {losses}

<b>Trades:</b>
{trades_text}

<b>Day P&L:</b> {self._format_pnl(total_pnl)} {result_emoji}
"""
        return self._send_message(message.strip())

    def send_startup(self, tokens: list) -> bool:
        """
        Send startup notification.

        Args:
            tokens: List of subscribed instrument tokens
        """
        message = f"""
<b>🚀 CONVICTBAR STARTED</b>

<b>Mode:</b> Forward Test (Paper Trading)
<b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
<b>Subscribed Tokens:</b> {', '.join(map(str, tokens))}

<i>Monitoring for conviction bars...</i>
"""
        return self._send_message(message.strip())

    def send_shutdown(self, reason: str = "Normal shutdown") -> bool:
        """
        Send shutdown notification.

        Args:
            reason: Shutdown reason
        """
        message = f"""
<b>🛑 CONVICTBAR STOPPED</b>

<b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
<b>Reason:</b> {reason}
"""
        return self._send_message(message.strip())

    def send_error(self, error: str, context: str = "") -> bool:
        """
        Send error notification.

        Args:
            error: Error message
            context: Additional context
        """
        message = f"""
<b>⚠️ ERROR</b>

<b>Message:</b> {error}
<b>Context:</b> {context}
<b>Time:</b> {datetime.now().strftime('%H:%M:%S')}
"""
        return self._send_message(message.strip())

    def send_connection_status(self, connected: bool, details: str = "") -> bool:
        """
        Send connection status update.

        Args:
            connected: Connection status
            details: Additional details
        """
        if connected:
            message = f"""
<b>🟢 CONNECTED</b>

WebSocket connection established.
{details}
"""
        else:
            message = f"""
<b>🔴 DISCONNECTED</b>

WebSocket connection lost.
{details}

<i>Attempting reconnection...</i>
"""
        return self._send_message(message.strip())
