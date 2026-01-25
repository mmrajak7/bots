# FIFTY Bot - Technical Design Plan

## Overview
Monthly timeframe trading bot with Telegram-based interactive approval workflow. Trades CSP signals with NIFTY weekly filter, 20% SL trailing to monthly LOW.

---

## 1. Directory Structure

```
BOTS/FIFTY/
├── config/
│   └── config.yaml                  # FIFTY configuration
├── data/
│   ├── trading.db                   # SQLite database
│   ├── kite_credentials.json        # New Kite API key/secret (user provides)
│   └── kite_access_token.json       # Generated daily for new account
├── logs/
│   └── fifty_YYYY-MM-DD.log
├── reports/                         # Temp folder for HTML (deleted after send)
├── src/
│   ├── api/                         # REUSE from CROCODILE (symlink/import)
│   ├── core/
│   │   ├── orchestrator.py          # NEW: Time-based task scheduler
│   │   ├── signal_processor.py      # NEW: CSP signal detection & dedup
│   │   ├── order_manager.py         # NEW: Order placement & monitoring
│   │   ├── position_manager.py      # NEW: Position tracking
│   │   ├── exit_manager.py          # NEW: GTT & SL management
│   │   ├── capital_manager.py       # REUSE from CROCODILE
│   │   └── invalidation_monitor.py  # NEW: Price breakdown detection
│   ├── indicators/                  # REUSE from CROCODILE
│   ├── models/
│   │   └── database.py              # NEW: FIFTY-specific schema
│   ├── telegram/
│   │   ├── bot.py                   # NEW: Interactive bot with callbacks
│   │   ├── commands.py              # NEW: /positions, /stats, /kill, etc.
│   │   ├── approval_handler.py      # NEW: Signal approval workflow
│   │   └── report_generator.py      # NEW: HTML report generation
│   └── utils/                       # REUSE from CROCODILE
├── main.py                          # Entry point - runs via cron every 5 mins
├── generate_token.py                # Token generator for new Kite account
└── requirements.txt
```

---

## 2. Database Schema (FIFTY/data/trading.db)

### Table: signal_queue
Tracks all CSP signals from detection through lifecycle.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| script | TEXT | Stock symbol (e.g., RELIANCE) |
| signal_date | DATE | Date signal first appeared in CSV |
| signal_level | REAL | Monthly SuperTrend value at detection |
| status | TEXT | pending/notified/approved/rejected/hold/invalidated/entered/expired |
| first_seen_at | DATETIME | Timestamp of first detection |
| last_notified_at | DATETIME | Last Telegram notification sent |
| user_price | REAL | User-revised entry price (if any) |
| telegram_msg_id | INTEGER | Message ID for button handling |
| notes | TEXT | Any notes |
| UNIQUE(script, strftime('%Y-%m', signal_date)) | | One signal per script per month |

### Table: open_orders
Pending GTT orders waiting for fill.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | |
| signal_id | INTEGER FK | Reference to signal_queue |
| gtt_id | TEXT | Zerodha GTT order ID |
| script | TEXT | Stock symbol |
| trigger_price | REAL | GTT trigger price (SuperTrend or user-revised) |
| quantity | INTEGER | Shares |
| capital_deployed | REAL | Amount blocked |
| placed_at | DATETIME | Order placement time |
| status | TEXT | PENDING/FILLED/CANCELLED/REJECTED/EXPIRED/INVALIDATED |
| exchange | TEXT | NSE |

### Table: open_positions
Active held positions.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | |
| signal_id | INTEGER FK | Reference to signal_queue |
| script | TEXT | Stock symbol |
| entry_price | REAL | Average fill price |
| quantity | INTEGER | Shares held |
| entry_date | DATE | Fill date |
| initial_sl | REAL | 20% below entry |
| current_sl | REAL | Current GTT trigger price |
| highest_sl | REAL | Best SL ever achieved |
| sl_movements | INTEGER | Count of SL updates |
| gtt_id | TEXT | Current GTT order ID |
| last_sl_update | DATETIME | Last GTT update time |
| capital_deployed | REAL | Amount in position |

