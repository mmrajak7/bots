# SNAIL - Systematic NIFTY Automated Iron-fly Leverager

Automated options trading system that executes Iron Fly strategies on NIFTY weekly options.

## Features

- **Iron Fly Strategy**: Sell ATM straddle + Buy OTM wings for defined risk
- **Zerodha Kite Integration**: Automated order execution via Kite Connect API
- **Claude AI Advisory**: Pre-entry checks, stop-loss decisions, market event analysis
- **Telegram Alerts**: Real-time notifications with interactive response buttons
- **Paper Trading Mode**: Test without risking real money

## Quick Start

```bash
# Clone
git clone https://github.com/mmrajak7/bots.git
cd bots/SNAIL

# Install
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.template .env
# Edit .env with your credentials

# Initialize
python main.py init

# Test
python main.py test

# Run
python main.py run
```

## Commands

| Command | Description |
|---------|-------------|
| `python main.py run` | Start trading loop |
| `python main.py startup` | Run morning checks |
| `python main.py status` | Show current status |
| `python main.py entry` | Check entry conditions |
| `python main.py exit --force` | Force exit position |
| `python main.py summary` | Generate daily summary |
| `python main.py test` | Run system tests |

## Configuration

Edit `.env` for credentials:
- Zerodha Kite API credentials
- Anthropic Claude API key
- Telegram bot token

Set `SNAIL_PAPER_TRADING=true` for paper trading mode.

## Cron Schedule (Raspberry Pi)

```cron
# Daily startup - 8:45 AM
45 8 * * 1-5 /home/pi/bots/SNAIL/scripts/cron_startup.sh

# Entry attempts - Hourly 9:30-14:30
30 9-14 * * 1-5 /home/pi/bots/SNAIL/scripts/cron_entry.sh

# Monitoring - Every 10 min
*/10 9-15 * * 1-5 /home/pi/bots/SNAIL/scripts/cron_monitor.sh

# Daily summary - 3:35 PM
35 15 * * 1-5 /home/pi/bots/SNAIL/scripts/cron_summary.sh
```

## Structure

```
SNAIL/
├── main.py              # CLI entry point
├── config/
│   ├── config.yaml      # Trading parameters
│   └── prompts/         # Claude prompt templates
├── src/
│   ├── api/             # Kite, Claude, Telegram clients
│   ├── services/        # Entry, exit, monitoring logic
│   ├── workflows/       # Process orchestration
│   └── utils/           # Helpers, DB, calculations
├── scripts/             # Cron job scripts
└── data/
    └── schema.sql       # Database schema
```

## License

Private - All rights reserved.
