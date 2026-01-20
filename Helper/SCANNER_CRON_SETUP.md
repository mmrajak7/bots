# Scanner - Every Minute Setup (Two-Tier Strategy)

Run the scanner automatically **every minute** during market hours.

---

## How It Works

### **Automatic Mode Detection**

The scanner automatically decides what to do based on the current minute:

**Minutes :16, :31, :46 → FULL SCAN**
- Fetches 30 days of 15-min historical data
- Identifies reversal zones
- Scores and ranks zones
- Removes broken zones (price moved 3% through)
- Saves zones to database

**All Other Minutes → QUICK CHECK**
- Loads zones from database
- Fetches current LTP
- Checks if price within **2% of any zone**
- Sends **proximity alert** (real-time entry signal)
- **1-hour cooldown** per symbol/zone (no spam)

---

## Windows Task Scheduler Setup

### Step 1: Open Task Scheduler
1. Press `Win + R`
2. Type `taskschd.msc` and press Enter

### Step 2: Create Task
1. Click **"Create Task"** (not "Create Basic Task")
2. **General Tab**:
   - Name: `Options Scanner (Every Minute)`
   - Description: `Two-tier scanner: Full scan every 15 mins + quick proximity checks`
   - Run whether user is logged on or not: ✓
   - Run with highest privileges: ✓

### Step 3: Triggers
Click **"New"** and configure:

**Trigger Settings:**
- Begin: On a schedule
- Daily, Recur every: 1 day
- Start: 9:15 AM
- Repeat task every: **1 minute**
- For a duration of: **6 hours 15 minutes** (9:15 AM - 3:30 PM)
- Days: Monday, Tuesday, Wednesday, Thursday, Friday
- Enabled: ✓

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
- Run task as soon as possible after a scheduled start is missed: ✗ (uncheck)
- If the running task does not end when requested, force it to stop: ✓
- If the task is already running, do not start a new instance: ✓ (IMPORTANT)

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
# Run scanner every minute during market hours (9:15 AM - 3:30 PM IST, Mon-Fri)
* 9-14 * * 1-5 cd /path/to/Helper/helper && python3 scanner.py >> ../logs/scanner.log 2>&1
0-30 15 * * 1-5 cd /path/to/Helper/helper && python3 scanner.py >> ../logs/scanner.log 2>&1
```

**Explanation:**
- `* 9-14`: Every minute from 9 AM to 2:59 PM
- `0-30 15`: Minutes 0-30 of 3 PM (until 3:30 PM)
- `1-5`: Monday to Friday
- Script has built-in market hours check as backup

---

## Alert Examples

### Full Scan Alert (at :16, :31, :46)
```
🟢 BANKNIFTY26JAN60000CE [Score: 53]
Zone: 495-513 (11 bounces)
Entry: 485 | Stop: 475
LTP: 493 (AT ZONE)

Other: 485 (9×)
```

### Proximity Alert (any minute when price near zone)
```
⚡ ENTRY SIGNAL!

NIFTY26JAN25900PE @ 157
Zone: 155-174 (Score: 62)
Entry: 151 | Stop: 148

Price just entered reversal zone!
```

---

## Alert Cooldown Logic

**Problem:** Without cooldown, you'd get an alert every minute while price stays in zone.

**Solution:** 1-hour cooldown per symbol+zone combination
- First alert: ✓ Sent immediately
- Next 59 minutes: ✗ Suppressed
- After 1 hour: ✓ Can alert again

---

## Log Monitoring

### View Today's Log
```bash
# Windows (PowerShell)
Get-Content ..\logs\scanner_20260108.log -Wait -Tail 50

# Linux/Mac
tail -f ../logs/scanner_20260108.log
```

### Log Rotation
- **Automatic:** Creates new log daily (`scanner_YYYYMMDD.log`)
- **Cleanup:** Deletes previous day's log automatically
- **Retention:** Only keeps current day

---

## Performance

| Metric | Full Scan | Quick Check |
|--------|-----------|-------------|
| **Runtime** | ~10-15 sec | ~2-3 sec |
| **API Calls** | ~15 | ~3 |
| **Frequency** | 3x per hour | ~57x per hour |
| **Data Fetched** | Historical + LTP | LTP only |

**Total per hour:**
- Full scans: 3
- Quick checks: 57
- Total API calls: ~215
- Well within Kite API limits (3000 req/sec)

---

## Troubleshooting

### Scanner not running
```bash
# Check if task is enabled (Windows)
schtasks /query /tn "Options Scanner (Every Minute)" /v

# Test manually
cd helper
python scanner.py --test
```

### Missing zones in quick check
- Wait for next full scan (:16, :31, or :46)
- Full scan builds the zones database
- Quick check depends on zones DB

### Too many alerts
- Increase `PROXIMITY_PCT` in `scanner.py` (default: 2.0%)
- Increase `ALERT_COOLDOWN_HOURS` (default: 1 hour)

### Token expired
- Ensure `BOTS/data/kite_access_token.json` is valid
- Token should be auto-refreshed at 8:45 AM daily

---

## Configuration

Edit `scanner.py` to adjust:

```python
# Full scan timing
FULL_SCAN_MINUTES = [16, 31, 46]  # Which minutes trigger full scan

# Alert thresholds
MIN_SCORE = 50                     # Minimum score to track zone
MIN_BOUNCES = 5                    # Minimum bounces for valid zone

# Proximity alerts
PROXIMITY_PCT = 2.0                # Alert when within 2% of zone
ALERT_COOLDOWN_HOURS = 1           # Don't re-alert for 1 hour
```

---

## File Structure

```
Helper/
├── helper/
│   ├── scanner.py                    # Main scanner (two-tier)
│   ├── run_scanner.bat               # Windows wrapper
│   └── data/
│       └── cache/
│           ├── instruments.pkl       # Cached daily
│           ├── zones_db.pkl          # Full scan results
│           └── alerts_tracker.pkl    # Cooldown tracking
└── logs/
    └── scanner_YYYYMMDD.log          # Daily log (auto-rotated)
```

---

## What Happens Each Minute

```
09:15 → Quick check (no zones yet)
09:16 → FULL SCAN (builds zones DB)
09:17 → Quick check (zones available)
09:18 → Quick check
...
09:30 → Quick check
09:31 → FULL SCAN (updates zones)
09:32 → Quick check
...
09:45 → Quick check
09:46 → FULL SCAN (updates zones)
09:47 → Quick check
...
15:30 → Quick check (last run of day)
```

---

## Testing

### Test Full Scan
```bash
cd helper
python scanner.py --test
```

### Test Quick Check
1. Run full scan first (builds zones DB)
2. Wait 1 minute
3. Run again (should do quick check)

### Force Full Scan
```bash
python scanner.py --force
```

---

## Next Steps

1. ✅ Scanner built with two-tier strategy
2. ✅ Test run completed successfully (2 alerts sent)
3. ✅ Documentation complete
4. ⏳ Set up Windows Task Scheduler (every 1 minute)
5. ⏳ Monitor logs for first hour

---

**Simple, Smart, No Spam.** ⚡
