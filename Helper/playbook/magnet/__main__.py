"""CLI entry point: python -m playbook.magnet [command]"""

import argparse
import logging
import sys

from . import get_store


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )


def cmd_scan(args):
    """One-shot Chartink scan — validates signals, adds to watchlist."""
    from .monitor import run_scan_once
    run_scan_once(dry_run=args.dry_run)


def cmd_run(args):
    """Full run loop: scan every 5 min + monitor every 30s."""
    from .monitor import run
    run(dry_run=args.dry_run)


def cmd_list(args):
    """List all trades."""
    store = get_store()
    store.list_trades(status_filter=args.status)


def cmd_status(args):
    """Dashboard: open positions + P&L summary."""
    store = get_store()
    trades = store.load_trades()

    watching = [t for t in trades if t['status'] == 'watching']
    entered = [t for t in trades if t['status'] == 'entered']
    exited = [t for t in trades if t['status'] == 'exited']
    cancelled = [t for t in trades if t['status'] == 'cancelled']

    print("\n=== MAGNET DASHBOARD ===\n")
    print(f"  Watching:   {len(watching)}")
    print(f"  Entered:    {len(entered)}")
    print(f"  Exited:     {len(exited)}")
    print(f"  Cancelled:  {len(cancelled)}")

    if exited:
        total_pnl = sum(t.get('pnl', 0) or 0 for t in exited)
        wins = sum(1 for t in exited if (t.get('pnl', 0) or 0) > 0)
        losses = sum(1 for t in exited if (t.get('pnl', 0) or 0) <= 0)
        win_rate = (wins / len(exited) * 100) if exited else 0
        avg_days = sum(t.get('days_held', 0) for t in exited) / len(exited)

        print(f"\n  --- Performance ---")
        print(f"  Total P&L:  Rs {total_pnl:,.0f}")
        print(f"  Win rate:   {win_rate:.0f}% ({wins}W / {losses}L)")
        print(f"  Avg hold:   {avg_days:.1f} days")

        # By exit reason
        reasons = {}
        for t in exited:
            r = t.get('exit_reason', '?')
            if r not in reasons:
                reasons[r] = {'count': 0, 'pnl': 0}
            reasons[r]['count'] += 1
            reasons[r]['pnl'] += t.get('pnl', 0) or 0

        print(f"\n  --- By Exit Reason ---")
        for reason, stats in reasons.items():
            print(f"  {reason:<12} {stats['count']:>3} trades  Rs {stats['pnl']:>10,.0f}")

    if watching:
        print(f"\n  --- Watching ---")
        for t in watching:
            print(f"  #{t['id']} {t['stock']:<12} {t['timeframe']:<8} "
                  f"gap={t['signal_gap_pct']:.1f}% -> {t['direction']}")

    if entered:
        print(f"\n  --- Open Trades ---")
        for t in entered:
            print(f"  #{t['id']} {t['stock']:<12} {t['direction']:<4} "
                  f"entry={t.get('entry_spot', 0):.2f} "
                  f"target={t['target_spot']:.2f} "
                  f"sl={t.get('sl_spot', 0):.2f} "
                  f"option={t.get('option_symbol', '?')}")

    print()


def cmd_close(args):
    """Manually close a trade."""
    store = get_store()
    trade_id = args.id

    trade = None
    for t in store.load_trades():
        if t['id'] == trade_id:
            trade = t
            break

    if not trade:
        print(f"Trade #{trade_id} not found")
        return

    if trade['status'] == 'watching':
        store.cancel_signal(trade_id, args.reason or 'manual cancel')
        print(f"Signal #{trade_id} cancelled")
    elif trade['status'] == 'entered':
        # Get current spot for paper exit
        try:
            from .scanner import _get_kite, get_ltp
            kite = _get_kite()
            ltps = get_ltp(kite, [trade['stock']])
            spot = ltps.get(trade['stock'], 0)

            # Get current option premium (BID = what we'd sell for)
            premium = 0
            if trade.get('option_symbol'):
                from .monitor import get_option_bid
                premium = get_option_bid(kite, trade['option_symbol'])
        except Exception:
            spot = trade.get('entry_spot', 0)
            premium = 0

        store.exit_trade(trade_id, spot, premium,
                         args.reason or 'manual close')
        # Re-read to get computed P&L
        for t in store.load_trades():
            if t['id'] == trade_id:
                trade = t
                break
        print(f"Trade #{trade_id} closed: P&L Rs {trade.get('pnl', 0):,.0f}")
    else:
        print(f"Trade #{trade_id} is already {trade['status']}")


def main():
    parser = argparse.ArgumentParser(
        prog='python -m playbook.magnet',
        description='Magnet — ST-magnet naked option strategy'
    )
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Debug logging')
    sub = parser.add_subparsers(dest='command')

    # scan
    p_scan = sub.add_parser('scan', help='One-shot Chartink scan')
    p_scan.add_argument('--dry-run', action='store_true',
                        help='Show signals without adding to store')
    p_scan.set_defaults(func=cmd_scan)

    # run
    p_run = sub.add_parser('run', help='Full monitor loop')
    p_run.add_argument('--dry-run', action='store_true',
                       help='Scan only, no entries')
    p_run.set_defaults(func=cmd_run)

    # list
    p_list = sub.add_parser('list', help='List trades')
    p_list.add_argument('--status', choices=['watching', 'entered', 'exited', 'cancelled'],
                        help='Filter by status')
    p_list.set_defaults(func=cmd_list)

    # status
    p_status = sub.add_parser('status', help='Dashboard')
    p_status.set_defaults(func=cmd_status)

    # close
    p_close = sub.add_parser('close', help='Manual close/cancel')
    p_close.add_argument('id', type=int, help='Trade ID')
    p_close.add_argument('--reason', default=None, help='Exit reason')
    p_close.set_defaults(func=cmd_close)

    args = parser.parse_args()
    setup_logging(args.verbose)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == '__main__':
    main()
