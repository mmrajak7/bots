# Code Review Issues - Z-Score Trading Bot

## New Issues Found (Review 4 - 2025-12-19)

| ID | File | Line | Description | Severity | Status |
|----|------|------|-------------|----------|--------|
| 17 | main.py | 1095-1103 | **EXCHANGE MISMATCH**: `place_entry_order()` hardcodes `EXCHANGE_NFO` but SENSEX uses `BFO` | Critical | FIXED |
| 18 | main.py | 1159-1167 | **EXCHANGE MISMATCH**: `place_exit_order()` hardcodes `EXCHANGE_NFO` but SENSEX uses `BFO` | Critical | FIXED |
| 19 | main.py | 1216, 1256, 1277 | **EXCHANGE MISMATCH**: `get_market_depth()`, `place_limit_exit()`, `place_market_exit()` hardcode `NFO:` prefix | Critical | FIXED |
| 20 | main.py | 2060-2068 | **TYPE MISMATCH**: `alert_recovery()` receives `main.Position` but expects `db.Position` - attributes may diverge | High | FIXED |
| 21 | main.py | 1378,1425 | **VARIABLE SHADOWING**: `ce_order_id`/`pe_order_id` defined twice in `exit_straddle_smart()` | Medium | FIXED |
| 22 | main.py | 2750 | **UNUSED VARIABLE**: `all_stats` assigned but never used | Low | FIXED |
| 23 | main.py | 1077,1149 | **PAPER ORDER ID COLLISION**: Uses `int(time.time())` - two orders in same second get same ID | Low | FIXED |
| 24 | main.py | 2758 | **TRADE COUNT ERROR**: `len([...]) // 2` for straddle count ignores orphan positions | Low | DEFERRED |
| 25 | main.py | 1884-1903 | **WEBSOCKET RESUBSCRIBE**: Reconnection may not resubscribe to active straddle tokens | Medium | FIXED |
| 26 | db.py | 218, 281 | **RETURN TYPE**: `create_order()`/`create_position()` can return `None` but declared as `int` | Low | FIXED |

## Fixed Issues (Review 3 - Smart Exit - 2025-12-16)

| ID | File | Line | Description | Severity | Fix Applied |
|----|------|------|-------------|----------|-------------|
| 11 | main.py | exit_straddle_smart | **DOUBLE EXIT RISK**: modify fails -> cancel -> new market = double sell | Critical | Removed cancel+new pattern; re-check status before modify; let monitoring loop handle retries |
| 12 | main.py | exit_straddle_smart | **RACE CONDITION**: PE fills between status check and modify | Critical | Added re-check of order status immediately before any modify_to_market call |
| 13 | main.py | exit_straddle_smart | **MARKET ORDER ASSUMED FILLED**: skipped monitoring | High | Removed `ce_filled=True` assumption; all orders now monitored regardless of type |
| 14 | main.py | exit_straddle_smart | **ORPHAN ORDER**: one leg fails, other executes | High | Added cancel of successful leg if other fails to place |
| 15 | main.py | exit_straddle_smart | **PARTIAL FILL IGNORED** | Medium | Deferred - options typically fill all-or-nothing; added note for future enhancement |
| 16 | main.py | exit_straddle_smart | **TIMEOUT EXCEEDED**: 3s + 5s + 5s = 13s | Medium | Reduced verify_order timeout from 5s to 2s; max total now ~7s |

## Fixed Issues (Review 2 - 2025-12-16)

| ID | File | Line | Description | Severity | Fix Applied |
|----|------|------|-------------|----------|-------------|
| 7 | main.py | process_straddle_exit() | Partial exit success mishandled - if CE succeeds but PE fails, CE not closed in DB | Critical | Added partial success handling: close successful leg in DB even if other fails |
| 8 | main.py | main_loop (single position) | Orphan CE/PE position uses wrong price tracking (self.prices['option']) | Critical | Fixed to check position symbol and use appropriate price source (ce/pe/option) |
| 9 | main.py | place_straddle_entry_parallel() | Thread exceptions could orphan position without DB record | High | Added try/except in thread functions and timeout on result() |
| 10 | db.py | get_today_stats() | Straddle counted as 2 trades instead of 1 | Medium | Rewrote to count by trade_group_id - straddle = 1 trade |

## Fixed Issues (Review 1)

| ID | File | Line | Description | Severity | Fix Applied |
|----|------|------|-------------|----------|-------------|
| 1 | main.py | check_and_recover_position() | Straddle recovery not handled on restart | Critical | Fixed to recover both CE and PE positions |
| 2 | main.py | process_entry() | PE order failure orphans CE position | Critical | Added orphaned position tracking with DB record |
| 3 | main.py | process_straddle_exit() | No retry logic for exit orders | High | Added max_retries=2 with 2-second delay |
| 4 | main.py | Line 32 | Unused imports: List, Set | Low | Removed |
| 5 | main.py | Line 39 | Unused import: BOT_ID | Low | Removed |
| 6 | main.py | Line 274 | Unused exception variable | Low | Removed |

## Remaining Type Annotation Issues (mypy --strict)

These don't cause runtime bugs but indicate areas for stricter typing:

| ID | File | Line | Description | Severity |
|----|------|------|-------------|----------|
| T1 | main.py | 101, 116 | Optional parameters with `= None` need explicit `Optional[]` type | Info |
| T2 | main.py | 292-293 | `nse_instruments_path`/`bse_instruments_path` default None but typed as `str` | Info |
| T3 | main.py | 2764 | `inst_key` default None but typed as `str` | Info |
| T4 | main.py | various | Missing return type annotations on internal methods | Info |
| T5 | db.py | 98, 220, 353, 386, 484 | Missing return type annotations | Info |

## Deferred Items

| ID | Description | Reason |
|----|-------------|--------|
| 15 | Partial fill handling | Options typically fill all-or-nothing |
| 24 | Trade count for orphans | Complex edge case, low probability |
