# Bouncer - Support Bounce Strategy (BULLISH ONLY)

## Overview

Bouncer scans high-liquidity F&O stocks for **SUPPORT bounce setups** and suggests optimal **Bull Call Spreads** using **ATR-based targeting**.

**v2.1 Features (BULLISH ONLY):**
- **SUPPORT levels only** - No resistance/bearish trades
- **Bull Call Spreads** - Buy calls at support, sell at ATR target
- **LONG futures fallback** for exceptional setups (90+ score)
- S/R reliability scoring (historical backtest - support only)
- Position tracking with automated exit signals
- Independent Kite token generation

---

## Strategy Logic (BULLISH ONLY)

### 1. Level Detection
- Scans 180 days of price history
- Identifies swing lows (SUPPORT levels only)
- Clusters touches within 1.5% tolerance
- Detects polarity flips (resistance turned support = strong bullish signal)

### 2. Level Scoring

| Factor | Points |
|--------|--------|
| 2 touches | +10 |
| 3 touches | +20 |
| 4+ touches | +30 |
| Polarity flip | +25 |
| Round number | +10 |
| Recent touch (30d) | +10 |
| High volume | +15 |
| **Reliability bonus** | +0 to +20 |

**Minimum score to trade: 40**
**Alert threshold: 50**
**Futures threshold: 90+**

### 3. Reliability Scoring (NEW in v2.0)

Historical backtest determines how well each stock respects S/R levels:

| Success Rate | Bonus Points |
|--------------|--------------|
| 75%+ (Exceptional) | +20 |
| 60-74% (Strong) | +15 |
| 50-59% (Moderate) | +10 |
| 40-49% (Weak) | +5 |
| <40% (Poor) | +0 |

**Recency weighting:**
- Last 1 year: 100% weight
- 1-2 years: 70% weight
- 2-3 years: 40% weight
- 3+ years: 25% weight

### 4. Strike Selection (ATR-Based) - BULLISH ONLY

```
BULL CALL SPREAD at Support:
  Long Strike:  At or below support level
  Short Strike: At ATR x 1.5 target above current price
```

**Why ATR:**
- Adapts to each stock's volatility
- More realistic targets than fixed %
- Better risk/reward optimization

---

## Files

### Core Scripts

| File | Purpose |
|------|---------|
| `scripts/1_fetch_symbols.py` | Daily instrument data fetch |
| `scripts/2_analyze_candidates.py` | Pre-compute S/R levels with reliability bonus |
| `scripts/3_market_scanner.py` | Real-time monitoring + alerts + exit signals |

### Weekly Jobs (Sunday)

| File | Purpose |
|------|---------|
| `scripts/kite_auth.py` | Independent TOTP token generation |
| `scripts/0_build_historical.py` | Fetch 4 years fresh daily data |
| `scripts/0_analyze_reliability.py` | Backtest S/R reliability |

### Config & Data

| Path | Purpose |
|------|---------|
| `config/config.json` | All configuration |
| `data/levels.json` | Pre-computed S/R levels |
| `data/open_positions.json` | Position tracking |
| `data/sr_reliability.json` | Reliability scores |
| `data/historical/` | Stock daily OHLCV CSVs |
| `logs/` | Scanner logs |

---

## Configuration

### Key Settings

| Setting | Value | Location |
|---------|-------|----------|
| Min DTE | 10 days | `entry_rules.min_dte` |
| ATR Multiplier | 1.5 | `spread_config.atr_multiplier` |
| Alert Score | 50+ | `scoring.alert_score` |
| Futures Score | 90+ | `futures.min_score` |
| Scan Interval | 5 mins | `scanner.interval_mins` |

---

## Usage

### Daily Operations

```bash
# Single scan
python scripts/3_market_scanner.py

# Test mode (no alerts)
python scripts/3_market_scanner.py --test

# Loop mode (continuous)
python scripts/3_market_scanner.py --loop

# Morning SL check (run at 9 AM)
python scripts/3_market_scanner.py --check-sl
```

### Weekly Operations (Sunday)

```bash
# Generate fresh Kite token
python scripts/kite_auth.py

# Fetch 4 years historical data
python scripts/0_build_historical.py

# Analyze S/R reliability
python scripts/0_analyze_reliability.py
```

### Pre-market Operations (Daily)

```bash
# Fetch today's instruments
python scripts/1_fetch_symbols.py

# Compute S/R levels for active stocks
python scripts/2_analyze_candidates.py
```

---

## Cron Setup

### Sunday Weekly Jobs (8 PM)

```cron
# Kite token (first - needs valid session)
0 20 * * 0 cd /path/to/BOTS/Bouncer && python scripts/kite_auth.py >> logs/weekly.log 2>&1

# Historical data fetch (~2 hours for ~200 stocks)
5 20 * * 0 cd /path/to/BOTS/Bouncer && python scripts/0_build_historical.py >> logs/weekly.log 2>&1

# Reliability analysis (after data fetch completes)
0 23 * * 0 cd /path/to/BOTS/Bouncer && python scripts/0_analyze_reliability.py >> logs/weekly.log 2>&1
```

### Daily Pre-market (8:30 AM)

```cron
# Fetch instruments
30 8 * * 1-5 cd /path/to/BOTS/Bouncer && python scripts/1_fetch_symbols.py >> logs/daily.log 2>&1

# Compute levels
35 8 * * 1-5 cd /path/to/BOTS/Bouncer && python scripts/2_analyze_candidates.py >> logs/daily.log 2>&1
```

### Daily Morning SL Check (9:00 AM)

