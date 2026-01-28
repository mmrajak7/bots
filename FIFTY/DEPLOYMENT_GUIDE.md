# FIFTY Bot - Raspberry Pi Deployment Guide

## Pre-Deployment Checklist

- [ ] SSH access to Pi working
- [ ] Git installed on Pi
- [ ] Python venv at `/home/trustit/Desktop/BOTS/CROCODILE/venv`
- [ ] Current cron entry to be replaced

---

## Step 1: Pull Latest Code

```bash
ssh trustit@<pi-ip>
cd /home/trustit/Desktop/BOTS/FIFTY
git pull origin main
```

---

## Step 2: Install Dependencies for HTML→Image Conversion

```bash
# Install wkhtmltopdf (for HTML to image conversion)
sudo apt-get update
sudo apt-get install -y wkhtmltopdf

# Install Python package
/home/trustit/Desktop/BOTS/CROCODILE/venv/bin/pip install imgkit

# Verify installation
which wkhtmltoimage
```

**Note:** If image conversion fails, reports will still send as HTML documents (fallback).

---

## Step 3: Update Telegram BotFather Commands

1. Open Telegram → Search for `@BotFather`
2. Send `/setcommands`
3. Select your FIFTY bot
4. Paste these commands (copy entire block):

```
positions - List open positions with P&L
pending - Show pending signals and orders
stats - Trading statistics and win rate
capital - Capital allocation status
report - Generate reports (Daily/Weekly/Monthly/Overall)
sync - Compare Zerodha vs DB positions
import - Import position from Zerodha
fix - Fix unprotected position (place SL)
kill - Activate kill switch
resume - Deactivate kill switch
help - Show all commands
```

---

## Step 4: Remove Old Cron Entry

```bash
crontab -e
```

Find and comment out (add `#` at start) or delete:
```
*/1 8-16 * * 1-5 cd /home/trustit/Desktop/BOTS/FIFTY && ...
```

Save and exit.

---

## Step 5: Install Systemd Service (Daemon Mode)

### Copy service file:
```bash
sudo cp /home/trustit/Desktop/BOTS/FIFTY/fifty-daemon.service /etc/systemd/system/
```

### Edit service file:
```bash
sudo nano /etc/systemd/system/fifty-daemon.service
```

### Update content to:
```ini
[Unit]
Description=FIFTY Trading Bot Daemon (24/7 Telegram Service)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=trustit
WorkingDirectory=/home/trustit/Desktop/BOTS/FIFTY
ExecStart=/home/trustit/Desktop/BOTS/CROCODILE/venv/bin/python main.py --daemon
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=fifty-daemon

# Resource limits (optional, adjust as needed)
MemoryMax=500M
CPUQuota=50%

[Install]
WantedBy=multi-user.target
```

Save and exit (Ctrl+X, Y, Enter).

---

## Step 6: Enable and Start the Service

```bash
# Reload systemd
sudo systemctl daemon-reload

# Enable service to start on boot
sudo systemctl enable fifty-daemon

# Start the service
sudo systemctl start fifty-daemon

# Check status
sudo systemctl status fifty-daemon
```

---

## Step 7: Verify Deployment

### Check service is running:
```bash
sudo systemctl status fifty-daemon
```

Expected output should show `active (running)`.

### View logs:
```bash
# Follow logs in real-time
sudo journalctl -u fifty-daemon -f

# View recent logs
sudo journalctl -u fifty-daemon --since "10 minutes ago"
```

### Test Telegram:
Send these commands in Telegram:
1. `/help` - Should show command list
2. `/positions` - Should show positions table
3. `/report` - Should show 4 buttons (Daily/Weekly/Monthly/Overall)

---

## Step 8: Setup Watchdog (Recommended)

The watchdog monitors the daemon and auto-restarts if frozen or crashed.

### How it works:
1. Daemon writes heartbeat timestamp every loop (~30 seconds)
2. Watchdog cron checks every 5 minutes:
   - Is service running?
   - Is heartbeat fresh (< 5 minutes old)?
3. If either fails → restart + Telegram alert

### Setup:

```bash
# Make watchdog executable
chmod +x /home/trustit/Desktop/BOTS/FIFTY/watchdog.sh

# Create logs directory
mkdir -p /home/trustit/Desktop/BOTS/FIFTY/logs

# Add to cron
crontab -e
```

Add this line:
```cron
*/5 * * * * /home/trustit/Desktop/BOTS/FIFTY/watchdog.sh >> /home/trustit/Desktop/BOTS/FIFTY/logs/watchdog.log 2>&1
```

### Verify watchdog:
```bash
# Check heartbeat file exists (after daemon starts)
cat /home/trustit/Desktop/BOTS/FIFTY/data/.heartbeat

# View watchdog logs
tail -f /home/trustit/Desktop/BOTS/FIFTY/logs/watchdog.log
```

### What triggers restart:
| Condition | Action |
|-----------|--------|
| Service not running | Restart + alert |
| Heartbeat > 5 min old | Restart + alert (daemon frozen) |
| Restart fails | CRITICAL alert for manual intervention |

---

## Service Management Commands

```bash
# Start service
sudo systemctl start fifty-daemon

# Stop service
sudo systemctl stop fifty-daemon

# Restart service
sudo systemctl restart fifty-daemon

# View status
sudo systemctl status fifty-daemon

# View logs
sudo journalctl -u fifty-daemon -f

# View last 100 lines
sudo journalctl -u fifty-daemon -n 100

# Disable service (won't start on boot)
sudo systemctl disable fifty-daemon
```

---

## Schedule Summary

