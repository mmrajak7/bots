# Helper Bot - Claude Instructions

This folder contains helper utilities for options trade analysis and execution.

---

## DAILY WORKFLOW (MANDATORY)

```
┌─────────────────────────────────────────────────────────────────┐
│  STEP 1: Refresh Options Data (MUST RUN FIRST EVERY MORNING)   │
│  ─────────────────────────────────────────────────────────────  │
│  python kite_nse_options.py                                     │
│                                                                 │
│  Creates:                                                       │
│    • nse_stocks_options.csv      (all stock options)            │
│    • nse_stocks_options_summary.csv                             │
│    • index_options.csv           (NIFTY, SENSEX options)        │
├─────────────────────────────────────────────────────────────────┤
│  STEP 2: Analyze Butterfly Trades                               │
│  ─────────────────────────────────────────────────────────────  │
│  python butterfly_analyzer.py NIFTY BUY                         │
│  python butterfly_analyzer.py ICICIBANK BUY --execute           │
├─────────────────────────────────────────────────────────────────┤
│  STEP 3: Check Open Positions                                   │
│  ─────────────────────────────────────────────────────────────  │
│  python position_checker.py                                     │
└─────────────────────────────────────────────────────────────────┘
```

---

## CRITICAL RULES

### 1. Symbol Derivation (NEVER construct manually)
```
ALWAYS read tradingsymbol from CSV files:
  • Stock options  → nse_stocks_options.csv (column: option_tradingsymbol)
  • Index options  → index_options.csv (column: tradingsymbol)

WHY: Symbol format changes frequently (e.g., NIFTY25JAN → NIFTY26JAN)
     Manual construction WILL break. CSV is the source of truth.
```

### 2. Lot Sizes (ALWAYS from CSV)
```
  • Stock options  → nse_stocks_options.csv (column: option_lot_size)
  • Index options  → index_options.csv (column: lot_size)

NEVER hardcode lot sizes - they change with exchange circulars.
```

### 3. Pricing (ALWAYS use BID-ASK, never LTP)
```
WHY: LTP is unreliable due to low liquidity in options.

For BUYING options  → Use ASK price (what you pay)
For SELLING options → Use BID price (what you receive)

Close value calculation:
  • Long positions  → Sell at BID
  • Short positions → Buy back at ASK
```

---

## Scripts

| Step | Script | Purpose |
|------|--------|---------|
| 1 | `kite_nse_options.py` | Fetch all option symbols & lot sizes to CSV (RUN FIRST!) |
| 2 | `butterfly_analyzer.py` | Analyze and execute butterfly trades |
| 3 | `position_checker.py` | Check P&L of open positions |

---

## Kite API Authentication

### Token Location (IMPORTANT)
```
BOTS/data/kite_access_token.json   ← ALWAYS CHECK HERE FIRST
```

**Token file structure:**
```json
{
  "access_token": "xxxxx",
  "api_key": "REDACTED_API_KEY",
  "user_id": "YL6478",
  "generated_at": "2025-12-31T08:45:06",
  "valid_until": "6:00 AM IST next day"
}
```

### Token Usage Rules
1. **ALWAYS check `BOTS/data/kite_access_token.json` FIRST**
2. Token is shared across all bots (SNAIL, CROCODILE, Helper)
3. Token is auto-generated at **8:45 AM IST daily** by scheduled task
4. Token expires at **6:00 AM IST next day**
5. Do NOT trigger re-authentication if token exists and is from today

### Quick Kite Access (for scripts)
```python
import json
from kiteconnect import KiteConnect

# Load token directly
with open('../data/kite_access_token.json') as f:
    token_data = json.load(f)

kite = KiteConnect(api_key=token_data['api_key'])
kite.set_access_token(token_data['access_token'])

# Now use kite.ltp(), kite.quote(), etc.
```

### Login Failure Handling
If Kite authentication fails:
1. **Check token file exists** at `../data/kite_access_token.json`
2. **Verify token date** - is `generated_at` from today?
3. If token is stale/missing, inform user to run SNAIL auth
4. Do NOT proceed with any API calls if auth fails

---

## Butterfly Trade Rules

