"""
Kite Technical — OHLC data + computed technical indicators.

Loads Kite token from BOTS/data/kite_access_token.json, fetches 365 days
of daily OHLC candles, and computes:
  - RSI(14) with Wilder's smoothing
  - MACD(12,26,9) with signal line + histogram
  - 50 DMA and 200 DMA (simple moving averages)
  - 52-week high/low
  - Volume analysis (20-day avg, 5-day avg, trend)
  - Bollinger Bands(20,2)
  - Price vs DMA (% above/below)
  - Support/Resistance from recent swing highs/lows (60 days)

Returns a TechnicalData dataclass. All computations use pandas/numpy only
(no external ta-lib).
"""

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from kiteconnect import KiteConnect

from .cache import get_cache

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent.resolve()          # stock_analyzer/sources/
PACKAGE_DIR = SCRIPT_DIR.parent                        # stock_analyzer/
PROJECT_ROOT = PACKAGE_DIR.parent                      # Helper/
BOTS_DIR = PROJECT_ROOT.parent                         # BOTS/
TOKEN_FILE = BOTS_DIR / 'data' / 'kite_access_token.json'

CACHE_SOURCE = 'kite_technical'


# ── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class RSIData:
    """RSI(14) values."""
    current: Optional[float] = None
    previous: Optional[float] = None
    signal: str = ""  # 'oversold', 'overbought', 'neutral'


@dataclass
class MACDData:
    """MACD(12,26,9) values."""
    macd_line: Optional[float] = None
    signal_line: Optional[float] = None
    histogram: Optional[float] = None
    crossover: str = ""  # 'bullish', 'bearish', 'none'


@dataclass
class DMAData:
    """Moving average data."""
    dma_50: Optional[float] = None
    dma_200: Optional[float] = None
    price_vs_50dma_pct: Optional[float] = None   # +ve = above, -ve = below
    price_vs_200dma_pct: Optional[float] = None
    golden_cross: bool = False    # 50 DMA > 200 DMA
    death_cross: bool = False     # 50 DMA < 200 DMA


@dataclass
class BollingerBands:
    """Bollinger Bands(20,2) values."""
    upper: Optional[float] = None
    middle: Optional[float] = None
    lower: Optional[float] = None
    bandwidth_pct: Optional[float] = None   # (upper - lower) / middle * 100
    position: str = ""  # 'above_upper', 'near_upper', 'middle', 'near_lower', 'below_lower'


@dataclass
class VolumeAnalysis:
    """Volume analysis."""
    current_volume: Optional[int] = None
    avg_5d: Optional[float] = None
    avg_20d: Optional[float] = None
    volume_ratio: Optional[float] = None   # current / 20d avg
    trend: str = ""  # 'rising', 'falling', 'stable'


@dataclass
class SupportResistance:
    """Support and resistance levels from swing highs/lows."""
    supports: List[float] = field(default_factory=list)      # Up to 3 levels
    resistances: List[float] = field(default_factory=list)    # Up to 3 levels


@dataclass
class TechnicalData:
    """Complete technical analysis data."""
    symbol: str = ""
    ltp: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    timestamp: Optional[str] = None

    # 52-week
    week_52_high: Optional[float] = None
    week_52_low: Optional[float] = None
    pct_from_52w_high: Optional[float] = None
    pct_from_52w_low: Optional[float] = None

    # Indicators
    rsi: Optional[RSIData] = None
    macd: Optional[MACDData] = None
    dma: Optional[DMAData] = None
    bollinger: Optional[BollingerBands] = None
    volume: Optional[VolumeAnalysis] = None
    support_resistance: Optional[SupportResistance] = None

    def to_dict(self) -> dict:
        """Convert to plain dict (JSON-serializable)."""
        return asdict(self)


# ── Kite Connection ──────────────────────────────────────────────────────────

def _get_kite() -> KiteConnect:
    """Load Kite token and return authenticated KiteConnect instance."""
    if not TOKEN_FILE.exists():
        raise FileNotFoundError(
            f"Kite token file not found at {TOKEN_FILE}. "
            "Ensure SNAIL auth has run today."
        )

    with open(TOKEN_FILE) as f:
        token_data = json.load(f)

    # Check token freshness
    generated_at = token_data.get('generated_at', '')
    if generated_at:
        try:
            gen_date = datetime.fromisoformat(generated_at).date()
            today = datetime.now().date()
            if gen_date < today:
                logger.warning(
                    "Kite token is from %s (today is %s) — may be expired",
                    gen_date, today
                )
        except (ValueError, TypeError):
            pass

    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


