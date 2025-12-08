# SNAIL Complete Operational Guide

**Document Version:** 1.0.0
**Generated:** 2025-12-04
**Codebase Version:** 1.0.0

---

## SECTION 1: SYSTEM OVERVIEW

| Item | Value | Source File |
|------|-------|-------------|
| Application Name | SNAIL (Systematic NIFTY Automated Iron-fly Leverager) | `main.py:3` |
| Version | 1.0.0 | `main.py:38` |
| Technology Stack | Python 3.8+, SQLite, Zerodha Kite API, Claude AI, Telegram | `requirements.txt` |
| Architecture Pattern | Service-Oriented Architecture (SOA) | Code structure |
| Database Type | SQLite with WAL mode | `data/schema.sql:13-15` |
| Cache / Queue | File-based instrument cache, SQLite alert queue | `config/config.yaml:121` |

### Architecture Diagram

```
                              SNAIL SYSTEM ARCHITECTURE
                              ========================

    ┌─────────────────────────────────────────────────────────────────────┐
    │                         CLI INTERFACE                                │
    │                          (main.py)                                   │
    │  Commands: run | startup | entry | exit | status | summary | test   │
    └─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
    ┌─────────────────────────────────────────────────────────────────────┐
    │                       WORKFLOWS LAYER                                │
    │  ┌──────────────┐ ┌───────────────┐ ┌─────────────────────────────┐ │
    │  │daily_startup │ │entry_workflow │ │    monitor_workflow         │ │
    │  │   .py        │ │    .py        │ │       .py                   │ │
    │  └──────────────┘ └───────────────┘ └─────────────────────────────┘ │
    │  ┌──────────────┐                                                   │
    │  │daily_summary │                                                   │
    │  │   .py        │                                                   │
    │  └──────────────┘                                                   │
    └─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
    ┌─────────────────────────────────────────────────────────────────────┐
    │                       SERVICES LAYER                                 │
    │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌────────────┐  │
    │  │entry_manager │ │exit_manager  │ │position_     │ │claude_     │  │
    │  │   .py        │ │   .py        │ │monitor.py    │ │advisor.py  │  │
    │  └──────────────┘ └──────────────┘ └──────────────┘ └────────────┘  │
    └─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
    ┌─────────────────────────────────────────────────────────────────────┐
    │                      UTILITIES LAYER                                 │
    │  ┌────────────┐ ┌────────────┐ ┌──────────────┐ ┌────────────────┐  │
    │  │config.py   │ │db.py       │ │calculations  │ │symbol_builder  │  │
    │  │            │ │            │ │   .py        │ │   .py          │  │
    │  └────────────┘ └────────────┘ └──────────────┘ └────────────────┘  │
    │  ┌────────────┐ ┌────────────┐ ┌──────────────┐ ┌────────────────┐  │
    │  │helpers.py  │ │order_      │ │alert_dedup   │ │startup_        │  │
    │  │            │ │helpers.py  │ │   .py        │ │validation.py   │  │
    │  └────────────┘ └────────────┘ └──────────────┘ └────────────────┘  │
    │  ┌────────────┐                                                     │
    │  │holiday_    │                                                     │
    │  │scraper.py  │                                                     │
    │  └────────────┘                                                     │
    └─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
    ┌─────────────────────────────────────────────────────────────────────┐
    │                        API LAYER                                     │
    │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌────────────┐  │
    │  │kite_client   │ │claude_client │ │telegram_     │ │telegram_   │  │
    │  │   .py        │ │   .py        │ │alerts.py     │ │bot.py      │  │
    │  └──────────────┘ └──────────────┘ └──────────────┘ └────────────┘  │
    │  ┌──────────────┐                                                   │
    │  │kite_auth.py  │                                                   │
    │  └──────────────┘                                                   │
    └─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
    ┌─────────────────────────────────────────────────────────────────────┐
    │                    EXTERNAL SERVICES                                 │
    │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌────────────┐  │
    │  │ Zerodha Kite │ │ Anthropic    │ │  Telegram    │ │    NSE     │  │
    │  │     API      │ │ Claude API   │ │  Bot API     │ │  (via Kite)│  │
    │  └──────────────┘ └──────────────┘ └──────────────┘ └────────────┘  │
    └─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
    ┌─────────────────────────────────────────────────────────────────────┐
    │                        DATA LAYER                                    │
    │  ┌──────────────────────────┐ ┌─────────────────────────────────┐   │
    │  │  SQLite Database         │ │    Shared Files (../data/)      │   │
    │  │  (data/snail.db)         │ │  - kite_access_token.json       │   │
    │  │  - positions             │ │  - instruments.csv               │   │
    │  │  - position_legs         │ │  - holiday_calendar.json         │   │
    │  │  - orders                │ │                                   │   │
    │  │  - pnl_snapshots         │ └─────────────────────────────────┘   │
    │  │  - claude_decisions      │                                       │
    │  │  - alert_queue           │                                       │
    │  │  - system_status         │                                       │
    │  └──────────────────────────┘                                       │
    └─────────────────────────────────────────────────────────────────────┘
```

### Trading Strategy Summary

**Iron Fly Strategy:**
- Sell ATM Call + Sell ATM Put (Short Straddle)
- Buy OTM Call Wing + Buy OTM Put Wing (Protection)
- Net Credit Strategy with defined risk
- Profit when NIFTY stays near ATM strike

---

## SECTION 2: DIRECTORY STRUCTURE

