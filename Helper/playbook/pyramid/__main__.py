"""CLI for pyramid position management.

Usage:
    python -m playbook.pyramid list
    python -m playbook.pyramid add MARUTI --price 11000 --qty 27 --sector Auto --thesis "ST touch"
    python -m playbook.pyramid fill 1 --level 2 --price 11880 --qty 25
    python -m playbook.pyramid check [--month YYYY-MM] [--force]
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
    if args.target_amount:
        data['target_amount'] = args.target_amount
    if args.account:
        data['account_id'] = args.account
    if args.decision_id:
        data['decision_id'] = args.decision_id

    pos = store.add_position(data)

    print(f"\nPyramid position #{pos['id']} created: {pos['symbol']}")
    print(f"  Sector: {pos['sector']}")
    print(f"  Target: Rs {pos['target_amount']:,.0f}")
    print(f"  L1: FILLED @ {pos['levels'][0]['price']:.2f} x{pos['levels'][0]['quantity']} "
          f"= Rs {pos['levels'][0]['amount']:,.0f}")

    for lvl in pos['levels'][1:]:
        trigger = (f"trigger={lvl['trigger_price']:.2f}"
                   if lvl['trigger_price'] else
                   f"+{lvl['trigger_pct']}% from prev fill")
        print(f"  L{lvl['level']}: PENDING ({trigger}, budget Rs {lvl['amount']:,.0f})")

    print(f"  Avg cost: {pos['avg_cost']:.2f}")
    sl_display = "-" if pos['current_sl'] is None else f"{pos['current_sl']:.2f}"
    print(f"  SL: {sl_display}")
    print(f"  Thesis: {pos['thesis']}")


def cmd_fill(args):
    store = get_pyramid_store()
    pos = store.fill_level(
        args.id, args.level, args.price, args.qty,
        date=args.date,
    )

    filled_lvl = pos['levels'][args.level - 1]
    print(f"\nPyramid #{pos['id']} {pos['symbol']} - Level {args.level} FILLED")
    print(f"  Price: {filled_lvl['price']:.2f} x{filled_lvl['quantity']} "
          f"= Rs {filled_lvl['amount']:,.0f}")
    print(f"  Total invested: Rs {pos['total_invested']:,.0f}")
    print(f"  Total quantity: {pos['total_quantity']}")
    print(f"  Avg cost: {pos['avg_cost']:.2f}")
    sl_display = "-" if pos['current_sl'] is None else f"{pos['current_sl']:.2f}"
    print(f"  SL: {sl_display}")
    print(f"  Status: {pos['status']}")

    # Show next pending level
    for lvl in pos['levels']:
        if lvl['status'] == 'pending':
            trigger = (f"trigger={lvl['trigger_price']:.2f}"
                       if lvl['trigger_price'] else
                       f"+{lvl['trigger_pct']}% from prev fill")
            print(f"  Next: L{lvl['level']} ({trigger}, budget Rs {lvl['amount']:,.0f})")
            break


def cmd_check(args):
    from .pyramid_checker import check_month_end

    store = get_pyramid_store()

    # Initialize Kite
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
    print(f"Pyramid #{pos['id']} {pos['symbol']}: SL set to {pos['current_sl']:.2f}")


def cmd_close(args):
    store = get_pyramid_store()
    pos = store.close_position(args.id, args.price, args.reason)
    ex = pos['exit']
    print(f"\nPyramid #{pos['id']} {pos['symbol']} CLOSED")
    print(f"  Exit: {ex['price']:.2f}")
    print(f"  Invested: Rs {pos['total_invested']:,.0f}")
    print(f"  Exit value: Rs {ex['total_exit_value']:,.0f}")
    print(f"  P&L: Rs {ex['realized_pnl']:,.0f} ({ex['pnl_pct']:.1f}%)")
    print(f"  Reason: {ex['reason']}")


def cmd_status(args):
    store = get_pyramid_store()
    active = store.get_active()
    pending = store.get_pending_adds()
    all_pos = store.load_positions()
    exited = [p for p in all_pos if p['status'] == 'exited']

    total_invested = sum(p['total_invested'] for p in active)
    max_per_position = store._config.get('max_per_position', 1000000)

    # Sector breakdown
    sectors = {}
    for p in active:
        s = p.get('sector', 'Unknown')
        sectors[s] = sectors.get(s, 0) + 1

    max_per_sector = store._config.get('max_per_sector', 3)
    max_positions = store._config.get('max_positions', 20)

    print(f"\n{'='*50}")
    print(f"  PYRAMID STATUS")
    print(f"{'='*50}")
    print(f"  Active positions: {len(active)} / {max_positions}")
    print(f"  Pending adds:     {len(pending)}")
    print(f"  Exited:           {len(exited)}")
    print(f"  Total invested:   Rs {total_invested:,.0f}")
    print(f"  Max per position: Rs {max_per_position:,.0f}")

    if sectors:
        print(f"\n  Sectors:")
        for s, count in sorted(sectors.items()):
            print(f"    {s}: {count} / {max_per_sector}")

    if pending:
        print(f"\n  Pending Level Fills:")
        for p in pending:
            for lvl in p['levels']:
                if lvl['status'] == 'pending':
                    trigger = (f">{lvl['trigger_price']:.2f}"
                               if lvl['trigger_price'] else
                               f"+{lvl['trigger_pct']}%")
                    print(f"    {p['symbol']} L{lvl['level']}: "
                          f"{trigger} (Rs {lvl['amount']:,.0f})")
                    break  # Show only next pending per position

    if exited:
        total_pnl = sum(p['exit']['realized_pnl'] for p in exited)
        print(f"\n  Exited P&L: Rs {total_pnl:,.0f}")

    print(f"{'='*50}\n")


def _init_kite():
    """Initialize Kite client from shared token file."""
    try:
        from kiteconnect import KiteConnect
        token_path = Path(__file__).resolve().parents[2].parent / 'data' / 'kite_access_token.json'
        if not token_path.exists():
            print(f"Token file not found: {token_path}")
            return None
        with open(token_path) as f:
            token_data = json.load(f)
        kite = KiteConnect(api_key=token_data['api_key'])
        kite.set_access_token(token_data['access_token'])
        return kite
    except Exception as e:
        print(f"Kite init error: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Pyramid position management (3-level scaling)",
        prog="python -m playbook.pyramid",
    )
    sub = parser.add_subparsers(dest='command')

    # list
    p_list = sub.add_parser('list', help='List all pyramid positions')
    p_list.add_argument('--status', choices=['active', 'completed', 'exited'],
                        help='Filter by status')

    # add
    p_add = sub.add_parser('add', help='Create new pyramid position (L1 filled)')
    p_add.add_argument('symbol', help='Stock symbol (e.g., MARUTI)')
    p_add.add_argument('--price', type=float, required=True,
                       help='L1 fill price')
    p_add.add_argument('--qty', type=int, required=True,
                       help='L1 quantity (shares)')
    p_add.add_argument('--sector', required=True,
                       help='Sector (e.g., Auto, IT, Banking)')
    p_add.add_argument('--thesis', required=True,
                       help='Investment thesis')
    p_add.add_argument('--target-amount', type=int, default=None,
                       help='Target full position (default: Rs 10L from config)')
    p_add.add_argument('--account', default=None,
                       help='Account ID (default: QSK814)')
    p_add.add_argument('--decision-id', type=int, default=None,
                       help='Link to portfolio_tracker decision ID')

    # fill
    p_fill = sub.add_parser('fill', help='Fill a pending pyramid level')
    p_fill.add_argument('id', type=int, help='Position ID')
    p_fill.add_argument('--level', type=int, required=True,
                        help='Level number (2 or 3)')
    p_fill.add_argument('--price', type=float, required=True,
                        help='Fill price')
    p_fill.add_argument('--qty', type=int, required=True,
                        help='Quantity (shares)')
    p_fill.add_argument('--date', default=None,
                        help='Fill date YYYY-MM-DD (default: today)')

    # check
    p_check = sub.add_parser('check', help='Run month-end checks')
    p_check.add_argument('--month', default=None,
                         help='Target month YYYY-MM (default: previous month)')
    p_check.add_argument('--force', action='store_true',
                         help='Override idempotency (re-check even if already done)')

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
        'fill': cmd_fill,
        'check': cmd_check,
        'breach': cmd_breach,
        'sl': cmd_sl,
        'close': cmd_close,
        'status': cmd_status,
    }
    cmd_map[args.command](args)


if __name__ == '__main__':
    main()