| Time (IST) | Task | Frequency |
|------------|------|-----------|
| 08:50-09:00 | Token generation | Daily |
| 09:00-09:05 | Morning startup | Daily |
| 09:15-15:30 | Signal processing, order monitoring | Market hours |
| 09:30-09:35 | Re-notify HOLD signals | Daily |
| 15:50-15:55 | Monthly SL trailing | Last trading day |
| 16:00-16:05 | Recovery checks | Daily |
| 16:15-16:20 | Weekly report | Friday |
| 16:20-16:25 | Monthly report | Last trading day |
| 16:25-16:30 | Month-end cleanup | Last trading day |

---

## Troubleshooting

### Service won't start:
```bash
# Check detailed status
sudo systemctl status fifty-daemon -l

# Check Python path
/home/trustit/Desktop/BOTS/CROCODILE/venv/bin/python --version

# Check working directory
ls -la /home/trustit/Desktop/BOTS/FIFTY/main.py
```

### Permission issues:
```bash
# Ensure trustit owns the files
sudo chown -R trustit:trustit /home/trustit/Desktop/BOTS/FIFTY

# Ensure correct permissions
chmod +x /home/trustit/Desktop/BOTS/FIFTY/main.py
chmod +x /home/trustit/Desktop/BOTS/FIFTY/watchdog.sh
```

### Token generation fails:
```bash
# Check credentials file exists
cat /home/trustit/Desktop/BOTS/FIFTY/data/kite_credentials.json

# Manually generate token
cd /home/trustit/Desktop/BOTS/FIFTY
/home/trustit/Desktop/BOTS/CROCODILE/venv/bin/python generate_token.py --force
```

### Telegram not responding:
```bash
# Check if bot token and chat_id are correct
cat /home/trustit/Desktop/BOTS/FIFTY/config/config.yaml | grep -A 3 telegram

# Check daemon is running
sudo systemctl status fifty-daemon

# Check heartbeat is fresh
cat /home/trustit/Desktop/BOTS/FIFTY/data/.heartbeat
```

### Position imported without SL:
```bash
# Use /fix command in Telegram
/fix SCRIPTNAME

# Or re-import (now auto-fixes)
/import SCRIPTNAME
```

---

## Rollback (If Needed)

If something goes wrong, revert to cron mode:

```bash
# Stop daemon
sudo systemctl stop fifty-daemon
sudo systemctl disable fifty-daemon

# Remove watchdog from cron
crontab -e
# Comment out the watchdog line

# Restore cron
crontab -e
```

Add back (every 5 minutes):
```cron
50 8 * * 1-5 cd /home/trustit/Desktop/BOTS/FIFTY && mkdir -p logs && /home/trustit/Desktop/BOTS/CROCODILE/venv/bin/python main.py >> logs/cron.log 2>&1
55 8 * * 1-5 cd /home/trustit/Desktop/BOTS/FIFTY && mkdir -p logs && /home/trustit/Desktop/BOTS/CROCODILE/venv/bin/python main.py >> logs/cron.log 2>&1
*/5 9-16 * * 1-5 cd /home/trustit/Desktop/BOTS/FIFTY && mkdir -p logs && /home/trustit/Desktop/BOTS/CROCODILE/venv/bin/python main.py >> logs/cron.log 2>&1
```

---

## Quick Reference

| Command | Description |
|---------|-------------|
| `/positions` | Clean table with LTP, P&L, Age |
| `/pending` | Pending signals and orders |
| `/stats` | Win rate, P&L statistics |
| `/capital` | Capital allocation status |
| `/report` | Interactive: Daily/Weekly/Monthly/Overall |
| `/sync` | Compare Zerodha vs DB |
| `/import SCRIPT` | Import Zerodha position |
| `/fix SCRIPT` | Fix unprotected position (place SL) |
| `/kill` | Stop all operations |
| `/resume` | Resume operations |
| `/help` | Show all commands |

---

## Files Changed (Summary)

| File | Changes |
|------|---------|
| `main.py` | Daemon mode with heartbeat, long-polling |
| `orchestrator.py` | Monthly report, compact startup message |
| `commands.py` | /fix, /sync, /import, interactive /report |
| `approval_handler.py` | Command routing, help text updates |
| `report_generator.py` | Monthly/Overall reports, HTML→image |
| `bot.py` | send_photo method |
| `signal_processor.py` | NIFTY filter fix (CRITICAL) |
| `order_manager.py` | SL failure alerts |
| `exit_manager.py` | Emergency exit verification |
| `watchdog.sh` | NEW - Auto-restart frozen daemon |

---

## Critical Fixes in This Release

1. **NIFTY Filter was INVERTED** - Was blocking bullish, allowing bearish!
2. **Position without SL** - Now sends CRITICAL alert + /fix command
3. **Emergency exit** - Now verifies order with retries
4. **Commands blocked when awaiting price** - Fixed
5. **Unprotected positions** - /import and /fix now auto-place SL GTT
6. **Duplicate report command** - Fixed unreachable code

---

## New Features

- **Daemon Mode**: 24/7 long-polling for instant Telegram response
- **Watchdog**: Auto-restart frozen/crashed daemon with alerts
- **Heartbeat**: Daemon writes timestamp every loop for health check
- **Interactive Reports**: /report shows buttons for Daily/Weekly/Monthly/Overall
- **Position Sync**: /sync compares Zerodha vs DB positions
- **Import Position**: /import SCRIPT adds existing Zerodha position
- **Fix Position**: /fix SCRIPT places SL on unprotected position
- **Monthly Report**: Auto-sends on last trading day

---

**Deployment Date:** ____________

**Verified By:** ____________
