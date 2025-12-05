"""
SNAIL Startup Validation

Runtime validation checks performed at application startup to ensure
all dependencies are properly configured before trading begins.

@file        startup_validation.py
@description Startup validation framework
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
"""

import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
from loguru import logger


@dataclass
class ValidationCheck:
    """Result of a single validation check."""
    name: str
    passed: bool
    message: str
    severity: str = 'ERROR'  # 'ERROR', 'WARNING', 'INFO'


@dataclass
class StartupValidationResult:
    """Complete validation result."""
    checks: List[ValidationCheck] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None

    @property
    def passed(self) -> bool:
        """All critical checks passed."""
        return all(c.passed for c in self.checks if c.severity == 'ERROR')

    @property
    def has_warnings(self) -> bool:
        """Has any warnings."""
        return any(not c.passed for c in self.checks if c.severity == 'WARNING')

    def add(self, name: str, passed: bool, message: str, severity: str = 'ERROR'):
        """Add a check result."""
        self.checks.append(ValidationCheck(
            name=name,
            passed=passed,
            message=message,
            severity=severity
        ))

    def summary(self) -> str:
        """Get a summary string."""
        passed = sum(1 for c in self.checks if c.passed)
        failed = sum(1 for c in self.checks if not c.passed and c.severity == 'ERROR')
        warnings = sum(1 for c in self.checks if not c.passed and c.severity == 'WARNING')
        return f"Passed: {passed}, Failed: {failed}, Warnings: {warnings}"