### Table: closed_positions
Historical trades with P&L.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | |
| script | TEXT | |
| entry_price | REAL | |
| exit_price | REAL | |
| quantity | INTEGER | |
| entry_date | DATE | |
| exit_date | DATE | |
| exit_reason | TEXT | SL_HIT/MANUAL/EMERGENCY_EXIT |
| gross_pnl | REAL | (exit - entry) * qty |
| transaction_costs | REAL | ~0.111% of turnover |
| net_pnl | REAL | gross - costs |
| days_held | INTEGER | |
| sl_movements | INTEGER | Trail count |

### Table: capital_ledger
Daily snapshot of capital.

| Column | Type | Description |
|--------|------|-------------|
| date | DATE PK | |
| opening_capital | REAL | Start of day |
| deployed_capital | REAL | In positions |
| free_capital | REAL | Available |
| realized_pnl | REAL | Cumulative realized |
| monthly_pnl | REAL | Current month P&L |

### Table: gtt_update_log
Audit trail for SL changes.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | |
| position_id | INTEGER FK | |
| old_sl | REAL | |
| new_sl | REAL | |
| old_gtt_id | TEXT | |
| new_gtt_id | TEXT | |
| status | TEXT | SUCCESS/FAILED |
| timestamp | DATETIME | |

### Table: bot_state
Key-value store for bot state.

| Column | Type | Description |
|--------|------|-------------|
| key | TEXT PK | kill_switch, last_run, telegram_offset, etc. |
| value | TEXT | JSON or simple value |
| updated_at | DATETIME | |

---

## 3. Orchestrator Flow (main.py)

Cron runs `main.py` every 5 minutes. Internal logic handles time-based tasks.

```python
def main():
    # 1. Check kill switch
    if bot_state.get('kill_switch') == 'active':
        log("Kill switch active - skipping all operations")
        return

    current = get_ist_time()

    # 2. Morning startup (9:00-9:05 AM)
    if in_window(current, "09:00", "09:05"):
        morning_startup()  # Token validation, margin check, reconciliation

    # 3. Process new signals from CSV (9:15-3:30 PM)
    if is_market_hours(current):
        process_new_csp_signals()      # Read CSV, detect new, calculate ST level
        process_telegram_callbacks()    # Handle user button clicks
        send_pending_notifications()    # Send signals awaiting user response
        monitor_open_orders()           # Check for GTT triggers/fills
        monitor_positions_for_drops()   # Alert on 30% drops

    # 4. Daily hold signal re-ask (9:30-9:35 AM)
    if in_window(current, "09:30", "09:35"):
        resend_hold_signals()

    # 5. EOD GTT Update (3:50-3:55 PM, only last trading day of month)
    if in_window(current, "15:50", "15:55") and is_last_trading_day():
        update_monthly_trailing_sl()

    # 6. Same-day recovery (4:00-4:05 PM)
    if in_window(current, "16:00", "16:05"):
        run_recovery_checks()

    # 7. Daily report (4:10-4:15 PM)
    if in_window(current, "16:10", "16:15"):
        send_daily_summary()

    # 8. Weekly report (Friday 4:15 PM)
    if is_friday(current) and in_window(current, "16:15", "16:20"):
        send_weekly_report_html()

    # 9. Monthly cleanup & invalidation (last trading day 4:25 PM)
    if in_window(current, "16:25", "16:30") and is_last_trading_day():
        check_monthly_invalidations()   # Invalidate signals where close < Level
        expire_pending_orders()         # Cancel unfilled entry GTTs
        send_monthly_report_html()      # Comprehensive report
```

---

## 4. Signal Processing Flow

