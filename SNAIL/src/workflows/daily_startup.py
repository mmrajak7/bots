"""
SNAIL Daily Startup Workflow

Morning initialization and validation procedures.

@file        daily_startup.py
@description Daily startup and validation workflow
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 9.1
"""

import os
import sys
from datetime import datetime, date, time, timedelta
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
from loguru import logger

from src.api.kite_client import SNAILKiteClient, get_kite_client
from src.api.telegram_alerts import TelegramAlerts, get_telegram
from src.api.claude_client import SNAILClaudeClient, get_claude_client
from src.services.claude_advisor import ClaudeAdvisor, get_claude_advisor
from src.utils.symbol_builder import (
    load_instruments,
    get_target_expiry,
    get_all_expiries,
    refresh_instruments_csv,
    get_instruments_age,
    INSTRUMENTS_MAX_AGE_HOURS
)
from src.utils.holiday_scraper import (
    scrape_and_save_holidays,
    get_upcoming_holidays,
    is_friday_before_holiday,
    is_long_weekend
)
from src.utils.market_events_scraper import (
    scrape_and_save_all as scrape_market_events,
    get_events_for_telegram,
    get_news_for_telegram
)
from src.utils.db import (
    get_active_position,
    get_position_legs,
    get_db_session,
    cleanup_old_data
)
from src.utils.config import (
    load_config,
    get_trading_config,
    get_instruments_path,
    validate_config,
    PROJECT_ROOT
)
from src.utils.helpers import is_trading_day, is_market_open


# =============================================================================
# CONSTANTS
# =============================================================================

# Startup windows
PRE_MARKET_START = time(8, 45)
MARKET_OPEN = time(9, 15)
GAP_CHECK_TIME = time(9, 16)


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class StartupCheck:
    """
    Individual startup check result.

    Attributes:
        name: Check name
        passed: Whether check passed
        message: Result message
        critical: Whether failure is critical
    """
    name: str
    passed: bool
    message: str
    critical: bool = False


@dataclass
class StartupResult:
    """
    Complete startup result.

    Attributes:
        success: Whether all critical checks passed
        checks: List of check results
        warnings: List of warning messages
        ready_to_trade: Whether system is ready for trading
        active_position: Current active position if any
        market_data: Market data snapshot
    """
    success: bool
    checks: List[StartupCheck] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    ready_to_trade: bool = False
    active_position: Optional[Dict[str, Any]] = None
    market_data: Optional[Dict[str, Any]] = None


# =============================================================================
# DAILY STARTUP CLASS
# =============================================================================

