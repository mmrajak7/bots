# Z-Score Trading Bot - Setup Guide

## Prerequisites

1. Python 3.8+ with pip
2. Kite Connect credentials (auto-refreshed token JSON)
3. Telegram bot token and chat ID

## Installation

```bash
# Clone/copy to Pi
scp -r live_trader pi@raspberrypi:/home/pi/zscore/

# SSH to Pi
ssh pi@raspberrypi

# Install dependencies
cd /home/pi/zscore
pip3 install kiteconnect requests

# Make scripts executable
chmod +x start_bot.sh stop_bot.sh
```

## Configuration

Edit `config.json`:

```json
{
  "credentials": {
    "path": "/path/to/kite_access_token.json"
  },
  "data_dir": "/home/pi/zscore/data",
  "telegram": {
    "enabled": true,
    "bot_token": "YOUR_BOT_TOKEN",
    "chat_id": "YOUR_CHAT_ID"
  }
}
```

### Key Settings

| Setting | Description | Default |
|---------|-------------|---------|
| `data_dir` | Directory for instruments cache, state, logs | Required |
| `instruments.underlying` | Underlying symbol (NIFTY, BANKNIFTY) | NIFTY |
| `instruments.min_dte` | Minimum days to expiry for options | 3 |
| `strategy.z_threshold` | Z-score trigger for current month | 2.5 |
| `strategy.z_threshold_next_month` | Z-score trigger for next month | 3.0 |
| `strategy.valid_hours` | Hours when signals are valid | [13, 14] |
| `risk.max_trades_per_day` | Maximum trades per day | 4 |
| `risk.max_daily_loss` | Stop trading if loss exceeds (Rs) | 3000 |
| `paper_trade` | Enable paper trading mode | true |

### What's Auto-Detected (No config needed)
- Current month futures symbol and token
- Next month futures symbol and token
- ATM option symbol and token (at trade time)

## Cron Setup

```bash
# Edit crontab
crontab -e

# Add these lines:
# Start bot at 12:00 PM on weekdays (ready before 13:00 session)
0 12 * * 1-5 /home/pi/zscore/start_bot.sh >> /home/pi/zscore/logs/cron.log 2>&1

# Stop bot at 15:30 PM on weekdays (after market close)
30 15 * * 1-5 /home/pi/zscore/stop_bot.sh >> /home/pi/zscore/logs/cron.log 2>&1
```

## Manual Operation

```bash
# Start manually
cd /home/pi/zscore
python3 main.py

# Or with paper mode flag
python3 main.py --paper

# Stop (Ctrl+C or)
./stop_bot.sh
```

## Files Created

```
data/
├── nfo_instruments.csv     # Cached instruments (refreshed daily)
├── zscore_state.json       # Trading state (position, trades)
└── logs/
    └── zscore/
        ├── YYYY-MM-DD.log  # Daily trading logs
        └── trades.csv      # Trade history
```

## Monitoring

1. **Telegram Alerts**: Startup, signals, entries, exits, errors
2. **Log Files**: Detailed logs in `data/logs/zscore/`
3. **State File**: Check `zscore_state.json` for current position

## Recovery After Restart

The bot automatically:
1. Loads state from `zscore_state.json`
2. If position exists, resumes monitoring for exit
3. If new day, resets daily counters

## Futures Auto-Detection

**No manual updates needed!** The bot automatically:
1. Fetches all instruments from Kite API on startup
2. Finds NIFTY futures sorted by expiry
3. Uses nearest expiry as current month, second nearest as next month
4. Logs what it detected: `"Auto-detected futures - Current: NIFTY25DECFUT, Next: NIFTY25JANFUT"`

When December futures expire, the bot will automatically use January as current and February as next.

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Symbol not found" | Check instruments cache is fresh, symbols are correct |
| "No ATM option found" | Verify DTE filter, check if market is open |
| WebSocket disconnect | Bot auto-reconnects; check network |
| Telegram not working | Verify bot_token and chat_id |

## Going Live

1. Run in paper mode for 1-2 weeks
2. Verify signals match expected behavior
3. Check P&L calculations
4. Set `paper_trade: false` when ready