### 4.1 New Signal Detection
```
Every 5 mins during market hours:
1. Read op_signals.csv
2. Filter: TF = 'CSP'
3. For each signal:
   a. Check if script already in signal_queue for this month
   b. If new:
      - Fetch monthly OHLC data
      - Calculate monthly SuperTrend(10,3)
      - Get signal_level (current ST value)
      - Insert into signal_queue with status='pending'
```

### 4.2 Signal Notification
```
For signals with status='pending' not yet notified:
1. Check NIFTY weekly filter - if bearish, skip (log reason)
2. Check open positions count < 5 - if full, skip (log reason)
3. Check pending orders count < 3 - if full, skip
4. Calculate:
   - Current LTP
   - Distance from Level (%)
   - Suggested quantity (20,000 / signal_level)
   - Available capital
5. Send Telegram message with inline buttons:
   [Approve] [Reject] [Hold] [Revise Price]
6. Update signal_queue: status='notified', telegram_msg_id
```

### 4.3 User Response Handling
```
Every 5 mins:
1. Call Telegram getUpdates API
2. For each callback_query (button click):
   a. Parse callback_data: "approve_123" / "reject_123" / "hold_123" / "revise_123"
   b. Look up signal by ID
   c. Based on action:
      - APPROVE: status='approved', trigger order placement
      - REJECT: status='rejected'
      - HOLD: status='hold', will be re-asked tomorrow
      - REVISE: status='awaiting_price', send "Enter price" message
3. For text messages after REVISE:
   - Parse price
   - Store in user_price
   - status='approved', trigger order placement with user_price
```

### 4.4 Order Placement (GTT-based Entry)
```
When signal approved:
1. Determine entry price:
   - If user_price set: use user_price
   - Else: use signal_level (SuperTrend)
2. Calculate quantity:
   - position_value = 20,000 (configurable)
   - quantity = floor(position_value / entry_price)
3. Place GTT BUY order:
   - trigger_type: single
   - trigger_price: entry_price
   - order_type: LIMIT at trigger_price (or market)
   - exchange: NSE only
   - product: CNC (delivery)
   - GTT stays active until triggered, cancelled, or 1 year (auto by Zerodha)
4. Create open_orders record with gtt_id
5. Update signal_queue: status='entered'
6. Send Telegram confirmation

Benefits of GTT for entry:
- No daily re-placement needed
- Order stays active for up to 1 year
- Just need to cancel at month end or on invalidation
```

### 4.5 GTT Entry Monitoring
```
Every 5 mins during market hours:
1. Get all open_orders with status='PENDING'
2. For each GTT:
   a. Query Zerodha for GTT status
   b. If TRIGGERED (filled):
      - Proceed to position creation
   c. If CANCELLED/REJECTED:
      - Update status accordingly
      - Send Telegram alert
3. Note: No re-placement needed - GTT persists until triggered
```

### 4.6 Month-End Cleanup
```
Last trading day of month, 4:25 PM:
1. Get all open_orders with status='PENDING' (unfilled entry GTTs)
2. For each:
   - Cancel GTT via API
   - Update status = 'EXPIRED'
   - Update signal_queue status = 'expired'
3. Send Telegram summary: "X entry orders expired for [month]"
4. Next month starts fresh iteration
```

---

## 5. Position & Exit Management

### 5.1 Order Fill Detection
```
Every 5 mins:
1. For each open_order with status='PENDING':
   a. Query Zerodha API for GTT status
   b. If TRIGGERED (filled):
      - Create open_position with:
        - entry_price = fill price
        - initial_sl = entry_price * 0.80 (20% below)
        - current_sl = initial_sl
      - Place GTT SELL at initial_sl (protective stop)
      - Verify GTT exists
      - Send Telegram confirmation
   c. If REJECTED/CANCELLED:
      - Update order status
      - Release capital
      - Send Telegram alert
```