| Directory | Purpose | Key Files |
|-----------|---------|-----------|
| `/` (root) | Project root | `main.py`, `requirements.txt`, `.env.template` |
| `/src` | Main source code | `__init__.py` |
| `/src/api` | External API integrations | `kite_client.py`, `claude_client.py`, `telegram_alerts.py`, `telegram_bot.py`, `kite_auth.py`, `response_handler.py` |
| `/src/utils` | Utility modules | `config.py`, `db.py`, `calculations.py`, `symbol_builder.py`, `helpers.py`, `order_helpers.py`, `alert_dedup.py`, `holiday_scraper.py`, `startup_validation.py` |
| `/src/services` | Business logic services | `entry_manager.py`, `exit_manager.py`, `position_monitor.py`, `claude_advisor.py` |
| `/src/workflows` | High-level workflows | `daily_startup.py`, `monitor_workflow.py`, `entry_workflow.py`, `daily_summary.py` |
| `/config` | Configuration files | `config.yaml`, `nse_holidays.json` |
| `/config/prompts` | Claude AI prompt templates | `pre_entry.txt`, `stop_loss_advisory.txt`, `wing_approach.txt`, `friday_decision.txt`, `eod_decision.txt`, `vix_spike.txt`, `market_event.txt`, `gap_beyond_wing.txt`, `adjustment.txt` |
| `/data` | Database and data files | `snail.db`, `schema.sql`, `instruments_cache.json`, `holiday_calendar.json` |
| `/logs` | Log files | `snail.log`, `orders.log`, `/claude/` |
| `/tests` | Test suites | `/unit/`, `/standalone/`, `/logic/`, `/calculations/`, `/integration/`, `run_all_tests.py` |
| `/docs` | Documentation | `TECHNICAL_DESIGN.md`, `TECHNICAL_DESIGN_REFERENCE.md` |
| `/kite_integration` | Legacy Kite examples | `kite_auth_server.py`, `KITE_COMPLETE_GUIDE.md` |
| `/Archive` | Archived documents | Previous versions, requirements |

### Key Files

| File | Purpose |
|------|---------|
| `main.py` | Main entry point with CLI commands |
| `config/config.yaml` | Primary configuration file |
| `.env` | Environment variables (secrets) |
| `data/snail.db` | SQLite database |
| `data/schema.sql` | Database schema definition |
| `../data/kite_access_token.json` | Shared Kite access token |
| `../data/instruments.csv` | Shared instruments file |

---

## SECTION 3: CONFIGURATION DECODE

### 3.1 Environment Variables

Extract from `.env.template`:

| Variable | Purpose | Required | Default | Example |
|----------|---------|----------|---------|---------|
| `ZERODHA_API_KEY` | Kite Connect API key | Yes | None | `xxxxxxxx` |
| `ZERODHA_API_SECRET` | Kite Connect API secret | Yes | None | `xxxxxxxxxxxxxxxx` |
| `ZERODHA_USER_ID` | Zerodha client ID | Yes | None | `AB1234` |
| `ZERODHA_PASSWORD` | Zerodha login password | Yes | None | `your_password` |
| `ZERODHA_TOTP_SECRET` | TOTP secret for 2FA | Yes | None | `base32_secret` |
| `ANTHROPIC_API_KEY` | Claude AI API key | Yes | None | `sk-ant-xxxxx` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | Yes | None | `123456789:ABC-DEF` |
| `TELEGRAM_CHAT_ID` | Telegram chat/group ID | Yes | None | `-1001234567890` |
| `SNAIL_PAPER_TRADING` | Enable paper trading mode | No | `true` | `true` or `false` |

### 3.2 Config Files

| File | Purpose | Key Settings | Override Method |
|------|---------|--------------|-----------------|
| `config/config.yaml` | Main configuration | Trading params, API config, paths | Environment variable substitution `${VAR}` |
| `config/nse_holidays.json` | NSE trading holidays | Holiday dates | Updated by `holiday_scraper.py` |
| `config/prompts/*.txt` | Claude AI prompts | Decision prompts | Edit files directly |

### 3.3 Key Configuration Parameters

From `config/config.yaml`:

**Trading Parameters:**
```yaml
trading:
  instrument: "NIFTY"
  strategy: "iron_fly"

  capital:
    allocated: 120000        # Total capital in INR
    min_available: 100000    # Minimum required margin
    num_lots: 1              # Number of lots to trade

  entry:
    vix_range:
      min: 10.0              # VIX >= 10 required
      max: 16.0              # VIX <= 16 required
      soft_min: 9.5          # Soft buffer lower
      soft_max: 16.5         # Soft buffer upper
    min_dte: 6               # Minimum days to expiry
    window:
      start: "09:30"         # Entry window start
      end: "14:30"           # Entry window end
    cooldown_days: 1         # Days to wait after exit
    slippage_ticks: 2        # Fixed slippage ticks
    use_tiered_slippage: false  # Use progressive tiered slippage

  exit:
    profit_target_pct: 50    # Exit at 50% of max profit
    stop_loss_pct: 50        # Alert at 50% of max loss
    vix_hard_exit: 20        # Hard exit if VIX > 20
    vix_warning_zone: 16.5   # Warning if VIX > 16.5
    friday_check_time: "15:00"
    friday_exit_time: "15:15"
    expiry_exit_time: "15:20"
    cooldown_hours: 24       # Cooldown after exit

  wing_approach:             # Wing proximity thresholds
    early_warning_pct: 30    # Early warning at 30%
    advisory_pct: 40         # Claude advisory at 40%

  slippage:
    entry_tier1: 2           # First attempt (2 pts)
    entry_tier2: 3           # Second attempt (3 pts)
    entry_tier3: 5           # Third attempt (5 pts then MARKET)
    exit_normal: 3
    exit_urgent: 0           # MARKET order for urgent exit
    timeout_seconds: 10

  spread:                    # Bid-ask spread thresholds
    atm_warn: 2              # ATM warning threshold
    atm_block: 5             # ATM block threshold
    wing_warn: 5             # Wing warning threshold
    wing_block: 10           # Wing block threshold
```

**Monitoring Parameters:**
```yaml
monitoring:
  interval_minutes: 10       # Position check interval
  start_time: "09:16"        # Monitor start time
  end_time: "15:26"          # Monitor end time
  max_quote_failures: 3      # Max failures before pause
  retry_delay_seconds: 5     # Delay between retries

  alert_cooldown:            # Hours between duplicate alerts
    stop_loss: 1
    wing_approach: 1
    big_move: 1
    vix_warning: 1
    profit_milestone: 2
    system: 0.083            # 5 minutes

retention:                   # Data retention periods
  entry_attempts_days: 90
  alert_queue_days: 30
  pnl_snapshots: "forever"
  positions: "forever"
  claude_decisions: "forever"
```

