#!/usr/bin/env python3
"""
Diagnostic: Show ACTUAL proven supports and calculation flow
Reveals how bounces are bucketed, merged, and converted to zones
"""

import json
import pickle
import logging
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from kiteconnect import KiteConnect

# Setup
SCRIPT_DIR = Path(__file__).parent.resolve()
BOTS_DIR = SCRIPT_DIR.parent
DATA_DIR = BOTS_DIR / 'data'
CACHE_DIR = SCRIPT_DIR / 'data' / 'cache'

TOKEN_FILE = DATA_DIR / 'kite_access_token.json'
ZONES_DB = CACHE_DIR / 'zones_db.pkl'

INDICES = {
    'BANKNIFTY': {'spot': 'NSE:NIFTY BANK', 'round_to': 1000},
    'NIFTY': {'spot': 'NSE:NIFTY 50', 'round_to': 100},
}

MIN_BOUNCES = 5
MAX_ZONE_WIDTH = 10
TIMEFRAME = '15m'
LOOKBACK_DAYS = 30
MAX_BOUNCE_AGE = 12

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def get_kite():
    with open(TOKEN_FILE) as f:
        token_data = json.load(f)
    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite

def get_instruments(kite):
    """Get ATM options for analysis."""
    instruments = {}

    for exchange in ['NFO']:
        for inst in kite.instruments(exchange):
            if inst['instrument_type'] not in ('CE', 'PE'):
                continue
            if inst['name'] not in INDICES:
                continue

            key = (inst['name'], inst['strike'], inst['instrument_type'], inst['expiry'])
            instruments[key] = {
                'symbol': inst['tradingsymbol'],
                'token': inst['instrument_token'],
                'exchange': inst['exchange']
            }

    return instruments

def calculate_atm(ltp: float, index: str) -> int:
    round_to = INDICES[index]['round_to']
    return int(round(ltp / round_to) * round_to)

def get_monthly_expiry(index: str, instruments: dict):
    expiries = set()
    for (idx, strike, opt_type, expiry) in instruments.keys():
        if idx == index:
            expiries.add(expiry)

    if not expiries:
        return None

    from datetime import date
    today = date.today()
    current_month = [e for e in expiries if e.year == today.year and e.month == today.month]
    if current_month:
        return max(current_month)

    next_month = today.month + 1 if today.month < 12 else 1
    next_year = today.year if today.month < 12 else today.year + 1
    next_month_expiries = [e for e in expiries if e.year == next_year and e.month == next_month]

    return max(next_month_expiries) if next_month_expiries else max(expiries)

