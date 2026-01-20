# Scanner - Complete Reference Guide

**Quick links to all documentation**

---

## 📚 Documentation Files

| File | Purpose | Read This When... |
|------|---------|-------------------|
| **SCANNER_FLOW_EXPLAINED.md** | Complete flow, config details | You want to understand how it works |
| **LINUX_DEPLOYMENT.md** | Linux deployment, cron setup | You're deploying to Linux server |
| **SCANNER_CRON_SETUP.md** | Windows Task Scheduler setup | You're using Windows |
| **SCANNER_README.md** | Basic overview, how it works | You're new to the scanner |
| **TELEGRAM_ALERTS.md** | Alert examples, format breakdown | You want to see what alerts look like |
| **SCANNER_STATUS.md** | Implementation status, test results | You want to know what's done |
| **CLAUDE.md** | Project-wide instructions | You're working on other Helper scripts |

---

## 🎯 QUICK ANSWERS

### How does the 30 days configuration work?

**File:** `helper/scanner.py`
**Line:** 95

```python
LOOKBACK_DAYS = 30  # ← Change this value
```

**Used here (Line 273):**
```python
def get_historical_data(kite: KiteConnect, token: int) -> List[Dict]:
    return kite.historical_data(
        instrument_token=token,
        from_date=datetime.now() - timedelta(days=LOOKBACK_DAYS),  # ← Uses config
        to_date=datetime.now(),
        interval='15minute'
    )
```

**Effect:**
- 30 days = ~2000 candles (default)
- 45 days = ~3000 candles (more data, stronger zones)
- 60 days = ~4000 candles (very strong zones, longer runtime)

---

### How is Telegram token handled?

**Auto-loaded from Bouncer config (Line 82-86):**

```python
# In scanner.py
with open(BOUNCER_CONFIG) as f:  # Path: BOTS/Bouncer/config/config.json
    BOUNCER_CFG = json.load(f)

TELEGRAM_BOT_TOKEN = BOUNCER_CFG['telegram']['bot_token']
TELEGRAM_CHAT_ID = BOUNCER_CFG['telegram']['chat_id']
```

**Config file location:**
```
BOTS/Bouncer/config/config.json
```

**Config file format:**
```json
{
  "telegram": {
    "bot_token": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
    "chat_id": "123456789"
  }
}
```

**Used here (Line 362):**
```python
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    response = requests.post(url, json=payload, timeout=10)
```

**✅ No hardcoding needed!** Token is shared with Bouncer bot.

---

### What files are needed for Linux deployment?

**Essential Files:**

```
BOTS/
├── Helper/
│   ├── helper/
│   │   ├── scanner.py              ✅ Main script
│   │   └── data/cache/             ✅ Empty dir (auto-populated)
│   │
│   └── logs/                       ✅ Empty dir (auto-populated)
│
├── Bouncer/
│   └── config/
│       └── config.json             ✅ Telegram token
│
└── data/
    └── kite_access_token.json      ✅ Kite API token
```

**NOT Needed:**
- ❌ `opportunity_scanner.py` (old version)
- ❌ `opportunity_scanner_live.py` (old version)
- ❌ `run_scanner.bat` (Windows only)
- ❌ Documentation files (*.md)

---

### Linux Cron Setup (Every Minute)

**Edit crontab:**
```bash
crontab -e
```

**Add these lines:**
```bash
# Run scanner every minute during market hours (9:15 AM - 3:30 PM IST)
* 9-14 * * 1-5 cd /home/user/BOTS/Helper/helper && python3 scanner.py >> ../logs/cron.log 2>&1
0-30 15 * * 1-5 cd /home/user/BOTS/Helper/helper && python3 scanner.py >> ../logs/cron.log 2>&1
```

**Breakdown:**
- `* 9-14`: Every minute from 9 AM to 2:59 PM
- `0-30 15`: Minutes 0-30 of 3 PM (up to 3:30 PM)
- `* * 1-5`: Every day, every month, Monday-Friday
- `cd /home/user/BOTS/Helper/helper`: Change to script directory
- `python3 scanner.py`: Run scanner
- `>> ../logs/cron.log 2>&1`: Append output to log

