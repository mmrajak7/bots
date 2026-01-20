# Striker CLI - Claude Code Instructions

## Quick Start
```bash
run.bat              # Windows launcher
py -3.11 striker.py  # Manual
```

## Environment
- **Python**: 3.11+ required (NEO API v2 compatibility)
- **NEO API**: Credentials in `../Scalper/config/credentials.yaml`
- **Kite API**: Token in `../data/kite_access_token.json`

## Architecture
```
Striker/
├── striker.py           # Main CLI
├── striker_core/        # Striker-specific modules
│   ├── option_chain.py  # Option chain fetcher (uses Kite)
│   └── command_parser.py # Natural language parser
├── strategies/
│   └── builder.py       # Strategy builder (spreads, condors, etc.)
├── config/
│   └── settings.yaml    # Striker-specific settings
└── run.bat              # Windows launcher
```

## Key Commands
| Command | Description |
|---------|-------------|
| `pos` | Show open positions |
| `pos closed` | Show closed positions (today) |
| `pos all` | Show all positions (open + closed) |
| `NIFTY iron condor` | Build iron condor strategy |
| `BANKNIFTY straddle` | Build straddle |
| `trade` / `go` | Execute built strategy |
| `exit NIFTY` | Exit position |
| `exit 50 NIFTY` | Exit 50% of position |
| `sl NIFTY 50%` | Set stop loss at 50% of premium |
| `target NIFTY 80%` | Set target at 80% profit |
| `tsl NIFTY` | Enable trailing stop loss |
| `margin` | Check available margin |
| `status` | Session P&L stats |
| `help` | Show all commands |

## Checking Positions (Quick Script)
```bash
# Open positions
cd /c/Users/mail2/Documents/Projects/BOTS/Striker && echo "pos" | py -3.11 striker.py

# All positions (open + closed)
cd /c/Users/mail2/Documents/Projects/BOTS/Striker && echo "pos all" | py -3.11 striker.py

# Closed positions only
cd /c/Users/mail2/Documents/Projects/BOTS/Striker && echo "pos closed" | py -3.11 striker.py
```

## Dependencies on Scalper
- `core/session_manager.py` - NEO session management
- `core/order_manager.py` - Order execution
- `core/symbol_mapper.py` - Kite to NEO symbol mapping
- `config/credentials.yaml` - API credentials

## Common Issues
1. **Wrong Python version**: Use `py -3.11`, not `python`
2. **Module not found**: Ensure running from Striker directory
3. **Session expired**: Will auto-login with TOTP
4. **Kite token expired**: Refresh via Kite login flow
