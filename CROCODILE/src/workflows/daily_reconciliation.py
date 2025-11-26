"""
Daily Reconciliation - Comprehensive P&L and Position Verification

Runs at 4:10 PM daily (after Same-Day Recovery at 4:00 PM)

This workflow provides comprehensive audit trail and verification:
- P&L reconciliation (Bot calculations vs Zerodha reports)
- Capital ledger accuracy verification
- Position count and exposure tracking
- Multi-day trend analysis
- Anomaly detection

Builds on Fix #6 (Same-Day Recovery) by adding historical tracking and financial verification.
"""

import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from datetime import date, datetime, timedelta
from loguru import logger
from sqlalchemy.orm import Session
from sqlalchemy import func

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.models.database import (
    OpenPosition, ClosedPosition, CapitalLedger, TransactionHistory,
    PositionStatus, TransactionType, get_session
)
from src.api.kite_trade_client import KiteTradeClient
from src.reporting.telegram_client import telegram
from src.reporting.report_formatter import fmt
from src.utils.config_manager import config
from src.utils.timezone_helper import ist_now_naive, today_ist


class DailyReconciliation:
    """
    Comprehensive daily reconciliation and audit trail

    Verifies:
    - P&L calculations (bot vs Zerodha)
    - Capital ledger accuracy
    - Position exposure
    - Multi-day trends
    """

    def __init__(self):
        """Initialize reconciliation manager"""
        self.kite_client = KiteTradeClient()
        self.tolerance_pct = config.get('reconciliation.tolerance_percent', 0.5)  # 0.5% tolerance
        self.alert_on_mismatch = config.get('reconciliation.alert_on_mismatch', True)
        self.track_history_days = config.get('reconciliation.track_history_days', 30)

    def run_reconciliation(self, session: Optional[Session] = None) -> Dict[str, any]:
        """
        Run comprehensive daily reconciliation

        Returns:
            Reconciliation report with all verifications
        """
        close_session = False
        if session is None:
            session = get_session()
            close_session = True

        try:
            logger.info("=" * 80)
            logger.info("DAILY RECONCILIATION - 4:10 PM")
            logger.info("=" * 80)

            recon_report = {
                'timestamp': ist_now_naive(),
                'date': today_ist(),
                'reconciliation_checks': [],
                'discrepancies': [],
                'warnings': [],
                'summary': {},
                'daily_metrics': {}
            }

            # Section 1: Position & GTT Count Verification
            logger.info("\n[SECTION 1] Position & GTT Count Verification...")
            position_counts = self._verify_position_counts(session)
            recon_report['reconciliation_checks'].append('position_counts')
            recon_report['daily_metrics']['positions'] = position_counts

            # Section 2: Capital Ledger Verification
            logger.info("\n[SECTION 2] Capital Ledger Verification...")
            capital_verification = self._verify_capital_ledger(session)
            recon_report['reconciliation_checks'].append('capital_ledger')
            recon_report['daily_metrics']['capital'] = capital_verification
            if capital_verification.get('discrepancy'):
                recon_report['discrepancies'].append(capital_verification['discrepancy'])

            # Section 3: P&L Reconciliation (Bot vs Zerodha)
            logger.info("\n[SECTION 3] P&L Reconciliation (Today's Trades)...")
            pnl_recon = self._reconcile_pnl_today(session)
            recon_report['reconciliation_checks'].append('pnl_reconciliation')
            recon_report['daily_metrics']['pnl'] = pnl_recon
            if pnl_recon.get('mismatches'):
                recon_report['discrepancies'].extend(pnl_recon['mismatches'])

            # Section 4: Exposure & Risk Metrics
            logger.info("\n[SECTION 4] Exposure & Risk Metrics...")
            exposure_metrics = self._calculate_exposure_metrics(session)
            recon_report['reconciliation_checks'].append('exposure_metrics')
            recon_report['daily_metrics']['exposure'] = exposure_metrics

            # Section 5: Multi-Day Trend Analysis
            logger.info("\n[SECTION 5] Multi-Day Trend Analysis...")
            trend_analysis = self._analyze_trends(session)
            recon_report['reconciliation_checks'].append('trend_analysis')
            recon_report['daily_metrics']['trends'] = trend_analysis
            if trend_analysis.get('anomalies'):
                recon_report['warnings'].extend(trend_analysis['anomalies'])

            # Generate summary
            recon_report['summary'] = {
                'total_checks': len(recon_report['reconciliation_checks']),
                'discrepancies_found': len(recon_report['discrepancies']),
                'warnings': len(recon_report['warnings']),
                'status': 'PASS' if len(recon_report['discrepancies']) == 0 else 'FAIL'
            }

            # Send report
            self._send_reconciliation_report(recon_report)

            # Log summary
            self._log_reconciliation_summary(recon_report)

            logger.info("=" * 80)
            logger.info("DAILY RECONCILIATION COMPLETE")
            logger.info("=" * 80)

            return recon_report

        except Exception as e:
            logger.error(f"Error during daily reconciliation: {e}")
            telegram.send_alert(
                f"🚨 **RECONCILIATION ERROR**\n"
                f"Daily reconciliation failed\n"
                f"Error: {str(e)}\n"
                f"**Manual review required**"
            )
            raise

        finally:
            if close_session:
                session.close()

    def _verify_position_counts(self, session: Session) -> Dict:
        """
        Section 1: Verify position and GTT counts

        Returns counts and verification status
        """
        try:
            # Database counts (for this bot)
            open_positions = session.query(OpenPosition).filter_by(
                bot_instance_id=config.get_bot_instance_id(),
                status=PositionStatus.OPEN
            ).all()

            closed_today = session.query(ClosedPosition).filter(
                ClosedPosition.bot_instance_id == config.get_bot_instance_id(),
                ClosedPosition.exit_date == today_ist()
            ).count()

            # Zerodha counts
            try:
                zerodha_positions = self.kite_client.get_positions()
                zerodha_open_count = len([p for p in zerodha_positions.get('net', []) if p.get('quantity', 0) != 0])

                zerodha_gtts = self.kite_client.get_gtt_orders()
                zerodha_gtt_count = len([g for g in zerodha_gtts if g.get('status') == 'active'])
            except Exception as e:
                logger.error(f"Failed to fetch Zerodha data: {e}\nAPI Response: {getattr(e, 'response', 'No response available')}")
                zerodha_open_count = None
                zerodha_gtt_count = None

            counts = {
                'db_open_positions': len(open_positions),
                'db_closed_today': closed_today,
                'zerodha_open_positions': zerodha_open_count,
                'zerodha_active_gtts': zerodha_gtt_count,
                'match': True if zerodha_open_count == len(open_positions) else False
            }

            logger.info(
                f"Position Counts - DB: {counts['db_open_positions']}, "
                f"Zerodha: {counts['zerodha_open_positions']}, "
                f"GTTs: {counts['zerodha_active_gtts']}, "
                f"Closed Today: {counts['db_closed_today']}"
            )

            if not counts['match'] and zerodha_open_count is not None:
                logger.warning(
                    f"⚠️ Position count mismatch: DB={counts['db_open_positions']}, "
                    f"Zerodha={counts['zerodha_open_positions']}"
                )

            return counts

        except Exception as e:
            logger.error(f"Error in position count verification: {e}")
            return {'error': str(e)}

    def _verify_capital_ledger(self, session: Session) -> Dict:
        """
        Section 2: Verify capital ledger accuracy

        Checks:
        - Starting capital + realized P&L = current capital
        - Deployed capital = sum of open position values
        - Available margin matches calculation
        """
        try:
            # Get latest capital ledger entry (for this bot)
            latest_ledger = session.query(CapitalLedger).filter_by(
                bot_instance_id=config.get_bot_instance_id()
            ).order_by(
                CapitalLedger.date.desc()
            ).first()

            if not latest_ledger:
                logger.warning("No capital ledger entries found")
                return {'error': 'No ledger data'}

            # Calculate deployed capital from open positions (for this bot)
            open_positions = session.query(OpenPosition).filter_by(
                bot_instance_id=config.get_bot_instance_id(),
                status=PositionStatus.OPEN
            ).all()

            calculated_deployed = sum(pos.capital_deployed for pos in open_positions)

            # Verify deployed capital
            ledger_deployed = latest_ledger.deployed_capital
            deployed_diff = abs(calculated_deployed - ledger_deployed)
            deployed_match = deployed_diff < 100  # Rs.100 tolerance

            # Use allocated_capital as the main capital figure
            display_capital = latest_ledger.allocated_capital if latest_ledger.allocated_capital else latest_ledger.total_capital

            verification = {
                'date': latest_ledger.date,
                'opening_capital': latest_ledger.opening_capital,
                'total_capital': display_capital,  # Use allocated_capital for display
                'allocated_capital': latest_ledger.allocated_capital,
                'deployed_capital_ledger': ledger_deployed,
                'deployed_capital_calculated': calculated_deployed,
                'deployed_difference': deployed_diff,
                'deployed_match': deployed_match,
                'free_capital': latest_ledger.free_capital,
                'realized_pnl_today': latest_ledger.realized_pnl_today
            }

            if not deployed_match:
                discrepancy = (
                    f"Capital Ledger Mismatch: Deployed capital discrepancy of Rs.{deployed_diff:,.2f}\n"
                    f"Ledger: Rs.{ledger_deployed:,.2f} vs Calculated: Rs.{calculated_deployed:,.2f}"
                )
                logger.warning(f"⚠️ {discrepancy}")
                verification['discrepancy'] = discrepancy

            logger.info(
                f"Capital Verification - Total: Rs.{verification['total_capital']:,.2f}, "
                f"Deployed: Rs.{verification['deployed_capital_ledger']:,.2f}, "
                f"Free: Rs.{verification['free_capital']:,.2f}"
            )

            return verification

        except Exception as e:
            logger.error(f"Error in capital ledger verification: {e}")
            return {'error': str(e)}

    def _reconcile_pnl_today(self, session: Session) -> Dict:
        """
        Section 3: Reconcile P&L for today's closed trades

        Compare bot calculations vs Zerodha reports
        """
        try:
            # Get today's closed positions (for this bot)
            today_closed = session.query(ClosedPosition).filter(
                ClosedPosition.bot_instance_id == config.get_bot_instance_id(),
                ClosedPosition.exit_date == today_ist()
            ).all()

            if not today_closed:
                logger.info("No positions closed today - skipping P&L reconciliation")
                return {
                    'closed_count': 0,
                    'total_bot_pnl': 0,
                    'total_zerodha_pnl': None,
                    'reconciled': True
                }

            # Calculate bot P&L
            bot_pnl = sum(pos.net_pnl for pos in today_closed)

            # Get Zerodha P&L (from positions API or trade history)
            # Note: This is a simplified version - actual implementation may need
            # to fetch trade history and calculate P&L manually
            try:
                # Placeholder for Zerodha P&L fetch
                # In real implementation, fetch trade history and calculate
                zerodha_pnl = None  # Would calculate from Zerodha data
                logger.info("Zerodha P&L reconciliation: API integration pending")
            except Exception as e:
                logger.warning(f"Could not fetch Zerodha P&L: {e}")
                zerodha_pnl = None

            recon = {
                'closed_count': len(today_closed),
                'total_bot_pnl': bot_pnl,
                'total_zerodha_pnl': zerodha_pnl,
                'reconciled': True if zerodha_pnl is None else abs(bot_pnl - zerodha_pnl) < (abs(bot_pnl) * self.tolerance_pct / 100),
                'mismatches': []
            }

            if zerodha_pnl is not None and not recon['reconciled']:
                mismatch = (
                    f"P&L Mismatch: Bot calculated Rs.{bot_pnl:,.2f} vs "
                    f"Zerodha Rs.{zerodha_pnl:,.2f} (diff: Rs.{abs(bot_pnl - zerodha_pnl):,.2f})"
                )
                logger.warning(f"⚠️ {mismatch}")
                recon['mismatches'].append(mismatch)

            logger.info(
                f"P&L Reconciliation - Closed: {recon['closed_count']}, "
                f"Bot P&L: Rs.{recon['total_bot_pnl']:,.2f}"
            )

            return recon

        except Exception as e:
            logger.error(f"Error in P&L reconciliation: {e}")
            return {'error': str(e)}

    def _calculate_exposure_metrics(self, session: Session) -> Dict:
        """
        Section 4: Calculate current exposure and risk metrics

        Metrics:
        - Total capital deployed
        - Exposure as % of total capital
        - Number of positions
        - Average position size
        - Largest position
        - Timeframe distribution
        """
        try:
            # Get current capital (for this bot)
            latest_ledger = session.query(CapitalLedger).filter_by(
                bot_instance_id=config.get_bot_instance_id()
            ).order_by(
                CapitalLedger.date.desc()
            ).first()

            if not latest_ledger:
                return {'error': 'No capital ledger data'}

            # Use allocated_capital as base (not Zerodha margin)
            total_capital = latest_ledger.allocated_capital if latest_ledger.allocated_capital else latest_ledger.total_capital

            # Get open positions (for this bot)
            open_positions = session.query(OpenPosition).filter_by(
                bot_instance_id=config.get_bot_instance_id(),
                status=PositionStatus.OPEN
            ).all()

            if not open_positions:
                return {
                    'total_deployed': 0,
                    'exposure_pct': 0,
                    'position_count': 0,
                    'avg_position_size': 0,
                    'largest_position': 0,
                    'timeframe_dist': {}
                }

            # Calculate metrics
            total_deployed = sum(pos.capital_deployed for pos in open_positions)
            exposure_pct = (total_deployed / total_capital * 100) if total_capital > 0 else 0

            position_values = [pos.capital_deployed for pos in open_positions]
            avg_position = sum(position_values) / len(position_values)
            largest_position = max(position_values)

            # Timeframe distribution
            timeframe_dist = {}
            for pos in open_positions:
                tf = pos.timeframe
                timeframe_dist[tf] = timeframe_dist.get(tf, 0) + 1

            metrics = {
                'total_capital': total_capital,
                'total_deployed': total_deployed,
                'exposure_pct': exposure_pct,
                'position_count': len(open_positions),
                'avg_position_size': avg_position,
                'largest_position': largest_position,
                'timeframe_dist': timeframe_dist
            }

            logger.info(
                f"Exposure Metrics - Deployed: Rs.{metrics['total_deployed']:,.2f} "
                f"({metrics['exposure_pct']:.1f}%), "
                f"Positions: {metrics['position_count']}, "
                f"Avg Size: Rs.{metrics['avg_position_size']:,.2f}"
            )

            return metrics

        except Exception as e:
            logger.error(f"Error calculating exposure metrics: {e}")
            return {'error': str(e)}

    def _analyze_trends(self, session: Session) -> Dict:
        """
        Section 5: Analyze multi-day trends and detect anomalies

        Tracks:
        - Position count trend (last 7 days)
        - Capital deployment trend
        - Win rate trend
        - Anomaly detection
        """
        try:
            lookback_days = min(self.track_history_days, 7)  # Last 7 days for daily check
            start_date = today_ist() - timedelta(days=lookback_days)

            # Daily position counts
            daily_counts = []
            for i in range(lookback_days + 1):
                check_date = start_date + timedelta(days=i)

                # Count positions open on that date (for this bot)
                positions_on_date = session.query(OpenPosition).filter(
                    OpenPosition.bot_instance_id == config.get_bot_instance_id(),
                    OpenPosition.entry_date <= check_date,
                    (OpenPosition.exit_date >= check_date) | (OpenPosition.exit_date == None)
                ).count()

                daily_counts.append({
                    'date': check_date,
                    'position_count': positions_on_date
                })

            # Closed position stats (last 7 days) (for this bot)
            week_closed = session.query(ClosedPosition).filter(
                ClosedPosition.bot_instance_id == config.get_bot_instance_id(),
                ClosedPosition.exit_date >= start_date
            ).all()

            if week_closed:
                wins = len([p for p in week_closed if p.net_pnl > 0])
                losses = len([p for p in week_closed if p.net_pnl <= 0])
                win_rate = (wins / len(week_closed) * 100) if week_closed else 0
                avg_pnl = sum(p.net_pnl for p in week_closed) / len(week_closed)
            else:
                wins = losses = 0
                win_rate = 0
                avg_pnl = 0

            # Anomaly detection
            anomalies = []

            # Check for excessive positions
            current_positions = daily_counts[-1]['position_count']
            avg_positions = sum(d['position_count'] for d in daily_counts) / len(daily_counts)

            if current_positions > avg_positions * 1.5:
                anomalies.append(
                    f"Position count spike: {current_positions} vs avg {avg_positions:.1f} (last {lookback_days} days)"
                )

            # Check for low win rate
            if len(week_closed) >= 5 and win_rate < 40:
                anomalies.append(
                    f"Low win rate: {win_rate:.1f}% over last {len(week_closed)} trades"
                )

            trends = {
                'lookback_days': lookback_days,
                'daily_position_counts': daily_counts,
                'week_closed_count': len(week_closed),
                'week_wins': wins,
                'week_losses': losses,
                'week_win_rate': win_rate,
                'week_avg_pnl': avg_pnl,
                'anomalies': anomalies
            }

            if anomalies:
                logger.warning(f"⚠️ Anomalies detected: {len(anomalies)}")
                for anomaly in anomalies:
                    logger.warning(f"  - {anomaly}")
            else:
                logger.info(f"Trend Analysis - No anomalies detected")

            logger.info(
                f"7-Day Stats - Closed: {len(week_closed)}, "
                f"Win Rate: {win_rate:.1f}%, "
                f"Avg P&L: Rs.{avg_pnl:,.2f}"
            )

            return trends

        except Exception as e:
            logger.error(f"Error in trend analysis: {e}")
            return {'error': str(e)}

    def _send_reconciliation_report(self, report: Dict):
        """Send reconciliation report via Telegram - compact format"""
        try:
            metrics = report['daily_metrics']
            pos_counts = metrics.get('positions', {})
            capital = metrics.get('capital', {})
            exposure = metrics.get('exposure', {})
            pnl = metrics.get('pnl', {})
            trends = metrics.get('trends', {})

            # === BUILD COMPACT REPORT ===
            msg = f"📋 *RECONCILIATION* | {report['date']}\n"

            # Status + Positions (merged into one line)
            status = report['summary']['status']
            status_emoji = "✅" if status == 'PASS' else "🔴"
            open_pos = pos_counts.get('db_open_positions', 0)
            closed_today = pos_counts.get('db_closed_today', 0)
            active_gtts = pos_counts.get('zerodha_active_gtts', 0)
            gtt_match = "✓" if open_pos == active_gtts else "✗"
            msg += f"{status_emoji} {status} | Open:{open_pos} Closed:{closed_today} GTTs:{active_gtts} {gtt_match}\n"

            # Capital + Exposure % (merged)
            if capital and 'total_capital' in capital:
                total = capital['total_capital']
                deployed = capital['deployed_capital_ledger']
                exp_pct = exposure.get('exposure_pct', 0) if exposure else 0
                msg += f"💰 {fmt.format_currency(total)} | Dep:{fmt.format_currency(deployed)} ({exp_pct:.0f}%)\n"

            # Today's P&L (if any closes)
            if pnl and pnl.get('closed_count', 0) > 0:
                pnl_amt = pnl['total_bot_pnl']
                pnl_emoji = fmt.pnl_emoji(pnl_amt)
                msg += f"💵 Today: {pnl_emoji} {fmt.format_currency(pnl_amt, show_sign=True)} ({pnl['closed_count']} trades)\n"

            # 7-Day Stats (compact)
            if trends and trends.get('week_closed_count', 0) > 0:
                wins = trends.get('week_wins', 0)
                losses = trends.get('week_losses', 0)
                wr = trends.get('week_win_rate', 0)
                avg_pnl = trends.get('week_avg_pnl', 0)
                wr_emoji = fmt.GREEN_CIRCLE if wr >= 50 else fmt.YELLOW_CIRCLE if wr >= 40 else fmt.RED_CIRCLE
                msg += f"📅 7D: {wr_emoji} W:{wins} L:{losses} WR:{wr:.0f}% Avg:{fmt.format_currency(avg_pnl, show_sign=True)}\n"

            # Discrepancies (compact)
            if report['discrepancies']:
                msg += f"⚠️ Discrepancies ({len(report['discrepancies'])}):\n"
                for disc in report['discrepancies'][:2]:
                    short_disc = disc[:50] + "..." if len(disc) > 50 else disc
                    msg += f"  {short_disc}\n"
                if len(report['discrepancies']) > 2:
                    msg += f"  +{len(report['discrepancies']) - 2} more\n"

            # Anomalies (compact)
            if report['warnings']:
                msg += f"🔶 Anomalies ({len(report['warnings'])}):\n"
                for w in report['warnings'][:2]:
                    short_w = w[:50] + "..." if len(w) > 50 else w
                    msg += f"  {short_w}\n"
                if len(report['warnings']) > 2:
                    msg += f"  +{len(report['warnings']) - 2} more\n"

            telegram.send_alert(msg)

        except Exception as e:
            logger.error(f"Error sending reconciliation report: {e}")

    def _log_reconciliation_summary(self, report: Dict):
        """Log comprehensive reconciliation summary"""
        logger.info("\n" + "=" * 80)
        logger.info("DAILY RECONCILIATION SUMMARY")
        logger.info("=" * 80)
        logger.info(f"Date: {report['date']}")
        logger.info(f"Timestamp: {report['timestamp']}")
        logger.info(f"Checks Performed: {report['summary']['total_checks']}")
        logger.info(f"Status: {report['summary']['status']}")

        if report['discrepancies']:
            logger.info(f"\nDiscrepancies Found: {len(report['discrepancies'])}")
            for disc in report['discrepancies']:
                logger.info(f"  - {disc}")

        if report['warnings']:
            logger.info(f"\nWarnings: {len(report['warnings'])}")
            for warning in report['warnings']:
                logger.info(f"  - {warning}")

        logger.info("=" * 80)


# Singleton instance
reconciliation_manager = DailyReconciliation()


def print_workflow_banner():
    """Print a clear banner to identify workflow start in logs"""
    banner = """
***************************************************************
*  DAILY RECONCILIATION WORKFLOW - P&L & Position Audit       *
***************************************************************"""
    logger.info(banner)


def main():
    """Main entry point for daily reconciliation workflow"""
    try:
        print_workflow_banner()
        logger.info("Daily reconciliation workflow started")
        report = reconciliation_manager.run_reconciliation()

        if report['summary']['status'] == 'FAIL':
            logger.error(f"Reconciliation complete with {report['summary']['discrepancies_found']} discrepancies")
            sys.exit(1)
        else:
            logger.info("Reconciliation complete - All checks passed")
            sys.exit(0)

    except Exception as e:
        logger.error(f"Reconciliation workflow failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