### 3.4 Secrets & Credentials

| Secret | Purpose | How to Obtain |
|--------|---------|---------------|
| `ZERODHA_API_KEY` | Kite Connect access | Register at developers.kite.trade |
| `ZERODHA_API_SECRET` | Kite Connect secret | Generated with API key |
| `ZERODHA_TOTP_SECRET` | 2FA authentication | From authenticator app setup |
| `ANTHROPIC_API_KEY` | Claude AI access | console.anthropic.com |
| `TELEGRAM_BOT_TOKEN` | Telegram bot | Create bot via @BotFather |
| `TELEGRAM_CHAT_ID` | Target chat | Send /start to bot, check updates API |

---

## SECTION 4: DATABASE DECODE

| Attribute | Value | Source |
|-----------|-------|--------|
| Database Type | SQLite 3 | `data/schema.sql` |
| Connection Config | WAL mode, 5000ms timeout | `schema.sql:13-15` |
| Pool Settings | None (single connection) | `src/utils/db.py` |
| Location | `data/snail.db` | `config/config.yaml:108` |

### Database Tables

| Table | Purpose | Key Fields | Relationships |
|-------|---------|------------|---------------|
| `positions` | Main position tracking | id, status, atm_strike, wing_distance, entry_premium, max_profit, max_loss, lot_size, margin_deployed | Parent of position_legs, orders |
| `position_legs` | Individual option legs | position_id, leg_type, option_type, strike, tradingsymbol, entry_price, quantity, instrument_token | FK: positions.id |
| `orders` | Order audit trail | position_id, kite_order_id, order_type, transaction_type, fill_price, slippage, status | FK: positions.id |
| `pnl_snapshots` | Point-in-time P&L | position_id, current_pnl, pnl_percent, nifty_spot, vix, ce_bid/ask, pe_bid/ask | FK: positions.id |
| `claude_decisions` | AI decision logs | position_id, trigger_type, prompt, response, decision, model_used, tokens_used | FK: positions.id |
| `market_data` | Daily OHLC + ATR | date, atr_14, previous_close, day_open, day_high, day_low | None |
| `alert_queue` | Outgoing alerts | position_id, alert_type, message, priority, status, requires_response, timeout_action | FK: positions.id |
| `response_queue` | User responses | alert_id, user_response, parsed_action, status | FK: alert_queue.id |
| `alert_cooldowns` | Deduplication tracking | alert_type, content_hash, last_alert_time | UNIQUE(alert_type, content_hash) |
| `cooldowns` | Post-exit cooldown | position_id, exit_date, cooldown_end | FK: positions.id |
| `entry_attempts` | Entry attempt audit | attempt_time, result, block_reason, checklist_json | None |
| `system_status` | System operational state | status, reason, set_by, set_at, cleared_at | None |

### Database Schema Details

**Position Status Values:** `pending`, `active`, `exiting`, `closed`

**Exit Reason Values:** `profit_target`, `stop_loss`, `friday_exit`, `vix_spike`, `manual`, `timeout`, `adjustment`, `expiry_day`

**Leg Types:** `straddle_ce`, `straddle_pe`, `wing_ce`, `wing_pe`

### Database Commands

```bash
# Initialize database
python -m src.utils.db init

# Cleanup old data
python -m src.utils.db cleanup

# View database via SQLite CLI
sqlite3 data/snail.db
.tables
.schema positions
SELECT * FROM positions WHERE status = 'active';
```

---

## SECTION 5: API ENDPOINTS DECODE

SNAIL is a CLI application, not a web service. It integrates with external APIs:

### External API Integrations

| Service | Purpose | Base URL | Auth Method | Source File |
|---------|---------|----------|-------------|-------------|
| Zerodha Kite | Trading & Market Data | `https://api.kite.trade` | OAuth + TOTP | `src/api/kite_client.py` |
| Anthropic Claude | AI Decision Advisory | `https://api.anthropic.com` | API Key Bearer | `src/api/claude_client.py` |
| Telegram | Alerts & User Commands | `https://api.telegram.org` | Bot Token | `src/api/telegram_alerts.py`, `telegram_bot.py` |

### Kite API Operations Used

| Operation | Method | Purpose | Source |
|-----------|--------|---------|--------|
| `quote()` | Get quotes | Fetch bid-ask prices | `kite_client.py:162` |
| `ltp()` | Get LTP | Last traded prices | `kite_client.py:190` |
| `place_order()` | Place order | Execute trades | `kite_client.py:227` |
| `modify_order()` | Modify order | Change price/type | `kite_client.py:280` |
| `cancel_order()` | Cancel order | Cancel pending order | `kite_client.py:310` |
| `positions()` | Get positions | Current holdings | `kite_client.py:361` |
| `margins()` | Get margins | Available capital | `kite_client.py:372` |
| `instruments()` | Get instruments | Symbol master | `kite_client.py:421` |
| `historical_data()` | Get OHLC | ATR calculation | `kite_client.py:450` |
| `profile()` | Get profile | User validation | `kite_client.py:505` |

### Claude API Decision Types

| Decision Type | Prompt File | Purpose |
|---------------|-------------|---------|
| `pre_entry` | `pre_entry.txt` | Approve/reject entry |
| `stop_loss_advisory` | `stop_loss_advisory.txt` | Hold/exit at 50% loss |
| `wing_approach` | `wing_approach.txt` | NIFTY nearing wing strike |
| `friday_decision` | `friday_decision.txt` | Weekend carry decision |
| `eod_decision` | `eod_decision.txt` | End of day decision |
| `vix_spike` | `vix_spike.txt` | VIX spike response |
| `market_event` | `market_event.txt` | Major news/event |
| `gap_beyond_wing` | `gap_beyond_wing.txt` | Gap open beyond wing |
| `significant_gap` | (inline) | Significant gap at open |
| `adjustment` | `adjustment.txt` | Position adjustment |
| `user_query` | (dynamic) | User-initiated queries |

