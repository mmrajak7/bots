"""Test price proximity filter with IRFC data"""
import sys
sys.path.insert(0, '.')

from src.indicators.supertrend_calculator import SuperTrendCalculator

st_calc = SuperTrendCalculator()

print("="*60)
print("Testing IRFC Weekly Signal - Price Proximity Filter")
print("="*60)

# Test IRFC
is_valid, st_price, metadata = st_calc.verify_signal(
    script="IRFC",
    timeframe="W",
    signal_date="2025-12-29",
    use_completed_candle_only=True,
    check_fresh_touch=True
)

print(f"\nResult: {'VALID' if is_valid else 'REJECTED'}")
print(f"ST Price: {st_price}")
print(f"\nMetadata:")
for k, v in metadata.items():
    print(f"  {k}: {v}")

if not is_valid:
    print("\n*** SUCCESS: IRFC signal correctly REJECTED ***")
    if metadata.get('rejection_reason') == 'price_proximity':
        print(f"    Reason: Price {metadata.get('price_distance_pct', 0):.1f}% above ST (max: {metadata.get('max_distance_pct')}%)")
else:
    print("\n*** FAILURE: IRFC should have been rejected! ***")
