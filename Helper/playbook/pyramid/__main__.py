"""CLI for position management.

Usage:
    python -m playbook.pyramid list
    python -m playbook.pyramid add MARUTI --price 11000 --qty 45 --sector Auto --thesis "ST touch"
    python -m playbook.pyramid check [--month YYYY-MM] [--force]
    python -m playbook.pyramid breach
    python -m playbook.pyramid sl 1 --price 10500
    python -m playbook.pyramid close 1 --price 12000 --reason "SL hit"
    python -m playbook.pyramid status
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .pyramid_store import get_pyramid_store


def cmd_list(args):
    store = get_pyramid_store()
    store.list_positions(status_filter=args.status)


def cmd_add(args):
    store = get_pyramid_store()

    data = {
        'symbol': args.symbol.upper(),
        'spot_symbol': f"NSE:{args.symbol.upper()}",
        'sector': args.sector,
        'thesis': args.thesis,
        'price': args.price,
        'quantity': args.qty,
    }
    if args.sl:
        data['touch_month_low'] = args.sl
    if args.account:
        data['account_id'] = args.account
    if args.decision_id:
        data['decision_id'] = args.decision_id
    if args.entry_date:
        data['entry_date'] = args.entry_date

    pos = store.add_position(data)

    print(f"\nPosition #{pos['id']} created: {pos['symbol']}")
    print(f"  Sector:  {pos['sector']}")
    print(f"  Entry:   {pos['entry_price']:.2f} x {pos['entry_quantity']} "
          f"= Rs {pos['entry_amount']:,.0f}")
    sl_display = f"{pos['current_sl']:.2f}" if pos['current_sl'] else "-"
    print(f"  SL:      {sl_display}")
    print(f"  Thesis:  {pos['thesis']}")
    if not args.sl:
        print(f"  WARNING: No initial SL set. Position unprotected until "
              f"first month-end check (`python -m playbook.pyramid check`).")


def cmd_check(args):
    from .pyramid_checker import check_month_end

    store = get_pyramid_store()

    kite = _init_kite()
    if not kite:
        print("ERROR: Could not initialize Kite. Check token.")
        sys.exit(1)

    check_month_end(
        kite=kite,
        store=store,
        month=args.month,
        force=args.force,
    )


def cmd_breach(args):
    """Daily SL breach check — compare today's intraday low vs SL."""
    from .pyramid_checker import check_sl_breaches

    store = get_pyramid_store()

    kite = _init_kite()
    if not kite:
        print("ERROR: Could not initialize Kite. Check token.")
        sys.exit(1)

    check_sl_breaches(kite=kite, store=store)


def cmd_sl(args):
    store = get_pyramid_store()
    pos = store.update_sl(args.id, args.price)
    print(f"Position #{pos['id']} {pos['symbol']}: SL set to {pos['current_sl']:.2f}")


def cmd_close(args):
    store = get_pyramid_store()
    pos = store.close_position(args.id, args.price, args.reason)
    ex = pos['exit']
    invested = pos.get('entry_amount') or pos.get('total_invested', 0)
    print(f"\nPosition #{pos['id']} {pos['symbol']} CLOSED")
    print(f"  Exit:     {ex['price']:.2f}")
    print(f"  Invested: Rs {invested:,.0f}")
    print(f"  Exit val: Rs {ex['total_exit_value']:,.0f}")
    print(f"  P&L:      Rs {ex['realized_pnl']:,.0f} ({ex['pnl_pct']:.1f}%)")
    print(f"  Reason:   {ex['reason']}")


def cmd_status(args):
    store = get_pyramid_store()
    active = store.get_active()
    all_pos = store.load_positions()
    exited = [p for p in all_pos if p['status'] == 'exited']

    total_invested = sum(p.get('entry_amount') or p.get('total_invested', 0) for p in active)
    position_size = store._config.get('position_size', 500000)

    # Sector breakdown
    sectors = {}
    for p in active:
        s = p.get('sector', 'Unknown')
        sectors[s] = sectors.get(s, 0) + 1

    max_per_sector = store._config.get('max_per_sector', 3)
    max_positions = store._config.get('max_positions', 25)

    print(f"\n{'='*50}")
    print(f"  POSITION STATUS")
    print(f"{'='*50}")
    print(f"  Active positions: {len(active)} / {max_positions}")
    print(f"  Position size:    Rs {position_size:,.0f}")
    print(f"  Total invested:   Rs {total_invested:,.0f}")
    print(f"  Max deployable:   Rs {max_positions * position_size:,.0f}")
    print(f"  Exited:           {len(exited)}")

    if sectors:
        print(f"\n  Sectors:")
        for s, count in sorted(sectors.items()):
            print(f"    {s}: {count} / {max_per_sector}")

    if exited:
        total_pnl = sum(p['exit']['realized_pnl'] for p in exited)
        print(f"\n  Exited P&L: Rs {total_pnl:,.0f}")

    print(f"{'='*50}\n")


