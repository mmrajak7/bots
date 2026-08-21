# -*- coding: utf-8 -*-
"""Open-position detail in the EOD/weekly report.

Owner's request, 2026-08-21: "Positions - Entry (spot price), LTP, our P&L
for this position ... so we know which positions are performing now" — for
both the daily and the weekly summary.

The traps these tests hold shut:
  * a missing quote must blank only ITS field, never the whole line, and must
    never be counted as a flat position when ranking;
  * the aggregate must say so when it could not quote everything;
  * `M&M` is a live NSE symbol in this book, and one bare `&` 400-rejects the
    entire Telegram message silently.
"""

import pytest

from zebra import config as cfg
from zebra import report as R


def _trade(tid, stock, debit, qty, entry_spot, **kw):
    t = {
        'id': tid, 'stock': stock, 'direction': 'CE', 'status': 'entered',
        'long_strike': 100, 'short_strike': 110,
        'debit': debit, 'quantity': qty, 'capital': debit * qty,
        'entry_spot': entry_spot, 'entry_date': '2026-08-20',
        'tp_spot': entry_spot * 1.03, 'structure': 'bcs',
    }
    t.update(kw)
    return t


def _report(trades, unreal, typ='daily'):
    return {'date': '2026-08-21', 'type': typ, 'closed': [], 'open': trades,
            'closed_summary': R._summarize_exits([]), 'unrealized': unreal,
            'week_start': '2026-08-17', 'week_end': '2026-08-21'}


@pytest.fixture(autouse=True)
def _listing_on(monkeypatch):
    monkeypatch.setattr(cfg, 'EOD_OPEN_POSITIONS', True)


# -- the three numbers the owner asked for --------------------------------

def test_entry_spot_ltp_and_pnl_all_present():
    t = _trade(1, 'KOTAKBANK', 6.55, 2000, 395.5)
    u = {1: {'mid': 7.65, 'pnl': 2200.0, 'pnl_pct': 16.79,
             'spot': 402.8, 'spot_pct': 1.846}}
    for text in (R.format_text(_report([t], u)),
                 R.format_telegram(_report([t], u))):
        assert '395.50' in text          # entry spot
        assert '402.80' in text          # LTP now
        assert '+1.8%' in text           # spot move since entry
        assert '+2,200' in text          # position P&L in rupees
        assert '+16.8%' in text          # position P&L in percent


def test_both_daily_and_weekly_carry_the_detail():
    t = _trade(1, 'CDSL', 24.6, 475, 1379.0)
    u = {1: {'mid': 25.7, 'pnl': 522.5, 'pnl_pct': 4.47,
             'spot': 1388.9, 'spot_pct': 0.718}}
    for typ in ('daily', 'weekly'):
        tg = R.format_telegram(_report([t], u, typ=typ))
        assert '1379.00' in tg and '1388.90' in tg and '+522' in tg


# -- partial quotes -------------------------------------------------------

def test_missing_mid_still_reports_spot():
    """A dead option quote must not take the underlying down with it."""
    t = _trade(1, 'PERSISTENT', 86.6, 125, 5598.5)
    u = {1: {'mid': None, 'pnl': None, 'pnl_pct': None,
             'spot': 5667.5, 'spot_pct': 1.232}}
    tg = R.format_telegram(_report([t], u))
    assert '5598.50' in tg and '5667.50' in tg
    assert 'quote n/a' in tg


def test_missing_spot_still_reports_pnl():
    t = _trade(1, 'MCX', 65.8, 225, 3124.7)
    u = {1: {'mid': 70.5, 'pnl': 1057.5, 'pnl_pct': 7.14,
             'spot': None, 'spot_pct': None}}
    tg = R.format_telegram(_report([t], u))
    assert '+1,058' in tg
    assert 'n/a' in tg


def test_aggregate_flags_that_it_could_not_quote_everything():
    a = _trade(1, 'AAA', 10.0, 100, 100.0)
    b = _trade(2, 'BBB', 10.0, 100, 100.0)
    u = {1: {'mid': 11.0, 'pnl': 100.0, 'pnl_pct': 10.0,
             'spot': 101.0, 'spot_pct': 1.0},
         2: {'mid': None, 'pnl': None, 'pnl_pct': None,
             'spot': None, 'spot_pct': None}}
    for text in (R.format_text(_report([a, b], u)),
                 R.format_telegram(_report([a, b], u))):
        assert '[1/2 quoted]' in text
    # ...and stays silent when everything quoted.
    u[2] = {'mid': 9.0, 'pnl': -100.0, 'pnl_pct': -10.0,
            'spot': 99.0, 'spot_pct': -1.0}
    assert 'quoted]' not in R.format_telegram(_report([a, b], u))


