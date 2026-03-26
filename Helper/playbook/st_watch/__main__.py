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


def _run_breadth_scan(dry_run: bool = False):
    """Scan Chartink for live breadth, record, and check for alerts."""
    try:
        from .breadth_tracker import scan_and_record
        reading = scan_and_record(dry_run=dry_run)
        if reading:
            logging.info("Breadth: %.1f%% (%d/%d)",
                         reading['breadth_pct'], reading['above'], reading['total'])
        else:
            logging.warning("Breadth scan returned no data")
    except Exception as e:
        logging.error("Breadth scan failed: %s", e, exc_info=True)


def _send_morning_briefing(dry_run: bool = False):
    """Send morning briefing if not already sent today."""
    from .regime_monitor import load_state
    state = load_state()
    today = datetime.now(IST).strftime('%Y-%m-%d')

    if state.get('last_briefing_date') == today:
        logging.debug("Morning briefing already sent today")
        return

    logging.info("Sending morning briefing...")
    try:
        from .breadth_tracker import build_morning_briefing
        msg = build_morning_briefing(dry_run=dry_run)
        if msg:
            # Mark as sent in regime state
            from .regime_monitor import save_state
            state['last_briefing_date'] = today
            save_state(state)
    except Exception as e:
        logging.error("Morning briefing failed: %s", e, exc_info=True)


def _run_regime_check(kite=None, dry_run: bool = False):
    """Run regime check — called every scan cycle.

    Recomputes signals using live NIFTY LTP (fast, cached NIFTY history + breadth).
    Alerts at most once per day per transition type.
    """
    from .regime_monitor import check_regime
    if kite is None:
        from .watcher import _get_kite
        try:
            kite = _get_kite()
        except Exception as e:
            logging.error("Kite init failed for regime: %s", e)
            kite = None
    try:
        check_regime(kite, dry_run=dry_run)
    except Exception as e:
        logging.error("Regime check failed: %s", e, exc_info=True)


def cmd_run(args):
    """Single scan with market hours check — designed for hourly cron.

    Timeline:
      9:00-9:14  → Morning briefing (once/day) + breadth scan
      9:15-15:30 → ST scan + breadth scan + regime check
      15:30+     → Skip
    """
    now = datetime.now(IST)

    if now.weekday() >= 5:
        logging.info("Weekend — skipping")
        return

    pre_market = now.replace(hour=9, minute=0, second=0, microsecond=0)
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)

    # Pre-market window (9:00-9:14): morning briefing + first breadth scan
    if pre_market <= now < market_open:
        logging.info("Pre-market — sending morning briefing")
        _send_morning_briefing(dry_run=args.dry_run)
        _run_breadth_scan(dry_run=args.dry_run)
        return

    if now < pre_market or now > market_close:
        logging.info("Outside market hours (%s) — skipping", now.strftime('%H:%M'))
        return

    # Market hours: full scan cycle
    # Morning briefing if not sent yet (e.g., 9:00 cron was missed)
    _send_morning_briefing(dry_run=args.dry_run)

    logging.info("Market open, running ST scan + breadth + regime...")
    try:
        from .watcher import scan
        results = scan(dry_run=args.dry_run)
        close_count = sum(1 for r in results if abs(r['gap_pct']) <= 5)
        logging.info("Scan complete: %d checks, %d within 5%% of support",
                     len(results), close_count)
    except Exception as e:
        logging.error("ST scan failed: %s", e, exc_info=True)

    # Breadth scan (Chartink) + regime check — every scan cycle
    _run_breadth_scan(dry_run=args.dry_run)
    _run_regime_check(dry_run=args.dry_run)


def cmd_cron(args):
    """Long-lived process: scans hourly within market hours.

    Alternative to cron — runs continuously, sleeping between scans.
    """
    logging.info("Support level monitor started (hourly scans)")

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
            # Send morning briefing at 9:00+ if not sent yet
            pre_market = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if now >= pre_market:
                _send_morning_briefing(dry_run=args.dry_run)
                _run_breadth_scan(dry_run=args.dry_run)
            wait = (market_open - now).total_seconds()
            logging.info("Waiting %.0f min for market open...", wait / 60)
            time.sleep(max(wait, 60))
            continue

        if now > market_close:
            logging.info("Market closed — final regime check before exit")
            _run_regime_check(dry_run=args.dry_run)
            logging.info("Exiting cron mode")
            break

        # Run ST scan + regime check
        try:
            from .watcher import scan
            results = scan(dry_run=args.dry_run)
            close_count = sum(1 for r in results if abs(r['gap_pct']) <= 5)
            logging.info("Scan done: %d checks, %d within 5%%", len(results), close_count)
        except Exception as e:
            logging.error("ST scan failed: %s", e, exc_info=True)

        # Breadth scan + regime check
        try:
            _run_breadth_scan(dry_run=args.dry_run)
        except Exception as e:
            logging.error("Breadth scan failed: %s", e, exc_info=True)

        try:
            _run_regime_check(dry_run=args.dry_run)
        except Exception as e:
            logging.error("Regime check failed: %s", e, exc_info=True)

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


