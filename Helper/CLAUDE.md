# Helper Bot - Claude Instructions

This folder contains helper utilities for options trade analysis and execution.

> ## ⚠ THIS FILE IS TRACKED IN A PUBLIC REPO — NO SECRETS, EVER
>
> `.gitignore` excludes `*.md`; this file is force-added (`git add -f`) so it
> reaches the Pi with an ordinary `git pull`. Before 2026-08-13 it was synced by
> hand, drifted, and a spawned agent read a stale routing rule out of it and
> vetoed a valid signal.
>
> The cost of that convenience: **anything written here is published.** Never
> paste an API key, access token, service-account address, credential path with
> a real home directory, or account number into this file. Name the config file
> that holds the value instead — the code reads it at runtime and never needs it
> inline. (A live Kite `api_key` sat at line ~88 until 2026-08-13; it had never
> been committed, and was removed before this file was first tracked.)

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

**Token file structure** (shape only — the real values live in that file and
are never reproduced here; see the note at the top of this document):
```json
{
  "access_token": "<runtime>",
  "api_key": "<runtime>",
  "user_id": "<runtime>",
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

### MANDATORY: Save to Trade Store After Execution

**Every executed BCS or Fallen Hero trade MUST be saved to its trade store immediately after all legs are filled.**

- **BCS trades:** `bcs.trade_store.add_trade()` → saves to `logs/bcs_trades.json` + Google Drive
- **Fallen Hero trades:** `fallen_hero.trade_store.add_trade()` → saves to `logs/fallen_hero_trades.json` + Google Drive
- Do NOT skip this step. The trade store is the source of truth for monitoring and exit management.
- If Drive sync fails, the trade is still saved locally — never block on Drive.

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

### Current Positions

This table goes stale the moment a trade opens or closes — check the live
source of truth instead of trusting a snapshot:
```bash
python -m zebra status                 # zebra/BCS cohort (paper mode)
python -m bcs.spread_monitor --list    # manually-entered BCS trades
python -c "from fallen_hero import get_store; get_store().list_trades()"  # Fallen Hero
```
As of 2026-08-27: 8 open positions, all zebra-cohort BCS entries from
`cohort='2026-08-14'` (paper mode — no real money); no open manual BCS or
Fallen Hero trades (the INFY Modified Fallen Hero listed as a live position
in earlier versions of this file closed 2026-03-18 — see `logs/fallen_hero_trades.json`).

### Closed Positions (2026)

| Underlying | Type | Strikes | Entry | Exit | P&L | Date |
|------------|------|---------|-------|------|-----|------|
| NHPC | Bull Call Spread | 80/86 CE | Rs 9,800 (1.41/sh) | Rs 2,502 (0.36/sh) | −Rs 7,297 (FALSE SL_SPREAD, bug-impacted) | 2026-07-24 |
| ICICIBANK | Bull Call Spread | 1360/1410 CE | Rs 9,485 (13.55/sh) | Rs 29,330 + Rs 12,285 | +Rs 2,000 (net, bug-impacted) | 2026-02-18 |
| POWERGRID | Call Butterfly | 252.5/260/267.5 | Rs 5,035 | Rs 6,745 | +Rs 1,710 (+34%) | 2026-01-22 |
| ICICIBANK | Bull Call Spread | 1340/1390 | Rs 15,225 | Rs 27,160 | +Rs 11,935 (+78%) | 2026-01-06 |

> **Note on ICICIBANK 1360/1410:** Spread was up +190% but spread_monitor.py bug at market open
> (2026-02-18) placed 4x BUY orders on the short leg, flipping it to long. Manual exit recovered
> ~Rs 2K net instead of ~Rs 16K. Root cause fixed in spread_monitor.py (6 safety layers added).

### Check Position Status
```bash
cd Helper
python position_checker.py           # All positions
python position_checker.py POWERGRID # Specific position
```

### Position Data Source
- **BCS trades:** `bcs/trade_store.py` → `logs/bcs_trades.json` (local) + Google Drive sync
- **Butterfly trades:** `logs/analyses.json` (filter: `"executed": true`)

---

## Bull Call Spread (BCS) Rules

Full playbook: `docs/BCS_PLAYBOOK.md`

> **TWO rule sets, deliberately.** The MANUAL playbook below governs spreads
> you pick by hand off a crash/bounce thesis, where you choose the width. The
> ST-magnet automation in `zebra/` runs a DIFFERENT, measured rule set — its
> width is pinned at ~3.8% of spot by the magnet distance, so the manual gates
> do not transfer. Measured across all 42 automated BCS records: 3/42 pass
> d/w < 30%, 8/42 pass R:R > 1:2, and **1/42 passes every manual gate at
> once.** That is by design, not drift. See "Automated BCS (zebra)" below and
> never judge one rule set by the other's thresholds.

### PRICING — applies to BOTH rule sets (non-negotiable)

**Price every leg at the side you actually trade against.** Buy at the ASK,
sell at the BID, never the LTP and never the mid.

```
Entry debit  = ASK(long)  -  BID(short)     <- what it costs to open
Exit value   = BID(long)  -  ASK(short)     <- what closing actually pays
```

Mid-mid quotes a debit nobody can fill at, and it is optimistic at BOTH ends —
entry understates cost, exit overstates proceeds, so the round trip records
zero spread cost when the real figure is the full bid-ask of both legs. The
automated builder priced at mid-mid until 2026-08-12; every record before that
carries `pricing_basis: 'mid'` and its P&L is optimistic. Basis is stamped on
the trade at entry and **never changes under an open position** — flipping a
live trade would move its stop levels beneath it.

### When to Enter

Find large-cap F&O stock that dropped 5-10% on non-structural event (earnings miss, macro scare), with fundamentals intact. Enter on the recovery bounce.

### Entry Criteria — MANUAL spreads (ALL must pass)

| Rule | Threshold | Hard Fail |
|------|-----------|-----------|
| **Net debit / spread width** | **< 30%** | > 35% = NO TRADE |
| Long strike | ATM (nearest to spot) | |
| Short strike | 3.5-5% above spot | |
| Risk:Reward | > 1:2 | < 1:1.5 = NO TRADE |
| DTE | 20-30 days | < 15 or > 45 = NO TRADE |
| Bid-ask per leg | < Rs 1.50 | > Rs 2.00 = NO TRADE |
| OI at both strikes | > 5,000 | |

**The 30% debit rule is the single most important filter.** The ICICI 1360/1410 trade entered at 27% -> +190%. The earlier 1340/1390 entered at 43% -> only +78%.

Debit here means the FILL debit, ASK(long) - BID(short). A spread that reads
30% at mid and 38% at the touch is a 38% spread.

### Entry Criteria — AUTOMATED BCS (zebra), relaxed 2026-08-10 on evidence

The 25-trade shadow study replaced the manual gates with measured ones. All
are HARD blocks returning `{'error': ...}` so an unclean signal is SUPPRESSED,
never alerted with a warning nobody reads.

| Rule | Threshold | Basis | Why this number |
|------|-----------|-------|-----------------|
| OI both legs | >= 5,000 | — | Was a soft warning. Now hard: on a thin book the stop does not fill where it is set. OI-flagged trades overshot the -50% trigger by -22.0 pts (realised -72.0%) vs -2.7 pts on clean books. COCHINSHIP collapsed 2.18 -> 0.18 in one session while spot moved the RIGHT way; NHPC cost real money the same way. Unknown OI fails CLOSED. |
| debit / width | <= 45% | **MID** | Not the manual 30/35%: the ST-magnet pins width at ~3.8% of spot, so d/w is the market's own probability quote rather than a width you chose. Past ~45% the payoff is priced out — that band ran PF 0.24 while still winning 50% of the time, a payoff problem not a hit-rate one. Gates 1 & 2 together rejected 32% of the sample, and that third ran 37.5% WR / -22.9% RoC / PF 0.27. **Evaluated on the MID basis because that is what it was fitted on** — no historical record persisted its entry books, so it cannot be re-derived on the fill basis. |
| entry cost / max gain | <= 15% | fill vs mid | Added 2026-08-12 with fill pricing. What the book charges just to open. **UNCALIBRATED — reasoned, not fitted.** Replaces the per-leg rupee bid-ask cap (below); denominated in the payoff, which is the same logic the d/w gate uses. Review once ~30 fill-basis records exist. |
| Bid-ask per leg | **REMOVED** | — | Fired on 17 of 25 closed shadows (68%) and carried no signal at all: 58.8% WR flagged vs 62.5% clean. Its only real effect was training the reader to ignore the warning marker, which is how the OI flag on COCHINSHIP got waved through. The measurement still ships as `short_spread_pct`. |
| debit floor | **NONE, ever** | — | Cheap spreads are the high-payoff tail (avg win +127%). Power-law rule: never cap the upside. |
| DTE | first expiry 15-45 | — | Takes the first expiry >= 15 DTE, so entries routinely sit below the manual 20-30 band. **45 (owner decision, 2026-08-27; both sources now agree).** 55 had drifted into the tracked config — snapshotted from the live overlay by the config-split commit `7d1b107`, never chosen. No cohort trade ever entered above 41 DTE, so the two values never differed in effect. |
| Short strike | nearest the ST target | — | Not a % band. At least one strike beyond ATM so the spread always has width. |

`_leg_reliable` (leg width <= 25% of mid) is **not** a tradeability gate — it
is a garbage-print detector shared with the live monitor's valuation path.
Do not treat it as a depth rule; the entry-cost gate is the depth rule.

### Pre-Entry Checklist (MANDATORY — run ALL before any BCS entry)

Codified 2026-07-23 from the NHPC entry / ASHOKLEY + EICHERMOT skips. Claude MUST walk this
checklist and show the results BEFORE presenting a go/no-go. The mechanical gates above are
necessary but NOT sufficient.

**A. Re-quote live at decision time (never trust alert pricing)**
- [ ] Fetch fresh bid/ask for both legs via Kite quote; compute debit at ASK(long) − BID(short)
- [ ] Re-check debit/width against the 30/35% rule on LIVE prices
- WHY: alert pricing decays within hours (ASHOKLEY: 33% at signal → 40.5% next morning = hard fail)

**B. Event-risk research inside the expiry window (delegate to a subagent)**
- [ ] **Quarterly results date** — usually not filed until ~1 week before; estimate from last
      2 years' pattern + SEBI 45-day ceiling. If the print lands inside the window, entry is a
      CONSCIOUS EARNINGS BET — state it explicitly, never a footnote
- [ ] **Dividend ex-dates / record dates** — mechanical spot drop on ex-date works AGAINST a
      call spread. Judge by % of spot (EICHERMOT Rs 82 ≈ 1% = material; NHPC Rs 0.21 = noise)
- [ ] **Structural overhangs** — OFS/QIP/promoter sales/lock-in expiries clearing or looming
      (NHPC: OFS completed June = the fall-to-72 explained + floor validated = de-risk)
- [ ] **Bonus/split/rights** inside the window (strike adjustments, liquidity migration)

**C. Multi-timeframe trend view (closes are signal, intraday is noise)**
- [ ] Monthly ST direction UP (completed closes only — never judge mid-month)
- [ ] Weekly structure supportive (higher lows / double bottom / above weekly ST)
- [ ] Daily above key MAs; entry not chasing an extended candle
- WHY: an intraday level cross can whipsaw same-day (NHPC 80.70 did); when monthly/weekly/daily
  align, trend evidence outweighs pip-level trigger noise. The capped debit is the failed-bounce
  protection — the trigger is not.

**D. Mechanics (existing hard rules — re-verify, don't assume)**
- [ ] Symbols + lot size read from `nse_stocks_options.csv` (never constructed)
- [ ] Quantity is exact multiple of lot size
- [ ] OI ≥ 5,000 both strikes AND bid-ask per leg within limits on the live quote
- [ ] Order-placement IP gate verified if entering from a new machine/network
      (zero-risk probe: `kite.cancel_order(variety='regular', order_id='1')` —
      OMS error = gate open, PermissionException = blocked)

**E. Position plan frozen BEFORE entry**
- [ ] SL spot (~3% below entry spot / below the bounce structure)
- [ ] SL spread (50% of debit)
- [ ] Target (short strike; sanity-check vs weekly ST line)
- [ ] If results inside window: pre-decide hold-through vs exit-before, and note it in the trade

Any single failure in A-D = NO TRADE (or explicit conscious-bet acknowledgment for B).
After fills: save via `bcs.trade_store.add_trade()` immediately — no exceptions.

### Two-Phase Profit Engine

- **Phase 1 (Delta, Week 1-2):** Stock recovers, intrinsic spread widens. This is where most P&L comes from.
- **Phase 2 (Theta, Week 3-4):** Both legs ITM, short leg's TV decays faster (closer to ATM = peak theta). Spread converges to max value passively.

### Execution

```
ENTRY (margin-optimized):
  1. BUY long CE (lower strike)   <- Long FIRST
  2. SELL short CE (higher strike) <- Short AFTER

