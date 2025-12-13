# Z-Score Trading Bot v3.0 - Flow Logic & Deployment Guide

## Part 1: Flow Logic

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         STARTUP PHASE                                │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐          │
│  │ Load Config  │───▶│ Init Kite    │───▶│ Fetch/Cache  │          │
│  │ (config.json)│    │ (credentials)│    │ Instruments  │          │
│  └──────────────┘    └──────────────┘    └──────┬───────┘          │
│                                                  │                   │
│  ┌──────────────┐    ┌──────────────┐    ┌──────▼───────┐          │
│  │ Check DB for │◀───│ Start        │◀───│ Auto-Detect  │          │
│  │ Open Position│    │ WebSocket    │    │ Futures      │          │
│  └──────────────┘    └──────────────┘    └──────────────┘          │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         MAIN LOOP (every 0.5s)                       │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ CHECK WEBSOCKET (every 30s)                                   │   │
│  │ If disconnected → Reconnect (max 5 attempts) → REST fallback  │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                              │                                       │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                    RECEIVE TICK DATA                          │   │
│  │  WebSocket ──▶ Spot Price, Current Fut Price, Next Fut Price │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                              │                                       │
│                              ▼                                       │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                   CALCULATE Z-SCORE                           │   │
│  │  1. current_basis = current_fut - spot                        │   │
│  │  2. next_basis = next_fut - spot                              │   │
│  │  3. active_basis = current if >= 250 else next                │   │
│  │  4. basis_pct = (active_basis / spot) * 100                   │   │
│  │  5. Store in 20-minute rolling buffer (1 per minute)          │   │
│  │  6. z_score = (basis_pct - mean) / std                        │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                              │                                       │
│              ┌───────────────┴───────────────┐                      │
│              ▼                               ▼                      │
│  ┌─────────────────────┐         ┌─────────────────────┐           │
│  │   NO POSITION?      │         │   HAS POSITION?     │           │
│  │   (Check DB)        │         │   (Check DB)        │           │
│  │   → Check Entry     │         │   → Check Exit      │           │
│  └──────────┬──────────┘         └──────────┬──────────┘           │
│             │                               │                       │
│             ▼                               ▼                       │
│  ┌─────────────────────┐         ┌─────────────────────┐           │
│  │   ENTRY CONDITIONS  │         │   EXIT CONDITIONS   │           │
│  │   (ALL must pass)   │         │   (ANY triggers)    │           │
│  │   + Order to DB     │         │   + Retry x2        │           │
│  └─────────────────────┘         └─────────────────────┘           │
└─────────────────────────────────────────────────────────────────────┘
```

---

### Detailed Flow

#### 1. STARTUP SEQUENCE

```
[12:00 PM - Cron triggers start_bot.sh]
     │
     ▼
┌─────────────────────────────────────┐
│ 1. Load config.json                 │
│    - credentials path               │
│    - data_dir                       │
│    - strategy params                │
│    - telegram config                │
└─────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────┐
│ 2. Initialize Kite Connect          │
│    - Read access token from file    │
│    - Create KiteConnect instance    │
└─────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────┐
│ 3. Initialize SQLite Database       │
│    - Create zscore_trades.db        │
│    - Tables: orders, positions,     │
│      daily_summary                  │
│    - BOT_ID = "ZSCORE_V1"          │
└─────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────┐
│ 4. Fetch Instruments (if needed)    │
│    - Check if nfo_instruments.csv   │
│      has today's date               │
│    - If stale: fetch from Kite API  │
│    - Save ~100k instruments to CSV  │
└─────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────┐
│ 5. Auto-Detect Futures              │
│    - Scan for NIFTY*FUT symbols     │
│    - Sort by expiry date            │
│    - Current = nearest expiry       │
│    - Next = second nearest          │
│    - Get lot_size from instruments  │
│    Example output:                  │
│    "Current: NIFTY25DECFUT (lot:75)"│
│    "Next: NIFTY25JANFUT (lot:75)"   │
└─────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────┐
│ 6. Check DB for Open Position       │
│    - Query: status = 'OPEN'         │
│    - If found: Resume exit          │
│      monitoring                     │
│    - Send recovery alert            │
└─────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────┐
│ 7. Start WebSocket (15s timeout)    │
│    - Connect to Kite ticker         │
│    - Subscribe to:                  │
│      • NIFTY 50 (spot)              │
│      • Current month futures        │
│      • Next month futures           │
│    - If timeout: Alert + continue   │
└─────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────┐
│ 8. Send Telegram Startup Alert      │
│    "Z-Score Bot Started"            │
│    "Mode: PAPER", Version: 3.0.0    │
└─────────────────────────────────────┘
     │
     ▼