### Telegram Bot Architecture

SNAIL uses a **hybrid Telegram architecture**:

| Component | Purpose | Method | Source File |
|-----------|---------|--------|-------------|
| `TelegramAlerts` | Send alerts/notifications | HTTP POST | `src/api/telegram_alerts.py` |
| `TelegramBot` | Receive commands/responses | Long Polling | `src/api/telegram_bot.py` |

**Polling Behavior:**
- Polling runs in a background daemon thread
- Long polling timeout: 30 seconds
- Poll interval: 2 seconds between requests
- Response timeout: 15 minutes for user decisions
- Callback deduplication prevents double-processing

**Available Bot Commands:**

| Command | Purpose | Example |
|---------|---------|---------|
| `/start` | Initialize bot conversation | `/start` |
| `/help` | Show available commands | `/help` |
| `/status` | Show current market & position status | `/status` |
| `/position` | Show active position details | `/position` |
| `/pnl` | Show current P&L | `/pnl` |
| `/exit` | Request position exit | `/exit` |
| `/hold` | Confirm hold decision | `/hold` |

**Setting Up Bot Commands in Telegram (via @BotFather):**

To enable command autocomplete in Telegram, register commands with BotFather:

```
1. Open Telegram and message @BotFather
2. Send: /setcommands
3. Select your SNAIL bot
4. Paste the following command list:

start - Initialize bot and show welcome message
help - Show available commands and usage
status - Show current market data and position status
position - Show active position details with P&L
pnl - Show current profit/loss summary
exit - Request immediate position exit
hold - Confirm hold decision for current position
```

**Setting Bot Description (optional):**
```
1. Message @BotFather
2. Send: /setdescription
3. Select your bot
4. Enter: SNAIL Trading Bot - Automated Iron Fly strategy for NIFTY options
```

**Setting Bot About Text (optional):**
```
1. Message @BotFather
2. Send: /setabouttext
3. Select your bot
4. Enter: Systematic NIFTY Automated Iron-fly Leverager. Manages options positions with AI-powered decision support.
```

**Inline Keyboard Responses:**

When SNAIL sends decision alerts, users can respond via inline buttons:

| Button | Action | Callback Data Format |
|--------|--------|---------------------|
| HOLD | Keep position open | `hold:alert_type:position_id` |
| EXIT | Close position | `exit:alert_type:position_id` |
| ADJUST | Adjust position | `adjust:alert_type:position_id` |
| YES | Confirm action | `yes:alert_type:position_id` |
| NO | Cancel action | `no:alert_type:position_id` |

**Text Responses:**

Users can also type text responses directly:
- `EXIT` - Request immediate exit
- `HOLD` - Confirm hold
- `ADJUST` - Request adjustment

**Integration with Monitor Workflow:**

```python
# In monitor_workflow.py
from src.api.telegram_bot import TelegramBot

# Start polling when monitoring begins
bot = TelegramBot()
bot.start_polling()

# Check for user responses during monitoring loop
response = bot.get_pending_response()
if response:
    if response.action == CallbackAction.EXIT:
        # Trigger exit
    elif response.action == CallbackAction.HOLD:
        # Continue holding
```

---

## SECTION 6: SCHEDULED JOBS DECODE

SNAIL runs via external cron scheduler (Raspberry Pi or Windows Task Scheduler).

### Cron Schedule

| Job Name | Purpose | Schedule | Command | File |
|----------|---------|----------|---------|------|
| Token Refresh | Refresh Kite token | `45 8 * * 1-5` | External script | `../data/kite_access_token.json` |
| Daily Startup | Morning initialization | `15 9 * * 1-5` | `python main.py startup` | `daily_startup.py` |
| Entry Check | Hourly entry attempt | `20,30 9-14 * * 1-5` | `python main.py entry --execute` | `entry_workflow.py` |
| Monitor Loop | Position monitoring | `16-56/10 9-15 * * 1-5` | `python main.py run` | `monitor_workflow.py` |
| Daily Summary | End of day summary | `30 15 * * 1-5` | `python main.py summary --send` | `daily_summary.py` |

### Cron Expression Reference

| Expression | Meaning |
|------------|---------|
| `45 8 * * 1-5` | 8:45 AM, Mon-Fri |
| `15 9 * * 1-5` | 9:15 AM, Mon-Fri |
| `20,30 9-14 * * 1-5` | :20 and :30 minutes, 9 AM - 2 PM, Mon-Fri |
| `16-56/10 9-15 * * 1-5` | Every 10 min from :16-:56, 9 AM - 3 PM, Mon-Fri |

### Example Crontab (Linux/Raspberry Pi)

```cron
# SNAIL Trading Bot Schedule
# Timezone: Asia/Kolkata (IST)

# Token refresh at 8:45 AM (handled by CROCODILE)
# 45 8 * * 1-5 cd /home/pi/BOTS/CROCODILE && python kite_token_refresh.py

# Daily startup at 9:15 AM
15 9 * * 1-5 cd /home/pi/BOTS/SNAIL && python main.py startup >> logs/cron.log 2>&1

# Entry attempts at 9:20, 9:30, 10:30, 11:30, 12:30, 13:30, 14:30
20 9 * * 1-5 cd /home/pi/BOTS/SNAIL && python main.py entry --execute >> logs/cron.log 2>&1
30 9-14 * * 1-5 cd /home/pi/BOTS/SNAIL && python main.py entry --execute >> logs/cron.log 2>&1

# Monitoring every 10 minutes from 9:16 AM to 3:26 PM
16,26,36,46,56 9-14 * * 1-5 cd /home/pi/BOTS/SNAIL && python main.py run >> logs/cron.log 2>&1
6,16,26 15 * * 1-5 cd /home/pi/BOTS/SNAIL && python main.py run >> logs/cron.log 2>&1

# Daily summary at 3:30 PM
30 15 * * 1-5 cd /home/pi/BOTS/SNAIL && python main.py summary --send >> logs/cron.log 2>&1
```

