# Opportunity Scanner - Automated Setup

Run the scanner automatically every 15 minutes during market hours.

---

## Features

✅ **Delta Reporting**: Only shows NEW or IMPROVED opportunities
✅ **Smart Caching**: Instruments cached daily, previous scan cached for comparison
✅ **Auto Market Hours**: Only runs during 9:15 AM - 3:30 PM IST (Mon-Fri)
✅ **File Logging**: All scans logged to `logs/opportunity_scanner_YYYYMMDD.log`
✅ **Score-Based Alerts**: Only alerts for scores >= 60 or improvements >= 5 points

---

## Windows Task Scheduler Setup

### Step 1: Open Task Scheduler
1. Press `Win + R`
2. Type `taskschd.msc` and press Enter

### Step 2: Create Task
1. Click **"Create Task"** (not "Create Basic Task")
2. **General Tab**:
   - Name: `Options Scanner`
   - Description: `Runs opportunity scanner every 15 minutes`
   - Run whether user is logged on or not: ✓
   - Run with highest privileges: ✓

### Step 3: Triggers
Click **"New"** and add these triggers (one for each):

**Trigger 1: 9:16 AM - 10:01 AM**
- Begin: On a schedule
- Daily, Recur every: 1 day
- Start: 9:16 AM
- Repeat task every: 15 minutes
- For a duration of: 45 minutes
- Days: Monday, Tuesday, Wednesday, Thursday, Friday
- Enabled: ✓

**Trigger 2: 10:16 AM - 11:01 AM**
- Same settings, Start: 10:16 AM

**Trigger 3: 11:16 AM - 12:01 PM**
- Same settings, Start: 11:16 AM

**Trigger 4: 12:16 PM - 1:01 PM**
- Same settings, Start: 12:16 PM

**Trigger 5: 1:16 PM - 2:01 PM**
- Same settings, Start: 1:16 PM

**Trigger 6: 2:16 PM - 3:01 PM**
- Same settings, Start: 2:16 PM

**Trigger 7: 3:16 PM - 3:31 PM**
- Same settings, Start: 3:16 PM, Duration: 15 minutes

### Step 4: Actions
1. Click **"New"**
2. Action: Start a program
3. Program/script: `C:\Users\mail2\Documents\Projects\BOTS\Helper\helper\run_scanner.bat`
4. Start in: `C:\Users\mail2\Documents\Projects\BOTS\Helper\helper`

### Step 5: Conditions
- Start only if computer is on AC power: ✗ (uncheck)
- Wake the computer to run this task: ✓ (optional)

### Step 6: Settings
- Allow task to be run on demand: ✓
- Run task as soon as possible after a scheduled start is missed: ✓
- If the task fails, restart every: 1 minute
- Attempt to restart up to: 3 times

### Step 7: Save
- Click OK
- Enter your Windows password when prompted

---

## Linux/Mac Cron Setup

### Edit Crontab
```bash
crontab -e
```

### Add Cron Job
```bash
# Run scanner every 16th minute (after 15-min candle close) during market hours
# Monday-Friday, 9:16 AM to 3:31 PM IST
16,31,46 9 * * 1-5 cd /path/to/Helper/helper && python3 opportunity_scanner_live.py
01,16,31,46 10-14 * * 1-5 cd /path/to/Helper/helper && python3 opportunity_scanner_live.py
01,16,31 15 * * 1-5 cd /path/to/Helper/helper && python3 opportunity_scanner_live.py
```

**Explanation:**
- `16,31,46`: Runs at 16th, 31st, 46th minute of the hour
- `9`: 9 AM hour (runs at 9:16, 9:31, 9:46)
- `10-14`: 10 AM to 2 PM (runs at :01, :16, :31, :46)
- `15`: 3 PM hour (runs at 3:01, 3:16, 3:31)
- `1-5`: Monday to Friday
- Script has built-in market hours check as backup

---

## Usage

### Manual Run (Test)
```bash
# Full scan
python opportunity_scanner_live.py --full

# Live mode (delta only)
python opportunity_scanner_live.py

# Test outside market hours
python opportunity_scanner_live.py --test --full
```

### View Logs
```bash
# Today's log
tail -f logs/opportunity_scanner_20260108.log

# Watch for new opportunities (Windows)
Get-Content logs\opportunity_scanner_20260108.log -Wait -Tail 50
```

---

## Output Examples

### First Run (Full Scan)
Shows top 10 opportunities with scores >= 60

### Subsequent Runs (Delta)
```
================================================================================
DELTA REPORT - 10:16:45
================================================================================

🆕 NEW OPPORTUNITIES (2):
#   SYMBOL                    TYPE LTP      ZONE         ENTRY    STOP     BOUNCES  SCORE  STATUS
-------------------------------------------------------------------------------------------------------------------
1   NIFTY26JAN25900PE         PE   169.2    155-174      152.0    148.9    23       69.5   AT LTP
2   BANKNIFTY26JAN60000CE     CE   499.1    555-573      544.0    532.9    33       59.9   13% above

📈 IMPROVED OPPORTUNITIES (1):
#   SYMBOL                    TYPE SCORE  CHANGE   ENTRY    STATUS
-------------------------------------------------------------------------------
1   SENSEX26JAN84000PE        PE   58.2   +6.3     426.3    7% below
```

### No Changes
```
2026-01-08 10:31:15 - INFO - No new or improved opportunities since last scan.
```

---

## File Structure

```
Helper/
├── helper/
│   ├── opportunity_scanner_live.py    # Live scanner (cron-optimized)
│   ├── run_scanner.bat                 # Windows wrapper
│   └── data/
│       └── cache/
│           ├── options_cache.pkl       # Instruments (daily)
│           └── previous_scan.pkl       # Last scan results
└── logs/
    └── opportunity_scanner_YYYYMMDD.log  # Daily log
```

---

## Alerts & Thresholds

| Alert Type | Condition |
|------------|-----------|
| **NEW** | Score >= 60 (high quality only) |
| **IMPROVED** | Score increased by >= 5 points |
| **No Changes** | Logged but not displayed |

---

## Troubleshooting

### Scanner not running
```bash
# Check if task is enabled (Windows)
schtasks /query /tn "Options Scanner" /v

# Test manually
python opportunity_scanner_live.py --test --full
```

### Missing opportunities
- Check `MIN_SCORE_ALERT` in script (default: 60)
- Run with `--full` to see all opportunities
- Check logs for errors

### Token expired
- Ensure `BOTS/data/kite_access_token.json` is valid
- Token should be auto-refreshed at 8:45 AM daily

---

## Performance

- **First run**: ~30-45 seconds (fetches instruments + scans 6 options)
- **Subsequent runs**: ~10-15 seconds (uses cached instruments)
- **API calls per run**: ~10 (3 index quotes + 6 option quotes + 6 historical data)
- **Memory**: ~50MB
- **Logs**: ~5KB per scan (~500KB per day)

---

## Next Steps

1. Test manually first: `python opportunity_scanner_live.py --test --full`
2. Set up Task Scheduler (Windows) or cron (Linux/Mac)
3. Monitor logs for first few runs
4. Adjust `MIN_SCORE_ALERT` if needed (lower = more alerts)
