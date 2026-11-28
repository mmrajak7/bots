"""Live naked records are their OWN books in the digest and the dashboard (P5).

A `naked_long` record with `paper: False` is real money. The BCS cohort
readers already skip it (`scored`/`decided` default to structure 'bcs'), so
without a section of its own a real-money result would simply be ABSENT from
the dated record. These tests pin both halves: the naked books appear (live,
rehearsal and override separately, "none" when empty), and none of them leaks
into the BCS cohort totals.
"""
import json
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg          # noqa: E402
from zebra import digest                 # noqa: E402

DAY = '2026-12-02'


def _naked(i, status='exited', pnl=500.0, net=450.0, rehearsal=False, override=False,
           paper=False, entry='2026-12-01', exit_='2026-12-02', capital=20000.0):
    t = {'id': i, 'structure': 'naked_long', 'cohort': cfg.COHORT_START, 'status': status,
         'paper': paper, 'rehearsal': rehearsal, 'override': override, 'parent_id': 900 + i,
         'stock': 'ACME', 'direction': 'CE', 'entry_date': entry, 'entry_time': '10:00:00',
         'long_symbol': 'ACME26DEC100CE', 'entry_long_price': 10.0, 'tp_spot': 104.0,
         'expiry': '2026-12-29', 'capital': capital, 'debit': 10.0, 'quantity': 2000}
    if status == 'exited':
        t.update(pnl=pnl, pnl_net=net, exit_date=exit_, exit_reason='tp')
    return t


def _bcs(i, pnl=100.0, day=DAY):
    return {'id': i, 'structure': 'bcs', 'cohort': cfg.COHORT_START, 'status': 'exited',
            'paper': True, 'stock': 'BCSCO', 'direction': 'CE', 'entry_date': '2026-11-30',
            'exit_date': day, 'pnl': pnl, 'pnl_net': pnl, 'exit_reason': 'paper:tp'}


# -- digest: the pure split ------------------------------------------------------

def test_the_three_books_are_kept_apart():
    rows = [_naked(1), _naked(2, rehearsal=True, pnl=-100.0, net=-120.0),
            _naked(3, override=True, rehearsal=True), _naked(4, status='entered', exit_=None)]
    nb = digest._naked_books(rows, DAY)
    assert (nb['live']['closed'], nb['live']['net'], nb['live']['open']) == (1, 450.0, 1)
    assert nb['live']['capital_open'] == 20000.0
    assert (nb['rehearsal']['closed'], nb['rehearsal']['wins'], nb['rehearsal']['net']) == (1, 0, -120.0)
    # An override rehearsal is an OVERRIDE trade: it reaches no other book.
    assert nb['override']['closed'] == 1 and nb['rehearsal']['closed'] == 1


def test_the_books_are_bounded_by_the_digest_date():
    rows = [_naked(1, exit_='2026-12-03'), _naked(2, entry='2026-12-03', exit_='2026-12-04')]
    nb = digest._naked_books(rows, DAY)
    # #1 closed AFTER the day: on the day it was open. #2 did not exist yet.
    assert (nb['live']['closed'], nb['live']['open'], nb['live']['opened']) == (0, 1, 0)


def test_an_unpriced_naked_exit_is_closed_not_open_and_not_zero():
    nb = digest._naked_books([_naked(1, pnl=None, net=None)], DAY)
    assert (nb['live']['unpriced'], nb['live']['open'], nb['live']['closed']) == (1, 0, 0)
    assert nb['live']['capital_open'] == 0.0
    assert 'UNPRICED' in ' '.join(digest._render_naked(nb))


def test_a_naked_record_not_marked_live_is_left_out_and_counted():
    nb = digest._naked_books([_naked(1, paper=True), _naked(2, paper=None)], DAY)
    assert nb['skipped_paper'] == 2 and nb['live']['closed'] == 0


def test_no_naked_record_renders_none_cleanly():
    text = '\n'.join(digest._render_naked(digest._naked_books([], DAY)))
    assert 'own books' in text and '_none — no live, rehearsal or override naked record_' in text
    assert 'could not be read' in '\n'.join(digest._render_naked(None))


# -- digest: end to end, off a real store file -------------------------------------

@pytest.fixture
def logdir(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    return tmp_path


def _store(logdir, rows):
    (logdir / 'zebra_trades.json').write_text(json.dumps(rows), encoding='utf-8')


def test_the_digest_prints_none_when_the_store_has_no_naked_record(logdir):
    _store(logdir, [_bcs(1)])
    text = digest.render(digest.build(DAY))
    assert '## Naked long — own books' in text and '_none —' in text


def test_a_live_naked_result_never_enters_the_bcs_cohort_total(logdir):
    _store(logdir, [_bcs(1, pnl=100.0), _naked(2, pnl=9000.0, net=8800.0),
                    _naked(3, rehearsal=True, pnl=-50.0, net=-60.0)])
    d = digest.build(DAY)
    assert d['cohort']['closed'] == 1 and d['cohort']['net'] == 100
    assert d['naked_books']['live']['net'] == 8800.0
    assert d['naked_books']['rehearsal']['net'] == -60.0
    text = digest.render(d)
    assert '**live**' in text and '**rehearsal**' in text and '**override**' not in text


# -- the dashboard -----------------------------------------------------------------

class _Store:
    def __init__(self, rows):
        self.rows = rows

    def load_trades(self):
        return list(self.rows)


def _status(monkeypatch, capsys, rows):
    from zebra import __main__ as cli
    monkeypatch.setattr('zebra.trade_store.get_store', lambda *a, **k: _Store(rows))
    cli.cmd_status(type('A', (), {'json': False})())
    return capsys.readouterr().out


def test_the_dashboard_says_none_when_there_is_no_naked_record(monkeypatch, capsys):
    out = _status(monkeypatch, capsys, [])
    i = out.index('Naked long: own books')
    assert out[i:].splitlines()[1].strip() == 'none'


def test_the_dashboard_shows_each_naked_book_on_its_own(monkeypatch, capsys):
    out = _status(monkeypatch, capsys, [
        _naked(1, pnl=500.0, net=450.0), _naked(2, rehearsal=True, pnl=-100.0, net=-120.0),
        _naked(3, status='entered')])
    sec = out[out.index('Naked long: own books'):]
    assert 'live      open 1  closed 1  net Rs 450  (1W / 0L)' in sec
    assert 'rehearsal open 0  closed 1  net Rs -120  (0W / 1L)' in sec
    assert 'override  none' in sec
    # The cohort scoreboard above it is BCS-only: no naked P&L reaches it.
    board = out[out.index('CURRENT ENGINE'):out.index('Naked long: own books')]
    assert 'Rs 450' not in board and 'No trades yet' in board
    # And the WHOLE BOOK performance lines never carry a naked trade.
    perf = out[out.index('Performance (WHOLE BOOK'):out.index('Naked long: own books')]
    assert 'Naked' not in perf and 'Rs 450' not in perf and 'Rs -120' not in perf
