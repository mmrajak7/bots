"""
SNAIL Configuration Loader

Configuration loading, validation, and environment variable substitution.

@file        config.py
@description YAML configuration loader with env var support
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 10
"""

import os
import re
from pathlib import Path
from typing import Dict, Any, Optional
import yaml
from loguru import logger


# =============================================================================
# PATH CONFIGURATION
# =============================================================================

PROJECT_ROOT = Path(__file__).parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


# =============================================================================
# CONFIGURATION CACHE
# =============================================================================

_config_cache: Dict[str, Any] = {}


# =============================================================================
# ENVIRONMENT VARIABLE SUBSTITUTION
# =============================================================================

def _substitute_env_vars(value: Any) -> Any:
    """
    Recursively substitute ${VAR_NAME} with environment variables.

    Args:
        value: Value to process (can be dict, list, or scalar)

    Returns:
        Processed value with env vars substituted
    """
    if isinstance(value, dict):
        return {k: _substitute_env_vars(v) for k, v in value.items()}

    elif isinstance(value, list):
        return [_substitute_env_vars(v) for v in value]

    elif isinstance(value, str):
        # Pattern: ${VAR_NAME} or ${VAR_NAME:default}
        pattern = r'\$\{([^}:]+)(?::([^}]*))?\}'

        def replace(match):
            var_name = match.group(1)
            default = match.group(2)
            env_value = os.getenv(var_name)

            if env_value is not None:
                return env_value
            elif default is not None:
                return default
            else:
                # Return original if no env var and no default
                logger.warning(f"Environment variable {var_name} not set")
                return match.group(0)

        return re.sub(pattern, replace, value)

    return value


def _convert_bool_strings(value: Any) -> Any:
    """
    Convert string boolean values to actual booleans.

    Args:
        value: Value to process

    Returns:
        Processed value with bool strings converted
    """
    if isinstance(value, dict):
        return {k: _convert_bool_strings(v) for k, v in value.items()}

    elif isinstance(value, list):
        return [_convert_bool_strings(v) for v in value]

    elif isinstance(value, str):
        lower = value.lower()
        if lower in ('true', 'yes', '1', 'on'):
            return True
        elif lower in ('false', 'no', '0', 'off'):
            return False

    return value


# =============================================================================
# CONFIGURATION LOADING
# =============================================================================

def load_config(config_path: Optional[Path] = None, force_reload: bool = False) -> Dict[str, Any]:
    """
    Load configuration from YAML file.

    Caches result for subsequent calls unless force_reload is True.

    Args:
        config_path: Path to config file (default: config/config.yaml)
        force_reload: Force reload from file

    Returns:
        Configuration dictionary

    Raises:
        FileNotFoundError: If config file doesn't exist
        yaml.YAMLError: If config file is invalid
    """
    global _config_cache

    # Return cached config if available
    if _config_cache and not force_reload:
        return _config_cache

    config_path = config_path or CONFIG_PATH

    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    logger.debug(f"Loading configuration from {config_path}")

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    # Substitute environment variables
    config = _substitute_env_vars(config)

    # Convert boolean strings
    config = _convert_bool_strings(config)

    # Add computed paths
    config['_paths'] = {
        'root': PROJECT_ROOT,
        'config': config_path.parent,
        'data': PROJECT_ROOT / 'data',
        'logs': PROJECT_ROOT / 'logs',
        'src': PROJECT_ROOT / 'src',
    }

    _config_cache = config
    logger.info(f"Configuration loaded from {config_path}")

    return config


def reload_config() -> Dict[str, Any]:
    """
    Force reload configuration from file.

    Returns:
        Fresh configuration dictionary
    """
    return load_config(force_reload=True)


# =============================================================================
# CONFIGURATION ACCESSORS
# =============================================================================

def get_trading_config() -> Dict[str, Any]:
    """Get trading-specific configuration."""
    return load_config().get('trading', {})


def get_entry_config() -> Dict[str, Any]:
    """Get entry-specific configuration including R:R and Claude settings."""
    return get_trading_config().get('entry', {})


def get_api_config() -> Dict[str, Any]:
    """Get API configuration."""
    return load_config().get('api', {})


def get_kite_config() -> Dict[str, Any]:
    """Get Kite API configuration."""
    return get_api_config().get('kite', {})


def get_claude_config() -> Dict[str, Any]:
    """Get Claude API configuration."""
    return get_api_config().get('claude', {})


def get_telegram_config() -> Dict[str, Any]:
    """Get Telegram configuration."""
    return get_api_config().get('telegram', {})


def get_paths_config() -> Dict[str, Any]:
    """Get paths configuration."""
    return load_config().get('paths', {})


def get_monitoring_config() -> Dict[str, Any]:
    """Get monitoring configuration."""
    return load_config().get('monitoring', {})


def get_charges_config() -> Dict[str, Any]:
    """Get transaction charges configuration."""
    return load_config().get('charges', {})


