"""
Data bridge — flatten nested TechnicalData dict for scoring engine.

The kite_technical.py module returns nested dataclass dicts:
    {"rsi": {"current": 45.2, "previous": 47.1, "signal": "neutral"}, ...}

The technical_scores.py engine expects flat keys:
    {"rsi": 45.2, "dma_50": 1450.0, "macd_line": 12.5, ...}

This module bridges the gap.
"""

from typing import Optional


def flatten_technical_data(nested: dict) -> dict:
    """Convert nested TechnicalData dict to flat dict for scoring engine.

    Input (from TechnicalData.to_dict() / _dataclass_to_dict()):
        symbol, ltp, open, high, low, close, timestamp,
        week_52_high, week_52_low, pct_from_52w_high, pct_from_52w_low,
        rsi: {current, previous, signal},
        macd: {macd_line, signal_line, histogram, crossover},
        dma: {dma_50, dma_200, price_vs_50dma_pct, price_vs_200dma_pct,
              golden_cross, death_cross},
        bollinger: {upper, middle, lower, bandwidth_pct, position},
        volume: {current_volume, avg_5d, avg_20d, volume_ratio, trend},
        support_resistance: {supports, resistances}

    Output (flat dict for compute_technical_score()):
        rsi, price, dma_50, dma_200, dma_50_prev, dma_200_prev,
        macd_line, signal_line, histogram, histogram_prev,
        macd_line_prev, signal_line_prev,
        high_52w, low_52w, volume_5d_avg, volume_20d_avg, price_change_5d
    """
    if not nested:
        return {}

    flat = {}

    # Price
    flat['price'] = nested.get('ltp') or nested.get('close')
    flat['current_price'] = flat['price']

    # RSI
    rsi = nested.get('rsi')
    if isinstance(rsi, dict):
        flat['rsi'] = rsi.get('current')
    elif isinstance(rsi, (int, float)):
        flat['rsi'] = rsi

    # MACD
    macd = nested.get('macd')
    if isinstance(macd, dict):
        flat['macd_line'] = macd.get('macd_line')
        flat['signal_line'] = macd.get('signal_line')
        flat['histogram'] = macd.get('histogram')
        # No prev values in current dataclass — set None
        flat['histogram_prev'] = None
        flat['macd_line_prev'] = None
        flat['signal_line_prev'] = None
    elif nested.get('macd_line') is not None:
        # Already flat
        flat['macd_line'] = nested.get('macd_line')
        flat['signal_line'] = nested.get('signal_line') or nested.get('macd_signal')
        flat['histogram'] = nested.get('histogram')

    # DMA
    dma = nested.get('dma')
    if isinstance(dma, dict):
        flat['dma_50'] = dma.get('dma_50')
        flat['dma_200'] = dma.get('dma_200')
        flat['dma_50_prev'] = None  # Not in current dataclass
        flat['dma_200_prev'] = None
    else:
        flat['dma_50'] = nested.get('dma_50')
        flat['dma_200'] = nested.get('dma_200')

    # 52-week
    flat['high_52w'] = nested.get('week_52_high') or nested.get('high_52w')
    flat['low_52w'] = nested.get('week_52_low') or nested.get('low_52w')

    # Volume
    volume = nested.get('volume')
    if isinstance(volume, dict):
        flat['volume_5d_avg'] = volume.get('avg_5d')
        flat['volume_20d_avg'] = volume.get('avg_20d')
        flat['volume_trend'] = volume.get('trend')
    else:
        flat['volume_5d_avg'] = nested.get('volume_5d_avg')
        flat['volume_20d_avg'] = nested.get('volume_20d_avg')

    # Price change (not in current TechnicalData — compute if possible)
    flat['price_change_5d'] = nested.get('price_change_5d')

    # Support/Resistance (pass-through for display)
    sr = nested.get('support_resistance')
    if isinstance(sr, dict):
        flat['supports'] = sr.get('supports', [])
        flat['resistances'] = sr.get('resistances', [])

    # Bollinger (pass-through for display)
    bb = nested.get('bollinger')
    if isinstance(bb, dict):
        flat['bollinger_upper'] = bb.get('upper')
        flat['bollinger_middle'] = bb.get('middle')
        flat['bollinger_lower'] = bb.get('lower')
        flat['bollinger_position'] = bb.get('position')

    return flat
