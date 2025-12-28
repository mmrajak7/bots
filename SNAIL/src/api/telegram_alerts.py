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
from pathlib import Path
from typing import Optional, Dict, List, Union
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

            # Truncate if too long (log warning so we know important info may be lost)
            if len(message) > self.MAX_MESSAGE_LENGTH:
                logger.warning(f"Telegram message truncated from {len(message)} to {self.MAX_MESSAGE_LENGTH} chars")
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

            logger.info(f"Telegram message sent successfully ({len(message)} chars)")
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

    def send_photo(
        self,
        photo_path: Union[str, Path],
        caption: Optional[str] = None,
        parse_mode: Optional[str] = "Markdown"
    ) -> bool:
        """
        Send a photo via Telegram.

        Args:
            photo_path: Path to the image file
            caption: Optional caption for the photo
            parse_mode: "Markdown" or "HTML" (optional)

        Returns:
            True if sent successfully
        """
        try:
            photo_path = Path(photo_path)
            if not photo_path.exists():
                logger.error(f"Photo file not found: {photo_path}")
                return False

            if not photo_path.is_file():
                logger.error(f"Photo path is not a file: {photo_path}")
                return False

            url = f"{self.TELEGRAM_API}{self.bot_token}/sendPhoto"

            data = {"chat_id": self.chat_id}
            if caption:
                data["caption"] = caption
            if parse_mode:
                data["parse_mode"] = parse_mode

            with open(photo_path, 'rb') as photo_file:
                files = {"photo": photo_file}
                response = requests.post(
                    url,
                    data=data,
                    files=files,
                    timeout=30  # Longer timeout for file upload
                )

            if response.status_code != 200:
                logger.error(f"Telegram photo error: HTTP {response.status_code}")
                return False

            result = response.json()
            if not result.get("ok"):
                logger.error(f"Telegram API error: {result.get('description')}")
                return False

            logger.debug(f"Photo sent: {photo_path.name}")
            return True

        except requests.exceptions.Timeout:
            logger.error("Telegram photo upload timed out")
            return False
        except Exception as e:
            logger.error(f"Telegram photo error: {e}")
            return False

    def send_photos(
        self,
        photo_paths: List[Union[str, Path]],
        captions: Optional[List[Optional[str]]] = None
    ) -> int:
        """
        Send multiple photos via Telegram.

        Args:
            photo_paths: List of paths to image files
            captions: Optional list of captions (one per photo)

        Returns:
            Number of photos successfully sent
        """
        sent_count = 0
        final_captions: List[Optional[str]] = captions if captions else [None] * len(photo_paths)

        for photo_path, caption in zip(photo_paths, final_captions):
            if self.send_photo(photo_path, caption):
                sent_count += 1

        logger.info(f"Sent {sent_count}/{len(photo_paths)} photos to Telegram")
        return sent_count

    def test_connection(self) -> bool:
        """
        Test Telegram connection by calling getMe API.

        Returns:
            True if connection works
        """
        try:
            url = f"{self.TELEGRAM_API}{self.bot_token}/getMe"
            response = requests.get(url, timeout=self.DEFAULT_TIMEOUT)

            if response.status_code == 200:
                data = response.json()
                if data.get("ok"):
                    logger.debug(f"Telegram connected: @{data['result'].get('username')}")
                    return True

            return False
        except Exception as e:
            logger.error(f"Telegram connection test failed: {e}")
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
        logger.info(f"Sending entry alert: ATM={atm_strike}, Wings={wing_distance}, "
                   f"Expiry={expiry}, MaxProfit={max_profit:.0f}, MaxLoss={max_loss:.0f}")
        net_credit = premium.get('net', 0)

        # Calculate values
        short_ce = premium.get('short_ce', 0)
        short_pe = premium.get('short_pe', 0)
        long_ce = premium.get('long_ce', 0)
        long_pe = premium.get('long_pe', 0)
        straddle_total = short_ce + short_pe
        wings_total = long_ce + long_pe

        # Calculate lots (NIFTY lot size is 75)
        nifty_lot_size = 75
        num_lots = lot_size // nifty_lot_size if lot_size >= nifty_lot_size else 1

        message = f"""🦋 *ENTRY: Iron Fly*

🎯 ATM {atm_strike} | 🪽 Wings ±{wing_distance} | 📅 {expiry} | 📦 {num_lots} lot ({lot_size} qty)

💰 *Premium (per share):*
• SELL Straddle: ₹{short_ce:.2f} + ₹{short_pe:.2f} = ₹{straddle_total:.2f}
• BUY Wings: ₹{long_ce:.2f} + ₹{long_pe:.2f} = ₹{wings_total:.2f}
• *Net Credit: ₹{net_credit:.2f}*

✅ Max Profit: ₹{max_profit:,.0f} | ⛔ Max Loss: ₹{max_loss:,.0f}"""

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
        emoji = "💚" if net_pnl >= 0 else "❌"
        result_emoji = "🎉" if net_pnl >= 0 else "😔"
        pnl_sign = "+" if net_pnl >= 0 else ""
        duration = exit_time - entry_time
        hours = int(duration.total_seconds() // 3600)
        mins = int((duration.total_seconds() % 3600) // 60)

        message = f"""🚪 *EXIT: {exit_reason.replace('_', ' ').title()}* {result_emoji}

{emoji} P&L: *{pnl_sign}₹{net_pnl:,.0f}* ({pnl_sign}{pnl_percent:.1f}%)
⏱️ Duration: {hours}h {mins}m"""

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
        emoji = "📈" if current_pnl >= 0 else "📉"
        pnl_sign = "+" if current_pnl >= 0 else ""

        if current_pnl >= 0:
            pct_of_max = (current_pnl / max_profit) * 100 if max_profit > 0 else 0
            progress_text = f"✅ {pct_of_max:.0f}% of max profit"
        else:
            pct_of_max = (abs(current_pnl) / max_loss) * 100 if max_loss > 0 else 0
            progress_text = f"⚠️ {pct_of_max:.0f}% of max loss"

        message = f"""{emoji} *P&L: {pnl_sign}₹{current_pnl:,.0f}* ({pnl_sign}{pnl_percent:.1f}%) - {progress_text}

📊 NIFTY {nifty_spot:,.0f} | 🌡️ VIX {vix:.1f}"""

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
        message = f"""🛑 *STOP LOSS: {loss_pct_of_max:.0f}% of Max Loss*

💸 P&L: *-₹{abs(current_pnl):,.0f}* | 📊 NIFTY {nifty_spot:,.0f} | 🪽 Wing {distance_to_wing:.0f}pts away"""

        if claude_advice:
            message += f"""

🤖 *Claude:* {claude_advice}

⌨️ _Reply: EXIT / HOLD / ADJUST_"""

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
        dir_emoji = "⬆️" if direction.upper() == "CE" else "⬇️"
        message = f"""🪽 *WING APPROACH: {wing_proximity:.0f}% to {direction.upper()}* {dir_emoji}

📊 NIFTY {nifty_spot:,.0f} | 📏 {distance_to_wing:.0f}pts to wing"""

        if claude_advice:
            message += f"""

🤖 *Claude:* {claude_advice}

⌨️ _Reply: EXIT / HOLD / ADJUST_"""

        return self.send(message)

    def send_vix_warning(
        self,
        current_vix: float,
        vix_change: float,
        claude_advice: Optional[str] = None
    ) -> bool:
        """Send VIX warning alert."""
        direction = "📈" if vix_change > 0 else "📉"

        message = f"""🌡️ *VIX WARNING: {current_vix:.1f}* {direction} ({vix_change:+.1f})

🚨 Hard exit at 20"""

        if claude_advice:
            message += f"""

🤖 *Claude:* {claude_advice}

⌨️ _Reply: EXIT / HOLD_"""

        return self.send(message)

    def send_vix_hard_exit(self, current_vix: float) -> bool:
        """Send VIX hard exit alert."""
        message = f"""🚨🚨🚨 *VIX HARD EXIT* 🚨🚨🚨

🌡️ VIX {current_vix:.1f} > 20 threshold breached!

⚡ Auto-closing position..."""

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
        priority = "🚨" if is_beyond_wing else "⚠️"
        dir_emoji = "⬆️" if gap_direction.upper() == "UP" else "⬇️"
        status = "❌ BEYOND WING" if is_beyond_wing else "✅ Within range"

        message = f"""{priority} *GAP {gap_direction.upper()}* {dir_emoji} *{gap_percent:.1%}* - {status}

📊 Prev {previous_close:,.0f} → Open {day_open:,.0f}"""

        if claude_advice:
            message += f"""

🤖 *Claude:* {claude_advice}

⌨️ _Reply: EXIT / HOLD_"""

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
        emoji = "💚" if current_pnl >= 0 else "❌"
        pnl_sign = "+" if current_pnl >= 0 else ""
        events = weekend_events if weekend_events else "None"

        message = f"""📅 *FRIDAY DECISION* 🤔

{emoji} P&L: *{pnl_sign}₹{current_pnl:,.0f}* ({pnl_sign}{pnl_percent:.1f}%)
⏳ DTE {dte} | 📰 Events: {events}

🤖 *Claude:* {claude_advice}

⌨️ _Reply: EXIT / HOLD_"""

        return self.send(message)

    def send_error_alert(
        self,
        error: str,
        module: str,
        function: str
    ) -> bool:
        """Send error alert."""
        message = f"""🐌❌ *SNAIL ERROR*

📍 `{module}.{function}`
💥 {error[:150]}"""

        return self.send(message)

    def send_daily_summary(
        self,
        date_str: str,
        total_pnl: float,
        has_position: bool,
        entry_date: Optional[str] = None,
        exit_date: Optional[str] = None
    ) -> bool:
        """Send daily summary alert."""
        emoji = "💚" if total_pnl >= 0 else "❌"
        result_emoji = "🎉" if total_pnl > 0 else ("😐" if total_pnl == 0 else "😔")
        pnl_sign = "+" if total_pnl >= 0 else ""
        pos_text = '🟢 Active' if has_position else '⚪ None'

        message = f"""🐌 *SNAIL Daily Summary* 📊

📅 {date_str}
{emoji} P&L: *{pnl_sign}₹{total_pnl:,.0f}* {result_emoji}
🦋 Position: {pos_text}"""

        if entry_date:
            message += f"\n📥 Entry: {entry_date}"
        if exit_date:
            message += f"\n📤 Exit: {exit_date}"

        return self.send(message)

    def send_friday_charts(
        self,
        chart_paths: Dict[str, Path],
        total_pnl: float,
        total_trades: int,
        win_rate: float
    ) -> int:
        """
        Send Friday weekly performance charts.

        Args:
            chart_paths: Dictionary with chart type to file path mapping
            total_pnl: Total P&L amount
            total_trades: Total number of trades
            win_rate: Win rate percentage

        Returns:
            Number of charts successfully sent
        """
        pnl_sign = "+" if total_pnl >= 0 else ""
        emoji = "🎉" if total_pnl > 0 else ("😐" if total_pnl == 0 else "😔")

        # Send header message first
        header = f"""📊 *SNAIL Friday Report* {emoji}

💰 Total P&L: *{pnl_sign}₹{total_pnl:,.0f}*
📈 Trades: {total_trades} | Win Rate: {win_rate:.1f}%

_Charts below:_"""

        self.send(header)

        # Chart captions
        captions = {
            'cumulative_pnl': '📈 *Cumulative P&L*',
            'drawdown': '📉 *Drawdown*',
            'capital_growth': '💹 *Capital Growth*',
            'monthly_metrics': '📅 *Monthly Metrics*',
            'yearly_metrics': '📊 *Yearly Metrics*'
        }

        sent_count = 0
        for chart_type, path in chart_paths.items():
            if path and path.exists():
                caption = captions.get(chart_type, chart_type.replace('_', ' ').title())
                if self.send_photo(path, caption):
                    sent_count += 1

        logger.info(f"Friday charts sent: {sent_count}/{len(chart_paths)}")
        return sent_count

    def send_morning_summary(
        self,
        nifty_spot: float,
        vix: float,
        has_position: bool,
        entry_conditions: Dict[str, bool],
        margin_available: float,
        events_summary: str = "",
        news_summary: str = ""
    ) -> bool:
        """Send morning startup summary with events and news."""
        vix_ok = "✅" if entry_conditions.get('vix_ok') else "❌"
        dte_ok = "✅" if entry_conditions.get('dte_ok') else "❌"
        cooldown_ok = "✅" if entry_conditions.get('cooldown_ok') else "❌"
        margin_ok = "✅" if entry_conditions.get('margin_ok') else "❌"
        pos_emoji = "🟢" if has_position else "⚪"

        message = f"""🐌 *SNAIL Morning* ☀️

📊 NIFTY {nifty_spot:,.0f} | 🌡️ VIX {vix:.1f}
💰 Zerodha Balance: ₹{margin_available:,.0f}

*Entry Checklist:*
{vix_ok} VIX | {dte_ok} DTE | {cooldown_ok} Cooldown | {margin_ok} Margin

🦋 Position: *{pos_emoji} {'Active' if has_position else 'None'}*"""

        # Add events if available
        if events_summary:
            message += f"\n\n📅 *Events (10d):*\n{events_summary}"

        # Add news if available
        if news_summary:
            message += f"\n\n📰 *News:*\n{news_summary}"

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

        except Exception as e:
            logger.debug(f"Failed to get bot info: {e}")
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
