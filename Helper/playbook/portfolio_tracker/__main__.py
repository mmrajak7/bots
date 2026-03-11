"""CLI entry point: python -m playbook.portfolio_tracker [command]"""

import argparse
import json
import sys
from pathlib import Path

# Add Helper to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from playbook.portfolio_tracker.decision_store import get_decision_store
from playbook.portfolio_tracker.journal import get_journal


def cmd_list(args):
    """List decisions."""
    store = get_decision_store()
    store.list_decisions(status_filter=args.status)


def cmd_add(args):
    """Add a decision interactively (mainly used by Claude slash commands)."""
    store = get_decision_store()

    data = {
        'symbol': args.symbol.upper(),
        'decision_date': args.date or __import__('datetime').datetime.now().strftime('%Y-%m-%d'),
        'verdict': args.verdict.upper(),
        'composite_score': args.score,
        'thesis': args.thesis,
    }

    if args.entry_price:
        data['entry_price'] = args.entry_price
    if args.target:
        data['target_price'] = args.target
    if args.sl:
        data['stop_loss'] = args.sl

    decision = store.add_decision(data)
    print("Decision #%d saved: %s %s (score: %.1f)" % (
        decision['id'], decision['verdict'], decision['symbol'],
        decision['composite_score']
    ))


def cmd_accept(args):
    """Accept a decision (mark as bought)."""
    store = get_decision_store()
    result = store.accept_decision(
        args.id,
        quantity=args.quantity,
        avg_buy_price=args.price,
    )
    if result:
        print("Decision #%d accepted: %s, qty=%s, price=Rs %.2f" % (
            args.id, result['symbol'], args.quantity, args.price or 0
        ))


def cmd_reject(args):
    """Reject a decision."""
    store = get_decision_store()
    result = store.reject_decision(args.id, reason=args.reason or '')
    if result:
        print("Decision #%d rejected: %s" % (args.id, result['symbol']))


def cmd_exit(args):
    """Exit a decision (mark as sold)."""
    store = get_decision_store()
    result = store.exit_decision(
        args.id,
        exit_price=args.price,
        exit_reason=args.reason or '',
    )
    if result:
        print("Decision #%d exited: %s at Rs %.2f" % (
            args.id, result['symbol'], args.price or 0
        ))
        if result.get('realized_pnl') is not None:
            print("Realized P&L: Rs %+,.0f" % result['realized_pnl'])


def cmd_scorecard(args):
    """Show decision-making scorecard."""
    store = get_decision_store()
    sc = store.get_scorecard()
    print("\n  DECISION SCORECARD")
    print("=" * 50)
    print(f"  Total Decisions:  {sc['total_decisions']}")
    print(f"  Accepted:         {sc['accepted']}")
    print(f"  Rejected:         {sc['rejected']}")
    print(f"  Exited:           {sc['exited']}")
    print(f"  Watching:         {sc['watching']}")
    print(f"  Win Rate:         {sc['win_rate']}%")
    print(f"  Realized P&L:     Rs {sc['total_realized_pnl']:+,.0f}")
    print(f"  Unrealized P&L:   Rs {sc['total_unrealized_pnl']:+,.0f}")
    print(f"  Avg P&L/Trade:    Rs {sc['avg_realized_pnl']:+,.0f}")
    print("=" * 50)


def cmd_journal(args):
    """Show journal entries."""
    journal = get_journal()
    if args.symbol:
        entries = journal.search(symbol=args.symbol)
        for e in entries:
            print(f"[{e.get('date')}] {e.get('type')}: {e.get('summary')}")
    else:
        journal.list_entries(limit=args.limit or 20)


def cmd_journal_add(args):
    """Add a journal entry."""
    journal = get_journal()
    symbols = [s.strip().upper() for s in args.symbols.split(',')] if args.symbols else []
    entry = journal.add_entry({
        'type': args.type or 'analysis',
        'symbols': symbols,
        'summary': args.summary,
        'details': args.details or '',
        'tags': [t.strip() for t in args.tags.split(',')] if args.tags else [],
    })
    print("Journal #%d saved: %s" % (entry['id'], entry['summary'][:50]))


def cmd_json(args):
    """Export all decisions as JSON."""
    store = get_decision_store()
    decisions = store.load_decisions()
    if args.status:
        decisions = [d for d in decisions if d.get('status') == args.status]
    print(json.dumps(decisions, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(
        description="Portfolio Tracker — Investment decision tracking",
        prog="portfolio_tracker"
    )
    sub = parser.add_subparsers(dest='command')

    # list
    p_list = sub.add_parser('list', help='List decisions')
    p_list.add_argument('--status', choices=['accepted', 'rejected', 'watching', 'exited'])

    # add
    p_add = sub.add_parser('add', help='Add decision')
    p_add.add_argument('symbol')
    p_add.add_argument('verdict', choices=['BUY', 'WAIT', 'EXIT', 'SELL'])
    p_add.add_argument('--score', type=float, required=True)
    p_add.add_argument('--thesis', required=True)
    p_add.add_argument('--entry-price', type=float)
    p_add.add_argument('--target', type=float)
    p_add.add_argument('--sl', type=float)
    p_add.add_argument('--date')

    # accept
    p_accept = sub.add_parser('accept', help='Accept decision')
    p_accept.add_argument('id', type=int)
    p_accept.add_argument('--quantity', type=int)
    p_accept.add_argument('--price', type=float)

    # reject
    p_reject = sub.add_parser('reject', help='Reject decision')
    p_reject.add_argument('id', type=int)
    p_reject.add_argument('--reason')

    # exit
    p_exit = sub.add_parser('exit', help='Exit decision')
    p_exit.add_argument('id', type=int)
    p_exit.add_argument('--price', type=float)
    p_exit.add_argument('--reason')

    # scorecard
    sub.add_parser('scorecard', help='Decision scorecard')

    # journal
    p_journal = sub.add_parser('journal', help='Show journal')
    p_journal.add_argument('--symbol')
    p_journal.add_argument('--limit', type=int, default=20)

    # journal-add
    p_jadd = sub.add_parser('journal-add', help='Add journal entry')
    p_jadd.add_argument('--summary', required=True)
    p_jadd.add_argument('--symbols')
    p_jadd.add_argument('--type', default='analysis')
    p_jadd.add_argument('--details')
    p_jadd.add_argument('--tags')

    # json
    p_json = sub.add_parser('json', help='Export as JSON')
    p_json.add_argument('--status')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmd_map = {
        'list': cmd_list,
        'add': cmd_add,
        'accept': cmd_accept,
        'reject': cmd_reject,
        'exit': cmd_exit,
        'scorecard': cmd_scorecard,
        'journal': cmd_journal,
        'journal-add': cmd_journal_add,
        'json': cmd_json,
    }

    cmd_map[args.command](args)


if __name__ == '__main__':
    main()
