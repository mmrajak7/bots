# Telegram Alerts - Examples

This shows what alerts were sent during the test run.

---

## Test Run - 2026-01-08 15:53

**Scan Results:**
- BANKNIFTY: 59686.50
- NIFTY: 25876.85
- SENSEX: 84180.96

**Alerts Sent:** 2

---

### Alert 1: BANKNIFTY CE

```
🟢 BANKNIFTY26JAN60000CE [Score: 53]
Zone: 465-513 (11 bounces)
Entry: 456 | Stop: 447
LTP: 476 (AT ZONE)

Other: 455 (7×), 436 (11×), 521 (8×)
```

**What this means:**
- Reversal zone detected at 465-513 (price bounced here 11 times)
- Strong setup with score 53 (out of 100)
- Entry suggested at 456 (with 2% buffer below zone)
- Stop loss at 447 (2% below entry)
- Current LTP at 476 is inside the zone
- Other nearby zones: 455, 436, 521

---

### Alert 2: NIFTY PE

```
🔴 NIFTY26JAN25900PE [Score: 62]
Zone: 146-174 (24 bounces)
Entry: 143 | Stop: 140
LTP: 171 (AT ZONE)

Other: 135 (35×), 196 (35×), 177 (11×)
```

**What this means:**
- Very strong zone at 146-174 with 24 bounces
- High score of 62 (stronger than BANKNIFTY)
- Entry at 143 with stop at 140
- LTP at 171 is inside the reversal zone (good timing)
- Other strong zones: 135 (35 bounces!), 196 (35 bounces)

---

## Alert Format Breakdown

```
🟢 SYMBOL [Score: XX]              ← Emoji (🟢 CE, 🔴 PE) + Score
Zone: LOW-HIGH (N bounces)         ← Zone range + bounce count
Entry: XXX | Stop: XXX             ← Trade levels
LTP: XXX (STATUS)                  ← Current price + position

Other: XXX (Nx), XXX (Nx)          ← Other nearby zones (notes)
```

**Compact & Clear:**
- One message per option
- Strongest zone highlighted
- Other zones as reference
- Ready to trade (entry/stop provided)

---

## Score Interpretation

| Score | Strength | Action |
|-------|----------|--------|
| 70+ | Excellent | High confidence trade |
| 60-70 | Very Good | Good setup |
| 50-60 | Good | Valid trade |
| 40-50 | Fair | Monitor (no alert) |
| <40 | Weak | Ignore |

**Alert Threshold:** Only score > 50 sent to Telegram

---

## Zone Status

| Status | Meaning |
|--------|---------|
| AT ZONE | LTP is inside the reversal zone (prime entry) |
| 5% below | Zone is 5% below current LTP (wait for dip) |
| 10% above | Zone is 10% above LTP (breakout watch) |

---

## What Happens Next?

### During Trading Hours (Every 15 mins):

1. **New zones** with score > 50 → Alert sent
2. **Improved zones** (score +5) → Alert sent
3. **Broken zones** (price crossed through) → No more alerts for that zone
4. **No changes** → Silent (no spam)

### Next Day:

- All state resets
- Fresh scan starts
- Old alerts cleared
- Logs rotated

---

## Example Day's Alerts

**9:16 AM** - Market opens
```
🟢 NIFTY26JAN25900CE [Score: 55]
Zone: 270-291 (8 bounces)
Entry: 265 | Stop: 260
LTP: 263 (AT ZONE)
```

**9:31 AM** - New opportunity detected
```
🔴 BANKNIFTY26JAN60000PE [Score: 58]
Zone: 604-634 (16 bounces)
Entry: 592 | Stop: 580
LTP: 610 (AT ZONE)
```

**9:46 AM - 2:46 PM** - No changes (silent)

**3:01 PM** - Score improved
```
🟢 NIFTY26JAN25900CE [Score: 61]  ← +6 from earlier
Zone: 270-291 (10 bounces)  ← More bounces added
Entry: 265 | Stop: 260
LTP: 268 (AT ZONE)
```

**3:16 PM** - Zone broken
```
(No alert - zone at 270 broken, stopped tracking)
```

---

## Configuration

To adjust alert frequency/quality, edit `scanner.py`:

```python
MIN_SCORE_ALERT = 50   # Lower = more alerts, Higher = fewer but stronger
MIN_BOUNCES = 5        # Minimum bounces to consider a zone
```

---

**Simple, Clean, No Spam.** ✅
