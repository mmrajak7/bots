# SCALPER - Code Review Summary

**Review Date:** 2026-01-16
**Reviewed By:** Claude Opus 4.5

---

## Executive Summary

Comprehensive code review of the Scalper trading terminal from a trader's perspective. The review identified **20 issues** across 5 severity levels, with **5 critical bugs** that could cause financial loss or unexpected behavior in production.

**Critical issues have been fixed in this review.**

---

## Issues Fixed in This Session

### 1. Order Book Time Sort Not Persisting (User Reported)
**File:** `gui/main_window.py`
**Problem:** Clicking Time column to sort descending would reset on every refresh (every 5 seconds).
**Root Cause:**
- Sorting was not enabled on the table
- `setRowCount()` clears table and loses sort state
- Time column stored display text "HH:MM" instead of sortable timestamp

**Fix:**
- Added `setSortingEnabled(True)` with default descending sort on Time column
- Created `SortableTableWidgetItem` class that sorts by UserRole data
- Store full timestamp in UserRole for proper sorting
- Save/restore sort state across refreshes via `_orders_sort_column` and `_orders_sort_order`

---

### 2. Position Limit Check Only Blocked BUY Orders
**File:** `core/order_manager.py:105-110`
**Problem:** Could exceed max position limit by opening SHORT positions (SELL orders).
**Fix:** Created `_check_position_limits_for_entry()` that intelligently detects:
- Exit orders (opposite direction) - always allowed
- Scaling orders (same direction on existing position) - allowed
- New position entries - checked against limit

---

### 3. Trailing SL Auto-Trail LTP Fetch Bug
**File:** `core/trailing_sl.py:276-293`
**Problem:** Used `pos.symbol` (string) instead of actual `instrument_token` for LTP API calls. Auto-trailing never worked.
**Fix:**
- Added `instrument_token` field to `TrailingPosition` dataclass
- Updated `add_position()` to accept and store instrument_token
- Fixed `_process_auto_trails()` to use proper token for LTP fetch
- Updated all 3 GUI call sites to pass instrument_token

---

### 4. Trail to Cost Has No Profit Check
**File:** `core/trailing_sl.py:142-159`
**Problem:** `trail_to_cost()` would move SL to entry price without verifying position is in profit. For SHORT positions where entry < LTP, this would trigger SL immediately.
**Fix:** Added profit validation:
- For LONG: Verify LTP > entry_price before allowing trail to cost
- For SHORT: Verify LTP < entry_price before allowing trail to cost
- Also verify new SL is better than current SL

---

### 5. OCO Monitor - No Retry for Failed Cancellations
**File:** `core/oco_monitor.py:176-250`
**Problem:** When SL/Target hit and cancellation of opposite order fails, the order becomes orphan with no notification.
**Fix:**
- Added `_cancel_order_with_retry()` with exponential backoff (3 attempts: 0.5s, 1s, 2s delays)
- Added Telegram notification for critical failure (orphan order)
- Updated both `_handle_sl_hit()` and `_handle_target_hit()` to use retry mechanism

---

## Remaining Issues (Not Fixed - Documented in ISSUES.md)

### HIGH Severity (5)
- SL validation LTP stale risk
- Position sync race condition with OCO monitor
- Basket order premium calculation bug
- No margin check before order placement

### MEDIUM Severity (5)
- Duplicate order prevention blocks legitimate scaling
- WebSocket order update not triggering immediate refresh
- Trail manager SL order ID sync after partial exit
- Order book ID truncation collision risk

### LOW Severity (5)
- Quote timer continues when no symbol selected
- Log panel unbounded growth
- Position tracker size not limited
- Sound alert no volume control
- Keyboard shortcuts not documented in UI

### Design Gaps (5)
- No risk-based position sizing
- No ATR-based trailing
- No intraday cutoff warning
- No profit target alert
- No order book filtering

---

## Files Modified

| File | Changes |
|------|---------|
| `gui/main_window.py` | Order book sorting, SortableTableWidgetItem class, instrument_token passing to trail manager |
| `core/order_manager.py` | `_check_position_limits_for_entry()` method |
| `core/trailing_sl.py` | `instrument_token` field, profit check in `trail_to_cost()`, LTP fetch fix |
| `core/oco_monitor.py` | `_cancel_order_with_retry()` method with exponential backoff |

---

## Files Created

| File | Purpose |
|------|---------|
| `ISSUES.md` | Complete list of all 20 issues with severity, description, and suggested fixes |
| `REVIEW_SUMMARY.md` | This summary document |

---

## Recommendations

1. **Before Production Use:**
   - Fix remaining HIGH severity issues (especially margin check and basket premium calculation)
   - Test all fixed functionality in paper trading mode

2. **Testing Priority:**
   - Test order book time sorting across multiple refreshes
   - Test position limits with both LONG and SHORT positions
   - Test auto-trailing with actual positions (requires valid instrument_token)
   - Test OCO cancellation retry with simulated failures

3. **Future Improvements:**
   - Add comprehensive logging for debugging
   - Implement position sizing based on risk
   - Add intraday cutoff warning
   - Enhance order book with status filtering

---

## Session 2 Fixes (HIGH + MEDIUM)

### HIGH Priority Fixed:
| # | Issue | Fix |
|---|-------|-----|
| 7 | SL validation LTP stale risk | Added configurable safety buffer from `ltp_buffer` config |
| 8 | Position sync race with OCO | Check order status before cancel, clean up tracker on any error |
| 9 | Basket premium calculation | Verified correct (false positive) |
| 10 | No margin check | Added `_check_margin_for_order()` with warn/block modes |

### MEDIUM Priority Fixed:
| # | Issue | Fix |
|---|-------|-----|
| 11 | Duplicate blocks scaling | Added price comparison and SCALE tag bypass |
| 12 | WebSocket order refresh | Added refresh for all status changes including pending |
| 13 | Trail manager SL sync | Added OCO monitor update after SL recreation |
| 14 | Order ID collision | Added tooltip with full ID, store full ID in UserRole |

### New Feature: ATM Strike Autocomplete
- Type "NIFTY", "NIF", "BANK", "BNF", "FIN", "MID", "SEN" to trigger
- Shows dropdown with 5 CE + 5 PE strikes around ATM
- Uses Kite spot price for ATM calculation
- Respects Monthly/Weekly expiry setting

---

## Conclusion

The Scalper trading terminal has a solid architecture. After this comprehensive review:

**Session 1 (Critical):**
1. Order book sorting now persists
2. Position limits apply to both LONG and SHORT
3. Auto-trailing can now fetch LTP properly
4. Trail to cost validates profitability first
5. OCO monitor retries failed cancellations

**Session 2 (HIGH + MEDIUM):**
6. SL validation has safety buffer for LTP movement
7. Position sync handles OCO race condition
8. Margin check before orders (configurable)
9. Duplicate detection allows scaling
10. WebSocket updates trigger immediate refresh
11. SL recreation updates all monitors
12. Order IDs show full ID on hover

**New Feature:** ATM strike autocomplete dropdown

**Remaining:** 5 LOW severity issues and 5 design gaps documented for future sprints.

**Recommendation:** Test thoroughly in paper trading mode before production deployment.
