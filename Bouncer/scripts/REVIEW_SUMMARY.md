# Momentum Scanner - Code Review Summary

**File:** `scripts/4_momentum_scanner.py`
**Review Date:** 2026-01-16 (Updated)
**Previous Review:** 2026-01-14

---

## Executive Summary

Second comprehensive review completed. Found **7 additional issues** related to alert deduplication and position limits. **All fixed.**

**Verdict:** Ready for production.

---

## Review History

| Date | Issues Found | Fixed | Focus |
|------|--------------|-------|-------|
| 2026-01-14 | 23 | 16 | Initial review (risk calc, candle data, limits) |
| 2026-01-16 | 7 | 7 | Alert deduplication, position limits |

---

## New Issues Found (2026-01-16)

| # | Severity | Issue | Line | Status |
|---|----------|-------|------|--------|
| 1 | **CRITICAL** | CE+PE both alert in same index (same scan) | 1040-1095 | FIXED |
| 2 | **CRITICAL** | Max positions not enforced for new signals | 1098-1118 | FIXED |
| 3 | **HIGH** | Telegram KeyError if config keys missing | 192-208 | FIXED |
| 4 | **HIGH** | No index-level position duplicate check | 758-765 | FIXED |
| 5 | **MEDIUM** | `strike_priority` function redefined in loop | 1020-1037 | FIXED |
| 6 | **MEDIUM** | IOError not caught in config loading | 162-167 | FIXED |
| 7 | **LOW** | Float comparison for strike matching | 718-726 | FIXED |

---

## Detailed Fixes (2026-01-16)

### 1. CRITICAL: CE+PE Same Index Alert (Lines 1040-1095)

**Problem:** When both SENSEX CE and SENSEX PE had patterns in the same scan, BOTH alerts were sent because:
- `select_best_signals()` grouped by `(index, option_type)`
- This allowed one signal per CE AND one per PE for the same index
- Index freeze only prevented FUTURE scans, not same-scan duplicates

**Fix:** Changed grouping from `(index, option_type)` to just `index`:
```python
# Before: groups by index + option_type (allows CE+PE)
groups: Dict[Tuple[str, str], List[PatternSignal]] = {}
key = (sig.index, sig.option_type)

# After: groups by index only (one alert per index)
groups: Dict[str, List[PatternSignal]] = {}
key = sig.index
```

### 2. CRITICAL: Max Positions Not Enforced (Lines 1098-1118)

**Problem:** If 2 positions open (MAX=3) and 4 signals found, all 4 would be processed.

**Fix:** Calculate available slots and pass to `select_best_signals()`:
```python
available_slots = max(0, MAX_POSITIONS - open_count)
if available_slots == 0:
    return positions
signals = select_best_signals(signals, max_signals=available_slots)
```

### 3. HIGH: Telegram KeyError (Lines 192-208)

**Problem:** Direct access to `CONFIG['telegram']['bot_token']` would raise KeyError if missing.

**Fix:** Safe access with validation:
```python
telegram_config = CONFIG.get('telegram', {})
bot_token = telegram_config.get('bot_token')
chat_id = telegram_config.get('chat_id')
if not bot_token or not chat_id:
    log.error("Telegram config missing bot_token or chat_id")
    return False
```

### 4. HIGH: Index-Level Position Duplicate (Lines 758-765)

**Problem:** Only checked symbol-level duplicates. Could open multiple positions in same index on different strikes.

**Fix:** Added index-level check:
```python
existing_indices = {p.index for p in open_positions}
if index in existing_indices:
    log.debug(f"{index}: Already have open position, skipping scan")
    continue
```

### 5. MEDIUM: Function Redefined in Loop (Lines 1020-1037)

**Problem:** `strike_priority()` function was defined inside a for loop, recreated on each iteration.

**Fix:** Moved to module-level functions:
```python
def _get_strike_priority(sig: PatternSignal) -> int:
    """Get strike priority: ATM (0) > OTM (1) > ITM (-1)."""
    ...

def _get_strike_label(otm_position: int) -> str:
    """Get human-readable strike label."""
    ...
```

### 6. MEDIUM: IOError Not Caught (Lines 162-167)

**Problem:** `load_config()` only caught `JSONDecodeError`, not file read errors.

**Fix:** Added IOError handling:
```python
except IOError as e:
    log.error(f"Config read error: {e}")
    return {}
```

### 7. LOW: Float Comparison for Strikes (Lines 718-726)

**Problem:** Using `==` with floats can have precision issues.

**Fix:** Cast to int for comparison:
```python
strike_int = int(strike)
if (int(opt['strike']) == strike_int and ...
```

---

## Logic Flow After All Fixes

```
1. Load positions, count open
2. If open >= MAX_POSITIONS: skip signal scan entirely

3. For each index:
   a. Skip if already have open position in this index
   b. Skip if index frozen (alert within 60 min)
   c. Scan CE and PE strikes for patterns

4. select_best_signals():
   a. Group all signals by INDEX (not index+option_type)
   b. Pick ONE best signal per index (ATM > OTM > ITM)
   c. Limit total to available_slots

5. Process selected signals:
   - Create position
   - Record index freeze
   - Send alert
```

---

## Behavior Changes (2026-01-16)

| Before | After |
|--------|-------|
| CE+PE both alert if patterns found | Only ONE alert per index per scan |
| Could exceed MAX_POSITIONS | Strictly enforced |
| Would crash on missing telegram keys | Graceful error handling |
| Could have 2+ positions on same index | One position per index |

---

## Previous Fixes (2026-01-14)

### Critical Fixes
- **C1:** Risk calculation edge case - skip if risk <= 0
- **C2:** Incomplete candle for SL - use candles[-2] not candles[-1]

### High Priority
- **H1:** Max positions limit constant added
- **H2:** Conservative SL exit price

### Other
- Removed unused imports/constants
- Fixed mutable defaults
- Added MIN_TRAIL_AMOUNT to reduce alert spam

---

## Configuration Summary

```python
# Key settings
MAX_POSITIONS = 3              # Limit open positions
MIN_TRAIL_AMOUNT = 2.0         # Min Rs to trail SL
TARGET_RR_RATIO = 1.5          # 1.5:1 risk-reward
STRIKE_OFFSETS = [-1, 0, 1]    # ATM +/- 1 strike
EMA_PERIOD = 20                # Trend filter
MARKET_START = (10, 0)         # 10:00 AM
MARKET_END = (14, 30)          # 2:30 PM
ALERT_FREEZE_MINUTES = 60      # 1 hour index freeze
```

---

## Remaining Considerations (Not Bugs)

1. **Timing window**: Scans run 15s after candle close. Pattern detection still works if delayed.

2. **Position ID uniqueness**: Uses `{index}_{symbol}_{timestamp}` with second precision. Collision extremely unlikely given 15-min scan intervals and one-per-index rule.

3. **Strike selection priority**: ATM > OTM > ITM, then CE > PE (tiebreaker). This is deterministic.

---

## Sign-off

| Reviewer | Date | Status |
|----------|------|--------|
| Claude Code | 2026-01-14 | APPROVED |
| Claude Code | 2026-01-16 | APPROVED (7 additional fixes) |
