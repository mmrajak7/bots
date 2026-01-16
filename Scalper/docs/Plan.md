# NEO Trade Terminal - Setup & Implementation Guide

## Implementation Summary

### Modules Created (13 files)

| Module | Purpose | Status |
|--------|---------|--------|
| `core/session_manager.py` | Auto-TOTP login for NEO API | ✅ Complete |
| `core/symbol_mapper.py` | Kite → NEO symbol mapping with scrip master | ✅ Complete |
| `core/order_manager.py` | Order execution with safety checks | ✅ Complete |
| `core/kite_spot.py` | Spot price fetcher for ATM calculation | ✅ Complete |
| `core/websocket_handler.py` | Live market data handler | ✅ Complete |
| `core/trailing_sl.py` | Trailing SL (manual + auto modes) | ✅ Complete |
| `core/oco_monitor.py` | OCO simulation for SL/Target pairs | ✅ Complete |
| `core/trade_logger.py` | Trade audit logging to CSV/JSON | ✅ Complete |
| `core/sound_alerts.py` | Sound notifications (Windows beep) | ✅ Complete |
| `core/telegram_notifier.py` | Telegram trade alerts | ✅ Complete |
| `gui/main_window.py` | PyQt6 trading GUI | ✅ Complete |
| `main.py` | Application entry point | ✅ Complete |
| `requirements.txt` | Python dependencies | ✅ Complete |

---

## Manual Steps Required

### Step 1: Install Python Dependencies

```bash
cd Scalper
pip install -r requirements.txt
```

**Note:** NEO API client may need manual installation:
```bash
pip install git+https://github.com/Kotak-Neo/kotak-neo-api.git
```

### Step 2: Create Credentials File

Create `config/credentials.yaml` with your actual credentials:

```yaml
# config/credentials.yaml
# DO NOT commit this file to git!

neo_credentials:
  consumer_key: "YOUR_NEO_CONSUMER_KEY"      # From Kotak Neo developer portal
  mobile_number: "+91XXXXXXXXXX"              # Registered mobile
  ucc: "YOUR_UCC_CODE"                        # Your UCC (User Client Code)
  mpin: "XXXXXX"                              # 6-digit MPIN
  totp_secret: "YOUR_TOTP_SECRET_KEY"         # TOTP secret from authenticator setup

kite_credentials:
  api_key: "YOUR_KITE_API_KEY"                # From Kite Connect
  access_token: ""                            # Leave empty - reads from session file

telegram:
  enabled: true                               # Set false to disable
  bot_token: "YOUR_TELEGRAM_BOT_TOKEN"        # From @BotFather
  chat_id: "YOUR_CHAT_ID"                     # Your Telegram chat ID
```

### Step 3: Kite Access Token (for ATM presets)

The terminal uses Kite for spot prices. Ensure your Kite session file exists at:
```
../data/kite_access_token.json
```

Format:
```json
{
  "api_key": "your_kite_api_key",
  "access_token": "your_access_token"
}
```

**If using existing Kite session from other bots**, update path in `config/settings.yaml`:
```yaml
paths:
  kite_token: "../data/kite_access_token.json"
```

### Step 4: Run the Terminal

```bash
python main.py
```

---

## Configuration Reference

### settings.yaml Options

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `trading_defaults.product` | MIS | MIS/NRML | Default product type |
| `trading_defaults.order_type` | L | L/MKT | Default order type |
| `trading_defaults.default_lots` | 1 | 1-100 | Default lot count |
| `risk_management.max_loss_per_day` | 10000 | ₹ | Daily loss circuit breaker |
| `risk_management.max_open_positions` | 5 | count | Max concurrent positions |
| `risk_management.duplicate_order_window_sec` | 5 | seconds | Duplicate prevention window |
| `trailing_sl.min_sl_distance` | 5 | points | Min SL distance from LTP |
| `trailing_sl.trail_buttons` | [10,25,50] | points | Quick trail button increments |
| `ui_preferences.confirm_before_order` | false | bool | Order confirmation dialogs |
| `ui_preferences.sound_on_fill` | true | bool | Sound alerts |

---

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+B` | Buy (current symbol) |
| `Ctrl+S` | Sell (current symbol) |
| `Ctrl+L` | Focus symbol input |
| `F1` | Load NIFTY CE ATM |
| `F2` | Load NIFTY PE ATM |
| `F3` | Load BANKNIFTY CE ATM |
| `F4` | Load BANKNIFTY PE ATM |
| `F5` | +1 Lot |
| `F6` | -1 Lot |
| `F9` | Exit all positions |
| `F10` | Cancel all pending orders |
| `T` | Trail selected position to COST |
| `Shift+T` | Trail selected +10 points |
| `Escape` | Clear all inputs |

---

## Workflow

### Typical Trading Flow

1. **Start terminal** → Auto-login with TOTP
2. **Load symbol**:
   - Paste Kite symbol (e.g., `NIFTY25JAN24000CE`) OR
   - Press F1-F4 for ATM presets
3. **Set parameters**: Lots, Price, SL, Target
4. **Execute**: Ctrl+B (Buy) or Ctrl+S (Sell)
5. **Manage position**:
   - Trail SL with buttons (BE, +10, +25)
   - Partial exit (50%) or full EXIT
6. **Monitor**: Live P&L updates every 2 seconds

### OCO Workflow (SL + Target)

1. Enter position
2. Set SL and Target in position row
3. OCO monitor auto-cancels opposite leg when one hits

---

## Directory Structure

```
Scalper/
├── config/
│   ├── settings.yaml           # App config (committed)
│   └── credentials.yaml        # Secrets (NOT committed)
├── core/                       # Backend modules
├── gui/                        # PyQt6 frontend
├── data/
│   ├── session.json            # NEO session cache
│   └── scrip_master/           # Daily scrip master cache
├── logs/
│   ├── terminal_YYYYMMDD.log   # App logs
│   └── trades_YYYY-MM-DD.csv   # Trade audit log
├── assets/
│   └── sounds/                 # Alert sounds (optional)
├── main.py                     # Entry point
├── requirements.txt            # Dependencies
└── .gitignore                  # Git rules
```

---

## Troubleshooting

### "NEO login failed"
- Verify credentials in `config/credentials.yaml`
- Check TOTP secret is correct (from authenticator app setup)
- Ensure mobile number includes country code (+91)

### "Symbol not found"
- Scrip master may not be downloaded - check `data/scrip_master/`
- Delete old scrip master files to force re-download
- Verify symbol format matches Kite (e.g., `NIFTY25JAN24000CE`)

### "Kite not connected - ATM presets will not work"
- Update Kite access token (expires daily)
- Check path in `settings.yaml` → `paths.kite_token`

### GUI not responding
- Check logs in `logs/terminal_YYYYMMDD.log`
- Ensure PyQt6 is properly installed

---

## What's NOT Implemented (Future)

- [ ] WebSocket live LTP feed (currently uses polling)
- [ ] GTT orders dialog
- [ ] Signal file watcher for automation
- [ ] Strategy integration (Bouncer/Momentum signals)
- [ ] Multi-account support
- [ ] Custom sound files (currently uses system beeps)
