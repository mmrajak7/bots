"""Read back a session's order intents. `python -m bcs.journal_report`.

This is the reading end of "alert-only first": run the exit path with
`--dry-run` against the live book for a session, then ask this what it would
have done and whether that matches what the stores actually booked.

    python -m bcs.journal_report                # today
    python -m bcs.journal_report --day 20260915
    python -m bcs.journal_report --unresolved   # only the dangling intents

It computes nothing about the market. Every number printed is read straight off
the journal line or off the trade store — a report that re-derives a price can
disagree with the system it is auditing, and then it is evidence of nothing.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime

from bcs import order_journal


def _fmt_ctx(ctx: dict) -> str:
    bits = []
    for k in ('stock', 'strategy', 'reason', 'leg'):
        if ctx.get(k) is not None:
            bits.append(str(ctx[k]))
    tail = []
    if ctx.get('bid') is not None or ctx.get('ask') is not None:
        tail.append(f"book {ctx.get('bid')}/{ctx.get('ask')}")
    if ctx.get('book_reliable') is not None:
        tail.append('reliable' if ctx['book_reliable'] else 'UNRELIABLE')
    if ctx.get('attempt'):
        tail.append(f"attempt {ctx['attempt']}")
    if ctx.get('urgent'):
        tail.append('URGENT')
    s = ' | '.join(bits)
    return s + ('   [' + ', '.join(tail) + ']' if tail else '')


def report(day=None, only_unresolved=False) -> int:
    """Prints the report. Returns the number of unresolved intents, which is
    the one condition a person must act on, so it doubles as an exit code."""
    records = order_journal.read_day(day)
    path = order_journal.journal_path(day)

    if not records:
        print(f'No order journal at {path}.')
        print('Nothing was placed, and nothing was ATTEMPTED - those are the '
              'same line here only because the journal writes on intent. A '
              'session that ran and did nothing looks exactly like a session '
              'that never ran, so check the cron log too.')
        return 0

    corrupt = [r for r in records if r.get('kind') == 'corrupt']
    intents = [r for r in records if r.get('kind') == 'intent']
    results = {r['intent_id']: r for r in records if r.get('kind') == 'result'}
    dangling = [i for i in intents if i['intent_id'] not in results]

    print(f'ORDER JOURNAL  {path.name}')
    print('=' * 78)
    dry = sum(1 for i in intents if i.get('dry_run'))
    print(f'{len(intents)} order intent(s): {dry} dry-run, {len(intents) - dry} LIVE')
    if corrupt:
        print(f'{len(corrupt)} CORRUPT line(s) - see below')
    print()

    if only_unresolved:
        shown = dangling
        if not shown:
            print('No unresolved intents. Every order that was started was '
                  'also finished.')
            return 0
    else:
        shown = intents

    by_trade = defaultdict(list)
    for i in shown:
        by_trade[(i.get('context') or {}).get('trade_id')].append(i)

    for tid in sorted(by_trade, key=lambda x: (x is None, x)):
        head = f'trade #{tid}' if tid is not None else 'no trade id on record'
        print(f'-- {head} ' + '-' * max(0, 74 - len(head)))
        for i in by_trade[tid]:
            res = results.get(i['intent_id'])
            if res is None:
                status = 'NO RESULT - may exist at the broker'
            elif res.get('error'):
                status = f"rejected: {res['error']}"
            else:
                status = f"-> {res.get('order_id')}"
            mode = 'DRY ' if i.get('dry_run') else 'LIVE'
            print(f"  {i['ts'][11:]} {mode} {i['txn_type']:4s} {i['symbol']} "
                  f"x {i['qty']} @ {i['price']}")
            print(f"          {_fmt_ctx(i.get('context') or {})}")
            print(f"          {status}")
        print()

    if corrupt and not only_unresolved:
        print('CORRUPT LINES')
        for c in corrupt:
            print(f"  line {c['line_no']}: {c['error']}")
            print(f"    {c['raw']}")
        print()

    if dangling:
        print('!' * 78)
        print(f'{len(dangling)} INTENT(S) WITH NO RESULT.')
        print('The process did not survive the broker call. An order may be '
              'live at the broker while the trade store believes nothing '
              'happened. Reconcile against the broker order book BEFORE the '
              'next session - this is the one line in this report that is not '
              'informational.')
        print('!' * 78)
    return len(dangling)


def compare_to_store(day=None) -> None:
    """What the journal says was placed, next to what the stores recorded.

    Deliberately a side-by-side and NOT a verdict. The two can legitimately
    differ — a dry run books nothing by design, a partial close writes one
    store row for several orders — so a machine that declared PASS/FAIL here
    would be asserting a mapping nobody has established yet. That mapping is
    what the first dry-run session is for.
    """
    records = order_journal.read_day(day)
    intents = [r for r in records if r.get('kind') == 'intent']

    print()
    print('JOURNAL vs STORES')
    print('=' * 78)
    placed = defaultdict(list)
    for i in intents:
        placed[(i.get('context') or {}).get('trade_id')].append(i)

    # ALL trades, not just open ones: an order placed today most likely
    # CLOSED its trade, so filtering to open would hide every row this report
    # exists to show.
    try:
        from bcs.trade_store import get_store as get_bcs
        from fallen_hero import get_store as get_fh
        from bear_put import get_store as get_bps
        trades = []
        for get, tag in ((get_bcs, 'BCS'), (get_fh, 'FH'), (get_bps, 'BPS')):
            try:
                for t in get().load_trades():
                    trades.append(dict(t, _strategy=tag))
            except Exception as e:
                print(f'  ({tag} store unreadable: {e})')
    except Exception as e:
        print(f'Could not read the stores ({e}). Journal side only.')
        trades = []

    day_str = day or datetime.now().strftime('%Y%m%d')
    closed_today = [
        t for t in trades
        if str((t.get('exit') or {}).get('exit_date', '')).replace('-', '')
        == day_str]

    print(f'journal: {len(intents)} intent(s) across '
          f'{len(placed)} trade(s)')
    print(f'stores:  {len(closed_today)} trade(s) recorded as closed today')
    print()
    for tid, rows in sorted(placed.items(), key=lambda kv: (kv[0] is None, kv[0])):
        match = next((t for t in trades if t.get('id') == tid), None)
        state = (match.get('status') if match else 'not found in any store')
        reasons = {(r.get('context') or {}).get('reason') for r in rows}
        print(f'  trade #{tid}: {len(rows)} order(s), reason(s) '
              f'{sorted(x for x in reasons if x)} - store says {state}')
    if not placed:
        print('  (no orders intended)')
    print()
    print('Read this side by side; it is not a verdict. A dry run books '
          'nothing by design, and one store row can cover several orders.')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--day', help='YYYYMMDD (default: today)')
    ap.add_argument('--unresolved', action='store_true',
                    help='only intents with no result')
    ap.add_argument('--compare', action='store_true',
                    help='also show the trade stores side by side')
    a = ap.parse_args(argv)
    n = report(a.day, a.unresolved)
    if a.compare:
        compare_to_store(a.day)
    return 1 if n else 0


if __name__ == '__main__':
    raise SystemExit(main())