class DailyStartup:
    """
    Handles daily startup procedures.

    Startup sequence:
    1. Validate configuration
    2. Check Kite authentication
    3. Validate database
    4. Download/validate instruments
    5. Check for active positions
    6. Get market data
    7. Check for gap open (if position exists)
    8. Send morning summary

    Attributes:
        config: System configuration
        kite: Kite client
        telegram: Telegram client
        claude_advisor: Claude advisor
    """

    def __init__(
        self,
        kite: Optional[SNAILKiteClient] = None,
        telegram: Optional[TelegramAlerts] = None,
        claude_advisor: Optional[ClaudeAdvisor] = None,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize daily startup.

        Args:
            kite: Kite client
            telegram: Telegram client
            claude_advisor: Claude advisor
            config: Configuration
        """
        self.config = config or load_config()
        self.trading_config = get_trading_config()

        self.kite = kite
        self.telegram = telegram or get_telegram()
        self.claude_advisor = claude_advisor

        self._instruments_df = None

        logger.info("Daily startup workflow initialized")

    # =========================================================================
    # INDIVIDUAL CHECKS
    # =========================================================================

    def _check_configuration(self) -> StartupCheck:
        """Check configuration validity."""
        try:
            result = validate_config(self.config)

            if result['errors']:
                return StartupCheck(
                    name="Configuration",
                    passed=False,
                    message=f"Errors: {', '.join(result['errors'])}",
                    critical=True
                )

            if result['warnings']:
                return StartupCheck(
                    name="Configuration",
                    passed=True,
                    message=f"Warnings: {', '.join(result['warnings'])}"
                )

            return StartupCheck(
                name="Configuration",
                passed=True,
                message="All configuration checks passed"
            )

        except Exception as e:
            return StartupCheck(
                name="Configuration",
                passed=False,
                message=f"Configuration error: {e}",
                critical=True
            )

    def _check_kite_auth(self) -> StartupCheck:
        """Check Kite authentication."""
        try:
            if self.kite is None:
                self.kite = get_kite_client(self.config)

            self.kite.ensure_authenticated()
            profile = self.kite.profile()

            return StartupCheck(
                name="Kite Authentication",
                passed=True,
                message=f"Authenticated as {profile.get('user_name', 'Unknown')}"
            )

        except Exception as e:
            return StartupCheck(
                name="Kite Authentication",
                passed=False,
                message=f"Authentication failed: {e}",
                critical=True
            )

    def _check_database(self) -> StartupCheck:
        """Check database connectivity and schema."""
        try:
            with get_db_session() as conn:
                cursor = conn.cursor()

                # Check tables exist
                cursor.execute("""
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name NOT LIKE 'sqlite_%'
                """)
                tables = [row[0] for row in cursor.fetchall()]

                required_tables = [
                    'positions', 'position_legs', 'orders',
                    'pnl_snapshots', 'claude_decisions', 'system_status'
                ]

                missing = [t for t in required_tables if t not in tables]

                if missing:
                    return StartupCheck(
                        name="Database",
                        passed=False,
                        message=f"Missing tables: {', '.join(missing)}",
                        critical=True
                    )

                return StartupCheck(
                    name="Database",
                    passed=True,
                    message=f"Database OK ({len(tables)} tables)"
                )

        except Exception as e:
            return StartupCheck(
                name="Database",
                passed=False,
                message=f"Database error: {e}",
                critical=True
            )

    def _refresh_holiday_calendar(self) -> StartupCheck:
        """
        Refresh NSE holiday calendar from Zerodha.

        TDD Section 5.2: NSE Holiday Calendar Auto-Refresh
        """
        try:
            success, message = scrape_and_save_holidays()

            if success:
                # Check for upcoming holidays
                upcoming = get_upcoming_holidays(7)  # Next 7 days
                upcoming_str = ""
                if upcoming:
                    upcoming_str = f" | Next: {upcoming[0]['name']} in {upcoming[0]['days_until']}d"

                return StartupCheck(
                    name="Holiday Calendar",
                    passed=True,
                    message=f"Updated{upcoming_str}"
                )
            else:
                return StartupCheck(
                    name="Holiday Calendar",
                    passed=True,  # Non-critical failure
                    message=f"Scrape failed (using cache): {message}"
                )

        except Exception as e:
            return StartupCheck(
                name="Holiday Calendar",
                passed=True,  # Non-critical
                message=f"Holiday scrape error: {e}"
            )

    def _check_long_weekend(self) -> StartupCheck:
        """
        Check if today is Friday before a long weekend.

        Important for position management decisions.
        """
        try:
            is_friday_holiday, holiday_name = is_friday_before_holiday()
            is_long, off_days = is_long_weekend()

            if is_friday_holiday:
                return StartupCheck(
                    name="Long Weekend Check",
                    passed=True,
                    message=f"Friday before {holiday_name} - consider early exit"
                )
            elif is_long:
                return StartupCheck(
                    name="Long Weekend Check",
                    passed=True,
                    message=f"Long weekend ahead ({off_days} days off)"
                )
            else:
                return StartupCheck(
                    name="Long Weekend Check",
                    passed=True,
                    message="No long weekend detected"
                )

        except Exception as e:
            return StartupCheck(
                name="Long Weekend Check",
                passed=True,
                message=f"Check error: {e}"
            )

    def _scrape_market_events(self) -> StartupCheck:
        """
        Scrape market events and news from Zerodha.

        Scrapes:
        - Zerodha Calendar for upcoming market events
        - Zerodha Pulse for latest news headlines

        Saves to shared ../BOTS/data/ folder.
        """
        try:
            events_count, news_count = scrape_market_events()

            if events_count > 0 or news_count > 0:
                return StartupCheck(
                    name="Market Events",
                    passed=True,
                    message=f"Scraped {events_count} events, {news_count} news"
                )
            else:
                return StartupCheck(
                    name="Market Events",
                    passed=True,  # Non-critical
                    message="No events/news scraped (sources may be unavailable)"
                )

        except Exception as e:
            return StartupCheck(
                name="Market Events",
                passed=True,  # Non-critical
                message=f"Scrape error: {e}"
            )

    def _refresh_instruments(self) -> StartupCheck:
        """
        Refresh instruments CSV from Kite API.

        TDD Section 5.1: Daily Startup - Instruments Auto-Refresh
        """
        try:
            instruments_path = get_instruments_path()
            age = get_instruments_age(instruments_path)

            # Refresh if file doesn't exist or is older than threshold
            needs_refresh = age is None or age > INSTRUMENTS_MAX_AGE_HOURS

            if needs_refresh:
                logger.info(f"Instruments refresh needed (age: {age:.1f}h)" if age else "Instruments file not found")
                success, message = refresh_instruments_csv(self.kite, instruments_path)

                if success:
                    return StartupCheck(
                        name="Instruments Refresh",
                        passed=True,
                        message=message
                    )
                else:
                    return StartupCheck(
                        name="Instruments Refresh",
                        passed=False,
                        message=message,
                        critical=True
                    )
            else:
                return StartupCheck(
                    name="Instruments Refresh",
                    passed=True,
                    message=f"Instruments current ({age:.1f}h old)"
                )

        except Exception as e:
            return StartupCheck(
                name="Instruments Refresh",
                passed=False,
                message=f"Refresh error: {e}",
                critical=True
            )

    def _check_instruments(self) -> StartupCheck:
        """Check instruments file."""
        try:
            instruments_path = get_instruments_path()

            if not instruments_path.exists():
                return StartupCheck(
                    name="Instruments",
                    passed=False,
                    message=f"Instruments file not found: {instruments_path}",
                    critical=True
                )

            # Load and validate
            self._instruments_df = load_instruments(instruments_path)

            if self._instruments_df is None or self._instruments_df.empty:
                return StartupCheck(
                    name="Instruments",
                    passed=False,
                    message="Failed to load instruments file or file is empty",
                    critical=True
                )

            # Check for NIFTY options
            nifty_options = self._instruments_df[
                (self._instruments_df['name'] == 'NIFTY') &
                (self._instruments_df['instrument_type'].isin(['CE', 'PE']))
            ]

            if nifty_options.empty:
                return StartupCheck(
                    name="Instruments",
                    passed=False,
                    message="No NIFTY options found in instruments",
                    critical=True
                )

            # Get available expiries
            expiries = get_all_expiries(self._instruments_df)

            return StartupCheck(
                name="Instruments",
                passed=True,
                message=f"Loaded {len(nifty_options)} NIFTY options, {len(expiries)} expiries"
            )

        except Exception as e:
            return StartupCheck(
                name="Instruments",
                passed=False,
                message=f"Instruments error: {e}",
                critical=True
            )

    def _check_telegram(self) -> StartupCheck:
        """Check Telegram connectivity."""
        try:
            # Send test message (silently)
            success = self.telegram.test_connection()

            if success:
                return StartupCheck(
                    name="Telegram",
                    passed=True,
                    message="Telegram bot connected"
                )
            else:
                return StartupCheck(
                    name="Telegram",
                    passed=False,
                    message="Telegram connection failed",
                    critical=False  # Non-critical, can continue without alerts
                )

        except Exception as e:
            return StartupCheck(
                name="Telegram",
                passed=False,
                message=f"Telegram error: {e}",
                critical=False
            )

    def _check_claude(self) -> StartupCheck:
        """Check Claude API connectivity."""
        try:
            if self.claude_advisor is None:
                self.claude_advisor = get_claude_advisor()

            # Quick test
            client = get_claude_client()
            if client.test_connection():
                return StartupCheck(
                    name="Claude API",
                    passed=True,
                    message="Claude API connected"
                )
            else:
                return StartupCheck(
                    name="Claude API",
                    passed=False,
                    message="Claude connection failed",
                    critical=False  # Can trade without AI
                )

        except Exception as e:
            return StartupCheck(
                name="Claude API",
                passed=False,
                message=f"Claude error: {e}",
                critical=False
            )

    def _check_active_position(self) -> Tuple[StartupCheck, Optional[Dict]]:
        """Check for active positions."""
        try:
            position = get_active_position()

            if position:
                legs = get_position_legs(position.id)
                position_info = {
                    'id': position.id,
                    'strategy': 'Iron Fly',  # Default strategy for SNAIL
                    'atm_strike': position.atm_strike,
                    'wing_distance': position.wing_distance,
                    'entry_premium': position.entry_premium,
                    'expiry_date': position.expiry_date.isoformat() if position.expiry_date else None,
                    'legs': len(legs)
                }

                return StartupCheck(
                    name="Active Position",
                    passed=True,
                    message=f"Position {position.id}: Iron Fly @ {position.atm_strike}"
                ), position_info
            else:
                return StartupCheck(
                    name="Active Position",
                    passed=True,
                    message="No active position"
                ), None

        except Exception as e:
            return StartupCheck(
                name="Active Position",
                passed=False,
                message=f"Position check error: {e}",
                critical=False
            ), None

    def _get_market_data(self) -> Tuple[StartupCheck, Optional[Dict]]:
        """Get current market data."""
        try:
            nifty_spot = self.kite.get_nifty_spot()
            india_vix = self.kite.get_india_vix()
            margins = self.kite.margins()

            available_margin = margins.get('equity', {}).get('net', 0)

            market_data = {
                'nifty_spot': nifty_spot,
                'india_vix': india_vix,
                'available_margin': available_margin,
                'timestamp': datetime.now().isoformat()
            }

            return StartupCheck(
                name="Market Data",
                passed=True,
                message=f"NIFTY: {nifty_spot:,.2f}, VIX: {india_vix:.2f}"
            ), market_data

        except Exception as e:
            return StartupCheck(
                name="Market Data",
                passed=False,
                message=f"Market data error: {e}",
                critical=False
            ), None

    # =========================================================================
    # MAIN STARTUP PROCEDURE
    # =========================================================================

    def run(self) -> StartupResult:
        """
        Execute daily startup procedure.

        Returns:
            StartupResult with all check results
        """
        logger.info("Starting daily startup procedure...")
        start_time = datetime.now()

        result = StartupResult(success=True)
        checks = []
        warnings = []

        # Check if trading day
        if not is_trading_day():
            logger.info("Not a trading day, skipping most checks")
            result.warnings.append("Today is not a trading day")

        # 1. Configuration check
        config_check = self._check_configuration()
        checks.append(config_check)
        if not config_check.passed and config_check.critical:
            result.success = False

        # 2. Kite authentication
        kite_check = self._check_kite_auth()
        checks.append(kite_check)
        if not kite_check.passed and kite_check.critical:
            result.success = False

        # 3. Database check
        db_check = self._check_database()
        checks.append(db_check)
        if not db_check.passed and db_check.critical:
            result.success = False

        # 4. Holiday calendar refresh (before instruments)
        holiday_check = self._refresh_holiday_calendar()
        checks.append(holiday_check)
        if not holiday_check.passed:
            warnings.append(f"Holidays: {holiday_check.message}")

        # 4b. Market events scrape (calendar + news)
        events_check = self._scrape_market_events()
        checks.append(events_check)
        if not events_check.passed:
            warnings.append(f"Events: {events_check.message}")

        # 5. Instruments refresh (before instruments check)
        instruments_refresh_check = self._refresh_instruments()
        checks.append(instruments_refresh_check)
        if not instruments_refresh_check.passed and instruments_refresh_check.critical:
            result.success = False

        # 6. Instruments validation
        instruments_check = self._check_instruments()
        checks.append(instruments_check)
        if not instruments_check.passed and instruments_check.critical:
            result.success = False

        # 7. Long weekend check (for position management)
        long_weekend_check = self._check_long_weekend()
        checks.append(long_weekend_check)
        if "consider early exit" in long_weekend_check.message:
            warnings.append(f"Long Weekend: {long_weekend_check.message}")

        # 8. Telegram check
        telegram_check = self._check_telegram()
        checks.append(telegram_check)
        if not telegram_check.passed:
            warnings.append(f"Telegram: {telegram_check.message}")

        # 9. Claude check
        claude_check = self._check_claude()
        checks.append(claude_check)
        if not claude_check.passed:
            warnings.append(f"Claude: {claude_check.message}")

        # 10. Active position check
        position_check, active_position = self._check_active_position()
        checks.append(position_check)
        result.active_position = active_position

        # 11. Market data
        market_check, market_data = self._get_market_data()
        checks.append(market_check)
        result.market_data = market_data

        # Compile results
        result.checks = checks
        result.warnings = warnings
        result.ready_to_trade = result.success and is_trading_day()

        # Cleanup old data
        try:
            cleanup_old_data()
            logger.info("Old data cleanup completed")
        except Exception as e:
            logger.warning(f"Data cleanup error: {e}")

        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info(f"Startup complete in {elapsed:.2f}s. Success: {result.success}")

        return result

    def send_morning_summary(self, startup_result: StartupResult) -> None:
        """
        Send morning summary via Telegram.

        Args:
            startup_result: Result from startup procedure
        """
        # Extract market data
        nifty_spot = 0.0
        vix = 0.0
        if startup_result.market_data:
            md = startup_result.market_data
            nifty_spot = md.get('nifty_spot', 0.0)
            vix = md.get('india_vix', 0.0)

        # Check if position exists
        has_position = startup_result.active_position is not None

        # Build entry conditions dict from startup checks
        entry_conditions = {
            'vix_ok': True,  # Will be set based on VIX range check
            'dte_ok': True,  # Will be set based on DTE check
            'cooldown_ok': True,  # Will be set based on cooldown check
            'margin_ok': True  # Will be set based on margin check
        }

        # Extract condition status from startup checks
        for check in startup_result.checks:
            if 'vix' in check.name.lower():
                entry_conditions['vix_ok'] = check.passed
            elif 'dte' in check.name.lower() or 'expiry' in check.name.lower():
                entry_conditions['dte_ok'] = check.passed
            elif 'cooldown' in check.name.lower():
                entry_conditions['cooldown_ok'] = check.passed
            elif 'margin' in check.name.lower():
                entry_conditions['margin_ok'] = check.passed

        # Get actual margin from market data
        margin_available = 0
        if startup_result.market_data:
            margin_available = startup_result.market_data.get('available_margin', 0)

        # Get scraped events and news for Telegram
        events_summary = get_events_for_telegram()
        news_summary = get_news_for_telegram(limit=5)

        self.telegram.send_morning_summary(
            nifty_spot=nifty_spot,
            vix=vix,
            has_position=has_position,
            entry_conditions=entry_conditions,
            margin_available=margin_available,
            events_summary=events_summary,
            news_summary=news_summary
        )


# =============================================================================
# STANDALONE EXECUTION
# =============================================================================

def run_daily_startup() -> StartupResult:
    """
    Run daily startup as standalone function.

    Returns:
        StartupResult
    """
    startup = DailyStartup()
    result = startup.run()
    startup.send_morning_summary(result)
    return result


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    from dotenv import load_dotenv

    load_dotenv()

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    print("\n" + "=" * 60)
    print("SNAIL Daily Startup")
    print("=" * 60)

    try:
        result = run_daily_startup()

        print("\n[STARTUP CHECKS]:")
        for check in result.checks:
            status = "[OK]" if check.passed else ("[FAIL]" if check.critical else "[WARN]")
            print(f"   {status} {check.name}: {check.message}")

        if result.warnings:
            print("\n[WARNINGS]:")
            for warning in result.warnings:
                print(f"   - {warning}")

        print(f"\n{'='*60}")
        print(f"Ready to trade: {result.ready_to_trade}")
        print(f"{'='*60}\n")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
