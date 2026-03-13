"""CLI entry point: python -m playbook.portfolio_tracker [command]"""

import argparse
import json
import sys
from pathlib import Path

# Add Helper to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from playbook.portfolio_tracker.decision_store import get_decision_store
from playbook.portfolio_tracker.journal import get_journal


# Account shortcuts for convenience
ACCOUNT_ALIASES = {
    'investment': 'QSK814',
    'inv': 'QSK814',
    'holdings': 'QSK814',
    'options': 'YL6478',
    'opt': 'YL6478',
    'trading': 'YL6478',
}


def _resolve_account(account_str):
    """Resolve account alias or ID."""
    if not account_str:
        return None
    return ACCOUNT_ALIASES.get(account_str.lower(), account_str.upper())


def cmd_list(args):
    """List decisions."""
    store = get_decision_store()
    account = _resolve_account(args.account)
    store.list_decisions(status_filter=args.status, account_id=account)


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

    account = _resolve_account(args.account)
    if account:
        data['account_id'] = account
        # Resolve account name from config
        cfg_path = Path(__file__).resolve().parent.parent.parent / 'config' / 'portfolio_config.json'
        if cfg_path.exists():
            with open(cfg_path) as f:
                cfg = json.load(f)
            acct_cfg = cfg.get('accounts', {}).get(account, {})
            data['account_name'] = acct_cfg.get('name', account)

    if args.entry_price:
        data['entry_price'] = args.entry_price
    if args.target:
        data['target_price'] = args.target
    if args.sl:
        data['stop_loss'] = args.sl

    decision = store.add_decision(data)
    acct_label = f" [{data.get('account_name', account)}]" if account else ""
    print("Decision #%d saved: %s %s (score: %.1f)%s" % (
        decision['id'], decision['verdict'], decision['symbol'],
        decision['composite_score'], acct_label
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
            print(f"Realized P&L: Rs {result['realized_pnl']:+,.0f}")


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
        account = _resolve_account(args.account)
        entries = journal.search(symbol=args.symbol, account_id=account)
        for e in entries:
            print(f"[{e.get('date')}] {e.get('type')}: {e.get('summary')}")
    else:
        journal.list_entries(limit=args.limit or 20)


def cmd_journal_add(args):
    """Add a journal entry."""
    journal = get_journal()
    symbols = [s.strip().upper() for s in args.symbols.split(',')] if args.symbols else []
    data = {
        'type': args.type or 'analysis',
        'symbols': symbols,
        'summary': args.summary,
        'details': args.details or '',
        'tags': [t.strip() for t in args.tags.split(',')] if args.tags else [],
    }
    account = _resolve_account(args.account)
    if account:
        data['account_id'] = account
    entry = journal.add_entry(data)
    print("Journal #%d saved: %s" % (entry['id'], entry['summary'][:50]))


def cmd_json(args):
    """Export all decisions as JSON."""
    store = get_decision_store()
    decisions = store.load_decisions()
    if args.status:
        decisions = [d for d in decisions if d.get('status') == args.status]
    account = _resolve_account(args.account)
    if account:
        decisions = [d for d in decisions if d.get('account_id') == account]
    print(json.dumps(decisions, indent=2, default=str))


def cmd_accounts(args):
    """Show configured accounts."""
    cfg_path = Path(__file__).resolve().parent.parent.parent / 'config' / 'portfolio_config.json'
    if not cfg_path.exists():
        print("No portfolio config found.")
        return
    with open(cfg_path) as f:
        cfg = json.load(f)
    accounts = cfg.get('accounts', {})
    defaults = cfg.get('default_account', {})

    print("\n  CONFIGURED ACCOUNTS")
    print("=" * 60)
    for acct_id, info in accounts.items():
        is_default_holdings = '(default for holdings)' if defaults.get('holdings') == acct_id else ''
        is_default_options = '(default for options)' if defaults.get('options') == acct_id else ''
        default_tag = is_default_holdings or is_default_options
        print(f"  {acct_id:<10} {info.get('name', 'N/A'):<25} {default_tag}")
        print(f"             Purpose: {info.get('purpose', 'N/A')}")
        print(f"             Strategies: {', '.join(info.get('strategies', []))}")
        print()
    print("  Aliases: investment/inv/holdings -> %s" % defaults.get('holdings', '?'))
    print("           options/opt/trading -> %s" % defaults.get('options', '?'))
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Portfolio Tracker — Investment decision tracking",
        prog="portfolio_tracker"
    )
    sub = parser.add_subparsers(dest='command')

    # list
    p_list = sub.add_parser('list', help='List decisions')
    p_list.add_argument('--status', choices=['accepted', 'rejected', 'watching', 'exited'])
    p_list.add_argument('--account', help='Account ID or alias (investment, options)')

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
    p_add.add_argument('--account', help='Account ID or alias (investment, options)')

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

    # accounts
    sub.add_parser('accounts', help='Show configured accounts')

    # journal
    p_journal = sub.add_parser('journal', help='Show journal')
    p_journal.add_argument('--symbol')
    p_journal.add_argument('--limit', type=int, default=20)
    p_journal.add_argument('--account', help='Account ID or alias')

    # journal-add
    p_jadd = sub.add_parser('journal-add', help='Add journal entry')
    p_jadd.add_argument('--summary', required=True)
    p_jadd.add_argument('--symbols')
    p_jadd.add_argument('--type', default='analysis')
    p_jadd.add_argument('--details')
    p_jadd.add_argument('--tags')
    p_jadd.add_argument('--account', help='Account ID or alias')

    # json
    p_json = sub.add_parser('json', help='Export as JSON')
    p_json.add_argument('--status')
    p_json.add_argument('--account', help='Account ID or alias')

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
        'accounts': cmd_accounts,
        'journal': cmd_journal,
        'journal-add': cmd_journal_add,
        'json': cmd_json,
    }

    cmd_map[args.command](args)


if __name__ == '__main__':
    main()
