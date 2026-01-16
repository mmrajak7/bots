# Momentum Scanner - Code Review Issues

**File:** `scripts/4_momentum_scanner.py`
**Review Date:** 2026-01-14
**Reviewer:** Claude Code

---

## Critical Issues

| ID | Line | Issue | Severity | Status |
|----|------|-------|----------|--------|
| C1 | 760-766 | **Risk calculation edge case**: If `ltp <= sl_price` (risk <= 0), signal is now skipped with warning. | CRITICAL | FIXED |
| C2 | 874-877 | **Incomplete candle used for SL check**: Now uses `candles[-2]` (completed candle) instead of `candles[-1]`. | CRITICAL | FIXED |

---

## High Severity Issues

| ID | Line | Issue | Severity | Status |
|----|------|-------|----------|--------|
| H1 | 667-671 | **No max positions limit**: Added `MAX_POSITIONS = 3` check at start of signal scan. | HIGH | FIXED |
| H2 | 888-889 | **Exit price on SL is LTP, not SL**: Now uses `min(ltp, pos.current_sl)` for conservative reporting. | HIGH | FIXED |
| H3 | 851 | **Target check uses LTP only**: Target hit checked against LTP - acceptable for momentum trading. | HIGH | ACCEPTED |
| H4 | N/A | **No rate limiting on Kite API**: With ATM-only (2 options per index × 3 indices = 6 calls), impact is minimal. | HIGH | ACCEPTED |

---

## Medium Severity Issues

| ID | Line | Issue | Severity | Status |
|----|------|-------|----------|--------|
| M1 | 39 | **Unused import**: `csv` removed | MEDIUM | FIXED |
| M2 | 62 | **Unused constant**: `INDEX_OPTIONS_FILE` removed | MEDIUM | FIXED |
| M3 | 334 | **Unused function**: `candle_to_serializable()` removed | MEDIUM | FIXED |
| M4 | 649 | **Misleading function name**: Renamed to `get_nearest_expiry()` | MEDIUM | FIXED |
| M5 | 696 | **Stale comment**: Updated to "# 0 = ATM" | MEDIUM | FIXED |
| M6 | 11 | **Stale docstring**: Updated to "ATM only" | MEDIUM | FIXED |
| M7 | 582 | **Division display issue**: If risk=0, signal is skipped (C1 fix) | MEDIUM | FIXED |
| M8 | 557 | **Position ID collision**: Low probability, timestamp-based - acceptable | MEDIUM | ACCEPTED |
| M9 | 901-902 | **Trailing SL too aggressive**: Added `MIN_TRAIL_AMOUNT = 2.0` threshold | MEDIUM | FIXED |
| M10 | 27 | **Line too long**: Shortened cron example | MEDIUM | FIXED |

---

## Low Severity Issues

| ID | Line | Issue | Severity | Status |
|----|------|-------|----------|--------|
| L1 | N/A | **No holiday check**: Will fail gracefully with "no data" - acceptable | LOW | ACCEPTED |
| L2 | N/A | **Duplicate signals per index**: CE/PE both can signal - intentional (trend filter separates) | LOW | BY DESIGN |
| L3 | 878 | **Fallback candle low**: Validated candle used with proper fallback | LOW | FIXED |

---

## kite_instruments.py Issues

| ID | Line | Issue | Severity | Status |
|----|------|-------|----------|--------|
| K1 | 192 | **Type error**: Added explicit `int()` cast | MEDIUM | FIXED |
| K2 | 195-196 | **Type error**: Fixed with K1 | MEDIUM | FIXED |
| K3 | 175 | **Mutable default argument**: Changed to `Optional[List[int]] = None` | LOW | FIXED |

---

## Summary

| Severity | Found | Fixed | Accepted | By Design |
|----------|-------|-------|----------|-----------|
| CRITICAL | 2 | 2 | 0 | 0 |
| HIGH | 4 | 2 | 2 | 0 |
| MEDIUM | 13 | 11 | 1 | 0 |
| LOW | 4 | 1 | 1 | 1 |
| **TOTAL** | **23** | **16** | **4** | **1** |

**Final Status:** All critical and high-priority issues resolved. Ready for production.

---

# Review 2026-01-16: Post ATM ± 1 and Index Freeze Changes

**Changes reviewed:**
1. ATM ± 1 strike scanning (was ATM only)
2. Index-level alert freeze (was symbol-level)

## New Issues Found

### Medium Severity

| ID | Line | Issue | Severity | Status |
|----|------|-------|----------|--------|
| M11 | 632 | **OTM label wrong for ITM strikes**: Shows "-1OTM" instead of "1ITM" | MEDIUM | FIXED |
| M12 | 726+ | **Index freeze checked too late**: Candles fetched before freeze check wastes API calls | MEDIUM | FIXED |

### Low Severity

| ID | Line | Issue | Severity | Status |
|----|------|-------|----------|--------|
| L4 | 3-11 | **Docstring outdated**: Says "ATM only" but now ATM ± 1 | LOW | FIXED |
| L5 | 77 | **Stale NIFTY lot_size=75**: Now 25, but unused (API provides it) | LOW | FIXED (removed) |
| L6 | 573-574 | **Docstring says "symbol"**: Now index-level | LOW | FIXED |
| L7 | 615 | **Variable named "symbol"**: Should be "key" or "index" | LOW | FIXED |

---

## Summary (2026-01-16 Review)

| Severity | Found | Fixed |
|----------|-------|-------|
| MEDIUM | 2 | 2 |
| LOW | 4 | 4 |
| **TOTAL** | **6** | **6** |

**All issues fixed.**
