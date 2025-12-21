"""
Telegram Alerts for Expiry Trading System

Notifications for entry, exit, P&L updates, and warnings.

@file        telegram_alerts.py
@description Telegram messaging for expiry day trading
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Union
import requests  # type: ignore[import-untyped]
from loguru import logger

from src.state_manager import Position
from src.position_manager import PnLSnapshot, ExitReason


class TelegramAlerts:
    """
    Send alerts via Telegram for expiry trading.

    Handles:
    - Entry notifications with leg details
    - P&L updates with charts
    - Wing approach warnings
    - VIX spike alerts
    - Exit notifications with summary
    """

    API_URL = "https://api.telegram.org/bot"
    MAX_MESSAGE_LENGTH = 4096
    DEFAULT_TIMEOUT = 10

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Telegram client.

        Args:
            config: Configuration with telegram section
        """
        telegram_config = config.get('telegram', {})

        # Get tokens from config or environment
        self.bot_token = telegram_config.get('bot_token', '')
        self.chat_id = telegram_config.get('chat_id', '')

        # Replace env var placeholders
        if self.bot_token.startswith('${'):
            env_var = self.bot_token[2:-1]
            self.bot_token = os.getenv(env_var, '')

        if self.chat_id.startswith('${'):
            env_var = self.chat_id[2:-1]
            self.chat_id = os.getenv(env_var, '')

        self.mode = config.get('mode', 'paper')
        self.instrument = config.get('instrument', 'NIFTY')

        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram credentials not configured")

    def _send(
        self,
        message: str,
        parse_mode: str = "Markdown"
    ) -> bool:
        """
        Send message to Telegram.

        Args:
            message: Message text
            parse_mode: Message format

        Returns:
            True if sent successfully
        """
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram not configured, skipping message")
            return False

        try:
            url = f"{self.API_URL}{self.bot_token}/sendMessage"

            if len(message) > self.MAX_MESSAGE_LENGTH:
                message = message[:self.MAX_MESSAGE_LENGTH - 20] + "\n...[truncated]"

            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode
            }

            response = requests.post(url, json=payload, timeout=self.DEFAULT_TIMEOUT)

            if response.status_code != 200:
                logger.error(f"Telegram error: HTTP {response.status_code}")
                return False

            data = response.json()
            if not data.get("ok"):
                logger.error(f"Telegram API error: {data.get('description')}")
                return False

            return True

        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False

    def send_photo(
        self,
        photo_path: Union[str, Path],
        caption: Optional[str] = None
    ) -> bool:
        """
        Send photo to Telegram.

        Args:
            photo_path: Path to image file
            caption: Optional caption

        Returns:
            True if sent successfully
        """
        if not self.bot_token or not self.chat_id:
            return False

        try:
            photo_path = Path(photo_path)
            if not photo_path.exists():
                logger.error(f"Photo not found: {photo_path}")
                return False

            url = f"{self.API_URL}{self.bot_token}/sendPhoto"

            data = {"chat_id": self.chat_id}
            if caption:
                data["caption"] = caption
                data["parse_mode"] = "Markdown"

            with open(photo_path, 'rb') as photo:
                files = {"photo": photo}
                response = requests.post(url, data=data, files=files, timeout=30)

            if response.status_code != 200:
                logger.error(f"Telegram photo error: HTTP {response.status_code}")
                return False

            return response.json().get("ok", False)

        except Exception as e:
            logger.error(f"Telegram photo error: {e}")
            return False

    def send_entry_alert(
        self,
        position: Position,
        setup_details: Dict[str, Any]
    ) -> bool:
        """
        Send entry notification.

        Args:
            position: Position details
            setup_details: Strategy setup details

        Returns:
            True if sent
        """
        mode_tag = "[PAPER]" if self.mode == 'paper' else "[LIVE]"

        # Build leg details
        legs_text = ""
        for leg in position.legs:
            action = "SELL" if leg.transaction_type == 'SELL' else "BUY"
            legs_text += f"  {action} {leg.symbol} @ {leg.entry_price:.2f}\n"

        message = f"""🎯 *EXPIRY IC ENTRY* {mode_tag}

📅 *{self.instrument}* | {datetime.now().strftime('%d %b %Y')}

📊 *Market:*
  NIFTY: {position.spot_at_entry:,.0f}
  VIX: {position.vix_at_entry:.1f}

🦋 *Iron Condor Setup:*
  ATM Strike: {position.legs[0].strike:,.0f}
  Wing Distance: {position.wing_distance} pts
  Lots: {position.num_lots} ({position.num_lots * position.lot_size} qty)

📝 *Legs:*
{legs_text}
💰 *Premium:*
  Total Credit: {position.total_credit:.2f} pts
  Credit Value: Rs.{position.total_credit * position.num_lots * position.lot_size:,.0f}

🎯 Target: Rs.{position.target_pnl:,.0f} ({self.mode.upper()})
⛔ Stop Loss: Rs.{position.stop_loss_pnl:,.0f}
⏰ Hard Exit: 3:15 PM"""

        return self._send(message)

    def send_pnl_update(
        self,
        position: Position,
        snapshot: PnLSnapshot,
        chart_path: Optional[Path] = None
    ) -> bool:
        """
        Send P&L update with optional chart.

        Args:
            position: Position details
            snapshot: Current P&L snapshot
            chart_path: Path to P&L chart image

        Returns:
            True if sent
        """
        pnl_emoji = "📈" if snapshot.mtm_pnl >= 0 else "📉"
        pnl_sign = "+" if snapshot.mtm_pnl >= 0 else ""

        # Progress bar
        progress = min(max(snapshot.pnl_pct, 0), 100)
        filled = int(progress / 10)
        bar = "█" * filled + "░" * (10 - filled)

        # Wing distances
        ce_warning = "⚠️" if snapshot.distance_to_ce_wing < position.wing_distance * 0.3 else ""
        pe_warning = "⚠️" if snapshot.distance_to_pe_wing < position.wing_distance * 0.3 else ""

        caption = f"""{pnl_emoji} *EXPIRY IC P&L UPDATE*

💰 *P&L: {pnl_sign}Rs.{snapshot.mtm_pnl:,.0f}*
[{bar}] {snapshot.pnl_pct:.0f}% of target

📊 NIFTY: {snapshot.spot:,.0f} | VIX: {snapshot.vix:.1f}
⏰ Time: {snapshot.timestamp}

🪽 *Wing Status:*
  CE Wing: {snapshot.distance_to_ce_wing:.0f} pts away {ce_warning}
  PE Wing: {snapshot.distance_to_pe_wing:.0f} pts away {pe_warning}

🎯 Target: Rs.{position.target_pnl:,.0f}
⛔ SL: Rs.{position.stop_loss_pnl:,.0f}"""

        if chart_path and chart_path.exists():
            return self.send_photo(chart_path, caption)
        else:
            return self._send(caption)

    def send_wing_approach_alert(
        self,
        direction: str,
        proximity_pct: float,
        distance: float,
        spot: float
    ) -> bool:
        """
        Send wing approach warning.

        Args:
            direction: CE or PE
            proximity_pct: Proximity percentage
            distance: Distance to wing in points
            spot: Current spot price

        Returns:
            True if sent
        """
        dir_emoji = "⬆️" if direction == 'CE' else "⬇️"

        message = f"""⚠️ *WING APPROACH WARNING* {dir_emoji}

🪽 *{direction} Wing* at {proximity_pct:.0f}% proximity

📊 NIFTY: {spot:,.0f}
📏 Distance to wing: {distance:.0f} pts

_Monitor closely for potential breach_"""

        return self._send(message)

    def send_vix_spike_alert(
        self,
        current_vix: float,
        entry_vix: float,
        change: float
    ) -> bool:
        """
        Send VIX spike alert.

        Args:
            current_vix: Current VIX
            entry_vix: VIX at entry
            change: VIX change

        Returns:
            True if sent
        """
        message = f"""🌡️ *VIX SPIKE ALERT*

📈 VIX: {entry_vix:.1f} → {current_vix:.1f} (+{change:.1f})

_Increased volatility detected. Wing breaches more likely._"""

        return self._send(message)

    def send_exit_alert(
        self,
        position: Position,
        reason: ExitReason,
        final_pnl: float,
        gross_pnl: float,
        charges: float
    ) -> bool:
        """
        Send exit notification with summary.

        Args:
            position: Position details
            reason: Exit reason
            final_pnl: Net P&L after charges
            gross_pnl: P&L before charges
            charges: Transaction charges

        Returns:
            True if sent
        """
        # Result emoji
        if final_pnl > 0:
            result_emoji = "🎉"
            pnl_emoji = "💚"
        elif final_pnl == 0:
            result_emoji = "😐"
            pnl_emoji = "⚪"
        else:
            result_emoji = "😔"
            pnl_emoji = "❌"

        pnl_sign = "+" if final_pnl >= 0 else ""

        # Duration
        try:
            entry_time = datetime.strptime(position.entry_time, "%H:%M:%S")
            exit_time = datetime.now()
            entry_time = entry_time.replace(
                year=exit_time.year, month=exit_time.month, day=exit_time.day
            )
            duration = exit_time - entry_time
            hours = int(duration.total_seconds() // 3600)
            mins = int((duration.total_seconds() % 3600) // 60)
            duration_str = f"{hours}h {mins}m"
        except ValueError:
            duration_str = "N/A"

        # Reason text
        reason_text = {
            ExitReason.TARGET_HIT: "🎯 Target Hit",
            ExitReason.STOP_LOSS: "⛔ Stop Loss",
            ExitReason.TIME_EXIT: "⏰ Time Exit (3:15 PM)",
            ExitReason.MANUAL: "👤 Manual Exit",
            ExitReason.ERROR: "❌ Error Exit"
        }.get(reason, reason.value)

        mode_tag = "[PAPER]" if self.mode == 'paper' else "[LIVE]"

        message = f"""🚪 *EXPIRY IC EXIT* {result_emoji} {mode_tag}

📋 *Reason:* {reason_text}

{pnl_emoji} *Net P&L: {pnl_sign}Rs.{final_pnl:,.0f}*
  Gross: {pnl_sign}Rs.{gross_pnl:,.0f}
  Charges: Rs.{charges:,.0f}

📊 *Trade Summary:*
  Entry: {position.entry_time}
  Exit: {datetime.now().strftime('%H:%M:%S')}
  Duration: {duration_str}
  Lots: {position.num_lots}
  Credit: {position.total_credit:.2f} pts

📈 *Market:*
  Entry Spot: {position.spot_at_entry:,.0f}
  Entry VIX: {position.vix_at_entry:.1f}"""

        return self._send(message)

    def send_error_alert(
        self,
        error: str,
        context: str = ""
    ) -> bool:
        """
        Send error notification.

        Args:
            error: Error message
            context: Additional context

        Returns:
            True if sent
        """
        message = f"""❌ *EXPIRY IC ERROR*

💥 {error}

{f'📍 Context: {context}' if context else ''}

⏰ {datetime.now().strftime('%H:%M:%S')}"""

        return self._send(message)

    def send_day_summary(
        self,
        was_expiry: bool,
        traded: bool,
        final_pnl: Optional[float] = None,
        exit_reason: Optional[str] = None
    ) -> bool:
        """
        Send end-of-day summary.

        Args:
            was_expiry: Whether today was expiry
            traded: Whether we traded
            final_pnl: Final P&L if traded
            exit_reason: Exit reason if traded

        Returns:
            True if sent
        """
        if not was_expiry:
            # Don't send message for non-expiry days
            return True

        if not traded or final_pnl is None:
            message = f"""📊 *EXPIRY DAY SUMMARY*

📅 {datetime.now().strftime('%d %b %Y')}
⚪ No trade executed today

_Check logs for details_"""
        else:
            pnl_sign = "+" if final_pnl >= 0 else ""
            pnl_emoji = "💚" if final_pnl >= 0 else "❌"
            exit_text = exit_reason if exit_reason else "Unknown"

            message = f"""📊 *EXPIRY DAY SUMMARY*

📅 {datetime.now().strftime('%d %b %Y')}
{pnl_emoji} *P&L: {pnl_sign}Rs.{final_pnl:,.0f}*
📋 Exit: {exit_text}"""

        return self._send(message)
