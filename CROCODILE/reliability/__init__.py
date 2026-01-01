"""
CROCODILE Reliability Testing Module
=====================================
Tests SuperTrend touch reliability to filter stocks.

Usage:
    python backtest_st_reliability.py          # Full backtest
    python backtest_st_reliability.py --symbol RELIANCE  # Single stock
"""

from .supertrend import calculate_supertrend, find_supertrend_touches, calculate_atr
from .backtest_st_reliability import backtest_stock, run_full_backtest

__all__ = [
    'calculate_supertrend',
    'find_supertrend_touches',
    'calculate_atr',
    'backtest_stock',
    'run_full_backtest'
]
