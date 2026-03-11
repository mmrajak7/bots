"""
Technical scoring engine — pure computation, zero I/O.

Takes a TechnicalData dict (from kite_technical.py source) and computes
individual indicator scores plus a weighted composite.

Expected technical_data structure (dict with these keys):
  rsi: float                — 14-period RSI (0-100)
  price: float              — Current/latest close price
  dma_50: float             — 50-day moving average
  dma_200: float            — 200-day moving average
  dma_50_prev: float|None   — DMA_50 from ~5 sessions ago (for crossover detection)
  dma_200_prev: float|None  — DMA_200 from ~5 sessions ago (for crossover detection)
  macd_line: float          — MACD line value
  signal_line: float        — MACD signal line value
  histogram: float          — MACD histogram (macd_line - signal_line)
  histogram_prev: float|None — Previous histogram value (for direction)
  macd_line_prev: float|None — Previous MACD for crossover detection
  signal_line_prev: float|None — Previous signal for crossover detection
  high_52w: float           — 52-week high
  low_52w: float            — 52-week low
  volume_5d_avg: float      — 5-day average volume
  volume_20d_avg: float     — 20-day average volume
  price_change_5d: float    — 5-day price change in percent
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe_float(val: Any, default: float = 0.0) -> float:
    """Convert to float, returning default on failure."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    """Clamp a value to [lo, hi]."""
    return max(lo, min(hi, value))


# ── 4a. RSI Scoring ─────────────────────────────────────────────────────────

def score_rsi(rsi: Optional[float]) -> dict:
    """Score the Relative Strength Index (0-100 scale).

    RSI measures momentum — oversold (<30) suggests buying pressure may build,
    overbought (>70) suggests selling pressure may build.

    Scoring:
      <20:  95 (deeply oversold — strong contrarian buy)
      20-30: 80 (oversold — buy signal)
      30-50: 55 (neutral-bearish)
      50-70: 50 (neutral-bullish)
      70-80: 25 (overbought — caution)
      >80:  10 (deeply overbought — sell signal)

    Args:
        rsi: RSI value (0-100). None returns neutral score.

    Returns:
        {"score": 0-100, "signal": str, "value": float|None}
    """
    if rsi is None:
        return {
            'score': 50,
            'signal': "RSI unavailable — neutral default",
            'value': None,
        }

    rsi = _safe_float(rsi, 50.0)

    if rsi < 20:
        score = 95
        signal = "Deeply oversold — strong buy signal"
    elif rsi < 30:
        score = 80
        signal = "Oversold — buy signal"
    elif rsi < 50:
        score = 55
        signal = "Neutral-bearish"
    elif rsi < 70:
        score = 50
        signal = "Neutral-bullish"
    elif rsi < 80:
        score = 25
        signal = "Overbought — caution"
    else:
        score = 10
        signal = "Deeply overbought — sell signal"

    return {
        'score': score,
        'signal': signal,
        'value': round(rsi, 2),
    }


# ── 4b. DMA Scoring ─────────────────────────────────────────────────────────

def score_dma(price: Optional[float], dma_50: Optional[float],
              dma_200: Optional[float],
              dma_50_prev: Optional[float] = None,
              dma_200_prev: Optional[float] = None) -> dict:
    """Score price position relative to moving averages.

    The 50/200-day moving average relationship is a widely-followed trend
    indicator. Golden cross (50 > 200) is bullish; death cross (50 < 200)
    is bearish. Price position relative to both gives trend context.

    Scoring:
      Price > DMA_50 > DMA_200 (golden alignment):  90
      Golden cross detected (recent 50 > 200):      85
      Price > DMA_200, below DMA_50 (mild pullback): 65
      Price < DMA_200 but above DMA_50:              45
      Death cross detected (recent 50 < 200):        20
      Price < DMA_200 < DMA_50 (death cross zone):   15

    Args:
        price: Current stock price.
        dma_50: 50-day moving average.
        dma_200: 200-day moving average.
        dma_50_prev: DMA_50 from ~5 sessions ago (for crossover detection).
        dma_200_prev: DMA_200 from ~5 sessions ago (for crossover detection).

    Returns:
        {"score": 0-100, "signal": str, "alignment": str}
    """
    if price is None or dma_50 is None or dma_200 is None:
        return {
            'score': 50,
            'signal': "DMA data unavailable — neutral default",
            'alignment': "unknown",
        }

    price = _safe_float(price)
    dma_50 = _safe_float(dma_50)
    dma_200 = _safe_float(dma_200)

    if dma_200 <= 0 or dma_50 <= 0:
        return {
            'score': 50,
            'signal': "Invalid DMA values — neutral default",
            'alignment': "unknown",
        }

    # Check for recent crossovers (if prev data available)
    golden_cross = False
    death_cross = False
    if dma_50_prev is not None and dma_200_prev is not None:
        dma_50_prev = _safe_float(dma_50_prev)
        dma_200_prev = _safe_float(dma_200_prev)
        if dma_50_prev > 0 and dma_200_prev > 0:
            # Golden cross: 50 was below 200, now above
            if dma_50_prev <= dma_200_prev and dma_50 > dma_200:
                golden_cross = True
            # Death cross: 50 was above 200, now below
            if dma_50_prev >= dma_200_prev and dma_50 < dma_200:
                death_cross = True

    # Determine alignment and score
    if golden_cross:
        score = 85
        signal = "Golden cross detected — bullish crossover"
        alignment = "golden_cross"
    elif death_cross:
        score = 20
        signal = "Death cross detected — bearish crossover"
        alignment = "death_cross"
    elif price > dma_50 and dma_50 > dma_200:
        score = 90
        signal = "Golden alignment — price above both rising DMAs"
        alignment = "bullish"
    elif price > dma_200 and price <= dma_50:
        score = 65
        signal = "Above DMA_200 but below DMA_50 — mild pullback in uptrend"
        alignment = "pullback"
    elif price > dma_50 and dma_50 <= dma_200:
        # Price above 50 but 50 still below 200 — early recovery
        score = 45
        signal = "Price recovering above DMA_50 but DMA_50 still below DMA_200"
        alignment = "early_recovery"
    elif price < dma_200 and price > dma_50:
        # Below 200 but above 50 — confused
        score = 40
        signal = "Below DMA_200, above DMA_50 — mixed signals"
        alignment = "mixed"
    elif price < dma_200 and dma_50 < dma_200:
        score = 15
        signal = "Death cross zone — price below both declining DMAs"
        alignment = "bearish"
    else:
        score = 50
        signal = "Neutral DMA positioning"
        alignment = "neutral"

    return {
        'score': score,
        'signal': signal,
        'alignment': alignment,
    }


# ── 4c. MACD Scoring ────────────────────────────────────────────────────────

def score_macd(macd_line: Optional[float], signal_line: Optional[float],
               histogram: Optional[float],
               histogram_prev: Optional[float] = None,
               macd_line_prev: Optional[float] = None,
               signal_line_prev: Optional[float] = None) -> dict:
    """Score MACD momentum indicator.

    MACD measures trend momentum via the convergence/divergence of two EMAs.
    The histogram (MACD - Signal) shows acceleration of momentum.

    Scoring:
      MACD > 0, histogram expanding:     85 (bullish momentum building)
      Bullish crossover:                  80 (MACD crossed above signal)
      MACD > 0, histogram shrinking:     60 (weakening bullish)
      MACD < 0, histogram less negative:  45 (bearish weakening — potential reversal)
      Bearish crossover:                  20 (MACD crossed below signal)
      MACD < 0, histogram expanding neg:  15 (bearish momentum building)

    Args:
        macd_line: Current MACD line value.
        signal_line: Current signal line value.
        histogram: Current histogram value (macd - signal).
        histogram_prev: Previous histogram for direction detection.
        macd_line_prev: Previous MACD for crossover detection.
        signal_line_prev: Previous signal for crossover detection.

    Returns:
        {"score": 0-100, "signal": str, "momentum": str}
    """
    if macd_line is None or signal_line is None or histogram is None:
        return {
            'score': 50,
            'signal': "MACD data unavailable — neutral default",
            'momentum': "unknown",
        }

    macd_line = _safe_float(macd_line)
    signal_line = _safe_float(signal_line)
    histogram = _safe_float(histogram)

    # Check for crossovers
    bullish_crossover = False
    bearish_crossover = False
    if macd_line_prev is not None and signal_line_prev is not None:
        macd_prev = _safe_float(macd_line_prev)
        sig_prev = _safe_float(signal_line_prev)
        # Bullish: MACD was below signal, now above
        if macd_prev <= sig_prev and macd_line > signal_line:
            bullish_crossover = True
        # Bearish: MACD was above signal, now below
        if macd_prev >= sig_prev and macd_line < signal_line:
            bearish_crossover = True

    # Determine histogram direction
    hist_expanding = False
    hist_shrinking = False
    if histogram_prev is not None:
        hist_prev = _safe_float(histogram_prev)
        if histogram > 0 and histogram > hist_prev:
            hist_expanding = True  # Bullish expansion
        elif histogram > 0 and histogram < hist_prev:
            hist_shrinking = True  # Bullish weakening
        elif histogram < 0 and histogram < hist_prev:
            hist_expanding = True  # Bearish expansion
        elif histogram < 0 and histogram > hist_prev:
            hist_shrinking = True  # Bearish weakening (less negative)

    # Score
    if bullish_crossover:
        score = 80
        signal = "Bullish crossover — MACD crossed above signal"
        momentum = "bullish_crossover"
    elif bearish_crossover:
        score = 20
        signal = "Bearish crossover — MACD crossed below signal"
        momentum = "bearish_crossover"
    elif macd_line > 0 and histogram > 0 and hist_expanding:
        score = 85
        signal = "Bullish momentum building — MACD positive, histogram expanding"
        momentum = "strong_bullish"
    elif macd_line > 0 and histogram > 0 and hist_shrinking:
        score = 60
        signal = "Weakening bullish — MACD positive but histogram shrinking"
        momentum = "weakening_bullish"
    elif macd_line > 0 and histogram > 0:
        # No prev data for direction, but still bullish
        score = 70
        signal = "Bullish — MACD and histogram positive"
        momentum = "bullish"
    elif macd_line < 0 and histogram < 0 and hist_expanding:
        score = 15
        signal = "Bearish momentum building — MACD negative, histogram expanding"
        momentum = "strong_bearish"
    elif macd_line < 0 and histogram < 0 and hist_shrinking:
        score = 45
        signal = "Bearish weakening — MACD negative but histogram less negative"
        momentum = "weakening_bearish"
    elif macd_line < 0 and histogram < 0:
        score = 30
        signal = "Bearish — MACD and histogram negative"
        momentum = "bearish"
    elif macd_line > 0 and histogram <= 0:
        score = 50
        signal = "MACD positive but histogram turning — watch for reversal"
        momentum = "caution_bullish"
    elif macd_line < 0 and histogram >= 0:
        score = 45
        signal = "MACD negative but histogram turning positive — potential recovery"
        momentum = "caution_bearish"
    else:
        score = 50
        signal = "Neutral MACD"
        momentum = "neutral"

    return {
        'score': score,
        'signal': signal,
        'momentum': momentum,
    }


# ── 4d. 52-Week Position Scoring ────────────────────────────────────────────

def score_52w_position(price: Optional[float], high_52w: Optional[float],
                       low_52w: Optional[float]) -> dict:
    """Score the stock's position within its 52-week range.

    Where a stock sits relative to its 52W high/low gives context on
    whether it's extended or pulled back. Neither extreme is inherently
    good — context matters (hence moderate scores for near-high).

    Scoring:
      Within 5% of 52W high:     75 (strong but limited upside)
      5-20% off high:            65 (healthy pullback)
      20-40% off high:           45 (significant correction)
      40-60% off high:           30 (deep correction)
      Within 10% of 52W low:     20 (near bottom — high risk or deep value)

    Args:
        price: Current price.
        high_52w: 52-week high.
        low_52w: 52-week low.

    Returns:
        {"score": 0-100, "signal": str, "pct_from_high": float,
         "pct_from_low": float, "range_position": float}
    """
    if price is None or high_52w is None or low_52w is None:
        return {
            'score': 50,
            'signal': "52-week data unavailable — neutral default",
            'pct_from_high': None,
            'pct_from_low': None,
            'range_position': None,
        }

    price = _safe_float(price)
    high_52w = _safe_float(high_52w)
    low_52w = _safe_float(low_52w)

    if high_52w <= 0 or low_52w <= 0 or high_52w <= low_52w:
        return {
            'score': 50,
            'signal': "Invalid 52-week data — neutral default",
            'pct_from_high': None,
            'pct_from_low': None,
            'range_position': None,
        }

    pct_from_high = ((high_52w - price) / high_52w) * 100
    pct_from_low = ((price - low_52w) / low_52w) * 100
    range_width = high_52w - low_52w
    range_position = ((price - low_52w) / range_width) * 100  # 0 = at low, 100 = at high

    if pct_from_high <= 5:
        score = 75
        signal = f"Near 52W high ({pct_from_high:.1f}% off) — strong but limited upside"
    elif pct_from_high <= 20:
        score = 65
        signal = f"Healthy pullback ({pct_from_high:.1f}% off 52W high)"
    elif pct_from_high <= 40:
        score = 45
        signal = f"Significant correction ({pct_from_high:.1f}% off 52W high)"
    elif pct_from_high <= 60:
        score = 30
        signal = f"Deep correction ({pct_from_high:.1f}% off 52W high) — may be opportunity or trap"
    else:
        score = 20
        signal = f"Near 52W low ({pct_from_high:.1f}% off high) — high risk or deep value"

    # Override if very close to 52W low
    pct_above_low = ((price - low_52w) / low_52w) * 100
    if pct_above_low <= 10:
        score = 20
        signal = f"Within 10% of 52W low — high risk or deep value"

    return {
        'score': score,
        'signal': signal,
        'pct_from_high': round(pct_from_high, 2),
        'pct_from_low': round(pct_from_low, 2),
        'range_position': round(range_position, 2),
    }


# ── 4e. Volume Scoring ──────────────────────────────────────────────────────

def score_volume(volume_5d_avg: Optional[float], volume_20d_avg: Optional[float],
                 price_change_5d: Optional[float]) -> dict:
    """Score volume-price relationship for accumulation/distribution.

    Volume confirms price moves. Rising price with rising volume = conviction.
    Rising price with falling volume = weak rally. Volume divergences often
    precede reversals.

    Scoring:
      Rising volume + rising price:  85 (accumulation)
      Falling volume + rising price: 55 (weak rally — caution)
      Falling volume + falling price: 50 (apathy — wait for catalyst)
      Rising volume + falling price: 25 (distribution — bearish)

    Args:
        volume_5d_avg: 5-day average volume.
        volume_20d_avg: 20-day average volume.
        price_change_5d: 5-day price change in percent (positive = up).

    Returns:
        {"score": 0-100, "signal": str, "volume_trend": str,
         "volume_ratio": float|None}
    """
    if volume_5d_avg is None or volume_20d_avg is None or price_change_5d is None:
        return {
            'score': 50,
            'signal': "Volume data unavailable — neutral default",
            'volume_trend': "unknown",
            'volume_ratio': None,
        }

    vol_5d = _safe_float(volume_5d_avg)
    vol_20d = _safe_float(volume_20d_avg)
    price_chg = _safe_float(price_change_5d)

    if vol_20d <= 0:
        return {
            'score': 50,
            'signal': "Volume baseline too low — neutral default",
            'volume_trend': "unknown",
            'volume_ratio': None,
        }

    volume_ratio = vol_5d / vol_20d
    volume_rising = volume_ratio > 1.05  # 5% threshold for "rising"
    volume_falling = volume_ratio < 0.95
    price_rising = price_chg > 0.5  # 0.5% threshold for "rising"
    price_falling = price_chg < -0.5

    if volume_rising and price_rising:
        score = 85
        signal = "Accumulation — rising volume confirms price rise"
        volume_trend = "accumulation"
    elif volume_rising and price_falling:
        score = 25
        signal = "Distribution — rising volume with falling price (bearish)"
        volume_trend = "distribution"
    elif volume_falling and price_rising:
        score = 55
        signal = "Weak rally — price rising on declining volume (caution)"
        volume_trend = "weak_rally"
    elif volume_falling and price_falling:
        score = 50
        signal = "Apathy — falling volume and price (wait for catalyst)"
        volume_trend = "apathy"
    else:
        # Neutral zone — volume and/or price not decisively moving
        score = 50
        signal = "Neutral volume-price action"
        volume_trend = "neutral"

    # Bonus/penalty for extreme volume spikes
    if volume_ratio > 2.0 and price_rising:
        score = min(95, score + 10)
        signal = "Strong accumulation — volume spike with rising price"
        volume_trend = "strong_accumulation"
    elif volume_ratio > 2.0 and price_falling:
        score = max(10, score - 10)
        signal = "Heavy distribution — volume spike with falling price (very bearish)"
        volume_trend = "heavy_distribution"

    return {
        'score': score,
        'signal': signal,
        'volume_trend': volume_trend,
        'volume_ratio': round(volume_ratio, 2),
    }


# ── 4f. Composite Technical Score ────────────────────────────────────────────

def compute_technical_score(technical_data: dict, config: dict) -> dict:
    """Compute a weighted composite technical score (0-100).

    Weights:
      RSI:          20%
      DMA:          25%
      MACD:         15%
      52W Position: 20%
      Volume:       20%

    Missing indicators are excluded and remaining weights are renormalized.

    Args:
        technical_data: Dict with RSI, DMA, MACD, 52W, volume fields.
        config: Config dict (currently unused but reserved for weight overrides).

    Returns:
        {"score": 0-100, "sub_scores": dict, "signals": list[str]}
    """
    # Default weights
    weights = {
        'rsi': 0.20,
        'dma': 0.25,
        'macd': 0.15,
        '52w_position': 0.20,
        'volume': 0.20,
    }

    sub_scores: Dict[str, dict] = {}
    signals: List[str] = []
    weighted_sum = 0.0
    total_weight = 0.0

    # ── RSI ──────────────────────────────────────────────────────────────

    rsi_result = score_rsi(technical_data.get('rsi'))
    sub_scores['rsi'] = rsi_result
    if rsi_result.get('value') is not None:
        w = weights['rsi']
        weighted_sum += rsi_result['score'] * w
        total_weight += w
        signals.append(f"RSI ({rsi_result['value']:.0f}): {rsi_result['signal']}")

    # ── DMA ──────────────────────────────────────────────────────────────

    dma_result = score_dma(
        price=technical_data.get('price'),
        dma_50=technical_data.get('dma_50'),
        dma_200=technical_data.get('dma_200'),
        dma_50_prev=technical_data.get('dma_50_prev'),
        dma_200_prev=technical_data.get('dma_200_prev'),
    )
    sub_scores['dma'] = dma_result
    if dma_result.get('alignment') != 'unknown':
        w = weights['dma']
        weighted_sum += dma_result['score'] * w
        total_weight += w
        signals.append(f"DMA: {dma_result['signal']}")

    # ── MACD ─────────────────────────────────────────────────────────────

    macd_result = score_macd(
        macd_line=technical_data.get('macd_line'),
        signal_line=technical_data.get('signal_line'),
        histogram=technical_data.get('histogram'),
        histogram_prev=technical_data.get('histogram_prev'),
        macd_line_prev=technical_data.get('macd_line_prev'),
        signal_line_prev=technical_data.get('signal_line_prev'),
    )
    sub_scores['macd'] = macd_result
    if macd_result.get('momentum') != 'unknown':
        w = weights['macd']
        weighted_sum += macd_result['score'] * w
        total_weight += w
        signals.append(f"MACD: {macd_result['signal']}")

    # ── 52-Week Position ─────────────────────────────────────────────────

    pos_52w_result = score_52w_position(
        price=technical_data.get('price'),
        high_52w=technical_data.get('high_52w'),
        low_52w=technical_data.get('low_52w'),
    )
    sub_scores['52w_position'] = pos_52w_result
    if pos_52w_result.get('range_position') is not None:
        w = weights['52w_position']
        weighted_sum += pos_52w_result['score'] * w
        total_weight += w
        signals.append(f"52W: {pos_52w_result['signal']}")

    # ── Volume ───────────────────────────────────────────────────────────

    vol_result = score_volume(
        volume_5d_avg=technical_data.get('volume_5d_avg'),
        volume_20d_avg=technical_data.get('volume_20d_avg'),
        price_change_5d=technical_data.get('price_change_5d'),
    )
    sub_scores['volume'] = vol_result
    if vol_result.get('volume_trend') != 'unknown':
        w = weights['volume']
        weighted_sum += vol_result['score'] * w
        total_weight += w
        signals.append(f"Volume: {vol_result['signal']}")

    # ── Compute final ────────────────────────────────────────────────────

    if total_weight > 0:
        # Renormalize: divide by actual weight used (handles missing indicators)
        final_score = weighted_sum / total_weight
    else:
        final_score = 50.0  # No data at all — neutral
        signals.append("No technical data available — score is neutral default")

    return {
        'score': round(_clamp(final_score), 1),
        'sub_scores': sub_scores,
        'signals': signals,
    }