---

## SECTION 7: DEPLOYMENT GUIDE

### Prerequisites Checklist

- [ ] Python 3.8+ installed
- [ ] pip package manager
- [ ] Git installed
- [ ] Zerodha Kite Connect API credentials
- [ ] Anthropic Claude API key
- [ ] Telegram bot created and token obtained
- [ ] Network access to Zerodha, Anthropic, Telegram APIs
- [ ] Sufficient capital in Zerodha account (min ₹1,20,000)

### Step-by-Step Deployment

```bash
# ============================================================================
# STEP 1: Clone & Setup
# ============================================================================
cd /path/to/projects
git clone <repository_url> SNAIL
cd SNAIL

# Create virtual environment
python -m venv venv

# Activate virtual environment
# Linux/Mac:
source venv/bin/activate
# Windows:
venv\Scripts\activate

# ============================================================================
# STEP 2: Install Dependencies
# ============================================================================
pip install -r requirements.txt

# ============================================================================
# STEP 3: Configure Environment
# ============================================================================
# Copy template and edit
cp .env.template .env

# Edit .env with your credentials
# IMPORTANT: Never commit .env to git!

# Example .env content:
# ZERODHA_API_KEY=your_api_key
# ZERODHA_API_SECRET=your_api_secret
# ZERODHA_USER_ID=your_user_id
# ZERODHA_PASSWORD=your_password
# ZERODHA_TOTP_SECRET=your_totp_secret
# ANTHROPIC_API_KEY=sk-ant-xxxxx
# TELEGRAM_BOT_TOKEN=123456789:ABC-DEF
# TELEGRAM_CHAT_ID=your_chat_id
# SNAIL_PAPER_TRADING=true   # Start with paper trading!

# ============================================================================
# STEP 4: Create Required Directories
# ============================================================================
mkdir -p data logs logs/claude config/prompts ../data

# ============================================================================
# STEP 5: Initialize System
# ============================================================================
python main.py init

# ============================================================================
# STEP 6: Initialize Database
# ============================================================================
python -m src.utils.db init

# ============================================================================
# STEP 7: Verify Configuration
# ============================================================================
python main.py test

# Expected output:
# [1] Testing configuration...
#     ✅ Configuration valid
# [2] Testing database...
#     ✅ Database connected
# [3] Testing Kite authentication...
#     ✅ Authenticated as [Your Name]
# [4] Testing Telegram...
#     ✅ Telegram connected
# [5] Testing Claude API...
#     ✅ Claude API connected

# ============================================================================
# STEP 8: Run First Startup (Paper Trading Mode)
# ============================================================================
python main.py startup

# ============================================================================
# STEP 9: Configure Cron Jobs (Production)
# ============================================================================
# Edit crontab (Linux):
crontab -e
# Add the cron entries from Section 6

# Windows Task Scheduler:
# Create tasks for each command with appropriate triggers

# ============================================================================
# STEP 10: Go Live (When Ready)
# ============================================================================
# 1. Set SNAIL_PAPER_TRADING=false in .env
# 2. Ensure sufficient margin in Zerodha account
# 3. Start with 1 lot
# 4. Monitor closely for first few trades
```

### Environment-Specific Notes

| Environment | Config | Special Steps |
|-------------|--------|---------------|
| Development | `SNAIL_PAPER_TRADING=true` | All orders simulated, no real trades |
| Staging | N/A | Not applicable (single instance) |
| Production | `SNAIL_PAPER_TRADING=false` | Real orders executed, requires margin |

### Shared Files Setup

SNAIL shares certain files with sibling bots (CROCODILE):

```
BOTS/
├── data/                          # Shared data folder
│   ├── kite_access_token.json     # Shared Kite token
│   ├── instruments.csv            # Shared instruments
│   └── holiday_calendar.json      # Shared holidays
├── SNAIL/                         # This project
│   └── data/snail.db              # SNAIL-specific database
└── CROCODILE/                     # Sibling bot
```

Ensure the parent `../data/` directory exists and is accessible.

### Rollback Procedure

```bash
# 1. Stop all running instances
pkill -f "python main.py"

# 2. Check current state
python main.py status

# 3. If active position exists, manually exit via Kite console

# 4. Rollback to previous version (if using git)
git stash
git checkout <previous_commit>

# 5. Restore database from backup
cp data/snail.db.backup data/snail.db

# 6. Restart
python main.py test
python main.py startup
```

---

## SECTION 8: STARTUP & SHUTDOWN

### Startup Sequence

```
1. Load environment variables from .env
2. Parse command line arguments
3. Display ASCII banner (unless --quiet)
4. Setup logging (loguru)
5. Initialize configuration (YAML with env substitution)
6. Validate configuration
7. Initialize database connection
8. Authenticate with Kite API
9. Refresh instruments if stale (>24 hours)
10. Refresh holiday calendar
11. Check for active positions
12. Get market data (NIFTY, VIX)
13. Send morning summary via Telegram
14. Start Telegram bot polling (background thread)
15. Begin monitoring loop
```

### Startup Commands

| Environment | Command |
|-------------|---------|
| Development | `python main.py startup` |
| Production | `python main.py run` (full trading loop) |
| Status Check | `python main.py status` |
| Paper Trading | Set `SNAIL_PAPER_TRADING=true` in .env |

### Graceful Shutdown

```
1. Receive SIGINT (Ctrl+C) or SIGTERM
2. Set _running = False in monitor workflow
3. Wait for current iteration to complete
4. Stop Telegram polling
5. Generate daily summary
6. Send daily summary via Telegram
7. Close database connections
8. Exit with code 0
```

### Manual Shutdown

