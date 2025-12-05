"""
SNAIL Telegram Alerts

Telegram messaging for trading alerts and notifications.

@file        telegram_alerts.py
@description Telegram HTTP-based messaging client
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 7
"""

import os
from datetime import datetime
from typing import Optional, Dict, Any, List
import requests
from loguru import logger


# =============================================================================
# MARKDOWN ESCAPING
# =============================================================================

def escape_markdown(text: str) -> str:
    """
    Escape special characters for Telegram Markdown.

    Telegram Markdown v1 special characters: _ * ` [

    Args:
        text: Text to escape

    Returns:
        Escaped text safe for Markdown
    """
    if not text:
        return ""

    # Escape special Markdown characters
    escape_chars = ['_', '*', '`', '[']
    for char in escape_chars:
        text = text.replace(char, f'\\{char}')

    return text


# =============================================================================
# EXCEPTIONS
# =============================================================================

class TelegramError(Exception):
    """Exception raised for Telegram API errors."""
    pass


# =============================================================================
# TELEGRAM ALERTS CLIENT
# =============================================================================

class TelegramAlerts:
    """
    Send alerts via Telegram using direct HTTP.

    Uses requests library for direct HTTP calls to api.telegram.org.
    No external Telegram library needed.

    Attributes:
        bot_token: Telegram bot token
        chat_id: Target chat ID
    """

    TELEGRAM_API = "https://api.telegram.org/bot"
    MAX_MESSAGE_LENGTH = 4096
    DEFAULT_TIMEOUT = 10

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None
    ):
        """
        Initialize Telegram client.

        Args:
            bot_token: Bot token (defaults to env var)
            chat_id: Chat ID (defaults to env var)

        Raises:
            ValueError: If credentials are missing or invalid
        """
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")

        if not self.bot_token or not self.chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required")

        if ":" not in self.bot_token:
            raise ValueError("Invalid bot token format (missing ':')")

    def send(
        self,
        message: str,
        parse_mode: Optional[str] = "Markdown",
        disable_notification: bool = False
    ) -> bool:
        """
        Send a message via Telegram.

        Args:
            message: Message text
            parse_mode: "Markdown" or "HTML" (optional)
            disable_notification: Silent message

        Returns:
            True if sent successfully
        """
        try:
            url = f"{self.TELEGRAM_API}{self.bot_token}/sendMessage"

            # Truncate if too long
            if len(message) > self.MAX_MESSAGE_LENGTH:
                message = message[:self.MAX_MESSAGE_LENGTH - 20] + "\n...[truncated]"

            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "disable_notification": disable_notification
            }

            if parse_mode:
                payload["parse_mode"] = parse_mode

            response = requests.post(url, json=payload, timeout=self.DEFAULT_TIMEOUT)

            if response.status_code != 200:
                logger.error(f"Telegram error: HTTP {response.status_code}")
                return False

            data = response.json()
            if not data.get("ok"):
                logger.error(f"Telegram API error: {data.get('description')}")
                return False

            logger.debug(f"Message sent: {message[:50]}...")
            return True

        except requests.exceptions.Timeout:
            logger.error("Telegram request timed out")
            return False
        except Exception as e:
            logger.error(f"Telegram error: {e}")
            return False

    def send_with_retry(
        self,
        message: str,
        max_retries: int = 3,
        parse_mode: Optional[str] = "Markdown"
    ) -> bool:
        """
        Send message with retry logic.

        Args:
            message: Message text
            max_retries: Maximum retry attempts
            parse_mode: Message format

        Returns:
            True if sent successfully
        """
        for attempt in range(max_retries):
            if self.send(message, parse_mode):
                return True
            logger.warning(f"Telegram send attempt {attempt + 1} failed, retrying...")

        logger.error(f"Failed to send message after {max_retries} attempts")
        return False

    # =========================================================================
    # SPECIALIZED ALERTS
    # =========================================================================

    def send_entry_alert(
        self,
        atm_strike: int,
        wing_distance: int,
        expiry: str,
        lot_size: int,
        premium: Dict[str, float],
        max_profit: float,
        max_loss: float
    ) -> bool:
        """Send trade entry alert."""
        net_credit = premium.get('net', 0)

        message = f"""🟢 *TRADE ENTRY*

*Iron Fly Opened*

📊 *Position Details*
• ATM Strike: {atm_strike}
• Wings: ±{wing_distance}
• Expiry: {expiry}
• Lot Size: {lot_size}

💰 *Premium*
• Short CE: ₹{premium.get('short_ce', 0):.2f}
• Short PE: ₹{premium.get('short_pe', 0):.2f}
• Long CE: -₹{premium.get('long_ce', 0):.2f}
• Long PE: -₹{premium.get('long_pe', 0):.2f}
• *Net Credit: ₹{net_credit:.2f}*

⚙️ *Risk*
• Max Profit: ₹{max_profit:,.2f}
• Max Loss: ₹{max_loss:,.2f}

_Time: {datetime.now().strftime('%H:%M:%S')}_"""

        return self.send(message)

    def send_exit_alert(
        self,
        exit_reason: str,
        net_pnl: float,
        pnl_percent: float,
        entry_time: datetime,
        exit_time: datetime
    ) -> bool:
        """Send trade exit alert."""
        emoji = "🟢" if net_pnl >= 0 else "🔴"
        pnl_sign = "+" if net_pnl >= 0 else ""
        duration = exit_time - entry_time

        message = f"""{emoji} *TRADE EXIT*

*Position Closed*

📊 *Result*
• Net P&L: {pnl_sign}₹{net_pnl:,.2f} ({pnl_sign}{pnl_percent:.1f}%)
• Exit Reason: {exit_reason.replace('_', ' ').title()}
• Duration: {duration}

_Time: {exit_time.strftime('%H:%M:%S')}_"""

        return self.send(message)

    def send_pnl_update(
        self,
        current_pnl: float,
        pnl_percent: float,
        nifty_spot: float,
        vix: float,
        max_profit: float,
        max_loss: float
    ) -> bool:
        """Send P&L update alert."""
        emoji = "🟢" if current_pnl >= 0 else "🔴"
        pnl_sign = "+" if current_pnl >= 0 else ""

        if current_pnl >= 0:
            pct_of_max = (current_pnl / max_profit) * 100 if max_profit > 0 else 0
            progress_text = f"{pct_of_max:.1f}% of max profit"
        else:
            pct_of_max = (abs(current_pnl) / max_loss) * 100 if max_loss > 0 else 0
            progress_text = f"{pct_of_max:.1f}% of max loss"

        message = f"""{emoji} *P&L Update*

• Current P&L: {pnl_sign}₹{current_pnl:,.2f} ({pnl_sign}{pnl_percent:.1f}%)
• Progress: {progress_text}
• NIFTY: {nifty_spot:,.2f}
• VIX: {vix:.2f}

_Time: {datetime.now().strftime('%H:%M:%S')}_"""

        return self.send(message)

    def send_stop_loss_alert(
        self,
        current_pnl: float,
        loss_pct_of_max: float,
        nifty_spot: float,
        distance_to_wing: float,
        claude_advice: Optional[str] = None
    ) -> bool:
        """Send stop loss advisory alert."""
        advice_section = ""
        if claude_advice:
            advice_section = f"""
📋 *Claude Analysis:*
{claude_advice}

_Reply with: EXIT, HOLD, or ADJUST_
"""
        else:
            advice_section = "\n_Awaiting Claude analysis..._\n"

        message = f"""⚠️ *STOP LOSS ADVISORY*

🔴 *Position at {loss_pct_of_max:.0f}% of Max Loss*

• Current P&L: -₹{abs(current_pnl):,.2f}
• NIFTY: {nifty_spot:,.2f}
• Distance to Wing: {distance_to_wing:.0f} pts
{advice_section}
_Time: {datetime.now().strftime('%H:%M:%S')}_"""

        return self.send(message)

    def send_wing_approach_alert(
        self,
        direction: str,
        wing_proximity: float,
        distance_to_wing: float,
        nifty_spot: float,
        claude_advice: Optional[str] = None
    ) -> bool:
        """Send wing approach alert."""
        advice_section = ""
        if claude_advice:
            advice_section = f"""
📋 *Claude Analysis:*
{claude_advice}

_Reply with: EXIT, HOLD, or ADJUST_
"""
        else:
            advice_section = "\n_Awaiting Claude analysis..._\n"

        message = f"""⚠️ *WING APPROACH*

📍 *{wing_proximity:.0f}% toward {direction.upper()} wing*

• NIFTY: {nifty_spot:,.2f}
• Distance to Wing: {distance_to_wing:.0f} pts
{advice_section}
_Time: {datetime.now().strftime('%H:%M:%S')}_"""

        return self.send(message)

    def send_vix_warning(
        self,
        current_vix: float,
        vix_change: float,
        claude_advice: Optional[str] = None
    ) -> bool:
        """Send VIX warning alert."""
        direction = "↑" if vix_change > 0 else "↓"

        advice_section = ""
        if claude_advice:
            advice_section = f"""
📋 *Claude Analysis:*
{claude_advice}

_Reply with: EXIT or HOLD_
"""
        else:
            advice_section = "\n_Awaiting Claude analysis..._\n"

        message = f"""⚠️ *VIX WARNING*

📈 *VIX in Warning Zone (16.5-20)*

• Current VIX: {current_vix:.2f} {direction} ({vix_change:+.2f})
• Hard Exit Threshold: 20
{advice_section}
_Time: {datetime.now().strftime('%H:%M:%S')}_"""

        return self.send(message)

    def send_vix_hard_exit(self, current_vix: float) -> bool:
        """Send VIX hard exit alert."""
        message = f"""🚨 *VIX HARD EXIT TRIGGERED*

❌ *VIX > 20 - Auto-Exit Executing*

• Current VIX: {current_vix:.2f}
• Threshold: 20.0

Closing position immediately...

_Time: {datetime.now().strftime('%H:%M:%S')}_"""

        return self.send(message)

    def send_gap_alert(
        self,
        gap_percent: float,
        gap_direction: str,
        previous_close: float,
        day_open: float,
        is_beyond_wing: bool,
        claude_advice: Optional[str] = None
    ) -> bool:
        """Send gap detection alert."""
        priority = "🚨 CRITICAL" if is_beyond_wing else "⚠️ SIGNIFICANT"
        status = "BEYOND WING - MAX LOSS" if is_beyond_wing else "Within position range"

        message = f"""{priority}: *GAP AT OPEN*

📊 *Gap Details*
• Previous Close: {previous_close:,.2f}
• Today's Open: {day_open:,.2f}
• Gap: {gap_percent:.1%} {gap_direction.upper()}

⚠️ *Position Status: {status}*"""

        if claude_advice:
            message += f"""

📋 *Claude Analysis:*
{claude_advice}

_Reply with: EXIT or HOLD_"""

        message += f"""

_Time: {datetime.now().strftime('%H:%M:%S')}_"""

        return self.send(message)

    def send_friday_decision(
        self,
        current_pnl: float,
        pnl_percent: float,
        dte: int,
        weekend_events: str,
        claude_advice: str
    ) -> bool:
        """Send Friday exit decision alert."""
        emoji = "🟢" if current_pnl >= 0 else "🔴"
        pnl_sign = "+" if current_pnl >= 0 else ""

        message = f"""📅 *FRIDAY DECISION*

{emoji} *Current P&L: {pnl_sign}₹{current_pnl:,.2f} ({pnl_sign}{pnl_percent:.1f}%)*

• Days to Expiry: {dte}
• Weekend Events: {weekend_events or 'None known'}

📋 *Claude Analysis:*
{claude_advice}

_Reply with: EXIT or HOLD_

_Time: {datetime.now().strftime('%H:%M:%S')}_"""

        return self.send(message)

    def send_error_alert(
        self,
        error: str,
        module: str,
        function: str
    ) -> bool:
        """Send error alert."""
        message = f"""🚨 *SNAIL ERROR*

⚠️ *Error Detected*

```
Error: {error[:200]}
Module: {module}
Function: {function}
```

_Time: {datetime.now().strftime('%H:%M:%S')}_"""

        return self.send(message)

    def send_daily_summary(
        self,
        date_str: str,
        total_pnl: float,
        trades: int,
        has_position: bool,
        position_status: Optional[str] = None
    ) -> bool:
        """Send daily summary alert."""
        emoji = "🟢" if total_pnl >= 0 else "🔴"
        pnl_sign = "+" if total_pnl >= 0 else ""

        message = f"""📊 *SNAIL Daily Summary*
_{date_str}_

{emoji} *Day P&L: {pnl_sign}₹{total_pnl:,.2f}*

• Trades: {trades}
• Position: {'Active' if has_position else 'None'}"""

        if position_status:
            message += f"\n• Status: {position_status}"

        return self.send(message)

    def send_morning_summary(
        self,
        nifty_spot: float,
        vix: float,
        has_position: bool,
        entry_conditions: Dict[str, bool],
        margin_available: float
    ) -> bool:
        """Send morning startup summary."""
        vix_ok = "✅" if entry_conditions.get('vix_ok') else "❌"
        dte_ok = "✅" if entry_conditions.get('dte_ok') else "❌"
        cooldown_ok = "✅" if entry_conditions.get('cooldown_ok') else "❌"
        margin_ok = "✅" if entry_conditions.get('margin_ok') else "❌"

        message = f"""🌅 *SNAIL Morning Summary*

📈 *Market Status*
• NIFTY: {nifty_spot:,.2f}
• VIX: {vix:.2f}

💰 *Account*
• Margin Available: ₹{margin_available:,.0f}

📋 *Entry Conditions*
• VIX (10-16): {vix_ok}
• DTE ≥ 6: {dte_ok}
• Cooldown: {cooldown_ok}
• Margin: {margin_ok}

📍 *Position: {'Active' if has_position else 'None'}*

_Time: {datetime.now().strftime('%H:%M:%S')}_"""

        return self.send(message)

    # =========================================================================
    # UTILITY METHODS
    # =========================================================================

    def get_bot_info(self) -> Optional[Dict]:
        """Get bot info to verify token is valid."""
        try:
            url = f"{self.TELEGRAM_API}{self.bot_token}/getMe"
            response = requests.get(url, timeout=self.DEFAULT_TIMEOUT)

            if response.status_code != 200:
                return None

            data = response.json()
            if data.get("ok"):
                return data.get("result")
            return None

        except Exception:
            return None

    def is_valid(self) -> bool:
        """Check if bot credentials are valid."""
        return self.get_bot_info() is not None


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_telegram: Optional[TelegramAlerts] = None


