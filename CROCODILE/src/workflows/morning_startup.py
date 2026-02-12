"""Morning Startup Workflow - 9:00 AM Daily"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from loguru import logger
from datetime import date, timedelta
from typing import Dict, List

from src.utils.config_manager import config
from src.models.database import init_database, get_session, OpenPosition, PositionStatus
from src.services.capital_manager import capital_manager
from src.services.exit_manager import exit_manager
from src.reporting.telegram_client import telegram
from src.api.broker_factory import get_broker, validate_kite_api_token


def check_missed_gtt_updates(session) -> Dict[str, List[str]]:
    """
    Catch-up check for missed GTT updates (self-healing mechanism)

    Checks and fixes:
    1. Weekly positions - if Monday and last update was previous week
    2. Monthly positions - if first 3 days of month and last update was previous month

    Handles cases:
    - Market holidays at week/month end
    - Bot downtime
    - Any other missed updates

    Returns:
        Dict with 'weekly_fixed' and 'monthly_fixed' lists of script names
    """
    today = date.today()
    results = {
        'weekly_fixed': [],
        'monthly_fixed': [],
        'errors': []
    }

    logger.info("Running catch-up check for missed GTT updates...")

    try:
        # Get all open positions (for this bot)
        open_positions = session.query(OpenPosition).filter_by(
            bot_instance_id=config.get_bot_instance_id(),
            status=PositionStatus.OPEN
        ).all()

        if not open_positions:
            logger.info("No open positions to check")
            return results

        # ========== CHECK WEEKLY POSITIONS ==========
        # If today is Monday, check if weekly positions missed Friday update
        if today.weekday() == 0:  # Monday
            logger.info("Monday detected - checking weekly positions for missed updates...")

            for position in open_positions:
                if position.timeframe.upper() != 'W':
                    continue

                # Check if last update was in previous week
                if position.last_sl_update:
                    last_update_date = position.last_sl_update.date()

                    # Get last Friday
                    days_since_friday = (today.weekday() - 4) % 7  # Friday = 4
                    if days_since_friday == 0:
                        days_since_friday = 7  # If today is Friday, go back 7 days
                    last_friday = today - timedelta(days=days_since_friday + 2)  # Previous Friday

                    # If last update was before last Friday, we missed it
                    if last_update_date < last_friday:
                        logger.warning(
                            f"Weekly position {position.script} missed GTT update. "
                            f"Last update: {last_update_date}, Expected: {last_friday}. "
                            f"Updating now with last week's LOW."
                        )

                        # Calculate trailing SL for last week
                        trailing_sl = exit_manager.get_trailing_sl_for_timeframe(
                            position,
                            last_friday
                        )

                        if trailing_sl:
                            # Update GTT now
                            success, error, _ = exit_manager.update_gtt_with_trailing_sl(
                                position,
                                trailing_sl,
                                session
                            )

                            if success:
                                results['weekly_fixed'].append(position.script)
                                logger.info(f"✅ Catch-up update for {position.script}: Rs.{trailing_sl:.2f}")
                            else:
                                results['errors'].append(f"{position.script} (W): {error}")
                                logger.error(f"Failed catch-up for {position.script}: {error}")
                        else:
                            results['errors'].append(f"{position.script} (W): No historical data")

        # ========== CHECK MONTHLY POSITIONS ==========
        # If first 3 days of month, check if monthly positions missed month-end update
        if today.day <= 3:
            logger.info(f"Day {today.day} of month - checking monthly positions for missed updates...")

            for position in open_positions:
                if position.timeframe.upper() != 'M':
                    continue

                # Check if last update was in previous month
                if position.last_sl_update:
                    last_update_date = position.last_sl_update.date()

                    # Get last month-end trading day
                    first_of_this_month = today.replace(day=1)
                    last_day_of_prev_month = first_of_this_month - timedelta(days=1)

                    # Walk back to find last trading day of previous month
                    last_trading_day_prev_month = last_day_of_prev_month
                    while last_trading_day_prev_month.weekday() >= 5:  # Skip weekends
                        last_trading_day_prev_month -= timedelta(days=1)

                    # If last update was before last month's end, we missed it
                    if last_update_date.month != today.month and last_update_date < last_trading_day_prev_month:
                        logger.warning(
                            f"Monthly position {position.script} missed GTT update. "
                            f"Last update: {last_update_date}, Expected: {last_trading_day_prev_month}. "
                            f"Updating now with last month's LOW."
                        )

                        # Calculate trailing SL for last month
                        trailing_sl = exit_manager.get_trailing_sl_for_timeframe(
                            position,
                            last_trading_day_prev_month
                        )

                        if trailing_sl:
                            # Update GTT now
                            success, error, _ = exit_manager.update_gtt_with_trailing_sl(
                                position,
                                trailing_sl,
                                session
                            )

                            if success:
                                results['monthly_fixed'].append(position.script)
                                logger.info(f"✅ Catch-up update for {position.script}: Rs.{trailing_sl:.2f}")
                            else:
                                results['errors'].append(f"{position.script} (M): {error}")
                                logger.error(f"Failed catch-up for {position.script}: {error}")
                        else:
                            results['errors'].append(f"{position.script} (M): No historical data")

        # Log summary
        total_fixed = len(results['weekly_fixed']) + len(results['monthly_fixed'])
        if total_fixed > 0:
            logger.info(
                f"Catch-up completed: {len(results['weekly_fixed'])} weekly, "
                f"{len(results['monthly_fixed'])} monthly positions fixed"
            )
        else:
            logger.info("No missed updates detected - all positions up to date")

        return results

    except Exception as e:
        logger.error(f"Error in catch-up check: {e}", exc_info=True)
        results['errors'].append(f"System error: {str(e)}")
        return results


def print_workflow_banner():
    """Print a clear banner to identify workflow start in logs"""
    banner = """
