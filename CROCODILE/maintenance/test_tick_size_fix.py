"""
Test tick size fix for NSE stocks
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from src.utils.price_rounder import PriceRounder, round_price

# Setup logging
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    level="INFO"
)

logger.info("=" * 80)
logger.info("TESTING DYNAMIC TICK SIZE FIX")
logger.info("=" * 80)

# Test cases
test_cases = [
    # (price, expected_tick_size, description)
    (100.02, 0.05, "Low price stock"),
    (456.78, 0.05, "Mid price stock < 1000"),
    (999.99, 0.05, "Just below 1000"),
    (1000.00, 0.10, "Exactly 1000"),
    (1234.53, 0.10, "Stock > 1000"),
    (1945.05, 0.10, "SBILIFE example"),
    (2497.87, 0.10, "High price stock"),
]

logger.info("\n[1] Testing get_nse_tick_size()...")
logger.info("-" * 80)

for price, expected_tick, desc in test_cases:
    actual_tick = PriceRounder.get_nse_tick_size(price)
    status = "✅" if actual_tick == expected_tick else "❌"
    logger.info(
        f"{status} Price: Rs.{price:>8.2f} | Expected: {expected_tick} | "
        f"Got: {actual_tick} | {desc}"
    )

logger.info("\n[2] Testing round_price()...")
logger.info("-" * 80)

rounding_tests = [
    # (input_price, expected_output)
    (100.02, 100.00),   # < 1000, tick 0.05
    (456.78, 456.80),   # < 1000, tick 0.05
    (999.99, 1000.00),  # < 1000, tick 0.05
    (1234.53, 1234.50), # >= 1000, tick 0.10
    (1945.05, 1945.10), # >= 1000, tick 0.10 (SBILIFE case)
    (1945.07, 1945.10), # >= 1000, tick 0.10
    (2497.87, 2497.90), # >= 1000, tick 0.10
]

for input_price, expected_output in rounding_tests:
    actual_output = round_price(input_price)
    status = "✅" if actual_output == expected_output else "❌"
    tick_size = PriceRounder.get_nse_tick_size(input_price)
    logger.info(
        f"{status} Input: Rs.{input_price:>8.2f} | "
        f"Expected: Rs.{expected_output:>8.2f} | "
        f"Got: Rs.{actual_output:>8.2f} | "
        f"Tick: {tick_size}"
    )

logger.info("\n[3] Specific SBILIFE Test Case...")
logger.info("-" * 80)

sbilife_price = 1945.05
rounded = round_price(sbilife_price)
tick_size = PriceRounder.get_nse_tick_size(sbilife_price)

logger.info(f"Original price: Rs.{sbilife_price}")
logger.info(f"Detected tick size: {tick_size}")
logger.info(f"Rounded price: Rs.{rounded}")

# Verify it would pass Zerodha validation (account for floating point precision)
remainder = abs(rounded % tick_size)
is_valid = (remainder < 0.001) or (abs(remainder - tick_size) < 0.001)
logger.info(f"Is valid multiple of {tick_size}? {is_valid} (remainder: {remainder})")

if is_valid:
    logger.info("✅ PASS: Price is valid multiple of tick size")
else:
    logger.error("❌ FAIL: Price is NOT a valid multiple of tick size")

logger.info("\n" + "=" * 80)
logger.info("TEST COMPLETE")
logger.info("=" * 80)
