"""The doubles must drive the REAL functions, not just satisfy their own tests.

A harness that is only exercised by its own unit tests proves nothing. Every
test here feeds `FakeBroker` into an unmodified `spread_monitor` function and
checks the real code produces the right answer — which is what makes the B10 /
B11 behavioural tests (next) trustworthy rather than circular.

Run:  cd Helper && python -m pytest bcs/tests/test_fakes_drive_real_code.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                        # noqa: E402
from bcs.tests.fakes import (FakeBroker, FakeClock, MemoryStore,  # noqa: E402
                             TelegramSpy, always_complete, partial,
                             partial_status, rejects, never_fills)


# ── Market data ──────────────────────────────────────────────────────────────

def test_get_spot_reads_the_fake():
    k = FakeBroker(spots={'NSE:TESTCO': 1234.5})
    assert sm.get_spot(k, 'NSE:TESTCO') == 1234.5


def test_a_renamed_spot_symbol_raises_the_way_kite_does():
    """B12's trigger. A KeyError, NOT an auth error — the two must stay
    distinguishable or the fix collapses back into one handler."""
    k = FakeBroker(spots={'NSE:TESTCO': 100.0})
    with pytest.raises(KeyError):
        sm.get_spot(k, 'NSE:RENAMEDCO')
    assert sm._is_auth_error(KeyError('NSE:RENAMEDCO')) is False


def test_an_auth_death_is_classified_as_auth():
    k = FakeBroker(spots={'NSE:TESTCO': 100.0})
    k.ltp_raises = Exception('Incorrect `api_key` or `access_token`.')
    with pytest.raises(Exception) as ei:
        sm.get_spot(k, 'NSE:TESTCO')
    assert sm._is_auth_error(ei.value) is True


def test_get_option_depth_unpacks_the_real_quote_shape():
    k = FakeBroker(books={'NFO:TESTCO26SEP100CE': {
        'bid': 10.05, 'bid_qty': 1400, 'ask': 10.30, 'ask_qty': 1400,
        'ltp': 10.20, 'prev_close': 9.80}})
    d = sm.get_option_depth(k, 'NFO', 'TESTCO26SEP100CE')
    assert (d['bid'], d['ask'], d['bid_qty']) == (10.05, 10.30, 1400)
    assert d['prev_close'] == 9.80


def test_a_one_sided_book_reports_zero_not_a_crash():
    """The NHPC shape: no bid at all. `leg_quote_reliable` must see it."""
    k = FakeBroker(books={'NFO:TESTCO26SEP100CE': {
        'bid': 0, 'ask': 1.40, 'ltp': 0.30}})
    d = sm.get_option_depth(k, 'NFO', 'TESTCO26SEP100CE')
    assert d['bid'] == 0
    ok, why = sm.leg_quote_reliable(d)
    assert ok is False and why


# ── Positions ────────────────────────────────────────────────────────────────

def test_get_net_position_reads_the_fake():
    k = FakeBroker(positions=[{'tradingsymbol': 'X', 'quantity': -700}])
    assert sm.get_net_position(k, 'X') == -700
    assert sm.get_net_position(k, 'ABSENT') == 0


def test_a_fill_moves_the_position():
    """The property the B10/B11 tests depend on. If fills did not move
    positions, those tests would assert against a hand-written premise
    instead of against what the orders actually did."""
    k = FakeBroker(positions=[{'tradingsymbol': 'X', 'quantity': -700}])
    k.place_order(variety='regular', exchange='NFO', tradingsymbol='X',
                  transaction_type='BUY', quantity=700, product='NRML',
                  order_type='LIMIT', price=10.0, tag='BCS_MON')
    assert k.net_qty('X') == 0, "a BUY of 700 against -700 must flatten it"


def test_a_double_buy_flips_the_leg_long():
    """The literal Feb-2026 ICICIBANK shape, reproduced by the fake."""
    k = FakeBroker(positions=[{'tradingsymbol': 'X', 'quantity': -700}])
    for _ in range(4):
        k.place_order(variety='regular', exchange='NFO', tradingsymbol='X',
                      transaction_type='BUY', quantity=700, product='NRML',
                      order_type='LIMIT', price=10.0, tag='BCS_MON')
    assert k.net_qty('X') == 2100, "four 700-lot buys against -700 = +2100"


# ── Orders ───────────────────────────────────────────────────────────────────

def test_place_limit_order_goes_through_the_real_function():
    k = FakeBroker()
    oid = sm.place_limit_order(k, 'NFO', 'X', 'BUY', 700, 10.023, dry_run=False)
    assert oid and k.placed[0]['tradingsymbol'] == 'X'
    assert k.placed[0]['price'] == sm.round_to_tick(10.023)
    assert k.placed[0]['tag'] == sm.ORDER_TAG
    assert k.placed[0]['order_type'] == FakeBroker.ORDER_TYPE_LIMIT


def test_dry_run_places_nothing():
    """Negative control for the test above."""
    k = FakeBroker()
    oid = sm.place_limit_order(k, 'NFO', 'X', 'BUY', 700, 10.0, dry_run=True)
    assert oid.startswith('DRY_')
    assert k.placed == [], "a dry run reached the broker"


def test_pending_orders_are_found_through_the_real_guard():
    k = FakeBroker(fill_policy=never_fills)
    sm.place_limit_order(k, 'NFO', 'X', 'BUY', 700, 10.0, dry_run=False)
    pend = sm._find_pending_orders(k, 'X', 'BUY')
    assert pend is not None and len(pend) == 1


def test_a_completed_order_is_not_pending():
    k = FakeBroker(fill_policy=always_complete)
    sm.place_limit_order(k, 'NFO', 'X', 'BUY', 700, 10.0, dry_run=False)
    assert sm._find_pending_orders(k, 'X', 'BUY') == []


def test_an_unreadable_order_book_returns_none_not_empty():
    """The distinction the guard exists for: "cannot tell" != "none live"."""
    k = FakeBroker()
    k.orders_raises = Exception('boom')
    assert sm._find_pending_orders(k, 'X', 'BUY') is None


def test_wait_for_fill_reads_a_completed_order(monkeypatch):
    clock = FakeClock().install(monkeypatch, sm)
    k = FakeBroker(fill_policy=always_complete)
    oid = sm.place_limit_order(k, 'NFO', 'X', 'BUY', 700, 10.0, dry_run=False)
    o = sm.wait_for_fill(k, oid, dry_run=False)
    assert o and o['status'] == 'COMPLETE' and o['filled_quantity'] == 700


def test_wait_for_fill_times_out_without_blocking(monkeypatch):
    """FakeClock earns its place here: this is a 30s wait in real time."""
    clock = FakeClock().install(monkeypatch, sm)
    k = FakeBroker(fill_policy=never_fills)
    oid = sm.place_limit_order(k, 'NFO', 'X', 'BUY', 700, 10.0, dry_run=False)
    assert sm.wait_for_fill(k, oid, dry_run=False) is None
    assert clock.now >= 1_600_000_000.0 + sm.ORDER_WAIT_SEC


def test_a_partial_fill_reports_its_residue(monkeypatch):
    """B10's input. 500 of 700 filled, 200 still short."""
    FakeClock().install(monkeypatch, sm)
    k = FakeBroker(positions=[{'tradingsymbol': 'X', 'quantity': -700}],
                   fill_policy=partial(0.714))
    oid = sm.place_limit_order(k, 'NFO', 'X', 'BUY', 700, 10.0, dry_run=False)
    o = sm.wait_for_fill(k, oid, dry_run=False)
    assert o['filled_quantity'] == 499
    assert k.net_qty('X') == -201, "the residue must be visible in positions"


def test_cancel_marks_the_order_cancelled():
    k = FakeBroker(fill_policy=never_fills)
    oid = sm.place_limit_order(k, 'NFO', 'X', 'BUY', 700, 10.0, dry_run=False)
    sm.cancel_order_safe(k, oid, dry_run=False)
    assert oid in k.cancelled
    assert sm._find_pending_orders(k, 'X', 'BUY') == []


# ── Store + Telegram ─────────────────────────────────────────────────────────

def test_memorystore_records_mutations():
    s = MemoryStore(trades=[{'id': 1, 'stock': 'TESTCO', 'status': 'open'}])
    assert len(s.get_open_trades()) == 1
    s.begin_close(1, reason='TP')
    assert s.get_open_trades() == [] and len(s.get_closing_trades()) == 1
    s.update_trade_exit(1, {'exit_price': 12.0})
    assert s.trades[0]['status'] == 'closed'
    assert s.called('update_trade_exit')


def test_telegram_spy_captures_without_sending(monkeypatch):
    spy = TelegramSpy().install(monkeypatch, sm)
    sm.send_telegram('hello TESTCO')
    assert spy.any('TESTCO')
