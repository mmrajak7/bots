# Scanner - Linux Deployment Guide

Complete guide for deploying the scanner on Linux with cron automation.

---

## 📦 FILES TO DEPLOY

### Required Files (Copy to Linux server)

```
BOTS/
├── Helper/
│   ├── helper/
│   │   ├── scanner.py                ✅ Main scanner script
│   │   └── data/cache/               ✅ Create empty dir (will auto-populate)
│   │       ├── .gitkeep              ✅ Keep dir in git
│   │       └── (pkl files created at runtime)
│   │
│   └── logs/                         ✅ Create empty dir (will auto-populate)
│       └── .gitkeep
│
├── Bouncer/
│   └── config/
│       └── config.json               ✅ MUST EXIST (contains Telegram token)
│
└── data/
    └── kite_access_token.json        ✅ MUST EXIST (Kite API token)
```

### Files NOT Needed

```
❌ opportunity_scanner.py              (old version, not used)
❌ opportunity_scanner_live.py         (old version, not used)
❌ run_scanner.bat                     (Windows only)
❌ *.md files                          (documentation, optional)
```

---

## 🚀 STEP-BY-STEP DEPLOYMENT

### Step 1: Prepare Local Files

On your Windows machine:

```bash
# Create deployment package
cd C:\Users\mail2\Documents\Projects\BOTS

# Verify required files exist
dir Helper\helper\scanner.py
dir Bouncer\config\config.json
dir data\kite_access_token.json
```

### Step 2: Create Deployment Archive

```bash
# Option A: Use Git (recommended)
cd C:\Users\mail2\Documents\Projects\BOTS
git archive --format=tar HEAD Helper Bouncer/config data/kite_access_token.json | gzip > scanner_deploy.tar.gz

# Option B: Manual zip (if no git)
# Zip these folders/files:
# - Helper/helper/scanner.py
# - Helper/helper/data/cache/ (empty dir)
# - Helper/logs/ (empty dir)
# - Bouncer/config/config.json
# - data/kite_access_token.json
```

### Step 3: Copy to Linux Server

```bash
# SCP to server
scp scanner_deploy.tar.gz user@your-server.com:/home/user/

# Or use SFTP, rsync, etc.
rsync -avz --exclude='*.pkl' --exclude='*.log' \
  BOTS/ user@server:/home/user/BOTS/
```

### Step 4: Extract on Server

```bash
ssh user@your-server.com

# Extract
cd /home/user
tar -xzf scanner_deploy.tar.gz

# Verify structure
ls -R BOTS/Helper/helper
ls BOTS/Bouncer/config/config.json
ls BOTS/data/kite_access_token.json
```

### Step 5: Install Python Dependencies

```bash
# Install Python 3.8+ (if not installed)
python3 --version  # Check version

# Install pip (if needed)
sudo apt-get update
sudo apt-get install python3-pip

# Install required packages
pip3 install kiteconnect requests

# Verify installation
python3 -c "import kiteconnect; print(kiteconnect.__version__)"
python3 -c "import requests; print(requests.__version__)"
```

### Step 6: Create Cache Directories

```bash
cd /home/user/BOTS/Helper

# Create cache dir (if not exists)
mkdir -p helper/data/cache
mkdir -p logs

# Set permissions
chmod 755 helper/data/cache
chmod 755 logs
```

### Step 7: Test Scanner

```bash
cd /home/user/BOTS/Helper/helper

# Test run (bypasses market hours check)
python3 scanner.py --test

# Expected output:
# 2026-01-08 19:31:49 - ============================================================
# 2026-01-08 19:31:49 - FULL SCAN: 19:31:49
# 2026-01-08 19:31:49 - ============================================================
# 2026-01-08 19:31:49 - BANKNIFTY: 59686.50
# ...
```

### Step 8: Verify Config Files

```bash
# Check Telegram config
cat /home/user/BOTS/Bouncer/config/config.json

# Should show:
# {
#   "telegram": {
#     "bot_token": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
#     "chat_id": "123456789"
#   }
# }

# Check Kite token
cat /home/user/BOTS/data/kite_access_token.json

# Should show:
# {
#   "access_token": "xxxxx",
#   "api_key": "REDACTED_API_KEY",
#   "user_id": "YL6478",
#   "generated_at": "2026-01-08T08:45:06"
# }
```

---

## ⏰ CRON SETUP (Every Minute)

### Method 1: Simple Cron (Recommended)