EXIT (CRITICAL - always close short first):
  1. BUY back short CE  <- Close short FIRST (avoid naked short margin spike)
  2. SELL long CE        <- Close long AFTER
```

### Exit Triggers (3-Layer SL + TP)

Checked every poll cycle in this order:

| # | Trigger | Condition | Action |
|---|---------|-----------|--------|
| 1 | **SL_SPOT** | `spot <= sl_spot` | Thesis dead, close immediately |
| 2 | **SL_SPREAD** | `spread_value <= sl_spread` | 50% loss guard, close |
| 3 | **SL_TRAIL** | Auto-engages at 2x entry debit, trails 60% of peak spread | Lock in gains |
| 4 | **TP** | `spot >= target` | Profit target hit, close |

Additional manual triggers:
- P&L > 70% of max profit → book profits *(judgement call; not automated anywhere)*
- DTE < 5 and spread < 80% max → close, gamma risk

> **A value stop is REQUESTED at its level, not TAKEN at it.** SL_SPREAD and
> SL_TRAIL are priced off the option book, so `needs_exit_vet` flags both — and
> with `spot_sl_enabled: False` those are the cohort's ONLY loss-side exits, so
> every stop this book can take waits on a Claude agent (~1m50s, plus a cycle
> per `defer`). Do not read "50% of debit" as the price it fills at.
>
> **That wait is now BOUNDED** (`exit_vet_max_hold_sec`, default 900s, owner
> decision 2026-08-29). Past it the exit proceeds on the deterministic guards
> alone and says so loudly. The vet is ADDITIVE, never load-bearing — the
> guards had already cleared the exit before it was asked — and an unbounded
> hold inverted that: one `defer` means a later timeout no longer fails open,
> it escalates to a human and waits. ASHOKLEY #390 went −50% → −75% over three
> cycles on an agent that had died on quota two seconds after spawning. The
> budget is PER SESSION, so a Friday hold cannot fire Monday's opening print.
> `0` restores the unbounded behaviour without a code change.
>
> **M12** (`exit_vet_incycle_wait_sec`, default 120s): zebra's cron looks at
> the marker once every 5 minutes, so it now waits in-line for a verdict it
> just requested — ~3 minutes of a measured ~4m50s round trip. Capped per
> CYCLE, not per trade, so several triggering positions cannot run the cron
> past its own interval. `bcs/spread_monitor.py` passes 0: its poll is 5
> seconds, so the same verdict arrives free and blocking would stop watching
> every other position.

**The live monitor (`bcs/spread_monitor.py`) keeps rows 1-4 exactly as above.**
The automated paper system uses a DIFFERENT trail — see below. Do not port one
into the other until the paper scorecard earns it.

### Automated exits (zebra) — differences that are deliberate

| Trigger | zebra (paper) | live monitor | Note |
|---------|---------------|--------------|------|
| TRAIL | arms when the PEAK reaches 50% of max gain (`width - debit`), then exits at `debit + 50% of peak gain` | arms at 2x entry debit, trails 60% of peak | The two cross at d/w = 1/3, and **34 of 42 records sit above 1/3** — so the gain-anchored rule arms EARLIER on ~80% of this book. 2x debit means 43% of max gain at 30% d/w but 82% at 45%; anchoring to max gain keeps the trigger meaning the same thing across spreads. |
| TIME | `TIME_SL_DAYS` trading SESSIONS before expiry, unconditional | warns daily from E-5, force-closes on expiry day | Sessions, not calendar days: for an Aug-25 expiry the old calendar count's first weekday firing was ONE session out, because its earlier hits landed on a weekend when cron does not run. |
| Delivery margin | — | daily warning from E-5 with sessions left + ITM legs | Stock options are PHYSICALLY settled and the exchange ramps a delivery margin over the last ~4 sessions. Alert-only. **Confirm with the broker:** exact ramp schedule, and whether both BCS legs net for delivery. |

**M10, completed 2026-08-29.** Two rules were designed with the 6-session close
and only now built:

- **Moneyness may only ACCELERATE the close, never delay it.** The stored
  `time_stop_sessions` is a FLOOR. When the LONG leg is ITM — the leg the
  margin is actually levied on, at its STRIKE, full contract value — the close
  comes forward one session (`delivery_stop_sessions`). The intuition runs
  backwards, so the invariant is a `max()` rather than a convention: a far-OTM
  spread is worth pennies at E-6 and risks nothing, while the deep-ITM one
  converging on max value carries the whole exposure. Unknown moneyness leaves
  the schedule alone.
- **`delivery_preflight`, from E-9.** A SEPARATE gate with exactly two members,
  `CLOSE_NOW | CLOSE_ON_SCHEDULE`, monotonic, alert-only. **Do NOT extend the
  exit vet to cover it** — the vet's whole safety argument is that holding is
  bounded, and past the delivery deadline that premise INVERTS (a long ITM put
  is a give-delivery obligation auctioned at E+3 with a 20% floor and no
  ceiling). A gate whose safety argument has inverted must not have a state
  meaning "wait", so this one has no DEFER and no HOLD, and
  `EXPIRY_FORCE_CLOSE` stays out of `VET_KIND`. CLOSE_NOW on a long ITM put
  unconditionally, or a long ITM call with ≥90% of max value already captured.

**The holiday calendar is `BOTS/data/holiday_calendar.json`, scraped daily
from Zerodha by `SNAIL/src/utils/holiday_scraper.py`.** `common/nse_holidays.py`
reads it at call time, cached on the file's mtime, so a refresh lands without
restarting either engine. It is NOT in git — a `git pull` does not deliver it;
SNAIL's daily startup has to produce it on the box.

> ⚠ **The static list this replaced was mostly WRONG.** It shipped 2026-08-29
> claiming three independent publications of the NSE calendar agreed on it.
> Checked against 160 FIFTY daemon logs — a real holiday leaves a ~20-35 line
> log (daemon started, found the market closed, exited) against ~5,700 on a
> trading day — it scored **2 of 6** evidenced holidays and listed **6 dates
> that were full trading days** (2026-03-03, 03-26, 03-31, 04-14, 05-01,
> 05-28, 06-26). The three it MISSED (2026-02-19, 03-19, 04-01) are the half
> that costs money: a missing holiday makes the session count over-estimate,
> so the delivery close fires LATER, into the ramp.

`coverage_status()` reports `ok / expiring / expired / stale / missing /
unreadable`. zebra checks it every cycle: **daily** Telegram for anything that
means the calendar is not working now (missing, unreadable, stale, expired),
weekly for `expiring`, which is only a diary note about December. Every bad
state degrades the count to weekday-only and says which way the error points.

**The file is taken AS-IS** (owner decision, 2026-08-30) — no hand-patching and
no reconciliation against observed sessions. Two divergences are therefore live
and deliberate, both pinned in
`test_the_known_imperfections_are_still_the_known_ones`: 2026-06-23 is an
evidenced closure the file omits (count over-estimates, close fires later), and
2026-08-26 is listed while `logs/cron_zebra_20260826.log` shows a full 347-poll
session (count under-estimates, close fires earlier).

**A fired TRAIL is not proof of a profit.** The trigger is `mid <= level` and
the booking price is `mid`; a gap straight through the level books wherever it
landed, possibly below the entry debit. That is intended — a breached trail
means get out — but never score a `trail` exit as a win off the reason string.
`outcomes.label_for_reason` takes the realised P&L for exactly this reason.

### Valuation bounds — clamp the arithmetic, refuse the estimate

Two different things get two different treatments, and conflating them is how
optimism creeps back in.

| Bound | Kind | Treatment |
|-------|------|-----------|
| value `< 0` | mathematical — expiry is always available and costs nothing | **CLAMP to 0.** It is a real, realisable value. Long bid 0.55 / short ask 0.60 is an ORDINARY book for a worthless spread, so book the loss rather than stranding the position. |
| value `> width` | mathematical — a vertical's ceiling | **CLAMP to width.** |
| value `< intrinsic floor` | heuristic — an ESTIMATE of fair value | **REFUSE the quote.** Clamping to it invents a fill exactly the way the garbage book did. Defer to the next poll. |

PIIND #50 booked `exit_debit -30.04` on a debit of 242.11 — **-112.4% on a
-100%-capped structure** — because no bound existed anywhere. The floor is
also never allowed below zero: `intr - 1.5*allowance` goes negative once the
spread is OTM, which made the guard inert in precisely the loss region it
exists for.

**Paper never books a price it could not have transacted at.** No quote, an
unreliable book, or a refused quote all defer. The deferred close RELEASES its
consume-once flag so the exit can fire again — otherwise the exit is announced
on Telegram, never booked, and that exit kind is disarmed for good. TIME is the
exception: its alert is a nag about the calendar, not a claim about a price.

### Spot-based stops — VETO, never TRIGGER (measured 2026-08-12)

Do not re-argue this from intuition. Over 147 records with candle coverage
(zebra and BCS share the signal, the stock and the entry timing, and MAE is a
property of the SPOT path, not the structure):

| spot stop | winners cut | given up | losers caught |
|-----------|-------------|----------|---------------|
| 2.0% | 57/78 (73%) | Rs 16.4L | 96% |
| **3.0%** | **31/78 (40%)** | **Rs 8.9L** | 91% |
| 4.0% | 20/78 (26%) | Rs 5.3L | 67% |
| 5.0% | 9/78 (12%) | Rs 2.7L | 46% |

Eventual winners take a **median 2.74% adverse excursion** before working, so
the stored 3% `sl_spot` sits ON the winners' median. The book's biggest winner
(IDFCFIRSTB +155.4%) has an MAE of 4.43% and dies at 3% or 4%. "Caught" is not
"saved" — the 19 real `spot_sl` exits booked a median -25.1% against debit-SL's
-51.6%, i.e. roughly half the depth — which makes 3% a **wash** and 5% mildly
positive. Reaping winners to shave losers is the power-law rule inverted.

The mechanism: the scanner enters on a **pullback TOWARD the ST line**, so
adverse movement is the thesis, not its failure. A tight spot stop fights the
strategy's own premise.

**What spot IS for.** The loss side had four checks (reliability gate,
intrinsic floor, debounce, blind alert) reading ONE source — the option book.
Count sources, not checks. Spot is the independent one:

- `_spot_corroborates` (ported from `bcs/spread_monitor.py:369`) vetoes one
  shape: value collapsing ≥35% while spot moves <0.4%. That is the NHPC
  signature and no real repricing of a vertical produces it. **Veto-only** — it
  can refuse an exit the book asked for, never ask for one. The reference is
  PERSISTED, unlike the live monitor's in-memory one, because zebra's cron
  process exits between cycles.
- **Value triggers are dark for 15 min after the open.** Both incidents that
  cost real money (ICICI Feb, NHPC Jul) were at the open on the first prints.
  Spot TP and the expiry nag are deliberately NOT gated.
- `sl_spot` stays a REPORTED number: during a blind spell the alert carries
  where spot sits against entry. It is not a trigger.

### Evidence, because paper mode is a forensic exercise

- **The exit book is persisted** (`exit_legs`), not just `exit_debit`. Entry
  books have been stored since fill pricing; exits kept only scalars, so the
  one direction that has twice cost real money was the one with no evidence.
  An option book cannot be reconstructed after the fact.
- **One POLL line per open position per cycle**, unconditionally, with both
  legs' book. `check_entered` used to speak only when a trigger fired, so
  "what did it see at 14:35" and "why did TP not fire" had no answer.
- **Timestamps carry the date.** The cron redirect is date-stamped per day
  (`cron_zebra_$(date +\%Y\%m\%d).log`); cron owns the fd, so Python cannot
  rotate it.
- **A Telegram non-200 is logged** with Telegram's own reason and the message —
  previously it returned False silently, so an HTML-escape 400 vanished
  without trace.
- **Safety interlocks do not depend on optional subsystems.** The corp-action
  calendar's only writer used to live inside the vet side-channels, which ship
  disabled — so the guard was wired in, looked deployed, and could never fire.
- **A dead Kite token now alerts.** `get_ltp` guards `kite.ltp()` but not the
  instrument-cache load beneath it, so an expired token raised, killed
  `check_entered`, and stopped exit monitoring on every open position with no
  Telegram at all.

### Trade Store (`bcs/` package + Google Drive sync)

BCS trades live in `logs/bcs_trades.json` (local) and sync to Google Drive for server access.

**Architecture:**
```
bcs/
  __init__.py          — Package exports
  drive_store.py       — Google Drive API wrapper (upload/download JSON)
  trade_store.py       — TradeStore class: CRUD + Drive sync + lot_size validation
  spread_monitor.py    — Monitor + executor + CLI (moved from helper/)