```bash
# Graceful shutdown
python main.py status  # Check state
# Press Ctrl+C if running interactively

# Force stop
pkill -f "python main.py"

# If position exists, manually exit:
python main.py exit --force
```

---

## SECTION 9: LOGGING & MONITORING

| Attribute | Value |
|-----------|-------|
| Log Library | loguru 0.7.3 |
| Log Format | `{time:YYYY-MM-DD HH:mm:ss} \| {level: <8} \| {name}:{function}:{line} \| {message}` |
| Log Location | `logs/snail.log` |
| Log Rotation | 10 MB per file |
| Log Retention | 30 days |
| Log Compression | zip |

### Log Files

| File | Purpose | Location |
|------|---------|----------|
| `snail.log` | Main application log | `logs/snail.log` |
| `orders.log` | Order execution log | `logs/orders.log` |
| `claude_usage.log` | Claude API usage | `logs/claude_usage.log` |
| `claude/*.json` | Claude decision logs | `logs/claude/` |

### Log Levels

| Level | Usage |
|-------|-------|
| DEBUG | Detailed debugging info |
| INFO | Normal operations |
| WARNING | Non-critical issues (spreads too wide, VIX warning) |
| ERROR | Critical failures (auth failed, order rejected) |
| CRITICAL | System failures |

### Monitoring Commands

```bash
# View live logs
tail -f logs/snail.log

# View today's orders
grep "$(date +%Y-%m-%d)" logs/orders.log

# Check Claude usage
cat logs/claude_usage.log | jq .

# Database queries for monitoring
sqlite3 data/snail.db "SELECT * FROM positions WHERE status='active';"
sqlite3 data/snail.db "SELECT * FROM pnl_snapshots ORDER BY created_at DESC LIMIT 10;"
```

### Health Checks

| Check | Command | Expected Result |
|-------|---------|-----------------|
| System Status | `python main.py status` | Shows market data and position |
| API Connectivity | `python main.py test` | All 5 tests pass |
| Database Health | `python -m src.utils.db` | No errors |
| Active Position | SQL query | 0 or 1 active position |

---

## SECTION 10: DEPENDENCIES

### Runtime Dependencies

| Package | Version | Purpose | Critical |
|---------|---------|---------|----------|
| anthropic | 0.72.0 | Claude AI API | Yes |
| kiteconnect | 5.0.1 | Zerodha trading API | Yes |
| loguru | 0.7.3 | Structured logging | Yes |
| python-dotenv | 1.0.1 | Environment variables | Yes |
| PyYAML | 6.0.2 | Configuration parsing | Yes |
| pandas | 2.0.3 | Data manipulation | Yes |
| pydantic | 2.10.6 | Data validation | No |
| requests | 2.32.4 | HTTP client | Yes |
| beautifulsoup4 | 4.9.1 | Holiday scraping | No |
| pyotp | 2.9.0 | TOTP generation | Yes |
| APScheduler | 3.11.0 | Job scheduling | No |

### System Requirements

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.8+ | 3.8 tested |
| SQLite | 3.x | Built-in |
| Operating System | Windows/Linux | Raspberry Pi supported |
| RAM | 512 MB+ | Minimal requirements |
| Disk Space | 100 MB+ | Plus logs/database growth |
| Network | Stable internet | Required for API calls |

### External Services

| Service | Required For | Fallback |
|---------|--------------|----------|
| Zerodha Kite | Trading execution | None (critical) |
| Anthropic Claude | Decision advisory | Can skip with `--skip-claude` |
| Telegram | Alerts and commands | System continues without alerts |
| NSE (via Kite) | Market data | None (critical) |

---

## SECTION 11: TROUBLESHOOTING

### Common Issues

| Issue | Symptoms | Cause | Solution |
|-------|----------|-------|----------|
| App won't start | Import errors | Missing dependencies | `pip install -r requirements.txt` |
| Kite auth failing | "Authentication failed" | Expired token | Refresh token at 8:45 AM |
| | "Invalid TOTP" | Clock sync issue | Sync system time with NTP |
| | "API key mismatch" | Wrong credentials | Verify `.env` values |
| No market data | "Connection timeout" | Network issue | Check internet, retry |
| | "Invalid token" | Token expired | Re-authenticate |
| Entry blocked | "VIX out of range" | VIX < 10 or > 16 | Wait for favorable conditions |
| | "Active position exists" | Already holding | Exit existing position first |
| | "On cooldown" | Recent exit | Wait 1 trading day or `python main.py cooldown --clear entry` |
| | "user_skip cooldown" | User skipped earlier | `python main.py cooldown --clear user_skip` |
| Orders rejected | "Insufficient margin" | Low capital | Add funds to Zerodha |
| | "Order rejected by exchange" | Invalid price | Check tick size |
| Claude errors | "Rate limit" | Too many requests | Wait 60 seconds |
| | "API key invalid" | Wrong key | Verify Anthropic key |
| Telegram not working | "Bot token invalid" | Wrong token | Verify with @BotFather |
| | "Chat not found" | Wrong chat ID | Get correct ID from API |
| | Commands not responding | Polling not started | Check `bot.start_polling()` called |
| | Duplicate responses | Deduplication failed | Restart bot, check logs |
| | Buttons not working | Callback not processed | Check `_handle_callback_query()` |
| | Response not received after `git pull` | Telegram poller has old code | Restart: `sudo systemctl restart snail-telegram` |
| | First response ignored, second works | Poller daemon needs restart | Always restart after code changes |

### Debug Commands

