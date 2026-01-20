#!/usr/bin/env python3
"""
Analyze the zone detection merging logic to understand the issue
"""

# Simulate the zone detection logic
def analyze_merging_scenario():
    """
    Simulate what happens when we have bounces at two different levels
    that get merged due to the rounding and merging logic.
    """

    print("="*60)
    print("ANALYZING ZONE MERGING LOGIC")
    print("="*60)

    # Scenario: Bounces at two distinct levels
    # Level 1: Around 157-163 (rounds to 160)
    # Level 2: Around 165-172 (rounds to 170)

    scenario_bounces = [
        # Group 1: Around 160 level (13 bounces)
        {'low': 155}, {'low': 157}, {'low': 158}, {'low': 159},
        {'low': 160}, {'low': 161}, {'low': 162}, {'low': 163},
        {'low': 157}, {'low': 158}, {'low': 161}, {'low': 162}, {'low': 163},

        # Group 2: Around 170 level (12 bounces)
        {'low': 165}, {'low': 166}, {'low': 167}, {'low': 168},
        {'low': 169}, {'low': 170}, {'low': 171}, {'low': 172},
        {'low': 166}, {'low': 167}, {'low': 170}, {'low': 171},
    ]

    print(f"\nTotal bounces: {len(scenario_bounces)}")
    print(f"Actual low values: {sorted([b['low'] for b in scenario_bounces])}")

    # Step 1: Round to nearest 10
    from collections import defaultdict
    zone_data = defaultdict(list)

    for b in scenario_bounces:
        rounded = round(b['low'] / 10) * 10
        zone_data[rounded].append(b)

    print(f"\n--- After Rounding ---")
    for level in sorted(zone_data.keys()):
        lows = [b['low'] for b in zone_data[level]]
        print(f"Level {level}: {len(lows)} bounces, lows = {sorted(lows)}")

    # Step 2: Merge adjacent levels (level and level+10)
    print(f"\n--- Merging Process ---")

    sorted_levels = sorted(zone_data.keys())
    used = set()

    for level in sorted_levels:
        if level in used:
            continue

        merged = [level]
        if level + 10 in zone_data:
            print(f"Merging {level} and {level+10}")
            merged.append(level + 10)
            used.add(level + 10)
        else:
            print(f"Level {level} not merged (no level at {level+10})")

        all_bounces = []
        for price_level in merged:
            all_bounces.extend(zone_data[price_level])

        lows = [b['low'] for b in all_bounces]
        zone_center = sum(merged) / len(merged)

        print(f"  Zone center: {zone_center}")
        print(f"  Total bounces: {len(all_bounces)}")
        print(f"  Low range: {min(lows)} - {max(lows)} ({max(lows) - min(lows)} points)")
        print()

        used.add(level)

    print("="*60)
    print("PROBLEM IDENTIFIED:")
    print("="*60)
    print("- Two distinct support levels (160 and 170) are merged")
    print("- Creates a 17-point wide zone (155-172)")
    print("- Appears strong (25 bounces) but is actually two separate levels")
    print("- When LTP is at 168, it's not really at either support level!")
    print()
    print("SUGGESTED FIXES:")
    print("1. Don't merge levels (remove merging logic)")
    print("2. Add max zone width check (e.g., 8-10 points max)")
    print("3. Use tighter rounding (e.g., round to nearest 5)")
    print("="*60)


def test_alternative_approaches():
    """Test alternative zone detection approaches"""

    scenario_bounces = [
        155, 157, 158, 159, 160, 161, 162, 163, 157, 158, 161, 162, 163,  # ~160
        165, 166, 167, 168, 169, 170, 171, 172, 166, 167, 170, 171,  # ~170
    ]

    print("\n" + "="*60)
    print("TESTING ALTERNATIVE APPROACHES")
    print("="*60)

    # Approach 1: Round to nearest 5 instead of 10
    print("\n--- Approach 1: Round to nearest 5 ---")
    from collections import defaultdict
    zone_data_5 = defaultdict(int)

    for low in scenario_bounces:
        rounded = round(low / 5) * 5
        zone_data_5[rounded] += 1

    for level in sorted(zone_data_5.keys()):
        if zone_data_5[level] >= 5:
            print(f"Level {level}: {zone_data_5[level]} bounces")

    # Approach 2: Don't merge levels, keep separate
    print("\n--- Approach 2: No merging (round to 10) ---")
    zone_data_10 = defaultdict(int)

    for low in scenario_bounces:
        rounded = round(low / 10) * 10
        zone_data_10[rounded] += 1

    for level in sorted(zone_data_10.keys()):
        if zone_data_10[level] >= 5:
            print(f"Level {level}: {zone_data_10[level]} bounces")

    # Approach 3: Group by range, max width 8 points
    print("\n--- Approach 3: Group by range (max 8 points) ---")
    sorted_bounces = sorted(scenario_bounces)

    zones = []
    current_zone = [sorted_bounces[0]]

    for low in sorted_bounces[1:]:
        if low - current_zone[0] <= 8:
            current_zone.append(low)
        else:
            if len(current_zone) >= 5:
                zones.append(current_zone)
            current_zone = [low]

    if len(current_zone) >= 5:
        zones.append(current_zone)

    for i, zone in enumerate(zones, 1):
        print(f"Zone {i}: {min(zone)}-{max(zone)} ({len(zone)} bounces)")

    print("\n" + "="*60)


if __name__ == "__main__":
    analyze_merging_scenario()
    test_alternative_approaches()
