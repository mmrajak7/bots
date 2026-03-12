"""Compute Weekly Supertrend (10,3) from daily OHLC data."""
import json
import sys
from datetime import datetime, timedelta
from collections import defaultdict


def aggregate_to_weekly(daily_data):
    """Convert daily OHLC to weekly OHLC (Mon-Fri weeks)."""
    weeks = defaultdict(list)
    for candle in daily_data:
        dt = datetime.fromisoformat(candle['date'].replace('+05:30', ''))
        # ISO week: (year, week_number)
        week_key = dt.isocalendar()[:2]
        weeks[week_key].append(candle)

    weekly = []
    for key in sorted(weeks.keys()):
        candles = weeks[key]
        weekly.append({
            'date': candles[0]['date'],
            'open': candles[0]['open'],
            'high': max(c['high'] for c in candles),
            'low': min(c['low'] for c in candles),
            'close': candles[-1]['close'],
            'volume': sum(c['volume'] for c in candles),
        })
    return weekly


def compute_supertrend(candles, period=10, multiplier=3):
    """Compute Supertrend indicator."""
    if len(candles) < period + 1:
        return []

    # Step 1: Compute True Range
    tr = []
    for i in range(len(candles)):
        high = candles[i]['high']
        low = candles[i]['low']
        if i == 0:
            tr.append(high - low)
        else:
            prev_close = candles[i-1]['close']
            tr.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    # Step 2: Compute ATR (Simple moving average for first, then EMA-like)
    atr = [0.0] * len(candles)
    # First ATR = simple average of first 'period' TRs
    atr[period - 1] = sum(tr[:period]) / period
    # Subsequent ATRs use smoothing: ATR = (prev_ATR * (period-1) + TR) / period
    for i in range(period, len(candles)):
        atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period

    # Step 3: Compute Supertrend
    upper_band = [0.0] * len(candles)
    lower_band = [0.0] * len(candles)
    supertrend = [0.0] * len(candles)
    direction = [1] * len(candles)  # 1 = up (bullish), -1 = down (bearish)

    for i in range(period - 1, len(candles)):
        hl2 = (candles[i]['high'] + candles[i]['low']) / 2
        upper_band[i] = hl2 + multiplier * atr[i]
        lower_band[i] = hl2 - multiplier * atr[i]

        if i == period - 1:
            supertrend[i] = lower_band[i]
            direction[i] = 1
            continue

        # Adjust bands based on previous values
        if lower_band[i] > lower_band[i-1] or candles[i-1]['close'] < lower_band[i-1]:
            pass  # keep new lower band
        else:
            lower_band[i] = lower_band[i-1]  # don't let it decrease in uptrend

        if upper_band[i] < upper_band[i-1] or candles[i-1]['close'] > upper_band[i-1]:
            pass  # keep new upper band
        else:
            upper_band[i] = upper_band[i-1]  # don't let it increase in downtrend

        # Determine direction
        if supertrend[i-1] == lower_band[i-1]:  # was bullish
            if candles[i]['close'] < lower_band[i]:
                direction[i] = -1
                supertrend[i] = upper_band[i]
            else:
                direction[i] = 1
                supertrend[i] = lower_band[i]
        else:  # was bearish
            if candles[i]['close'] > upper_band[i]:
                direction[i] = 1
                supertrend[i] = lower_band[i]
            else:
                direction[i] = -1
                supertrend[i] = upper_band[i]

    return [{
        'date': candles[i]['date'],
        'close': candles[i]['close'],
        'high': candles[i]['high'],
        'low': candles[i]['low'],
        'atr': round(atr[i], 2),
        'upper': round(upper_band[i], 2),
        'lower': round(lower_band[i], 2),
        'supertrend': round(supertrend[i], 2),
        'direction': 'UP' if direction[i] == 1 else 'DOWN',
    } for i in range(period - 1, len(candles))]


def main():
    # Load data from stdin or files
    # For now, demonstrate with the data we'll pipe in
    data = json.load(sys.stdin)

    for name, daily in data.items():
        weekly = aggregate_to_weekly(daily)
        st = compute_supertrend(weekly, period=10, multiplier=3)

        print(f"\n{'='*60}")
        print(f"  {name} — Weekly Supertrend (10,3)")
        print(f"{'='*60}")
        print(f"  Total weekly candles: {len(weekly)}")
        print(f"\n  Last 8 weeks:")
        print(f"  {'Date':<14} {'Close':>8} {'ST':>8} {'Dir':>5} {'ATR':>8} {'Gap%':>7}")
        print(f"  {'-'*54}")
        for row in st[-8:]:
            dt = row['date'][:10]
            gap_pct = ((row['close'] - row['supertrend']) / row['supertrend']) * 100
            print(f"  {dt:<14} {row['close']:>8.2f} {row['supertrend']:>8.2f} {row['direction']:>5} {row['atr']:>8.2f} {gap_pct:>+6.1f}%")

        latest = st[-1]
        print(f"\n  >>> CURRENT: Close={latest['close']:.2f}, ST={latest['supertrend']:.2f}, Direction={latest['direction']}")
        gap = ((latest['close'] - latest['supertrend']) / latest['supertrend']) * 100
        print(f"  >>> Gap from ST: {gap:+.1f}%")


if __name__ == '__main__':
    main()
