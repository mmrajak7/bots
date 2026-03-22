"""CLI entry point: python -m playbook.st_watch [command]"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta

import pytz

IST = pytz.timezone('Asia/Kolkata')
SCAN_INTERVAL = 3600  # 1 hour


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )


def cmd_scan(args):
    """One-shot scan: compute ST, check gaps, send alerts."""
    from .watcher import scan, print_status
    results = scan(dry_run=args.dry_run, symbol_filter=args.symbol)
    print_status(results)


def cmd_status(args):
    """Show current gap status (full scan, no Telegram alerts sent)."""
    from .watcher import scan, print_status
    results = scan(dry_run=True, symbol_filter=args.symbol)
    print_status(results)


def cmd_run(args):
    """Single scan with market hours check — designed for hourly cron."""
    now = datetime.now(IST)

    if now.weekday() >= 5:
        logging.info("Weekend — skipping")
        return

    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)

    if now < market_open or now > market_close:
        logging.info("Outside market hours (%s) — skipping", now.strftime('%H:%M'))
        return

    logging.info("Market open, running ST Watch scan...")
    from .watcher import scan
    results = scan(dry_run=args.dry_run)

    close_count = sum(1 for r in results if abs(r['gap_pct']) <= 5)
    logging.info("Scan complete: %d checks, %d within 5%% of ST",
                 len(results), close_count)


def cmd_cron(args):
    """Long-lived process: scans hourly within market hours.

    Alternative to cron — runs continuously, sleeping between scans.
    """
    logging.info("ST Watch cron mode started (hourly scans)")

    while True:
        now = datetime.now(IST)

        # Weekend: sleep until Monday 9:00
        if now.weekday() >= 5:
            if now.weekday() == 5:  # Saturday
                days_until_monday = 2
            else:  # Sunday
                days_until_monday = 1
            wake_time = now.replace(hour=9, minute=0, second=0, microsecond=0) + \
                        timedelta(days=days_until_monday)
            sleep_secs = (wake_time - now).total_seconds()
            logging.info("Weekend — sleeping %.0f hours until Monday 9:00",
                         sleep_secs / 3600)
            time.sleep(max(sleep_secs, 60))
            continue

        market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)

        if now < market_open:
            wait = (market_open - now).total_seconds()
            logging.info("Waiting %.0f min for market open...", wait / 60)
            time.sleep(max(wait, 60))
            continue

        if now > market_close:
            logging.info("Market closed — exiting cron mode")
            break

        # Run scan
        try:
            from .watcher import scan
            results = scan(dry_run=args.dry_run)
            close_count = sum(1 for r in results if abs(r['gap_pct']) <= 5)
            logging.info("Scan done: %d checks, %d within 5%%", len(results), close_count)
        except Exception as e:
            logging.error("Scan failed: %s", e, exc_info=True)

        # Sleep until next scan, but ensure a final scan near market close
        now_after = datetime.now(IST)
        next_scan = now_after + timedelta(seconds=SCAN_INTERVAL)

        if next_scan > market_close:
            # Check if there's time for one more scan before close
            remaining = (market_close - now_after).total_seconds()
            if remaining > 300:  # at least 5 min left
                logging.info("Final scan in %.0f min (before close)", remaining / 60 - 5)
                time.sleep(remaining - 300)
                continue
            logging.info("Market closing — exiting cron mode")
            break

        time.sleep(SCAN_INTERVAL)


def cmd_history(args):
    """Show alert history from persistent log."""
    from .alert_store import get_alert_store
    store = get_alert_store()
    store.list_alerts(days=args.days, symbol=args.symbol)


def main():
    parser = argparse.ArgumentParser(
        prog='python -m playbook.st_watch',
        description='ST Watch — Core & Tactical basket ST touch monitor'
    )
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Debug logging')
    sub = parser.add_subparsers(dest='command')

    # scan (one-shot, sends alerts)
    p_scan = sub.add_parser('scan', help='One-shot scan (sends alerts)')
    p_scan.add_argument('--dry-run', action='store_true',
                        help='Show alerts without sending Telegram')
    p_scan.add_argument('--symbol', help='Check only this symbol')
    p_scan.set_defaults(func=cmd_scan)

    # status (dry-run scan, just display)
    p_status = sub.add_parser('status', help='Show gap status (no alerts)')
    p_status.add_argument('--symbol', help='Check only this symbol')
    p_status.set_defaults(func=cmd_status)

    # history (alert log)
    p_hist = sub.add_parser('history', help='Show alert history')
    p_hist.add_argument('--days', type=int, default=7, help='Days to look back (default: 7)')
    p_hist.add_argument('--symbol', help='Filter by symbol')
    p_hist.set_defaults(func=cmd_history)

    # run (single invocation, market hours check — for hourly cron)
    p_run = sub.add_parser('run', help='Single scan with market hours check (cron target)')
    p_run.add_argument('--dry-run', action='store_true')
    p_run.set_defaults(func=cmd_run)

    # cron (long-lived process, hourly loop)
    p_cron = sub.add_parser('cron', help='Long-lived hourly loop (alternative to cron)')
    p_cron.add_argument('--dry-run', action='store_true')
    p_cron.set_defaults(func=cmd_cron)

    args = parser.parse_args()
    setup_logging(getattr(args, 'verbose', False))

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == '__main__':
    main()
