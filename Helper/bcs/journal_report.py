"""Read back a session's order intents. `python -m bcs.journal_report`.

This is the reading end of "alert-only first": run the exit path with
`--dry-run` against the live book for a session, then ask this what it would
have done and whether that matches what the stores actually booked.

    python -m bcs.journal_report                # today
    python -m bcs.journal_report --day 20260915
    python -m bcs.journal_report --unresolved   # only the dangling intents
    python -m bcs.journal_report --compare      # beside what the books recorded

`--compare` reads all FOUR books the order path can touch — bcs, fallen_hero,
bear_put and the BCS cohort in `logs/zebra_trades.json`. The fourth was missing
until 2026-08-27, so every cohort trade read "not found in any store" in the
one tool the dry-run evidence week is gated on.

It computes nothing about the market. Every number printed is read straight off
the journal line or off the trade store — a report that re-derives a price can
disagree with the system it is auditing, and then it is evidence of nothing.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
from typing import Callable, List, Optional

from bcs import order_journal


# ── the books a journalled order could belong to ────────────────────────────
#
# The BCS cohort lives in `logs/zebra_trades.json`, and until 2026-08-27 this
# report did not load it — so every cohort trade printed "not found in any
# store" while the dry-run evidence week was gated on exactly this tool.
#
# zebra is reached through `bcs.zebra_adapter`, the SAME seam the money path
# uses, rather than by importing `zebra.trade_store` here. A report that finds
# the cohort by a different route than the monitor does can agree with itself
# while disagreeing with the system it audits.
#
# One try per book, not one around all four: a single import error used to
# take out the whole store side, so an unreadable BCS store printed "Journal
# side only" and the other three books went unlooked-at.

def _load_bcs():
    from bcs.trade_store import get_store
    return get_store().load_trades()


def _load_fh():
    from fallen_hero import get_store
    return get_store().load_trades()


def _load_bps():
    from bear_put import get_store
    return get_store().load_trades()


def _load_zebra():
    from bcs.zebra_adapter import get_adapter
    adapter = get_adapter()
    # `load_trades` on the adapter is deliberately the RAW zebra records, not
    # `map_trade`d ones: the report reads `exit_date`, `status` and `cohort`
    # by zebra's own names, and a mapped copy would rename half of them.
    return adapter.load_trades() if adapter is not None else []


#: (tag, loader). The tag is stamped onto every record as `_strategy` and is
#: what selects the exit-date schema below, so the two lists cannot drift.
STORES = (
    ('BCS', _load_bcs),
    ('FH', _load_fh),
    ('BPS', _load_bps),
    ('ZEBRA', _load_zebra),
)

#: Which of the TWO exit schemas each book writes, named per book.
#:
#: Deliberately not a fallback chain. `(t.get('exit') or {}).get('exit_date')
#: or t.get('exit_date')` answers for both shapes without ever saying which
#: one matched, so the day a store changes shape this report reads "0 trades
#: closed today" — the quietest possible failure in a tool whose entire job is
#: to notice that the journal and the stores disagree.
#:
#:   nested — `t['exit']['exit_date']`  (bcs / fallen_hero / bear_put)
#:   flat   — `t['exit_date']`          (zebra, `zebra/trade_store.py:822`)
EXIT_SCHEMA = {
    'BCS': 'nested',
    'FH': 'nested',
    'BPS': 'nested',
    'ZEBRA': 'flat',
}


def exit_day(trade: dict) -> Optional[str]:
    """The trade's exit date as YYYYMMDD, or None while it is still open.

    Raises on a record with no known schema rather than guessing: the only
    thing that stamps `_strategy` is `load_all_trades` below, so an unknown
    tag is a wiring mistake in this file, and silently treating it as "never
    exited" would hide a whole book.
    """
    tag = trade.get('_strategy')
    schema = EXIT_SCHEMA.get(tag)
    if schema is None:
        raise ValueError(
            'no exit-date schema for strategy %r - add it to EXIT_SCHEMA '
            'beside its entry in STORES' % (tag,))
    if schema == 'nested':
        raw = (trade.get('exit') or {}).get('exit_date')
    else:
        raw = trade.get('exit_date')
    raw = str(raw or '').replace('-', '').strip()
    return raw or None


def load_all_trades(warn: Optional[Callable[[str], None]] = None) -> List[dict]:
    """Every record from every book, each tagged with `_strategy`.

    ALL trades, not just open ones: an order placed today most likely CLOSED
    its trade, so filtering to open would hide every row this report exists to
    show.
    """
    say = warn or print
    out: List[dict] = []
    for tag, loader in STORES:
        try:
            rows = loader()
        except Exception as e:
            say('  (%s store unreadable: %s)' % (tag, e))
            continue
        for t in rows or []:
            out.append(dict(t, _strategy=tag))
    return out


def _describe(trade: dict) -> str:
    """One store row, named by book and id. The book matters: the four stores
    number their trades from 1 independently, so `#1` alone names four
    different positions."""
    bit = '%s#%s %s' % (trade.get('_strategy'), trade.get('id'),
                        trade.get('status'))
    if trade.get('cohort'):
        bit += ' [cohort %s]' % trade['cohort']
    return bit


def match_state(trades: List[dict], trade_id) -> str:
    """How the stores answer for one journalled trade id.

    Ambiguity is REPORTED, never resolved. Ids collide across the four books
    and the journal line carries only the number, so picking the first match
    would silently name the wrong position in an incident report.
    """
    if trade_id is None:
        return 'no trade id on the journal line'
    hits = [t for t in trades if t.get('id') == trade_id]
    if not hits:
        return 'not found in any store'
    if len(hits) == 1:
        return 'store says ' + _describe(hits[0])
    return ('AMBIGUOUS - id %s exists in %d books: %s (the journal line names '
            'only the number)' % (trade_id, len(hits),
                                  ', '.join(_describe(t) for t in hits)))


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

    trades = load_all_trades()

    day_str = day or datetime.now().strftime('%Y%m%d')
    closed_today = [t for t in trades if exit_day(t) == day_str]
    by_book = defaultdict(int)
    for t in closed_today:
        by_book[t['_strategy']] += 1

    print(f'journal: {len(intents)} intent(s) across '
          f'{len(placed)} trade(s)')
    breakdown = ', '.join('%s %d' % (tag, by_book.get(tag, 0))
                          for tag, _ in STORES)
    print(f'stores:  {len(closed_today)} trade(s) recorded as closed today '
          f'({breakdown})')
    print()
    for tid, rows in sorted(placed.items(), key=lambda kv: (kv[0] is None, kv[0])):
        reasons = {(r.get('context') or {}).get('reason') for r in rows}
        print(f'  trade #{tid}: {len(rows)} order(s), reason(s) '
              f'{sorted(x for x in reasons if x)} - {match_state(trades, tid)}')
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