def analyze_bounces_detailed(symbol: str, data: list, ltp: float):
    """
    Show EXACT bounce detection and bucketing process.
    This reveals the calculation flaws.
    """
    print("\n" + "="*80)
    print(f"ANALYZING: {symbol} (LTP: {ltp:.1f})")
    print("="*80)

    # Step 1: Find bounces (same as scanner)
    bounces = []
    cutoff_date = datetime.now() - timedelta(days=MAX_BOUNCE_AGE)

    for candle in data:
        required_keys = ['open', 'high', 'low', 'close', 'date']
        if not all(k in candle for k in required_keys):
            continue

        candle_date = candle['date'].replace(tzinfo=None) if candle['date'].tzinfo else candle['date']
        if candle_date < cutoff_date:
            continue

        open_price, high, low, close = candle['open'], candle['high'], candle['low'], candle['close']

        if any(v is None or v < 0 for v in [open_price, high, low, close]):
            continue

        candle_range = high - low
        if candle_range <= 0:
            continue

        close_position = (close - low) / candle_range
        lower_wick = min(open_price, close) - low
        wick_pct = lower_wick / candle_range

        # Reversal criteria
        if close_position >= 0.4 or wick_pct >= 0.3:
            bounces.append({
                'low': low,
                'date': candle['date'],
                'strength': close_position
            })

    if not bounces:
        print("❌ NO BOUNCES FOUND")
        return

    print(f"\n✅ Found {len(bounces)} bounces in last {MAX_BOUNCE_AGE} days\n")

    # Show raw bounces
    print("RAW BOUNCES (Actual Price Levels):")
    print("-" * 80)
    sorted_bounces = sorted(bounces, key=lambda x: x['low'])
    for i, b in enumerate(sorted_bounces[:20], 1):  # Show first 20
        print(f"  {i:2d}. Price: {b['low']:7.2f}  |  Date: {b['date'].strftime('%Y-%m-%d %H:%M')}  |  Strength: {b['strength']:.2f}")

    if len(bounces) > 20:
        print(f"  ... and {len(bounces) - 20} more bounces")

    # Step 2: Bucket to nearest 10 (CURRENT FLAWED APPROACH)
    print("\n" + "-"*80)
    print("STEP 2: BUCKETING TO NEAREST 10 (Current Approach)")
    print("-" * 80)

    zone_data = defaultdict(list)
    for b in bounces:
        rounded = round(b['low'] / 10) * 10
        zone_data[rounded].append(b)

    print(f"Bounces grouped into {len(zone_data)} buckets:\n")

    for level in sorted(zone_data.keys()):
        bounce_prices = [b['low'] for b in zone_data[level]]
        print(f"  Bucket {level:6.0f}: {len(bounce_prices):2d} bounces")
        print(f"    └─ Actual prices: {min(bounce_prices):.1f} to {max(bounce_prices):.1f} (range: {max(bounce_prices)-min(bounce_prices):.1f})")
        print(f"    └─ Prices: {', '.join([f'{p:.1f}' for p in sorted(bounce_prices)[:8]])}{' ...' if len(bounce_prices) > 8 else ''}")

    # Step 3: Merge adjacent buckets
    print("\n" + "-"*80)
    print("STEP 3: MERGING ADJACENT BUCKETS")
    print("-" * 80)

    zones = []
    sorted_levels = sorted(zone_data.keys())
    used = set()

    for level in sorted_levels:
        if level in used:
            continue

        merged = [level]

        # Try to merge with level+10
        if level + 10 in zone_data:
            test_bounces = zone_data[level] + zone_data[level + 10]
            test_lows = [b['low'] for b in test_bounces]
            zone_width = max(test_lows) - min(test_lows)

            if zone_width <= MAX_ZONE_WIDTH:
                merged.append(level + 10)
                used.add(level + 10)
                print(f"\n  ✓ Merged buckets {level} + {level+10}")
                print(f"    └─ Combined width: {zone_width:.1f} points (max allowed: {MAX_ZONE_WIDTH})")
            else:
                print(f"\n  ✗ Did NOT merge {level} + {level+10}")
                print(f"    └─ Combined width: {zone_width:.1f} points (exceeds max {MAX_ZONE_WIDTH})")

        all_bounces = []
        for price_level in merged:
            all_bounces.extend(zone_data[price_level])

        if len(all_bounces) >= MIN_BOUNCES:
            lows = [b['low'] for b in all_bounces]
            zone_center = sum(merged) / len(merged)  # ⚠️ CALCULATED FROM ROUNDED LEVELS!
            actual_center = sum(lows) / len(lows)    # ✓ TRUE center from actual prices

            zones.append({
                'merged_levels': merged,
                'zone_center_current': zone_center,
                'actual_center': actual_center,
                'low': min(lows),
                'high': max(lows),
                'bounces': len(all_bounces),
                'strength': sum(b['strength'] for b in all_bounces) / len(all_bounces),
                'all_lows': sorted(lows)
            })

        used.add(level)

    # Step 4: Show final zones with calculation errors
    print("\n" + "="*80)
    print("FINAL ZONES (With Calculation Errors Highlighted)")
    print("="*80)

    for i, z in enumerate(zones, 1):
        print(f"\n🎯 ZONE {i}:")
        print(f"  Merged bucket levels: {z['merged_levels']}")
        print(f"  Bounces: {z['bounces']} bounces")
        print(f"  Actual bounce range: {z['low']:.1f} to {z['high']:.1f} (width: {z['high']-z['low']:.1f} points)")

        print(f"\n  ⚠️  CURRENT ZONE CENTER: {z['zone_center_current']:.1f} (averaged from rounded buckets)")
        print(f"  ✓  CORRECT ZONE CENTER: {z['actual_center']:.1f} (averaged from actual bounces)")
        print(f"  ❌ ERROR: {abs(z['zone_center_current'] - z['actual_center']):.1f} points off!")

        # Show entry/stop calculation with current approach
        buffer = z['low'] * 0.02  # 2% buffer
        entry_current = int(z['low'] - buffer)
        stop_current = int(entry_current - buffer)

        print(f"\n  📊 ENTRY/STOP (Current 2% buffer approach):")
        print(f"    Buffer: {buffer:.1f} points (2% of zone low)")
        print(f"    Entry: {entry_current} (zone_low - buffer)")
        print(f"    Stop:  {stop_current} (entry - buffer)")

        # Show what correct approach would give
        actual_gap_to_zone = z['low'] - entry_current
        print(f"\n  📏 Distance from entry to zone: {actual_gap_to_zone:.1f} points")
        print(f"    └─ Is this appropriate for a 'sharp' reversal? (Probably too much!)")

        # Show all actual bounce prices
        print(f"\n  📍 All actual bounce prices:")
        for j in range(0, len(z['all_lows']), 10):
            chunk = z['all_lows'][j:j+10]
            print(f"    {', '.join([f'{p:.1f}' for p in chunk])}")

