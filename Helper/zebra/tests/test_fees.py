"""Charges, stamped on every paper exit.

The two-month paper run answers ONE question: does this strategy clear its own
costs? The measured baseline is a median BCS trade of +0.90% gross and -0.79%
NET, so a cohort scored on `pnl_pct` alone looks fine while losing money.

The trap these tests exist for is UNDER-counting. A fee model that quietly
reports less than the truth makes the go-live decision on a number that was
never real, and the first cut did exactly that twice.
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg          # noqa: E402
from zebra import fees                   # noqa: E402


FULL = {
    'id': 1, 'stock': 'X', 'structure': 'bcs', 'quantity': 1000,
    'debit': 5.0, 'long_symbol': 'XCE', 'short_symbol': 'YCE',
    'long_ask_entry': 20.0, 'long_bid_entry': 19.8, 'long_mid_entry': 19.9,
    'short_bid_entry': 15.0, 'short_ask_entry': 15.2, 'short_mid_entry': 15.1,
    'exit_legs': {'long': {'price': 25.0}, 'short': {'price': 18.0}},
}


def test_a_two_leg_round_trip_is_four_orders(monkeypatch):
    est = fees.round_trip_for_trade(FULL, exit_debit=7.0)
    assert est['orders'] == 4, "a leg that opens and closes is two orders"
    assert est['basis'] == 'full'


def test_brokerage_is_charged_even_when_prices_are_missing():
    """THE BUG THIS FILE EXISTS FOR. The first cut skipped an order whenever a
    leg price was missing and reported a median of Rs 47 across the book —
    BELOW the Rs 80 fixed brokerage floor, an impossible number that would have
    flattered the cohort's net P&L."""
    bare = {'id': 2, 'stock': 'X', 'structure': 'zebra', 'quantity': 1000,
            'debit': 10.0, 'long_symbol': 'A', 'short_symbol': 'B'}
    est = fees.round_trip_for_trade(bare, exit_debit=5.0)
    floor = 4 * cfg.FEE_RATES['brokerage_per_order']
    assert est['orders'] == 4
    assert est['brokerage'] >= floor
    assert est['total'] > floor, "GST on brokerage was dropped too"


def test_an_uncostable_record_says_so_rather_than_reading_as_complete():
    """STT is levied per leg on that leg's premium. With no leg prices it is
    not recoverable, and a total that omits it is a FLOOR, not an estimate —
    179 of 215 closed zebra records are in exactly this state."""
    bare = {'id': 3, 'stock': 'X', 'structure': 'zebra', 'quantity': 1000,
            'debit': 10.0, 'long_symbol': 'A', 'short_symbol': 'B'}
    est = fees.round_trip_for_trade(bare, exit_debit=5.0)
    assert est['basis'] == 'brokerage_only'
    assert est['stt'] == 0
    assert 'FLOOR' in est['note']


def test_stt_lands_on_the_sell_side_only():
    """The big asymmetry, and the reason per-leg prices matter at all."""
    buys = [{'side': 'BUY', 'price': 100.0, 'qty': 100}]
    sells = [{'side': 'SELL', 'price': 100.0, 'qty': 100}]
    assert fees.estimate(buys)['stt'] == 0
    assert fees.estimate(sells)['stt'] > 0
    assert fees.estimate(buys)['stamp'] > 0      # ...and stamp duty is the mirror
    assert fees.estimate(sells)['stamp'] == 0


def test_gst_never_rides_on_stt_or_stamp():
    """They are taxes in their own right. Taxing a tax overstates the drag and
    would make the strategy look worse than it is — an error in the SAFE
    direction is still an error when it drives a go-live decision."""
    o = [{'side': 'SELL', 'price': 1000.0, 'qty': 1000}]
    est = fees.estimate(o)
    r = cfg.FEE_RATES
    expected = (est['brokerage'] + est['exchange'] + est['sebi']) * r['gst_pct'] / 100.0
    assert abs(est['gst'] - expected) < 0.01
    assert est['stt'] > est['gst'], "fixture too small to prove the point"


def test_a_zebra_charges_two_longs_per_short():
    """The back ratio holds 2x the long strike, so its premium turnover — and
    therefore its charges — are not the same as a vertical of equal quantity."""
    z = dict(FULL, structure='zebra')
    v = dict(FULL, structure='bcs')
    assert fees.round_trip_for_trade(z, 7.0)['total'] > \
        fees.round_trip_for_trade(v, 7.0)['total']