def _init_kite():
    """Initialize Kite client from QSK814 (investment account) token.

    Pyramid positions are in QSK814, whose token lives in BOTS/FIFTY/data/.
    Falls back to BOTS/data/ (YL6478) if FIFTY token not found.
    """
    try:
        from kiteconnect import KiteConnect
        bots_root = Path(__file__).resolve().parents[2].parent  # BOTS/

        # QSK814 token (investment account) — primary
        token_path = bots_root / 'FIFTY' / 'data' / 'kite_access_token.json'
        if not token_path.exists():
            # Fallback to shared token (YL6478)
            token_path = bots_root / 'data' / 'kite_access_token.json'

        if not token_path.exists():
            print(f"Token file not found: {token_path}")
            return None
        with open(token_path) as f:
            token_data = json.load(f)
        kite = KiteConnect(api_key=token_data['api_key'])
        kite.set_access_token(token_data['access_token'])
        print(f"Kite initialized: {token_data.get('user_id', '?')} "
              f"(from {token_path.parent.parent.name}/{token_path.parent.name})")
        return kite
    except Exception as e:
        print(f"Kite init error: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Position management (flat 5L per trade)",
        prog="python -m playbook.pyramid",
    )
    sub = parser.add_subparsers(dest='command')

    # list
    p_list = sub.add_parser('list', help='List all positions')
    p_list.add_argument('--status', choices=['active', 'exited'],
                        help='Filter by status')

    # add
    p_add = sub.add_parser('add', help='Create new position')
    p_add.add_argument('symbol', help='Stock symbol (e.g., MARUTI)')
    p_add.add_argument('--price', type=float, required=True,
                       help='Entry price')
    p_add.add_argument('--qty', type=int, required=True,
                       help='Quantity (shares)')
    p_add.add_argument('--sector', required=True,
                       help='Sector (e.g., Auto, IT, Banking)')
    p_add.add_argument('--thesis', required=True,
                       help='Investment thesis')
    p_add.add_argument('--sl', type=float, default=None,
                       help='Initial SL (touch month low)')
    p_add.add_argument('--account', default=None,
                       help='Account ID (default: QSK814)')
    p_add.add_argument('--decision-id', type=int, default=None,
                       help='Link to portfolio_tracker decision ID')
    p_add.add_argument('--entry-date', default=None,
                       help='Entry date YYYY-MM-DD (default: today)')

    # check
    p_check = sub.add_parser('check', help='Run month-end SL checks')
    p_check.add_argument('--month', default=None,
                         help='Target month YYYY-MM (default: previous month)')
    p_check.add_argument('--force', action='store_true',
                         help='Override idempotency')

    # sl
    p_sl = sub.add_parser('sl', help='Manual SL update')
    p_sl.add_argument('id', type=int, help='Position ID')
    p_sl.add_argument('--price', type=float, required=True,
                      help='New SL price')

    # close
    p_close = sub.add_parser('close', help='Close/exit a position')
    p_close.add_argument('id', type=int, help='Position ID')
    p_close.add_argument('--price', type=float, required=True,
                         help='Exit price')
    p_close.add_argument('--reason', required=True,
                         help='Exit reason (e.g., "SL hit", "Manual exit")')

    # breach
    sub.add_parser('breach', help='Daily SL breach check (uses intraday low)')

    # status
    sub.add_parser('status', help='Summary: active count, sectors, deployed capital')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmd_map = {
        'list': cmd_list,
        'add': cmd_add,
        'check': cmd_check,
        'breach': cmd_breach,
        'sl': cmd_sl,
        'close': cmd_close,
        'status': cmd_status,
    }
    cmd_map[args.command](args)


if __name__ == '__main__':
    main()
