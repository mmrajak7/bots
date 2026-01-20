# Technical Trading Rules - Support/Resistance Setups

## Philosophy

This system identifies **high-probability support/resistance bounce trades** in liquid F&O stocks. We look for:
1. Clear horizontal levels tested multiple times
2. Previous resistance turned support (or vice versa)
3. Confluence with round numbers
4. Entry at support with tight risk

---

## Part 1: Stock Universe

### Liquidity Requirements

| Criteria | Minimum | Preferred |
|----------|---------|-----------|
| Average Daily Volume | 10 lakh shares | 50 lakh+ |
| Option OI (ATM) | 10 lakh contracts | 50 lakh+ |
| Bid-Ask Spread (ATM) | < Rs 2.00 | < Rs 1.00 |
| Lot Value | < Rs 10 lakh | < Rs 5 lakh |

### Stock Selection

Maintain a watchlist of 15-25 high-liquidity F&O stocks:
- Update weekly (every Sunday)
- Remove stocks with declining OI
- Add stocks showing increased options activity

**Core List (Always Include):**
- RELIANCE, HDFCBANK, ICICIBANK, INFY, TCS
- SBIN, AXISBANK, KOTAKBANK, BAJFINANCE
- TATAMOTORS, MARUTI, LT, HINDUNILVR

**Rotate Based on OI:**
- Check NSE F&O OI data weekly
- Add top gainers in OI
- Remove bottom performers

---

## Part 2: Support/Resistance Identification

### What Makes a Valid Level?

#### A. Multiple Touches (Minimum 2, Preferred 3+)
```
Price
  |     X           X
  |    / \         /
  |   /   \       /
  |  /     \     /
  | /       \   /
  |/         \ /
  +===========X============ SUPPORT (3 touches)
              ^
           Current
```

#### B. Resistance Turned Support (Most Powerful)
```
Price
  |
  |     RESISTANCE----X----
  |                  /|\
  |                 / | \
  |    BREAKOUT-> /  |  \
  |              /   |   \
  |  ----X------    |    X <-- NOW SUPPORT
  |      ^          |    ^
  |   Old Resist    |  Current (at new support)
  |                 |
```

#### C. Round Number Confluence
- Levels near round numbers (1000, 1500, 2000) are stronger
- +/- 1% of round number counts as confluence

#### D. Volume Confirmation
- High volume at the level = stronger support/resistance
- Low volume breakout = likely false breakout

### Level Scoring System

| Factor | Points | Description |
|--------|--------|-------------|
| 2 touches | +10 | Basic validity |
| 3 touches | +20 | Strong level |
| 4+ touches | +30 | Very strong level |
| Resistance turned support | +25 | Polarity flip |
| Round number (+/- 1%) | +10 | Psychological level |
| High volume at level | +15 | Institutional interest |
| Recent touch (< 30 days) | +10 | Level is "active" |
| Clean bounces (not messy) | +10 | Respect shown |

**Minimum Score to Trade: 40 points**

---

## Part 3: Entry Rules

### Setup Checklist (ALL must be YES)

| # | Check | Requirement |
|---|-------|-------------|
| 1 | Valid Level | Score >= 40 |
| 2 | Distance from Level | Within 1.5% of support/resistance |
| 3 | Not Overextended | RSI not < 20 (support) or > 80 (resistance) |
| 4 | DTE | >= 20 days for stocks, >= 7 days for index |
| 5 | Liquidity | ATM OI > 10 lakh |
| 6 | Trend Alignment | Not fighting major trend (optional) |

### Entry Timing

**For Support Bounce (Bullish):**
```
IDEAL: Price touches support and shows reversal candle
       - Hammer / Bullish Engulfing / Morning Star

ACCEPTABLE: Price within 1% of support, no breakdown
```

**For Resistance Rejection (Bearish):**
```
IDEAL: Price touches resistance and shows rejection
       - Shooting Star / Bearish Engulfing / Evening Star

ACCEPTABLE: Price within 1% of resistance, no breakout
```

---

## Part 4: Strategy Selection

### At Support (Bullish Setup)

| Conviction | Strategy | Risk Profile |
|------------|----------|--------------|
| **High** | Bull Call Spread (ITM/OTM) | Defined risk, good R:R |
| **Medium** | Bull Put Spread | Credit, profits if support holds |
| **Low** | Long Call (OTM) | Small premium, lottery |

### At Resistance (Bearish Setup)

| Conviction | Strategy | Risk Profile |
|------------|----------|--------------|
| **High** | Bear Put Spread (ITM/OTM) | Defined risk |
| **Medium** | Bear Call Spread | Credit, profits if resistance holds |
| **Low** | Long Put (OTM) | Small premium |

### Strike Selection Rules

**For Bull Call Spread at Support:**
```
Long Strike:  AT or BELOW support level (ITM or ATM)
Short Strike: AT or ABOVE target level

Example (ICICIBANK at 1346, support 1340, target 1394):
  Long:  1340 CE (at support)
  Short: 1390 CE (near target)
```

