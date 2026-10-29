import json
from datetime import date

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
    assert cfg.fee_rates['stt_sell_pct'] == 0.10
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


def test_compare_refuses_unknown_headers(tmp_path):
    csv = tmp_path / 'v.csv'
    csv.write_text('a,b,c\n1,2,3\n')
    with pytest.raises(compare.CompareError):
        compare.load_volrix(csv)
