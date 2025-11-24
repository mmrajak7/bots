"""EOD GTT Update Workflow - 3:50 PM Daily"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from loguru import logger

from src.utils.config_manager import config
from src.services.exit_manager import exit_manager
from src.reporting.telegram_client import telegram


def print_workflow_banner():
    """Print a clear banner to identify workflow start in logs"""
    banner = """
***************************************************************
*  EOD GTT UPDATE WORKFLOW - Update Stop Loss with Day's LOW  *
***************************************************************"""
    logger.info(banner)


def update_gtt_eod():
    """
    Update all GTT orders with today's LOW

    Run at 3:50 PM daily (after market close at 3:30 PM)
    """
    print_workflow_banner()
    logger.info("EOD GTT update workflow started")

    try:
        # Update all positions with today's LOW
        stats = exit_manager.update_all_positions_eod()

        logger.info(
            f"EOD GTT update complete: Total={stats['total_positions']}, "
            f"Updated={stats['updated']}, NoChange={stats['no_change']}, "
            f"Failed={stats['failed']}"
        )

        # Alert on failures
        if stats['failed'] > 0:
            error_details = "\n".join(f"• {err}" for err in stats['errors'][:5])  # First 5 errors
            alert_msg = (
                f"⚠️ **EOD GTT Update - Issues Detected**\n\n"
                f"**Summary:**\n"
                f"• Total positions: {stats['total_positions']}\n"
                f"• Successfully updated: {stats['updated']}\n"
                f"• No change needed: {stats['no_change']}\n"
                f"• Failed: {stats['failed']}\n\n"
                f"**Errors:**\n{error_details}\n\n"
                f"⚠️ **Action required:** Review failed positions manually"
            )
            telegram.send_alert(alert_msg, critical=True)
        elif stats['updated'] > 0:
            # Success message with detailed table of updates
            lines = [
                f"✅ **EOD GTT Update Complete**\n",
                f"**Summary:** {stats['updated']} position(s) updated\n",
                "<pre>"
            ]

            # Header
            lines.append(f"{'Script':<12} {'TF':<3} {'Old SL':>9} {'New SL':>9} {'Change':>9}")
            lines.append("-" * 50)

            # Update rows
            for update in stats['updates']:
                old_sl_str = f"Rs.{update['old_sl']:.2f}"
                new_sl_str = f"Rs.{update['new_sl']:.2f}"
                change = update['new_sl'] - update['old_sl']
                change_str = f"+Rs.{change:.2f}" if change >= 0 else f"Rs.{change:.2f}"

                lines.append(
                    f"{update['script']:<12} {update['timeframe']:<3} "
                    f"{old_sl_str:>9} {new_sl_str:>9} {change_str:>9}"
                )

            lines.append("</pre>")

            # Add summary footer
            if stats['no_change'] > 0 or stats['skipped'] > 0:
                lines.append(f"\n**Other Positions:**")
                if stats['no_change'] > 0:
                    lines.append(f"• No change needed: {stats['no_change']}")
                if stats['skipped'] > 0:
                    lines.append(f"• Skipped (wrong day): {stats['skipped']}")

            lines.append(f"\n✓ All positions protected with updated GTTs")

            alert_msg = "\n".join(lines)
            telegram.send_alert(alert_msg, critical=False)
        else:
            # No updates but workflow ran successfully
            alert_msg = (
                f"✅ **EOD GTT Update Complete**\n\n"
                f"• Total positions monitored: {stats['total_positions']}\n"
                f"• No GTT updates needed today\n"
                f"• All positions protected ✓"
            )
            telegram.send_alert(alert_msg, critical=False)

        return True

    except Exception as e:
        logger.error(f"EOD GTT update failed: {e}", exc_info=True)
        error_msg = (
            f"🔴 *CRITICAL: EOD GTT Update Failed*\n"
            f"❌ Error: {str(e)}\n"
            f"⚠️ Positions may be unprotected!\n"
            f"🛠️ Manual intervention required"
        )
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

    success = update_gtt_eod()
    sys.exit(0 if success else 1)