**Auto-detection:**
- At :16, :31, :46 → Full scan (analyzes zones)
- All other minutes → Quick check (proximity alerts)

---

## 📊 COMPLETE FLOW SUMMARY

### Full Scan (Every 15 mins at :16, :31, :46)

```
1. Get index LTPs (BANKNIFTY, NIFTY, SENSEX)
2. Calculate ATM strikes
3. Fetch 30 days of 15-min historical data
4. Find reversal zones (bounces)
5. Score zones (0-100)
6. Remove broken zones (price moved through)
7. Save to zones database (zones_db.pkl)
```

**Runtime:** ~10-15 seconds

### Quick Check (Every Other Minute)

```
1. Load zones database
2. Get current option LTP
3. Check if price within 2% of any zone
4. If yes + no recent alert → Send Telegram
5. Save alert tracker (1-hour cooldown)
```

**Runtime:** ~2-3 seconds

---

## 🔧 ALL CONFIGURATION OPTIONS

**File:** `helper/scanner.py` (Lines 95-105)

```python
# Historical data
LOOKBACK_DAYS = 30              # Days of history (30/45/60)

# Zone filters
MIN_BOUNCES = 5                 # Min bounces for valid zone
MIN_SCORE = 50                  # Min score to track/alert (40-70)

# Entry/exit
BUFFER_PCT = 2.0                # Entry buffer below zone (%)

# Proximity alerts
PROXIMITY_PCT = 2.0             # Alert when within N% of zone (1-5)
ALERT_COOLDOWN_HOURS = 1        # Re-alert cooldown (0.5-2 hours)

# Timing
FULL_SCAN_MINUTES = [16, 31, 46]  # When to run full scan
```

---

## 🚀 DEPLOYMENT STEPS (Linux)

### 1. Copy Files

```bash
# On Windows
cd C:\Users\mail2\Documents\Projects\BOTS
scp -r Helper/helper/scanner.py user@server:/home/user/BOTS/Helper/helper/
scp Bouncer/config/config.json user@server:/home/user/BOTS/Bouncer/config/
scp data/kite_access_token.json user@server:/home/user/BOTS/data/
```

### 2. Install Dependencies

```bash
# On Linux server
pip3 install kiteconnect requests
```

### 3. Create Directories

```bash
mkdir -p /home/user/BOTS/Helper/helper/data/cache
mkdir -p /home/user/BOTS/Helper/logs
```

### 4. Test

```bash
cd /home/user/BOTS/Helper/helper
python3 scanner.py --test
```

### 5. Setup Cron

```bash
crontab -e

# Add:
* 9-14 * * 1-5 cd /home/user/BOTS/Helper/helper && python3 scanner.py >> ../logs/cron.log 2>&1
0-30 15 * * 1-5 cd /home/user/BOTS/Helper/helper && python3 scanner.py >> ../logs/cron.log 2>&1
```

### 6. Monitor

```bash
tail -f /home/user/BOTS/Helper/logs/scanner_$(date +%Y%m%d).log
```

---

## 🎛️ CUSTOMIZATION EXAMPLES

### More Historical Data (Stronger Zones)
```python
LOOKBACK_DAYS = 60  # Instead of 30
```

### Fewer But Stronger Alerts
```python
MIN_SCORE = 60      # Instead of 50
MIN_BOUNCES = 8     # Instead of 5
```

### Earlier Proximity Alerts
```python
PROXIMITY_PCT = 5.0  # Instead of 2.0
```

### More Frequent Full Scans
```python
FULL_SCAN_MINUTES = [15, 30, 45, 0]  # Every 15 mins
```

### Less Aggressive Alerts
```python
PROXIMITY_PCT = 1.0          # Closer to zone
ALERT_COOLDOWN_HOURS = 2     # Longer cooldown
```

---

## 📊 MONITORING COMMANDS

