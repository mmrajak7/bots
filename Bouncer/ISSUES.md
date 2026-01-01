# Bouncer v2.1 - Code Review Issues

**Review Date:** 2026-01-01 (Updated)
**Previous Review:** 2025-12-31
**Reviewer:** Claude Opus 4.5
**Scope:** All scripts in `scripts/` directory

---

## Status Update from Previous Review

| Previous Issue | Status | Notes |
|----------------|--------|-------|
| C2: SL Check Uses LTP | ✅ FIXED | Now fetches actual previous day close |
| C5: Position File Corruption | ✅ FIXED | Uses atomic write with temp file |
| H4: Config Parsing | ✅ FIXED | Added try/except with clear errors |
| H5: Token Validation | ✅ FIXED | Validates api_key and access_token |
| M2: get_strike_interval | ✅ FIXED | Changed min() to max() |

---

## CRITICAL Issues (Must Fix)

### C1: Race Condition in Position File Operations
**File:** `scripts/3_market_scanner.py`
**Lines:** 270-299
**Severity:** CRITICAL
**Status:** OPEN

**Issue:** Position file read/write operations lack proper locking. If scanner cron overlaps with manual run, data corruption possible.

```python
def add_position(position: Position):
    positions = load_positions()  # READ
    positions.append(position)    # MODIFY
    save_positions(positions)     # WRITE - race window
```

**Note:** Atomic write was added, but file locking still missing. `shutil.move` is NOT atomic on Windows across filesystems.

**Fix:** Add file locking with `fcntl` (Unix) or `msvcrt.locking` (Windows).

---

### C2: Type Mismatch in FuturesSetup - Runtime Crash Risk
**File:** `scripts/3_market_scanner.py`
**Lines:** 640-641
**Severity:** CRITICAL
**Status:** NEW

**Issue:** `expiry` and `dte` passed to `FuturesSetup` can be `None` but dataclass expects `str` and `int`.

```python
futures_symbol, fut_lot_size, expiry_str, dte = get_futures_symbol(symbol)
# expiry_str and dte can be None!
return FuturesSetup(
    expiry=expiry_str,  # Type error: Optional[str] vs str
    dte=dte,            # Type error: Optional[int] vs int
)
```

**Fix:** Add null check or make dataclass fields Optional.

---

### C3: Type Mismatch in TradeSetup DTE
**File:** `scripts/3_market_scanner.py`
**Line:** 1102
**Severity:** CRITICAL
**Status:** NEW

**Issue:** `dte` from `get_expiry_for_stock()` can be `None`, passed to TradeSetup expecting `int`.

**Fix:** Add validation after `get_expiry_for_stock()` call.

---

### C4: Futures Symbol Construction Violates Design Rule
**File:** `scripts/3_market_scanner.py`
**Lines:** 560-564
**Severity:** CRITICAL
**Status:** OPEN (from previous review)

**Issue:** Design states "NEVER construct symbols manually!" but futures symbol is:

```python
futures_symbol = f"{symbol}{year_suffix}{month_abbr}FUT"  # Manual construction!
```

**Impact:** Could generate invalid symbols during expiry transitions.

**Fix:** Load futures symbols from instruments CSV.

---

## HIGH Severity Issues

### H1: No Retry Logic for Kite API Calls
**File:** `scripts/3_market_scanner.py`
**Lines:** 1137-1142
**Severity:** HIGH
**Status:** OPEN

**Issue:** Single LTP fetch failure aborts entire scan.

```python
try:
    ltps = kite.ltp(symbols)
except Exception as e:
    log.error(f"Failed to fetch LTPs: {e}")
    return []  # Entire scan lost!
```

**Impact:** Transient network issues cause complete scan failure.

**Fix:** Implement retry with exponential backoff (3 attempts).

---

### H2: Instruments Cache Never Refreshed
**File:** `scripts/3_market_scanner.py`
**Lines:** 77-108
**Severity:** HIGH
**Status:** NEW

**Issue:** `INSTRUMENTS_CACHE` loaded once at import, never refreshed.

```python
load_instruments_cache()  # Called once at module import
```

**Impact:** Long-running scanner (--loop) may have stale option data.

**Fix:** Reload cache periodically or at start of each scan.

---

### H3: Silent Exception in Previous Close Fetch
**File:** `scripts/3_market_scanner.py`
**Lines:** 684-686
**Severity:** HIGH
**Status:** NEW

**Issue:** All exceptions silently caught, SL check could fail for all positions.

```python
except Exception as e:
    log.error(f"{symbol}: Failed to fetch historical data: {e}")
    return None  # Silent failure - position unprotected!
```

**Fix:** Distinguish transient vs permanent errors, add retry.

---

### H4: Division by Zero in Reliability Score
**File:** `scripts/0_analyze_reliability.py`
**Lines:** 376-378
**Severity:** HIGH
**Status:** NEW

