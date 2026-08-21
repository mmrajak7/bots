"""Telegram Bot Client for Alerts and Reports"""

import requests
from typing import Optional
from loguru import logger

from src.utils.config_manager import config


class TelegramClient:
    """
    Simple Telegram bot client for sending messages

    Uses Telegram Bot API directly (no python-telegram-bot library needed)
    """

    def __init__(self):
        """Initialize Telegram client"""
        self.enabled = config.get('telegram.enabled', True)
        self.bot_token = config.get('telegram.bot_token')
        self.chat_id = config.get('telegram.chat_id')
        self.retry_attempts = config.get('telegram.retry_attempts', 3)
        self.retry_delay = config.get('telegram.retry_delay', 5)
        # Mute every non-critical message: transactions, reports, EOD and
        # startup summaries. Owner's call 2026-08-21 while the bot is parked
        # and not trading -- it keeps RUNNING, it just stops narrating.
        # Defaults OFF: a code default that silences alerts is how a safety
        # channel goes dark without anyone deciding it should.
        self.suppress_non_critical = config.get(
            'telegram.suppress_non_critical', False)

        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram bot_token or chat_id not configured - alerts disabled")
            self.enabled = False

        if self.enabled:
            logger.info(f"Telegram client initialized - Chat ID: {self.chat_id}")
            if self.suppress_non_critical:
                # Positive log line: a mute that leaves no trace is
                # indistinguishable from a broken bot.
                logger.info("Telegram: suppress_non_critical ON - only "
                            "critical alerts will be sent")

    def send_message(
        self,
        message: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False,
        critical: bool = False
    ) -> bool:
        """
        Send message to Telegram

        Args:
            message: Message text (supports Markdown or HTML)
            parse_mode: "Markdown" or "HTML" (default: HTML for better compatibility)
            disable_notification: Silent notification
            critical: Survives `telegram.suppress_non_critical`. Reserved for
                messages about money or an unprotected position -- an order
                that failed, a position with no stop-loss, a dead API. NOT for
                routine trade narration.

        Returns:
            Success status
        """
        if not self.enabled:
            logger.debug(f"Telegram disabled - message not sent: {message[:100]}...")
            return False

        # The gate lives HERE, at the one function that performs the POST, so
        # no call site can route around it -- including any added later.
        if self.suppress_non_critical and not critical:
            logger.info(f"Telegram suppressed (non-critical): {message[:80]}")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        payload = {
            'chat_id': self.chat_id,
            'text': message,
            'parse_mode': parse_mode,
            'disable_notification': disable_notification
        }

        for attempt in range(self.retry_attempts):
            try:
                response = requests.post(url, json=payload, timeout=10)
                response.raise_for_status()

                result = response.json()

                if result.get('ok'):
                    logger.debug(f"Telegram message sent successfully")
                    return True
                else:
                    logger.error(f"Telegram API error: {result}")
                    return False

            except Exception as e:
                logger.warning(f"Telegram send attempt {attempt + 1}/{self.retry_attempts} failed: {e}")

                if attempt < self.retry_attempts - 1:
                    import time
                    time.sleep(self.retry_delay)
                else:
                    logger.error(f"All Telegram send attempts failed: {e}")
                    return False

        return False

    def send_alert(self, message: str, critical: bool = False) -> bool:
        """
        Send alert message

        Args:
            message: Alert message
            critical: If True, enable notification sound

        Returns:
            Success status
        """
        # Add test mode prefix if in test mode
        if config.is_test_mode():
            message = "[TEST MODE] " + message

        return self.send_message(
            message=message,
            parse_mode="HTML",
            disable_notification=not critical,
            critical=critical
        )

    def send_formatted_report(self, report: str) -> bool:
        """
        Send formatted report (daily/weekly)

        Args:
            report: Formatted report text

        Returns:
            Success status
        """
        # Add test mode prefix
        if config.is_test_mode():
            report = "<b>[TEST MODE]</b>\n\n" + report

        return self.send_message(
            message=report,
            parse_mode="HTML",
            disable_notification=True,  # Reports are not urgent
            critical=False              # ...and not critical, so they mute
        )


# Singleton instance
telegram = TelegramClient()
