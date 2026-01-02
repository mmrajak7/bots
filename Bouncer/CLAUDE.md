# Bouncer - Support Bounce Strategy (BULLISH ONLY)

Bull call spreads at support levels using ATR-based targeting.

---

## DAILY WORKFLOW (MANDATORY)

```
┌─────────────────────────────────────────────────────────────────┐
│  WEEKLY (Sunday 8 PM) - Run Once                                │
│  ─────────────────────────────────────────────────────────────  │
│  python scripts/kite_auth.py           # Generate Kite token    │
│  python scripts/0_build_historical.py  # Fetch 4yr history      │
│  python scripts/0_analyze_reliability.py  # Backtest S/R        │
├─────────────────────────────────────────────────────────────────┤
│  PRE-MARKET (8:30 AM Daily)                                     │
│  ─────────────────────────────────────────────────────────────  │
│  python scripts/1_fetch_symbols.py     # Fetch instruments      │
│  python scripts/2_analyze_candidates.py # Compute S/R levels    │
├─────────────────────────────────────────────────────────────────┤
│  MARKET HOURS (9:15 AM - 3:30 PM)                               │
│  ─────────────────────────────────────────────────────────────  │
│  python scripts/3_market_scanner.py --loop  # Continuous scan   │
│  OR: Run via cron every 5 mins                                  │
├─────────────────────────────────────────────────────────────────┤
│  MORNING SL CHECK (9:00 AM Daily)                               │
│  ─────────────────────────────────────────────────────────────  │
│  python scripts/3_market_scanner.py --check-sl                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## CRITICAL RULES

### 1. BULLISH ONLY - Support Bounces
```
Direction: BULLISH only (no shorts)
Levels:    SUPPORT only (no resistance)
Trade:     Bull Call Spreads + OTM Call Buy fallback
```

### 2. ATR-Based Targeting
```
Target = LTP + (ATR × 1.2)   # Config: spread_config.atr_multiplier
Min Width = ATR × 1.25       # Dynamic based on volatility
Floor     = 1.0%             # Absolute minimum
```

### 3. Reliability Filter
```
Skip stocks below 40% S/R success rate
Reward 50%+ success rate: +5 to +25 points
```

### 4. Score Thresholds
```
Min Trade Score:  40  # Skip below this
Alert Score:      50  # Send Telegram alert
OTM Buy Score:    50  # OTM call fallback (when spread fails)
```

---

## Scripts

| Step | Script | Purpose |
|------|--------|---------|
| Weekly | `scripts/kite_auth.py` | Generate fresh Kite token |
| Weekly | `scripts/0_build_historical.py` | Fetch 4 years OHLCV data |
| Weekly | `scripts/0_analyze_reliability.py` | Backtest S/R reliability |
| Daily | `scripts/1_fetch_symbols.py` | Fetch today's instruments |
| Daily | `scripts/2_analyze_candidates.py` | Compute S/R levels |
| Market | `scripts/3_market_scanner.py` | Real-time scanner + alerts |

---

## Kite API Authentication

### Token Location
```
BOTS/data/kite_access_token.json   <- Shared token (SNAIL generates)
```

### Quick Kite Access
```python
import json
from kiteconnect import KiteConnect

with open('../data/kite_access_token.json') as f:
    token_data = json.load(f)