```bash
# Edit crontab
crontab -e

# Add this line (runs every minute during market hours)
* 9-14 * * 1-5 cd /home/user/BOTS/Helper/helper && /usr/bin/python3 scanner.py >> /home/user/BOTS/Helper/logs/cron.log 2>&1
0-30 15 * * 1-5 cd /home/user/BOTS/Helper/helper && /usr/bin/python3 scanner.py >> /home/user/BOTS/Helper/logs/cron.log 2>&1
```

**Explanation:**
- `* 9-14`: Every minute from 9:00 AM to 2:59 PM
- `0-30 15`: Minutes 0-30 of 3 PM (9:00 AM - 3:30 PM IST)
- `* * 1-5`: Every day, every month, Monday-Friday
- `cd ...`: Change to script directory
- `/usr/bin/python3`: Full path to Python
- `>> logs/cron.log`: Append output to log
- `2>&1`: Redirect stderr to stdout

### Method 2: Wrapper Script (Better Logging)

Create wrapper script:

```bash
# Create wrapper
nano /home/user/BOTS/Helper/helper/run_scanner.sh
```

Add content:

```bash
#!/bin/bash
# Scanner wrapper for cron

# Set working directory
cd /home/user/BOTS/Helper/helper

# Log start time
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting scanner..." >> ../logs/cron.log

# Run scanner
/usr/bin/python3 scanner.py >> ../logs/cron.log 2>&1

# Check exit code
if [ $? -ne 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Scanner failed!" >> ../logs/cron.log
fi
```

Make executable:

```bash
chmod +x /home/user/BOTS/Helper/helper/run_scanner.sh
```

Add to crontab:

```bash
crontab -e

# Add these lines
* 9-14 * * 1-5 /home/user/BOTS/Helper/helper/run_scanner.sh
0-30 15 * * 1-5 /home/user/BOTS/Helper/helper/run_scanner.sh
```

### Method 3: IST Timezone Handling

If server is in different timezone:

```bash
crontab -e

# Convert IST to server timezone
# Example: IST = UTC+5:30
# 9:15 AM IST = 3:45 AM UTC
# 3:30 PM IST = 10:00 AM UTC

# Run every minute from 3:45 AM to 10:00 AM UTC (9:15 AM - 3:30 PM IST)
45-59 3 * * 1-5 /home/user/BOTS/Helper/helper/run_scanner.sh
* 4-9 * * 1-5 /home/user/BOTS/Helper/helper/run_scanner.sh
0-30 10 * * 1-5 /home/user/BOTS/Helper/helper/run_scanner.sh
```

---

## 📝 SYSTEMD SERVICE (Alternative to Cron)

For better control and monitoring, use systemd:

### Create Service File

```bash
sudo nano /etc/systemd/system/options-scanner.service
```

Add content:

```ini
[Unit]
Description=Options Scanner Service
After=network.target

[Service]
Type=simple
User=your_username
WorkingDirectory=/home/user/BOTS/Helper/helper
ExecStart=/usr/bin/python3 /home/user/BOTS/Helper/helper/scanner.py
Restart=on-failure
RestartSec=60
StandardOutput=append:/home/user/BOTS/Helper/logs/scanner.log
StandardError=append:/home/user/BOTS/Helper/logs/scanner_error.log

[Install]
WantedBy=multi-user.target
```

### Create Timer (Runs Every Minute)

```bash
sudo nano /etc/systemd/system/options-scanner.timer
```

Add content:

```ini
[Unit]
Description=Options Scanner Timer (Every Minute)
Requires=options-scanner.service

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
Persistent=true

[Install]
WantedBy=timers.target
```

### Enable and Start

```bash
# Reload systemd
sudo systemctl daemon-reload

# Enable timer (start on boot)
sudo systemctl enable options-scanner.timer

# Start timer
sudo systemctl start options-scanner.timer

# Check status
sudo systemctl status options-scanner.timer
systemctl list-timers options-scanner.timer

# View logs
journalctl -u options-scanner.service -f
```

---

## 🔍 MONITORING & DEBUGGING

### View Live Logs

```bash
# Scanner logs (auto-rotated daily)
tail -f /home/user/BOTS/Helper/logs/scanner_$(date +%Y%m%d).log

# Cron logs
tail -f /home/user/BOTS/Helper/logs/cron.log

# System cron logs
grep CRON /var/log/syslog | tail -20
```

### Check Cron Status

