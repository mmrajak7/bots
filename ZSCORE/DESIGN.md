# Live Trading System Design - Basis Z-Score Strategy v3.0

## Overview

Real-time trading bot for NIFTY basis z-score scalping strategy with:
- SQLite database for multi-bot safe position tracking
- Auto-detection of futures symbols and lot sizes
- WebSocket with auto-reconnect + REST API fallback
- Comprehensive error handling and retry logic

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                      CONFIG.JSON                              │
│  ├── credentials.path  (kite_access_token.json)              │
│  ├── data_dir          (shared data folder)                  │
│  ├── instruments       (spot_symbol, underlying, min_dte)    │
│  ├── strategy          (z_threshold, basis_min, etc.)        │
│  ├── risk              (max_trades, max_loss, max_lots)      │
│  ├── paper_trade       (true/false)                          │
│  └── telegram          (bot_token, chat_id)                  │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                   SQLite DATABASE                             │
│  zscore_trades.db                                            │
│  ├── orders     (order tracking with slippage)               │
│  ├── positions  (open/closed positions with P&L)             │
│  └── daily_summary (daily stats per bot_id)                  │
│                                                               │
│  BOT_ID = "ZSCORE_V1" (unique identifier for this bot)       │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                    MAIN PROCESS                               │
│                                                               │
│  ┌────────────────┐    ┌────────────────┐                    │
│  │  WebSocket     │───▶│  Data Buffer   │                    │
│  │  (Auto-reconnect)   │  (Last 20 mins)│                    │
│  └────────────────┘    └───────┬────────┘                    │
│         │                      │                              │
│         │ REST fallback        ▼                              │
│         │              ┌────────────────────┐                 │
│         └─────────────▶│  Signal Engine     │                 │
│                        │  - Calc Z-score    │                 │
│                        │  - Check conditions│                 │
│                        └─────────┬──────────┘                 │
│                                  │                            │
│              ┌───────────────────┴───────────────┐            │
│              ▼                                   ▼            │
│    ┌─────────────────┐                ┌─────────────────┐    │
│    │  Entry Logic    │                │  Exit Logic     │    │
│    │  (DB check)     │                │  (Retry x2)     │    │
│    └────────┬────────┘                └────────┬────────┘    │
│             │                                  │              │
│             └──────────────┬───────────────────┘              │
│                            ▼                                  │
│                 ┌─────────────────────┐                       │
│                 │  Order Manager      │                       │
│                 │  - DB tracking      │                       │
│                 │  - Margin check     │                       │
│                 │  - Order verify     │                       │
│                 └─────────────────────┘                       │
└──────────────────────────────────────────────────────────────┘
```

---

## Database Schema

### Orders Table
```sql
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    order_id TEXT,           -- Kite order ID
    order_type TEXT,         -- ENTRY or EXIT
    symbol TEXT,
    exchange TEXT,
    qty INTEGER,
    side TEXT,               -- BUY or SELL
    order_status TEXT,       -- PENDING, COMPLETE, REJECTED, ERROR
    expected_price REAL,
    fill_price REAL,
    slippage REAL,
    created_at TEXT,
    updated_at TEXT,
    error_message TEXT,
    paper_trade INTEGER
)
```

### Positions Table
```sql
CREATE TABLE positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    trade_date TEXT,
    symbol TEXT,
    instrument_token INTEGER,
    qty INTEGER,
    lot_size INTEGER,
    entry_order_id TEXT,
    exit_order_id TEXT,
    entry_price REAL,
    exit_price REAL,
    entry_time TEXT,
    exit_time TEXT,
    entry_spot REAL,
    exit_spot REAL,
    entry_z_score REAL,
    entry_basis REAL,
    fut_used TEXT,           -- CURRENT or NEXT
    stop_loss REAL,
    target REAL,
    exit_deadline TEXT,
    exit_reason TEXT,
    pnl REAL,
    pnl_pct REAL,
    status TEXT,             -- OPEN, CLOSED, ERROR
    paper_trade INTEGER
)
```

### Daily Summary Table
```sql
CREATE TABLE daily_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    total_trades INTEGER,
    wins INTEGER,
    losses INTEGER,
    gross_pnl REAL,
    charges REAL,
    net_pnl REAL,
    max_drawdown REAL,
    paper_trade INTEGER,
    UNIQUE(bot_id, trade_date)  -- Multi-bot safe
)
```

---

## Auto-Detection Features

### Futures Auto-Detection
```python
# No manual updates needed - bot automatically:
1. Fetches all NFO instruments from Kite API on startup
2. Filters for NIFTY*FUT symbols
3. Sorts by expiry date
4. Uses nearest expiry as current month
5. Uses second nearest as next month

