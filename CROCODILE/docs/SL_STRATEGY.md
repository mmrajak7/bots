# Stop Loss Strategy: Legacy vs ATR-Based (S4-Refined)

## The Problem

CROCODILE enters Weekly SuperTrend breakout trades. At SuperTrend entry, price is always near the ST line. The old trailing SL (week's LOW) sat just 1-5% below entry. Normal weekly volatility immediately triggered SL before the trade could play out.

**Analysis of 25 Weekly SL-hit trades:**
- 92% of stopped-out trades recovered and went higher
- 0% survival rate (all got stopped out prematurely)
- Average P&L: -1.45%

---

## OLD Strategy: Week's LOW Trailing

### How it worked

```
Entry Day:   Place GTT at Entry - 15% (dummy protective SL)
Every Friday EOD (3:50 PM):
  1. Fetch this week's daily candles (Mon-Fri)
  2. Find the minimum LOW of the week
  3. If week_low > current_SL: move SL up to week_low
  4. If week_low <= current_SL: no change (SL never moves down)
```

### Example: COFORGE entry at Rs.1800

```
Week 0 (Entry):  SL = 1800 - 15% = Rs.1530.00 (dummy)
Week 1 Friday:   Week LOW = Rs.1760 → SL moves to Rs.1760 (2.2% from entry)
Week 2 Friday:   Week LOW = Rs.1740 → no change (1740 < 1760)
Week 3 Friday:   Week LOW = Rs.1780 → SL moves to Rs.1780 (1.1% from entry)
Week 4 Monday:   Stock dips to Rs.1775 → SL HIT at Rs.1780 → EXIT
Week 4 Friday:   Stock closes at Rs.1850 (recovered, but we're already out)
```

### Why it failed

| Issue | Detail |
|-------|--------|
| SL too tight | Week's LOW is typically 1-3% from close for large caps |
| No volatility awareness | Same logic for a 2% weekly range stock (HDFC) and a 8% range stock (ADANI) |
| Premature exits | Normal intraweek dips trigger SL before weekly candle completes |
| First week is worst | At SuperTrend entry, price is near the line. First week's LOW immediately becomes a tight SL |

---

## NEW Strategy: ATR-Based Adaptive SL (S4-Refined)

### How it works

**At Entry:**
```
1. Calculate Weekly ATR(14) using Wilder's smoothing
2. Initial SL = Entry - 2.0 x Weekly_ATR
3. Store entry_atr in database for future trailing calculations
4. Place GTT at this SL on Zerodha
```

**Every Friday EOD (3:50 PM):**
```
1. Get Friday's close price
2. ACTIVATION CHECK: Is friday_close > entry + 1.5 x entry_atr?
   - NO  → Do nothing. SL stays where it is. (Stock hasn't moved enough)
   - YES → Proceed to trailing calculation
3. Calculate current Weekly ATR(14) (may differ from entry ATR if volatility changed)
4. New SL = friday_close - 2.0 x current_ATR
5. If new_SL > current_SL → move SL up
6. If new_SL <= current_SL → no change (SL never moves down)
```

### Key Parameters (config.yaml)

```yaml
sl_strategy:
  enabled: true
  initial_atr_multiplier: 2.0       # Initial SL = Entry - 2.0 x ATR
  trailing_activation_multiplier: 1.5 # Start trailing after Close > Entry + 1.5 x ATR
  trailing_atr_multiplier: 2.0       # Trailing SL = Close - 2.0 x ATR
  atr_period: 14                     # 14-week ATR period
  fallback_sl_percent: 10            # If ATR calculation fails
```

### Safety Rails

| Guard | Rule |
|-------|------|
| SL never moves down | `new_sl > current_sl` enforced in `update_gtt_with_trailing_sl()` |
| 50% floor | Initial SL never more than 50% below entry |
| ATR API fails at entry | Uses 10% fixed fallback SL (not the old 15%) |
| ATR API fails at trailing | Falls back to entry_atr stored in DB |
| Both ATR calls fail | No SL change, position stays at current SL |
| Legacy positions (entry_atr=NULL) | Old week-LOW behavior, untouched |

---

## Side-by-Side Example: COFORGE at Rs.1800

**Weekly ATR(14) = Rs.85**

### OLD (Week's LOW)

```
                  Price    SL      Gap from Entry
Entry:            1800     1530    -15.0% (dummy)
Week 1 (Fri):     1820     1760    -2.2% (week LOW)     <-- SL jumps dangerously close
Week 2 (Fri):     1790     1760    -2.2% (no change)
Week 3 (Fri):     1810     1780    -1.1%                <-- 1.1% gap = death zone
Week 4 (Mon):     1775     EXIT    SL HIT at 1780
                           Loss: -1.1% (Rs.20/share)
Week 4 (Fri):     1850     --      Would have been +2.8% profit
```

### NEW (ATR-Based)

```
                  Price    SL      Gap from Entry   Notes
Entry:            1800     1630    -9.4%            Initial = 1800 - 2x85
Week 1 (Fri):     1820     1630    -9.4%            Not activated (1820 < 1927.50*)
Week 2 (Fri):     1790     1630    -9.4%            Not activated (1790 < 1927.50)
Week 3 (Fri):     1810     1630    -9.4%            Not activated (1810 < 1927.50)
Week 4 (Mon):     1775     1630    -9.4%            SURVIVES (1775 > 1630)
Week 4 (Fri):     1850     1630    -9.4%            Not activated (1850 < 1927.50)
Week 8 (Fri):     1950     1780    -1.1%            ACTIVATED! SL = 1950 - 2x85
Week 12 (Fri):    2100     1930    +7.2%            SL above entry = risk-free
Week 16 (Mon):    1920     EXIT    SL HIT at 1930
                           Profit: +7.2% (Rs.130/share)

* Activation threshold = 1800 + 1.5 x 85 = Rs.1927.50
```

**Same trade. Old system: -1.1% loss. New system: +7.2% profit.**

---

## Example: High Volatility Stock (LTIM at Rs.5400)

**Weekly ATR(14) = Rs.350**

### OLD

```
Entry:            5400     4590    -15.0% (dummy)
Week 1 (Fri):     5350     5200    -3.7% (week LOW)
Week 2 (Tue):     5180     EXIT    SL HIT at 5200
                           Loss: -3.7%
```

### NEW

```
Entry:            5400     4700    -13.0%   Initial = 5400 - 2x350
Week 1 (Fri):     5350     4700    -13.0%   Not activated (5350 < 5925*)
Week 2 (Tue):     5180     4700    -13.0%   SURVIVES (5180 > 4700)
Week 4 (Fri):     5600     4700    -13.0%   Not activated (5600 < 5925)
Week 8 (Fri):     6100     5400    0.0%     ACTIVATED! SL = 6100 - 2x350
                                            Breakeven protected
Week 12 (Fri):    6500     5800    +7.4%    SL = 6500 - 2x350, risk-free

* Activation threshold = 5400 + 1.5 x 350 = Rs.5925
```

ATR = Rs.350 means this stock swings Rs.350/week. Old SL at Rs.200 gap (3.7%) was inside normal noise. New SL at Rs.700 gap (13%) respects the volatility.

---

## Example: Low Volatility Stock (HDFCLIFE at Rs.700)

**Weekly ATR(14) = Rs.32**

### OLD

```
Entry:            700      595     -15.0% (dummy)
Week 1 (Fri):     705      692     -1.1% (week LOW)     <-- immediate danger
Week 2 (Wed):     690      EXIT    SL HIT at 692
                           Loss: -1.1%
```

### NEW

```
Entry:            700      636     -9.1%    Initial = 700 - 2x32
Week 1 (Fri):     705      636     -9.1%    Not activated (705 < 748*)
Week 2 (Wed):     690      636     -9.1%    SURVIVES (690 > 636)
Week 6 (Fri):     760      636     -9.1%    ACTIVATED! (760 > 748)
                                            SL = 760 - 2x32 = 696
                                            But 696 > 636 → SL moves to 696
Week 8 (Fri):     780      716     +2.3%    SL = 780 - 2x32, now risk-free

* Activation threshold = 700 + 1.5 x 32 = Rs.748
```

ATR = Rs.32 for a Rs.700 stock means ~4.6% weekly range. Initial SL at 9.1% gives 2 ATR of breathing room.

---

## Activation Gate: Why It Matters

Without the gate, trailing would start immediately on the first profitable Friday close:

```
Entry: 1800, ATR: 85
Week 1 Friday close: 1820
  Without gate: SL = 1820 - 170 = 1650 (moved up from 1630 to 1650)
  With gate:    SL stays at 1630 (1820 < 1927.50 threshold)
```

This seems minor (Rs.20 difference), but over multiple weeks of small ups and downs, the gateless version keeps ratcheting up the SL during sideways movement until a normal dip triggers it. The gate ensures trailing only starts after the trade has proven itself with a 1.5 x ATR move.

---

## Backward Compatibility

| Scenario | Behavior |
|----------|----------|
| New Weekly entries | ATR calculated at fill, stored in `entry_atr`, ATR-based SL |
| Existing positions (backfilled) | `entry_atr` set via backfill script, ATR-based SL going forward |
| Existing positions (not backfilled) | `entry_atr = NULL`, legacy week-LOW behavior unchanged |
| Daily (D) positions | Unchanged. Still uses day's LOW |
| Monthly (M) positions | Unchanged. Still uses month's LOW |
| `sl_strategy.enabled: false` | Everything reverts to legacy behavior |
| ATR API fails at entry | `entry_atr = NULL`, uses 10% fallback SL, then legacy trailing |

---

## Config Required (config.yaml - not tracked in git)

Add under `trading:` section after `dummy_sl_percent: 15`:

```yaml
  sl_strategy:
    enabled: true
    initial_atr_multiplier: 2.0
    trailing_activation_multiplier: 1.5
    trailing_atr_multiplier: 2.0
    atr_period: 14
    fallback_sl_percent: 10
```

## Database Changes

Two new columns added via auto-migration (runs on first `get_session()` call):
- `open_positions.entry_atr` (FLOAT, nullable)
- `closed_positions.entry_atr` (FLOAT, nullable)

No manual migration needed. Column is added automatically on first workflow run after code deployment.