```bash
# List current crontab
crontab -l

# Check if cron service is running
systemctl status cron

# View recent cron jobs
grep scanner /var/log/syslog | tail -20
```

### Test Scanner Manually

```bash
cd /home/user/BOTS/Helper/helper

# Force full scan (bypass time check)
python3 scanner.py --force

# Test mode (bypass market hours)
python3 scanner.py --test

# Normal mode (respects market hours)
python3 scanner.py
```

### Check Cache Files

```bash
cd /home/user/BOTS/Helper/helper/data/cache

# List cache files
ls -lh

# Expected files (after first run):
# instruments.pkl        (~500KB, cached daily)
# zones_db.pkl          (~50KB, updated every 15 mins)
# alerts_tracker.pkl    (~5KB, updated every minute)

# Check file ages
stat instruments.pkl
stat zones_db.pkl
```

### Verify Telegram Delivery

```bash
# Check scanner logs for "ALERT:"
grep "ALERT:" /home/user/BOTS/Helper/logs/scanner_$(date +%Y%m%d).log

# Test Telegram manually
python3 -c "
import requests
import json

with open('/home/user/BOTS/Bouncer/config/config.json') as f:
    cfg = json.load(f)

url = f\"https://api.telegram.org/bot{cfg['telegram']['bot_token']}/sendMessage\"
payload = {'chat_id': cfg['telegram']['chat_id'], 'text': 'Scanner test message'}
response = requests.post(url, json=payload)
print(response.status_code, response.text)
"
```

---

## 🛠️ TROUBLESHOOTING

### Issue 1: Cron Job Not Running

```bash
# Check cron service
sudo systemctl status cron

# Restart cron
sudo systemctl restart cron

# Check syslog for errors
grep scanner /var/log/syslog | tail -20
```

### Issue 2: Python Not Found

```bash
# Find Python path
which python3

# Update crontab with full path
# Use: /usr/bin/python3 (or whatever 'which' shows)
```

### Issue 3: Module Import Error

```bash
# Verify kiteconnect installed
python3 -c "import kiteconnect"

# If error, install:
pip3 install kiteconnect requests

# Check Python path
python3 -c "import sys; print(sys.path)"
```

### Issue 4: Permission Denied

```bash
# Fix permissions
chmod 755 /home/user/BOTS/Helper/helper/scanner.py
chmod 755 /home/user/BOTS/Helper/helper/data/cache
chmod 755 /home/user/BOTS/Helper/logs

# If using wrapper script:
chmod +x /home/user/BOTS/Helper/helper/run_scanner.sh
```

### Issue 5: Config File Not Found

```bash
# Verify paths match scanner.py expectations
python3 -c "
from pathlib import Path
script_dir = Path('/home/user/BOTS/Helper/helper').resolve()
helper_dir = script_dir.parent
bots_dir = helper_dir.parent
print('BOTS_DIR:', bots_dir)
print('Bouncer config:', bots_dir / 'Bouncer' / 'config' / 'config.json')
print('Token file:', bots_dir / 'data' / 'kite_access_token.json')
"

# Verify files exist at printed paths
```

### Issue 6: Token Expired

```bash
# Check token file date
cat /home/user/BOTS/data/kite_access_token.json | grep generated_at

# Token expires at 6 AM IST next day
# Needs daily refresh at 8:45 AM by SNAIL bot

# If token is stale, run SNAIL authentication
```

---

## 📊 RESOURCE USAGE

### Expected Resource Consumption

```
CPU Usage:
- Full scan: ~10-20% for 10-15 seconds
- Quick check: ~5-10% for 2-3 seconds
- Idle: 0% (not a daemon)

Memory:
- Full scan: ~50-80 MB
- Quick check: ~30-50 MB

Disk:
- Logs: ~5 KB per scan (~2 MB per day)
- Cache: ~550 KB total

Network:
- Full scan: ~15 API calls (~50 KB data)
- Quick check: ~3 API calls (~10 KB data)
- Per day: ~6000 API calls (~10 MB data)
```

### Monitor Resources

```bash
# CPU/Memory during scan
top -p $(pgrep -f scanner.py)

# Disk usage
du -sh /home/user/BOTS/Helper/logs
du -sh /home/user/BOTS/Helper/helper/data/cache

# Network (if iftop installed)
sudo iftop
```

---

## 🔐 SECURITY BEST PRACTICES

### 1. Protect Sensitive Files