kite = KiteConnect(api_key=token_data['api_key'])
kite.set_access_token(token_data['access_token'])
```

---

## Data Files

| Path | Purpose |
|------|---------|
| `config/config.json` | All configuration |
| `data/levels.json` | Pre-computed S/R levels |
| `data/open_positions.json` | Position tracking |
| `data/sr_reliability.json` | Reliability scores |
| `data/historical/*.csv` | Stock daily OHLCV |
| `logs/` | Scanner logs |

---

## Level Scoring

| Factor | Points |
|--------|--------|
| 2 touches | +10 |
| 3 touches | +20 |
| 4+ touches | +30 |
| Polarity flip (R→S) | +25 |
| Round number | +10 |
| Recent touch (30d) | +10 |
| High volume | +15 |
| Reliability bonus | -15 to +20 |

---

## Entry Rules

| Rule | Value |
|------|-------|
| Max distance from level | 1.5% |
| Min DTE | 10 days |
| Preferred DTE | 15-30 days |
| Max slippage | Rs 750 (skip at Rs 1000) |
| Max debit | 50% of spread width |
| Min R:R | 0.8:1 |

---

## Exit Signals

| Signal | Timing | Condition |
|--------|--------|-----------|
| Take Profit | Intraday (5 min) | LTP >= target price |
| Stop Loss | 9 AM morning | Prev close breaks support by 1.5% |
| Time Stop | Daily check | 15 days no movement |
| Expiry Exit | Daily check | 5 DTE remaining |

---

## OTM Call Buy Fallback (Score >= 50)

When Bull Call Spread fails due to:
- Expensive debit (> 50% of spread width)
- High slippage (> Rs 1000)

```
Trade:     BUY OTM Call (target strike)
Premium:   Max 3% of stock price
Max Loss:  Premium × lot size (CAPPED!)
Target:    ATR × 1.2 above entry

Why OTM instead of Futures:
- Capped risk (no margin calls, no gap risk)
- Capital efficient (₹15k vs ₹3-5 lakhs)
- Same directional exposure
```

---

## Alert Formats

### Bull Call Spread (Primary)
```
🟢 BOUNCER | ICICIBANK
Score: 75 (4T) | 26 DTE

Support: ₹1340 | LTP: ₹1346

BUY ICICIBANK26JAN1340CE @ ₹28.1
SELL ICICIBANK26JAN1360CE @ ₹17.9

Debit: ₹12,285 | R:R 1:1.2
```

### OTM Call Buy (Fallback)
```
🟡 BOUNCER OTM | ICICIBANK
Score: 55 (3T) | 26 DTE

Support: ₹1340 | LTP: ₹1346

BUY ICICIBANK26JAN1360CE @ ₹17.5
Max Loss: ₹12,250 | Target: ₹1375

⚠️ Spread too expensive (debit > 50%)
```

---

## Error Handling

| Error | Action |
|-------|--------|
| Token not found | Check `../data/kite_access_token.json`, run SNAIL auth |
| No setups found | Normal - no stocks at levels currently |
| Reliability not applied | Run `0_build_historical.py` then `0_analyze_reliability.py` |
| OTM alerts missing | Verify `otm_buy.enabled: true` and score >= 50 |
| Position tracking issues | Check `data/open_positions.json` exists and is valid JSON |

---

## Key Config Settings

| Setting | Value | Path |
|---------|-------|------|
| ATR Multiplier | 1.2 | `spread_config.atr_multiplier` |
| Min Score | 40 | `scoring.min_score_to_trade` |
| Alert Score | 50 | `scoring.alert_score` |
| OTM Buy Score | 50 | `otm_buy.min_score` |
| Max Positions | 5 | `position_sizing.max_positions` |
| Scan Interval | 5 min | `scanner.interval_mins` |
| Min S/R Success | 40% | `reliability.min_success_rate` |

---

## Cron Setup (Production)

```cron
# Sunday Weekly Jobs (8 PM)
0 20 * * 0 cd /path/to/Bouncer && python scripts/kite_auth.py >> logs/weekly.log 2>&1
5 20 * * 0 cd /path/to/Bouncer && python scripts/0_build_historical.py >> logs/weekly.log 2>&1
0 23 * * 0 cd /path/to/Bouncer && python scripts/0_analyze_reliability.py >> logs/weekly.log 2>&1

# Daily Pre-market (8:30 AM)
30 8 * * 1-5 cd /path/to/Bouncer && python scripts/1_fetch_symbols.py >> logs/daily.log 2>&1
35 8 * * 1-5 cd /path/to/Bouncer && python scripts/2_analyze_candidates.py >> logs/daily.log 2>&1

# Morning SL Check (9 AM)
0 9 * * 1-5 cd /path/to/Bouncer && python scripts/3_market_scanner.py --check-sl >> logs/sl.log 2>&1

# Market Hours (every 5 min, 9:15-15:30)
*/5 9-15 * * 1-5 cd /path/to/Bouncer && python scripts/3_market_scanner.py >> logs/scanner.log 2>&1
```

---

## Changelog

### v2.2.0 (2026-01-02)
- OTM Call Buy fallback: Replaces futures (capped risk, capital efficient)
- Reliability thresholds tightened: 40% min (was 30%)
- Aggressive reliability bonuses: +25 for 70%+ (was +20)
- Stock universe strict: Only config stocks analyzed
- Compact alert formats

### v2.1.0 (2026-01-01)
- BULLISH ONLY: Removed all BEARISH/SHORT logic
- SUPPORT levels only: No resistance processed
- Bull Call Spreads only: No Bear Put Spreads

### v2.0.0 (2025-12-31)
- Reliability scoring with recency weighting
- Position tracking with automated exits
- ATR-based targeting

### v1.0.0 (2025-12-31)
- Initial release with S/R detection
