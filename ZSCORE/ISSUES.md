# Code Review Issues - Z-Score Trading Bot

## Fixed Issues (Review 3 - Smart Exit - 2025-12-16)

| ID | File | Line | Description | Severity | Fix Applied |
|----|------|------|-------------|----------|-------------|
| 11 | main.py | exit_straddle_smart | **DOUBLE EXIT RISK**: modify fails → cancel → new market = double sell | Critical | Removed cancel+new pattern; re-check status before modify; let monitoring loop handle retries |
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

## Remaining Type Annotation Issues (mypy)

These don't cause runtime bugs but indicate areas for stricter typing:

| ID | File | Line | Description | Severity |
|----|------|------|-------------|----------|
| T1 | main.py | 99, 114, 158 | Optional parameters need explicit Optional[] | Info |
| T2 | main.py | 163, 664, 665 | Missing type annotations | Info |
| T3 | main.py | Various | None-initialized variables used after assignment | Info |
| T4 | db.py | 209, 272 | Return type mismatch (Optional vs non-Optional) | Info |