# Example log output:
"Auto-detected futures - Current: NIFTY25DECFUT (exp: 2025-12-26, lot: 75),
 Next: NIFTY25JANFUT (exp: 2026-01-30)"
```

### Lot Size Auto-Detection
```python
# Lot size read from instruments file for each option:
option = inst_mgr.find_atm_option(spot_price)
lot_size = option['lot_size']  # e.g., 75 for NIFTY
qty = lot_size * config['risk']['max_lots']
```

---

## Z-Score Calculation

```python
# Every tick received:
1. current_basis = current_fut - spot
2. next_basis = next_fut - spot
3. active_basis = current_basis if >= 250 else next_basis
4. basis_pct = (active_basis / spot) * 100
5. Store in 20-minute rolling buffer (1 per minute)
6. z_score = (basis_pct - mean) / std

# Guard against division by zero:
if spot <= 0:
    return 0.0, 0.0, "CURRENT", 0.0
```

---

## Entry Conditions (ALL must be true)

```python
def _check_entry_conditions():
    # 1. No open position (from DB)
    if db.get_open_position():
        return False

    # 2. Daily limits not hit (from DB stats)
    stats = db.get_today_stats()
    if stats['total_trades'] >= max_trades_per_day:
        return False
    if stats['gross_pnl'] <= -max_daily_loss:
        return False

    # 3. Within trading hours
    if now.hour not in [13, 14]:  # 1 PM - 3 PM
        return False

    # 4. Z-score threshold met
    threshold = 2.5 if fut_used == "CURRENT" else 3.0
    if z_score < threshold:
        return False

    # 5. Basis minimum met
    if basis < 250:
        return False

    return True
```

---

## Exit Conditions (ANY triggers)

```python
def should_exit(position, current_premium, z_score):
    # 1. Time exit (with validation)
    if position.exit_deadline:
        deadline = datetime.fromisoformat(position.exit_deadline)
        if now >= deadline:
            return True, "TIME"

    # 2. Target hit (+35%)
    if current_premium >= position.target:
        return True, "TARGET"

    # 3. Stop loss hit (-25%)
    if current_premium <= position.stop_loss:
        return True, "STOP_LOSS"

    # 4. Z-score reversion
    if z_score < 0:
        return True, "Z_REVERT"

    return False, ""
```

---

## Error Handling

### Exit Order Retry Logic
```python
def process_exit(db_pos, reason, current_premium):
    max_retries = 2
    for attempt in range(max_retries):
        success, fill_price, order_id = place_exit_order(...)
        if success:
            break
        if attempt < max_retries - 1:
            time.sleep(2)  # Wait before retry

    if not success:
        # Mark position as error to prevent infinite retry
        db.mark_position_error(db_pos.id, f"EXIT_FAILED_{reason}")
        telegram.alert_error("MANUAL INTERVENTION REQUIRED")
        return
```

### WebSocket Auto-Reconnect
```python
# In main_loop, every 30 seconds:
if not ws_connected:
    ws_reconnect_attempts += 1
    if ws_reconnect_attempts <= 5:
        reconnect_websocket()
    else:
        # Fall back to REST API for prices
        logging.error("WebSocket failed, using REST API")
```

### Stale Option Price Fallback
```python
# During exit check:
if prices['option'] <= 0:
    # Try REST API
    rest_price = order_mgr.get_option_ltp(option_symbol)
    if rest_price:
        current_premium = rest_price