def _fetch_ohlc(kite: KiteConnect, instrument_token: int,
                days: int = 365) -> pd.DataFrame:
    """Fetch daily OHLC data for the given instrument token."""
    to_date = datetime.now()
    from_date = to_date - timedelta(days=days)

    data = kite.historical_data(
        instrument_token=instrument_token,
        from_date=from_date,
        to_date=to_date,
        interval='day'
    )

    if not data:
        raise ValueError("No OHLC data returned from Kite")

    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)
    return df


def _resolve_instrument_token(kite: KiteConnect, symbol: str) -> int:
    """Get instrument token for an NSE equity symbol.

    Tries to get it from the quote API (no separate instruments download needed).
    """
    exchange_symbol = f"NSE:{symbol}"
    quote = kite.quote(exchange_symbol)
    if exchange_symbol not in quote:
        raise ValueError(f"Quote not found for {exchange_symbol}")
    return quote[exchange_symbol]['instrument_token']


# ── Technical Indicator Computations ─────────────────────────────────────────

def _compute_rsi(df: pd.DataFrame, period: int = 14) -> RSIData:
    """Compute RSI using Wilder's smoothing method."""
    try:
        close = df['close'].values
        if len(close) < period + 1:
            return RSIData()

        deltas = np.diff(close)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        # Wilder's smoothing: first value is SMA, then EMA-like
        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            current_rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            current_rsi = 100.0 - (100.0 / (1.0 + rs))

        # Compute previous RSI for comparison
        prev_rsi = None
        if len(close) > period + 2:
            prev_gains = gains[:-1]
            prev_losses = losses[:-1]
            pavg_gain = np.mean(prev_gains[:period])
            pavg_loss = np.mean(prev_losses[:period])
            for i in range(period, len(prev_gains)):
                pavg_gain = (pavg_gain * (period - 1) + prev_gains[i]) / period
                pavg_loss = (pavg_loss * (period - 1) + prev_losses[i]) / period
            if pavg_loss == 0:
                prev_rsi = 100.0
            else:
                prs = pavg_gain / pavg_loss
                prev_rsi = 100.0 - (100.0 / (1.0 + prs))

        signal = 'neutral'
        if current_rsi <= 30:
            signal = 'oversold'
        elif current_rsi >= 70:
            signal = 'overbought'

        return RSIData(
            current=round(current_rsi, 2),
            previous=round(prev_rsi, 2) if prev_rsi is not None else None,
            signal=signal,
        )
    except Exception as e:
        logger.warning("RSI computation failed: %s", e)
        return RSIData()


def _compute_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26,
                  signal_period: int = 9) -> MACDData:
    """Compute MACD(12,26,9) using EMA."""
    try:
        close = df['close']
        if len(close) < slow + signal_period:
            return MACDData()

        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
        histogram = macd_line - signal_line

        current_macd = macd_line.iloc[-1]
        current_signal = signal_line.iloc[-1]
        current_hist = histogram.iloc[-1]

        # Determine crossover
        crossover = 'none'
        if len(histogram) >= 2:
            prev_hist = histogram.iloc[-2]
            if prev_hist <= 0 < current_hist:
                crossover = 'bullish'
            elif prev_hist >= 0 > current_hist:
                crossover = 'bearish'

        return MACDData(
            macd_line=round(float(current_macd), 2),
            signal_line=round(float(current_signal), 2),
            histogram=round(float(current_hist), 2),
            crossover=crossover,
        )
    except Exception as e:
        logger.warning("MACD computation failed: %s", e)
        return MACDData()


def _compute_dma(df: pd.DataFrame, ltp: float) -> DMAData:
    """Compute 50 DMA and 200 DMA (simple moving averages)."""
    try:
        close = df['close']
        result = DMAData()

        if len(close) >= 50:
            dma_50 = float(close.iloc[-50:].mean())
            result.dma_50 = round(dma_50, 2)
            result.price_vs_50dma_pct = round((ltp - dma_50) / dma_50 * 100, 2)

        if len(close) >= 200:
            dma_200 = float(close.iloc[-200:].mean())
            result.dma_200 = round(dma_200, 2)
            result.price_vs_200dma_pct = round((ltp - dma_200) / dma_200 * 100, 2)

        if result.dma_50 is not None and result.dma_200 is not None:
            result.golden_cross = result.dma_50 > result.dma_200
            result.death_cross = result.dma_50 < result.dma_200

        return result
    except Exception as e:
        logger.warning("DMA computation failed: %s", e)
        return DMAData()


