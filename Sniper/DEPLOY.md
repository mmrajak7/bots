# Sniper - Quick Deployment Guide

**3-minute deployment to Linux server.**

---

## 📦 Step 1: Copy Files to Linux

From your Windows machine:

```bash
# Copy Sniper directory
scp -r C:\Users\mail2\Documents\Projects\BOTS\Sniper user@server:/home/user/BOTS/

# Copy Bouncer config (if not exists)
scp C:\Users\mail2\Documents\Projects\BOTS\Bouncer\config\config.json user@server:/home/user/BOTS/Bouncer/config/

# Copy Kite token (if not exists)
scp C:\Users\mail2\Documents\Projects\BOTS\data\kite_access_token.json user@server:/home/user/BOTS/data/
```

Or use WinSCP, FileZilla, etc.

---

## 🚀 Step 2: Run Automated Setup

SSH to your Linux server:

```bash
ssh user@server
cd /home/user/BOTS/Sniper

# Run automated setup
./setup_cron.sh
```

**What it does:**
- ✅ Checks Python 3 & pip3
- ✅ Installs kiteconnect & requests
- ✅ Verifies config files exist
- ✅ Tests scanner
- ✅ Adds cron job automatically
- ✅ Shows monitoring commands

---

## 📊 Step 3: Monitor

```bash
# View live logs
tail -f /home/user/BOTS/Sniper/logs/scanner_$(date +%Y%m%d).log

# Check alerts
grep "ALERT:" /home/user/BOTS/Sniper/logs/scanner_$(date +%Y%m%d).log

# View cron output
tail -f /home/user/BOTS/Sniper/logs/cron.log
```

---

## ✅ That's It!

Scanner will now run every minute during market hours (9:15 AM - 3:30 PM IST).

- **Full scans** at :16, :31, :46 each hour
- **Quick checks** every other minute
- **Telegram alerts** sent automatically

---

## 🔧 Manual Setup (If Needed)

If you prefer manual setup:

```bash
# Install packages
pip3 install kiteconnect requests

# Test scanner
cd /home/user/BOTS/Sniper
python3 scanner.py --test

# Add to crontab
crontab -e

# Add these lines:
* 9-14 * * 1-5 cd /home/user/BOTS/Sniper && python3 scanner.py >> logs/cron.log 2>&1
0-30 15 * * 1-5 cd /home/user/BOTS/Sniper && python3 scanner.py >> logs/cron.log 2>&1
```

---

## 📁 Final Structure

```
BOTS/
├── Sniper/
│   ├── scanner.py           ← Main script
│   ├── setup_cron.sh        ← Auto-setup script
│   ├── README.md            ← Documentation
│   ├── DEPLOY.md            ← This file
│   ├── data/cache/          ← Runtime cache
│   └── logs/                ← Daily logs
│
├── Bouncer/config/config.json     ← Telegram token
└── data/kite_access_token.json    ← Kite token
```

---

## 🐛 Troubleshooting

**Scanner test fails?**
```bash
python3 scanner.py --test
# Check error message
```

**Config missing?**
```bash
ls -la /home/user/BOTS/Bouncer/config/config.json
ls -la /home/user/BOTS/data/kite_access_token.json
```

**Cron not running?**
```bash
crontab -l  # List cron jobs
grep CRON /var/log/syslog | tail -20  # Check cron logs
```

---

**3 steps, 3 minutes, done!** 🚀