### DTE (Days to Expiry) Requirements
| Instrument Type | Minimum DTE |
|-----------------|-------------|
| Stock Options | 20 days |
| Index Options (NIFTY, SENSEX) | 6 days |

### Wing Distance Calculation
- **Formula:** `Wing_Distance = ATM_Premium × Multiplier`
- **Default Multiplier:** 1.0 (can be adjusted with `--multiplier` flag)
- **Rounding:** To nearest strike interval (auto-detected from option chain)
- **Minimum:** 2× strike interval or 5 points, whichever is greater

### Filter Thresholds
| Filter | Threshold | Action |
|--------|-----------|--------|
| Bid-Ask Spread | < Rs 1.50/leg | FAIL if exceeded |
| Open Interest (ATM) | > 5,000 contracts | WARN if low |
| Daily Volume (ATM) | > 50,000 | WARN if low |
| Risk:Reward Ratio | > 3:1 | WARN |
| Risk:Reward Ratio | > 5:1 | FAIL |

### Strike Selection
- ATM Strike: Nearest available strike to underlying LTP
- Wing Strikes: Nearest available strikes at calculated wing distance
- Strike interval auto-detected from option chain (e.g., 50 for NIFTY, 2.5 for POWERGRID)

---

## Order Execution Rules

### Margin-Optimized Order Sequence

**For BUY (Call Butterfly):**
```
1. BUY ITM Call (long)     ← Buy longs FIRST
2. BUY OTM Call (long)     ← Buy longs FIRST
3. SELL 2x ATM Call (short) ← Short AFTER longs (margin benefit)
```

**For SELL (Put Butterfly):**
```
1. BUY ITM Put (long)      ← Buy longs FIRST
2. BUY OTM Put (long)      ← Buy longs FIRST
3. SELL 2x ATM Put (short) ← Short AFTER longs (margin benefit)
```

### Rationale
- Buying long options first creates a protective position
- Shorting ATM after longs are in place reduces margin requirement
- Exchange recognizes the spread and applies lower margin

### Order Parameters
- **Order Type:** LIMIT (with slippage tolerance)
- **Product:** NRML (for positional trades)
- **Slippage:** 2 ticks for entry, 3 ticks for exit
- **Timeout:** 30 seconds per order before retry

### Closing Spread Positions (CRITICAL - Margin Rules)

**ALWAYS close SHORT leg FIRST, then LONG leg.**

**For Bull Call Spread (Long lower strike, Short higher strike):**
```
1. BUY back short CE (higher strike)  ← Close short FIRST
2. SELL long CE (lower strike)         ← Close long AFTER
```

**For Bear Put Spread (Long higher strike, Short lower strike):**
```
1. BUY back short PE (lower strike)   ← Close short FIRST
2. SELL long PE (higher strike)        ← Close long AFTER
```

**For Butterfly (Long wings, Short body):**
```
1. BUY back 2x short ATM              ← Close shorts FIRST
2. SELL long ITM                       ← Close longs AFTER
3. SELL long OTM                       ← Close longs AFTER
```

### Rationale for Close Order
- If you sell long leg first, you're left with naked short = HUGE margin spike
- Exchange sees naked short and blocks order or demands full margin
- Closing short first removes the hedge requirement, then long can be sold freely

### Slippage Avoidance
- **ALWAYS check bid-ask depth** before placing orders
- Use **LIMIT orders at bid (for sells) / ask (for buys)**
- Add 1-2 ticks buffer for quick fills if needed
- Verify sufficient quantity at best bid/ask level

---

## Trade Logging

### Log File Location
- **All analyses:** `Helper/logs/analyses.json`
- Logs every analysis run (recommended or not)

### Log Structure
```json
{
  "timestamp": "2025-12-29T13:30:00",
  "underlying": "NIFTY",
  "direction": "BUY",
  "instrument_type": "INDEX",
  "expiry": "2026-01-06",
  "dte": 8,
  "strikes": {
    "itm": 25800,
    "atm": 25950,
    "otm": 26100
  },
  "wing_distance": 150,
  "pricing": {
    "itm_ask": 273.10,
    "atm_bid": 167.00,
    "otm_ask": 88.65,
    "net_debit": 1803.75
  },
  "metrics": {
    "max_profit": 7946.25,
    "max_loss": 1803.75,
    "risk_reward": 0.23,
    "breakeven_lower": 25827.75,
    "breakeven_upper": 26072.25
  },
  "filters": {
    "passed": true,
    "warnings": [],
    "failures": []
  },
  "recommendation": "RECOMMENDED",
  "executed": false,
  "execution_details": null
}
```

