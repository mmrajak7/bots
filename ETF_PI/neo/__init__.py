"""
neo — Self-contained Kotak Neo order execution package.

Bridges option_buy.py signals to live orders via Kotak Neo API v2.
All Neo-related code, config, and state live inside this package.
"""

from neo.orders import NeoTrader

__all__ = ['NeoTrader']