### 5.2 Monthly SL Trailing
```
Last trading day of month, 3:50 PM:
1. For each open_position:
   a. Fetch this month's LOW (from monthly candle)
   b. Calculate new_sl = monthly_low
   c. If new_sl > current_sl:
      - Cancel old GTT
      - Place new GTT at new_sl
      - Verify GTT exists
      - Update position: current_sl, highest_sl, sl_movements++
      - Log in gtt_update_log
   d. Else:
      - No change (SL only tightens, never widens)
2. Send Telegram summary with all SL updates
```

### 5.3 30% Drop Monitoring
```
Every 5 mins during market hours:
1. For each open_position:
   a. Get current LTP
   b. Calculate drop = (entry_price - LTP) / entry_price * 100
   c. If drop >= 30%:
      - Send Telegram alert with inline buttons:
        [HODL] [EXIT NOW]
      - Store alert_msg_id for callback handling
2. On EXIT callback:
   - Place MARKET sell order
   - On fill: Close position, calculate P&L, update closed_positions
   - Send confirmation
3. On HODL callback:
   - Log decision
   - Don't alert again for same position today
```

### 5.4 Invalidation Monitoring
```
ONLY on last trading day of month (when monthly candle completes):
1. For signals with status in ('pending', 'hold', 'notified', 'entered'):
   a. Fetch completed month's CLOSE price (monthly candle now finalized)
   b. Fetch signal_level (monthly SuperTrend)
   c. If monthly_close < signal_level:
      - Update status = 'invalidated'
      - Send Telegram: "{script} invalidated - monthly close below Level"

2. For entry GTTs (unfilled buy orders):
   a. Check if signal invalidated
   b. If invalidated: Cancel GTT, update order status='INVALIDATED'
   c. At month end: Cancel all unfilled entry GTTs, mark as 'EXPIRED'

NOTE: Invalidation ONLY happens when monthly candle CLOSES below Level.
      Intraday/daily breaks don't invalidate - only monthly close matters.
```

---

## 6. Telegram Bot Implementation

### 6.1 Architecture
- No long-running process needed
- Use `getUpdates` API each cron run to fetch new messages/callbacks
- Track `update_offset` in bot_state to avoid processing same update twice
- Inline keyboards for button interactions

### 6.2 Message Format for New Signal
```

RELIANCE @ 2,450.00
LTP: 2,480.00 ( +1.2% )
Qty: 8 shares / Value: 19,600

Capital Available: 80,000

[Approve] [Reject] [Hold] [Revise]
```

### 6.3 Commands
| Command | Description |
|---------|-------------|
| /positions | List all open positions with current P&L |
| /pending | List pending approvals and hold signals |
| /stats | Win rate, avg P&L, total trades |
| /drawdown | Current drawdown from peak |
| /report | Generate and send today's summary |
| /kill | Activate kill switch (full stop) |
| /resume | Deactivate kill switch |
| /capital | Show capital allocation |
| /help | List all commands |

### 6.4 30% Drop Alert
```
ALERT: Position Drop > 30%

RELIANCE
Entry: 2,500
Current: 1,700
Drop: -32%

[HODL] [EXIT NOW]
```

---

## 7. Configuration (config/config.yaml)

```yaml
# FIFTY Bot Configuration

bot:
  instance_id: "fifty_main"
  log_level: "INFO"

# Kite API for reading market data (shared with CROCODILE)
kite_read:
  token_path: "../data/kite_access_token.json"  # Shared token

# Kite API for trading (new account)
kite_trade:
  credentials_path: "data/kite_credentials.json"
  token_path: "data/kite_access_token.json"
  order_tag: "FIFTY"

# Telegram
telegram:
  bot_token: ""  # User provides
  chat_id: ""    # User provides

# Signal source
signals:
  file_path: "C:/Users/mail2/Documents/Projects/Investment/op_signals.csv"
  filter_tf: "CSP"

# Trading parameters
trading:
  initial_capital: 100000
  per_trade_amount: 20000
  max_positions: 5
  max_pending_orders: 3
  initial_sl_percent: 20  # Start with 20% SL

# Risk management
risk:
  emergency_drop_percent: 30  # Alert threshold

# Schedule (IST times for orchestrator)
schedule:
  morning_startup: "09:00"
  market_open: "09:15"
  market_close: "15:30"
  hold_renotify: "09:30"
  eod_sl_update: "15:50"
  recovery: "16:00"
  daily_report: "16:10"
  weekly_report: "16:15"
  monthly_cleanup: "16:25"

# Database
database:
  path: "data/trading.db"
```

