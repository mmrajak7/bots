# Kayal - Trading Terminal

## Quick Reference

**IMPORTANT: Use Python 3.11** (Python 3.8 has old neo_api_client v1.2.0)

**Run GUI Terminal:**
```bash
py -3.11 main.py
```

**Check NEO API / Positions / Orders:**
```bash
py -3.11 test_api.py --check
```

**Quick Position Check:**
```bash
py -3.11 -c "
import yaml, pyotp, json
from neo_api_client import NeoAPI
creds = yaml.safe_load(open('config/credentials.yaml'))['neo_credentials']
c = NeoAPI(environment='prod', consumer_key=creds['consumer_key'])
c.totp_login(mobile_number=creds['mobile_number'], ucc=creds['ucc'], totp=pyotp.TOTP(creds['totp_secret']).now())
c.totp_validate(mpin=creds['mpin'])
print(json.dumps(c.positions(), indent=2))
"
```

## Project Overview

Kayal is a high-speed PyQt6 GUI trading terminal for **Kotak NEO API v2**, optimized for intraday options scalping.

### Key Facts
- **NEO API v2**: No `consumer_secret` needed - only `consumer_key`
- **Auto-TOTP login**: Generates TOTP automatically using `pyotp`
- **Credentials**: `config/credentials.yaml` (not committed to git)
- **Session cache**: `data/session.json` (valid 8 hours, same day only)

## Architecture

```
core/
├── session_manager.py   # Auto-TOTP login, session management
├── symbol_mapper.py     # Kite symbol → NEO token mapping
├── order_manager.py     # Order execution with safety checks
├── position_tracker.py  # Track positions with SL/Target order IDs
├── kite_spot.py         # Spot prices from Kite for ATM calculation
├── trailing_sl.py       # Manual + auto trailing SL
├── oco_monitor.py       # OCO simulation (cancel other leg on fill)
├── websocket_handler.py # WebSocket for order updates
├── partial_fill_monitor.py
├── realized_pnl_tracker.py
├── order_cancel_manager.py
├── trade_logger.py      # CSV/JSON trade audit
├── telegram_notifier.py
└── sound_alerts.py

gui/
└── main_window.py       # PyQt6 main trading interface
```

## Fetching Positions Programmatically

```python
import yaml
import pyotp
from neo_api_client import NeoAPI

# Load credentials
with open('config/credentials.yaml') as f:
    creds = yaml.safe_load(f)['neo_credentials']

# Initialize (v2 - no consumer_secret)
client = NeoAPI(
    environment='prod',
    access_token=None,
    neo_fin_key=None,
    consumer_key=creds['consumer_key']
)

# Login
totp = pyotp.TOTP(creds['totp_secret'])
client.totp_login(mobile_number=creds['mobile_number'], ucc=creds['ucc'], totp=totp.now())
client.totp_validate(mpin=creds['mpin'])

# Fetch positions
positions = client.positions()
print(positions)
```

## Keyboard Shortcuts (GUI)

| Key | Action |
|-----|--------|
| Ctrl+B | Buy |
| Ctrl+S | Sell |
| F1/F2 | NIFTY CE/PE ATM |
| F3/F4 | BANKNIFTY CE/PE ATM |
| F7/F8 | SENSEX CE/PE ATM |
| F5/F6 | +1/-1 Lot |
| F9 | Exit all positions |
| F10 | Cancel all orders |
| T | Trail to COST |
| Escape | Clear inputs |

## Dependencies

- NEO API v2: `pip install git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git`
- PyQt6, pyotp, pandas, pyyaml, kiteconnect