[ENTER MAIN LOOP]
```

---

#### 2. Z-SCORE CALCULATION

```
Every tick received:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

INPUT:
  spot = 24500.00
  current_fut = 24620.00
  next_fut = 24750.00

STEP 0: Guard against division by zero
  IF spot <= 0:
      return 0.0, 0.0, "CURRENT", 0.0

STEP 1: Calculate Basis
  current_basis = 24620 - 24500 = 120 pts
  next_basis = 24750 - 24500 = 250 pts

STEP 2: Select Active Futures
  IF current_basis >= 250:
      active_basis = current_basis (120)
      fut_used = "CURRENT"
  ELSE:
      active_basis = next_basis (250)  ← Selected
      fut_used = "NEXT"

STEP 3: Calculate Basis Percentage
  basis_pct = (250 / 24500) * 100 = 1.02%

STEP 4: Update Rolling Buffer (once per minute)
  basis_buffer = [0.95, 0.97, 0.98, 1.00, 1.01, 1.02, ...]
                 └─────────── 20 values ───────────┘

STEP 5: Calculate Z-Score
  mean = 0.98
  std = 0.03
  z_score = (1.02 - 0.98) / 0.03 = 1.33

OUTPUT:
  z_score = 1.33
  basis = 250
  fut_used = "NEXT"
```

---

#### 3. ENTRY LOGIC

```
┌─────────────────────────────────────────────────────────────┐
│                    ENTRY CONDITIONS                          │
│                    (ALL must be TRUE)                        │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ 1. NO ACTIVE POSITION (from DB)                     │    │
│  │    db.get_open_position() == None                   │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │ ✓                                 │
│                          ▼                                   │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ 2. DAILY LIMITS NOT HIT (from DB stats)             │    │
│  │    stats['total_trades'] < 4                        │    │
│  │    stats['gross_pnl'] > -3000                       │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │ ✓                                 │
│                          ▼                                   │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ 3. WITHIN TRADING HOURS                              │    │
│  │    hour in [13, 14]  (1 PM - 3 PM)                   │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │ ✓                                 │
│                          ▼                                   │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ 4. Z-SCORE THRESHOLD MET                             │    │
│  │    IF fut_used == "CURRENT":                         │    │
│  │        z_score > 2.5                                 │    │
│  │    ELSE (NEXT month):                                │    │
│  │        z_score > 3.0  (stricter)                     │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │ ✓                                 │
│                          ▼                                   │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ 5. BASIS MINIMUM MET                                 │    │
│  │    active_basis >= 250 pts                           │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │ ✓                                 │
│                          ▼                                   │
│                   ┌─────────────┐                            │
│                   │ ENTER TRADE │                            │
│                   └─────────────┘                            │
└─────────────────────────────────────────────────────────────┘
```

---

#### 4. TRADE EXECUTION

```
ENTRY SIGNAL TRIGGERED (z=2.7, basis=280)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEP 1: Find ATM Option
  spot = 24520
  atm_strike = round(24520 / 50) * 50 = 24500

  Find weekly expiry (next Thursday):
    today = Monday
    expiry = Thursday (3 days)

  Check DTE:
    DTE = 3 days
    min_dte = 3
    3 >= 3 ✓ Use this week

  Search instruments for:
    NIFTY + 25D19 + 24500 + CE
    Found: NIFTY25D1924500CE
    lot_size: 75 (from instruments)

STEP 2: Get Option Premium
  Subscribe to option WebSocket
  Wait for tick
  premium = 185.50

