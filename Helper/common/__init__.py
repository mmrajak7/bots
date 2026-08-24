"""Primitives shared by every trade store, owned by none of them.

`filelock` used to live in `zebra/`, which was fine while zebra was its only
user. B6 needs the same lock in `bcs/`, `fallen_hero/` and `bear_put/` — and
`from zebra.filelock import ...` executes `zebra/__init__.py`, which imports
ZebraStore and its whole config chain. That would have made the LIVE-MONEY
monitor fail to import whenever anything in the PAPER system's package init
broke: exactly the coupling `feedback_guard_the_money_system_first` warns
about, in the wrong direction.

Nothing in this package may import a strategy package.
"""