config/
  bcs_config.json      — Drive folder ID, credentials paths
```

**Sync strategy:**
- Startup: download from Drive → set as cache → save to local
- Reads (every 5s poll): from in-memory cache, zero network
- Writes (add/close trade): local FIRST, then Drive upload
- Periodic re-sync: every 5 minutes from Drive (configurable)
- Drive failure: system continues in local-only mode, never blocks trading

**Trade schema (new fields):**
```json
{
  "id": 1,
  "version": 1,       // incremented on every write
  "lot_size": 700,     // shares per lot (validated on add)
  "lots": 1,           // quantity / lot_size (auto-computed)
  ...
}
```

**Google Drive setup (one-time):**
- Folder ID: `config/bcs_config.json` → `drive_folder_id` (not repeated here)
- Shared with the service account named in that same file, as Editor
- Credentials: `config/bcs_config.json` → `credentials_path_windows` / `credentials_path_linux`
- Env var override: `BCS_GOOGLE_CREDS=/path/to/secret.json`

### Trade Capture Rulebook (Claude Instructions)

**When user says they entered a BCS trade, Claude MUST:**

1. **Parse natural language input.** User may say things like:
   - "Entered ICICIBANK 1360/1410 CE Feb, 700 qty, long at 21.20, short at 7.65"
   - "Bought SBIN 800/850 call spread, 1500 qty, debit 12.30, expiry March"
   - "BCS on RELIANCE 2900/3000 CE, filled long 45.50 short 18.20"

2. **Derive ALL required fields:**

| Field | Derivation Rule |
|-------|----------------|
| `stock` | Extract from user input (e.g., "ICICIBANK") |
| `long_symbol` | Read from `nse_stocks_options.csv` — NEVER construct manually |
| `short_symbol` | Read from `nse_stocks_options.csv` — NEVER construct manually |
| `spot_symbol` | `"NSE:{stock}"` |
| `exchange` | `"NFO"` (always for stock options) |
| `quantity` | From user input (must be multiple of lot size) |
| `lot_size` | Read from `nse_stocks_options.csv` (column: `option_lot_size`) — NEVER hardcode |
| `entry_long_price` | From user input (fill price of long CE) |
| `entry_short_price` | From user input (fill price of short CE) |
| `net_debit` | `entry_long_price - entry_short_price` |
| `spread_width` | `short_strike - long_strike` |
| `target_spot` | `short_strike + 25pts` for stocks, or user-specified |
| `sl_spot` | `entry_spot - 3%` (round to nearest integer) |
| `sl_spread` | `net_debit * 0.50` (50% loss guard) |
| `entry_date` | Today's date |
| `entry_spot` | Fetch current spot via Kite LTP, or user-specified |
| `expiry` | From user input, format `YYYY-MM-DD` |
| `notes` | From user context (why this trade was entered) |

3. **Validate before saving:**
   - Confirm `net_debit / spread_width < 0.35` (35% hard fail)
   - Confirm quantity is a multiple of lot size
   - Confirm both symbols exist in Kite (LTP check)

4. **Save using `bcs.trade_store.add_trade()`:**
```python
# Run in Helper directory
from bcs.trade_store import get_store

