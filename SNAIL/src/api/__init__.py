"""
SNAIL API Module

External API integrations for Kite, Claude, and Telegram.

@file        __init__.py
@description API module initialization
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
"""

from src.api.kite_auth import KiteAuthenticator
from src.api.kite_client import SNAILKiteClient, get_kite_client
from src.api.telegram_alerts import TelegramAlerts, get_telegram, send_alert
from src.api.claude_client import SNAILClaudeClient, ClaudeDecision
from src.api.telegram_bot import (
    TelegramBot,
    get_telegram_bot,
    reset_telegram_bot,
    CallbackAction,
    UserResponse,
)

__all__ = [
    # Kite
    "KiteAuthenticator",
    "SNAILKiteClient",
    "get_kite_client",
    # Telegram Alerts (outbound)
    "TelegramAlerts",
    "get_telegram",
    "send_alert",
    # Telegram Bot (polling - inbound)
    "TelegramBot",
    "get_telegram_bot",
    "reset_telegram_bot",
    "CallbackAction",
    "UserResponse",
    # Claude
    "SNAILClaudeClient",
    "ClaudeDecision",
]