```bash
# Enable debug logging
python main.py --debug run

# View detailed logs
tail -100 logs/snail.log | grep -i error

# Check Kite authentication
python -c "
from src.api.kite_client import get_kite_client
kite = get_kite_client()
kite.ensure_authenticated()
print(kite.profile())
"

# Test Telegram alerts
python -c "
from src.api.telegram_alerts import get_telegram
tg = get_telegram()
print(tg.test_connection())
"

# Test Telegram bot polling
python -c "
from src.api.telegram_bot import TelegramBot
bot = TelegramBot()
print('Bot initialized, starting polling...')
bot.start_polling()
import time; time.sleep(10)  # Wait for updates
bot.stop_polling()
print('Polling test complete')
"

# Test Claude
python -c "
from src.api.claude_client import get_claude_client
claude = get_claude_client()
print(claude.test_connection())
"

# Database check
sqlite3 data/snail.db ".tables"
sqlite3 data/snail.db "SELECT * FROM system_status;"

# Check active position
python main.py status
```

### Post-Deployment Restart Requirements

After pulling code changes (`git pull`), always restart the telegram poller daemon:

```bash
# Check current status
sudo systemctl status snail-telegram

# Restart the telegram poller daemon
sudo systemctl restart snail-telegram

# Verify it's running
sudo systemctl status snail-telegram
```