***************************************************************
*  MORNING STARTUP WORKFLOW - Initialize Bot & Check Capital  *
***************************************************************"""
    logger.info(banner)


def morning_startup():
    """
    Morning startup workflow - Run at 9:00 AM

    Tasks:
    1. Initialize database
    2. Generate Kite token
    3. Fetch margin from Zerodha
    4. Check margin threshold
    5. Update capital ledger
    6. Check drawdown status
    7. Catch-up check for missed GTT updates (self-healing)
    8. Send status alerts
    """
    print_workflow_banner()
    logger.info("Morning startup workflow started")

    try:
        # Initialize database
        logger.info("Initializing database...")
        init_database()

        # Validate Kite API token if using kite_api method
        logger.info("Validating broker configuration...")
        token_valid, token_message = validate_kite_api_token()
        if not token_valid:
            error_msg = f"🔴 *Kite API Token Invalid*\n❌ {token_message}"
            logger.error(error_msg)
            telegram.send_alert(error_msg, critical=True)
            return False
        logger.info(f"Token validation: {token_message}")

        # Initialize broker client (enctoken or kite_api based on config)
        logger.info("Initializing broker client...")
        kite_client = get_broker()
        logger.info(f"Using trade method: {kite_client.trade_method}")

        # Validate connection
        if not kite_client.validate_connection():
            error_msg = "🔴 *Kite API Connection Failed*\n❌ Unable to connect to Zerodha\n⚠️ Check credentials and network"
            logger.error(error_msg)
            telegram.send_alert(error_msg, critical=True)
            return False

        logger.info("✅ Kite API connection validated")

        # Fetch margin and check threshold
        logger.info("Fetching margin from Zerodha...")
        margin_data = capital_manager.fetch_margin_from_zerodha()
        available_margin = margin_data['net']

        is_sufficient, alert_msg = capital_manager.check_margin_threshold(available_margin)

        if not is_sufficient:
            logger.warning(f"Low margin detected: Rs.{available_margin:.2f}")
            telegram.send_alert(alert_msg, critical=True)
        else:
            logger.info(f"✅ Margin check passed: Rs.{available_margin:.2f}")

        # Update capital ledger
        logger.info("Updating capital ledger...")
        session = get_session()
        try:
            ledger = capital_manager.update_capital_ledger(session)

            # Check drawdown status
            dd_status = capital_manager.get_drawdown_status(session)

            if dd_status['alert_level'] in ['CAUTION', 'CRITICAL']:
                logger.warning(f"Drawdown alert: {dd_status['alert_level']} - {dd_status['drawdown_pct']:.2f}%")
                telegram.send_alert(
                    dd_status['alert_message'],
                    critical=(dd_status['alert_level'] == 'CRITICAL')
                )

            # Catch-up check for missed GTT updates (self-healing)
            logger.info("Running catch-up check for missed GTT updates...")
            catchup_results = check_missed_gtt_updates(session)

            # Send alerts for catch-up fixes
            if catchup_results['weekly_fixed'] or catchup_results['monthly_fixed']:
                catchup_msg = "🔧 *GTT Catch-up Updates*\n\n"

                if catchup_results['weekly_fixed']:
                    catchup_msg += f"📅 *Weekly positions fixed:*\n"
                    for script in catchup_results['weekly_fixed']:
                        catchup_msg += f"  • {script}\n"

                if catchup_results['monthly_fixed']:
                    catchup_msg += f"\n📆 *Monthly positions fixed:*\n"
                    for script in catchup_results['monthly_fixed']:
                        catchup_msg += f"  • {script}\n"

                catchup_msg += "\n✅ Missed updates detected and corrected\n"
                catchup_msg += "ℹ️ Reason: Market holiday or bot downtime"

                telegram.send_alert(catchup_msg, critical=False)

            # Alert on catch-up errors
            if catchup_results['errors']:
                error_msg = "⚠️ *Catch-up Update Errors*\n\n"
                for error in catchup_results['errors']:
                    error_msg += f"❌ {error}\n"
                telegram.send_alert(error_msg, critical=True)

            # Send morning status message
            status_msg = (
                f"🌅 *Good Morning - Bot Started*\n\n"
                f"📅 Date: {date.today().strftime('%d %b %Y (%A)')}\n"
                f"💰 Available Margin: Rs.{available_margin:,.2f}\n"
                f"📊 Open Positions: {ledger.num_open_positions}\n"
                f"📉 Monthly DD: {ledger.monthly_drawdown_pct:.2f}%\n"
                f"✅ Status: Ready for trading"
            )

            if config.is_test_mode():
                status_msg += "\n\n⚠️ *TEST MODE ACTIVE* - Trading with 1 qty per position"

            telegram.send_alert(status_msg, critical=False)

            logger.info("✅ Morning startup completed successfully")
            return True

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Morning startup failed: {e}", exc_info=True)
        error_msg = f"🔴 *Morning Startup Failed*\n❌ Error: {str(e)}\n⚠️ Check logs for details"
        telegram.send_alert(error_msg, critical=True)
        return False


if __name__ == "__main__":
    # Setup logging
    logger.add(
        "logs/crocodile_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="45 days",
        level=config.get('logging.level', 'INFO')
    )

    success = morning_startup()
    sys.exit(0 if success else 1)