STEP 3: Calculate Levels
  qty = 75 * max_lots (1) = 75
  stop_loss = 185.50 * 0.75 = 139.13  (-25%)
  target = 185.50 * 1.35 = 250.43    (+35%)
  exit_deadline = now + 5 minutes

STEP 4: Check Margin (live mode)
  available = kite.margins()['equity']['available']
  required = premium * qty * 1.5
  IF available < required:
      Order REJECTED in DB
      Return

STEP 5: Place Order
  [PAPER] BUY 75 NIFTY25D1924500CE @ 185.50
  Create order record in DB (status: PENDING → COMPLETE)

STEP 6: Create Position in DB
  INSERT INTO positions (
    symbol: "NIFTY25D1924500CE",
    entry_price: 185.50,
    stop_loss: 139.13,
    target: 250.43,
    status: "OPEN"
  )

STEP 7: Send Telegram Alert
  "PAPER ENTRY ORDER"
  "Symbol: NIFTY25D1924500CE"
  "Price: ₹185.50"
  "Stop: ₹139.13"
  "Target: ₹250.43"

ON FAILURE (any step after STEP 2):
  - Unsubscribe option WebSocket
  - Clear option_token/option_symbol
  - Don't create position in DB
```

---

#### 5. EXIT LOGIC

```
┌─────────────────────────────────────────────────────────────┐
│                    EXIT CONDITIONS                           │
│                    (ANY triggers exit)                       │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ 1. TIME EXIT                                         │    │
│  │    current_time >= exit_deadline (entry + 5 min)     │    │
│  │    → Exit Reason: "TIME"                             │    │
│  └─────────────────────────────────────────────────────┘    │
│                          OR                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ 2. TARGET HIT                                        │    │
│  │    current_premium >= target (+35%)                  │    │
│  │    185.50 → 250.43                                   │    │
│  │    → Exit Reason: "TARGET"                           │    │
│  └─────────────────────────────────────────────────────┘    │
│                          OR                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ 3. STOP LOSS HIT                                     │    │
│  │    current_premium <= stop_loss (-25%)               │    │
│  │    185.50 → 139.13                                   │    │
│  │    → Exit Reason: "STOP_LOSS"                        │    │
│  └─────────────────────────────────────────────────────┘    │
│                          OR                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ 4. Z-SCORE REVERSION                                 │    │
│  │    z_score < 0 (signal invalidated)                  │    │
│  │    → Exit Reason: "Z_REVERT"                         │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │                                   │
│                          ▼                                   │
│                   ┌─────────────┐                            │
│                   │  EXIT TRADE │                            │
│                   └─────────────┘                            │
└─────────────────────────────────────────────────────────────┘

EXIT EXECUTION (with retry):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  max_retries = 2

  FOR attempt in [1, 2]:
      success = place_exit_order()
      IF success: BREAK
      ELSE: wait 2 seconds

  IF not success:
      Mark position as ERROR in DB
      Send "MANUAL INTERVENTION REQUIRED" alert
      RETURN (stop trying)

  [PAPER] SELL 75 NIFTY25D1924500CE @ 195.00

  P&L = (195.00 - 185.50) * 75 = ₹712.50
  Result = WIN

  Update DB:
    - Close position (status: CLOSED)
    - Calculate P&L in DB

  Unsubscribe option WebSocket

  Send Telegram:
    "PAPER EXIT - TIME"
    "Entry: ₹185.50"
    "Exit: ₹195.00"
    "P&L: ₹+712.50"
```

---

#### 6. CRASH RECOVERY

```
SCENARIO: Bot restarts at 14:15 with open position
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Bot starts, queries DB:
   SELECT * FROM positions
   WHERE bot_id = 'ZSCORE_V1' AND status = 'OPEN'

2. Position found:
   {
     id: 5,
     symbol: "NIFTY25D1924500CE",
     entry_price: 185.50,
     exit_deadline: "14:35:00",
     status: "OPEN"
   }

3. Recovery mode:
   - Look up option token from instruments
   - Subscribe to option WebSocket
   - Send Telegram: "POSITION RECOVERED"

