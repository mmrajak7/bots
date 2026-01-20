# Striker CLI

Interactive Options Trading CLI for NEO API with natural language commands.

## Table of Contents
- [Quick Start](#quick-start)
- [Features](#features)
- [Complete Command Reference](#complete-command-reference)
- [Workflows](#workflows)
- [Configuration](#configuration)
- [Examples](#examples)

---

## Quick Start

```bash
cd Striker
python striker.py
```

The CLI will:
1. Connect to NEO API (for trading) and Kite API (for market data)
2. Show market status (OPEN/CLOSED)
3. Display a prompt ready for commands

```
╔══════════════════════════════════════════════════════════════╗
║                      STRIKER CLI v1.0                        ║
║              Options Trading Terminal for NEO                ║
╚══════════════════════════════════════════════════════════════╝

  Market Status: OPEN
  Ready. Type a command or 'help' for options.

striker>
```

---

## Features

| Feature | Description |
|---------|-------------|
| Natural Language Commands | Build strategies with intuitive commands like "NIFTY iron condor" |
| Bid-Ask Adjusted Pricing | Shows realistic entry/exit using bid (SELL) and ask (BUY) prices |
| Liquidity Analysis | Warns about low OI, volume, or wide bid-ask spreads |
| Greeks Display | Shows net Delta and Theta for position risk |
| Market Hours Check | Blocks trading outside 9:15 AM - 3:30 PM |
| MIS Squareoff Warning | Alerts when approaching 3:25 PM squareoff |
| Margin Check | Verifies margin before trade execution |
| Daily Loss Limit | Configurable session loss limit |
| DTE Warnings | Alerts for low days-to-expiry positions |
| Trade Logging | All trades logged to `logs/trades_YYYYMMDD.json` |
| Session P&L | Real-time tracking of credits, debits, and net P&L |
| Partial Exit | Exit 25%, 50%, 75% of positions |
| Roll Functionality | Roll positions to next expiry |
| Stop Loss Orders | Set SL at % or absolute value via NEO |
| Target Orders | Set profit targets via NEO |
| Trailing Stop Loss | Lock in profits as position moves favorably |

---

## Complete Command Reference

### 1. Strategy Building Commands

Build option strategies using natural language. The CLI understands various formats.

#### Vertical Spreads

| Command | Strategy | Description |
|---------|----------|-------------|
| `NIFTY debit spread` | Bull Call Spread | Buy ATM CE, Sell OTM CE |
| `NIFTY bull call spread` | Bull Call Spread | Same as above |
| `NIFTY credit spread` | Bull Put Spread | Sell OTM PE, Buy further OTM PE |
| `NIFTY bull put spread` | Bull Put Spread | Same as above |
| `NIFTY bear call spread` | Bear Call Spread | Sell OTM CE, Buy further OTM CE |
| `NIFTY credit call spread` | Bear Call Spread | Same as above |
| `NIFTY bear put spread` | Bear Put Spread | Buy ATM PE, Sell OTM PE |
| `NIFTY debit put spread` | Bear Put Spread | Same as above |

#### Straddles & Strangles

| Command | Strategy | Description |
|---------|----------|-------------|
| `NIFTY straddle` | Long Straddle | Buy ATM CE + ATM PE |
| `NIFTY long straddle` | Long Straddle | Same as above |
| `NIFTY short straddle` | Short Straddle | Sell ATM CE + ATM PE |
| `NIFTY strangle` | Long Strangle | Buy OTM CE + OTM PE |
| `NIFTY short strangle` | Short Strangle | Sell OTM CE + OTM PE |

#### Iron Strategies

| Command | Strategy | Description |
|---------|----------|-------------|
| `NIFTY iron condor` | Iron Condor | Short strangle + long wings |
| `NIFTY condor` | Iron Condor | Same as above |
| `NIFTY iron butterfly` | Iron Butterfly | Short straddle + long wings |
| `NIFTY iron fly` | Iron Butterfly | Same as above |

#### Modifiers

Add these to any strategy command:

| Modifier | Example | Description |
|----------|---------|-------------|
| `X lots` | `NIFTY iron condor 2 lots` | Trade 2 lots instead of 1 |
| `X width` | `NIFTY debit spread 100 width` | Set strike width to 100 points |

#### Supported Symbols

- **Indices**: NIFTY, BANKNIFTY, FINNIFTY, SENSEX, MIDCPNIFTY
- **Stocks**: All F&O stocks (RELIANCE, TCS, INFY, HDFCBANK, ICICIBANK, SBIN, etc.)

---

### 2. Trading Commands

| Command | Description |
|---------|-------------|
| `trade` | Execute the last built strategy |
| `go` | Same as trade |
| `yes` | Same as trade |
| `execute` | Same as trade |

**Trade Execution Flow:**
1. Checks market hours (blocks if closed)
2. Checks daily loss limit (blocks if breached)
3. Checks margin availability
4. Refreshes prices for staleness
5. Shows warnings (liquidity, DTE)
6. Asks for confirmation
7. Executes BUY legs first, then SELL legs (safety)
8. Offers rollback if SELL legs fail

---

### 3. Position Management Commands

| Command | Description |
|---------|-------------|
| `positions` | Show all open positions grouped by underlying |
| `pos` | Same as positions |
| `orders` | Show pending/open orders |
| `exit NIFTY` | Exit 100% of all NIFTY positions |
| `exit 50 NIFTY` | Exit 50% of NIFTY positions |
| `exit 25 NIFTY` | Exit 25% of NIFTY positions |
| `exit 75 NIFTY` | Exit 75% of NIFTY positions |
| `exit all` | Exit ALL open positions |
| `roll NIFTY` | Roll NIFTY positions to next expiry |

**Exit Order:**
- SHORTS are closed first (buy back), then LONGS (sell)
- This reduces margin requirement during exit

---

### 4. Risk Management Commands (SL/Target/TSL)

#### Stop Loss

| Command | Description |
|---------|-------------|
| `sl NIFTY 50%` | Set SL at 50% of entry premium |
| `sl NIFTY 2000` | Set SL at Rs.2000 absolute loss |
| `stoploss NIFTY 50%` | Same as sl |
| `stop NIFTY 50%` | Same as sl |

**How SL Works:**
- Places SL-L (Stop Loss Limit) orders in NEO for each leg
- For SHORT positions: triggers when price rises (buy back)
- For LONG positions: triggers when price falls (sell)
- Limit price is set 1% beyond trigger for execution safety

#### Target

| Command | Description |
|---------|-------------|
| `target NIFTY 80%` | Set target at 80% of max profit |
| `target NIFTY 3000` | Set target at Rs.3000 profit |
| `tp NIFTY 80%` | Same as target |

**How Target Works:**
- Places Limit orders in NEO for each leg
- For SHORT positions: buy back at lower price
- For LONG positions: sell at higher price

#### Trailing Stop Loss

| Command | Description |
|---------|-------------|
| `tsl NIFTY` | Trail SL to lock 50% of current profit |
| `tsl NIFTY 70%` | Trail SL to lock 70% of current profit |
| `trail NIFTY` | Same as tsl |

**How TSL Works:**
1. Only works when position is in profit
2. Cancels any existing SL order
3. Places new SL order to lock in X% of current profit
4. Run `tsl NIFTY` again as profits increase to update

#### Cancel Orders

| Command | Description |
|---------|-------------|
| `cancel sl NIFTY` | Cancel SL order for NIFTY |
| `cancel target NIFTY` | Cancel target order for NIFTY |

---

### 5. Information Commands

| Command | Description |
|---------|-------------|
| `NIFTY` | Show NIFTY option chain (just type the symbol) |
| `spot NIFTY` | Show NIFTY spot price |
| `spot RELIANCE` | Show RELIANCE spot price |
| `margin` | Show available margin/funds |
| `funds` | Same as margin |
| `status` | Show session statistics and P&L |
| `pnl` | Same as status |
| `stats` | Same as status |

---

### 6. System Commands

| Command | Description |
|---------|-------------|
| `help` | Show command help |
| `?` | Same as help |
| `refresh` | Clear cache and refresh data |
| `quit` | Exit the CLI |
| `q` | Same as quit |

---

## Workflows

### Workflow 1: Build and Execute a Strategy

```
striker> NIFTY iron condor

======================================================================
  Iron Condor 23200/23300/23700/23800
======================================================================
  Symbol: NIFTY  |  Spot: Rs.23,512.00  |  DTE: 5 days
  Expiry: 23-Jan-2026  |  Lot Size: 75
----------------------------------------------------------------------
  LEGS:
  #   Action Strike   Type     Bid      Ask     Exec      Liq
  ------------------------------------------------------------------
  1   SELL   23700    CE      84.50    85.50    84.50     HIGH
  2   BUY    23800    CE      51.75    52.25    52.25     HIGH
  3   SELL   23300    PE      77.00    78.00    77.00     HIGH
  4   BUY    23200    PE      48.25    48.75    48.75   MEDIUM
----------------------------------------------------------------------
  ANALYSIS (Bid-Ask Adjusted):
    Net Credit:       Rs.4,537.50
    (LTP shows:       Rs.4,687.50)
    Est. Slippage:    Rs.150.00

    Max Profit:       Rs.4,537.50
    Max Loss:         Rs.2,962.50
    Breakeven:        Rs.23,239.50, Rs.23,760.50
    Risk:Reward:      1:1.53
----------------------------------------------------------------------
  GREEKS:
    Net Delta:        +0.05
    Net Theta:        Rs.+156.00/day
======================================================================

  Type 'trade' to execute this strategy.

striker> trade

  Margin Check: Margin OK. Available: Rs.125,000

  About to execute: Iron Condor 23200/23300/23700/23800
  Symbol: NIFTY
  Legs: 4
  Net Credit: Rs.4,537.50

  Refreshing prices...
  Prices are current

  Confirm trade? (yes/no): yes

  Execution order (BUY legs first for safety):
    1. BUY 23800 CE
    2. BUY 23200 PE
    3. SELL 23700 CE
    4. SELL 23300 PE

  Executing trades...
    Leg 1: BUY 23800 CE @ Rs.52.25 -> 241016000012345
    Leg 2: BUY 23200 PE @ Rs.48.75 -> 241016000012346
    Leg 3: SELL 23700 CE @ Rs.84.50 -> 241016000012347
    Leg 4: SELL 23300 PE @ Rs.77.00 -> 241016000012348

  Completed: 4 success, 0 failed

  Trade logged. Session stats: 1 trades, Net: Rs.4,537.50
```

### Workflow 2: Set SL and Target After Entry

```
striker> sl NIFTY 50%

  Setting SL at 50% of entry premium (Rs.2,268.75)

  Placing SL orders for 4 position(s)...
    NIFTY24JAN23700CE: SL Trigger Rs.126.75, Limit Rs.128.02
    NIFTY24JAN23800CE: SL Trigger Rs.26.13, Limit Rs.25.86
    NIFTY24JAN23300PE: SL Trigger Rs.115.50, Limit Rs.116.66
    NIFTY24JAN23200PE: SL Trigger Rs.24.13, Limit Rs.23.88
    SL order placed: 241016000012349
    SL order placed: 241016000012350
    SL order placed: 241016000012351
    SL order placed: 241016000012352

striker> target NIFTY 80%

  Setting target at 80% profit (Rs.3,630.00)

  Placing Target orders for 4 position(s)...
    NIFTY24JAN23700CE: Target Rs.16.90
    NIFTY24JAN23800CE: Target Rs.10.45
    NIFTY24JAN23300PE: Target Rs.15.40
    NIFTY24JAN23200PE: Target Rs.9.65
    Target order placed: 241016000012353
    ...
```

### Workflow 3: Trail Stop Loss as Profits Increase

```
striker> positions

================================================================================
  CURRENT POSITIONS
================================================================================

  NIFTY
  ----------------------------------------------------------------------------
    SHORT   75 @ Rs.   84.50  |  LTP: Rs.   60.00  |  P&L:   Rs.1,837.50  [24JAN23700CE]
    LONG    75 @ Rs.   52.25  |  LTP: Rs.   35.00  |  P&L:  Rs.-1,293.75  [24JAN23800CE]
    SHORT   75 @ Rs.   77.00  |  LTP: Rs.   55.00  |  P&L:   Rs.1,650.00  [24JAN23300PE]
    LONG    75 @ Rs.   48.75  |  LTP: Rs.   32.00  |  P&L:  Rs.-1,256.25  [24JAN23200PE]
  ----------------------------------------------------------------------------
  NIFTY P&L: Rs.937.50
  [ SL: Rs.2,269 (50%) | Target: Rs.3,630 (80%) ]

================================================================================
  TOTAL P&L: Rs.937.50
================================================================================

striker> tsl NIFTY 60%

  Trailing Stop Loss for NIFTY
  Current P&L: Rs.937.50

  Locking in 60% of profit = Rs.562.50

  Cancelling existing SL order...
    Old SL cancelled

  Placing trailed SL orders...
    NIFTY24JAN23700CE: Trailed SL Trigger Rs.77.00
    NIFTY24JAN23800CE: Trailed SL Trigger Rs.42.00
    NIFTY24JAN23300PE: Trailed SL Trigger Rs.69.00
    NIFTY24JAN23200PE: Trailed SL Trigger Rs.39.00
    TSL order placed: 241016000012360

  TSL active. Run 'tsl NIFTY' again to update as profits increase.
```

### Workflow 4: Partial Exit

```
striker> exit 50 NIFTY

  Partial Exit: 50% of positions matching 'NIFTY':
    NIFTY24JAN23700CE (-75) -> Exit 37
    NIFTY24JAN23300PE (-75) -> Exit 37
    NIFTY24JAN23800CE (75) -> Exit 37
    NIFTY24JAN23200PE (75) -> Exit 37

  Proceed with partial exit? (yes/no): yes

  Executing partial exits...
    50% exit placed for NIFTY24JAN23700CE
    50% exit placed for NIFTY24JAN23300PE
    50% exit placed for NIFTY24JAN23800CE
    50% exit placed for NIFTY24JAN23200PE
```

### Workflow 5: Roll to Next Expiry

```
striker> roll NIFTY

  Roll positions matching 'NIFTY':
    SHORT: NIFTY24JAN23700CE (-75) @ Rs.60.00
    LONG: NIFTY24JAN23800CE (75) @ Rs.35.00
    SHORT: NIFTY24JAN23300PE (-75) @ Rs.55.00
    LONG: NIFTY24JAN23200PE (75) @ Rs.32.00

  Roll to next expiry: 30-Jan-2026

  WARNING: Rolling will:
    1. Close all current positions at MARKET
    2. Open equivalent positions in next expiry at MARKET
    There may be slippage and price difference between expiries.

  Proceed with roll? (yes/no): yes

  Step 1: Closing current positions...
    Closed NIFTY24JAN23700CE
    Closed NIFTY24JAN23800CE
    Closed NIFTY24JAN23300PE
    Closed NIFTY24JAN23200PE

  Step 2: Opening new positions in next expiry...

  Positions closed. To complete the roll:
    1. Build your strategy for the new expiry:
       > NIFTY iron condor
    2. Execute with 'trade'
```

### Workflow 6: Check Session Stats

```
striker> status

==================================================
  SESSION STATISTICS
==================================================
  Session Start:    10:15:32
  Trades Executed:  3
--------------------------------------------------
  Total Credits:    Rs.9,075.00
  Total Debits:     Rs.2,100.00
  Net Premium:      Rs.6,975.00
--------------------------------------------------
  Daily Loss Limit: Rs.10,000
  Loss Used:        Rs.0 (0.0%)
  Remaining:        Rs.10,000
==================================================
```

---

## Configuration

Edit `config/settings.yaml`:

```yaml
# Default trading preferences
trading:
  default_lots: 1
  product: "MIS"               # MIS for intraday

# Risk management
risk:
  max_loss_per_trade: 5000     # Max loss per strategy
  daily_loss_limit: 10000      # Session loss limit (0 to disable)
  warn_unlimited_risk: true    # Warn for naked options

# Liquidity thresholds
liquidity:
  min_oi: 1000                 # Minimum OI for trading
  min_volume: 100              # Minimum volume
  max_spread_pct: 5            # Max bid-ask spread %
  warn_illiquid: true          # Show liquidity warnings

# Display
display:
  color_enabled: true
  show_greeks: true            # Show delta, theta
  show_bid_ask: true           # Show bid-ask spread
```

### API Credentials

Create `../Scalper/config/credentials.yaml`:

```yaml
neo_credentials:
  consumer_key: "YOUR_KEY"
  mobile_number: "+91XXXXXXXXXX"
  ucc: "YOUR_UCC"
  mpin: "XXXXXX"
  totp_secret: "YOUR_TOTP_SECRET"

kite_credentials:
  api_key: "YOUR_KITE_API_KEY"
```

---

## Safety Features

### Trade Execution Safety

1. **BUY First, SELL After**: Prevents naked short exposure
2. **Rollback on Failure**: Offers to close orphan BUY positions if SELL fails
3. **Price Staleness Check**: Warns if prices are >2 minutes old
4. **Confirmation Required**: Always asks before executing

### Risk Controls

1. **Market Hours**: Blocks trading outside 9:15 AM - 3:30 PM
2. **MIS Warning**: Alerts at 3:15 PM about squareoff
3. **Margin Check**: Verifies funds before trade
4. **Daily Loss Limit**: Halts trading when limit breached
5. **Liquidity Warnings**: Flags illiquid strikes

### Exit Safety

1. **SHORTS First**: Closes short positions before longs
2. **Partial Exit**: Reduces exposure gradually
3. **Position Grouping**: Shows positions grouped by underlying

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Not connected to NEO API" | Check credentials in `../Scalper/config/credentials.yaml` |
| "Market is closed" | Trade only during 9:15 AM - 3:30 PM on weekdays |
| "Insufficient margin" | Reduce lots or add funds |
| "No open position found" | Check symbol spelling, use `positions` to see all |
| Strategy shows "ILLIQUID" | Choose strikes closer to ATM |
| SL order rejected | Check if trigger price is valid (not too close to LTP) |

---

## Quick Reference Card

```
STRATEGY COMMANDS:
  <SYMBOL> debit spread       Bull call spread
  <SYMBOL> credit spread      Bull put spread
  <SYMBOL> iron condor        Iron condor
  <SYMBOL> short straddle     Short straddle
  ... 2 lots                  Multiple lots
  ... 100 width               Set strike width

TRADING:
  trade / go / yes            Execute strategy
  positions / pos             Show positions
  exit <SYMBOL>               Exit position
  exit 50 <SYMBOL>            Partial exit
  exit all                    Exit everything

RISK MANAGEMENT:
  sl <SYMBOL> 50%             Set stop loss
  target <SYMBOL> 80%         Set target
  tsl <SYMBOL>                Trail stop loss
  cancel sl <SYMBOL>          Cancel SL
  cancel target <SYMBOL>      Cancel target

INFO:
  <SYMBOL>                    Show option chain
  margin                      Show funds
  status                      Show session P&L
  help                        Show help
  quit                        Exit CLI
```
