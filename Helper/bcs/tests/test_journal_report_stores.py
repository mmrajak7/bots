"""`journal_report --compare` must be able to see the book under test.

The go-live plan gates on a dry-run evidence week, read back with
`python -m bcs.journal_report --compare`. Until 2026-08-27 that tool loaded
three stores — bcs, fallen_hero, bear_put — and not the fourth, so every trade
in the BCS cohort (`logs/zebra_trades.json`) printed "not found in any store",
and its `closed_today` filter read `t['exit']['exit_date']`, a key zebra never
writes: zebra keeps `exit_date` at the top level
(`zebra/trade_store.py:822`). Both halves of the tool were blind to the only
book it was being pointed at.

Run:  cd Helper && python -m pytest bcs/tests/test_journal_report_stores.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import journal_report as jr       # noqa: E402
from bcs import order_journal as oj        # noqa: E402
from zebra import config as zcfg           # noqa: E402

COHORT = zcfg.COHORT_START


def zebra_row(id_=419, status='exited', exit_date='2026-08-26', **extra):
    """A cohort record in ZEBRA's own shape: `exit_date` top level, no `exit`
    sub-dict at all. Copied from the real book (#419 KOTAKBANK, paper:tp)."""
    row = {'id': id_, 'stock': 'KOTAKBANK', 'status': status,
           'cohort': COHORT, 'structure': 'bcs', 'debit': 9.71,
           'exit_reason': 'paper:tp', 'pnl': 6800.0}
    if exit_date is not None:
        row['exit_date'] = exit_date
    row.update(extra)
    return row


def bcs_row(id_=1, status='closed', exit_date='2026-08-26'):
    """A BCS record in the OTHER shape: the exit lives under `exit`."""
    row = {'id': id_, 'stock': 'ICICIBANK', 'status': status}
    if exit_date is not None:
        row['exit'] = {'exit_date': exit_date, 'reason': 'TP'}
    return row


@pytest.fixture
def books(monkeypatch):
    """Swap the four store loaders for in-memory lists.

    Keeps the REAL tag->loader table shape, so a book added to `STORES`
    without an `EXIT_SCHEMA` entry still fails loudly here.
    """
    state = {'BCS': [], 'FH': [], 'BPS': [], 'ZEBRA': []}

    def table():
        return tuple((tag, (lambda t=tag: list(state[t])))
                     for tag, _ in jr.STORES)

    monkeypatch.setattr(jr, 'STORES', table())
    return state


def intent(trade_id, reason='TP'):
    return oj.record_intent(
        symbol='KOTAKBANK26SEP2100CE', txn_type='BUY', qty=400, price=12.5,
        exchange='NFO', dry_run=True,
        context={'trade_id': trade_id, 'stock': 'KOTAKBANK',
                 'reason': reason, 'leg': 'short', 'strategy': 'BCS'})


# ── end to end, through the REAL loader table ───────────────────────────────
#
# The tests below this section swap `jr.STORES`, which is a seam the pre-fix
# code did not have. This one does not: it stubs the four store modules at
# their own names, so it runs against either version of the report and is the
# test that actually proves the bug is gone.

class _Book:
    def __init__(self, rows):
        self._rows = rows

    def load_trades(self):
        return list(self._rows)


@pytest.fixture
def real_table(monkeypatch):
    """Stub the four books where the report's own imports find them."""
    import bear_put
    import fallen_hero
    from bcs import trade_store as bcs_store
    from bcs import zebra_adapter

    state = {'BCS': [], 'FH': [], 'BPS': [], 'ZEBRA': []}
    monkeypatch.setattr(bcs_store, 'get_store', lambda: _Book(state['BCS']))
    monkeypatch.setattr(fallen_hero, 'get_store', lambda: _Book(state['FH']))
    monkeypatch.setattr(bear_put, 'get_store', lambda: _Book(state['BPS']))
    monkeypatch.setattr(zebra_adapter, 'get_adapter',
                        lambda: _Book(state['ZEBRA']))
    return state


def test_the_cohort_book_is_loaded_at_all(real_table, capsys):
    """THE bug: `logs/zebra_trades.json` was not among the stores this report
    read, so every trade in the cohort — the only book the dry-run evidence
    week is about — printed 'not found in any store'."""
    # Today, because `record_intent` writes into today's journal file and the
    # report reads the journal and the stores for the SAME day.
    from datetime import datetime
    today = datetime.now()
    real_table['ZEBRA'].append(
        zebra_row(419, exit_date=today.strftime('%Y-%m-%d')))
    intent(419)
    jr.compare_to_store()
    out = capsys.readouterr().out
    assert 'not found in any store' not in out, \
        'the cohort store is still not being loaded'
    assert '419' in out
    assert '1 trade(s) recorded as closed today' in out, \
        "zebra's top-level exit_date is still not being read"


# ── the cohort is visible at all ────────────────────────────────────────────

def test_a_cohort_trade_is_named_not_reported_missing(books, capsys):
    """The bug, stated as its symptom: the one book the dry-run week is about
    printed 'not found in any store' for every trade in it."""
    books['ZEBRA'].append(zebra_row(419))
    intent(419)
    jr.compare_to_store()
    out = capsys.readouterr().out
    assert 'not found in any store' not in out
    assert 'ZEBRA#419' in out
    assert 'exited' in out


def test_a_cohort_trade_is_labelled_as_cohort(books, capsys):
    """450 records in that file are the dropped back-ratio generation. A
    report that cannot tell them apart cannot be read as cohort evidence."""
    books['ZEBRA'] += [zebra_row(419), zebra_row(7, status='exited',
                                                 exit_date='2026-03-01')]
    books['ZEBRA'][1].pop('cohort')
    intent(419)
    intent(7)
    jr.compare_to_store()
    out = capsys.readouterr().out
    assert ('ZEBRA#419 exited [cohort %s]' % COHORT) in out
    assert 'ZEBRA#7 exited\n' in out or 'ZEBRA#7 exited ' in out
    assert out.count('[cohort') == 1


# ── the two exit-date schemas ───────────────────────────────────────────────

def test_zebras_top_level_exit_date_is_read(books, capsys):
    """`t['exit']['exit_date']` does not exist on a zebra row, so the closed
    count read zero on a day the cohort actually closed trades."""
    books['ZEBRA'].append(zebra_row(419, exit_date='2026-08-26'))
    jr.compare_to_store(day='20260826')
    out = capsys.readouterr().out
    assert '1 trade(s) recorded as closed today' in out
    assert 'ZEBRA 1' in out


def test_the_bcs_nested_exit_date_still_counts(books, capsys):
    books['BCS'].append(bcs_row(1, exit_date='2026-08-26'))
    jr.compare_to_store(day='20260826')
    out = capsys.readouterr().out
    assert '1 trade(s) recorded as closed today' in out
    assert 'BCS 1' in out


def test_both_schemas_count_on_the_same_day(books, capsys):
    books['BCS'].append(bcs_row(1, exit_date='2026-08-26'))
    books['ZEBRA'].append(zebra_row(419, exit_date='2026-08-26'))
    books['ZEBRA'].append(zebra_row(433, exit_date='2026-08-24'))
    jr.compare_to_store(day='20260826')
    out = capsys.readouterr().out
    assert '2 trade(s) recorded as closed today' in out


def test_an_open_trade_has_no_exit_day():
    assert jr.exit_day(dict(zebra_row(423, status='entered', exit_date=None),
                            _strategy='ZEBRA')) is None
    assert jr.exit_day(dict(bcs_row(1, status='open', exit_date=None),
                            _strategy='BCS')) is None


def test_a_zebra_row_is_never_read_with_the_bcs_schema():
    """Explicit per book, not a fallback chain. A chain answers for both
    shapes without saying which matched, so the day a store changes shape the
    report reads '0 closed today' — the quietest possible failure in a tool
    whose whole job is to notice a mismatch."""
    assert jr.EXIT_SCHEMA['ZEBRA'] == 'flat'
    assert jr.EXIT_SCHEMA['BCS'] == 'nested'
    # a zebra row carrying BOTH keys must answer from its own schema
    row = dict(zebra_row(419, exit_date='2026-08-26'), _strategy='ZEBRA')
    row['exit'] = {'exit_date': '2020-01-01'}
    assert jr.exit_day(row) == '20260826'


def test_a_book_with_no_declared_schema_raises(books):
    """`STORES` and `EXIT_SCHEMA` are two lists that must move together. A
    fifth book added to one and not the other must fail loudly rather than
    silently never appearing in the closed count."""
    with pytest.raises(ValueError):
        jr.exit_day({'id': 1, '_strategy': 'NEWBOOK'})


# ── ids collide across the four books ───────────────────────────────────────

def test_a_colliding_id_is_reported_not_silently_resolved(books, capsys):
    """All four books number from 1, and the journal line carries only the
    number. Picking the first match would name the wrong position in an
    incident report, confidently."""
    books['BCS'].append(bcs_row(1, status='closed'))
    books['ZEBRA'].append(zebra_row(1, status='entered', exit_date=None))
    intent(1)
    jr.compare_to_store()
    out = capsys.readouterr().out
    assert 'AMBIGUOUS' in out
    assert 'BCS#1' in out and 'ZEBRA#1' in out


def test_an_unmatched_id_still_says_not_found(books, capsys):
    books['ZEBRA'].append(zebra_row(419))
    intent(999)
    jr.compare_to_store()
    assert 'not found in any store' in capsys.readouterr().out


# ── one broken book must not blind the others ───────────────────────────────

def test_an_unreadable_store_does_not_take_the_others_with_it(monkeypatch,
                                                              capsys):
    """The old code wrapped all three imports in ONE try, so a single failure
    printed 'Journal side only' and looked at nothing."""
    def boom():
        raise RuntimeError('drive auth failed')

    monkeypatch.setattr(jr, 'STORES', (
        ('BCS', boom),
        ('ZEBRA', lambda: [zebra_row(419)]),
    ))
    trades = jr.load_all_trades()
    assert [t['id'] for t in trades] == [419]
    assert 'BCS store unreadable' in capsys.readouterr().out


def test_a_store_returning_none_is_not_an_error(monkeypatch):
    monkeypatch.setattr(jr, 'STORES', (('ZEBRA', lambda: None),))
    assert jr.load_all_trades() == []


# ── the cohort is reached the way the money path reaches it ─────────────────

def test_the_zebra_loader_goes_through_the_adapter(monkeypatch):
    """Same seam `bcs/spread_monitor.py` uses. A report that finds the cohort
    by a different route can agree with itself while disagreeing with the
    system it audits."""
    from bcs import zebra_adapter

    class FakeAdapter:
        def load_trades(self):
            return [zebra_row(437)]

    monkeypatch.setattr(zebra_adapter, 'get_adapter', lambda: FakeAdapter())
    assert [t['id'] for t in jr._load_zebra()] == [437]


def test_no_zebra_store_is_an_empty_book_not_a_crash(monkeypatch):
    """`get_adapter` returning None is documented as 'zebra not usable here';
    the other three books must still report."""
    from bcs import zebra_adapter
    monkeypatch.setattr(zebra_adapter, 'get_adapter', lambda: None)
    assert jr._load_zebra() == []


# ── it is still readable after an incident ──────────────────────────────────

def test_the_compare_output_is_pure_ascii(books, capsys):
    """Same rail as the report half: a forensic tool must not depend on the
    terminal's encoding."""
    books['ZEBRA'].append(zebra_row(419))
    books['BCS'].append(bcs_row(1))
    intent(419)
    intent(1)
    jr.compare_to_store()
    out = capsys.readouterr().out
    assert out.strip()
    assert not [c for c in out if ord(c) > 127]
    out.encode('cp1252')


# ── N5 · the journal can NAME the book, so the collision resolves ───────────
#
# All four stores number their trades from 1, so `#1` on a journal line named
# four different positions and this tool could only report the collision.
# `context.book` (a `_store_type`) resolves it. Lines written before
# 2026-08-29 do not carry it, and those stay AMBIGUOUS rather than guessing —
# picking the first match would silently name the wrong position in the one
# file that says what this engine intended to trade.

def _booked_intent(trade_id, book, reason='TP'):
    return oj.record_intent(
        symbol='KOTAKBANK26SEP2100CE', txn_type='BUY', qty=400, price=12.5,
        exchange='NFO', dry_run=True,
        context={'trade_id': trade_id, 'book': book, 'stock': 'KOTAKBANK',
                 'reason': reason, 'leg': 'short', 'strategy': 'BCS'})


def test_a_colliding_id_is_resolved_by_the_book(books):
    trades = [dict(bcs_row(1), _strategy='BCS'),
              dict(zebra_row(1), _strategy='ZEBRA')]
    assert 'AMBIGUOUS' in jr.match_state(trades, 1)
    assert jr.match_state(trades, 1, book='zebra').startswith('store says ZEBRA#1')


def test_an_OLD_line_without_a_book_still_reports_the_collision(books):
    """The pre-2026-08-29 journal. Not resolvable, and it must not pretend to
    be — but it now says WHY it cannot be, which is a different instruction to
    the reader than "we cannot tell"."""
    trades = [dict(bcs_row(1), _strategy='BCS'),
              dict(zebra_row(1), _strategy='ZEBRA')]
    out = jr.match_state(trades, 1, book=None)
    assert 'AMBIGUOUS' in out and 'predates' in out


def test_a_book_the_id_is_NOT_in_is_reported_not_swallowed(books):
    """The journal and the stores disagreeing is the whole point of this tool.
    Falling back to "well, there is one match, use that" would hide exactly
    the disagreement it exists to surface."""
    trades = [dict(bcs_row(7), _strategy='BCS')]
    out = jr.match_state(trades, 7, book='zebra')
    assert 'NOT in that book' in out and 'BCS' in out


def test_the_compare_line_names_the_book(books, capsys):
    books['ZEBRA'].append(zebra_row(419))
    _booked_intent(419, 'zebra')
    jr.compare_to_store()
    out = capsys.readouterr().out
    assert 'trade zebra#419' in out
    assert 'ZEBRA#419' in out


def test_orders_tagged_with_TWO_books_under_one_id_are_flagged(books, capsys):
    """Should never happen. If it does, saying so is the report's job —
    resolving it by picking one is the silent mis-naming this field ended."""
    books['ZEBRA'].append(zebra_row(5))
    books['BCS'].append(bcs_row(5))
    _booked_intent(5, 'zebra')
    _booked_intent(5, 'bcs')
    jr.compare_to_store()
    out = capsys.readouterr().out
    assert '2 different books' in out