4. Resume exit monitoring:
   - Check if exit_deadline passed → Exit immediately
   - Else continue normal exit checks

5. Continue main loop
```

---

#### 7. WebSocket Auto-Reconnect

```
SCENARIO: WebSocket disconnects during trading
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Every 30 seconds in main_loop:
  IF not ws_connected:
      ws_reconnect_attempts += 1

      IF ws_reconnect_attempts <= 5:
          logging.warning("Reconnecting...")
          reconnect_websocket()
      ELSE:
          logging.error("Using REST API fallback")

  IF ws_connected:
      ws_reconnect_attempts = 0  # Reset

Stale option price handling:
  IF prices['option'] <= 0 AND have position:
      rest_price = get_option_ltp(symbol)  # REST API
      IF rest_price:
          current_premium = rest_price
```

---

## Part 2: Deployment Guide

### Prerequisites

| Requirement | Details |
|-------------|---------|
| Raspberry Pi | Pi 3B+ or newer, Raspbian OS |
| Python | 3.8 or higher |
| Network | Stable internet connection |
| Kite Account | With API access enabled |
| Telegram Bot | Created via @BotFather |

---

### Step 1: Prepare Raspberry Pi

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python and pip
sudo apt install python3 python3-pip -y

# Verify versions
python3 --version   # Should be 3.8+
pip3 --version
```

---

### Step 2: Create Directory Structure

```bash
# Create project directory
mkdir -p /home/pi/ZSCORE
cd /home/pi/ZSCORE

# Create data directory (shared with other bots)
mkdir -p /home/pi/bots_data/logs/zscore
```

---

### Step 3: Transfer Files

From your Windows machine:
```bash
# Using SCP (run in PowerShell/CMD)
scp -r C:\Users\...\BOTS\ZSCORE\* pi@<PI_IP>:/home/pi/ZSCORE/
```

Files to transfer:
- `main.py`
- `db.py`
- `config.json`
- `start_bot.sh`
- `stop_bot.sh`

---

### Step 4: Install Dependencies

```bash
cd /home/pi/ZSCORE

# Install required packages
pip3 install kiteconnect requests

# Verify installation
python3 -c "from kiteconnect import KiteConnect; print('OK')"
```

---

### Step 5: Configure Bot

Edit config.json:
```bash
nano config.json
```

Update these fields:
```json
{
  "credentials": {
    "path": "/home/pi/bots_data/kite_access_token.json"
  },
  "data_dir": "/home/pi/bots_data",
  "telegram": {
    "enabled": true,
    "bot_token": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
    "chat_id": "987654321"
  }
}
```

---

### Step 6: Setup Kite Token File

Your existing token refresh mechanism should save to:
`/home/pi/bots_data/kite_access_token.json`

Format:
```json
{
  "api_key": "your_api_key",
  "access_token": "your_access_token",
  "user_id": "AB1234"
}
```

---

### Step 7: Setup Holiday Calendar

Create `/home/pi/bots_data/holiday_calendar.json`:
```json
{
  "holidays": [
    "2025-01-26",
    "2025-03-14",
    "2025-08-15"
  ]
}
```

---

### Step 8: Make Scripts Executable

```bash
chmod +x start_bot.sh stop_bot.sh
```

---

### Step 9: Test Run (Manual)

```bash
cd /home/pi/ZSCORE
python3 main.py
```

Expected output:
```
2025-12-16 12:00:01 [INFO] ============================================================
2025-12-16 12:00:01 [INFO] Z-SCORE TRADING BOT STARTING
2025-12-16 12:00:01 [INFO] Version: 3.0.0
2025-12-16 12:00:01 [INFO] Paper Mode: True
2025-12-16 12:00:01 [INFO] Data Dir: /home/pi/bots_data
2025-12-16 12:00:01 [INFO] ============================================================
2025-12-16 12:00:02 [INFO] Kite initialized for user: AB1234
2025-12-16 12:00:02 [INFO] Database initialized: /home/pi/bots_data/zscore_trades.db
2025-12-16 12:00:03 [INFO] Loading cached instruments from /home/pi/bots_data/nfo_instruments.csv
2025-12-16 12:00:03 [INFO] Loaded 98543 instruments into memory
2025-12-16 12:00:03 [INFO] Auto-detected futures - Current: NIFTY25DECFUT, Next: NIFTY25JANFUT
2025-12-16 12:00:04 [INFO] WebSocket connected
2025-12-16 12:00:04 [INFO] Subscribed to 3 instruments
2025-12-16 12:00:04 [INFO] Starting main loop...
```

