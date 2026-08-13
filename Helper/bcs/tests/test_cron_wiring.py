"""The guards must be wired into `monitor_all` — the mode that actually runs.

Every defect pinned here shared one shape: a guard was designed, implemented,
unit-tested and merged — into `monitor()`, the single-trade mode nobody runs in
production — while `monitor_all()`, the `--cron` entrypoint on the Pi, never
got it. The unit tests passed the whole time. `test_sl_spot_needs_two_polls`
asserted the constant and would have stayed green forever while the deployed
loop fired on one print.

So these tests assert WIRING, not behaviour: they read the source of the
production loop and require the guard to appear in it. That is deliberately a
cruder check than a behavioural test, because the failure being prevented is
not "the guard computes the wrong answer" — it is "the guard is not there".

Run:  cd Helper && python -m pytest bcs/tests/test_cron_wiring.py -v
"""
import inspect
import io
import re
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm       # noqa: E402


def _cron_source() -> str:
    return inspect.getsource(sm.monitor_all)


def test_spread_value_is_called_with_spot_in_the_cron_loop():
    """`spot=` is what arms the no-arbitrage floor inside get_spread_value.

    Guard class 2 (possibility): a tidy, two-sided, tight book can still quote
    an impossible price. Every other call site passed spot; the deployed loop
    did not, so the floor was inert in production.
    """
    src = _cron_source()
    calls = re.findall(r'get_spread_value\(([^)]*)\)', src)
    assert calls, "monitor_all no longer calls get_spread_value"
    for call in calls:
        assert 'spot' in call, (
            "get_spread_value in the cron loop must pass spot= or the "
            f"no-arbitrage floor cannot fire; got: get_spread_value({call})")


def test_sl_spot_is_debounced_in_the_cron_loop():
    """SL_SPOT runs at URGENT urgency and its final attempt pays uncapped.

    Both real-money losses were opening prints. A single-poll SL_SPOT in the
    deployed loop is the last single-print path to a live order.
    """
    src = _cron_source()
    assert 'SL_SPOT_CONFIRM_POLLS' in src, (
        "monitor_all must debounce SL_SPOT with SL_SPOT_CONFIRM_POLLS — the "
        "constant existing and being tested elsewhere is not wiring")
    assert re.search(r"bump_confirm\([^)]*,\s*'sl_spot'\)", src), (
        "monitor_all must bump the sl_spot confirm counter")


def test_malformed_records_are_isolated_and_alerted():
    src = _cron_source()
    assert '_malformed_reason' in src, (
        "one unreadable record must not halt exit checks for the whole book")
    assert '_alert_malformed_record' in src, (
        "a live position that is not being monitored must never be silent")


# ── The malformed-record guard itself ────────────────────────────────────

def _bcs_record(**over):
    rec = {'id': 1, 'stock': 'INFY', '_strategy': 'BCS', 'sl_spot': 1400.0,
           'spot_symbol': 'INFY', 'quantity': 400, 'target_spot': 1600.0,
           'sl_spread': 5.0, 'net_debit': 20.0,
           'long_symbol': 'INFY26AUG1500CE', 'short_symbol': 'INFY26AUG1600CE'}
    rec.update(over)
    return rec


def test_a_good_record_is_not_quarantined():
    assert sm._malformed_reason(_bcs_record()) is None


@pytest.mark.parametrize('field', [
    'id', 'stock', 'sl_spot', 'spot_symbol', 'quantity',
    'target_spot', 'sl_spread', 'net_debit', 'long_symbol', 'short_symbol',
])
def test_each_dereferenced_field_is_caught(field):
    """Exactly the fields the loop dereferences unconditionally."""
    bad = _bcs_record()
    bad.pop(field)
    reason = sm._malformed_reason(bad)
    assert reason and field in reason


def test_an_optional_field_does_not_quarantine_a_live_position():
    """A quarantine stops monitoring, so it must be reserved for records that
    genuinely cannot be read — not for every missing nicety."""
    rec = _bcs_record()
    rec.pop('sl_spread')          # BCS-required...
    assert sm._malformed_reason(rec)
    fh = {'id': 2, 'stock': 'INFY', '_strategy': 'FH', 'sl_spot': 1600.0,
          'spot_symbol': 'INFY', 'quantity': 400}
    assert sm._malformed_reason(fh) is None, \
        "FH has no target_spot/net_debit and must not be quarantined for it"