```cron
0 9 * * 1-5 cd /path/to/BOTS/Bouncer && python scripts/3_market_scanner.py --check-sl >> logs/sl_check.log 2>&1
```

### Market Hours Scanner (Every 5 mins, 9:15 AM - 3:30 PM)

```cron
*/5 9-15 * * 1-5 cd /path/to/BOTS/Bouncer && python scripts/3_market_scanner.py >> logs/scanner.log 2>&1
```

---

## Position Tracking & Exit Signals

### Entry
When an alert is sent (options or futures), a position is automatically tracked in `data/open_positions.json`.

### Exit Signals

| Signal | Timing | Condition |
|--------|--------|-----------|
| **Take Profit** | Intraday (every 5 mins) | LTP >= target price |
| **Stop Loss** | 9 AM morning check | Previous day close breaks level |

**Why different timing:**
- TP intraday: Don't miss profits when target is hit
- SL on daily close: Avoid false stops from intraday wicks

### Position Lifecycle

```
Entry Alert -> Position Created -> Track -> Exit Signal
     |                                          |
     +------------------------------------------+
                 data/open_positions.json
```

---

## Futures Fallback (LONG ONLY)

For **exceptional setups (score >= 90)** where options have poor liquidity (high slippage):

1. Options setup fails due to slippage > Rs 1000
2. System automatically tries **LONG futures** fallback
3. Sends FUTURES LONG alert with:
   - Entry @ LTP (buy futures)
   - Stop Loss @ support break (1.5%)
   - Target @ ATR x 1.5 above entry
   - Full R:R calculation

**Why futures for exceptional setups:**
- High-score support levels are rare and valuable
- Missing them due to illiquidity is costly
- 1 lot LONG futures with tight SL = defined risk

---

## Alert Types (BULLISH ONLY)

### Bull Call Spread Alert
```
🟢 BOUNCER ALERT 🟢
ICICIBANK - BULLISH
Score: 75 (4 touches)

LEVEL: SUPPORT @ 1340.00
LTP: 1346.00 (0.45% away)

TRADE: BULL CALL SPREAD
BUY  ICICIBANK25JAN1340CE
SELL ICICIBANK25JAN1400CE
Net Debit: Rs 15,225
R:R: 1:2.3
```

### Futures LONG Alert
```
🔵 BOUNCER FUTURES ALERT 🔵
RELIANCE - LONG
Score: 92 (EXCEPTIONAL)

LEVEL: SUPPORT @ 2800.00
LTP: 2815.00

FUTURES TRADE:
BUY RELIANCE25JANFUT
Entry: 2815.00
Stop Loss: 2758.00 (support break)
Target: 2890.00
R:R: 1:1.3

Options illiquid (high slippage) - exceptional setup warrants futures
```

### Exit Alerts
- **TARGET HIT**: 🟢 Green alert with P&L (price rose to target)
- **STOP LOSS**: 🔴 Red alert with support break confirmation

---

## Risk Management (BULLISH ONLY)

| Rule | Value |
|------|-------|
| Max risk per trade | 2% of capital |
| Max positions | 5 |
| Direction | All LONG (bullish) |
| Stop loss | Support break by 1.5% (daily close) |
| Time stop | 10 days no move |
| Futures lots | 1 (fixed) |

---

## Data Flow

```
WEEKLY (Sunday 8 PM):
  Kite API -> data/historical/*.csv -> sr_reliability.db -> sr_reliability.json

DAILY (Pre-market 8:30 AM):
  Kite API -> stock_instruments.csv
  Historical + Reliability -> levels.json

MARKET HOURS (Every 5 mins):
  levels.json + LTP -> Trade Analysis -> Telegram Alert
                                      -> open_positions.json

MORNING (9 AM):
  open_positions.json + Prev Close -> SL Check -> Exit Alerts
```

---

## Troubleshooting

### No setups found
- Check if market is open
- Verify Kite token is valid
- May be no stocks at key levels currently

### Reliability bonus not applied
- Run `0_build_historical.py` first
- Then run `0_analyze_reliability.py`
- Check `data/sr_reliability.json` exists

### Futures alerts not working
- Verify `futures.enabled: true` in config
- Check score threshold (default 90)
- Ensure instruments CSV has futures data

### Position tracking issues
- Check `data/open_positions.json` permissions
- Verify JSON structure is valid
- Run with `--test` to debug without alerts

---

## Changelog

### v2.1.0 (2026-01-01)
- **BULLISH ONLY**: Removed all BEARISH/SHORT logic
- **SUPPORT levels only**: No resistance levels processed
- **Bull Call Spreads only**: No Bear Put Spreads
- **LONG futures only**: Futures fallback is BUY only
- **Simplified position tracking**: All positions are LONG
- **Config update**: Added `strategy.allowed_directions` and `strategy.level_types`

### v2.0.0 (2025-12-31)
- **Reliability Scoring**: Historical S/R backtest with recency weighting
- **Futures Fallback**: For 90+ score setups with illiquid options
- **Position Tracking**: Automated entry/exit tracking
- **Exit Signals**: TP intraday + SL at 9 AM morning check
- **Independent Auth**: Own Kite token generation using SNAIL config
- **Weekly Data Refresh**: 4 years fresh data (handles stock splits)
- **ATR-Based Targeting**: Replaced fixed 4% with volatility-adjusted targets

### v1.0.0 (2025-12-31)
- Initial release
- S/R detection with scoring
- 4% target approach
- Google Sheets integration
- Telegram alerts
- 25-stock universe
