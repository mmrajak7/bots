# Code Review Issues - Z-Score Trading Bot v3.0

## Review Date: 2025-12-13 (Second Review - Post Config Updates)

---

## NEW Critical Issues

| ID | File | Line | Description | Severity | Status |
|----|------|------|-------------|----------|--------|
| C1 | main.py | 1221-1235 | `_log_trade_csv` method references undefined `Trade` class - will cause NameError if called | Critical | TO FIX |
| C2 | main.py | 33 | `asdict` imported but never used after Trade class removal | Low | TO FIX |

---

## NEW High Issues

| ID | File | Line | Description | Severity | Status |
|----|------|------|-------------|----------|--------|
| H1 | main.py | 251 | `int(row['instrument_token'])` can throw ValueError if token is empty/invalid | High | TO FIX |
| H2 | main.py | 303, 369 | `int(data['lot_size'])` can throw ValueError for non-numeric strings | High | TO FIX |
| H3 | main.py | 365 | `float(data['strike'])` can throw ValueError for invalid strings | High | TO FIX |
| H4 | main.py | 1457-1464 | WebSocket not closed on shutdown - resource leak | High | TO FIX |
| H5 | db.py | 243, 287, 353 | Loading boolean from INTEGER could cause type issues | High | TO FIX |

---

## NEW Medium Issues

| ID | File | Line | Description | Severity | Status |
|----|------|------|-------------|----------|--------|
| M1 | main.py | 1096 | Only 1 second sleep before getting option price - may be insufficient | Medium | TO FIX |
| M2 | main.py | 652-655 | `check_margin` returns True on failures - logs but proceeds with risky order | Medium | DOCUMENT |

---

## Detailed Analysis

### C1: Dead Code - `_log_trade_csv` Method Uses Undefined Class

**Location:** main.py:1221-1235

**Problem:** The method references `Trade` dataclass which was removed in v3.0 cleanup. If this method is ever called, it will raise `NameError`.

```python
def _log_trade_csv(self, trade: Trade):  # Trade is undefined!
```

**Fix:** Remove the dead method entirely since we now use SQLite DB for all trade logging.

---

### H1-H3: Numeric Parsing Without Error Handling

**Location:** main.py lines 251, 303, 365, 369

**Problem:** When loading instruments from CSV:
```python
'token': int(row['instrument_token']),  # ValueError if empty/invalid
lot_size = int(data['lot_size']) if data['lot_size'] else 75  # ValueError for "abc"
strike = float(data['strike']) if data['strike'] else 0  # ValueError for invalid
```

**Fix:** Wrap in try/except with sensible defaults and skip corrupt rows.

---

### H4: WebSocket Not Closed on Shutdown

**Location:** main.py:1457-1464 (finally block)

**Problem:** When bot shuts down, WebSocket connection is not explicitly closed:
```python
finally:
    self.running = False
    # Missing: if self.ticker: self.ticker.close()
```

**Fix:** Add `self.ticker.close()` to cleanup block.

---

### H5: Boolean Type Mismatch from DB

**Location:** db.py lines 243, 287, 353

**Problem:** SQLite stores booleans as INTEGER (0/1). When loading with `Order(**dict(row))`, `paper_trade` is an int, not bool.

**Fix:** Convert explicitly when constructing dataclass from DB row.

---

### M1: Short Wait Before Option Price Fetch

**Location:** main.py:1096

**Problem:**
```python
time.sleep(1)  # Wait for price
premium = self.order_mgr.get_option_ltp(symbol)
```

**Fix:** Implement retry loop with exponential backoff (up to 5 seconds).

---

## PREVIOUSLY FIXED Issues (from v3.0 first review)

All these were identified and fixed in the previous code review:

| Issue | Status |
|-------|--------|
| Division by zero in z-score calculation | FIXED |
| Exit order failure leaves position stuck | FIXED (retry + mark_position_error) |
| Entry doesn't cleanup on failure | FIXED |
| WebSocket no auto-reconnect | FIXED |
| Slippage calculation bug in SQL | FIXED |
| Daily summary UNIQUE constraint | FIXED |
| Duplicate order records | FIXED |
| Empty exit_deadline exception | FIXED |
| Stale option price skips exit | FIXED (REST API fallback) |
| Empty instruments file | FIXED |
| VERSION constant mismatch | FIXED |
| WebSocket connection timeout | FIXED |
| Unused imports | FIXED |
| Dead code - StateManager | REMOVED |
| Dead code - Trade/State dataclasses | REMOVED |
| Unused import in db.py | FIXED |

---

## Recommendations

### Performance
- Consider connection pooling for DB operations
- Cache instrument lookups to reduce CSV reads

### Reliability
- Add position reconciliation with Kite API on startup
- Consider adding health check endpoint

### Monitoring
- Add metrics for latency tracking
- Log tick frequency to detect data gaps