def get_telegram() -> TelegramAlerts:
    """Get or create Telegram alerts singleton."""
    global _telegram
    if _telegram is None:
        _telegram = TelegramAlerts()
    return _telegram


def send_alert(message: str, parse_mode: str = "Markdown") -> bool:
    """Convenience function for sending alerts."""
    return get_telegram().send(message, parse_mode)


def reset_telegram() -> None:
    """Reset singleton (for testing)."""
    global _telegram
    _telegram = None


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
    print("SNAIL Telegram Alerts Test")
    print("=" * 60)

    try:
        telegram = TelegramAlerts()

        # Test bot info
        print("\n[1] Getting bot info...")
        bot_info = telegram.get_bot_info()
        if bot_info:
            print(f"    Bot: @{bot_info.get('username')} ({bot_info.get('first_name')})")
        else:
            print("    Failed to get bot info")
            sys.exit(1)

        # Test simple message
        print("\n[2] Sending test message...")
        result = telegram.send(f"🐌 SNAIL Test Message\n\nTime: {datetime.now()}")
        print(f"    Result: {'Success' if result else 'Failed'}")

        # Test formatted message
        print("\n[3] Sending formatted message...")
        result = telegram.send("""*SNAIL Test*

📊 *Market Data*
• NIFTY: `24150`
• VIX: `14.5`

_Formatted message test_""")
        print(f"    Result: {'Success' if result else 'Failed'}")

        print("\n" + "=" * 60)
        print("Telegram test complete! Check your chat.")
        print("=" * 60 + "\n")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
