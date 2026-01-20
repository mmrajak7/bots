# Sniper Scanner Fixes Applied

**Date:** 2026-01-09
**Issues Fixed:** Zone over-merging + Old data relevance

---

## Issue 1: Zone Over-Merging (FIXED ✓)

### Problem
Zone detection was merging two distinct support levels into one wide zone:
- **Level 160** (lows: 155-165) → 14 bounces
- **Level 170** (lows: 166-172) → 11 bounces
- **Merged into**: 155-172 (17 points wide, 25 bounces)

**Result:** False alert when LTP=168 (not actually near either support level!)

### Root Cause
Lines 433-440 in `scanner.py`: Logic automatically merged adjacent levels (160 + 170) without checking if they're actually distinct levels.

### Fix Applied
Added **MAX_ZONE_WIDTH = 10** points check:
- **Before merging**, check combined zone width
- **Only merge** if width ≤ 10 points
- **Result**: Two separate zones instead of one fake wide zone

**Test Results:**
```
OLD LOGIC: 1 zone (155-172, 17 points, 25 bounces)
NEW LOGIC: 2 zones
  - Zone 1: 155-165 (10 points, 14 bounces)
  - Zone 2: 166-172 (6 points, 11 bounces)
```

**Files Modified:**
- `scanner.py` line 118: Added `MAX_ZONE_WIDTH = 10`
- `scanner.py` lines 436-446: Updated merging logic with width check
- `scanner.py` line 464: Added 'width' field to zone data

---

## Issue 2: Old Data Giving False Importance (FIXED ✓)

### Problem
Using 30-90 day old bounces for zone detection causes:
1. **Options market**: Old data not relevant (different IV, different strikes)
2. **Market regime**: Old bounces from different volatility/trend
3. **Scoring flaw**: 20 bounces (18 old + 2 recent) scores higher than 8 recent bounces

**Example:**
- Zone A: 18 bounces (16 from 20-30 days ago, 2 recent) → Score: HIGH ✗
- Zone B: 8 bounces (all from last week) → Score: LOWER ✗

### Fix Applied
Added **max_bounce_age** parameter to each timeframe:
- **15m timeframe**: Only use bounces from last **12 days**
- **1h timeframe**: Only use bounces from last **20 days**

**Implementation:**
1. Keep long lookback (30/90 days) to fetch sufficient data
2. **Filter bounces** older than threshold before zone creation
3. Only recent bounces count toward zone scoring

**Test Results:**
```
Without filtering (30 days): 18 bounces → Zone created
With 10-day filter:           8 bounces → Zone created (recent only)
With 5-day filter:            5 bounces → Zone created (very recent)
```

**Files Modified:**
- `scanner.py` lines 128, 136: Added `max_bounce_age` to timeframe configs
- `scanner.py` lines 391-413: Updated `find_reversal_zones()` with age filtering
- `scanner.py` lines 642-644: Pass `max_bounce_age` to zone detection

---

## Configuration Changes

### Before
```python
TIMEFRAMES = {
    '15m': {
        'lookback_days': 30,
        # No bounce age limit
    },
    '1h': {
        'lookback_days': 90,
        # No bounce age limit
    }
}
```

### After
```python
MAX_ZONE_WIDTH = 10  # Prevent over-merging

TIMEFRAMES = {
    '15m': {
        'lookback_days': 30,       # Fetch 30 days of data
        'max_bounce_age': 12,      # Only use last 12 days
    },
    '1h': {
        'lookback_days': 90,       # Fetch 90 days of data
        'max_bounce_age': 20,      # Only use last 20 days
    }
}
```

---

## Impact

### ✓ Accuracy Improvements
- No more false zones from over-merging distinct levels
- Only zones with **recent activity** get detected
- Removed stale support/resistance from old market conditions

### ✓ Alert Quality
- Alerts only when price near **actual** support zones
- Reduced false signals from combined/old zones
- Better reflects **current** market sentiment

### ✓ Backward Compatible
- Legitimate zones (<10 points) still merge correctly
- Existing logic preserved for valid use cases
- No breaking changes to zone scoring

---

## Testing

### Zone Width Fix
**Test File:** `test_fix.py`
- Verified merging stops when width > 10 points
- Confirmed separate zones created for distinct levels

### Bounce Age Filter
**Test File:** `test_bounce_filtering.py`
- Verified old bounces filtered correctly
- Confirmed only recent bounces counted
- Tested multiple age thresholds (5, 10, 30 days)

---

## Tuning Recommendations

If you find zones too strict or too loose, adjust these parameters:

### Zone Width Control
```python
MAX_ZONE_WIDTH = 10  # Increase for wider zones (12-15)
                     # Decrease for tighter zones (7-8)
```

### Bounce Age Control
```python
'15m': {
    'max_bounce_age': 12,  # Increase for more zones (15-20)
                           # Decrease for fresher zones (7-10)
},
'1h': {
    'max_bounce_age': 20,  # Increase for more zones (25-30)
                           # Decrease for fresher zones (15-18)
}
```

---

## Next Steps

1. **Monitor alerts** - Check if zone quality improves
2. **Compare old vs new** - Use logs to see filtering impact
3. **Tune if needed** - Adjust parameters based on results
4. **Consider volume** - Future enhancement: weight by volume

---

## Questions?

These fixes address:
- ✓ "155-172 zone doesn't look valid" → **Fixed**: Over-merging prevented
- ✓ "Should we ignore old data?" → **Fixed**: Only recent bounces used

The scanner will now identify zones that are:
- **Narrow enough** to be real support levels (not merged artifacts)
- **Recent enough** to be relevant to current market conditions
