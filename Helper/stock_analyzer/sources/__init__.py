"""
Stock Analyzer — Data Sources

Provides unified access to fundamental + technical + sentiment data:
  - screener.py    : Screener.in fundamentals (quarterly, annual, ratios, etc.)
  - kite_technical.py : Kite OHLC + computed technical indicators
  - trendlyne.py   : Trendlyne SWOT / analyst consensus (best-effort)
  - cache.py       : Transparent file-based JSON cache for all sources

Each source returns a typed dataclass. Failures in one source never
affect others — callers get None for any section that fails.
"""

from .screener import ScreenerData, fetch_screener_data
from .kite_technical import TechnicalData, fetch_technical_data
from .trendlyne import TrendlyneData, fetch_trendlyne_data
from .cache import CacheManager

__all__ = [
    'ScreenerData', 'fetch_screener_data',
    'TechnicalData', 'fetch_technical_data',
    'TrendlyneData', 'fetch_trendlyne_data',
    'CacheManager',
]
