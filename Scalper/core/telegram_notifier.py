"""
NEO Trade Terminal - Telegram Notifier

Sends trade notifications to Telegram.
"""

import requests
from typing import Optional, Dict, Any
from datetime import datetime
import threading
import logging

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends notifications to Telegram."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        tg_config = config.get('telegram', {})
        self.enabled = tg_config.get('enabled', False)
        self.bot_token = tg_config.get('bot_token', '')
        self.chat_id = tg_config.get('chat_id', '')
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"

        # Alert mode: "all" = every notification, "eod_only" = only EOD summary + critical
        self.alert_mode = tg_config.get('alert_mode', 'all')

        self._session = requests.Session()
        self._timeout = 5

    def _should_send_trade_alert(self) -> bool:
        """Check if individual trade alerts should be sent based on alert_mode."""
        return self.alert_mode != 'eod_only'

    def send(self, message: str, parse_mode: str = 'HTML'):
        """
        Send message to Telegram (non-blocking).

        Args:
            message: Message text
            parse_mode: HTML or Markdown
        """
        if not self.enabled or not self.bot_token or not self.chat_id:
            return

        threading.Thread(
            target=self._send_message,
            args=(message, parse_mode),
            daemon=True
        ).start()

    def _send_message(self, message: str, parse_mode: str):
        """Internal message sender."""
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': parse_mode
            }
            response = self._session.post(url, json=payload, timeout=self._timeout)
            if not response.ok:
                logger.warning(f"[TG] Send failed: {response.text}")
        except Exception as e:
            logger.warning(f"[TG] Error: {e}")

    def notify_order_placed(self, symbol: str, action: str, qty: int,
                           price: float, order_id: str):
        """
        Notify order placement.

        Args:
            symbol: Trading symbol
            action: B or S
            qty: Quantity
            price: Order price
            order_id: Order ID
        """
        if not self._should_send_trade_alert():
            return

        emoji = "🟢" if action == 'B' else "🔴"
        action_text = "BUY" if action == 'B' else "SELL"

        msg = f"""
{emoji} <b>ORDER PLACED</b>
━━━━━━━━━━━━━━━━━
Symbol: <code>{symbol}</code>
Action: {action_text}
Qty: {qty}
Price: {price:.2f}
Order ID: {order_id}
Time: {datetime.now().strftime('%H:%M:%S')}
"""
        self.send(msg)

    def notify_order_filled(self, symbol: str, action: str, qty: int,
                           fill_price: float, order_id: str):
        """
        Notify order fill.

        Args:
            symbol: Trading symbol
            action: B or S
            qty: Quantity
            fill_price: Fill price
            order_id: Order ID
        """
        if not self._should_send_trade_alert():
            return

        action_text = "BOUGHT" if action == 'B' else "SOLD"

        msg = f"""
✅ <b>ORDER FILLED</b>
━━━━━━━━━━━━━━━━━
Symbol: <code>{symbol}</code>
{action_text} {qty} @ {fill_price:.2f}
Order ID: {order_id}
Time: {datetime.now().strftime('%H:%M:%S')}
"""
        self.send(msg)

    def notify_order_rejected(self, symbol: str, action: str,
                             reason: str, order_id: str):
        """
        Notify order rejection.

        Args:
            symbol: Trading symbol
            action: B or S
            reason: Rejection reason
            order_id: Order ID
        """
        if not self._should_send_trade_alert():
            return

        msg = f"""
❌ <b>ORDER REJECTED</b>
━━━━━━━━━━━━━━━━━
Symbol: <code>{symbol}</code>
Action: {'BUY' if action == 'B' else 'SELL'}
Reason: {reason}
Order ID: {order_id}
"""
        self.send(msg)

    def notify_sl_hit(self, symbol: str, exit_price: float,
                     pnl: float, pnl_pct: float):
        """
        Notify SL hit.

        Args:
            symbol: Trading symbol
            exit_price: Exit price
            pnl: P&L amount
            pnl_pct: P&L percentage
        """
        if not self._should_send_trade_alert():
            return

        msg = f"""
🛑 <b>STOP LOSS HIT</b>
━━━━━━━━━━━━━━━━━
Symbol: <code>{symbol}</code>
Exit Price: {exit_price:.2f}
P&L: {pnl:,.0f} ({pnl_pct:+.1f}%)
Time: {datetime.now().strftime('%H:%M:%S')}
"""
        self.send(msg)

    def notify_target_hit(self, symbol: str, exit_price: float,
                         pnl: float, pnl_pct: float):
        """
        Notify target hit.

        Args:
            symbol: Trading symbol
            exit_price: Exit price
            pnl: P&L amount
            pnl_pct: P&L percentage
        """
        if not self._should_send_trade_alert():
            return

        msg = f"""
🎯 <b>TARGET HIT</b>
━━━━━━━━━━━━━━━━━
Symbol: <code>{symbol}</code>
Exit Price: {exit_price:.2f}
P&L: {pnl:,.0f} ({pnl_pct:+.1f}%)
Time: {datetime.now().strftime('%H:%M:%S')}
"""
        self.send(msg)

    def notify_position_exit(self, symbol: str, qty: int,
                            avg_price: float, exit_price: float,
                            pnl: float):
        """
        Notify position exit.

        Args:
            symbol: Trading symbol
            qty: Position quantity
            avg_price: Average entry price
            exit_price: Exit price
            pnl: P&L amount
        """
        if not self._should_send_trade_alert():
            return

        emoji = "💰" if pnl >= 0 else "📉"

        msg = f"""
{emoji} <b>POSITION CLOSED</b>
━━━━━━━━━━━━━━━━━
Symbol: <code>{symbol}</code>
Qty: {qty}
Avg: {avg_price:.2f}
Exit: {exit_price:.2f}
P&L: {pnl:,.0f}
Time: {datetime.now().strftime('%H:%M:%S')}
"""
        self.send(msg)

    def notify_daily_summary(self, total_trades: int, winners: int,
                            losers: int, total_pnl: float):
        """
        Send daily trading summary.

        Args:
            total_trades: Total trades count
            winners: Winning trades count
            losers: Losing trades count
            total_pnl: Total P&L
        """
        pnl_emoji = "💚" if total_pnl >= 0 else "❤️"
        win_rate = (winners / total_trades * 100) if total_trades > 0 else 0

        msg = f"""
📊 <b>DAILY SUMMARY - NEO</b>
━━━━━━━━━━━━━━━━━
Total Trades: {total_trades}
Winners: {winners} ✅
Losers: {losers} ❌
Win Rate: {win_rate:.1f}%

{pnl_emoji} <b>Net P&L: {total_pnl:,.0f}</b>

Date: {datetime.now().strftime('%d-%b-%Y')}
"""
        self.send(msg)

    def notify_circuit_breaker(self, current_loss: float, limit: float):
        """
        Notify when circuit breaker triggers.

        Args:
            current_loss: Current loss amount
            limit: Loss limit
        """
        msg = f"""
🚨 <b>CIRCUIT BREAKER TRIGGERED</b>
━━━━━━━━━━━━━━━━━
Current Loss: {abs(current_loss):,.0f}
Daily Limit: {limit:,.0f}

⚠️ <b>TRADING HALTED</b>
New orders blocked until tomorrow.
"""
        self.send(msg)

    def notify_trail_update(self, symbol: str, old_sl: float,
                           new_sl: float, reason: str):
        """
        Notify SL trail update.

        Args:
            symbol: Trading symbol
            old_sl: Old SL price
            new_sl: New SL price
            reason: Trail reason
        """
        if not self._should_send_trade_alert():
            return

        msg = f"""
🔄 <b>SL TRAILED</b>
━━━━━━━━━━━━━━━━━
Symbol: <code>{symbol}</code>
SL: {old_sl:.2f} → {new_sl:.2f}
Reason: {reason}
Time: {datetime.now().strftime('%H:%M:%S')}
"""
        self.send(msg)

    def notify_error(self, error_type: str, message: str):
        """
        Notify error.

        Args:
            error_type: Type of error
            message: Error message
        """
        msg = f"""
⚠️ <b>ERROR: {error_type}</b>
━━━━━━━━━━━━━━━━━
{message}
Time: {datetime.now().strftime('%H:%M:%S')}
"""
        self.send(msg)

    def test(self) -> bool:
        """
        Test Telegram connection.

        Returns:
            True if successful
        """
        try:
            url = f"{self.base_url}/getMe"
            response = self._session.get(url, timeout=self._timeout)
            if response.ok:
                data = response.json()
                if data.get('ok'):
                    logger.info(f"[TG] Connected as: {data['result'].get('username')}")
                    return True
            return False
        except Exception as e:
            logger.error(f"[TG] Test failed: {e}")
            return False

    def toggle(self, enabled: bool):
        """
        Enable/disable notifications.

        Args:
            enabled: True to enable
        """
        self.enabled = enabled
        logger.info(f"[TG] Notifications {'enabled' if enabled else 'disabled'}")
