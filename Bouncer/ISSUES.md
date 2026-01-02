# Bouncer v2.2 - Code Review Issues

**Review Date:** 2026-01-02
**Previous Review:** 2026-01-01
**Reviewer:** Claude Opus 4.5
**Scope:** All scripts in `scripts/` directory + config

---

## Session 3 Updates (2026-01-02)

### Fixed Since Last Review
| Issue | Description | Status |
|-------|-------------|--------|
| C2, C3, C4 | FuturesSetup type mismatches and manual symbol construction | ✅ FIXED - Futures replaced with OTM Buy |
| H5 | Options P&L calculation incorrect | ✅ FIXED - Removed P&L calc from exit alerts |
| Unused imports | Multiple files had unused imports | ✅ FIXED |
| BULLISH ONLY | Resistance levels still being processed | ✅ FIXED |
| H1 (partial) | LTP fetch retry logic | ✅ FIXED - Added 3x retry with exponential backoff |

### New Issues Found
| Issue | Severity | Description |
|-------|----------|-------------|
| Division by zero risks | CRITICAL | Multiple divisions without zero guards |
| Unused `field` import | HIGH | dataclasses field imported but unused |
| Unused `resistance_clusters` | HIGH | Computed but never used |
| HAS_FCNTL unused | MEDIUM | fcntl imported but never used for locking |
| No retry on other API calls | HIGH | Only main LTP has retry |

---

## CRITICAL Issues

### C1: Division by Zero Risks
**Files:** All 3 main scripts
**Severity:** CRITICAL
**Status:** ✅ FIXED

Multiple locations divide by variables that could theoretically be zero:

**3_market_scanner.py:**
```python
# Line 547 - ltp could be 0
premium_pct = (premium / ltp) * 100

# Line 554 - quote['ask'] could be 0
bid_ask_spread_pct = ((quote['ask'] - quote['bid']) / quote['ask']) * 100

# Lines 567, 991, 1153 - level_price could be 0
distance_pct = abs(ltp - level_price) / level_price * 100
```

**2_analyze_candidates.py:**
```python
# Line 288 - avg could be 0
if abs(point[1] - avg) / avg * 100 <= tolerance_pct:

# Line 431 - ltp could be 0
distance_pct = abs(cluster['price'] - ltp) / ltp * 100
```

**0_analyze_reliability.py:**
```python
# Lines 167, 211, 216, 274 - similar division risks
```

**Mitigation:** In practice, prices from exchange are never 0. But defensive guards should be added.

**Fix:** Add `if divisor == 0: continue` or `return` guards.

---

### C2: Race Condition in Position File Operations
**File:** `scripts/3_market_scanner.py`
**Lines:** 326-331
**Severity:** CRITICAL
**Status:** OPEN (from previous review)

**Issue:** Position file read/write operations lack proper locking.

```python
def add_position(position: Position):
    positions = load_positions()  # READ
    positions.append(position)    # MODIFY
    save_positions(positions)     # WRITE - race window
```

**Note:** Atomic write helps but `shutil.move` is NOT atomic on Windows.

**Fix:** Add file locking or use SQLite for positions.

---

## HIGH Severity Issues

### H1: Unused Imports
**File:** `scripts/3_market_scanner.py`
**Severity:** HIGH (code smell)
**Status:** ✅ FIXED

Removed unused `date` and `field` imports.

---

### H2: Unused Variable - resistance_clusters
**File:** `scripts/2_analyze_candidates.py`
**Line:** 420
**Severity:** HIGH (dead code)
**Status:** ✅ FIXED

Removed unused computation.

---

### H3: No Retry on Other API Calls
**File:** `scripts/3_market_scanner.py`
**Severity:** HIGH
**Status:** NEW

Only main `kite.ltp()` in scan has retry. These don't:
- `kite.ltp()` in TP check (line 692)
- `kite.instruments()` in get_previous_close (line 603)
- `kite.quote()` in get_single_option_quote (line 481)
- `kite.quote()` in get_option_quotes (line 805)

**Fix:** Add retry wrapper for all Kite API calls.

---

### H4: Broad Exception Handling Loses Stack Traces
**File:** `scripts/3_market_scanner.py`
**Severity:** HIGH
**Lines:** 92-93, 289-291, 495-497, 826-828

```python
except Exception as e:
    log.warning(f"Failed to load reliability data: {e}")
    # Stack trace lost, debugging harder
```

**Fix:** Use `log.exception()` to preserve stack traces.

---

### H5: Missing Token Expiry Handling
**File:** `scripts/3_market_scanner.py`
**Severity:** HIGH

Kite tokens expire daily at 6 AM IST. If scanner runs overnight, `TokenException` will occur.

**Fix:** Catch `TokenException` specifically and alert user.

---

