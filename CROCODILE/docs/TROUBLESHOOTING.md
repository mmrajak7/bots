# CROCODILE Troubleshooting Guide

## Kite API Token Issues

### Symptoms
- Error message at 9 AM in Telegram: "Kite API token invalid"
- "Token expired" error
- "Token file not found" error
- Connection validation failed

### Quick Diagnosis

Check Telegram at 9:00 AM:
- **Margin message received** = Kite API working
- **Error alert received** = Token issue, follow remedies below

### Remedies

#### Option 1: Regenerate Token Manually
```bash
cd ~/BOTS/CROCODILE
source venv/bin/activate
python generate_kite_token.py
```

#### Option 2: Switch to Enctoken Method (Fallback)
```bash
nano ~/BOTS/CROCODILE/config/config.yaml
```
Change:
```yaml
kite:
  trade_method: "enctoken"  # Change from "kite_api" to "enctoken"
```

#### Option 3: Run SNAIL Morning Startup
```bash
cd ~/BOTS/SNAIL
source venv/bin/activate
python main.py startup
```

### Token Lifecycle

| Time | Event |
|------|-------|
| 8:45 AM | SNAIL generates fresh token |
| 9:00 AM | CROCODILE validates token and starts |
| 6:00 AM (next day) | Token expires |

### Token File Locations

- Shared token: `~/BOTS/data/kite_access_token.json`
- Local copy: `~/BOTS/CROCODILE/data/kite_access_token.json`

### Verify Token Manually

```bash
cd ~/BOTS/CROCODILE
source venv/bin/activate
python -c "
from src.api.broker_factory import validate_kite_api_token
valid, msg = validate_kite_api_token()
print(f'Valid: {valid}')
print(f'Message: {msg}')
"
```

### Run Full Test Suite

```bash
cd ~/BOTS/CROCODILE
source venv/bin/activate
python test_kite_api_adapter.py
```

---

## Trade Method Configuration

### Current Method Check
```bash
grep "trade_method" ~/BOTS/CROCODILE/config/config.yaml
```

### Available Methods

| Method | Description | Token Source |
|--------|-------------|--------------|
| `enctoken` | Web scraping login | CROCODILE generates own token |
| `kite_api` | Official Kite Connect API | SNAIL generates shared token |

### Switching Methods

Edit `config/config.yaml`:
```yaml
kite:
  trade_method: "kite_api"   # Official API (recommended)
  # OR
  trade_method: "enctoken"   # Fallback method
```

No code changes needed - restart CROCODILE workflows after config change.

---

## Common Errors

### "kiteconnect not installed"
```bash
source venv/bin/activate
pip install kiteconnect>=5.0.0
```

### "Token file not found"
SNAIL hasn't run yet today. Either:
1. Wait for 8:45 AM SNAIL cron job
2. Run `python generate_kite_token.py` manually

### "Token expired"
Token is from previous day (expires 6 AM). Regenerate with:
```bash
python generate_kite_token.py
```

### "Connection validation failed"
1. Check internet connectivity
2. Verify Zerodha API is not down
3. Regenerate token and retry

---

## Emergency Recovery

If all else fails and market is open:

1. **Immediate fallback to enctoken:**
   ```bash
   nano ~/BOTS/CROCODILE/config/config.yaml
   # Set: trade_method: "enctoken"
   ```

2. **Restart CROCODILE:**
   ```bash
   # Kill existing processes
   pkill -f "python.*crocodile"

   # Start fresh
   cd ~/BOTS/CROCODILE
   source venv/bin/activate
   python main.py
   ```

3. **Verify positions are protected:**
   - Check Zerodha Kite app for GTT orders
   - Verify all open positions have stop-loss GTTs
