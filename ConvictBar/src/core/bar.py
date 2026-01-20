"""
Bar dataclass and related utilities.
Core data structure for candlestick representation.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class BarClassification(Enum):
    """Classification of bar based on conviction analysis."""
    STRONG_BULL = "STRONG_BULL"
    MODERATE_BULL = "MODERATE_BULL"
    WEAK_BULL = "WEAK_BULL"
    DOJI = "DOJI"
    WEAK_BEAR = "WEAK_BEAR"
    MODERATE_BEAR = "MODERATE_BEAR"
    STRONG_BEAR = "STRONG_BEAR"


@dataclass
class Bar:
    """
    Represents a single OHLCV candlestick bar.

    Attributes:
        datetime: Bar timestamp (start time)
        open: Open price
        high: High price
        low: Low price
        close: Close price
        volume: Trading volume (optional)
        bar_number: Bar number within the session (1-indexed)
    """
    datetime: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    bar_number: int = 0

    def __post_init__(self):
        """Validate bar data after initialization."""
        self._validate()

    def _validate(self) -> None:
        """Validate OHLC relationships."""
        if self.high < self.low:
            raise ValueError(f"High ({self.high}) cannot be less than Low ({self.low})")
        if self.high < self.open or self.high < self.close:
            raise ValueError(f"High ({self.high}) must be >= Open ({self.open}) and Close ({self.close})")
        if self.low > self.open or self.low > self.close:
            raise ValueError(f"Low ({self.low}) must be <= Open ({self.open}) and Close ({self.close})")

    @property
    def range(self) -> float:
        """Bar range (high - low)."""
        return self.high - self.low

    @property
    def body(self) -> float:
        """Bar body size (absolute difference between open and close)."""
        return abs(self.close - self.open)

    @property
    def body_ratio(self) -> float:
        """
        Body as a ratio of total range.
        Returns 0 if range is 0 (doji).
        """
        if self.range == 0:
            return 0.0
        return self.body / self.range

    @property
    def close_position(self) -> float:
        """
        Close position within the bar's range.
        0.0 = closed at low
        0.5 = closed at midpoint
        1.0 = closed at high
        Returns 0.5 for doji (range=0).
        """
        if self.range == 0:
            return 0.5
        return (self.close - self.low) / self.range

    @property
    def is_bullish(self) -> bool:
        """True if close > open (green bar)."""
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        """True if close < open (red bar)."""
        return self.close < self.open

    @property
    def is_doji(self) -> bool:
        """True if body ratio < 20%."""
        return self.body_ratio < 0.20

    @property
    def midpoint(self) -> float:
        """Bar midpoint price."""
        return (self.high + self.low) / 2

    @property
    def upper_wick(self) -> float:
        """Upper wick size."""
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        """Lower wick size."""
        return min(self.open, self.close) - self.low

    def classify(self) -> BarClassification:
        """
        Classify bar based on body ratio and close position.

        Returns:
            BarClassification enum value
        """
        if self.is_doji:
            return BarClassification.DOJI

        if self.is_bullish:
            if self.body_ratio >= 0.60 and self.close_position >= 0.80:
                return BarClassification.STRONG_BULL
            elif self.body_ratio >= 0.50 and self.close_position >= 0.70:
                return BarClassification.MODERATE_BULL
            else:
                return BarClassification.WEAK_BULL
        else:
            if self.body_ratio >= 0.60 and self.close_position <= 0.20:
                return BarClassification.STRONG_BEAR
            elif self.body_ratio >= 0.50 and self.close_position <= 0.30:
                return BarClassification.MODERATE_BEAR
            else:
                return BarClassification.WEAK_BEAR

    def to_dict(self) -> dict:
        """Convert bar to dictionary."""
        return {
            'datetime': self.datetime.isoformat(),
            'open': self.open,
            'high': self.high,
            'low': self.low,
            'close': self.close,
            'volume': self.volume,
            'bar_number': self.bar_number,
            'range': self.range,
            'body_ratio': round(self.body_ratio, 4),
            'close_position': round(self.close_position, 4),
            'classification': self.classify().value,
        }

    def __str__(self) -> str:
        direction = "BULL" if self.is_bullish else "BEAR" if self.is_bearish else "DOJI"
        return (
            f"Bar({self.datetime.strftime('%H:%M')} | "
            f"O:{self.open:.2f} H:{self.high:.2f} L:{self.low:.2f} C:{self.close:.2f} | "
            f"R:{self.range:.2f} BR:{self.body_ratio:.2%} CP:{self.close_position:.2%} | "
            f"{direction})"
        )


@dataclass
class DayData:
    """
    Container for a single trading day's data.

    Attributes:
        date: Trading date
        bars: List of Bar objects for the day
        previous_close: Previous day's closing price (for gap calculation)
    """
    date: datetime
    bars: list = field(default_factory=list)
    previous_close: Optional[float] = None

    @property
    def gap(self) -> Optional[float]:
        """Gap from previous close to today's open."""
        if self.previous_close is None or not self.bars:
            return None
        return self.bars[0].open - self.previous_close

    @property
    def gap_percent(self) -> Optional[float]:
        """Gap as percentage of previous close."""
        if self.gap is None or self.previous_close == 0:
            return None
        return (self.gap / self.previous_close) * 100

    def get_bar(self, bar_number: int) -> Optional[Bar]:
        """Get bar by number (1-indexed)."""
        for bar in self.bars:
            if bar.bar_number == bar_number:
                return bar
        return None

    @property
    def first_bar(self) -> Optional[Bar]:
        """Get first bar of the day."""
        return self.bars[0] if self.bars else None

    @property
    def last_bar(self) -> Optional[Bar]:
        """Get last bar of the day."""
        return self.bars[-1] if self.bars else None

    @property
    def day_high(self) -> Optional[float]:
        """Highest price of the day."""
        if not self.bars:
            return None
        return max(bar.high for bar in self.bars)

    @property
    def day_low(self) -> Optional[float]:
        """Lowest price of the day."""
        if not self.bars:
            return None
        return min(bar.low for bar in self.bars)

    @property
    def day_range(self) -> Optional[float]:
        """Day's trading range."""
        if self.day_high is None or self.day_low is None:
            return None
        return self.day_high - self.day_low