## MEDIUM Severity Issues

### M1: HAS_FCNTL Imported but Never Used
**File:** `scripts/3_market_scanner.py`
**Lines:** 39-43
**Severity:** MEDIUM

```python
try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False
```

Imported but never used for file locking.

**Fix:** Implement locking or remove import.

---

### M2: Concurrent levels.json Modification Risk
**File:** `scripts/3_market_scanner.py`
**Line:** 1267-1268
**Severity:** MEDIUM

Scanner modifies `levels.json` in-place. If two scanner instances run, corruption possible.

**Fix:** Use atomic write pattern (same as positions file).

---

### M3: No Config Validation
**File:** `scripts/3_market_scanner.py`
**Severity:** MEDIUM

Config values like `atr_multiplier`, `min_score` used directly without validation.

**Fix:** Add config validation at startup.

---

### M4: Hardcoded Percentages
**Files:** All scripts
**Severity:** MEDIUM

Some thresholds in config, others hardcoded:
- Line 555: `if bid_ask_spread_pct > 15:` (hardcoded)
- Reliability: `approach_pct = 0.5` (hardcoded)

**Fix:** Move all thresholds to config.

---

### M5: Global Mutable State
**Files:** Multiple
**Severity:** MEDIUM

`RELIABILITY_DATA`, `INSTRUMENTS_CACHE`, `CONFIG` are global mutable state.

---

### M6: No Expiry Warning for Positions
**File:** `scripts/3_market_scanner.py`
**Severity:** MEDIUM

Config has `expiry_warning_days: 3` but never implemented.

---

## LOW Severity Issues

### L1: Inconsistent Logging
**Files:** All scripts
**Severity:** LOW

Mix of `print()` and `logging`. Standardize on logging.

---

### L2: Magic Numbers
**File:** `scripts/3_market_scanner.py`
**Severity:** LOW

```python
time.sleep(2 ** attempt)  # Magic backoff
time.sleep(60)            # Magic error sleep
```

**Fix:** Define as named constants.

---

### L3: Position ID Not Globally Unique
**File:** `scripts/3_market_scanner.py`
**Line:** 1191
**Severity:** LOW

```python
id=f"{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
# Two alerts in same second = same ID
```

**Fix:** Add milliseconds or use UUID.

---

### L4: Log File Rotation Not Configured
**File:** `scripts/3_market_scanner.py`
**Severity:** LOW

Logs created daily but no rotation/cleanup. Disk could fill up.

---

### L5: Temp Files on Failed Write
**File:** `scripts/3_market_scanner.py`
**Lines:** 318-323
**Severity:** LOW

Could leave stale temp files on write failure.

---

## API Failure Handling Summary

| Function | Try/Except | Retry | Timeout |
|----------|------------|-------|---------|
| `kite.ltp()` main scan | ✓ | ✓ (3x) | ✗ |
| `kite.ltp()` TP check | ✓ | ✗ | ✗ |
| `kite.instruments()` | ✓ | ✗ | ✗ |
| `kite.quote()` options | ✓ | ✗ | ✗ |
| `kite.historical_data()` | ✓ | ✗ | ✗ |
| `send_telegram()` | ✓ | ✗ | ✓ (10s) |

---

## Edge Cases Verified

| Edge Case | Handled? | Notes |
|-----------|----------|-------|
| Empty instruments CSV | ✓ | Returns (None, None) |
| No historical data | ✓ | Stock skipped |
| Empty levels.json | ✓ | Early return |
| No open positions | ✓ | Returns empty list |
| Market holidays | ⚠️ | Runs anyway |
| Weekend runs | ✓ | is_market_hours() |
| Token expired | ✗ | Will crash |
| Network timeout | ⚠️ | Only Telegram has timeout |

---

## Summary

| Severity | Count | Fixed Today |
|----------|-------|-------------|
| CRITICAL | 2 | 1 (C1) |
| HIGH | 5 | 7 total (2 more today) |
| MEDIUM | 6 | 0 |
| LOW | 5 | 0 |
| **TOTAL** | **18** | **8** |

### Fixed This Session
- ✅ FuturesSetup removed (replaced with OTM Buy)
- ✅ Options P&L calculation removed
- ✅ LTP fetch retry added
- ✅ Unused imports cleaned
- ✅ BULLISH ONLY enforced
- ✅ **C1: Division by zero guards** - Added in all 3 scripts
- ✅ **H1: Unused imports** - Removed `date`, `field`
- ✅ **H2: Unused resistance_clusters** - Removed

### Priority Fixes Remaining
1. **H3**: Add retry wrapper for all Kite API calls
2. **C2**: File locking for positions
3. **H5**: Handle token expiry gracefully
4. **H4**: Use log.exception() for stack traces

---

*Generated by Claude Code review on 2026-01-02*