def test_malformed_alert_fires_once_per_record(monkeypatch):
    sent = []
    monkeypatch.setattr(sm, 'send_telegram', lambda m: sent.append(m))
    seen = set()
    bad = _bcs_record()
    bad.pop('sl_spot')
    for _ in range(5):
        sm._alert_malformed_record(bad, 'missing sl_spot', seen)
    assert len(sent) == 1, "a 5s poll must not Telegram thousands of times"
    assert 'NOT being monitored' in sent[0]


# ── Duplicate-order guard ────────────────────────────────────────────────

class _Kite:
    def __init__(self, orders=None, raises=False):
        self._orders = orders or []
        self._raises = raises

    def orders(self):
        if self._raises:
            raise RuntimeError('order book unreadable')
        return self._orders


def _order(status, oid='1'):
    return {'order_id': oid, 'tradingsymbol': 'INFY26AUG1600CE',
            'transaction_type': 'BUY', 'tag': sm.ORDER_TAG, 'status': status}


@pytest.mark.parametrize('status', [
    'OPEN', 'PENDING', 'TRIGGER PENDING',
    # The transient statuses Kite actually emits under load at 09:15. Every
    # one of these was invisible to the old live-status allowlist, so the
    # retry placed a SECOND order on the same leg — the Feb-2026 ICICIBANK
    # shape, at the same time of day.
    'PUT ORDER REQ RECEIVED', 'VALIDATION PENDING', 'OPEN PENDING',
    'MODIFY PENDING', 'CANCEL PENDING',
    # A status this code has never heard of must count as live.
    'SOME FUTURE STATUS',
])
def test_live_and_transient_orders_are_seen(status):
    kite = _Kite([_order(status)])
    found = sm._find_pending_orders(kite, 'INFY26AUG1600CE', 'BUY')
    assert found, f"{status!r} must be treated as a live order"


@pytest.mark.parametrize('status', ['COMPLETE', 'REJECTED', 'CANCELLED'])
def test_terminal_orders_are_ignored(status):
    kite = _Kite([_order(status)])
    assert sm._find_pending_orders(kite, 'INFY26AUG1600CE', 'BUY') == []


def test_unreadable_order_book_returns_unknown_not_empty():
    """The guard exists for the network-flake window. Returning [] on an API
    error stood it down at exactly the moment it was needed."""
    assert sm._find_pending_orders(_Kite(raises=True), 'X', 'BUY') is None


def test_close_leg_refuses_to_place_while_the_book_is_unreadable():
    """None must mean wait, never 'no live orders, go ahead'."""
    src = inspect.getsource(sm.close_leg)
    assert 'pending is None' in src, (
        "close_leg must distinguish 'no pending orders' from 'could not tell'")
    idx_none = src.index('pending is None')
    idx_place = src.index('place_limit_order')
    assert idx_none < idx_place, \
        "the unknown-book check must precede placing an order"


def test_adopted_pending_order_is_cancelled_before_a_replacement():
    """`wait_for_fill` returns None on TIMEOUT without cancelling — its own
    docstring says the caller should cancel. For its own orders close_leg does;
    for an ADOPTED order it did not, then placed a replacement. Two live orders
    on one leg, both fillable."""
    src = inspect.getsource(sm.close_leg)
    adopted = src[src.index('Found pending order'):src.index('Wait for depth')]
    assert 'cancel_order_safe' in adopted, \
        "an adopted order that timed out must be cancelled, not abandoned"
    assert '_order_final_state' in adopted, \
        "a fill can land in the cancel race and must not be lost"


def test_order_final_state_reports_unknown_rather_than_not_filled():
    assert sm._order_final_state(_Kite(raises=True), '1') is None


def test_terminal_status_set_is_exactly_the_dead_states():
    assert sm._TERMINAL_ORDER_STATUS == frozenset(
        {'COMPLETE', 'REJECTED', 'CANCELLED'})


def test_no_market_orders_anywhere_in_the_package():
    """Zerodha made `market_protection` mandatory for MARKET orders (Apr 2026).
    This package is safe from that only because it places LIMIT orders
    exclusively — assert that stays true."""
    src = io.open(Path(sm.__file__), encoding='utf-8').read()
    assert 'ORDER_TYPE_MARKET' not in src
    assert 'ORDER_TYPE_LIMIT' in src
