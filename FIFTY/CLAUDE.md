# FIFTY Bot - Monthly CSP Trading Bot

## Overview
Monthly timeframe trading bot with Telegram-based interactive approval workflow. Trades CSP signals with NIFTY weekly filter, 20% SL trailing to monthly LOW.

## Architecture

### Key Components
- **orchestrator.py**: Time-based task scheduler (cron every 5 minutes)
- **signal_processor.py**: CSP signal detection & SuperTrend calculation
- **order_manager.py**: GTT entry order placement & monitoring
- **exit_manager.py**: SL GTT management & trailing
- **telegram/bot.py**: Interactive Telegram bot with inline buttons
- **telegram/approval_handler.py**: Signal approval workflow

### Dual Kite Client
Uses two separate Kite accounts:
- **Read client**: Shared token from SNAIL (for market data)
- **Trade client**: Dedicated FIFTY account (for orders)

## Database Schema

### signal_queue
Tracks CSP signals through lifecycle:
- pending -> notified -> approved -> entered -> filled
- Or: rejected / hold / invalidated / expired

### open_orders
Pending GTT entry orders.

### open_positions
Active positions with SL tracking.

### closed_positions
Historical trades with P&L.

### capital_ledger
Daily capital snapshots.

### bot_state
Key-value store for state (kill_switch, telegram_offset, etc.)

## Telegram Commands
- `/positions` - List open positions
- `/pending` - List pending signals
- `/stats` - Win rate, P&L statistics
- `/capital` - Capital allocation
- `/report` - Daily summary
- `/kill` - Activate kill switch
- `/resume` - Deactivate kill switch

## Signal Approval Workflow
1. Signal detected from CSV (CSP filter)
2. SuperTrend level calculated
3. NIFTY weekly filter checked
4. Telegram notification with buttons: [Approve] [Reject] [Hold] [Revise]
5. User response processed
6. GTT entry order placed (if approved)

## SL Management
- Initial SL: 20% below entry
- Monthly trailing: Trail to monthly LOW (last trading day only)
- SL only tightens (never widens)

## Schedule (IST)
- 08:50-09:00: Token generation (auto-generates if expired)
- 09:00-09:05: Morning startup (validates token, connections, capital)
- 09:15-15:30: Signal processing, order monitoring
- 09:30-09:35: Re-notify HOLD signals
- 15:50-15:55: Monthly SL trail (last trading day)
- 16:00-16:05: Recovery checks
- 16:15-16:20: Weekly report (Friday)
- 16:25-16:30: Month-end cleanup (last trading day)

Note: Daily report disabled per user preference (weekly/monthly only)

## Configuration
See `config/config.yaml` for all settings:
- Trading parameters (capital, position size)
- Risk management (max positions, drop alert threshold)
- Telegram credentials
- API settings

## Setup

### 1. Install Dependencies
```bash
cd FIFTY
pip install -r requirements.txt
```

### 2. Configure
```bash
# Copy and edit config
cp config/config.yaml.template config/config.yaml
# Add Telegram bot_token and chat_id

# Create Kite credentials
cp data/kite_credentials.json.template data/kite_credentials.json
# Add API key and secret
```

### 3. Generate Token
```bash
python generate_token.py
```

### 4. Initialize Database
```bash
python main.py --init
```

### 5. Test
```bash
python main.py --test        # Test Telegram
python main.py --test-kite   # Test Kite API
```

### 6. Run
```bash
python main.py
```

### 7. Setup Cron (Production)
```cron
# Run every 5 minutes from 8:50 AM to 4:30 PM on weekdays
50 8 * * 1-5 cd /path/to/FIFTY && python main.py >> logs/cron.log 2>&1
55 8 * * 1-5 cd /path/to/FIFTY && python main.py >> logs/cron.log 2>&1
*/5 9-16 * * 1-5 cd /path/to/FIFTY && python main.py >> logs/cron.log 2>&1
```

## Important Notes

### Kill Switch
When activated, ALL operations stop. Use `/kill` to activate, `/resume` to deactivate.

### GTT Entry Orders
- GTT stays active for 1 year
- No daily re-placement needed
- Cancelled at month-end if unfilled

### Position Protection
- Every position MUST have SL GTT
- If SL GTT fails, CRITICAL alert sent
- Position considered UNPROTECTED until GTT placed

### NIFTY Filter
- Blocks new entries when NIFTY weekly SuperTrend is bearish
- Existing positions unaffected

### 30% Drop Alert
- Sends alert with HODL/EXIT buttons
- User choice: HODL (ignore today) or EXIT (market sell)

## Daily Token Generation