### Executed Trade Log
When `--execute` flag is used and trade is placed:
```json
{
  "execution_details": {
    "executed_at": "2025-12-29T13:31:00",
    "orders": [
      {"leg": "ITM", "order_id": "12345", "fill_price": 273.50, "slippage": 0.40},
      {"leg": "OTM", "order_id": "12346", "fill_price": 88.90, "slippage": 0.25},
      {"leg": "ATM", "order_id": "12347", "fill_price": 166.80, "slippage": 0.20}
    ],
    "total_debit_actual": 1850.00,
    "slippage_total": 0.85,
    "status": "COMPLETE"
  }
}
```

---

## Usage Examples

### Step 1: Refresh Data (ALWAYS FIRST)
```bash
cd Helper
python kite_nse_options.py   # Creates CSV files with symbols & lot sizes
```

### Step 2: Analyze Butterflies
```bash
python butterfly_analyzer.py NIFTY BUY                    # Analysis only
python butterfly_analyzer.py RELIANCE SELL --multiplier 1.2
python butterfly_analyzer.py ICICIBANK BUY --lots 2
python butterfly_analyzer.py NIFTY BUY --execute          # With execution
```

### Step 3: Check Positions
```bash
python position_checker.py              # All open positions (uses BID-ASK)
python position_checker.py ICICIBANK    # Specific position
```

---

## Error Handling

### Common Errors and Actions

| Error | Action |
|-------|--------|
| Token file not found | Check `BOTS/data/kite_access_token.json`, run SNAIL auth |
| Token expired | Inform user, suggest re-login |
| No options found | **Run `kite_nse_options.py` first!** Check if stock has F&O |
| CSV stale/missing | **Run `kite_nse_options.py`** - must be Step 1 every morning |
| Strikes not available | Adjust wing distance or try different expiry |
| Spread too wide | Do NOT execute, inform user |
| Low OI/Volume | WARN user, proceed with caution |
| Order rejected | Log error, do NOT retry automatically |

### Kill Switch
- If any order fails during execution, **STOP immediately**
- Do NOT leave partial positions
- Log the failure with full details
- Alert user for manual intervention

---

## Open Positions

### Current Positions (Updated: 2026-01-06)

| Underlying | Direction | Strikes | Expiry | Entry | DTE |
|------------|-----------|---------|--------|-------|-----|
| POWERGRID | BUY (CE) | 252.5/260/267.5 | 2026-01-27 | Rs 5,035 | 21 |

### Closed Positions (2026)

| Underlying | Type | Strikes | Entry | Exit | P&L | Date |
|------------|------|---------|-------|------|-----|------|
| ICICIBANK | Bull Call Spread | 1340/1390 | Rs 15,225 | Rs 27,160 | +Rs 11,935 (+78%) | 2026-01-06 |

### Check Position Status
```bash
cd Helper
python position_checker.py           # All positions
python position_checker.py POWERGRID # Specific position
```

### Position Data Source
- Executed positions extracted from `logs/analyses.json`
- Filter: entries with `"executed": true`

---

## Future Enhancements (Provisioned)

- [ ] Debit Spreads (Bull Call / Bear Put)
- [ ] Iron Condor analysis
- [ ] Position exit analyzer
- [x] P&L tracking for open positions (position_checker.py)
- [x] Telegram alerts for opportunities (scanner.py)

---

## Configuration

Edit thresholds in `butterfly_analyzer.py`:

```python
FILTERS = {
    'bid_ask_spread_max': 1.50,  # Rs per leg
    'oi_atm_min': 5000,          # contracts
    'volume_min': 50000,         # daily volume
    'dte_min_stock': 20,         # days
    'dte_min_index': 6,          # days
}
```
