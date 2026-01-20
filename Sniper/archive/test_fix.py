#!/usr/bin/env python3
"""
Test the zone detection fix
"""

from collections import defaultdict
from datetime import datetime

MIN_BOUNCES = 5
MAX_ZONE_WIDTH = 10  # New parameter

def old_zone_logic(bounces):
    """Original logic that causes over-merging"""
    zone_data = defaultdict(list)
    for b in bounces:
        rounded = round(b['low'] / 10) * 10
        zone_data[rounded].append(b)

    zones = []
    sorted_levels = sorted(zone_data.keys())
    used = set()

    for level in sorted_levels:
        if level in used:
            continue

        merged = [level]
        if level + 10 in zone_data:
            merged.append(level + 10)
            used.add(level + 10)

        all_bounces = []
        for price_level in merged:
            all_bounces.extend(zone_data[price_level])

        if len(all_bounces) >= MIN_BOUNCES:
            lows = [b['low'] for b in all_bounces]
            zone_center = sum(merged) / len(merged)

            zones.append({
                'price': int(zone_center),
                'low': min(lows),
                'high': max(lows),
                'bounces': len(all_bounces),
            })

        used.add(level)

    return zones


def new_zone_logic(bounces):
    """Fixed logic with max zone width check"""
    zone_data = defaultdict(list)
    for b in bounces:
        rounded = round(b['low'] / 10) * 10
        zone_data[rounded].append(b)

    zones = []
    sorted_levels = sorted(zone_data.keys())
    used = set()

    for level in sorted_levels:
        if level in used:
            continue

        merged = [level]

        # Try to merge with level+10, but check zone width
        if level + 10 in zone_data:
            # Get all bounces from both levels to check width
            test_bounces = zone_data[level] + zone_data[level + 10]
            test_lows = [b['low'] for b in test_bounces]
            zone_width = max(test_lows) - min(test_lows)

            # Only merge if combined width is acceptable
            if zone_width <= MAX_ZONE_WIDTH:
                merged.append(level + 10)
                used.add(level + 10)

        all_bounces = []
        for price_level in merged:
            all_bounces.extend(zone_data[price_level])

        if len(all_bounces) >= MIN_BOUNCES:
            lows = [b['low'] for b in all_bounces]
            zone_center = sum(merged) / len(merged)
            zone_width = max(lows) - min(lows)

            zones.append({
                'price': int(zone_center),
                'low': min(lows),
                'high': max(lows),
                'bounces': len(all_bounces),
                'width': zone_width
            })

        used.add(level)

    return zones


# Test data: two distinct levels that shouldn't be merged
test_bounces = [
    # Group 1: Around 160 level (14 bounces)
    {'low': 155, 'strength': 0.7, 'date': datetime.now()},
    {'low': 157, 'strength': 0.7, 'date': datetime.now()},
    {'low': 157, 'strength': 0.7, 'date': datetime.now()},
    {'low': 158, 'strength': 0.7, 'date': datetime.now()},
    {'low': 158, 'strength': 0.7, 'date': datetime.now()},
    {'low': 159, 'strength': 0.7, 'date': datetime.now()},
    {'low': 160, 'strength': 0.7, 'date': datetime.now()},
    {'low': 161, 'strength': 0.7, 'date': datetime.now()},
    {'low': 161, 'strength': 0.7, 'date': datetime.now()},
    {'low': 162, 'strength': 0.7, 'date': datetime.now()},
    {'low': 162, 'strength': 0.7, 'date': datetime.now()},
    {'low': 163, 'strength': 0.7, 'date': datetime.now()},
    {'low': 163, 'strength': 0.7, 'date': datetime.now()},
    {'low': 165, 'strength': 0.7, 'date': datetime.now()},

    # Group 2: Around 170 level (11 bounces)
    {'low': 166, 'strength': 0.7, 'date': datetime.now()},
    {'low': 166, 'strength': 0.7, 'date': datetime.now()},
    {'low': 167, 'strength': 0.7, 'date': datetime.now()},
    {'low': 167, 'strength': 0.7, 'date': datetime.now()},
    {'low': 168, 'strength': 0.7, 'date': datetime.now()},
    {'low': 169, 'strength': 0.7, 'date': datetime.now()},
    {'low': 170, 'strength': 0.7, 'date': datetime.now()},
    {'low': 170, 'strength': 0.7, 'date': datetime.now()},
    {'low': 171, 'strength': 0.7, 'date': datetime.now()},
    {'low': 171, 'strength': 0.7, 'date': datetime.now()},
    {'low': 172, 'strength': 0.7, 'date': datetime.now()},
]

print("="*60)
print("TESTING ZONE DETECTION FIX")
print("="*60)

print("\n--- OLD LOGIC (with over-merging bug) ---")
old_zones = old_zone_logic(test_bounces)
for i, zone in enumerate(old_zones, 1):
    print(f"Zone {i}:")
    print(f"  Center: {zone['price']}")
    print(f"  Range: {zone['low']:.0f}-{zone['high']:.0f} ({zone['high']-zone['low']:.0f} points)")
    print(f"  Bounces: {zone['bounces']}")

print("\n--- NEW LOGIC (with max width check) ---")
new_zones = new_zone_logic(test_bounces)
for i, zone in enumerate(new_zones, 1):
    print(f"Zone {i}:")
    print(f"  Center: {zone['price']}")
    print(f"  Range: {zone['low']:.0f}-{zone['high']:.0f} ({zone['width']:.0f} points)")
    print(f"  Bounces: {zone['bounces']}")

print("\n" + "="*60)
print("RESULT:")
print("="*60)
print("✅ OLD: Creates 1 zone (155-172, 17 points wide, 25 bounces)")
print("✅ NEW: Creates 2 zones (separate levels with 14 and 11 bounces)")
print("="*60)