store = get_store()  # Initializes Drive sync on first call
trade = store.add_trade({
    "stock": "ICICIBANK",
    "long_symbol": "ICICIBANK26FEB1360CE",   # FROM CSV, never manual
    "short_symbol": "ICICIBANK26FEB1410CE",  # FROM CSV, never manual
    "spot_symbol": "NSE:ICICIBANK",
    "exchange": "NFO",
    "quantity": 700,
    "lot_size": 700,          # FROM CSV
    # "lots" auto-computed:   quantity // lot_size
    "entry_long_price": 21.20,
    "entry_short_price": 7.65,
    "net_debit": 13.55,
    "spread_width": 50,
    "target_spot": 1435.0,
    "sl_spot": 1319.0,
    "sl_spread": 6.78,
    "entry_date": "2026-01-06",
    "entry_spot": 1360.0,
    "expiry": "2026-02-26",
    "notes": "reason for trade",
})
# add_trade() validates lot_size, assigns ID/version, saves local + Drive
print(f"Trade #{trade['id']} saved")
```

5. **Confirm to user:** Print trade summary with ID, all SL levels, target.

### Monitor Script

```bash
# Preferred: run as package (auto-syncs from Drive on startup)
python -m bcs.spread_monitor --list
python -m bcs.spread_monitor ICICIBANK --dry-run
python -m bcs.spread_monitor --cron