---

## 8. Code Reuse Strategy

### Direct Reuse (import from CROCODILE)
```python
# In FIFTY/src/indicators/__init__.py
import sys
sys.path.insert(0, '../CROCODILE/src')

from indicators.supertrend_calculator import SuperTrendCalculator
from indicators.nifty_trend_filter import is_nifty_weekly_bullish
from indicators.nifty_data_fetcher import NiftyDataFetcher
```

### Copy with Modifications
- `capital_manager.py` - Modify for FIFTY's capital allocation
- `api_resilience.py` - Copy as-is
- `broker_adapter.py` - Copy as-is
- `kite_api_adapter.py` - Copy as-is, modify for dual-token usage

### New Development
- `orchestrator.py` - Time-based task scheduler
- `signal_processor.py` - CSP signal handling
- `telegram/bot.py` - Interactive bot
- `telegram/approval_handler.py` - Approval workflow
- `database.py` - FIFTY schema

---

## 9. Dual Kite Token Architecture

```python
class DualKiteClient:
    def __init__(self, read_token_path, trade_token_path, trade_credentials_path):
        # For reading historical data - use shared token
        self.read_client = KiteConnect(api_key=READ_API_KEY)
        self.read_client.set_access_token(read_token)

        # For trading - use new account
        self.trade_client = KiteConnect(api_key=TRADE_API_KEY)
        self.trade_client.set_access_token(trade_token)

    def historical_data(self, instrument, from_date, to_date, interval):
        return self.read_client.historical_data(...)

    def place_order(self, **kwargs):
        return self.trade_client.place_order(...)

    def place_gtt(self, **kwargs):
        return self.trade_client.place_gtt(...)

    def get_gtt(self, gtt_id):
        return self.trade_client.get_gtt(gtt_id)

    def orders(self):
        return self.trade_client.orders()

    def positions(self):
        return self.trade_client.positions()
```

---

## 10. Implementation Order

### Phase 1: Foundation
1. Create directory structure
2. Copy/setup reusable modules from CROCODILE
3. Create database schema and models
4. Create config.yaml template
5. Create generate_token.py for new Kite account

### Phase 2: Core Signal Processing
6. Implement signal_processor.py (CSV reading, dedup, ST calculation)
7. Implement orchestrator.py (time-based task runner)
8. Test signal detection flow

### Phase 3: Telegram Bot
9. Implement telegram/bot.py (getUpdates, callbacks)
10. Implement approval_handler.py (button handling)
11. Implement commands.py (/positions, /stats, etc.)
12. Test full approval workflow

### Phase 4: Trading
13. Implement order_manager.py (GTT orders, fill detection)
14. Implement position_manager.py (position tracking)
15. Implement exit_manager.py (GTT, SL trailing)
16. Implement DualKiteClient

### Phase 5: Monitoring & Alerts
17. Implement invalidation_monitor.py
18. Implement 30% drop detection
19. Implement kill switch

### Phase 6: Reporting
20. Implement report_generator.py (HTML reports)
21. Implement weekly/monthly report logic
22. Add report cleanup (delete after send)

### Phase 7: Testing & Refinement
23. End-to-end testing with test mode
24. Edge case handling
25. Documentation

---

## 11. Verification Plan