def main():
    print("\n" + "="*80)
    print("PROVEN SUPPORT FINDER - Diagnostic Mode")
    print("Analyzing calculation flow to find issues")
    print("="*80)

    kite = get_kite()
    instruments = get_instruments(kite)

    # Get NIFTY spot
    quote = kite.quote(INDICES['NIFTY']['spot'])
    nifty_ltp = quote[INDICES['NIFTY']['spot']]['last_price']

    print(f"\n📈 NIFTY LTP: {nifty_ltp:.2f}")

    # Get ATM options
    atm = calculate_atm(nifty_ltp, 'NIFTY')
    expiry = get_monthly_expiry('NIFTY', instruments)

    print(f"🎯 ATM: {atm}")
    print(f"📅 Expiry: {expiry.strftime('%d-%b-%Y')}")

    # Analyze PE option (usually more bounces)
    opt_type = 'PE'
    key = ('NIFTY', atm, opt_type, expiry)

    if key not in instruments:
        print(f"\n❌ Option not found: NIFTY {atm} {opt_type}")
        return

    inst = instruments[key]
    symbol = inst['symbol']

    # Get option LTP
    quote_key = f"{inst['exchange']}:{symbol}"
    quote = kite.quote(quote_key)
    opt_ltp = quote[quote_key]['last_price']

    # Fetch historical data
    print(f"\n📊 Fetching {LOOKBACK_DAYS}-day historical data for {symbol}...")
    data = kite.historical_data(
        instrument_token=inst['token'],
        from_date=datetime.now() - timedelta(days=LOOKBACK_DAYS),
        to_date=datetime.now(),
        interval='15minute'
    )

    print(f"✅ Fetched {len(data)} candles")

    # Analyze in detail
    analyze_bounces_detailed(symbol, data, opt_ltp)

    print("\n" + "="*80)
    print("ANALYSIS COMPLETE")
    print("="*80)
    print("\n💡 KEY FINDINGS:")
    print("  1. Check if zone centers match actual bounce clusters")
    print("  2. Look at entry distance from actual proven support")
    print("  3. Note any merged zones with large gaps between bucket groups")
    print("  4. Verify if zones are 'sharp' reversal points or wide ranges")
    print("\n")

if __name__ == "__main__":
    main()