# Backward-compat wrapper (delegates to bcs.spread_monitor.main())
python helper/spread_monitor.py --list
python helper/spread_monitor.py ICICIBANK

# Override TP/SL from CLI (takes precedence over trade store values)
python -m bcs.spread_monitor ICICIBANK --target 1440
python -m bcs.spread_monitor ICICIBANK --sl-spot 1380 --sl-spread 8.0

# Specific trade ID (if multiple open trades for same stock)
python -m bcs.spread_monitor ICICIBANK --trade-id 2

# Auto-monitor ALL open trades (cron mode - run as scheduled task)
python -m bcs.spread_monitor --cron
```

---

## Fallen Hero (Reverse Jade Lizard) Rules

### Strategy Overview
3-leg credit strategy for beaten-down stocks:
- **BUY OTM Put** (long put, protective floor)
- **SELL ATM/NTM Put** (short put, credit)
- **SELL OTM Call** (naked short, credit)

**Key property:** When `total_credit >= put_spread_width`, downside risk = ZERO. All risk is on the upside (stock rallying past short call + total credit = breakeven).

### TIMING IS EVERYTHING — Enter Immediately After Crash

**The entire edge of Fallen Hero comes from elevated IV after a panic selloff.**

- **Enter same day or next day of crash** — don't wait for "confirmation"
- Every day you wait, IV crushes → premiums shrink → worse credit → worse risk:reward
- Day 1 post-crash: IV at peak → maximum credit → zero downside risk achievable
- Day 3-5: IV normalizing → credit shrinks → may not cover put spread width
- Day 7+: Opportunity mostly gone

**Trigger criteria:**
- Large-cap F&O stock crashed **10%+ on non-structural event** (AI fears, tariff noise, macro scare, earnings miss)
- Fundamentals intact (profitable company, not fraud/bankruptcy)
- Option premiums feel "expensive" → that's elevated IV working for you

### Modified Fallen Hero (4-leg, Hedged Variant)
Standard 3-leg FH has unlimited upside risk. Add a **long far-OTM call** to cap it:
```
1. BUY OTM Put          <- Protective floor
2. SELL ATM/NTM Put     <- Credit
3. SELL OTM Call         <- Credit (creates risk)
4. BUY far-OTM Call      <- Caps upside risk
```
- Pick cheapest hedge with best liquidity (high OI, tight bid-ask)
- Tradeoff: less net credit but max loss is capped on both sides
- Target 30-60 DTE for good theta decay

### Entry Execution Order (Margin-Optimized)
```
ENTRY:
  1. BUY Long Put (OTM)        <- Protective leg FIRST
  2. SELL Short Put (ATM/NTM)   <- Creates bull put spread with #1
  3. SELL Short Call (OTM)       <- Naked short LAST

EXIT (CRITICAL - close naked first):
  1. BUY back Short Call         <- Naked leg FIRST (highest margin)
  2. BUY back Short Put          <- SECOND
  3. SELL Long Put               <- Protective leg LAST
```

### SL Logic
- **Risk direction is UPSIDE** (opposite of BCS)
- SL triggers when `spot >= sl_spot`
- SL level should be well below breakeven to exit before losses accumulate
- Breakeven = `short_call_strike + total_credit`

### Trade Store (`fallen_hero/` package + Google Drive sync)

**Architecture (mirrors bcs/):**
```
fallen_hero/
  __init__.py          — Package exports (FallenHeroStore, get_store, etc.)
  trade_store.py       — FallenHeroStore: CRUD + Drive sync + 5 cross-field validations
config/
  fallen_hero_config.json — Drive folder ID, credentials paths
```

**Reuses:** `bcs.drive_store` for all Google Drive operations (no duplication).

**Trade schema:**
```json
{
  "id": 1, "version": 1, "status": "open",
  "stock": "WAREEENER",
  "long_put_symbol": "WAREEENER26MAR2550PE",
  "short_put_symbol": "WAREEENER26MAR2600PE",
  "short_call_symbol": "WAREEENER26MAR3000CE",
  "spot_symbol": "NSE:WAREEENER", "exchange": "NFO",
  "quantity": 400, "lot_size": 400, "lots": 1,
  "entry_long_put_price": 122.85, "entry_short_put_price": 142.00,
  "entry_short_call_price": 78.60,
  "long_put_strike": 2550, "short_put_strike": 2600, "short_call_strike": 3000,
  "put_spread_width": 50, "put_spread_credit": 19.15,
  "call_credit": 78.60, "total_credit": 97.75,
  "breakeven": 3097.75, "downside_risk": 0,
  "sl_spot": 2850.0,
  "entry_date": "2026-02-25", "entry_spot": 2692.50, "expiry": "2026-03-30",
  "notes": "Post-crash FH, IV elevated",
  "exit": null
}
```

### Trade Capture Rulebook (Claude Instructions)

**When user says they entered a Fallen Hero trade, Claude MUST:**

1. **Parse natural language input.** User may say things like:
   - "Entered FH on WAREEENER: long 2550 PE at 122.85, short 2600 PE at 142, short 3000 CE at 78.60, 400 qty, March expiry"
   - "Fallen Hero SBIN: bought 700 PE, sold 750 PE + sold 900 CE, fill prices 45/55/30"
   - "Reverse jade lizard on RELIANCE 2800/2850 PE + 3200 CE"

2. **Derive ALL required fields:**

| Field | Derivation Rule |
|-------|----------------|
| `stock` | Extract from user input |
| `long_put_symbol` | Read from `nse_stocks_options.csv` — NEVER construct manually |
| `short_put_symbol` | Read from `nse_stocks_options.csv` — NEVER construct manually |
| `short_call_symbol` | Read from `nse_stocks_options.csv` — NEVER construct manually |
| `spot_symbol` | `"NSE:{stock}"` |
| `exchange` | `"NFO"` (always for stock options) |
| `quantity` | From user input (must be multiple of lot size) |
| `lot_size` | Read from `nse_stocks_options.csv` (column: `option_lot_size`) — NEVER hardcode |
| `entry_long_put_price` | From user input (fill price of long PE) |
| `entry_short_put_price` | From user input (fill price of short PE) |
| `entry_short_call_price` | From user input (fill price of short CE) |
| `long_put_strike` | From user input |
| `short_put_strike` | From user input |
| `short_call_strike` | From user input |
| `put_spread_width` | `short_put_strike - long_put_strike` |
| `put_spread_credit` | `entry_short_put_price - entry_long_put_price` |
| `call_credit` | `entry_short_call_price` |
| `total_credit` | `put_spread_credit + call_credit` |
| `breakeven` | `short_call_strike + total_credit` |
| `sl_spot` | User-specified, or default: midpoint between short_call and breakeven |
| `entry_date` | Today's date |
| `entry_spot` | Fetch current spot via Kite LTP, or user-specified |
| `expiry` | From user input, format `YYYY-MM-DD` |
| `notes` | From user context (why this trade was entered) |

3. **Validate before saving:**
   - `add_trade()` auto-validates 5 cross-field checks (credits, breakeven, spread width, strikes)
   - Confirm quantity is a multiple of lot size
   - Confirm all 3 symbols exist in CSV
   - Warning if `downside_risk > 0` (total_credit < put_spread_width)

4. **Save using `fallen_hero.trade_store.add_trade()`:**
```python
from fallen_hero.trade_store import get_store

