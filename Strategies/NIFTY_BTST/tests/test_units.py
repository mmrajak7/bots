import json
from datetime import date, timedelta

import pytest

from conftest import APP, EXPIRIES, make_options, sym
from nifty_btst import compare, config as config_mod, selection
from nifty_btst.pricing import Book, book_from_quote, charges, reliable
from nifty_btst.store import CorruptBook, Store


# ---------------------------------------------------------------- selection
def test_pick_expiry_nearest_on_a_normal_day():
    exps = [date.fromisoformat(e) for e in EXPIRIES]
    assert selection.pick_expiry(exps, date(2026, 10, 26)) == date(2026, 10, 27)


def test_pick_expiry_rolls_to_next_weekly_on_expiry_day():
    exps = [date.fromisoformat(e) for e in EXPIRIES]
    assert selection.pick_expiry(exps, date(2026, 10, 27)) == date(2026, 11, 3)
    assert selection.is_expiry_day(exps, date(2026, 10, 27))


def test_pick_expiry_refuses_when_nothing_is_live():
    with pytest.raises(selection.SelectionError):
        selection.pick_expiry([date(2026, 10, 27)], date(2026, 10, 27))
    with pytest.raises(selection.SelectionError):
        selection.pick_expiry([date(2026, 10, 27)], date(2026, 10, 28))


def test_nearest_premium_ignores_untraded_and_breaks_ties_toward_spot():
    opts = [o for o in make_options() if o['expiry'] == EXPIRIES[0]]
    books = {
        sym(EXPIRIES[0], 22800, 'CE'): Book(95.0, 94.9, 95.1, 1, 1, ''),
        sym(EXPIRIES[0], 22900, 'CE'): Book(105.0, 104.9, 105.1, 1, 1, ''),
        sym(EXPIRIES[0], 22850, 'CE'): Book(0.0, 0, 0, 0, 0, ''),   # never traded
    }
    pick = selection.pick_nearest_premium(opts, books, 'CE', 100.0, spot=22810)
    assert pick['strike'] == 22800.0          # equal distance; nearer spot wins
    assert selection.pick_nearest_premium(opts, {}, 'PE', 100.0, 22800) is None


def test_candidates_band():
    opts = make_options()
    c = selection.candidates(opts, date(2026, 10, 27), 22800, 100)
    assert {o['strike'] for o in c} == {22700.0, 22750.0, 22800.0, 22850.0, 22900.0}


# ------------------------------------------------------------------ pricing
def test_book_from_kite_quote_shape():
    q = {'last_price': 101.5, 'last_trade_time': 'x',
         'depth': {'buy': [{'price': 101.4, 'quantity': 650}],
                   'sell': [{'price': 101.6, 'quantity': 130}]}}
    b = book_from_quote(q)
    assert (b.ltp, b.bid, b.ask, b.bid_qty, b.ask_qty) == (101.5, 101.4, 101.6, 650, 130)
    assert book_from_quote(None) is None
    empty = book_from_quote({'last_price': 5, 'depth': {'buy': [], 'sell': []}})
    assert empty.bid == 0 and empty.ask == 0


@pytest.mark.parametrize('bid,ask,ok', [
    (100.0, 100.2, True),
    (0.0, 100.2, False),          # one-sided
    (100.3, 100.2, False),        # crossed
    (90.0, 110.0, False),         # Rs 20 wide on a Rs 100 option
    (1.0, 2.5, True),             # Rs 1.5 wide: under the Rs 2 absolute floor
])
def test_reliable(bid, ask, ok):
    assert reliable(Book(100, bid, ask, 1, 1, ''), 2.0, 10.0)[0] is ok
    assert reliable(None, 2.0, 10.0)[0] is False


def test_charges_match_a_hand_computed_contract_note():
    rates = dict(brokerage_per_order=20.0, stt_sell_pct=0.10, exchange_pct=0.03503,
                 sebi_pct=0.0001, stamp_buy_pct=0.003, gst_pct=18.0)
    c = charges(10000.0, 13000.0, rates)
    assert c['brokerage'] == 40.0
    assert c['stt'] == 13.0
    assert c['exchange'] == pytest.approx(8.06, abs=0.01)
    assert c['stamp'] == 0.3
    assert c['gst'] == pytest.approx((40 + 8.0569 + 0.023) * 0.18, abs=0.01)
    no_brk = charges(10000.0, 13000.0, rates, include_brokerage=False)
    assert no_brk['brokerage'] == 0 and no_brk['total'] < c['total'] - 40