# -- ranking --------------------------------------------------------------

def test_best_performer_first_and_unquoted_last():
    trades = [_trade(1, 'FLAT', 10.0, 100, 100.0),
              _trade(2, 'BEST', 10.0, 100, 100.0),
              _trade(3, 'DARK', 10.0, 100, 100.0),
              _trade(4, 'WORST', 10.0, 100, 100.0)]
    u = {1: {'mid': 10.0, 'pnl': 0.0, 'pnl_pct': 0.0, 'spot': 100.0,
             'spot_pct': 0.0},
         2: {'mid': 15.0, 'pnl': 500.0, 'pnl_pct': 50.0, 'spot': 105.0,
             'spot_pct': 5.0},
         3: {'mid': None, 'pnl': None, 'pnl_pct': None, 'spot': None,
             'spot_pct': None},
         4: {'mid': 5.0, 'pnl': -500.0, 'pnl_pct': -50.0, 'spot': 95.0,
             'spot_pct': -5.0}}
    order = [t['stock'] for t in R._open_sorted(_report(trades, u))]
    assert order == ['BEST', 'FLAT', 'WORST', 'DARK']


# -- the marker ------------------------------------------------------------

def test_green_ball_for_winners_red_for_losers_white_when_unquoted():
    """Owner's call 2026-08-21: a coloured ball and no BCS ruler."""
    trades = [_trade(1, 'WIN', 10.0, 100, 100.0),
              _trade(2, 'LOSE', 10.0, 100, 100.0),
              _trade(3, 'DARK', 10.0, 100, 100.0)]
    u = {1: {'mid': 11.0, 'pnl': 100.0, 'pnl_pct': 10.0, 'spot': 101.0,
             'spot_pct': 1.0},
         2: {'mid': 9.0, 'pnl': -100.0, 'pnl_pct': -10.0, 'spot': 99.0,
             'spot_pct': -1.0},
         3: {'mid': None, 'pnl': None, 'pnl_pct': None, 'spot': None,
             'spot_pct': None}}
    tg = R.format_telegram(_report(trades, u))
    lines = {ln.split(' ')[0]: ln
             for ln in tg.split(chr(10)) if '<code>' in ln}
    assert '🟢' in lines and 'WIN' in lines['🟢']
    assert '🔴' in lines and 'LOSE' in lines['🔴']
    assert '⚪' in lines and 'DARK' in lines['⚪']
    # the tags that were dropped
    assert '📐' not in tg      # BCS ruler
    assert '↑' not in tg and '↓' not in tg   # direction arrows


# -- the escaping trap ----------------------------------------------------

def test_ampersand_symbol_is_escaped():
    """M&M is real, in this book, and would 400 the whole message raw."""
    t = _trade(1, 'M&M', 10.0, 100, 3000.0)
    u = {1: {'mid': 11.0, 'pnl': 100.0, 'pnl_pct': 10.0,
             'spot': 3030.0, 'spot_pct': 1.0}}
    tg = R.format_telegram(_report([t], u))
    assert 'M&amp;M' in tg
    assert '<code>M&M</code>' not in tg


# -- totals ---------------------------------------------------------------

def test_deployed_capital_and_book_return():
    a = _trade(1, 'AAA', 10.0, 100, 100.0)          # capital 1000
    b = _trade(2, 'BBB', 20.0, 100, 200.0)          # capital 2000
    u = {1: {'mid': 11.0, 'pnl': 100.0, 'pnl_pct': 10.0, 'spot': 101.0,
             'spot_pct': 1.0},
         2: {'mid': 21.0, 'pnl': 100.0, 'pnl_pct': 5.0, 'spot': 202.0,
             'spot_pct': 1.0}}
    unreal, deployed, quoted = R._open_totals(_report([a, b], u))
    assert (unreal, deployed, quoted) == (200.0, 3000.0, 2)
    tg = R.format_telegram(_report([a, b], u))
    assert 'Rs 3,000 deployed' in tg
    assert '+6.7%' in tg                             # 200 / 3000


def test_count_and_aggregate_survive_the_listing_being_off(monkeypatch):
    """The switch hides the per-position lines, never the book's total."""
    monkeypatch.setattr(cfg, 'EOD_OPEN_POSITIONS', False)
    t = _trade(1, 'KOTAKBANK', 6.55, 2000, 395.5)
    u = {1: {'mid': 7.65, 'pnl': 2200.0, 'pnl_pct': 16.79,
             'spot': 402.8, 'spot_pct': 1.846}}
    tg = R.format_telegram(_report([t], u))
    assert '1 pos' in tg and '+2,200' in tg
    assert 'KOTAKBANK' not in tg