def _compute_bollinger(df: pd.DataFrame, period: int = 20,
                       num_std: float = 2.0) -> BollingerBands:
    """Compute Bollinger Bands(20,2)."""
    try:
        close = df['close']
        if len(close) < period:
            return BollingerBands()

        sma = float(close.iloc[-period:].mean())
        std = float(close.iloc[-period:].std())
        upper = sma + num_std * std
        lower = sma - num_std * std

        ltp = float(close.iloc[-1])
        bandwidth = (upper - lower) / sma * 100 if sma != 0 else None

        # Determine position
        position = 'middle'
        if ltp > upper:
            position = 'above_upper'
        elif ltp > upper - 0.2 * (upper - sma):
            position = 'near_upper'
        elif ltp < lower:
            position = 'below_lower'
        elif ltp < lower + 0.2 * (sma - lower):
            position = 'near_lower'

        return BollingerBands(
            upper=round(upper, 2),
            middle=round(sma, 2),
            lower=round(lower, 2),
            bandwidth_pct=round(bandwidth, 2) if bandwidth is not None else None,
            position=position,
        )
    except Exception as e:
        logger.warning("Bollinger Bands computation failed: %s", e)
        return BollingerBands()


def _compute_volume(df: pd.DataFrame) -> VolumeAnalysis:
    """Compute volume analysis: 5-day avg, 20-day avg, trend."""
    try:
        vol = df['volume']
        if len(vol) < 5:
            return VolumeAnalysis()

        current = int(vol.iloc[-1])
        avg_5d = float(vol.iloc[-5:].mean())
        avg_20d = float(vol.iloc[-20:].mean()) if len(vol) >= 20 else avg_5d
        ratio = current / avg_20d if avg_20d > 0 else None

        # Trend: compare 5-day avg to 20-day avg
        trend = 'stable'
        if len(vol) >= 20:
            if avg_5d > avg_20d * 1.2:
                trend = 'rising'
            elif avg_5d < avg_20d * 0.8:
                trend = 'falling'

        return VolumeAnalysis(
            current_volume=current,
            avg_5d=round(avg_5d, 0),
            avg_20d=round(avg_20d, 0),
            volume_ratio=round(ratio, 2) if ratio is not None else None,
            trend=trend,
        )
    except Exception as e:
        logger.warning("Volume analysis failed: %s", e)
        return VolumeAnalysis()


def _compute_support_resistance(df: pd.DataFrame,
                                lookback_days: int = 60) -> SupportResistance:
    """Find support/resistance from swing highs/lows in the last N days.

    A swing high is a candle whose high is higher than the 2 candles on each side.
    A swing low is a candle whose low is lower than the 2 candles on each side.
    """
    try:
        # Use only recent data
        recent = df.tail(lookback_days).reset_index(drop=True)
        if len(recent) < 5:
            return SupportResistance()

        highs = recent['high'].values
        lows = recent['low'].values
        current_close = float(recent['close'].iloc[-1])

        swing_highs: List[float] = []
        swing_lows: List[float] = []

        # Detect swing points with 2-bar lookback/lookahead
        for i in range(2, len(recent) - 2):
            # Swing high
            if (highs[i] > highs[i - 1] and highs[i] > highs[i - 2] and
                    highs[i] > highs[i + 1] and highs[i] > highs[i + 2]):
                swing_highs.append(float(highs[i]))

            # Swing low
            if (lows[i] < lows[i - 1] and lows[i] < lows[i - 2] and
                    lows[i] < lows[i + 1] and lows[i] < lows[i + 2]):
                swing_lows.append(float(lows[i]))

        # Cluster nearby levels (within 1% of each other)
        def cluster_levels(levels: List[float], tolerance_pct: float = 1.0) -> List[float]:
            if not levels:
                return []
            sorted_levels = sorted(levels)
            clusters: List[List[float]] = [[sorted_levels[0]]]
            for lvl in sorted_levels[1:]:
                cluster_avg = np.mean(clusters[-1])
                if abs(lvl - cluster_avg) / cluster_avg * 100 <= tolerance_pct:
                    clusters[-1].append(lvl)
                else:
                    clusters.append([lvl])
            # Return the mean of each cluster, sorted by number of touches
            result = [(np.mean(c), len(c)) for c in clusters]
            result.sort(key=lambda x: x[1], reverse=True)
            return [round(x[0], 2) for x in result]

        all_supports = cluster_levels(swing_lows)
        all_resistances = cluster_levels(swing_highs)

        # Filter: supports below current price, resistances above
        supports = [s for s in all_supports if s < current_close][:3]
        resistances = [r for r in all_resistances if r > current_close][:3]

        # Sort supports descending (nearest first), resistances ascending
        supports.sort(reverse=True)
        resistances.sort()

        return SupportResistance(supports=supports, resistances=resistances)
    except Exception as e:
        logger.warning("Support/Resistance computation failed: %s", e)
        return SupportResistance()


# ── Main Assembly ────────────────────────────────────────────────────────────