```bash
# Restrict token file access
chmod 600 /home/user/BOTS/data/kite_access_token.json
chmod 600 /home/user/BOTS/Bouncer/config/config.json

# Verify ownership
chown your_username:your_username /home/user/BOTS/data/kite_access_token.json
```

### 2. Run as Non-Root User

```bash
# NEVER run scanner as root
# Create dedicated user if needed
sudo useradd -m -s /bin/bash scanner_user
sudo su - scanner_user

# Copy files to user's home
cp -r /home/user/BOTS /home/scanner_user/

# Add crontab as scanner_user
crontab -e
```

### 3. Firewall (If Needed)

```bash
# Allow outgoing HTTPS (Kite API, Telegram)
sudo ufw allow out 443/tcp

# No incoming ports needed for scanner
```

---

## 📁 FINAL DIRECTORY STRUCTURE

After deployment, you should have:

```
/home/user/BOTS/
├── Helper/
│   ├── helper/
│   │   ├── scanner.py                    ← Main script
│   │   └── data/cache/
│   │       ├── instruments.pkl           ← Created at runtime
│   │       ├── zones_db.pkl              ← Created at runtime
│   │       └── alerts_tracker.pkl        ← Created at runtime
│   │
│   └── logs/
│       ├── scanner_20260108.log          ← Daily log (auto-rotated)
│       └── cron.log                      ← Cron output log
│
├── Bouncer/
│   └── config/
│       └── config.json                   ← Telegram token
│
└── data/
    └── kite_access_token.json            ← Kite API token
```

---

## ✅ DEPLOYMENT CHECKLIST

```
Pre-Deployment:
[ ] Python 3.8+ installed on server
[ ] kiteconnect package installed (pip3 install kiteconnect)
[ ] requests package installed (pip3 install requests)
[ ] config.json exists with valid Telegram token
[ ] kite_access_token.json exists and is current (< 1 day old)

Files Copied:
[ ] scanner.py uploaded to server
[ ] data/cache/ directory created (empty)
[ ] logs/ directory created (empty)
[ ] Bouncer/config/config.json uploaded
[ ] data/kite_access_token.json uploaded

Testing:
[ ] Manual test run successful: python3 scanner.py --test
[ ] Telegram alert received
[ ] Cache files created (instruments.pkl, zones_db.pkl)
[ ] Logs written to logs/scanner_YYYYMMDD.log

Cron Setup:
[ ] Crontab edited: crontab -e
[ ] Cron job added (every minute during market hours)
[ ] Full path to python3 used: which python3
[ ] Working directory set: cd /home/user/BOTS/Helper/helper
[ ] Logs redirected: >> logs/cron.log 2>&1

Monitoring (First Hour):
[ ] tail -f logs/scanner_YYYYMMDD.log
[ ] Verify full scans at :16, :31, :46
[ ] Verify quick checks at other minutes
[ ] Check Telegram alerts received
[ ] Monitor grep "ALERT:" logs/scanner_YYYYMMDD.log

Production:
[ ] Scanner running for full trading session
[ ] No errors in logs
[ ] Alerts received consistently
[ ] Token auto-refreshed daily (by SNAIL at 8:45 AM)
```

---

## 🚦 DEPLOYMENT COMPLETE

Once all steps are done:

1. ✅ Scanner deployed on Linux
2. ✅ Cron job runs every minute
3. ✅ Full scans every 15 mins at :16, :31, :46
4. ✅ Quick checks every other minute
5. ✅ Telegram alerts sent automatically
6. ✅ Logs auto-rotated daily

**Monitor for first trading session, then let it run!** 🚀

---

## 📞 SUPPORT COMMANDS

```bash
# Quick status check
tail -20 /home/user/BOTS/Helper/logs/scanner_$(date +%Y%m%d).log

# Check last 10 alerts
grep "ALERT:" /home/user/BOTS/Helper/logs/scanner_$(date +%Y%m%d).log | tail -10

# Restart scanner manually (kills any stuck processes)
pkill -f scanner.py
python3 /home/user/BOTS/Helper/helper/scanner.py --test

# View full scan times today
grep "FULL SCAN" /home/user/BOTS/Helper/logs/scanner_$(date +%Y%m%d).log

# Check quick checks count
grep "Quick check" /home/user/BOTS/Helper/logs/scanner_$(date +%Y%m%d).log | wc -l
```

---

**Linux deployment complete!** Ready for production. 🎯