**Why is this necessary?**
The telegram poller runs as a separate daemon process. When you `git pull`, the running process still has the old code in memory. Without a restart:
- First response may be ignored (old code doesn't recognize new patterns)
- Only after timeout or second attempt does it work (if service auto-restarts)

**Quick deploy checklist:**
```bash
cd ~/bots && git pull
sudo systemctl restart snail-telegram
python SNAIL/main.py cooldown  # Verify cooldown status if needed
```

### Error Recovery

**If entry fails mid-execution:**
1. Check Kite positions for partial fills
2. Manually close any orphan positions
3. Clear system status: `UPDATE system_status SET status='normal';`
4. Retry entry

**If exit fails:**
1. Critical: Check Kite for actual position
2. Use Kite web/app to manually exit
3. Update database: `UPDATE positions SET status='closed' WHERE id=X;`
4. Set cooldown manually if needed

**If database corrupted:**
1. Stop all instances
2. Backup corrupted file: `mv snail.db snail.db.corrupted`
3. Reinitialize: `python -m src.utils.db init`
4. Manual reconciliation needed

### Cooldown Management

SNAIL uses cooldowns to prevent repeated entry attempts and API calls.

**Cooldown Types:**

| Type | Duration | Trigger | Purpose |
|------|----------|---------|---------|
| `entry` | 1 day | After position exit | Prevent same-day re-entry |
| `user_skip` | 10 hours | User selects SKIP in pre-entry | Prevent repeated prompts during same session |

**Check Cooldown Status:**
```bash
python main.py cooldown
```

**Clear Cooldowns:**
```bash
# Clear all active cooldowns
python main.py cooldown --clear

# Clear only user_skip cooldown (to retry entry)
python main.py cooldown --clear user_skip

# Clear only entry cooldown (after manual exit)
python main.py cooldown --clear entry
```

**Common Scenarios:**

1. **User accidentally skipped entry** → `python main.py cooldown --clear user_skip` → `python main.py entry`

2. **Want to test entry without waiting** → `python main.py cooldown --clear` → `python main.py entry`

3. **Manually exited position, want to re-enter** → `python main.py cooldown --clear entry` → `python main.py entry`

**Database Inspection:**
```sql
-- View active cooldowns
SELECT * FROM cooldowns WHERE cooldown_end > date('now');

-- Manually clear all cooldowns (use with caution)
DELETE FROM cooldowns WHERE cooldown_end > date('now');
```

---

## SECTION 12: GOTCHAS & HIDDEN COMPLEXITIES

### Non-Obvious Behaviors

| Behavior | Location | Description |
|----------|----------|-------------|
| Tiered slippage | `entry_manager.py:487` | Entry uses progressive slippage (2→3→5→MARKET) |
| Soft VIX buffer | `entry_manager.py:250-285` | VIX 9.5-10 and 16-16.5 allowed with warning |
| Friday auto-exit | `exit_manager.py:286-301` | Auto-exit at 3:15 PM Friday if position > 2 days to expiry |
| Expiry day exit | `exit_manager.py:303-317` | Forced exit at 3:20 PM on expiry day |
| VIX hard exit | `exit_manager.py:258-270` | Auto-exit if VIX > 20 (non-negotiable) |
| Cooldown period | `entry_manager.py:208-213` | Cannot enter for 1 trading day after exit |
| User skip cooldown | `claude_advisor.py` | 10-hour cooldown after user skips pre-entry |
| Position verification | `entry_manager.py:499-522` | Verifies Kite positions match expected after entry |
| Gap detection | `monitor_workflow.py:191-346` | 9:16 AM gap check, different thresholds for severity |
| Alert deduplication | `alert_dedup.py` | Prevents spam by rate-limiting duplicate alerts |

### Hardcoded Values (Should Be Configurable)

| Value | Location | Current Value |
|-------|----------|---------------|
| NIFTY instrument token | `kite_client.py:103` | `256265` |
| Gap check time | `monitor_workflow.py:49` | `9:16 AM` |
| Market close time | `monitor_workflow.py:50` | `3:30 PM` |
| Entry start time | `entry_manager.py:67` | `09:20` |
| Entry end time | `entry_manager.py:68` | `14:30` |
| Loop intervals | `monitor_workflow.py:47-48` | 60s idle, 30s active |
| Claude retry delay | `claude_client.py:39` | 2 seconds |
| Max Claude retries | `claude_client.py:38` | 3 |

### Technical Debt

| Item | Location | Risk | Action Needed |
|------|----------|------|---------------|
| No position reconciliation | System-wide | Medium | Add Kite position sync on startup |
| Single lot only | `config.yaml:25` | Low | Multi-lot support in calculations |
| No historical backtest | N/A | Low | Add backtesting module |
| No web dashboard | N/A | Low | Add Flask/FastAPI UI |
| SQLite scalability | `db.py` | Low | Consider PostgreSQL for multi-user |
| Hardcoded prompts | `prompts/*.txt` | Low | Make prompts configurable via DB |

### Important Timing Considerations

1. **Token refresh must complete before 9:15 AM** - Otherwise startup fails
2. **9:16 AM is critical for gap detection** - First monitor run captures day open
3. **3:00 PM Friday decision point** - Claude decides weekend carry
4. **3:15 PM Friday auto-exit** - If decision is to exit
5. **3:20 PM expiry day forced exit** - No exceptions
6. **1-day cooldown after exit** - Cannot re-enter same day or next day

---

## SECTION 13: QUICK REFERENCE

### Essential Commands

```bash
Start:      python main.py run
Stop:       Ctrl+C (graceful) or pkill -f "python main.py"
Restart:    python main.py run
Logs:       tail -f logs/snail.log
Status:     python main.py status
Entry:      python main.py entry --execute
Exit:       python main.py exit --force
Summary:    python main.py summary --send
Test:       python main.py test
Init:       python main.py init
Cooldown:   python main.py cooldown              # Check cooldown status
            python main.py cooldown --clear      # Clear all cooldowns
            python main.py cooldown --clear user_skip  # Clear specific type
```

### Key File Locations

```
Entry Point:    main.py
Config:         config/config.yaml
Environment:    .env
Database:       data/snail.db
Schema:         data/schema.sql
Logs:           logs/snail.log
Prompts:        config/prompts/*.txt
Shared Token:   ../data/kite_access_token.json
Shared Instrs:  ../data/instruments.csv
```

### Key Module Paths

```
API Clients:    src/api/kite_client.py, claude_client.py, telegram_alerts.py
Services:       src/services/entry_manager.py, exit_manager.py, position_monitor.py
Workflows:      src/workflows/daily_startup.py, monitor_workflow.py
Utilities:      src/utils/db.py, calculations.py, symbol_builder.py
```

### Trading Parameters Quick Ref

```
Instrument:     NIFTY Weekly Options
Strategy:       Iron Fly (4-leg)
VIX Range:      10-16 (soft: 9.5-16.5)
Min DTE:        6 days
Entry Window:   9:30 AM - 2:30 PM
Profit Target:  50% of max profit
Stop Loss:      50% of max loss (advisory)
VIX Hard Exit:  > 20
Friday Exit:    3:15 PM
Expiry Exit:    3:20 PM
Cooldowns:
  - entry:      1 trading day (after position exit)
  - user_skip:  10 hours (after user skips pre-entry)
```

### Database Quick Queries

```sql
-- Active position
SELECT * FROM positions WHERE status = 'active';

-- Today's P&L snapshots
SELECT * FROM pnl_snapshots WHERE date(created_at) = date('now');

-- Recent orders
SELECT * FROM orders ORDER BY created_at DESC LIMIT 10;

-- Claude decisions today
SELECT * FROM claude_decisions WHERE date(created_at) = date('now');

-- System status
SELECT * FROM system_status WHERE cleared_at IS NULL;

-- Entry attempts today
SELECT * FROM entry_attempts WHERE date(attempt_time) = date('now');

-- Active cooldowns
SELECT * FROM cooldowns WHERE cooldown_end > date('now');

-- Clear all cooldowns
DELETE FROM cooldowns WHERE cooldown_end > date('now');
```

---

## VERIFICATION CHECKLIST

Before finalizing deployment, confirm:

- [x] Every directory explained
- [x] Every config file documented
- [x] Every environment variable listed
- [x] All API integrations catalogued
- [x] Every scheduled job documented
- [x] Deployment steps are reproducible
- [x] Troubleshooting covers common issues
- [x] Commands tested and documented
- [x] Database schema fully documented
- [x] All service dependencies listed
- [x] Trading parameters documented
- [x] Startup/shutdown procedures clear
- [x] Logging and monitoring explained
- [x] Hidden complexities documented

---

## APPENDIX A: Claude AI Prompt Templates

Located in `config/prompts/`:

| File | Trigger | Decision Options |
|------|---------|------------------|
| `pre_entry.txt` | Before Iron Fly entry | PROCEED, SKIP |
| `stop_loss_advisory.txt` | 50% max loss reached | HOLD, EXIT |
| `wing_approach.txt` | NIFTY near wing strike | HOLD, EXIT, ADJUST |
| `friday_decision.txt` | Friday 3:00 PM | HOLD, EXIT |
| `eod_decision.txt` | End of day check | HOLD, EXIT |
| `vix_spike.txt` | VIX spike detected | HOLD, EXIT |
| `market_event.txt` | Major news event | HOLD, EXIT, WAIT |
| `gap_beyond_wing.txt` | Gap open beyond wing | HOLD, EXIT |
| `adjustment.txt` | Position adjustment request | ADJUST options |

---

## APPENDIX B: Iron Fly Strategy Details

### Position Structure

```
                    Iron Fly Position
                    ==================

    WING_PE (Long)                        WING_CE (Long)
         |                                      |
         |   ← Wing Distance →   ← Wing Distance →   |
         |                                      |
    ATM-300    ←─────────── ATM ───────────→    ATM+300
                            |
                     STRADDLE (Short)
                     ATM CE + ATM PE
```

### P&L Profile

```
    Profit
      ↑
      │         ╭───────────────────────────╮
      │        ╱                             ╲
      │       ╱   Max Profit = Net Credit     ╲
    ──┼──────╱─────────────────────────────────╲───────→ NIFTY
      │     ╱                                   ╲
      │    ╱                                     ╲
    ──┼───╳─────────────────────────────────────────╳───
      │  Wing PE                              Wing CE
      │   (Max Loss = Wing Distance - Net Credit)
    Loss
```

### Calculations

```
Net Credit = (ATM CE Bid + ATM PE Bid) - (Wing CE Ask + Wing PE Ask)
Max Profit = Net Credit × Lot Size
Max Loss = (Wing Distance - Net Credit) × Lot Size
Breakeven Upper = ATM Strike + Net Credit
Breakeven Lower = ATM Strike - Net Credit
P&L at Exit = (Entry Credit - Exit Debit) × Lot Size
P&L % = Current P&L / Max Profit × 100
```

---

*End of SNAIL Operational Guide*