# ------------------------------------------------------------------- config
def _raw():
    return json.load(open(APP / 'btst_config.defaults.json'))


def test_defaults_load():
    cfg = config_mod.build(_raw())
    assert cfg.trigger_mult == 1.5 and cfg.stop_frac == 0.5 and cfg.lots == 1


@pytest.mark.parametrize('key,val', [
    ('telegram_enabled', 'false'),     # a string must not pass as a boolean
    ('stop_frac', 1.2),
    ('trigger_mult', 1.0),
    ('lots', 1.5),
    ('poll_seconds', 0.5),
    ('mark_time', '9:20'),
    ('entry_start', '09:19:00'),       # before the mark
])
def test_config_rejects(key, val):
    raw = _raw()
    raw[key] = val
    with pytest.raises(config_mod.ConfigError):
        config_mod.build(raw)


def test_overlay_merges_fee_rates_one_level(tmp_path):
    ov = tmp_path / 'o.json'
    ov.write_text(json.dumps({'fee_rates': {'brokerage_per_order': 0.0}, 'lots': 2}))
    cfg = config_mod.load(APP / 'btst_config.defaults.json', ov)
    assert cfg.fee_rates['brokerage_per_order'] == 0.0
    assert cfg.fee_rates['stt_sell_pct'] == 0.15
    assert cfg.lots == 2


# -------------------------------------------------------------------- store
def test_corrupt_book_is_refused_and_left_in_place(tmp_path):
    (tmp_path / 'book.json').write_text('{not json')
    with pytest.raises(CorruptBook):
        Store(tmp_path)
    assert (tmp_path / 'book.json').read_text() == '{not json'


def test_store_round_trip(tmp_path):
    s = Store(tmp_path)
    p = s.add_position({'status': 'open', 'x': 1})
    s.save()
    s2 = Store(tmp_path)
    assert s2.open_positions()[0]['id'] == p['id'] == 1
    assert s2.data['next_id'] == 2


# ------------------------------------------------------------------ compare
def test_compare_pairs_by_date_and_side(tmp_path):
    csv = tmp_path / 'v.csv'
    csv.write_text('Entry Date,Exit Date,Instrument,Entry Price,Exit Price,PnL,Qty,Remarks\n'
                   '26 Oct 2026,27 Oct 2026,"NIFTY 22,550 CE",163.71,178.2,"₹915",65,x\n'
                   '27 Oct 2026,28 Oct 2026,"NIFTY 22,500 PE",150.0,75.0,"-₹4,900",65,x\n',
                   encoding='utf-8')
    vol = compare.load_volrix(csv)
    assert vol[1]['pnl'] == -4900.0 and vol[0]['side'] == 'CE'
    paper = [{'entry_date': '2026-10-26', 'side': 'CE', 'symbol': 'S', 'entry_price': 164.0,
              'exit_price': 177.0, 'net_ex_brokerage': 800.0}]
    pairs, p_only, v_only = compare.pair(paper, vol)
    assert len(pairs) == 1 and not p_only and len(v_only) == 1
    assert 'gap' in compare.render(pairs, p_only, v_only).lower()


def _pair_rows(n, paper_exit=170.0, volrix_exit=170.0):
    """n matched CE trades on consecutive days from the gate's first day."""
    paper, vol = [], []
    for i in range(n):
        d = compare.GATE_START + timedelta(days=i)
        paper.append({'entry_date': d.isoformat(), 'side': 'CE', 'strike': 23000.0,
                      'entry_price': 150.0, 'exit_price': paper_exit})
        vol.append({'entry_date': d, 'side': 'CE', 'instrument': 'NIFTY 23,000 CE',
                    'entry_price': 150.0, 'exit_price': volrix_exit, 'pnl': 1300.0,
                    'remarks': 'Next day 09:25 exit'})
    return paper, vol


def test_gate_in_progress_below_30_pairs():
    g = compare.gate(*_pair_rows(29))
    assert g['verdict'] == 'IN PROGRESS' and g['pairs'] == 29


