"""
Test tick size error detection and retry mechanism
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import re

# Test error message parsing
test_error_messages = [
    "Tick size for this script is 0.10. Kindly enter price in the multiple of tick size for this script",
    "Tick size for this script is 0.05. Kindly enter price in the multiple of tick size",
    "The tick size for this symbol is 0.10. Please adjust your price.",
    "Invalid tick size. Expected 0.05",
]

print("=" * 80)
print("TICK SIZE ERROR PARSING TEST")
print("=" * 80)

for i, error_msg in enumerate(test_error_messages, 1):
    print(f"\n[{i}] Error Message:")
    print(f"    {error_msg}")

    # Extract tick size
    tick_match = re.search(r'tick size.*?is\s+(0\.\d+)', error_msg, re.IGNORECASE)

    if tick_match:
        required_tick = float(tick_match.group(1))
        print(f"    [OK] Detected tick size: {required_tick}")
    else:
        print(f"    [FAIL] Could not detect tick size")

print("\n" + "=" * 80)
print("PRICE CORRECTION SIMULATION")
print("=" * 80)

from src.utils.price_rounder import PriceRounder

# Simulate SBILIFE case
original_price = 1945.05
detected_tick = 0.10

print(f"\nOriginal price: Rs.{original_price}")
print(f"Zerodha required tick size: {detected_tick}")

corrected_price = PriceRounder.round_to_tick(original_price, tick_size=detected_tick)
print(f"Corrected price: Rs.{corrected_price}")
print(f"Valid? {corrected_price % detected_tick < 0.001}")

print("\n" + "=" * 80)
print("TEST COMPLETE")
print("=" * 80)