### View Live Logs
```bash
# Scanner logs
tail -f /home/user/BOTS/Helper/logs/scanner_$(date +%Y%m%d).log

# Cron logs
tail -f /home/user/BOTS/Helper/logs/cron.log
```

### Check Alerts
```bash
# Count alerts today
grep "ALERT:" /home/user/BOTS/Helper/logs/scanner_$(date +%Y%m%d).log | wc -l

# Last 10 alerts
grep "ALERT:" /home/user/BOTS/Helper/logs/scanner_$(date +%Y%m%d).log | tail -10
```

### Check Scan Times
```bash
# Full scans today
grep "FULL SCAN" /home/user/BOTS/Helper/logs/scanner_$(date +%Y%m%d).log

# Quick checks count
grep "Quick check" /home/user/BOTS/Helper/logs/scanner_$(date +%Y%m%d).log | wc -l
```

---

## 🐛 TROUBLESHOOTING

### Scanner Not Running

```bash
# Check cron
crontab -l
grep CRON /var/log/syslog | tail -20

# Test manually
cd /home/user/BOTS/Helper/helper
python3 scanner.py --test
```

### No Alerts

```bash
# Lower threshold
MIN_SCORE = 40  # Instead of 50

# Check logs
grep "zones tracked" /home/user/BOTS/Helper/logs/scanner_$(date +%Y%m%d).log
```

### Token Expired

```bash
# Check token date
cat /home/user/BOTS/data/kite_access_token.json | grep generated_at

# Token must be refreshed daily at 8:45 AM by SNAIL bot
```

---

## 📁 FILE REFERENCE

### Runtime Files (Auto-Created)

```
helper/data/cache/
├── instruments.pkl        ~500KB, daily cache
├── zones_db.pkl          ~50KB, updated every 15 mins (full scan)
└── alerts_tracker.pkl    ~5KB, updated every minute (quick check)

logs/
├── scanner_20260108.log  Daily log (auto-rotated)
└── cron.log              Cron output
```

### Config Files (Must Exist)

```
Bouncer/config/config.json         Telegram token
data/kite_access_token.json        Kite API token
```

---

## 🎯 ALERT EXAMPLES

### Full Scan Alert
```
🟢 BANKNIFTY26JAN60000CE [Score: 53]
Zone: 495-513 (11 bounces)
Entry: 485 | Stop: 475
LTP: 493 (AT ZONE)

Other: 485 (9×), 436 (11×)
```

### Proximity Alert (Quick Check)
```
⚡ ENTRY SIGNAL!

NIFTY26JAN25900PE @ 157
Zone: 155-174 (Score: 62)
Entry: 151 | Stop: 148

Price just entered reversal zone!
```

---

## ✅ QUICK CHECKLIST

**Deployment:**
- [ ] scanner.py copied
- [ ] config.json exists (Telegram token)
- [ ] kite_access_token.json exists
- [ ] pip3 install kiteconnect requests
- [ ] Test: python3 scanner.py --test
- [ ] Cron setup (every minute)

**Monitoring (First Hour):**
- [ ] Full scans at :16, :31, :46
- [ ] Quick checks at other minutes
- [ ] Telegram alerts received
- [ ] No errors in logs

**Production:**
- [ ] Running full trading session
- [ ] Alerts consistent
- [ ] Token refreshed daily

---

## 📖 DOCUMENTATION MAP

```
SCANNER_COMPLETE_GUIDE.md       ← YOU ARE HERE (Quick reference)
│
├─► SCANNER_FLOW_EXPLAINED.md   (Deep dive: flow, config, how 30 days works)
├─► LINUX_DEPLOYMENT.md         (Deploy to Linux, cron setup, monitoring)
├─► SCANNER_CRON_SETUP.md       (Windows Task Scheduler alternative)
├─► SCANNER_STATUS.md           (What's built, test results)
├─► TELEGRAM_ALERTS.md          (Alert format, examples)
└─► SCANNER_README.md           (Basic overview)
```

**Read top to bottom for complete understanding, or jump to specific sections as needed.**

---

**Everything you need in one place!** 🎯
