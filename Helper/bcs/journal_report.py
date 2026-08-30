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
    # WHOLE BOOK: a DIFFERENT book (hand-entered BCS trades). The cohort rule
    # scopes the zebra book only; `_load_zebra` below is the one that scopes.
    from bcs.trade_store import get_store
    return get_store().load_trades()


def _load_fh():
    # WHOLE BOOK: a different book (Fallen Hero). See `_load_bcs`.
    from fallen_hero import get_store
    return get_store().load_trades()


def _load_bps():
    # WHOLE BOOK: a different book (bear put). See `_load_bcs`.
    from bear_put import get_store
    return get_store().load_trades()


def _load_zebra():
    from bcs.zebra_adapter import get_adapter
    from zebra.trade_store import decided
    adapter = get_adapter()
    # `load_trades` on the adapter is deliberately the RAW zebra records, not
    # `map_trade`d ones: the report reads `exit_date`, `status` and `cohort`
    # by zebra's own names, and a mapped copy would rename half of them.
    #
    # `decided`: the journal compares INTENDED orders against what the books
    # record, and the retired engine placed none of the orders this journal
    # holds -- it predates the journal entirely. Including its 399 records
    # would report them all as "in the book, no matching intent", which is the
    # shape of a real finding and would bury the real ones.
    if adapter is None:
        return []
    return decided(adapter.load_trades())


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


def match_state(trades: List[dict], trade_id, book=None) -> str:
    """How the stores answer for one journalled trade id.

    Ids collide across the four books - all of them number from 1 - so the
    number alone names up to four different positions.

    **N5.** Journal lines written from 2026-08-29 carry `context.book`, which
    resolves the collision outright. Lines written before that do not, and
    ambiguity there is still REPORTED, never resolved: picking the first match
    would silently name the wrong position in an incident report. The two
    cases are distinguished in the text, because "we cannot tell" and "this
    journal is too old to say" need different responses from a reader.
    """
    if trade_id is None:
        return 'no trade id on the journal line'
    hits = [t for t in trades if t.get('id') == trade_id]
    if not hits:
        return 'not found in any store'
    if book:
        # `book` is a `_store_type` ('bcs'/'bps'/'fh'/'zebra'); `_strategy`
        # here is the same word uppercased. Pinned by
        # `test_the_journals_book_vocabulary_matches_this_reports`.
        named = [t for t in hits if t.get('_strategy') == str(book).upper()]
        if len(named) == 1:
            return 'store says ' + _describe(named[0])
        if not named:
            return ('journal says book %r, and id %s is NOT in that book '
                    '(found in: %s)'
                    % (book, trade_id,
                       ', '.join(sorted({str(t.get('_strategy'))
                                         for t in hits}))))
    if len(hits) == 1:
        return 'store says ' + _describe(hits[0])
    return ('AMBIGUOUS - id %s exists in %d books: %s (the journal line names '
            'only the number - it predates `context.book`)'
            % (trade_id, len(hits),
               ', '.join(_describe(t) for t in hits)))


def _fmt_ctx(ctx: dict) -> str:
    bits = []
    for k in ('book', 'stock', 'strategy', 'reason', 'leg'):
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


def _exit_is_approximate(trade: dict) -> bool:
    """Does this closed record admit its P&L is an approximation?

    One reader for ALL FOUR books. `bcs`, `bear_put` and `zebra` surface the
    marker at the top level as `exit_approximate`; every book that writes a
    nested exit also carries `pnl_approximate` inside it, and records closed
    before the marker existed carry neither. Reading both is what makes this
    schema-agnostic — the same reason `EXIT_SCHEMA` exists a few lines up.

    Absence means EXACT, deliberately: ~450 historical records predate the
    marker, and defaulting them to approximate puts a caveat on every line.
    """
    if trade.get('exit_approximate') is True:
        return True
    return ((trade.get('exit') or {}).get('pnl_approximate') is True)


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
    # N14 — a closed record whose P&L is known to be an approximation must say
    # so HERE, where the day's closes are reconciled against the orders. The
    # marker means a leg was counted at 0.00 (already flat on arrival, or a
    # fill that could not be recovered), so the figure is wrong in a KNOWN
    # direction. Silence in this tool is what let it read as a measurement.
    approx = [t for t in closed_today if _exit_is_approximate(t)]
    if approx:
        print('         ~ %d of %d has an APPROXIMATE P&L (a leg counted at '
              '0.00): %s' % (
                  len(approx), len(closed_today),
                  ', '.join('#%s %s' % (t.get('id'), t.get('stock', '?'))
                            for t in approx)))
    print()
    for tid, rows in sorted(placed.items(), key=lambda kv: (kv[0] is None, kv[0])):
        ctxs = [r.get('context') or {} for r in rows]
        reasons = {c.get('reason') for c in ctxs}
        # N5. The book, from the journal's own lines. A set, not the first
        # one: if two books' orders somehow landed under one id, saying so is
        # the report's job — resolving it by picking one is exactly the
        # silent mis-naming this field was added to end.
        books = {c.get('book') for c in ctxs if c.get('book')}
        book = next(iter(books)) if len(books) == 1 else None
        named = ('#%s' % tid) if book is None else ('%s#%s' % (book, tid))
        print(f'  trade {named}: {len(rows)} order(s), reason(s) '
              f'{sorted(x for x in reasons if x)} - '
              f'{match_state(trades, tid, book=book)}')
        if len(books) > 1:
            print('         !! this id carries orders tagged with %d different '
                  'books: %s' % (len(books), sorted(books)))
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