store = get_store()  # Initializes Drive sync on first call
trade = store.add_trade({
    "stock": "WAREEENER",
    "long_put_symbol": "WAREEENER26MAR2550PE",   # FROM CSV
    "short_put_symbol": "WAREEENER26MAR2600PE",  # FROM CSV
    "short_call_symbol": "WAREEENER26MAR3000CE",  # FROM CSV
    "spot_symbol": "NSE:WAREEENER",
    "exchange": "NFO",
    "quantity": 400,
    "lot_size": 400,          # FROM CSV
    "entry_long_put_price": 122.85,
    "entry_short_put_price": 142.00,
    "entry_short_call_price": 78.60,
    "long_put_strike": 2550,
    "short_put_strike": 2600,
    "short_call_strike": 3000,
    "put_spread_width": 50,
    "put_spread_credit": 19.15,
    "call_credit": 78.60,
    "total_credit": 97.75,
    "breakeven": 3097.75,
    "sl_spot": 2850.0,
    "entry_date": "2026-02-25",
    "entry_spot": 2692.50,
    "expiry": "2026-03-30",
    "notes": "Post-crash FH, IV elevated",
})
# add_trade() validates all fields, assigns ID/version, saves local + Drive
print(f"Trade #{trade['id']} saved, downside_risk={trade['downside_risk']}")
```

5. **Confirm to user:** Print trade summary with ID, credit received, breakeven, SL, downside risk.

### CLI Usage
```bash
# List all Fallen Hero trades
python -c "from fallen_hero import get_store; get_store().list_trades()"
```

---

## Zebra Strategy (Synthetic Long/Short via Back Ratio)

> ⚠ **STRUCTURE DROPPED.** The back-ratio structure described below (2× long
> ITM + 1× short ATM) no longer trades. The `zebra/` package is still live and
> still runs the ST-magnet scan/trigger pipeline, but every signal it enters
> now builds a **Bull Call Spread** instead (`zebra/strikes.py analyze_bcs`,
> `zebra/monitor.py _enter_as_bcs` / `mark_entered_bcs`) — records carry
> `structure: 'bcs'`. See "What zebra runs now" immediately below. The
> back-ratio description further down is kept only because trade records
> entered before the switch still use that structure.

### What zebra runs now (BCS cohort)

BUY 1× ATM + SELL 1× the strike nearest the ST target (forced at least one
strike beyond ATM) — a Bull Call Spread on the CE side, a Bear Put Spread on
the PE side — gated by the "Entry Criteria — AUTOMATED BCS (zebra)" table in
the Bull Call Spread section above. Three independent switches, all in
`config/zebra_config.defaults.json` (an untracked `config/zebra_config.json`
overlay can override):

| Switch | Config key | Currently | Controls |
|---|---|---|---|
| Paper simulation | `paper_mode` | **true** | On: zebra auto-enters a triggered signal as a paper BCS position and auto-closes it itself on TP/trail/spot_sl/debit_sl/time — no manual step. Off: the Telegram alert IS the order ticket; entry is manual unless `auto_entry` is armed. |
| Exit bridge | `exits_managed_externally` | **false** | On: `bcs/spread_monitor.py` (via `bcs/zebra_adapter.py`) takes over exit management for the cohort using the BCS engine's own vetted exits, instead of zebra's own paper auto-close. Off today. |
| Automated entry | `auto_entry` | **false** | Gates `bcs/entry_executor.py`, the automated LIVE order-placement path. Off today — no real order is placed without a human. |

**Current state: paper mode only, nothing armed** — cohort positions
(`logs/zebra_trades.json`, `cohort='2026-08-14'`) are simulated, not real
money. `python -m zebra status` shows the live count.

### ⚠ ARMING — the switch states that are silently dangerous

**Verified 2026-08-27 by an audit of the running code. The arming order that
stood in `docs/GO_LIVE_DOSSIER.md` before that date was found UNSAFE. That file
is NOT tracked in git and therefore does NOT reach the Pi — this section is the
copy that does. If the two ever disagree, re-verify against the code, not
against either document.**

There are FOUR switches, not three: `paper_mode` gates whether auto-entry means
anything (`zebra/monitor.py` only consults it under `if not cfg.PAPER_MODE:`),
and `--dry-run` on the `bcs.spread_monitor` crontab line is a fourth that lives
in no config file at all.

> **THE TABLE THAT USED TO BE HERE IS NOW CODE: `common/arming.py`.**
> It went stale exactly as you would expect a document to — its two-engine row
> was narrowed by the C5 record-level paper gate and nobody moved it — and
> neither engine could consult it. Both engines now call `arming.check()` at
> startup (zebra every cycle, the monitor once a session and again the moment
> the kill switch trips) and print the verdict. **Read that line, not this
> section.** What follows is why it exists, not what it decides.

The whole table is derived from one invariant:

> **Every cohort record must have EXACTLY ONE engine that can book its exit.**

Two predicates settle it, and `paper_mode` is in neither:

- **zebra books at MID**, which only a record whose legs never reached a broker
  could have transacted at. So it books a record iff `paper: True` and it has
  not stood down (`exits_managed_externally` AND cohort AND not paper).
- **the monitor places real orders**, so it refuses a `paper: True` record
  outright and books nothing at all under `--dry-run`.

From which: a **paper record always has exactly one engine, whatever the
switches** — the two predicates are opposite readings of one fact. Every
illegal state is therefore about a LIVE record, and there is only one:
**`--dry-run` is on and a live cohort record exists → NO ENGINE.** The single
fix is taking `--dry-run` off the crontab line;
`exits_managed_externally=false` is NOT an alternative, because zebra cannot
book a position with real legs at any price it knows.

That state is reported as **latent** while no live cohort record is open (today:
eight paper positions, so it is latent) and as a **fault** with a red Telegram
from both engines the moment one appears. `zebra enter` filing a hand-placed
live trade is exactly how one appears.

The two-engine state is now **unreachable**, and that is a code change rather
than a re-reading. `_paper_auto_close` gated on
`cfg.PAPER_MODE or is_paper_record(trade)` until 2026-08-29, so a `paper: False`
record in a `paper_mode: true` store — a hand-placed live trade, the FIRST
live-money action in the arming order — was bookable at mid by zebra and at the
broker by the monitor. The mode switch was never the cause; the `or` was.

Two more traps, both verified:

- **The stand-down is one-sided.** `exits_managed_externally` is read only by
  `zebra/monitor.py`; the string appears nowhere in `bcs/spread_monitor.py`.
  zebra stops managing exits on its own config, without ever checking that the
  other engine is alive. The heartbeat (`exit_engine_heartbeat.json`) is what
  closes that: it records whether the peer can BOOK, not whether it breathes.
- **The kill switch creates the no-engine state.** Tripping it forces the
  monitor to dry-run for the session; if `exits_managed_externally` is true,
  zebra has already stood down, so nothing books. Alerts continue, which is
  what makes it look healthy. The monitor now re-runs the arming preflight on
  that transition, so the state is announced instead of inferred.

**Arm exits and entries in this order, never in one step:**

1. Fix the store-bridge write path first — `ZebraStore.mark_exited`'s status
   precondition and the `exit_value` / `exit_spread` key mismatch in
   `bcs/zebra_adapter.py` — with a regression test that drives the REAL
   `ZebraStore`, not the test `MemoryStore`. **Nothing below is safe until
   this is done.** ✅ **DONE 2026-08-27** — a cohort TP now books correctly
   (+195% on the test case) instead of raising or booking −100%; 81 tests,
   including one that drives the REAL `ZebraStore` rather than the test fake.
2. Fix `bcs/journal_report.py` cohort visibility, THEN run the dry-run
   evidence week — the compare tool must name cohort trades, or it is
   reporting the book under test as missing. ✅ **DONE 2026-08-27** — it now
   names cohort rows and flags id ambiguity instead of guessing (all four
   books number from 1).
3. Put `vet_enabled` in the tracked defaults. It lives only in the Pi's
   untracked overlay today, so a routine overlay rebuild silently disarms
   vetting and the exit gate returns `proceed` unconditionally.
   ✅ **DONE 2026-08-27** — in the tracked defaults, and the code default is
   now `True` too so both sources agree. Safe because entry vetting fails
   CLOSED: no verdict, no entry. The Pi's overlay still wins, so its effective
   value is unchanged.

   > **⚠ Before ANY of this: the arming gate itself could be cleared by a
   > TAKE-PROFIT.** `ALREADY_FLAT_TP` lowercases to `already_flat_tp`, and
   > `zebra/digest.py` counted anything `!= 'tp'` as a stop exit. The
   > already-flat branch fires when the monitor finds no legs at the broker —
   > i.e. exactly the arm-against-paper-positions mistake step 4 exists to
   > prevent — so that mistake would have manufactured the evidence
   > authorising the next step. Being fixed 2026-08-27; the rule is an
   > ALLOWLIST of known stop reasons, because an unrecognised reason must
   > leave the gate UNMET.
4. Let the open paper positions close on their own. Never point the live order
   path at a record that has no legs at the broker.
5. Arm exits in ONE market-closed window: confirm the monitor runs and lists
   the cohort, set `exits_managed_externally: true`, remove `--dry-run`. Next
   morning verify BOTH zebra's "EXITS EXTERNAL" line AND the monitor's cohort
   banner showing the same count — the two lines cross-confirm the handoff.
6. Only after a live exit has correctly booked a real STOP: `paper_mode: false`
   (it changes alert semantics for the WHOLE store), then separately and later
   `auto_entry: true` — and not before the entry path has had a review by
   someone other than its author.
7. `compound: true` last.

### Back-ratio structure (HISTORICAL — dropped, kept for reading old records)

Bullish (CE-Zebra) and bearish (PE-Zebra) synthetic positions:
- **CE-Zebra:** BUY 2× ITM CE + SELL 1× ATM CE (same expiry)
- **PE-Zebra:** BUY 2× ITM PE + SELL 1× ATM PE (same expiry)

Max loss = debit (mathematically capped). No spot-based SL strictly required, but a spot SL alert is fired to let the user salvage premium before the debit floor.

**Full mechanics:** `Helper/zebra/PLAYBOOK.md`. **Server setup:** `Helper/zebra/SERVER_SETUP.md`.

### Pipeline

```
Chartink scan (monthly + weekly, ±8% of ST)
   ↓ filter: trend alignment + gap ≤ 5% + freshness + price > Rs 100
