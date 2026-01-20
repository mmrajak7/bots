#!/usr/bin/env python3
"""
Test bounce age filtering
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta

# Add parent directory to path
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from scanner import find_reversal_zones

print("="*70)
print("TESTING BOUNCE AGE FILTERING")
print("="*70)

# Create test data with bounces at different ages
now = datetime.now()

# Create candles with bounces at 160 level
# Group 1: Old bounces (20-30 days ago) - should be FILTERED
old_bounces = []
for i in range(10):
    days_ago = 20 + i
    old_bounces.append({
        'date': now - timedelta(days=days_ago),
        'open': 160,
        'high': 165,
        'low': 158,  # Will qualify as bounce
        'close': 162
    })

# Group 2: Recent bounces (1-8 days ago) - should be KEPT
recent_bounces = []
for i in range(8):
    days_ago = 1 + i
    recent_bounces.append({
        'date': now - timedelta(days=days_ago),
        'open': 160,
        'high': 165,
        'low': 159,  # Will qualify as bounce
        'close': 163
    })

# Combine all data
all_data = old_bounces + recent_bounces

print(f"\nTest Data:")
print(f"  Old bounces (20-30 days ago): {len(old_bounces)}")
print(f"  Recent bounces (1-8 days ago): {len(recent_bounces)}")
print(f"  Total candles: {len(all_data)}")

# Test 1: Without filtering (max_bounce_age=30)
print("\n" + "="*70)
print("TEST 1: Without filtering (max_bounce_age=30)")
print("="*70)

zones_no_filter = find_reversal_zones(all_data, ltp=160, max_bounce_age_days=30)

if zones_no_filter:
    for i, zone in enumerate(zones_no_filter, 1):
        print(f"\nZone {i}:")
        print(f"  Price: {zone['price']}")
        print(f"  Range: {zone['low']:.0f}-{zone['high']:.0f}")
        print(f"  Bounces: {zone['bounces']}")
        print(f"  Oldest bounce: {(now - zone['last_bounce'].replace(tzinfo=None)).days} days ago")
else:
    print("No zones found")

# Test 2: With 10-day filtering
print("\n" + "="*70)
print("TEST 2: With 10-day filtering (max_bounce_age=10)")
print("="*70)

zones_with_filter = find_reversal_zones(all_data, ltp=160, max_bounce_age_days=10)

if zones_with_filter:
    for i, zone in enumerate(zones_with_filter, 1):
        print(f"\nZone {i}:")
        print(f"  Price: {zone['price']}")
        print(f"  Range: {zone['low']:.0f}-{zone['high']:.0f}")
        print(f"  Bounces: {zone['bounces']}")
        oldest_days = (now - zone['last_bounce'].replace(tzinfo=None)).days
        print(f"  Oldest bounce: {oldest_days} days ago")
else:
    print("No zones found (expected: recent data only has 8 bounces, need 5 min)")

# Test 3: Stricter filtering (only 5 days)
print("\n" + "="*70)
print("TEST 3: Stricter filtering (max_bounce_age=5)")
print("="*70)

zones_strict = find_reversal_zones(all_data, ltp=160, max_bounce_age_days=5)

if zones_strict:
    for i, zone in enumerate(zones_strict, 1):
        print(f"\nZone {i}:")
        print(f"  Price: {zone['price']}")
        print(f"  Range: {zone['low']:.0f}-{zone['high']:.0f}")
        print(f"  Bounces: {zone['bounces']}")
        print(f"  Expected: 5 bounces (only 1-5 days old)")
else:
    print("No zones found")

# Summary
print("\n" + "="*70)
print("RESULTS SUMMARY")
print("="*70)

print(f"\nWithout filtering (30 days):")
if zones_no_filter:
    print(f"  Zones found: {len(zones_no_filter)}")
    print(f"  Total bounces: {sum(z['bounces'] for z in zones_no_filter)}")
    print(f"  Expected: 18 bounces (10 old + 8 recent)")
else:
    print("  No zones found")

print(f"\nWith 10-day filter:")
if zones_with_filter:
    print(f"  Zones found: {len(zones_with_filter)}")
    print(f"  Total bounces: {sum(z['bounces'] for z in zones_with_filter)}")
    print(f"  Expected: 8 bounces (only recent)")
else:
    print("  No zones found (8 bounces, need 5 min - should have found zones!)")

print(f"\nWith 5-day filter:")
if zones_strict:
    print(f"  Zones found: {len(zones_strict)}")
    print(f"  Total bounces: {sum(z['bounces'] for z in zones_strict)}")
    print(f"  Expected: 5 bounces (1-5 days old)")
else:
    print("  No zones found (need min 5 bounces)")

print("\n" + "="*70)
print("TEST COMPLETE")
print("="*70)
print("\nKey Takeaways:")
print("1. Filtering reduces bounce count by excluding old data")
print("2. Only zones with recent activity are detected")
print("3. Prevents false signals from stale support levels")
print("="*70)
