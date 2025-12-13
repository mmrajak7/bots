# Code Review - Issues Found

## Critical Issues

### 1. Exit Order Failure Leaves Position Stuck
- **File**: main.py, line 1233-1235
- **Description**: If exit order fails, we return early without marking position as ERROR. Position stays OPEN and will keep trying to exit, potentially getting stuck in infinite retry.
- **Severity**: Critical
- **Fix**: Mark position with error status or implement retry limit

### 2. Division by Zero in Z-Score Calculation
- **File**: main.py, line 634
- **Description**: If spot price is 0 (before first tick arrives), `basis_pct = (active_basis / spot) * 100` will raise ZeroDivisionError.
- **Severity**: Critical
- **Fix**: Add `if spot <= 0: return 0.0, 0.0, "CURRENT", 0.0` guard

### 3. Process Entry Doesn't Cleanup on Failure
- **File**: main.py, lines 1162-1175
- **Description**: If order placement fails after subscribing to option WebSocket, option_token/option_symbol remain set but no position exists. This corrupts state.
- **Severity**: Critical
- **Fix**: Clear option_token/option_symbol on failure

### 4. WebSocket Has No Auto-Reconnect
- **File**: main.py, lines 1083-1086
- **Description**: When WebSocket closes, `ws_connected` is set to False but there's no reconnection logic. Main loop continues without price data, causing the bot to hang waiting for prices.
- **Severity**: Critical
- **Fix**: Implement reconnection with exponential backoff

---

## High Severity Issues

### 5. Slippage Calculation Bug in SQL
- **File**: db.py, line 215
- **Description**: `slippage = fill_price - expected_price` in SQL SET clause doesn't work as intended. SQLite doesn't evaluate column references this way.
- **Severity**: High
- **Fix**: Use parameterized value: `slippage = ?` with calculated value

### 6. Daily Summary UNIQUE Constraint Missing bot_id
- **File**: db.py, line 159
- **Description**: `trade_date TEXT UNIQUE` doesn't include bot_id. If multiple bots run same day, INSERT OR REPLACE will overwrite each other's summaries.
- **Severity**: High
- **Fix**: Change to composite unique constraint: `UNIQUE(bot_id, trade_date)`

### 7. Stale Option Price Skips Exit Check
- **File**: main.py, lines 1382-1397
- **Description**: If `prices['option'] == 0` (no tick received), exit check is skipped entirely. Position could miss SL/target if option ticks stop flowing.
- **Severity**: High
- **Fix**: Use REST API fallback when option price is stale

### 8. Duplicate Order Record on Live Order Failure
- **File**: main.py, lines 838-839, 854-858
- **Description**: Order record is created at line 839 before Kite API call. If API call raises exception, another order record is created at line 858 with ERROR status, resulting in duplicate.
- **Severity**: High
- **Fix**: Don't create order record twice - update existing record on exception

### 9. Database Path Creation Fails for Simple Filename
- **File**: db.py, line 87
- **Description**: `os.path.dirname("zscore.db")` returns empty string. `os.makedirs('', exist_ok=True)` may fail on some systems.
- **Severity**: High
- **Fix**: Check if dirname is non-empty before makedirs

---

## Medium Severity Issues

### 10. Charges Calculation Assumes 1 Lot Per Trade
- **File**: main.py, line 1307
- **Description**: `num_lots = trades` assumes 1 lot per trade. If max_lots > 1, charges are underestimated.
- **Severity**: Medium
- **Fix**: Calculate actual lots from DB position data

### 11. Empty exit_deadline Causes Exception
- **File**: main.py, line 704
- **Description**: If `position.exit_deadline` is empty string, `datetime.fromisoformat("")` raises ValueError.
- **Severity**: Medium
- **Fix**: Validate exit_deadline before parsing

### 12. get_today_stats Counts Open Positions
- **File**: db.py, line 338
- **Description**: `COUNT(*)` includes open positions in total_trades. This could allow more entries than max_trades_per_day limit.
- **Severity**: Medium
- **Fix**: Only count closed positions for trade limit, or exclude open from count

### 13. Empty Instruments File Causes AttributeError
- **File**: main.py, lines 169-177
- **Description**: If instruments file exists but is empty, `next(reader, None)` returns None, then `first_row.get('fetch_date')` fails with AttributeError.
- **Severity**: Medium
- **Fix**: Check `if first_row is not None and first_row.get(...)`

### 14. VERSION Constant Mismatch
- **File**: main.py, line 101 vs line 19
- **Description**: Docstring says "Version: 3.0" but VERSION constant is "2.0.0"
- **Severity**: Medium
- **Fix**: Update VERSION to "3.0.0"

### 15. WebSocket Connection Timeout Not Handled
- **File**: main.py, lines 1108-1112
- **Description**: If WebSocket doesn't connect in 10 seconds, bot proceeds without data. Should fail or alert.
- **Severity**: Medium
- **Fix**: Raise exception or send Telegram alert if WS doesn't connect

---

## Low Severity Issues

### 16. Unused Imports
- **File**: main.py
- **Description**:
  - Line 29: `import threading` - unused
  - Line 30: `time as dt_time` - unused
  - Line 34: `from pathlib import Path` - unused
- **Severity**: Low
- **Fix**: Remove unused imports

### 17. Dead Code - StateManager Class
- **File**: main.py, lines 520-603
- **Description**: StateManager class is defined but never instantiated. All state management now uses database.
- **Severity**: Low
- **Fix**: Remove StateManager class

### 18. Dead Code - Trade and State Dataclasses
- **File**: main.py, lines 124-148
- **Description**: Trade and State dataclasses are unused - replaced by DB classes.
- **Severity**: Low
- **Fix**: Remove unused dataclasses

### 19. Dead Code - SignalEngine.should_enter Method
- **File**: main.py, lines 657-692
- **Description**: `should_enter` method is never called - replaced by `_check_entry_conditions`.
- **Severity**: Low
- **Fix**: Remove unused method

### 20. Dead Code - _log_trade_csv Method
- **File**: main.py, lines 1272-1286
- **Description**: `_log_trade_csv` method is defined but never called.
- **Severity**: Low
- **Fix**: Remove if not needed, or keep for CSV backup

### 21. Unused Import in db.py
- **File**: db.py, line 11
- **Description**: `import json` is imported but never used.
- **Severity**: Low
- **Fix**: Remove unused import

### 22. NSE Spot Type Mismatch
- **File**: main.py, lines 216-228
- **Description**: NSE instruments are written with `instrument_type: 'INDEX'` but actual "NIFTY 50" has type 'EQ'.
- **Severity**: Low
- **Fix**: Keep original instrument_type from API

---

## Recommendations

### Security
- No hardcoded secrets found
- Credentials loaded from external file
- No SQL injection risks (parameterized queries used)

### Performance
- Consider caching DB connections instead of opening/closing per operation
- Add connection pooling for high-frequency operations

### Reliability
- Add WebSocket auto-reconnect with exponential backoff
- Add heartbeat logging to detect stale state
- Consider adding position reconciliation with Kite API on startup

### Monitoring
- Add metrics for latency tracking
- Log tick frequency to detect data gaps
