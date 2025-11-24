"""Daily Report Workflow - 4:10 PM Daily"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from loguru import logger
from datetime import date

from src.utils.config_manager import config
from src.reporting.report_generator import report_generator


def print_workflow_banner():
    """Print a clear banner to identify workflow start in logs"""
    banner = """
***************************************************************
*  DAILY REPORT WORKFLOW - Generate & Send EOD Report         *
***************************************************************"""
    logger.info(banner)


def send_daily_report():
    """
    Generate and send daily EOD report

    Run at 4:10 PM daily
    """
    print_workflow_banner()
    logger.info("Daily report workflow started")

    try:
        # Generate and send daily report
        report_generator.send_daily_report()

        # Also send weekly report if Friday
        if date.today().weekday() == 4:  # Friday
            logger.info("Friday detected - sending weekly report")
            report_generator.send_weekly_report()

        logger.info("✅ Daily reporting complete")
        return True

    except Exception as e:
        logger.error(f"Daily reporting failed: {e}", exc_info=True)
        return False


if __name__ == "__main__":
    # Setup logging
    logger.add(
        "logs/crocodile_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="45 days",
        level=config.get('logging.level', 'INFO')
    )

    success = send_daily_report()
    sys.exit(0 if success else 1)