**Spread Width Guidelines:**
| Target Move | Spread Width |
|-------------|--------------|
| 3-5% | 40-50 points |
| 5-8% | 50-80 points |
| 8-12% | 80-120 points |

---

## Part 5: Position Sizing

### Risk Per Trade

| Account Size | Max Risk/Trade | Max Positions |
|--------------|----------------|---------------|
| < 5 lakh | 2% (Rs 10,000) | 3 |
| 5-15 lakh | 1.5% (Rs 15,000) | 5 |
| > 15 lakh | 1% | 7 |

### Calculating Position Size

```
Position Size = Max Risk / Max Loss per Lot

Example:
  Max Risk: Rs 15,000
  Bull Call Spread Cost: Rs 15,225 per lot
  Position Size: 1 lot (15,000 / 15,225 = 0.98)
```

---

## Part 6: Exit Rules

### Profit Targets

| Strategy Type | Target 1 | Target 2 | Max Hold |
|---------------|----------|----------|----------|
| Debit Spread | 50% of max | 80% of max | Till expiry |
| Credit Spread | 50% credit decay | 80% credit decay | Till expiry |

### Stop Loss Rules

**Hard Stop:**
- Support break: Close if price closes BELOW support by > 1%
- Time stop: Close if no move in 10 days (theta decay)

**For Bull Call Spread:**
```
Stop Loss Trigger: Support level - 1.5%

Example (Support 1340):
  Stop if ICICIBANK closes below 1320
  Close spread immediately
```

### Exit Checklist

| Condition | Action |
|-----------|--------|
| Target reached | Book 50-80% |
| Support broken (close below) | Exit immediately |
| 5 DTE remaining | Exit if < 50% profit |
| Expiry day | Let ITM spreads expire, close others |

---

## Part 7: Risk Management

### Correlation Rules

- Max 2 positions in same sector
- Max 3 positions in same direction (bullish/bearish)
- No position > 30% of total capital

### When NOT to Trade

| Condition | Action |
|-----------|--------|
| Major event (RBI, Budget, Elections) | No new positions |
| VIX > 20 | Reduce position size by 50% |
| Earnings within 7 days | No position in that stock |
| Gap up/down > 3% | Wait for stabilization |

---

## Part 8: Scanning Workflow

### Daily Routine (Market Hours)

```
9:15 AM  - Run scanner for setups at support/resistance
9:30 AM  - Review top 5 setups
10:00 AM - Enter trades if setups confirm
3:00 PM  - Review positions, adjust stops
```

### Weekly Routine (Weekend)

```
1. Update stock universe (add/remove based on OI)
2. Mark major S/R levels on charts
3. Review past week's trades
4. Plan next week's watchlist
```

---

## Part 9: Setup Examples

### Example 1: ICICIBANK (Dec 2025)

**Setup Identification:**
```
Level: 1340 (Previous resistance, tested 3x)
Current: 1346 (0.4% above support)
Pattern: Resistance turned support
Volume: High volume at level
Score: 30 (3 touches) + 25 (R->S) + 10 (recent) = 65 points
```

**Trade Executed:**
```
Strategy: Bull Call Spread 1340/1390
Entry: Support bounce
Cost: Rs 15,225
Target: Rs 19,775 (at 1390+)
Stop: Close below 1320
R:R: 1:1.3
```

### Example 2: Generic Template

```
Stock: _______________
Level: _______________ (Support/Resistance)
Current Price: _______________
Distance from Level: ___________%
Pattern: _______________
Score: _______________ points

Trade:
  Strategy: _______________
  Long Strike: _______________
  Short Strike: _______________
  Cost: Rs _______________
  Target: Rs _______________
  Stop Loss: Price below _______________
```

---

## Part 10: Record Keeping

### Trade Log Fields

| Field | Description |
|-------|-------------|
| Date | Entry date |
| Stock | Underlying |
| Setup Type | Support bounce / Resistance rejection |
| Level | S/R level |
| Score | Setup score |
| Strategy | Spread type |
| Entry Price | Net debit/credit |
| Exit Price | Net debit/credit |
| P/L | Profit/Loss |
| Notes | What worked/didn't |

### Monthly Review Questions

1. Win rate by setup score (40-60 vs 60+)?
2. Average R:R achieved vs planned?
3. Which stocks had best setups?
4. How many stops hit vs targets?
5. Correlation of losses - same sector/direction?

---

## Quick Reference Card

```
ENTRY:
  - Level score >= 40
  - Within 1.5% of level
  - DTE >= 20 days
  - OI > 10 lakh

STRATEGY:
  - At support: Bull Call Spread (long @ support, short @ target)
  - At resistance: Bear Put Spread

SIZING:
  - Max 1-2% risk per trade
  - Max 5-7 positions

EXIT:
  - Target: 50-80% of max profit
  - Stop: Level break by 1.5%
  - Time: Exit at 5 DTE if not profitable
```

---

## Appendix: Scanner Configuration

See `config/scanner_config.json` for:
- Stock universe list
- Liquidity thresholds
- Scoring weights
- Alert settings
