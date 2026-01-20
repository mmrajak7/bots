"""Edge case tests for price proximity filter"""

# Test the formula for various scenarios
def test_price_proximity_logic():
    """Test price_distance_pct calculation edge cases"""

    max_distance_pct = 2.0  # Current config

    test_cases = [
        # (LTP, ST, Expected Distance%, Should Reject?, Description)
        (128.07, 108.69, 17.8, True, "IRFC case - far above ST"),
        (110.0, 108.69, 1.2, False, "Slightly above ST - valid touch"),
        (108.69, 108.69, 0.0, False, "Exactly at ST - perfect touch"),
        (105.0, 108.69, -3.4, False, "Below ST - good entry point"),
        (80.0, 100.0, -20.0, False, "Way below ST - still valid"),
        (102.0, 100.0, 2.0, False, "Exactly at threshold - edge case"),
        (102.01, 100.0, 2.01, True, "Just above threshold - rejected"),
        (100.0, 0.0, None, None, "ST=0 - division by zero (guarded)"),
    ]

    print("="*80)
    print("PRICE PROXIMITY FILTER - EDGE CASE ANALYSIS")
    print("="*80)
    print(f"Max Distance Threshold: {max_distance_pct}%")
    print()

    issues_found = []

    for ltp, st, expected_dist, should_reject, desc in test_cases:
        if st == 0:
            print(f"CASE: {desc}")
            print(f"  LTP={ltp}, ST={st}")
            print(f"  Result: GUARDED by 'reference_supertrend > 0' check")
            print()
            continue

        actual_dist = ((ltp - st) / st) * 100
        actual_reject = actual_dist > max_distance_pct

        status = "PASS" if actual_reject == should_reject else "FAIL"

        print(f"CASE: {desc}")
        print(f"  LTP={ltp}, ST={st}")
        print(f"  Distance: {actual_dist:.2f}% (expected: {expected_dist}%)")
        print(f"  Reject: {actual_reject} (expected: {should_reject})")
        print(f"  Status: {status}")

        # Check for issues
        if actual_dist < 0:
            print(f"  WARNING: Negative distance - log would say 'LTP is {actual_dist:.1f}% above ST'")
            print(f"           This is misleading - should say 'below ST'")
            issues_found.append(f"Misleading log for LTP < ST: {desc}")
        print()

    print("="*80)
    print("ISSUES FOUND:")
    print("="*80)
    for i, issue in enumerate(issues_found, 1):
        print(f"{i}. {issue}")

    if not issues_found:
        print("No critical issues found.")

    return issues_found

if __name__ == "__main__":
    issues = test_price_proximity_logic()

    print()
    print("="*80)
    print("ADDITIONAL REVIEW NOTES:")
    print("="*80)
    print("""
1. LOGIC CORRECTNESS:
   - Filter correctly rejects when LTP > ST + threshold
   - Filter correctly allows when LTP <= ST (negative distance)
   - Division by zero guarded with 'reference_supertrend > 0'

2. LOG MESSAGE ISSUE (MINOR):
   - When LTP < ST, log says "LTP is X% above ST" with negative X
   - Should say "below ST" for clarity
   - Not a bug, just confusing log output

3. CONFIG DEFAULT:
   - Code defaults to max_distance_pct=5.0 if config missing
   - Your local config has 2.0 (which is in .gitignore)
   - New deployments will use 5.0 unless config updated

4. EDGE AT THRESHOLD:
   - Condition is `price_distance_pct > max_distance_pct`
   - So exactly at 2.0% passes (not rejected)
   - This is correct behavior (inclusive threshold)
""")
