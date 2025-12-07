"""
Broker Factory - Creates appropriate broker adapter based on configuration

This module provides a factory function that reads the trade_method from config
and returns the appropriate broker adapter (EnctokenAdapter or KiteAPIAdapter).

Usage:
    from src.api.broker_factory import get_broker_adapter, get_broker

    # Get broker adapter (creates new instance)
    broker = get_broker_adapter()

    # Get singleton broker (recommended for most use cases)
    broker = get_broker()

    # Both return a BrokerAdapter that can be used uniformly:
    broker.place_order(...)
    broker.get_historical_data(...)
"""

import os
from typing import Optional
from loguru import logger
import yaml

from src.api.broker_adapter import BrokerAdapter


# Singleton instance
_broker_instance: Optional[BrokerAdapter] = None


def load_config(config_path: str = "config/config.yaml") -> dict:
    """Load configuration from YAML file"""
    # Handle relative path
    if not os.path.isabs(config_path):
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        config_path = os.path.join(project_root, config_path)

    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def get_broker_adapter(config_path: str = "config/config.yaml") -> BrokerAdapter:
    """
    Factory function to create appropriate broker adapter

    Reads trade_method from config and returns:
    - "enctoken" -> EnctokenAdapter (default, existing method)
    - "kite_api" -> KiteAPIAdapter (uses SNAIL's token)

    Args:
        config_path: Path to configuration file

    Returns:
        BrokerAdapter instance (EnctokenAdapter or KiteAPIAdapter)

    Raises:
        ValueError: If unknown trade_method specified
        ImportError: If kite_api selected but kiteconnect not installed
    """
    config = load_config(config_path)
    trade_method = config.get('kite', {}).get('trade_method', 'enctoken')

    logger.info(f"Creating broker adapter with trade_method: {trade_method}")

    if trade_method == "enctoken":
        from src.api.enctoken_adapter import EnctokenAdapter
        return EnctokenAdapter(config_path=config_path)

    elif trade_method == "kite_api":
        from src.api.kite_api_adapter import KiteAPIAdapter
        return KiteAPIAdapter(config=config)

    else:
        raise ValueError(
            f"Unknown trade_method: '{trade_method}'. "
            f"Valid options are: 'enctoken', 'kite_api'"
        )


def get_broker(config_path: str = "config/config.yaml", force_new: bool = False) -> BrokerAdapter:
    """
    Get singleton broker adapter instance

    This is the recommended function for most use cases as it ensures
    a single broker instance is shared across the application.

    Args:
        config_path: Path to configuration file
        force_new: If True, create new instance even if one exists

    Returns:
        BrokerAdapter singleton instance
    """
    global _broker_instance

    if _broker_instance is None or force_new:
        _broker_instance = get_broker_adapter(config_path)
        logger.info(f"Broker singleton created: {_broker_instance.trade_method}")

    return _broker_instance


def reset_broker() -> None:
    """
    Reset the broker singleton

    Useful for testing or when switching configurations.
    """
    global _broker_instance
    _broker_instance = None
    logger.info("Broker singleton reset")


def validate_kite_api_token(config_path: str = "config/config.yaml"):
    """
    Validate that Kite API token exists and is valid

    This should be called during morning startup when using kite_api method.

    Args:
        config_path: Path to configuration file

    Returns:
        Tuple of (is_valid, message)
    """
    import json
    from datetime import datetime, timedelta
    from src.utils.timezone_helper import now_ist, IST

    config = load_config(config_path)
    trade_method = config.get('kite', {}).get('trade_method', 'enctoken')

    # Only relevant for kite_api method
    if trade_method != 'kite_api':
        return True, f"Using {trade_method} method - no token validation needed"

    # Get token file path
    kite_api_config = config.get('kite', {}).get('kite_api', {})
    token_file = kite_api_config.get('token_file', '../data/kite_access_token.json')

    # Resolve relative path
    if not os.path.isabs(token_file):
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        token_file = os.path.normpath(os.path.join(project_root, token_file))

    # Check file exists
    if not os.path.exists(token_file):
        return False, (
            f"Kite API token file not found: {token_file}\n"
            "SNAIL morning startup must run at 8:45 AM to generate token.\n"
            "Either:\n"
            "1. Wait for SNAIL to run, OR\n"
            "2. Run SNAIL morning startup manually, OR\n"
            "3. Switch to trade_method: 'enctoken' in config"
        )

    # Check token content and date
    try:
        with open(token_file, 'r') as f:
            token_data = json.load(f)

        access_token = token_data.get('access_token')
        api_key = token_data.get('api_key')
        generated_at = token_data.get('generated_at', '')

        if not access_token or not api_key:
            return False, "Token file is invalid - missing access_token or api_key"

        # Validate token date
        if generated_at:
            try:
                token_time = datetime.fromisoformat(generated_at.replace('Z', '+00:00'))
                if token_time.tzinfo is None:
                    token_time = token_time.replace(tzinfo=IST)

                current_time = now_ist()
                today_6am = current_time.replace(hour=6, minute=0, second=0, microsecond=0)

                if current_time < today_6am:
                    valid_from = today_6am - timedelta(days=1)
                else:
                    valid_from = today_6am

                if token_time < valid_from:
                    hours_old = (current_time - token_time).total_seconds() / 3600
                    return False, (
                        f"Token expired (generated {hours_old:.1f} hours ago at {generated_at}).\n"
                        "Tokens expire at 6 AM IST daily.\n"
                        "SNAIL morning startup must run to refresh token."
                    )

                hours_old = (current_time - token_time).total_seconds() / 3600
                return True, f"Token valid (generated {hours_old:.1f} hours ago)"

            except (ValueError, TypeError) as e:
                return True, f"Token exists but could not validate date: {e}"

        return True, "Token file exists and appears valid"

    except json.JSONDecodeError as e:
        return False, f"Token file is corrupted (invalid JSON): {e}"
    except Exception as e:
        return False, f"Error reading token file: {e}"


# Convenience alias for backward compatibility
# Code that was using KiteTradeClient directly can switch to this
def get_kite_client(config_path: str = "config/config.yaml") -> BrokerAdapter:
    """
    Backward-compatible alias for get_broker()

    Existing code using KiteTradeClient can migrate to this function
    without changing their variable names.
    """
    return get_broker(config_path)