def test_gate_passes_exactly_at_the_cost_limit():
    g = compare.gate(*_pair_rows(30, paper_exit=168.0))
    assert g['mean_cost'] == 2.0 and g['verdict'] == 'PASS'


def test_gate_fails_just_above_the_cost_limit():
    assert compare.gate(*_pair_rows(30, paper_exit=167.99))['verdict'] == 'FAIL'


def test_gate_population_is_frozen_at_the_first_30():
    """A bad 31st trade cannot fail a finished test, and a good one cannot rescue it."""
    paper, vol = _pair_rows(31)
    paper[30]['exit_price'] = 100.0
    g = compare.gate(paper, vol)
    assert g['pairs'] == 30 and g['verdict'] == 'PASS'


def test_gate_ignores_trades_before_it_started():
    paper, vol = _pair_rows(1)
    early = compare.GATE_START - timedelta(days=1)
    vol.append(dict(vol[0], entry_date=early, pnl=50000.0))
    g = compare.gate(paper, vol)
    assert g['pairs'] == 1 and not g['missed']


def test_gate_one_missed_big_win_fails_immediately():
    paper, vol = _pair_rows(3)
    vol.append(dict(vol[0], side='PE', pnl=10000.0))
    g = compare.gate(paper, vol)
    assert g['verdict'] == 'FAIL' and len(g['missed']) == 1


def test_gate_a_win_just_under_the_line_is_not_a_miss():
    paper, vol = _pair_rows(3)
    vol.append(dict(vol[0], side='PE', pnl=9999.0))
    assert compare.gate(paper, vol)['verdict'] == 'IN PROGRESS'


def test_gate_a_big_win_after_the_window_closed_is_not_a_miss():
    paper, vol = _pair_rows(30)
    late = compare.GATE_START + timedelta(days=45)
    vol.append(dict(vol[0], entry_date=late, pnl=20000.0))
    assert compare.gate(paper, vol)['verdict'] == 'PASS'


def test_gate_excludes_the_end_of_backtest_row():
    """That row's exit is a mark on a still-open trade, never a fill."""
    paper, vol = _pair_rows(1)
    vol[0]['remarks'] = 'IN x OUT End of backtest'
    vol[0]['exit_price'] = 10.0
    g = compare.gate(paper, vol)
    assert g['pairs'] == 0 and len(g['excluded']) == 1


def test_gate_waits_for_an_open_paper_position():
    paper, vol = _pair_rows(1)
    paper[0]['exit_price'] = None
    assert compare.gate(paper, vol)['pairs'] == 0


def test_gate_keeps_a_different_strike_and_reports_it():
    paper, vol = _pair_rows(1)
    vol[0]['instrument'] = 'NIFTY 23,050 CE'
    g = compare.gate(paper, vol)
    assert g['pairs'] == 1 and len(g['strike_diff']) == 1


def test_gate_with_no_data_is_in_progress():
    g = compare.gate([], [])
    assert g['verdict'] == 'IN PROGRESS' and g['mean_cost'] is None
    assert 'IN PROGRESS' in compare.render_gate(g)


def test_volrix_reads_sept_dates_and_multiline_cells(tmp_path):
    """The page writes 28 Sept 2026 and stacks a time under the price."""
    csv = tmp_path / 'v.csv'
    csv.write_text('Entry Date,Instrument,Entry Price,Exit Price,PnL,Remarks\n'
                   '"28 Sept 2026\nMonday","NIFTY 23,000 PE","155.12\n13:00:00",'
                   '"113.13\n15:38:59","-₹2,748\nMax ₹572",End of backtest\n',
                   encoding='utf-8')
    v = compare.load_volrix(csv)[0]
    assert v['entry_date'] == date(2026, 9, 28)
    assert v['entry_price'] == 155.12 and v['exit_price'] == 113.13 and v['pnl'] == -2748.0
    assert v['remarks'] == 'End of backtest'


def test_compare_refuses_unknown_headers(tmp_path):
    csv = tmp_path / 'v.csv'
    csv.write_text('a,b,c\n1,2,3\n')
    with pytest.raises(compare.CompareError):
        compare.load_volrix(csv)
