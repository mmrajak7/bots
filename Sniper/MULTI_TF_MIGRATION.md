# Multi-Timeframe Migration Guide

## Overview

Sniper scanner now supports **multiple timeframes** (15m and 1h) with independent zone detection and alerts.

---

## What Changed

### 1. **Configuration**
- Added `TIMEFRAMES` dict with per-TF settings:
  - `15m`: 30-day lookback, scans at :16, :31, :46, 1h cooldown
  - `1h`: 90-day lookback, scans at :17, :47, 2h cooldown

### 2. **Zones Database Structure**
**Before:**
```python
zones_db = {
    'NIFTY26JAN25900PE': {
        'ltp': 160,
        'zones': [...]
    }
}
```

**After:**
```python
zones_db = {
    'NIFTY26JAN25900PE': {
        '15m': {
            'ltp': 160,
            'zones': [...]
        },
        '1h': {
            'ltp': 160,
            'zones': [...]
        }
    }
}
```

### 3. **Alert Tracking**
**Before:** `SYMBOL_ZONE`
**After:** `SYMBOL_ZONE_TIMEFRAME`

Example: `NIFTY26JAN25900PE_164_15m`

### 4. **Alert Format**
**Before:**
```
🔴 NIFTY26JAN25900PE PE [Score: 65]
```

**After:**
```
🔴 NIFTY26JAN25900PE PE [15M] [Score: 65]
🔴 NIFTY26JAN25900PE PE [1H] [Score: 72]
```

---

## Migration Steps

### Option A: Clean Start (Recommended)

```bash
cd /path/to/Sniper

# Backup existing zones
cp data/cache/zones_db.pkl data/cache/zones_db.pkl.backup.$(date +%Y%m%d)

# Delete old zones (incompatible structure)
rm -f data/cache/zones_db.pkl

# Keep alerts tracker (will auto-cleanup old format entries)
# New multi-TF format will be created on first run
```

### Option B: Gradual Rollout

Test 1h timeframe first, then enable both:

**Step 1:** Disable 15m temporarily
```python
# In scanner.py:
TIMEFRAMES = {
    '15m': {
        'enabled': False  # Temporarily disable
    },
    '1h': {
        'enabled': True   # Test 1h only
    }
}
```

**Step 2:** Run for 1 day, compare zones quality

**Step 3:** Enable both timeframes

---

## Deployment (Linux)

```bash
# Pull latest code
cd /path/to/Sniper
git pull

# Backup cache
mkdir -p data/cache/backup
cp data/cache/*.pkl data/cache/backup/

# Clear old zones (structure changed)
rm -f data/cache/zones_db.pkl

# Restart scanner (cron will auto-run)
# Or force test run:
python3 scanner.py --test
```

---

## Verification

### Check Logs

```bash
tail -f logs/scanner_$(date +%Y%m%d).log
```

You should see:
```
--- Scanning 15M timeframe ---
NIFTY (15m): ATM 25900, Expiry 27-Jan
NIFTY26JAN25900PE (15m): 3 zones tracked

--- Scanning 1H timeframe ---
NIFTY (1h): ATM 25900, Expiry 27-Jan
NIFTY26JAN25900PE (1h): 2 zones tracked

Zones DB updated: 5 timeframe entries across 1 symbols
```

### Check Alerts

Alerts will include timeframe badge:
```
🔴 NIFTY26JAN25900PE PE [15M] [Score: 65]
Zone: 155-174 (25 bounces, 69% strength)
Entry: 151 | Stop: 147 | LTP: 163
```

### Check Cache Files

```bash
ls -lh data/cache/
```

You should see:
- `instruments.pkl` (existing)
- `zones_db.pkl` (new multi-TF structure)
- `alerts_tracker.pkl` (updated with TF keys)

---

## Configuration Options

### Disable a Timeframe

```python
TIMEFRAMES = {
    '15m': {
        'enabled': True
    },
    '1h': {
        'enabled': False  # Disable 1h
    }
}
```

### Adjust Scan Schedule

```python
TIMEFRAMES = {
    '15m': {
        'scan_minutes': [15, 30, 45]  # Different minutes
    },
    '1h': {
        'scan_minutes': [20, 50]  # Your choice
    }
}
```

### Adjust Cooldowns

```python
TIMEFRAMES = {
    '15m': {
        'cooldown_hours': 0.5  # 30 min cooldown
    },
    '1h': {
        'cooldown_hours': 3  # 3 hour cooldown
    }
}
```

### Change Lookback Period

```python
TIMEFRAMES = {
    '15m': {
        'lookback_days': 45  # More history
    },
    '1h': {
        'lookback_days': 120  # Even more for 1h
    }
}
```

---

## Expected Behavior

### API Calls

**Before:** 6 historical data calls per full scan (3 indices × 2 opt types × 1 TF)

**After:** Still 6 calls per run, but staggered:
- At :16, :31, :46 → 15m scan (6 calls)
- At :17, :47 → 1h scan (6 calls)

**Peak:** 6 API calls/minute ✓ Well within Zerodha limits (3 req/sec)

### Alert Frequency

- Same zone can alert on both timeframes independently
- 15m zones: 1h cooldown
- 1h zones: 2h cooldown

Example:
```
09:47 - NIFTY PE [15M] near 164 (alerted)
09:48 - NIFTY PE [1H] near 158 (alerted)  ← Different zone, different TF
10:47 - NIFTY PE [15M] near 164 (blocked - cooldown)
11:47 - NIFTY PE [15M] near 164 (re-alerted)
11:48 - NIFTY PE [1H] near 158 (blocked - cooldown)
```

---

## Troubleshooting

### Issue: No alerts after migration

**Cause:** zones_db.pkl has old structure

**Fix:**
```bash
rm data/cache/zones_db.pkl
# Wait for next full scan or force run
python3 scanner.py --test
```

### Issue: Duplicate alerts (same zone, both TFs)

**Expected behavior!** Each timeframe tracks independently.

If you want only highest TF, disable 15m:
```python
TIMEFRAMES = {
    '15m': {'enabled': False},
    '1h': {'enabled': True}
}
```

### Issue: Logs show "Skipping 1h scan"

**Cause:** Current minute not in scan_minutes

**Check:**
```bash
date +%M  # Show current minute
```

Ensure current minute is 17 or 47 for 1h scan.

---

## Rollback

If you need to revert to single-TF:

```python
# Disable multi-TF
TIMEFRAMES = {
    '15m': {'enabled': True},
    '1h': {'enabled': False}  # Disable
}
```

Then:
```bash
rm data/cache/zones_db.pkl  # Clear multi-TF zones
# Old 15m-only structure will be recreated
```

---

## Benefits

1. **Higher Conviction**: 1h zones typically stronger (higher scores)
2. **More Coverage**: 15m catches short-term reversals
3. **Flexibility**: Choose TF based on your trading style
4. **Independent Tracking**: Same zone on different TFs = different setups

---

## Next Steps

1. ✅ Pull latest code (`git pull`)
2. ✅ Backup cache files
3. ✅ Clear old zones_db.pkl
4. ✅ Monitor first full scan (check logs)
5. ✅ Verify alerts include timeframe badge
6. ✅ Compare 15m vs 1h zone quality over 3-5 days
7. ✅ Adjust cooldowns/schedules based on preference

---

**Questions?** Check logs: `logs/scanner_YYYYMMDD.log`

**Version:** 2.0.0 (Multi-Timeframe)
**Date:** 2026-01-09