class StartupValidator:
    """
    Validates system readiness at startup.

    Checks:
    - Environment variables
    - API connectivity
    - Database integrity
    - Configuration validity
    """

    def __init__(self):
        self.result = StartupValidationResult()

    def validate_all(self) -> StartupValidationResult:
        """Run all validation checks."""
        logger.info("Starting startup validation...")

        self._validate_environment()
        self._validate_configuration()
        self._validate_database()
        self._validate_telegram()
        self._validate_kite()
        self._validate_claude()

        self.result.completed_at = datetime.now()

        # Log results
        for check in self.result.checks:
            if check.passed:
                logger.info(f"[PASS] {check.name}: {check.message}")
            elif check.severity == 'WARNING':
                logger.warning(f"[WARN] {check.name}: {check.message}")
            else:
                logger.error(f"[FAIL] {check.name}: {check.message}")

        logger.info(f"Validation complete: {self.result.summary()}")

        return self.result

    def _validate_environment(self):
        """Validate required environment variables."""
        required_vars = [
            ('ZERODHA_API_KEY', 'Zerodha Kite API key'),
            ('ZERODHA_API_SECRET', 'Zerodha Kite API secret'),
            ('ZERODHA_USER_ID', 'Zerodha client ID'),
            ('ZERODHA_TOTP_SECRET', 'Zerodha TOTP secret'),
            ('TELEGRAM_BOT_TOKEN', 'Telegram bot token'),
            ('TELEGRAM_CHAT_ID', 'Telegram chat ID'),
        ]

        optional_vars = [
            ('ANTHROPIC_API_KEY', 'Claude API key (optional for advisory)'),
            ('ZERODHA_PASSWORD', 'Zerodha password (for auto-login)'),
        ]

        for var_name, description in required_vars:
            value = os.getenv(var_name)
            if value:
                self.result.add(
                    name=f'ENV_{var_name}',
                    passed=True,
                    message=f'{description} is set'
                )
            else:
                self.result.add(
                    name=f'ENV_{var_name}',
                    passed=False,
                    message=f'{description} is missing'
                )

        for var_name, description in optional_vars:
            value = os.getenv(var_name)
            if value:
                self.result.add(
                    name=f'ENV_{var_name}',
                    passed=True,
                    message=f'{description} is set',
                    severity='INFO'
                )
            else:
                self.result.add(
                    name=f'ENV_{var_name}',
                    passed=False,
                    message=f'{description} is missing - Claude advisory will be disabled',
                    severity='WARNING'
                )

    def _validate_configuration(self):
        """Validate configuration files."""
        from src.utils.config import load_config, get_trading_config

        try:
            config = load_config()
            self.result.add(
                name='CONFIG_LOAD',
                passed=True,
                message='Configuration loaded successfully'
            )

            # Check trading config
            trading = get_trading_config()
            if trading:
                self.result.add(
                    name='CONFIG_TRADING',
                    passed=True,
                    message='Trading configuration is valid'
                )
            else:
                self.result.add(
                    name='CONFIG_TRADING',
                    passed=False,
                    message='Trading configuration is missing or invalid'
                )

        except Exception as e:
            self.result.add(
                name='CONFIG_LOAD',
                passed=False,
                message=f'Failed to load configuration: {e}'
            )

    def _validate_database(self):
        """Validate database connectivity and schema."""
        from src.utils.db import get_db_session

        try:
            with get_db_session() as conn:
                cursor = conn.cursor()

                # Check tables exist
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = {row[0] for row in cursor.fetchall()}

                required_tables = {
                    'positions', 'position_legs', 'orders', 'pnl_snapshots',
                    'claude_decisions', 'market_data', 'alert_queue',
                    'alert_cooldowns', 'system_status'
                }

                missing_tables = required_tables - tables
                if missing_tables:
                    self.result.add(
                        name='DB_SCHEMA',
                        passed=False,
                        message=f'Missing tables: {missing_tables}'
                    )
                else:
                    self.result.add(
                        name='DB_SCHEMA',
                        passed=True,
                        message='All required tables exist'
                    )

                # Check if we can write
                cursor.execute("SELECT COUNT(*) FROM positions")
                count = cursor.fetchone()[0]
                self.result.add(
                    name='DB_READ',
                    passed=True,
                    message=f'Database readable ({count} positions)'
                )

        except Exception as e:
            self.result.add(
                name='DB_CONNECTION',
                passed=False,
                message=f'Database connection failed: {e}'
            )

    def _validate_telegram(self):
        """Validate Telegram connectivity."""
        try:
            from src.api.telegram_alerts import TelegramAlerts

            telegram = TelegramAlerts()

            # Check method exists
            if not hasattr(telegram, 'send'):
                self.result.add(
                    name='TELEGRAM_API',
                    passed=False,
                    message='TelegramAlerts.send() method not found'
                )
                return

            # Check bot token validity
            if telegram.is_valid():
                self.result.add(
                    name='TELEGRAM_API',
                    passed=True,
                    message='Telegram bot credentials are valid'
                )
            else:
                self.result.add(
                    name='TELEGRAM_API',
                    passed=False,
                    message='Telegram bot credentials are invalid'
                )

        except ValueError as e:
            self.result.add(
                name='TELEGRAM_API',
                passed=False,
                message=f'Telegram configuration error: {e}'
            )
        except Exception as e:
            self.result.add(
                name='TELEGRAM_API',
                passed=False,
                message=f'Telegram validation failed: {e}'
            )

    def _validate_kite(self):
        """Validate Kite Connect connectivity."""
        try:
            from src.api.kite_client import get_kite_client

            kite = get_kite_client()

            # Check method exists
            required_methods = ['quote', 'get_nifty_spot', 'get_india_vix', 'place_order']
            missing_methods = [m for m in required_methods if not hasattr(kite, m)]

            if missing_methods:
                self.result.add(
                    name='KITE_API',
                    passed=False,
                    message=f'SNAILKiteClient missing methods: {missing_methods}'
                )
                return

            # Try to get a quote (light connectivity test)
            try:
                spot = kite.get_nifty_spot()
                if spot and spot > 0:
                    self.result.add(
                        name='KITE_API',
                        passed=True,
                        message=f'Kite connected - NIFTY @ {spot:,.2f}'
                    )
                else:
                    self.result.add(
                        name='KITE_API',
                        passed=False,
                        message='Kite returned invalid NIFTY spot'
                    )
            except Exception as e:
                self.result.add(
                    name='KITE_API',
                    passed=False,
                    message=f'Kite API call failed: {e}'
                )

        except Exception as e:
            self.result.add(
                name='KITE_API',
                passed=False,
                message=f'Kite validation failed: {e}'
            )

    def _validate_claude(self):
        """Validate Claude API (optional)."""
        api_key = os.getenv('ANTHROPIC_API_KEY')

        if not api_key:
            self.result.add(
                name='CLAUDE_API',
                passed=False,
                message='Claude API key not configured - advisory features disabled',
                severity='WARNING'
            )
            return

        try:
            from src.api.claude_client import get_claude_client

            client = get_claude_client()

            # Check method exists
            if hasattr(client, 'get_advice'):
                self.result.add(
                    name='CLAUDE_API',
                    passed=True,
                    message='Claude client configured'
                )
            else:
                self.result.add(
                    name='CLAUDE_API',
                    passed=False,
                    message='Claude client missing get_advice method',
                    severity='WARNING'
                )

        except Exception as e:
            self.result.add(
                name='CLAUDE_API',
                passed=False,
                message=f'Claude validation failed: {e}',
                severity='WARNING'
            )


def run_startup_validation() -> Tuple[bool, StartupValidationResult]:
    """
    Run startup validation and return result.

    Returns:
        Tuple of (passed, result)
    """
    validator = StartupValidator()
    result = validator.validate_all()
    return result.passed, result


def require_valid_startup():
    """
    Run startup validation and exit if critical checks fail.

    Call this at the start of main.py to ensure system is ready.
    """
    passed, result = run_startup_validation()

    if not passed:
        logger.critical("Startup validation FAILED - cannot continue")
        for check in result.checks:
            if not check.passed and check.severity == 'ERROR':
                logger.critical(f"  - {check.name}: {check.message}")

        raise SystemExit("Startup validation failed")

    if result.has_warnings:
        logger.warning("Startup validation passed with warnings")

    return result


if __name__ == '__main__':
    from dotenv import load_dotenv
    import sys

    # Load environment
    PROJECT_ROOT = Path(__file__).parent.parent.parent
    load_dotenv(PROJECT_ROOT / '.env')

    # Configure logging
    logger.remove()
    logger.add(sys.stderr, level="DEBUG")

    print("\n" + "=" * 60)
    print("SNAIL Startup Validation")
    print("=" * 60)

    passed, result = run_startup_validation()

    print("\n" + "=" * 60)
    print(f"Validation: {'PASSED' if passed else 'FAILED'}")
    print(f"Summary: {result.summary()}")
    print("=" * 60 + "\n")

    sys.exit(0 if passed else 1)