# =============================================================================
# CONFIGURATION VALIDATION
# =============================================================================

def validate_config(config: Optional[Dict] = None) -> Dict[str, list]:
    """
    Validate configuration for required values.

    Args:
        config: Configuration to validate (loads from file if None)

    Returns:
        Dictionary with 'errors' and 'warnings' lists
    """
    if config is None:
        config = load_config()

    errors = []
    warnings = []

    # Check required API keys
    kite = config.get('api', {}).get('kite', {})
    if not kite.get('api_key') or '${' in str(kite.get('api_key', '')):
        errors.append("Kite API key not configured")
    if not kite.get('api_secret') or '${' in str(kite.get('api_secret', '')):
        errors.append("Kite API secret not configured")

    claude = config.get('api', {}).get('claude', {})
    if not claude.get('api_key') or '${' in str(claude.get('api_key', '')):
        errors.append("Claude API key not configured")

    telegram = config.get('api', {}).get('telegram', {})
    if not telegram.get('bot_token') or '${' in str(telegram.get('bot_token', '')):
        errors.append("Telegram bot token not configured")
    if not telegram.get('chat_id') or '${' in str(telegram.get('chat_id', '')):
        errors.append("Telegram chat ID not configured")

    # Check trading parameters
    trading = config.get('trading', {})
    if trading.get('capital', {}).get('allocated', 0) < 100000:
        warnings.append("Allocated capital seems low for Iron Fly strategy")

    entry = trading.get('entry', {})
    vix_range = entry.get('vix_range', {})
    if vix_range.get('min', 0) < 8 or vix_range.get('max', 0) > 20:
        warnings.append("VIX range may be too wide for Iron Fly")

    # Check paths exist
    paths = config.get('paths', {})
    prompts_path = PROJECT_ROOT / paths.get('prompts', 'config/prompts')
    if not prompts_path.exists():
        warnings.append(f"Prompts directory not found: {prompts_path}")

    return {'errors': errors, 'warnings': warnings}


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def is_paper_trading() -> bool:
    """Check if paper trading mode is enabled."""
    config = load_config()
    return config.get('paper_trading', {}).get('enabled', False)


def get_prompt_path(prompt_name: str) -> Path:
    """
    Get path to a prompt template file.

    Args:
        prompt_name: Prompt name (without .txt extension)

    Returns:
        Path to prompt file
    """
    paths = get_paths_config()
    prompts_dir = PROJECT_ROOT / paths.get('prompts', 'config/prompts')
    return prompts_dir / f"{prompt_name}.txt"


def get_database_path() -> Path:
    """Get path to database file."""
    paths = get_paths_config()
    return PROJECT_ROOT / paths.get('database', 'data/snail.db')


def get_instruments_path() -> Path:
    """Get path to instruments CSV file."""
    paths = get_paths_config()
    return PROJECT_ROOT / paths.get('instruments', 'data/instruments.csv')


def get_log_path(log_type: str = 'main') -> Path:
    """
    Get path to log file.

    Args:
        log_type: 'main', 'orders', or 'claude'

    Returns:
        Path to log file
    """
    paths = get_paths_config()

    if log_type == 'main':
        return PROJECT_ROOT / paths.get('logs', 'logs/snail.log')
    elif log_type == 'orders':
        return PROJECT_ROOT / paths.get('order_logs', 'logs/orders.log')
    elif log_type == 'claude':
        return PROJECT_ROOT / paths.get('claude_logs', 'logs/claude')
    else:
        return PROJECT_ROOT / 'logs' / f'{log_type}.log'


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    import sys
    from dotenv import load_dotenv

    # Load environment variables
    load_dotenv(PROJECT_ROOT / '.env')

    # Configure logging
    logger.remove()
    logger.add(sys.stderr, level="DEBUG")

    print("\n" + "=" * 60)
    print("SNAIL Configuration Test")
    print("=" * 60)

    try:
        config = load_config()

        print("\n[1] Configuration loaded successfully")
        print(f"    Root: {config['_paths']['root']}")

        print("\n[2] Trading configuration:")
        trading = get_trading_config()
        print(f"    Instrument: {trading.get('instrument')}")
        print(f"    Strategy: {trading.get('strategy')}")
        print(f"    Allocated capital: ₹{trading.get('capital', {}).get('allocated', 0):,}")

        print(f"\n[3] Paper trading: {is_paper_trading()}")

        print("\n[4] Validating configuration...")
        result = validate_config()
        if result['errors']:
            print("    Errors:")
            for err in result['errors']:
                print(f"      - {err}")
        if result['warnings']:
            print("    Warnings:")
            for warn in result['warnings']:
                print(f"      - {warn}")
        if not result['errors'] and not result['warnings']:
            print("    All checks passed!")

        print("\n" + "=" * 60)
        print("Configuration test complete!")
        print("=" * 60 + "\n")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
