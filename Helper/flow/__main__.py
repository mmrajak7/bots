"""CLI entry point: python -m flow [command]"""

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
    """One-shot: Chartink + 15M ST + proximity check."""
    from .monitor import run_scan_once
    run_scan_once(dry_run=args.dry_run)


def cmd_run(args):
    """Full run loop: watchlist + scanner + monitor."""
    from .monitor import run
    run(dry_run=args.dry_run)


def cmd_list(args):
    """List all trades."""
    store = get_store()
    store.list_trades(status_filter=args.status)


def cmd_status(args):
    """Dashboard: watchlist + open trades summary."""
    store = get_store()
    trades = store.load_trades()

    alerted = [t for t in trades if t['status'] == 'alerted']
    entered = [t for t in trades if t['status'] == 'entered']
    exited = [t for t in trades if t['status'] == 'exited']
    cancelled = [t for t in trades if t['status'] == 'cancelled']

    print("\n=== FLOW DASHBOARD ===\n")
    print(f"  Alerted:    {len(alerted)}")
    print(f"  Entered:    {len(entered)}")
    print(f"  Exited:     {len(exited)}")
    print(f"  Cancelled:  {len(cancelled)}")

    if entered:
        total_pnl = 0
        print(f"\n  Open Trades:")
        for t in entered:
            print(f"    #{t['id']} {t['stock']} ({t['side']}) "
                  f"entry=Rs {t.get('entry_spot', 0):,.2f} "
                  f"ST={t.get('current_st', 0):,.2f}")

    if exited:
        total_pnl = sum(t.get('pnl', 0) or 0 for t in exited)
        winners = sum(1 for t in exited if (t.get('pnl', 0) or 0) > 0)
        losers = sum(1 for t in exited if (t.get('pnl', 0) or 0) <= 0)
        print(f"\n  Closed P&L: Rs {total_pnl:,.0f} "
              f"({winners}W / {losers}L)")
    print()


def cmd_enter(args):
    """Mark an alerted trade as entered."""
    store = get_store()
    try:
        trade = store.enter_trade(args.trade_id, {
            'entry_spot': args.spot,
            'entry_premium': args.premium,
            'quantity': args.qty,
            'option_symbol': args.option,
        })
        print(f"Trade #{trade['id']} entered: {trade['stock']} "
              f"({trade['direction']}) @ Rs {args.spot}")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_close(args):
    """Manually close an entered trade."""
    store = get_store()
    try:
        trade = store.exit_trade(
            args.trade_id,
            exit_spot=args.spot,
            exit_premium=args.premium or 0,
            reason=args.reason or 'manual_close',
        )
        pnl = trade.get('pnl', 0) or 0
        print(f"Trade #{trade['id']} closed: {trade['stock']} "
              f"P&L=Rs {pnl:,.0f}")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_watchlist(args):
    """Show current Chartink watchlist (one-shot scan)."""
    from .watchlist import scan_both
    wl = scan_both()
    print(f"\n=== FLOW WATCHLIST (4-TF ST aligned) ===\n")
    print(f"UP ({len(wl['UP'])} stocks):")
    for s in sorted(wl['UP']):
        print(f"  {s}")
    print(f"\nDOWN ({len(wl['DOWN'])} stocks):")
    for s in sorted(wl['DOWN']):
        print(f"  {s}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description='Flow: Multi-TF ST alignment + 15M pullback strategy'
    )
    parser.add_argument('-v', '--verbose', action='store_true')
    sub = parser.add_subparsers(dest='command')

    # scan
    p_scan = sub.add_parser('scan', help='One-shot scan + proximity check')
    p_scan.add_argument('--dry-run', action='store_true',
                        help='No Telegram alerts')
    p_scan.set_defaults(func=cmd_scan)

    # run
    p_run = sub.add_parser('run', help='Full monitoring loop')
    p_run.add_argument('--dry-run', action='store_true')
    p_run.set_defaults(func=cmd_run)

    # list
    p_list = sub.add_parser('list', help='List trades')
    p_list.add_argument('--status', choices=['alerted', 'entered', 'exited',
                                              'cancelled'])
    p_list.set_defaults(func=cmd_list)

    # status
    p_status = sub.add_parser('status', help='Dashboard')
    p_status.set_defaults(func=cmd_status)

    # enter
    p_enter = sub.add_parser('enter', help='Mark alert as entered')
    p_enter.add_argument('trade_id', type=int)
    p_enter.add_argument('--spot', type=float, required=True,
                         help='Entry spot price')
    p_enter.add_argument('--premium', type=float, required=True,
                         help='Option premium paid')
    p_enter.add_argument('--qty', type=int, required=True,
                         help='Quantity (shares)')
    p_enter.add_argument('--option', type=str,
                         help='Override option symbol')
    p_enter.set_defaults(func=cmd_enter)

    # close
    p_close = sub.add_parser('close', help='Close a trade')
    p_close.add_argument('trade_id', type=int)
    p_close.add_argument('--spot', type=float, required=True,
                         help='Exit spot price')
    p_close.add_argument('--premium', type=float,
                         help='Option exit premium')
    p_close.add_argument('--reason', type=str, default='manual_close')
    p_close.set_defaults(func=cmd_close)

    # watchlist
    p_wl = sub.add_parser('watchlist', help='Show current Chartink watchlist')
    p_wl.set_defaults(func=cmd_watchlist)

    args = parser.parse_args()
    setup_logging(args.verbose)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == '__main__':
    main()