def cmd_briefing(args):
    """Send morning briefing (manual trigger)."""
    from .breadth_tracker import build_morning_briefing
    msg = build_morning_briefing(dry_run=args.dry_run)
    if msg:
        # Print for visibility
        print(msg.replace('<b>', '').replace('</b>', '')
              .replace('<i>', '').replace('</i>', ''))
    else:
        print("No breadth data available for briefing.")


def cmd_breadth(args):
    """Breadth tracker — live F&O stocks above 50DMA via Chartink."""
    from .breadth_tracker import scan_and_record, print_breadth_status

    if args.scan:
        reading = scan_and_record()
        if reading:
            print(f"Breadth: {reading['breadth_pct']}% "
                  f"({reading['above']}/{reading['total']}) at {reading['time']}")
        else:
            print("Chartink scan failed")
            return

    print_breadth_status()


def cmd_regime(args):
    """Check or display regime status."""
    from .regime_monitor import check_regime, print_regime_status, load_state

    if args.check:
        # Compute signals + update state + alert on transitions
        from .watcher import _get_kite
        try:
            kite = _get_kite()
        except Exception as e:
            logging.error("Kite init failed: %s (using cache fallback)", e)
            kite = None
        state = check_regime(kite, dry_run=args.dry_run)
        print_regime_status(state)
    else:
        # Just display last known state
        print_regime_status()


def cmd_unmapped(args):
    """Show F&O stocks missing from sector mapping."""
    from .breadth_tracker import get_latest

    latest = get_latest()
    if not latest:
        print("  No breadth data. Run: python -m playbook.st_watch breadth --scan")
        return

    unmapped = latest.get('unmapped', [])
    scan_date = latest.get('date', '?')
    scan_time = latest.get('time', '?')

    if not unmapped:
        total = latest.get('total', '?')
        print(f"\n  All F&O stocks mapped. (Last scan: {scan_date} {scan_time}, "
              f"{total} stocks)")
        return

    print(f"\n  Unmapped F&O stocks: {len(unmapped)}  "
          f"(scan: {scan_date} {scan_time})")
    print(f"  {'─' * 40}")
    for sym in unmapped:
        print(f"  {sym}")
    print(f"\n  Add to: playbook/sector_mapping.py")
    print(f"  Then re-run breadth scan to verify.\n")


def cmd_history(args):
    """Show alert history from persistent log."""
    from .alert_store import get_alert_store
    store = get_alert_store()
    store.list_alerts(days=args.days, symbol=args.symbol)


def main():
    parser = argparse.ArgumentParser(
        prog='python -m playbook.st_watch',
        description='Support Level Monitor — Core & Tactical basket monitor'
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

    # briefing (morning briefing — manual trigger)
    p_brief = sub.add_parser('briefing', help='Send morning briefing')
    p_brief.add_argument('--dry-run', action='store_true',
                         help='Show without sending Telegram')
    p_brief.set_defaults(func=cmd_briefing)

    # breadth (live F&O breadth tracking)
    p_breadth = sub.add_parser('breadth', help='Breadth tracker (F&O stocks above 50DMA)')
    p_breadth.add_argument('--scan', action='store_true',
                           help='Fetch live breadth from Chartink and record')
    p_breadth.set_defaults(func=cmd_breadth)

    # regime (check or display regime PAUSE/UNPAUSE status)
    p_regime = sub.add_parser('regime', help='Regime PAUSE/UNPAUSE status')
    p_regime.add_argument('--check', action='store_true',
                          help='Compute signals and check for transitions (daily)')
    p_regime.add_argument('--dry-run', action='store_true',
                          help='Show alerts without sending Telegram')
    p_regime.set_defaults(func=cmd_regime)

    # unmapped (F&O stocks not in sector mapping)
    p_unmap = sub.add_parser('unmapped', help='Show F&O stocks missing from sector mapping')
    p_unmap.set_defaults(func=cmd_unmapped)

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
