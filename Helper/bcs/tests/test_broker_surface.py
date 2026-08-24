"""The Kite surface is pinned, and `FakeBroker` IS the specification.

This test exists instead of a broker-abstraction layer.

The reasoning (2026-08-24 design review): only ~4 lines in the whole tree touch
the order API, but the hard part of swapping brokers is not signatures — it is
Kite's *status vocabulary*. `_TERMINAL_ORDER_STATUS` enumerates the dead states
precisely because enumerating the LIVE ones is what let the Feb-2026 ICICIBANK
loss happen: a transient status nobody had heard of was treated as "not live",
a second order went in on the same leg, both filled, and the short leg flipped
long. An adapter written in a hurry would re-import that failure mode through a
translation table that silently maps an unknown Neo status onto "terminal".

So: no adapter now. Instead this test says exactly what a broker must provide,
fails loudly the day someone adds a call the fakes do not implement, and turns
the November Kotak Neo port into "make these seven methods real" with every
replay fixture re-running unchanged against it.

Run:  cd Helper && python -m pytest bcs/tests/test_broker_surface.py -v
"""
import ast
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm          # noqa: E402
from bcs.tests.fakes import FakeBroker        # noqa: E402

SOURCE = Path(sm.__file__)

#: Every method the monitor may call on the broker client. Adding to this set
#: is a deliberate act: it means the November Neo port has one more thing to
#: implement, and it means FakeBroker must grow a matching double.
EXPECTED_METHODS = {
    'set_access_token',   # auth
    'ltp',                # get_spot
    'quote',              # get_option_depth
    'positions',          # get_net_position, verify_positions, reconcile
    'orders',             # _find_pending_orders, wait_for_fill, _order_final_state
    'place_order',        # place_limit_order  <- the money line
    'cancel_order',       # cancel_order_safe
}

EXPECTED_CONSTANTS = {'VARIETY_REGULAR', 'ORDER_TYPE_LIMIT'}


def _kite_attribute_uses():
    """Every `kite.<attr>` in the module, split into calls and constants."""
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    methods, constants = set(), set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        base = node.value
        name = base.id if isinstance(base, ast.Name) else (
            base.attr if isinstance(base, ast.Attribute) else None)
        if name != 'kite':
            continue
        (constants if node.attr.isupper() else methods).add(node.attr)
    return methods, constants


def test_the_broker_surface_is_exactly_these_seven_calls():
    methods, _ = _kite_attribute_uses()
    assert methods == EXPECTED_METHODS, (
        "the set of methods called on the Kite client changed.\n"
        f"  added:   {sorted(methods - EXPECTED_METHODS)}\n"
        f"  removed: {sorted(EXPECTED_METHODS - methods)}\n"
        "If you added one: implement it on FakeBroker, add it here, and note "
        "that the Kotak Neo port now has one more method to provide.")


def test_the_broker_constants_are_exactly_these_two():
    _, constants = _kite_attribute_uses()
    assert constants == EXPECTED_CONSTANTS, (
        f"constants read off the client changed: {sorted(constants)}")


def test_fakebroker_implements_the_whole_surface():
    """The fake must be able to stand in for the real client completely.

    A missing method here means some replay fixture silently exercises less
    than it appears to — the harness would pass while the real path diverges.
    """
    missing = {m for m in EXPECTED_METHODS if not callable(
        getattr(FakeBroker, m, None))}
    assert not missing, f"FakeBroker is missing: {sorted(missing)}"
    for c in EXPECTED_CONSTANTS:
        assert hasattr(FakeBroker, c), f"FakeBroker is missing constant {c}"


def test_fakebroker_does_not_over_implement():
    """Negative control, and a live spec.

    If FakeBroker grows a method the production code never calls, the "this is
    the adapter specification" claim quietly stops being true — the Neo port
    would implement something nobody needs, and worse, a reader would believe
    the surface is bigger than it is.
    """
    public = {n for n in dir(FakeBroker)
              if not n.startswith('_') and callable(getattr(FakeBroker, n))}
    helpers = {'net_qty', 'orders_for'}      # assertion sugar, not broker API
    assert public - helpers == EXPECTED_METHODS, (
        f"FakeBroker exposes methods outside the pinned surface: "
        f"{sorted(public - helpers - EXPECTED_METHODS)}")


def test_the_surface_check_can_actually_fail():
    """Prove the AST walk sees calls at all, rather than returning empty.

    An empty set would compare equal to an empty expectation and this whole
    file would be decorative.
    """
    methods, constants = _kite_attribute_uses()
    assert methods, "the AST walk found no kite.<method> uses — it is broken"
    assert constants, "the AST walk found no kite.<CONST> uses — it is broken"
    assert 'place_order' in methods, (
        "place_order is THE money line; if the walk cannot see it, this test "
        "guards nothing")
