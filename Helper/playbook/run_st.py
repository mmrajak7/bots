"""Run Weekly + Monthly Supertrend computation from saved Kite data files."""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from compute_st import aggregate_to_weekly, compute_supertrend


def aggregate_to_monthly(daily_data):
    """Convert daily OHLC to monthly OHLC."""
    from collections import defaultdict
    from datetime import datetime

    months = defaultdict(list)
    for candle in daily_data:
        dt = datetime.fromisoformat(candle['date'].replace('+05:30', ''))
        month_key = (dt.year, dt.month)
        months[month_key].append(candle)

    monthly = []
    for key in sorted(months.keys()):
        candles = months[key]
        monthly.append({
            'date': candles[0]['date'],
            'open': candles[0]['open'],
            'high': max(c['high'] for c in candles),
            'low': min(c['low'] for c in candles),
            'close': candles[-1]['close'],
            'volume': sum(c['volume'] for c in candles),
        })
    return monthly


def load_kite_data(filepath):
    """Load data from Kite MCP saved result file."""
    with open(filepath, 'r') as f:
        wrapper = json.load(f)
    # Format: [{"type": "text", "text": "[{...}, ...]"}]
    raw = wrapper[0]['text']
    return json.loads(raw)


def print_supertrend(name, candles, timeframe, period=10, multiplier=3, last_n=10):
    """Compute and print supertrend for given candles."""
    st = compute_supertrend(candles, period, multiplier)
    if not st:
        print(f"  {name} ({timeframe}): Not enough data ({len(candles)} candles, need {period+1})")
        return

    print(f"\n{'='*70}")
    print(f"  {name} — {timeframe} Supertrend ({period},{multiplier})")
    print(f"{'='*70}")
    print(f"  Total {timeframe.lower()} candles: {len(candles)}")
    print(f"\n  Last {last_n} periods:")
    print(f"  {'Date':<14} {'Close':>8} {'ST':>8} {'Dir':>5} {'ATR':>8} {'Gap%':>7}")
    print(f"  {'-'*58}")
    for row in st[-last_n:]:
        dt = row['date'][:10]
        gap_pct = ((row['close'] - row['supertrend']) / row['supertrend']) * 100
        print(f"  {dt:<14} {row['close']:>8.2f} {row['supertrend']:>8.2f} {row['direction']:>5} {row['atr']:>8.2f} {gap_pct:>+6.1f}%")

    latest = st[-1]
    print(f"\n  >>> CURRENT: Close={latest['close']:.2f}, ST={latest['supertrend']:.2f}, Direction={latest['direction']}")
    gap = ((latest['close'] - latest['supertrend']) / latest['supertrend']) * 100
    print(f"  >>> Gap from ST: {gap:+.1f}%")

    if latest['direction'] == 'UP':
        touch_price = latest['supertrend']
        print(f"  >>> BUY TOUCH LEVEL: {touch_price:.2f} (price needs to fall {abs(gap):.1f}% to touch)")
    else:
        print(f"  >>> TREND IS DOWN — ST is resistance at {latest['supertrend']:.2f}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Run ST on saved Kite data files")
    parser.add_argument('base_dir', nargs='?', help="Directory containing mcp-kite data files")
    parser.add_argument('--names', nargs='+', help="Instrument names matching files (sorted order)")
    cli_args = parser.parse_args()

    if not cli_args.base_dir:
        print("Usage: python run_st.py <data_dir> [--names NAME1 NAME2 ...]")
        print("  data_dir: folder with mcp-kite*.json files from Kite MCP")
        return

    base = cli_args.base_dir
    if not os.path.isdir(base):
        print(f"Directory not found: {base}")
        return

    # Map files to names (sorted by timestamp in filename)
    files = sorted([f for f in os.listdir(base) if f.startswith('mcp-kite')])

    if not files:
        print(f"No mcp-kite* files found in {base}")
        return

    if cli_args.names:
        names = cli_args.names
    else:
        # Default: use filenames as labels
        names = [os.path.splitext(f)[0] for f in files]

    if len(files) != len(names):
        print(f"Expected {len(names)} names, found {len(files)} files")
        print("Files found:", files)
        print("Provide --names with exactly one name per file")
        return

    for name, fname in zip(names, files):
        filepath = os.path.join(base, fname)
        daily = load_kite_data(filepath)

        # Weekly ST
        weekly = aggregate_to_weekly(daily)
        print_supertrend(name, weekly, "Weekly")

        # Monthly ST (for ETFs, also useful for comparison)
        monthly = aggregate_to_monthly(daily)
        print_supertrend(name, monthly, "Monthly", last_n=6)


if __name__ == '__main__':
    main()
