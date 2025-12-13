# Code Review Issues - Futures-Based Strike Selection

**Review Date:** 2025-12-13
**Status:** ALL ISSUES FIXED

## Summary
- **Critical**: 2 (2 fixed)
- **High**: 4 (4 fixed)
- **Medium**: 4 (4 fixed)
- **Low**: 5 (5 fixed)

---

## CRITICAL

### ISSUE-001: Network errors in CE-PE validation loop not handled [FIXED]
- **File**: `src/utils/calculations.py`
- **Line**: 288-298
- **Description**: The `kite.ltp()` call inside the CE-PE validation loop can throw network exceptions (timeout, connection error, API error). These are not caught, causing the entire function to crash and bubble up to the outer try/except in entry_manager which masks the real error.
- **Impact**: Entry could fail with cryptic error message; debugging difficult
- **Fix**: Wrapped LTP call in try/except, handle gracefully with warning

### ISSUE-002: No bounds checking on strike adjustment [FIXED]
- **File**: `src/utils/calculations.py`
- **Line**: 258-261, 319-342
- **Description**: Strike adjustments (+=100 or -=100) have no bounds checking. In pathological cases (e.g., extreme market conditions, stale data), the strike could drift far from spot price or even go negative.
- **Impact**: Could select invalid strike causing order rejection or wrong position
- **Fix**: Added bounds check: strike must be within +/-10% of spot

---

## HIGH

### ISSUE-003: Round returns float, implicit type coercion [FIXED]
- **File**: `src/utils/calculations.py`
- **Line**: 255
- **Description**: `round(estimated_forward / strike_interval) * strike_interval` returns a float in Python 3 when multiplied. Should explicitly cast to int for type safety.
- **Impact**: Type inconsistency, potential floating point issues
- **Fix**: Changed to `int(round(estimated_forward / strike_interval) * strike_interval)`

### ISSUE-004: Missing type hints on function parameters [FIXED]
- **File**: `src/utils/calculations.py`
- **Line**: 155-159
- **Description**: `kite` and `instruments_df` parameters lack type hints. Uses comments instead of proper annotations.
- **Impact**: Reduced IDE support, no mypy checking on these params
- **Fix**: Added proper type hints with TYPE_CHECKING import to avoid circular imports

### ISSUE-005: Date parsing could fail silently [FIXED]
- **File**: `src/utils/symbol_builder.py`
- **Line**: 537-542
- **Description**: `pd.to_datetime(futures_df['expiry'])` assumes expiry column exists and is parseable. If format is unexpected or column missing, could raise cryptic pandas error.
- **Impact**: Confusing error messages on malformed instruments.csv
- **Fix**: Added try/except around date parsing with error logging

### ISSUE-006: Broad exception catch masks real errors [FIXED]
- **File**: `src/services/entry_manager.py`
- **Line**: 348-353
- **Description**: Catching all exceptions with `except Exception as e` and falling back silently could mask real bugs (e.g., programming errors, assertion failures).
- **Impact**: Bugs could go unnoticed; debugging difficult
- **Fix**: Added full traceback logging at DEBUG level

---

## MEDIUM

### ISSUE-007: CE-PE oscillation not detected [FIXED]
- **File**: `src/utils/calculations.py`
- **Line**: 267-280
- **Description**: If CE-PE differences cause alternating adjustments (e.g., 26100->26200->26100), the loop runs 3 times without detecting oscillation. Final strike selection is arbitrary.
- **Impact**: Sub-optimal strike selection in edge cases
- **Fix**: Added oscillation detection with visited_strikes tracking; picks middle strike on oscillation

### ISSUE-008: DTE could be 0 on expiry day [FIXED]
- **File**: `src/utils/symbol_builder.py`
- **Line**: 556-557
- **Description**: `dte = (expiry_date - today).days` could be 0 on expiry day. While calculations.py uses `max(..., 1)`, the FuturesContract stores raw DTE=0 which could confuse callers.
- **Impact**: Misleading data in FuturesContract
- **Fix**: Changed to `dte = max((expiry_date - today).days, 1)`

### ISSUE-009: Missing column validation in get_nearest_futures_contract [FIXED]
- **File**: `src/utils/symbol_builder.py`
- **Line**: 519-524
- **Description**: Assumes 'name', 'instrument_type', 'expiry', 'tradingsymbol', 'instrument_token', 'lot_size' columns exist. Missing columns cause KeyError.
- **Impact**: Cryptic error on malformed instruments.csv
- **Fix**: Added required columns validation with clear error logging

### ISSUE-010: Unused import StrikeSelectionResult [FIXED]
- **File**: `src/services/entry_manager.py`
- **Line**: 57
- **Description**: `StrikeSelectionResult` is imported but never used as a type annotation (result is inferred from function return).
- **Impact**: Unnecessary import, ruff warning
- **Fix**: Removed unused import

---

## LOW

### ISSUE-011: Unused imports in kite_client.py [FIXED]
- **File**: `src/api/kite_client.py`
- **Line**: 16-23
- **Description**: `Path`, `Tuple`, `KiteAuthenticationError` imported but unused
- **Impact**: Code clutter, linting warnings
- **Fix**: Removed unused imports

### ISSUE-012: Unused imports in calculations.py [FIXED]
- **File**: `src/utils/calculations.py`
- **Line**: 15, 20
- **Description**: `timedelta`, `get_trading_config` imported but unused
- **Impact**: Code clutter, linting warnings
- **Fix**: Removed unused imports

### ISSUE-013: Unused imports in entry_manager.py [FIXED]
- **File**: `src/services/entry_manager.py`
- **Line**: Various
- **Description**: Multiple unused imports: `timedelta`, `TYPE_CHECKING`, `SpreadValidationResult`, `AtomicIronFlyExecutor`, `get_db_session`, `set_cooldown`
- **Impact**: Code clutter, linting warnings
- **Fix**: Removed all unused imports

### ISSUE-014: Unused import sys in __main__ [FIXED]
- **File**: `src/utils/symbol_builder.py`
- **Line**: 652
- **Description**: `sys` imported in __main__ block but never used
- **Impact**: Minor code clutter
- **Fix**: Removed by ruff auto-fix

### ISSUE-015: f-string without placeholders [FIXED]
- **File**: `src/utils/symbol_builder.py`
- **Line**: 686
- **Description**: `print(f"\n[6] Month code test:")` has no placeholders, should be regular string
- **Impact**: Minor inefficiency
- **Fix**: Removed `f` prefix by ruff auto-fix

---

## Edge Cases Identified (Not Bugs)

1. **Negative futures premium (backwardation)**: Algorithm handles correctly
2. **Options expiry today**: Uses min DTE=1, works correctly
3. **Futures expiry today**: Uses min DTE=1, works correctly
4. **Market closed (LTP=0)**: Falls back to spot + CE-PE
5. **Very high/low VIX**: Blocked at earlier check, doesn't reach strike selection