def _build_technical_data(symbol: str, kite: KiteConnect) -> TechnicalData:
    """Fetch OHLC + quote, compute all indicators, return TechnicalData."""
    exchange_symbol = f"NSE:{symbol}"

    # Get instrument token and quote
    quote = kite.quote(exchange_symbol)
    if exchange_symbol not in quote:
        raise ValueError(f"No quote data for {exchange_symbol}")

    q = quote[exchange_symbol]
    instrument_token = q['instrument_token']
    ltp = q['last_price']

    ohlc = q.get('ohlc', {})

    result = TechnicalData(
        symbol=symbol.upper(),
        ltp=ltp,
        open=ohlc.get('open'),
        high=ohlc.get('high'),
        low=ohlc.get('low'),
        close=ohlc.get('close'),
        timestamp=datetime.now().isoformat(),
    )

    # Fetch historical OHLC
    df = _fetch_ohlc(kite, instrument_token, days=365)

    if df.empty:
        logger.warning("No OHLC data for %s, returning quote-only data", symbol)
        return result

    # 52-week high/low
    result.week_52_high = round(float(df['high'].max()), 2)
    result.week_52_low = round(float(df['low'].min()), 2)
    if result.week_52_high and ltp:
        result.pct_from_52w_high = round(
            (result.week_52_high - ltp) / result.week_52_high * 100, 2
        )
    if result.week_52_low and ltp:
        result.pct_from_52w_low = round(
            (ltp - result.week_52_low) / result.week_52_low * 100, 2
        )

    # Compute indicators
    result.rsi = _compute_rsi(df)
    result.macd = _compute_macd(df)
    result.dma = _compute_dma(df, ltp)
    result.bollinger = _compute_bollinger(df)
    result.volume = _compute_volume(df)
    result.support_resistance = _compute_support_resistance(df)

    return result


# ── Public API ───────────────────────────────────────────────────────────────

def fetch_technical_data(symbol: str, use_cache: bool = True,
                         ttl_hours: Optional[float] = None) -> Optional[TechnicalData]:
    """Fetch technical analysis data for a stock.

    Args:
        symbol:     NSE equity symbol (e.g. 'RELIANCE')
        use_cache:  If True, check file cache first (default True)
        ttl_hours:  Override cache TTL (default: 1 hour)

    Returns:
        TechnicalData object, or None on failure.
    """
    symbol = symbol.upper()
    cache = get_cache()

    # Check cache
    if use_cache:
        cached = cache.get(symbol, CACHE_SOURCE, ttl_hours)
        if cached is not None:
            logger.info("Using cached technical data for %s", symbol)
            return _dict_to_technical_data(cached)

    # Fetch fresh
    try:
        kite = _get_kite()
        data = _build_technical_data(symbol, kite)

        # Cache result
        if use_cache:
            cache.set(symbol, CACHE_SOURCE, data.to_dict())

        logger.info("Computed technical data for %s (RSI=%.1f)",
                     symbol, data.rsi.current if data.rsi and data.rsi.current else 0)
        return data

    except FileNotFoundError as e:
        logger.error("Kite auth error: %s", e)
        return None
    except Exception as e:
        logger.error("Failed to fetch technical data for %s: %s", symbol, e)
        return None


def _dict_to_technical_data(d: dict) -> TechnicalData:
    """Reconstruct TechnicalData from a cached dict."""
    data = TechnicalData(
        symbol=d.get('symbol', ''),
        ltp=d.get('ltp'),
        open=d.get('open'),
        high=d.get('high'),
        low=d.get('low'),
        close=d.get('close'),
        timestamp=d.get('timestamp'),
        week_52_high=d.get('week_52_high'),
        week_52_low=d.get('week_52_low'),
        pct_from_52w_high=d.get('pct_from_52w_high'),
        pct_from_52w_low=d.get('pct_from_52w_low'),
    )

    rsi = d.get('rsi')
    if rsi:
        data.rsi = RSIData(**{k: v for k, v in rsi.items()
                              if k in RSIData.__dataclass_fields__})

    macd = d.get('macd')
    if macd:
        data.macd = MACDData(**{k: v for k, v in macd.items()
                                if k in MACDData.__dataclass_fields__})

    dma = d.get('dma')
    if dma:
        data.dma = DMAData(**{k: v for k, v in dma.items()
                              if k in DMAData.__dataclass_fields__})

    bb = d.get('bollinger')
    if bb:
        data.bollinger = BollingerBands(**{k: v for k, v in bb.items()
                                           if k in BollingerBands.__dataclass_fields__})

    vol = d.get('volume')
    if vol:
        data.volume = VolumeAnalysis(**{k: v for k, v in vol.items()
                                        if k in VolumeAnalysis.__dataclass_fields__})

    sr = d.get('support_resistance')
    if sr:
        data.support_resistance = SupportResistance(
            supports=sr.get('supports', []),
            resistances=sr.get('resistances', []),
        )

    return data
