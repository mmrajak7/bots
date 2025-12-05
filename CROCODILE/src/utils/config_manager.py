"""Configuration Manager for Crocodile Trading Bot"""

import os
from pathlib import Path
from typing import Dict, Any
import yaml
from loguru import logger


class ConfigManager:
    """Centralized configuration management"""

    _instance = None
    _config = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._config is None:
            self.load_config()

    def load_config(self, config_path: str = "config/config.yaml") -> None:
        """Load configuration from YAML file with path resolution"""
        try:
            # Resolve config path
            if not os.path.isabs(config_path):
                config_path = str(Path.cwd() / config_path)

            if not os.path.exists(config_path):
                raise FileNotFoundError(
                    f"Configuration file not found: {config_path}. "
                    f"Please copy config.yaml.template to config.yaml and fill in your credentials."
                )

            with open(config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f)

            logger.info(f"Configuration loaded from {config_path}")

            # Resolve relative paths in configuration
            bot_root = Path(config_path).parent.parent  # Go up to bot's root (e.g., BOTS/CROCODILE)
            self._resolve_paths(bot_root)

            self._validate_config()

        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")
            raise

    def _resolve_paths(self, bot_root: Path) -> None:
        """
        Resolve relative paths in configuration to absolute paths

        Handles paths for:
        - kite.token_file
        - database.url (SQLite)
        - signals.csv_file
        - signals.ignore_list_file
        - logging.log_dir
        - api_resilience.circuit_breaker.manual_reset_file

        Args:
            bot_root: Root directory of the bot (e.g., BOTS/CROCODILE)
        """
        # Resolve token file path
        if 'kite' in self._config and 'token_file' in self._config['kite']:
            token_file = self._config['kite']['token_file']
            if not os.path.isabs(token_file):
                resolved_path = str(bot_root / token_file)
                self._config['kite']['token_file'] = resolved_path
                logger.debug(f"Resolved token_file: {resolved_path}")

        # Resolve database path (SQLite URL format)
        if 'database' in self._config and 'url' in self._config['database']:
            db_url = self._config['database']['url']
            if db_url.startswith('sqlite:///'):
                db_path = db_url.replace('sqlite:///', '')
                if not os.path.isabs(db_path):
                    resolved_path = str(bot_root / db_path)
                    self._config['database']['url'] = f'sqlite:///{resolved_path}'
                    logger.debug(f"Resolved database URL: {self._config['database']['url']}")

        # Resolve signals paths
        if 'signals' in self._config:
            for key in ['csv_file', 'ignore_list_file']:
                if key in self._config['signals']:
                    path = self._config['signals'][key]
                    if not os.path.isabs(path):
                        resolved_path = str(bot_root / path)
                        self._config['signals'][key] = resolved_path
                        logger.debug(f"Resolved signals.{key}: {resolved_path}")

        # Resolve logging directory
        if 'logging' in self._config and 'log_dir' in self._config['logging']:
            log_dir = self._config['logging']['log_dir']
            if not os.path.isabs(log_dir):
                resolved_path = str(bot_root / log_dir)
                self._config['logging']['log_dir'] = resolved_path
                logger.debug(f"Resolved log_dir: {resolved_path}")

        # Resolve circuit breaker reset file
        if 'api_resilience' in self._config:
            if 'circuit_breaker' in self._config['api_resilience']:
                cb_config = self._config['api_resilience']['circuit_breaker']
                if 'manual_reset_file' in cb_config:
                    reset_file = cb_config['manual_reset_file']
                    if not os.path.isabs(reset_file):
                        resolved_path = str(bot_root / reset_file)
                        self._config['api_resilience']['circuit_breaker']['manual_reset_file'] = resolved_path
                        logger.debug(f"Resolved circuit_breaker reset file: {resolved_path}")

    def _validate_config(self) -> None:
        """Validate required configuration sections exist"""
        required_sections = ['kite', 'telegram', 'trading', 'database', 'logging']

        for section in required_sections:
            if section not in self._config:
                raise ValueError(f"Missing required configuration section: {section}")

        # Validate test mode flag
        test_mode = self._config.get('trading', {}).get('test_mode', '')
        if test_mode not in ['X', '']:
            logger.warning(f"Invalid test_mode value: '{test_mode}'. Should be 'X' or ''. Defaulting to 'X' (test mode)")
            self._config['trading']['test_mode'] = 'X'

        # Set default bot_instance_id if not present
        if 'bot_instance_id' not in self._config.get('trading', {}):
            self._config['trading']['bot_instance_id'] = 'crocodile_main'
            logger.info("No bot_instance_id configured, using default: 'crocodile_main'")

        # Validate capital allocation if present
        if 'capital_allocation' in self._config.get('trading', {}):
            cap_alloc = self._config['trading']['capital_allocation']

            # Check if both amount and percent are set
            if cap_alloc.get('allocated_capital_amount') and cap_alloc.get('allocated_capital_percent'):
                logger.warning(
                    "Both allocated_capital_amount and allocated_capital_percent are set. "
                    "allocated_capital_amount will take precedence."
                )

            # Validate reserve buffer percent
            buffer_pct = cap_alloc.get('reserve_buffer_percent', 0)
            if not (0 <= buffer_pct <= 100):
                raise ValueError(
                    f"Invalid reserve_buffer_percent: {buffer_pct}. Must be between 0 and 100."
                )

        logger.debug("Configuration validation passed")

    def get(self, key_path: str, default: Any = None) -> Any:
        """
        Get configuration value using dot notation

        Args:
            key_path: Path to config value (e.g., 'kite.credentials.userid')
            default: Default value if key not found

        Returns:
            Configuration value or default
        """
        keys = key_path.split('.')
        value = self._config

        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default

        return value

    def get_all(self) -> Dict[str, Any]:
        """Get entire configuration dictionary"""
        return self._config.copy()

    def is_test_mode(self) -> bool:
        """Check if bot is running in test mode"""
        return self.get('trading.test_mode', '') == 'X'

    def get_bot_instance_id(self) -> str:
        """Get bot instance ID"""
        return self.get('trading.bot_instance_id', 'crocodile_main')

    def has_capital_allocation(self) -> bool:
        """Check if capital allocation is configured"""
        cap_alloc = self.get('trading.capital_allocation', {})
        return bool(cap_alloc.get('allocated_capital_amount') or cap_alloc.get('allocated_capital_percent'))

    def reload(self) -> None:
        """Reload configuration from file"""
        self._config = None
        self.load_config()
        logger.info("Configuration reloaded")


# Singleton instance
config = ConfigManager()