def test_costing_never_raises_and_never_pays_you():
    """This runs inside the exit path: a costing error must not be able to stop
    a position closing, nor to manufacture a profit."""
    assert fees.estimate([])['total'] == 0
    junk = [{'side': 'SELL', 'price': 'oops', 'qty': 1}]
    assert fees.estimate(junk)['total'] == 0
    assert fees.round_trip_for_trade({'id': 4}, None)['total'] >= 0


def test_the_model_version_is_stamped():
    """Rates change. A stored figure whose model differs from today's must be
    recomputed from the leg prices, never compared."""
    est = fees.round_trip_for_trade(FULL, 7.0)
    assert est['model'] == fees.MODEL_VERSION
    assert set(est['rates']) >= {'stt_sell_pct', 'brokerage_per_order'}


def test_the_exit_stamps_net_beside_gross_never_instead(tmp_path, monkeypatch):
    """`pnl` keeps its meaning so every earlier record stays comparable.
    Replacing it would mix two definitions — the mistake `band_basis` exists to
    prevent on the magnet stat."""
    from zebra.trade_store import ZebraStore
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'z.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'z.lock')
    s = ZebraStore(config={})
    s._load_local()
    s.add_signal({'stock': 'X', 'timeframe': 'weekly', 'direction': 'CE',
                  'st_value': 110.0, 'st_direction': 'UP',
                  'signal_price': 100.0, 'signal_gap_pct': 4.0})
    s.mark_entered(1, {'long_strike': 96.0, 'short_strike': 104.0,
                       'long_symbol': 'A', 'short_symbol': 'B', 'debit': 5.0,
                       'lot_size': 500, 'lots': 1, 'expiry': '2026-09-24',
                       'long_ask_entry': 20.0, 'short_bid_entry': 15.0})
    t = s.mark_exited(1, exit_spot=108.0, exit_debit=8.0, reason='tp')
    assert t['pnl'] > 0 and t['pnl_net'] < t['pnl'], "charges were not deducted"
    assert t['fees']['total'] > 0
    assert t['pnl_pct'] == round((8.0 - 5.0) / 5.0 * 100, 2), \
        "gross P&L changed meaning"


def test_the_entry_leg_book_is_persisted(tmp_path, monkeypatch):
    """WHY 179 RECORDS CANNOT BE COSTED. The zebra path stored only the net
    debit; STT is levied per leg, so those trades are un-costable forever.
    The analyzer always had these prices — nothing ever wrote them down."""
    from zebra.trade_store import ZebraStore
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'z.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'z.lock')
    s = ZebraStore(config={})
    s._load_local()
    s.add_signal({'stock': 'X', 'timeframe': 'weekly', 'direction': 'CE',
                  'st_value': 110.0, 'st_direction': 'UP',
                  'signal_price': 100.0, 'signal_gap_pct': 4.0})
    t = s.mark_entered(1, {'long_strike': 96.0, 'short_strike': 104.0,
                           'long_symbol': 'A', 'short_symbol': 'B',
                           'debit': 5.0, 'lot_size': 500, 'lots': 1,
                           'expiry': '2026-09-24',
                           'long_bid_entry': 19.8, 'long_ask_entry': 20.0,
                           'short_bid_entry': 15.0, 'short_ask_entry': 15.2})
    for k in ('long_bid_entry', 'long_ask_entry', 'short_bid_entry',
              'short_ask_entry'):
        assert k in t, f"{k} was dropped — the trade is un-costable"


def test_the_live_entry_path_hands_over_its_leg_book():
    """Wiring. A field the live entry path never passes is a field that is
    never stored, however well `_apply_entry` handles it.

    Retargeted 2026-08-27: this used to read `best.get(...)` out of the
    back-ratio entry branch in `monitor.py`. That structure was
    decommissioned and the branch removed; the surviving entry path is
    `mark_entered_bcs`, so the property is pinned where it now lives.
    179 old zebra records are permanently un-costable for exactly this
    omission, which is why it is pinned at all."""
    src = (HELPER / 'zebra' / 'trade_store.py').read_text(encoding='utf-8')
    assert "'long_ask_entry': bcs.get('long_ask')" in src
    assert "'long_bid_entry': bcs.get('long_bid')" in src
    assert "'short_ask_entry': bcs.get('short_ask')" in src
    assert "'short_bid_entry': bcs.get('short_bid')" in src
    mon = (HELPER / 'zebra' / 'monitor.py').read_text(encoding='utf-8')
    assert "best.get('long_ask')" not in mon, \
        'the retired back-ratio entry path is back'
