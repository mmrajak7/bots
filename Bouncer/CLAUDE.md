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
Trade:     Bull Call Spreads + LONG Futures fallback
```

### 2. ATR-Based Targeting
```
Target = LTP + (ATR × 1.2)   # Config: spread_config.atr_multiplier
Min Width = ATR × 1.25       # Dynamic based on volatility
Floor     = 1.0%             # Absolute minimum
```

### 3. Reliability Filter
```
Skip stocks below 30% S/R success rate
Penalize 30-40% success rate: -5 points
Reward 60%+ success rate: +10 to +20 points
```

### 4. Score Thresholds
```
Min Trade Score:  40  # Skip below this
Alert Score:      50  # Send Telegram alert
Futures Score:    90  # LONG futures fallback
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

## Futures Fallback (Score >= 90)

When options have high slippage (> Rs 1000) on exceptional setups:

```
Trade:     LONG Futures (1 lot)
Entry:     Market price
Stop Loss: Support break (1.5%)
Target:    ATR × 1.2 above entry
```

---

## Alert Formats

### Bull Call Spread
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

### Futures LONG
```
🔵 BOUNCER FUTURES ALERT 🔵
RELIANCE - LONG
Score: 92 (EXCEPTIONAL)

Entry: 2815.00 | SL: 2758.00 | Target: 2890.00
R:R: 1:1.3
```

---

## Error Handling

| Error | Action |
|-------|--------|
| Token not found | Check `../data/kite_access_token.json`, run SNAIL auth |
| No setups found | Normal - no stocks at levels currently |
| Reliability not applied | Run `0_build_historical.py` then `0_analyze_reliability.py` |
| Futures alerts missing | Verify `futures.enabled: true` and score >= 90 |
| Position tracking issues | Check `data/open_positions.json` exists and is valid JSON |

---

## Key Config Settings

| Setting | Value | Path |
|---------|-------|------|
| ATR Multiplier | 1.2 | `spread_config.atr_multiplier` |
| Min Score | 40 | `scoring.min_score_to_trade` |
| Alert Score | 50 | `scoring.alert_score` |
| Futures Score | 90 | `futures.min_score` |
| Max Positions | 5 | `position_sizing.max_positions` |
| Scan Interval | 5 min | `scanner.interval_mins` |
| Min S/R Success | 30% | `reliability.min_success_rate` |

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

### v2.1.0 (2026-01-01)
- BULLISH ONLY: Removed all BEARISH/SHORT logic
- SUPPORT levels only: No resistance processed
- Bull Call Spreads only: No Bear Put Spreads
- LONG futures only: Buy futures fallback

### v2.0.0 (2025-12-31)
- Reliability scoring with recency weighting
- Futures fallback for 90+ score setups
- Position tracking with automated exits
- ATR-based targeting

### v1.0.0 (2025-12-31)
- Initial release with S/R detection