FIFTY uses automated daily token generation using TOTP + password (similar to CROCODILE).

### How It Works
1. **Early Generation (8:50-9:00)**: Orchestrator checks if token is valid
2. If expired/missing, automatically generates new token using:
   - API key + API secret (for checksum)
   - User ID + Password (for login)
   - TOTP secret (for 2FA)
3. **Morning Startup (9:00-9:05)**: Re-validates and retries if needed
4. Token saved to `data/kite_access_token.json`

### Credentials File
Create `data/kite_credentials.json`:
```json
{
    "api_key": "your_api_key",
    "api_secret": "your_api_secret",
    "user_id": "your_user_id",
    "password": "your_password",
    "totp_secret": "your_totp_secret"
}
```

### Manual Token Generation
```bash
python generate_token.py          # Generate token
python generate_token.py --check  # Check token validity
python generate_token.py --test   # Validate with API call
python generate_token.py --force  # Force regenerate
```

### Token Validity
- Tokens expire at 6 AM IST daily
- System auto-generates before market hours
- If generation fails, Telegram alert is sent

## Safety Features (Ported from CROCODILE)

### Ignore List (`data/ignore_list.csv`)
- CSV file with `Script,Reason` columns
- Scripts in ignore list are skipped during signal processing
- Checked at: signal detection, notification sending
- Edit file to add/remove scripts (no restart needed - reload on next run)

### Order Cutoff Time
- No entry orders placed after **15:29 IST** (3:29 PM)
- Prevents positions opening near market close without protection
- Configurable via `ORDER_CUTOFF_TIME` constant

### Duplicate Detection (Multi-Layer)
1. **Position Layer**: Blocks if already have open position in script
2. **Pending Order Layer**: Blocks if already have pending GTT in script
3. **Signal Layer**: Rejects signal if already exists for script this month
4. **Zerodha GTT Layer**: Detects orphaned GTTs and recovers them

### Pre-Order Validation (`order_manager.py`)
6-step validation before placing GTT orders:
1. LTP sanity (not None, > 0, >= 0.05)
2. Entry price sanity (> 0)
3. Price relationship sanity (not 10x different)
4. Tick size validation (auto-correct if needed)
5. Quantity validation (> 0)
6. Minimum order value (>= Rs.10)

### Position Limits
- Max positions: 5 (configurable)
- Max pending orders: 3 (configurable)
- Blocks new notifications when limits reached

### Error Auto-Recovery
- **"Price too close"**: Auto-retry with 0.3% buffer when SL GTT fails due to Zerodha's 0.25% LTP proximity rule
- **Tick size errors**: Parse Zerodha error message and auto-correct price to required tick size
- **GTT Recovery**: Unprotected positions detected and recovered during recovery checks (16:00-16:05)

### Circuit Breaker
- Opens after consecutive API failures (3 critical, 5 standard)
- Auto-resets after 30 minutes
- Allows exits even when open
- Manual reset via flag file

### GTT Verification
- Every placed GTT is verified against Zerodha API
- Unverified GTTs flagged and re-verified next run
- Recovery check verifies ALL positions have active GTT

### Kill Switch
- `/kill` command stops ALL operations
- `/resume` command resumes operations
- State persisted in database (survives restart)

### Kite Constants (`src/models/kite_constants.py`)
Type-safe constants for Kite API:
- `KiteOrderStatus`: COMPLETE, CANCELLED, REJECTED, OPEN, TRIGGER_PENDING
- `KiteGTTStatus`: ACTIVE, TRIGGERED, CANCELLED, REJECTED
- Helper methods: `is_final()`, `is_active()`, `is_terminal()`

### SL GTT Order Type
- Uses LIMIT order (not MARKET) for SL GTT - battle-tested approach from CROCODILE
- Prevents terrible fills in gap-down scenarios

## Safety Features Summary Table

| Feature | Location | When Checked |
|---------|----------|--------------|
| Ignore List | `signal_processor.py` | Signal detection, notification |
| Order Cutoff | `order_manager.py` | Before GTT placement |
| Position Limit | `signal_processor.py` | Before notification |
| Pending Order Limit | `signal_processor.py` | Before notification |
| Duplicate Position | `order_manager.py` | Before GTT placement |
| Duplicate Order | `order_manager.py` | Before GTT placement |
| Pre-Order Validation | `order_manager.py` | Before GTT placement |
| GTT Verification | `exit_manager.py` | After GTT placement |
| GTT Recovery | `orchestrator.py` | Recovery checks (16:00) |
| Circuit Breaker | `circuit_breaker.py` | Every API call |
| Kill Switch | `database.py` | Every orchestrator run |