### Manual Testing Checklist
1. [ ] Token generation for new Kite account works
2. [ ] Signal detection from CSV correctly filters CSP
3. [ ] NIFTY weekly filter blocks signals when bearish
4. [ ] Telegram notification sent with correct analysis
5. [ ] Approve button places GTT order at SuperTrend
6. [ ] Revise flow accepts custom price
7. [ ] Hold signal re-asked next day
8. [ ] GTT fill creates position with 20% SL GTT
9. [ ] Monthly SL trailing updates on last trading day
10. [ ] 30% drop triggers alert with HODL/EXIT buttons
11. [ ] EXIT places market order
12. [ ] Kill switch stops all operations
13. [ ] /positions shows correct data
14. [ ] HTML reports generated and sent
15. [ ] Reports deleted after sending
16. [ ] Monthly invalidation only triggers on monthly close below Level

### Test Mode
- Set `trading.test_mode: true` in config
- Uses 1 quantity for all orders
- Marks all Telegram messages with [TEST] prefix

---

## 12. Files to Create

| File | Lines (est.) | Priority |
|------|--------------|----------|
| `main.py` | 150 | P0 |
| `src/models/database.py` | 200 | P0 |
| `src/core/orchestrator.py` | 250 | P0 |
| `src/core/signal_processor.py` | 300 | P0 |
| `src/telegram/bot.py` | 200 | P0 |
| `src/telegram/approval_handler.py` | 250 | P0 |
| `src/telegram/commands.py` | 200 | P1 |
| `src/core/order_manager.py` | 250 | P1 |
| `src/core/position_manager.py` | 200 | P1 |
| `src/core/exit_manager.py` | 300 | P1 |
| `src/core/invalidation_monitor.py` | 150 | P1 |
| `src/telegram/report_generator.py` | 250 | P2 |
| `config/config.yaml` | 80 | P0 |
| `generate_token.py` | 100 | P0 |
| `requirements.txt` | 20 | P0 |

---

## 13. Required Credentials (User to Provide)

| Item | Where to Configure |
|------|-------------------|
| Telegram Bot Token | `config/config.yaml` → telegram.bot_token |
| Telegram Chat ID | `config/config.yaml` → telegram.chat_id |
| New Kite API Key | `data/kite_credentials.json` |
| New Kite API Secret | `data/kite_credentials.json` |

---

## 14. Risk Considerations

1. **Entry GTT not triggering**: Price may not reach SuperTrend level
   - Mitigation: GTT stays active until month end, user can revise price
   - Month-end cleanup cancels unfilled GTTs

2. **Missed callbacks**: If user clicks button but cron doesn't run
   - Mitigation: Callbacks stored by Telegram until fetched (up to 24h)

3. **GTT verification failure**: Same as CROCODILE
   - Mitigation: Retry logic with exponential backoff, critical alerts

4. **Dual token expiry**: Both tokens need daily refresh
   - Mitigation: Morning startup validates both tokens, alerts if expired

5. **NSE-only limitation**: Some CSP signals may not be on NSE
   - Mitigation: Log and skip, alert user of skipped signals

6. **GTT limits**: Zerodha has GTT limit (typically 20-50 active)
   - Mitigation: With max 5 positions + max 5 entry GTTs, should be fine
   - But user may have other GTTs from other bots

---

## 15. Key Design Decisions Summary

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Entry orders | GTT (not LIMIT) | No daily re-placement needed |
| Exchange | NSE only | Simpler, most CSP stocks on NSE |
| Telegram UX | Inline buttons | Cleaner, easier to track |
| Hold signals | Re-ask daily until invalidated | Maximizes entry opportunities |
| SL strategy | 20% initial, trail to monthly LOW | Balances protection with trend following |
| Invalidation | Only on monthly close | Intraday breaks don't matter, only monthly candle close |
| Kill switch | Full stop | Safety first |
| Reports | HTML, delete after send | Clean, no server clutter |