add to zebra watchlist (silent — no Telegram)
   ↓ LTP enters trigger zone (gap ≤ 4%, ≥ 3%)
strike analyzer builds a BCS shadow (analyze_bcs) from the live option chain
   ↓ ATM long leg + nearest-to-target short leg beyond ATM
Telegram ENTER alert carries the BCS shadow (debit, BE, OI, lots) — the
classic back-ratio (K_L/K_S) alert is silenced by default (`alert_structures: ['bcs']`)
   ↓ paper_mode=true (current default): auto-entered here, no manual step
      paper_mode=false: the alert IS the order ticket; manual, or auto_entry
monitor every 5 min: TP / DEBIT SL / TIME (SPOT SL disabled) — zebra auto-closes its
   own paper positions unless exits_managed_externally hands the cohort to
   bcs/spread_monitor.py
zebra close ID --exit-debit X --reason ...   (manual override / LIVE mode)
```

### Direction routing

**Price vs the ST line decides direction. ST DIRECTION DECIDES NOTHING.**

| Spot vs ST | Play | Logic |
|---|---|---|
| price < ST | **CE** | expect a rally UP to the ST line |
| price > ST | **PE** | expect a drop DOWN to the ST line |
| price == ST | SKIP | nothing to travel to (rare) |

The magnet IS the thesis: whichever side of the ST line price sits on, it tends
to get pulled back to it. Source of truth is `zebra/scanner.py _direction_for`.

> **Never SKIP on trend.** An earlier version of this table had a third row —
> *"any other → SKIP, trend not aligned"* — describing a filter the code has
> never implemented. On 2026-08-13 a spawned vetting agent read that row, quoted
> *"routing table says SKIP"* back as a veto reason, and killed a valid signal.
>
> The arithmetic is why it matters: on the SAME timeframe, `price < ST` and
> `ST DOWN` are the same fact, so `trend_aligned` is false on **381 of 383**
> records ever generated. Vetoing on it vetoes 99.5% of the strategy. And the
> only 2 "aligned" trades in the entire book averaged **−24.6%**, while the 205
> closed counter-trend trades ran 53.7% wins, median **+13.1%**.
>
> `trend_aligned` is a CONVICTION TAG (the ⭐ALIGNED badge), never a filter —
> see `cfg.is_trend_aligned`, whose docstring says exactly this.

### Strike selection rules — back-ratio criteria (HISTORICAL)

K_S/K_L/NetExt describe the dropped back-ratio structure; the live BCS entry
criteria are the "Entry Criteria — AUTOMATED BCS (zebra)" table in the Bull
Call Spread section above. The expiry window is shared infrastructure and
still applies to BCS entries.

- K_S = strike closest to spot (ATM)
- K_L = 5-10% ITM (deep enough that `2·long_extrinsic ≤ short_extrinsic`)
- Both legs OI ≥ 5,000
- Bid-ask spread per leg < 1% of mid (warning if exceeded, not block)
- DTE 15-45 (auto-picks first expiry ≥ 15 DTE — see the `max_dte` note above)
- BE within ±0.5% of spot (warning beyond 1%)

### Exit triggers

| Trigger | Condition | When |
|---|---|---|
| TP | spot reaches ST line (or the swing TP, if nearer) | hit max-profit zone |
| DEBIT SL | structure value drops to 50% of entry debit | half the debit gone |
| TIME | T-5 sessions from expiry (`time_sl_days_before_expiry`) | pin risk on short ATM |
| EXPIRY | expiry day | last resort |
| ~~SPOT SL~~ | **DISABLED** (`spot_sl_enabled: False`) | see below |

In paper mode (current default) zebra auto-closes on these triggers itself —
see "What zebra runs now" above. They are alert-only / user-closes-manually
only in LIVE mode, which is not armed today.

> **SPOT SL is OFF and has been since the 2026-08-12 measurement.** The switch
> is `spot_sl_enabled` (`zebra/config.py`, default `False`); the branch at
> `zebra/monitor.py` is guarded by `cfg.SPOT_SL_ENABLED` and cannot fire. Spot
> is a **VETO, never a trigger** — see "Spot-based stops" in the Bull Call
> Spread section for the 147-record table behind that. `sl_spot` is still
> STORED and still PRINTED on every POLL line and alert, so a position sitting
> below its `sl_spot` and staying open is CORRECT, not a stuck exit.
>
> This matters to the arming gate: a disabled kind can never produce the cohort
> stop evidence the gate waits for. `zebra/digest.py` now derives the stop-path
> list from `outcomes.STOP_KINDS` and marks disabled kinds `[disabled]`, so
> that message cannot drift from the code again.

### CLI

```bash
python -m zebra scan          # one-shot Chartink scan + watchlist add
python -m zebra run           # one full cycle (cron target)
python -m zebra loop          # long-running market-hours loop
python -m zebra status        # dashboard
python -m zebra list [--status STATUS]
python -m zebra analyze SYMBOL --direction CE  # manual strike picker
python -m zebra quote ID      # live re-quote of a signal/position (read-only, JSON)
python -m zebra trigger ID    # force alert on a watching signal
python -m zebra enter ID --pair K_L/K_S --debit X --lots N --expiry YYYY-MM-DD
python -m zebra close ID --exit-debit X --reason tp
python -m zebra cancel ID --reason "..."
```

### Trade store

- Local: `logs/zebra_trades.json`
- Drive: `<drive_folder_id from config/bcs_config.json>/zebra_trades.json`
- Schema: `watching → triggered → entered → exited` (or `cancelled` from watching/triggered)

### Replaced systems (silenced 2026-05-11)

`magnet`, `confidence_tracker`, `spot_tracker`, `flow` are all silenced via `_SILENCED = True` flags in their Telegram functions. Old trade stores archived to `logs/archive/2026-05-11/`. Roll-back instructions in `zebra/SERVER_SETUP.md`.

---

## Server Deployment (Cron Monitor)

### Server Paths
```
Python:       /home/trustit/Desktop/BOTS/CROCODILE/venv/bin/python
Helper:       /home/trustit/Desktop/BOTS/Helper/
Data:         /home/trustit/Desktop/BOTS/data/
Credentials:  /home/trustit/Desktop/BOTS/data/secret.json
Kite token:   /home/trustit/Desktop/BOTS/data/kite_access_token.json
```

### One-Time Setup
```bash
# 1. Install Google Drive dependencies in shared venv
/home/trustit/Desktop/BOTS/CROCODILE/venv/bin/pip install google-auth google-api-python-client