```

---

## Order Flow

### Entry Order
```
1. Find ATM option → get symbol, token, lot_size
2. Subscribe to option WebSocket
3. Get premium via LTP
4. Check margin (live mode only)
5. Place order → create DB record
6. Verify order completion
7. Create position in DB
8. Send Telegram alert

On failure at step 5+:
- Cleanup option_token/option_symbol
- Don't create position
```

### Exit Order
```
1. Place exit order (with retry x2)
2. Verify completion
3. Close position in DB (calculates P&L)
4. Check daily loss limit
5. Send Telegram alert
6. Unsubscribe option WebSocket

On failure after 2 retries:
- Mark position as ERROR in DB
- Send MANUAL INTERVENTION alert
- Don't keep retrying
```

---

## Recovery Scenarios

### Scenario 1: Bot restarts with open position
```
1. Query DB for OPEN position
2. If found:
   - Look up option token from instruments
   - Subscribe to WebSocket
   - Resume exit monitoring
3. Send Telegram "POSITION RECOVERED" alert
```

### Scenario 2: WebSocket disconnects during position
```
1. Detect disconnect in main_loop (every 30s check)
2. Attempt reconnect (max 5 attempts)
3. If still disconnected: Use REST API for prices
4. Continue exit monitoring via REST fallback
```

### Scenario 3: Exit order fails
```
1. Retry once after 2 seconds
2. If still fails:
   - Mark position as ERROR in DB
   - Send MANUAL INTERVENTION alert
   - Stop trying (prevent infinite loop)
3. Requires manual close via Zerodha app
```

---

## Config File Template

```json
{
  "credentials": {
    "path": "C:/Users/.../BOTS/data/kite_access_token.json"
  },
  "data_dir": "C:/Users/.../BOTS/data",
  "instruments": {
    "spot_symbol": "NIFTY 50",
    "underlying": "NIFTY",
    "min_dte": 3
  },
  "strategy": {
    "z_threshold": 2.5,
    "z_threshold_next_month": 3.0,
    "min_basis_current": 250,
    "min_basis_next": 250,
    "lookback_minutes": 20,
    "holding_minutes": 5,
    "direction": "LONG",
    "valid_hours": [13, 14]
  },
  "risk": {
    "max_trades_per_day": 4,
    "max_daily_loss": 3000,
    "max_lots": 1,
    "stop_loss_pct": 0.25,
    "target_pct": 0.35
  },
  "trading_hours": {
    "start": "13:00",
    "end": "15:15"
  },
  "paper_trade": true,
  "telegram": {
    "enabled": true,
    "bot_token": "YOUR_BOT_TOKEN",
    "chat_id": "YOUR_CHAT_ID"
  },
  "logging": {
    "level": "INFO",
    "console": true,
    "file": true
  }
}
```

---

## File Structure

```
BOTS/
├── data/                         # Shared data folder
│   ├── kite_access_token.json    # Kite credentials
│   ├── holiday_calendar.json     # Market holidays
│   ├── nfo_instruments.csv       # Cached instruments (auto-refreshed)
│   ├── zscore_trades.db          # SQLite database
│   └── logs/
│       └── zscore/
│           ├── YYYY-MM-DD.log
│           └── trades.csv
│
└── ZSCORE/                       # Bot code
    ├── main.py                   # Main trading bot (v3.0)
    ├── db.py                     # Database module
    ├── config.json               # Configuration
    ├── start_bot.sh              # Cron start script
    ├── stop_bot.sh               # Cron stop script
    ├── DESIGN.md                 # This document
    ├── DEPLOYMENT_GUIDE.md       # Deployment guide
    ├── ISSUES.md                 # Code review issues
    └── REVIEW_SUMMARY.md         # Code review summary
```

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2025-12-12 | Initial implementation with state.json |
| 2.0 | 2025-12-12 | Added auto-detection of futures |
| 3.0 | 2025-12-13 | SQLite DB, error handling, code review fixes |

### v3.0 Changes
- Replaced state.json with SQLite database
- Added BOT_ID for multi-bot isolation
- Auto-reconnect WebSocket with REST fallback
- Exit order retry (2 attempts) with ERROR marking
- Entry cleanup on order failure
- Division by zero guard in z-score
- Comprehensive code review and bug fixes
