"""Magnet — ST-magnet naked option buying strategy.

Core idea: Supertrend acts as a magnet. When price approaches within 2-3%
of the ST line, buy a naked option (CALL if below, PUT if above) and hold
until price touches ST (target) or thesis fails (SL).

Usage:
    python -m playbook.magnet scan          # One-shot Chartink scan
    python -m playbook.magnet run           # Full loop (scan + monitor)
    python -m playbook.magnet list          # Show all trades
    python -m playbook.magnet status        # Dashboard
    python -m playbook.magnet close ID      # Manual close
"""

from .trade_store import MagnetStore, get_store

__all__ = ['MagnetStore', 'get_store']
