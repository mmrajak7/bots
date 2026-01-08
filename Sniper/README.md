# Sniper - Options Scanner for Linux Deployment

Clean, production-ready scanner with only essential files for Linux cron deployment.

---

## 📁 Structure

```
Sniper/
├── scanner.py           # Main scanner script
├── data/cache/          # Runtime cache (auto-created)
│   ├── instruments.pkl
│   ├── zones_db.pkl
│   └── alerts_tracker.pkl
├── logs/                # Daily logs (auto-rotated)
│   └── scanner_YYYYMMDD.log
└── README.md            # This file
```

---

## 🚀 Quick Start (Linux)

### 1. Copy to Linux Server

```bash
# From local machine
scp -r Sniper user@server:/home/user/BOTS/

# Verify Bouncer config exists
scp Bouncer/config/config.json user@server:/home/user/BOTS/Bouncer/config/

# Verify Kite token exists
scp data/kite_access_token.json user@server:/home/user/BOTS/data/
```

### 2. Install Dependencies

```bash
# On Linux server
pip3 install kiteconnect requests
```

### 3. Test

```bash
cd /home/user/BOTS/Sniper
python3 scanner.py --test

# Expected output:
# FULL SCAN: HH:MM:SS
# BANKNIFTY: 59686.50
# NIFTY: 25876.85
# SENSEX: 84180.96
```

### 4. Setup Cron (Every Minute)

```bash
crontab -e

# Add these lines:
* 9-14 * * 1-5 cd /home/user/BOTS/Sniper && python3 scanner.py >> logs/cron.log 2>&1
0-30 15 * * 1-5 cd /home/user/BOTS/Sniper && python3 scanner.py >> logs/cron.log 2>&1
```

### 5. Monitor

```bash
# View live logs
tail -f /home/user/BOTS/Sniper/logs/scanner_$(date +%Y%m%d).log

# Check alerts
grep "ALERT:" /home/user/BOTS/Sniper/logs/scanner_$(date +%Y%m%d).log
```

---

## ⚙️ How It Works

### Two-Tier Strategy

**Full Scan (Every 15 mins at :16, :31, :46)**
- Fetches 30 days of historical data
- Identifies reversal zones
- Scores zones (0-100)
- Saves to zones database

**Quick Check (Every Other Minute)**
- Checks if price near zones (within 2%)
- Sends real-time Telegram alerts
- 1-hour cooldown per symbol/zone

**Auto-Detection:**
- Scanner automatically detects which mode to run based on current minute
- No additional configuration needed

---

## 📊 Configuration

Edit `scanner.py` to customize:

```python
# Line 95-105: Configuration
LOOKBACK_DAYS = 30              # Days of historical data (30/45/60)
MIN_BOUNCES = 5                 # Minimum bounces for valid zone
MIN_SCORE = 50                  # Minimum score to track/alert
BUFFER_PCT = 2.0                # Entry buffer (%)
PROXIMITY_PCT = 2.0             # Alert when within N% of zone
ALERT_COOLDOWN_HOURS = 1        # Re-alert cooldown
FULL_SCAN_MINUTES = [16, 31, 46]  # When to run full scan
```

---

## 🔧 Dependencies

### Config Files (Must Exist)

```
BOTS/
├── Bouncer/config/config.json    # Telegram token & chat_id
└── data/kite_access_token.json   # Kite API token
```

### Python Packages

```bash
pip3 install kiteconnect requests
```

---

## 📝 Telegram Alerts

### Full Scan Alert
```
🟢 BANKNIFTY26JAN60000CE [Score: 53]
Zone: 495-513 (11 bounces)
Entry: 485 | Stop: 475
LTP: 493 (AT ZONE)

Other: 485 (9×)
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

## 🐛 Troubleshooting

### Scanner Not Running

```bash
# Check cron
crontab -l

# Test manually
cd /home/user/BOTS/Sniper
python3 scanner.py --test
```

### No Alerts

```bash
# Lower threshold
# Edit scanner.py line 97:
MIN_SCORE = 40  # Instead of 50

# Check logs for zones found
grep "zones tracked" logs/scanner_$(date +%Y%m%d).log
```

### Token Expired

```bash
# Check token date
cat /home/user/BOTS/data/kite_access_token.json | grep generated_at

# Token must be refreshed daily at 8:45 AM by SNAIL bot
```

---

## 📦 File Sizes

```
scanner.py          ~18 KB (main script)
instruments.pkl     ~500 KB (cached daily)
zones_db.pkl        ~50 KB (updated every 15 mins)
alerts_tracker.pkl  ~5 KB (updated every minute)
scanner_*.log       ~2 MB per day
```

---

## ✅ Deployment Checklist

```
[ ] Python 3.8+ installed
[ ] pip3 install kiteconnect requests
[ ] Sniper/ directory copied to server
[ ] Bouncer/config/config.json exists
[ ] data/kite_access_token.json exists
[ ] Test run successful: python3 scanner.py --test
[ ] Cron job added (every minute)
[ ] Logs monitored: tail -f logs/scanner_*.log
[ ] Telegram alerts received
```

---

## 🎯 Why "Sniper"?

- **Precise:** Targets exact reversal zones
- **Fast:** Quick checks every minute for entry signals
- **Clean:** Only essential files, no bloat
- **Ready:** Production-ready for Linux cron

---

**Minimal, efficient, production-ready.** 🎯
