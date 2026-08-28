"""Fallen Hero (Reverse Jade Lizard) package — trade store and management."""

from .trade_store import (FallenHeroStore, get_store, load_trades, add_trade,
                          update_trade_exit, bound_fh_exit,
                          exit_is_approximate)

#: `exit_is_approximate` is exported so a reader of this book cannot present a
#: clamped or unpriced P&L as exact without having gone out of its way to. Every
#: consumer already imports from this package (`bcs/journal_report.py`, the
#: portfolio dashboard); a predicate they have to dig into a submodule for is a
#: predicate they will not call.
__all__ = ['FallenHeroStore', 'get_store', 'load_trades', 'add_trade',
           'update_trade_exit', 'bound_fh_exit', 'exit_is_approximate']
