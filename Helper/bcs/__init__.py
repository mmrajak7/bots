"""BCS (Bull Call Spread) package — trade store, monitor, and execution."""

from .trade_store import TradeStore, get_store, load_trades, add_trade, update_trade_exit

__all__ = ['TradeStore', 'get_store', 'load_trades', 'add_trade', 'update_trade_exit']