Check Telegram - you should receive startup message.

Press `Ctrl+C` to stop.

---

### Step 10: Setup Cron Jobs

```bash
# Edit crontab
crontab -e

# Add these lines at the bottom:

# Start bot at 12:00 PM on weekdays (1 hour before trading)
0 12 * * 1-5 /home/pi/ZSCORE/start_bot.sh >> /home/pi/bots_data/logs/zscore/cron.log 2>&1

# Stop bot at 15:30 PM on weekdays (after market close)
30 15 * * 1-5 /home/pi/ZSCORE/stop_bot.sh >> /home/pi/bots_data/logs/zscore/cron.log 2>&1

# Save and exit (Ctrl+X, Y, Enter)
```

Verify cron:
```bash
crontab -l
```

---

### Step 11: Monitor & Verify

#### Check if bot is running:
```bash
ps aux | grep main.py
```

#### View live logs:
```bash
tail -f /home/pi/bots_data/logs/zscore/$(date +%Y-%m-%d).log
```

#### Check database:
```bash
sqlite3 /home/pi/bots_data/zscore_trades.db "SELECT * FROM positions WHERE status='OPEN';"
```

#### Check daily stats:
```bash
sqlite3 /home/pi/bots_data/zscore_trades.db "SELECT * FROM daily_summary ORDER BY trade_date DESC LIMIT 5;"
```

---

### Troubleshooting

| Issue | Solution |
|-------|----------|
| "Module not found: kiteconnect" | `pip3 install kiteconnect` |
| "Token expired" | Check token refresh mechanism |
| "No futures found" | Verify instruments fetch worked |
| Bot not starting via cron | Check `cron.log`, ensure paths are absolute |
| WebSocket disconnects | Bot auto-reconnects (max 5 attempts) |
| "MANUAL INTERVENTION REQUIRED" | Exit order failed, close via Zerodha app |
| No Telegram messages | Verify bot_token and chat_id |

---

### File Locations Summary

```
/home/pi/ZSCORE/              # Bot code
├── main.py                   # Main trading bot (v3.0)
├── db.py                     # Database module
├── config.json               # Configuration
├── start_bot.sh              # Cron start script
├── stop_bot.sh               # Cron stop script
└── zscore_bot.pid            # PID file (auto-created)

/home/pi/bots_data/           # Shared data directory
├── kite_access_token.json    # Kite credentials
├── holiday_calendar.json     # Market holidays
├── nfo_instruments.csv       # Cached instruments (auto-refreshed)
├── zscore_trades.db          # SQLite database (orders, positions, summary)
└── logs/
    └── zscore/
        ├── 2025-12-16.log    # Daily log
        └── cron.log          # Cron output
```

---

### Going Live Checklist

- [ ] Paper traded for 1-2 weeks
- [ ] Verified signals match backtest logic
- [ ] Confirmed P&L calculations are correct
- [ ] Telegram alerts working
- [ ] Crash recovery tested (kill -9 and restart)
- [ ] WebSocket reconnect tested (network disconnect)
- [ ] Database queries working (check positions, stats)
- [ ] Cron jobs running on schedule
- [ ] Set `"paper_trade": false` in config.json
- [ ] Start with 1 lot, scale up after consistent results

---

### v3.0 Key Changes

1. **SQLite Database** instead of state.json
2. **Multi-bot safe** with BOT_ID isolation
3. **Auto-reconnect WebSocket** with REST fallback
4. **Exit order retry** (2 attempts) before marking ERROR
5. **Entry cleanup** on order failure
6. **Division by zero guard** in z-score calculation
7. **Daily summary** at 15:20 with charges estimate