**Issue:** Edge case where `total == 0` could slip through if logic changes.

```python
total = successes + failures
if total < 5:
    return 0.5, 50, 5
success_rate = successes / total  # If total somehow 0, crash
```

**Fix:** Add explicit `if total == 0` guard.

---

### H5: Options Position P&L Calculation Incorrect
**File:** `scripts/3_market_scanner.py`
**Lines:** 457-460, 489-492
**Severity:** HIGH
**Status:** OPEN (from previous review)

**Issue:** P&L for OPTIONS compares stock price to option net debit - apples to oranges.

**Fix:** Track stock entry price OR calculate spread value from option quotes.

---

## MEDIUM Severity Issues

### M1: Unused Imports
**Files:** Multiple
**Severity:** MEDIUM
**Status:** NEW

```
0_analyze_reliability.py: csv, timedelta, Optional
0_build_historical.py: Set
2_analyze_candidates.py: defaultdict
3_market_scanner.py: Union
```

**Fix:** Remove unused imports.

---

### M2: Ambiguous Variable Name
**File:** `scripts/2_analyze_candidates.py`
**Line:** 650
**Severity:** MEDIUM
**Status:** NEW

```python
for l in s['levels'] if l['score'] >= 60  # 'l' looks like '1'
```

**Fix:** Rename to `level`.

---

### M3: Global Mutable State
**Files:** Multiple
**Severity:** MEDIUM
**Status:** OPEN

**Issue:** `RELIABILITY_DATA`, `INSTRUMENTS_CACHE`, `CONFIG` are global mutable state.

**Impact:** Testing difficulty, potential race conditions.

---

### M4: Hardcoded Magic Numbers
**File:** `scripts/0_build_historical.py`
**Lines:** 148-150, 282
**Severity:** MEDIUM
**Status:** NEW

```python
if batch_start.year < 2020:  # Why 2020?
if data and len(data) > 100:  # Why 100?
```

**Fix:** Move to config or document reasoning.

---

### M5: f-string Without Placeholders
**File:** `scripts/0_analyze_reliability.py`
**Line:** 569
**Severity:** MEDIUM
**Status:** NEW

```python
print(f"Run 0_build_historical.py first")  # No placeholders
```

---

### M6: No Expiry Warning for Positions
**File:** `scripts/3_market_scanner.py`
**Severity:** MEDIUM
**Status:** OPEN (from previous review)

**Issue:** Config has `expiry_warning_days: 3` but never implemented.

---

### M7: Timezone Handling Fragile
**File:** `scripts/2_analyze_candidates.py`
**Lines:** 336-340
**Severity:** MEDIUM
**Status:** OPEN

---

### M8: SQLite Connection Not Using Context Manager
**File:** `scripts/0_analyze_reliability.py`
**Lines:** 421-467
**Severity:** MEDIUM
**Status:** NEW

---

## LOW Severity Issues

### L1: Inconsistent Logging
**Files:** All scripts
**Severity:** LOW

Mix of `print()` and `logging`. Standardize on logging module.

---

### L2: Missing Type Hints
**Files:** Multiple
**Severity:** LOW

`find_swing_points`, `cluster_levels` return types not fully specified.

---

### L3: Unused Variable
**File:** `scripts/0_build_historical.py`
**Line:** 283
**Severity:** LOW

```python
csv_path = save_to_csv(data, symbol)  # Never used
```

---

### L4: Position ID Not Globally Unique
**File:** `scripts/3_market_scanner.py`
**Line:** 1213
**Severity:** LOW

```python
id=f"{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
# Two alerts in same second = same ID
```

**Fix:** Add milliseconds or use UUID.

---

### L5: Inconsistent Timeouts
**Files:** Multiple
**Severity:** LOW

Timeouts vary: 60, 30, 10 seconds. Define standard constants.

---

### L6: Telegram Token in Config
**File:** `config/config.json`
**Severity:** LOW (gitignored)

Bot token in config. Low risk as gitignored, but should use env var.

---

## Summary

| Severity | Previous | Current | Change |
|----------|----------|---------|--------|
| CRITICAL | 5 | 4 | -1 |
| HIGH | 7 | 5 | -2 |
| MEDIUM | 6 | 8 | +2 |
| LOW | 4 | 6 | +2 |
| **TOTAL** | **22** | **23** | +1 |

### Progress
- 5 issues fixed since last review
- 6 new issues identified (mostly from mypy/ruff static analysis)
- Net: +1 issues (better detection)

### Priority Fixes
1. **C2, C3**: Type mismatches - can crash scanner
2. **H1**: Add retry logic - network issues cause missed trades
3. **C1**: File locking - prevent position corruption
4. **H2**: Refresh instruments cache - critical for loop mode

---

*Generated by Claude Code review on 2026-01-01*