# 2. Verify bcs package imports + Drive connectivity
cd /home/trustit/Desktop/BOTS/Helper
../CROCODILE/venv/bin/python -c "
from bcs.trade_store import get_store
s = get_store()
print(f'Trades: {len(s.load_trades())}, Drive: {s._drive_enabled}')
s.list_trades()
"

# 3. Verify cron mode starts cleanly (exits immediately outside market hours)
../CROCODILE/venv/bin/python -m bcs.spread_monitor --cron --dry-run
```

### Cron Entry
```bash
# BCS Spread Monitor — long-lived process, polls every 5s internally
# flock -n: skip if already running | 5-min retry as crash recovery
# Runs Mon-Fri, 9:00-15:55 IST (monitor waits for 9:15, exits at 15:30)
*/5 9-15 * * 1-5 cd /home/trustit/Desktop/BOTS/Helper && flock -n /tmp/bcs_monitor.lock ../CROCODILE/venv/bin/python -m bcs.spread_monitor --cron >> logs/cron_bcs.log 2>&1
```

**How it works:**
```
9:00  — First invocation starts, waits for market open (9:15)
9:05  — flock sees it's already running, skips silently
9:10  — flock skips
9:15  — Monitor begins polling (spot + spread every 5s)
 ...  — If process crashes, next 5-min cron restarts it
15:30 — Monitor detects market close, exits cleanly
15:35 — Would start but exits immediately (market closed)
```

### Verify It's Running
```bash
# Check if monitor is active
pgrep -f "bcs.spread_monitor --cron" && echo "RUNNING" || echo "NOT RUNNING"

# Tail the log
tail -f /home/trustit/Desktop/BOTS/Helper/logs/cron_bcs.log

# Today's detailed monitor log
tail -f /home/trustit/Desktop/BOTS/Helper/logs/spread_monitor_cron_$(date +%Y%m%d).log
```

### Troubleshooting
| Symptom | Check |
|---------|-------|
| Monitor not starting | `cat logs/cron_bcs.log` — look for import errors or Drive auth failures |
| "No open trades" exit | `python -m bcs.spread_monitor --list` — confirm trades exist with status=open |
| Drive sync failing | Monitor continues in local-only mode — check `cron_bcs.log` for Drive warnings |
| Stale lock file after crash | `rm /tmp/bcs_monitor.lock` (flock auto-releases on process exit, rarely needed) |
| Kite token expired | Check `data/kite_access_token.json` — `generated_at` must be today |

---

## Future Enhancements (Provisioned)

- [x] Debit Spreads (Bull Call / Bear Put) - BCS playbook documented
- [x] BCS trade store with Google Drive sync (`bcs/` package)
- [x] Fallen Hero trade store with Google Drive sync (`fallen_hero/` package)
- [x] Zebra (synthetic long/short via back ratio) — full pipeline (`zebra/` package, 2026-05-11); back ratio since DROPPED, package now runs a BCS cohort — see the deprecation banner in "Zebra Strategy" above
- [ ] Fallen Hero monitor (spread_monitor equivalent)
- [ ] BCS analyzer/scanner script
- [ ] Iron Condor analysis
- [ ] Position exit analyzer
- [x] P&L tracking for open positions (position_checker.py)
- [x] Telegram alerts for opportunities (scanner.py)

## Deprecated (2026-05-11)

These systems are silenced. Code is still importable for backtests; their Telegram functions early-return via `_SILENCED = True`. Trade stores archived to `logs/archive/2026-05-11/`.

- `playbook/magnet/` — replaced by `zebra/`
- `playbook/magnet/confidence_tracker.py` — replaced by zebra's strike analyzer
- `playbook/magnet/spot_tracker.py` — pullback-flip timing not needed in zebra (debit cap handles it)
- `flow/` — multi-TF alignment not used in zebra (trend alignment baked into scanner)

Roll-back: flip `_SILENCED = True` to `False` in each file, restore JSON files from archive.

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
