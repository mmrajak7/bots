# Options Scanner - Simple & Clean

Scans BANKNIFTY, NIFTY, SENSEX every 15 minutes for reversal zones.
Sends Telegram alerts for strong setups (score > 50).

---

## How It Works

1. **Fetches live LTPs** for indices
2. **Calculates ATM strikes** (auto-rounded)
3. **Scans reversal zones** using last 30 days of 15-min data
4. **Scores each zone** (0-100) based on:
   - Bounces (more = stronger)
   - Strength (how well price recovered)
   - Proximity to LTP
   - Freshness (recent touches)
5. **Sends Telegram** for zones with score > 50
6. **Tracks broken zones** - stops alerting when price crosses through

---

## Setup (Linux Cron)

### 1. Make executable
```bash
chmod +x /path/to/Helper/helper/scanner.py
```

### 2. Add to crontab
```bash
crontab -e
```

### 3. Add this line (runs every 15 mins during market hours)
```bash
# Options Scanner - every 15 minutes (9:15 AM - 3:30 PM IST, Mon-Fri)
*/15 9-15 * * 1-5 cd /path/to/BOTS/Helper/helper && python3 scanner.py >> scanner.log 2>&1
```

**Explanation:**
- `*/15`: Every 15 minutes
- `9-15`: Hours 9 AM to 3 PM
- `* * 1-5`: Every day, every month, Monday to Friday
- `>> scanner.log 2>&1`: Append output to log

### 4. Test it
```bash
cd /path/to/BOTS/Helper/helper
python3 scanner.py
```

---

## Telegram Alert Format

```
🟢 NIFTY26JAN25900CE [Score: 65]
Zone: 155-174 (23 bounces)
Entry: 152 | Stop: 149
LTP: 170 (AT ZONE)

Other: 135 (35×), 196 (35×)
```

**Compact format:**
- One alert per option (CE/PE separate)
- Strongest zone shown
- Other zones listed at bottom as notes
- Score visible for confidence

---

## How Zones Are Scored (0-100)

| Factor | Weight | Example |
|--------|--------|---------|
| **Bounces** | 50% | 5 bounces = 20 pts, 50+ = 50 pts |
| **Strength** | 20% | How high price closed after touching zone |
| **Proximity** | 20% | Closer to LTP = more tradeable |
| **Freshness** | 10% | Recent touches (< 7 days) |

**Alert Threshold:** Only sends if score > 50

---

## Broken Zone Detection

A zone is **broken** when:
- Price goes **3% below** zone bottom (support broken)
- Price goes **3% above** zone top (resistance broken)

Once broken, the scanner **stops alerting** for that zone.

---

## Files

```
Helper/
├── helper/
│   ├── scanner.py                  # Main scanner
│   └── data/
│       └── cache/
│           ├── instruments.pkl     # Cached daily
│           ├── scanner_state.pkl   # Tracks zones & alerts
└── logs/
    └── scanner_YYYYMMDD.log        # Daily log (auto-cleared)
```

---

## Logs

**Auto-rotation:**
- Creates new log daily: `scanner_20260108.log`
- Deletes previous day's log automatically
- Keeps only current day

**View live logs:**
```bash
tail -f logs/scanner_20260108.log
```

---

## Alert Logic

### When alerts are sent:

1. **New zone** with score > 50
2. **Existing zone improved** by 5+ points
3. **NOT sent** for broken zones

### Deduplication:

- Each zone alerted only once per day
- Tracks by symbol + zone price
- Resets daily

---

## Config

All config is in the script. Adjust if needed:

```python
MIN_SCORE_ALERT = 50   # Minimum score to alert
LOOKBACK_DAYS = 30     # Historic data window
MIN_BOUNCES = 5        # Minimum bounces for valid zone
BUFFER_PCT = 2.0       # Entry buffer %
```

---

## Telegram Config

Uses Bouncer's Telegram config automatically:
- `BOTS/Bouncer/config/config.json`
- Bot token: Already configured
- Chat ID: Already configured

---

## Manual Run (Test)

```bash
cd /path/to/BOTS/Helper/helper

# Test (bypasses market hours check)
python3 scanner.py --test

# Normal run (checks market hours)
python3 scanner.py
```

Output:
```
2026-01-08 10:16:45 - ============================================================
2026-01-08 10:16:45 - SCAN START: 10:16:45
2026-01-08 10:16:45 - ============================================================
2026-01-08 10:16:46 - BANKNIFTY: 59691.75
2026-01-08 10:16:46 - NIFTY: 25879.00
2026-01-08 10:16:46 - SENSEX: 84194.91
2026-01-08 10:16:47 - NIFTY: ATM 25900, Expiry 27-Jan
2026-01-08 10:16:48 - ALERT: NIFTY26JAN25900PE PE - Zone 165 (Score: 69)
2026-01-08 10:16:49 - SCAN COMPLETE: 1 alerts sent
```

---

## Troubleshooting

### No alerts sent
- Check `MIN_SCORE_ALERT` (lower to 40 for testing)
- Check logs for errors
- Verify Telegram token in Bouncer config

### Duplicate alerts
- State file tracks alerts
- Resets daily
- Delete `data/cache/scanner_state.pkl` to reset

### Scanner not running (cron)
```bash
# Check cron logs
grep CRON /var/log/syslog

# Test manually
python3 scanner.py
```

---

## Performance

- **Runtime:** ~15 seconds per scan
- **API calls:** ~10 per scan
- **Memory:** ~30MB
- **Disk:** ~5KB logs per day

---

## Next Steps

1. Test manually: `python3 scanner.py`
2. Check Telegram alert received
3. Add to cron: `crontab -e`
4. Monitor logs for first hour

---

**That's it! Simple, clean, effective.** 🚀
