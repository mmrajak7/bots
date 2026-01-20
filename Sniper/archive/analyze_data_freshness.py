#!/usr/bin/env python3
"""
Analyze the impact of old data on zone detection
"""

from datetime import datetime, timedelta

print("="*70)
print("ANALYSIS: Should We Use Data Older Than 10 Days?")
print("="*70)

print("\n--- CURRENT SETUP ---")
print("15m timeframe: 30-day lookback")
print("1h timeframe:  90-day lookback")

print("\n--- PROBLEMS WITH OLD DATA ---")

print("\n1. OPTIONS MARKET SPECIFICS:")
print("   - Monthly expiry options")
print("   - Strike relevance changes as spot moves")
print("   - 25700 PE was OTM 30 days ago, might be ATM now")
print("   - Old support at 150 when IV was 20% != support at 150 when IV is 35%")

print("\n2. MARKET REGIME CHANGES:")
print("   - Volatility shifts (calm → volatile or vice versa)")
print("   - Trend changes (bullish → bearish)")
print("   - Volume/liquidity patterns evolve")
print("   - News/events change sentiment")

print("\n3. SCORING FLAW:")
print("   Example Zone A:")
print("   - 20 bounces: 18 from 25-30 days ago, 2 from this week")
print("   - bounce_score = 20 bounces → HIGH")
print("   - freshness_score = based on LAST bounce → HIGH")
print("   - Total score = HIGH ✗ MISLEADING!")
print()
print("   Example Zone B:")
print("   - 8 bounces: all from last 5 days")
print("   - bounce_score = 8 bounces → LOWER")
print("   - freshness_score = based on LAST bounce → HIGH")
print("   - Total score = LOWER ✗ But more relevant!")

print("\n--- SCENARIO SIMULATION ---")

# Simulate two zones
zone_a_bounces = (
    [datetime.now() - timedelta(days=d) for d in range(25, 31)]  # 6 old bounces
    + [datetime.now() - timedelta(days=d) for d in range(20, 25)]  # 5 old bounces
    + [datetime.now() - timedelta(days=d) for d in range(15, 20)]  # 5 old bounces
    + [datetime.now() - timedelta(days=2), datetime.now() - timedelta(days=1)]  # 2 recent
)

zone_b_bounces = [
    datetime.now() - timedelta(days=d) for d in range(1, 9)  # 8 recent bounces
]

def score_freshness(last_bounce_date):
    days_ago = (datetime.now() - last_bounce_date).days
    return max(0, 10 - (days_ago / 7) * 10)

def score_bounces(count):
    return min(50, (count / 50) * 50)

zone_a_score = score_bounces(len(zone_a_bounces)) + score_freshness(max(zone_a_bounces))
zone_b_score = score_bounces(len(zone_b_bounces)) + score_freshness(max(zone_b_bounces))

print(f"\nZone A (mostly old data):")
print(f"  Bounces: {len(zone_a_bounces)} (16 old, 2 recent)")
print(f"  Oldest: {min(zone_a_bounces).strftime('%Y-%m-%d')} ({(datetime.now() - min(zone_a_bounces)).days} days ago)")
print(f"  Newest: {max(zone_a_bounces).strftime('%Y-%m-%d')} ({(datetime.now() - max(zone_a_bounces)).days} days ago)")
print(f"  Bounce Score: {score_bounces(len(zone_a_bounces)):.1f}")
print(f"  Freshness Score: {score_freshness(max(zone_a_bounces)):.1f}")
print(f"  TOTAL: {zone_a_score:.1f}")

print(f"\nZone B (all recent data):")
print(f"  Bounces: {len(zone_b_bounces)} (0 old, 8 recent)")
print(f"  Oldest: {min(zone_b_bounces).strftime('%Y-%m-%d')} ({(datetime.now() - min(zone_b_bounces)).days} days ago)")
print(f"  Newest: {max(zone_b_bounces).strftime('%Y-%m-%d')} ({(datetime.now() - max(zone_b_bounces)).days} days ago)")
print(f"  Bounce Score: {score_bounces(len(zone_b_bounces)):.1f}")
print(f"  Freshness Score: {score_freshness(max(zone_b_bounces)):.1f}")
print(f"  TOTAL: {zone_b_score:.1f}")

print(f"\n❌ Zone A scores HIGHER ({zone_a_score:.1f}) despite being mostly stale data!")
print(f"✓ Zone B scores LOWER ({zone_b_score:.1f}) but is more relevant!")

print("\n" + "="*70)
print("RECOMMENDATION: FILTER OLD BOUNCES")
print("="*70)

print("\nProposed thresholds:")
print("  15m timeframe: Only use bounces from last 10-15 days")
print("  1h timeframe:  Only use bounces from last 15-20 days")

print("\nBenefits:")
print("  ✓ Zones reflect CURRENT market conditions")
print("  ✓ More relevant for options trading")
print("  ✓ Removes stale support/resistance")
print("  ✓ Better reflects recent volatility regime")
print("  ✓ Reduces false signals from old data")

print("\nImplementation:")
print("  1. Keep long lookback (30/90 days) to collect data")
print("  2. Filter bounces older than threshold before zone creation")
print("  3. Only count recent bounces for scoring")

print("\n" + "="*70)

# Test with filtering
print("\n--- WITH 10-DAY FILTER ---")

def filter_recent_bounces(bounces, max_days=10):
    cutoff = datetime.now() - timedelta(days=max_days)
    return [b for b in bounces if b >= cutoff]

zone_a_filtered = filter_recent_bounces(zone_a_bounces, 10)
zone_b_filtered = filter_recent_bounces(zone_b_bounces, 10)

if len(zone_a_filtered) >= 5:  # MIN_BOUNCES
    zone_a_new_score = score_bounces(len(zone_a_filtered)) + score_freshness(max(zone_a_filtered))
    print(f"\nZone A (after filter):")
    print(f"  Bounces: {len(zone_a_filtered)} (was {len(zone_a_bounces)})")
    print(f"  TOTAL: {zone_a_new_score:.1f} (was {zone_a_score:.1f})")
else:
    print(f"\nZone A: REJECTED (only {len(zone_a_filtered)} bounces after filter)")

if len(zone_b_filtered) >= 5:
    zone_b_new_score = score_bounces(len(zone_b_filtered)) + score_freshness(max(zone_b_filtered))
    print(f"\nZone B (after filter):")
    print(f"  Bounces: {len(zone_b_filtered)} (was {len(zone_b_bounces)})")
    print(f"  TOTAL: {zone_b_new_score:.1f} (was {zone_b_score:.1f})")
else:
    print(f"\nZone B: REJECTED (only {len(zone_b_filtered)} bounces after filter)")

print(f"\n✓ Now Zone B ({zone_b_new_score:.1f}) scores appropriately!")
if len(zone_a_filtered) >= 5:
    print(f"✓ Zone A reduced to {zone_a_new_score:.1f} (more accurate)")

print("\n" + "="*70)
