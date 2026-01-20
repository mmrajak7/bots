# Scanner - Complete Flow Explanation

---

## 📋 CONFIGURATION (All in scanner.py)

### Line 95-105: Main Config Variables

```python
# Scanning params
LOOKBACK_DAYS = 30              ← Change to 45, 60, etc for more history
MIN_BOUNCES = 5                 ← Minimum bounces to consider zone valid
MIN_SCORE = 50                  ← Minimum score to track/alert
BUFFER_PCT = 2.0                ← Entry buffer (2% below zone)

# Proximity alert params
PROXIMITY_PCT = 2.0             ← Alert when price within 2% of zone
ALERT_COOLDOWN_HOURS = 1        ← Don't re-alert same zone for N hours

# Full scan minutes
FULL_SCAN_MINUTES = [16, 31, 46]  ← Add/remove minutes for full scan
```

### Line 82-86: Telegram Config (Auto-loaded)

```python
with open(BOUNCER_CONFIG) as f:
    BOUNCER_CFG = json.load(f)

TELEGRAM_BOT_TOKEN = BOUNCER_CFG['telegram']['bot_token']
TELEGRAM_CHAT_ID = BOUNCER_CFG['telegram']['chat_id']
```

**Reads from:** `BOTS/Bouncer/config/config.json`

```json
{
  "telegram": {
    "bot_token": "your_bot_token_here",
    "chat_id": "your_chat_id_here"
  }
}
```

**No hardcoding needed!** Token is automatically loaded from Bouncer config.

---

## 🔄 COMPLETE FLOW

### **Entry Point (Line 541-557)**

```
┌─────────────────────────────────────────────┐
│  python scanner.py                          │
└───────────────┬─────────────────────────────┘
                │
                ▼
        Check market hours
        (9:15 AM - 3:30 PM?)
                │
        ┌───────┴────────┐
        │                │
       YES              NO → Exit
        │
        ▼
   Load Kite token
   Fetch instruments (cached daily)
        │
        ▼
   Check current minute
        │
   ┌────┴─────┐
   │          │
  :16,:31,:46  Other mins
   │          │
   ▼          ▼
FULL SCAN  QUICK CHECK
```

---

## 📊 FULL SCAN FLOW (Every 15 mins at :16, :31, :46)

### Step-by-Step (Line 404-480)

```
1. START FULL SCAN
   └─> Log: "============ FULL SCAN: HH:MM:SS ============"

2. GET INDEX LTPs
   ├─> BANKNIFTY: Quote from 'NSE:NIFTY BANK'
   ├─> NIFTY: Quote from 'NSE:NIFTY 50'
   └─> SENSEX: Quote from 'BSE:SENSEX'
   └─> Log: "BANKNIFTY: 59686.50"

3. CALCULATE ATM STRIKES
   ├─> BANKNIFTY: Round to nearest 1000 (59686 → 60000)
   ├─> NIFTY: Round to nearest 100 (25876 → 25900)
   └─> SENSEX: Round to nearest 1000 (84180 → 84000)

4. FIND MONTHLY EXPIRY
   └─> Last Thursday/Friday of current month

5. FOR EACH INDEX × CE/PE (6 options total):
   │
   ├─> GET OPTION LTP
   │   └─> Quote from Kite
   │
   ├─> FETCH HISTORICAL DATA (Line 270-276)
   │   └─> kite.historical_data(
   │           token=option_token,
   │           from_date=NOW - LOOKBACK_DAYS,  ← Uses config!
   │           to_date=NOW,
   │           interval='15minute'
   │       )
   │   └─> Returns ~2000 candles (30 days × 24 candles/day)
   │
   ├─> FIND REVERSAL ZONES (Line 278-337)
   │   │
   │   ├─> Step 1: Find bounce candles
   │   │   └─> For each candle:
   │   │       ├─> Close in upper 60% of range? → Bounce
   │   │       └─> Lower wick > 30%? → Bounce
   │   │
   │   ├─> Step 2: Group bounces into zones
   │   │   └─> Round each bounce low to nearest 10
   │   │       (e.g., 147 → 150, 152 → 150)
   │   │
   │   ├─> Step 3: Merge adjacent levels
   │   │   └─> If levels 150 and 160 both exist → merge
   │   │
   │   └─> Step 4: Filter zones
   │       └─> Keep only if bounces >= MIN_BOUNCES (5)
   │
   ├─> SCORE ZONES (Line 342-357)
   │   └─> Score = bounces×40% + strength×20% + proximity×20%
   │           + freshness×10% + risk_reward×10%
   │   └─> Range: 0-100
   │
   ├─> REMOVE BROKEN ZONES (Line 339-340)
   │   └─> Zone broken if:
   │       ├─> LTP > zone_high × 1.03 (3% above)
   │       └─> LTP < zone_low × 0.97 (3% below)
   │
   ├─> FILTER ZONES (Score >= MIN_SCORE)
   │   └─> Only keep zones with score >= 50
   │
   └─> SAVE TO ZONES DB
       └─> File: data/cache/zones_db.pkl
       └─> Structure:
           {
             'NIFTY26JAN25900PE': {
               'ltp': 171,
               'token': 12345,
               'exchange': 'NFO',
               'type': 'PE',
               'zones': [
                 {'price': 165, 'low': 155, 'high': 174,
                  'bounces': 24, 'score': 62, ...},
                 {'price': 135, 'low': 126, 'high': 145,
                  'bounces': 26, 'score': 58, ...}
               ]
             },
             ...
           }

6. LOG COMPLETION
   └─> "SCAN COMPLETE: Zones DB updated: 3 symbols"
```

---

## ⚡ QUICK CHECK FLOW (Every Minute Except :16, :31, :46)

### Step-by-Step (Line 486-536)

```
1. START QUICK CHECK
   └─> Log: "Quick check: HH:MM:SS"

2. LOAD ZONES DB
   └─> Read: data/cache/zones_db.pkl
   └─> If empty → Exit (wait for full scan)

3. LOAD ALERTS TRACKER
   └─> Read: data/cache/alerts_tracker.pkl
   └─> Clean entries > 2 hours old
   └─> Structure:
       {
         'NIFTY26JAN25900PE_165': datetime(2026, 1, 8, 10, 30),
         'BANKNIFTY26JAN60000CE_505': datetime(2026, 1, 8, 11, 15)
       }

4. FOR EACH SYMBOL IN ZONES DB:
   │
   ├─> FETCH CURRENT LTP
   │   └─> kite.quote(exchange:symbol)
   │
   └─> FOR EACH ZONE:
       │
       ├─> CALCULATE DISTANCE
       │   └─> distance_pct = |ltp - zone_center| / zone_center × 100
       │
       ├─> CHECK IF NEAR ZONE
       │   └─> If distance_pct <= PROXIMITY_PCT (2%):
       │       │
       │       ├─> CHECK COOLDOWN
       │       │   └─> Key = f"{symbol}_{zone_price}"
       │       │   └─> If key in tracker:
       │       │       └─> hours_since = (now - last_alert) / 3600
       │       │       └─> Can alert if hours_since >= 1
       │       │
       │       ├─> IF CAN ALERT:
       │       │   ├─> FORMAT MESSAGE (Line 374-403)
       │       │   │   └─>
       │       │   │       🎯 NIFTY 25900 PE [Score: 62]
       │       │   │       Zone: 155-174 (24 bounces, 68% strength)
       │       │   │       Entry: 151 | Stop: 148 | LTP: 158
       │       │   │
       │       │   │       ⚡ PRICE NEAR ZONE - Ready to enter
       │       │   │
       │       │   ├─> SEND TELEGRAM (Line 360-372)
       │       │   │   └─> POST to https://api.telegram.org/bot{TOKEN}/sendMessage
       │       │   │   └─> Payload: {chat_id, text, parse_mode: 'HTML'}
       │       │   │
       │       │   ├─> MARK ALERTED (Line 233-236)
       │       │   │   └─> tracker[key] = datetime.now()
       │       │   │
       │       │   └─> LOG
       │       │       └─> "ALERT: NIFTY26JAN25900PE @ 158 near zone 165"
       │       │
       │       └─> ELSE:
       │           └─> Skip (cooldown active)

5. SAVE ALERTS TRACKER
   └─> Write: data/cache/alerts_tracker.pkl

6. LOG COMPLETION
   └─> "Quick check: 2 alerts sent" (if any)
```

---

## 🕐 TIMING BREAKDOWN (Entire Day)

```
09:15 → Quick check (DB empty, no alerts)
09:16 → FULL SCAN (builds zones DB, may send alerts)
09:17 → Quick check (zones available)
09:18 → Quick check
09:19 → Quick check
...
09:30 → Quick check
09:31 → FULL SCAN (updates zones)
09:32 → Quick check
...
09:45 → Quick check
09:46 → FULL SCAN (updates zones)
09:47 → Quick check
...
15:29 → Quick check
15:30 → Quick check
15:31 → Exit (market closed)
```

**Full scans per day:** 25 (from 9:16 to 3:16)
**Quick checks per day:** ~350 (every other minute)

---

## 📁 FILE DEPENDENCIES

### Runtime Files

```
helper/
├── scanner.py                     ← Main script
└── data/cache/
    ├── instruments.pkl            ← Cached daily (auto-refresh)
    ├── zones_db.pkl               ← Full scan results
    └── alerts_tracker.pkl         ← Cooldown tracking

logs/
└── scanner_YYYYMMDD.log          ← Daily log (auto-rotated)
```

### Config Files (Must Exist)

```
BOTS/
├── Bouncer/config/config.json    ← Telegram token/chat_id
└── data/kite_access_token.json   ← Kite auth token
```

---

## 🔧 HOW CONFIGURATION IS USED

### 1. LOOKBACK_DAYS (Line 273)

```python
# In get_historical_data():
kite.historical_data(
    instrument_token=token,
    from_date=datetime.now() - timedelta(days=LOOKBACK_DAYS),  ← HERE
    to_date=datetime.now(),
    interval='15minute'
)
```

**Change LOOKBACK_DAYS:**
- 30 days (default) = ~2000 candles
- 45 days = ~3000 candles
- 60 days = ~4000 candles

**Trade-off:**
- More days = more historical data = stronger zones
- More days = longer runtime (~2-3 sec per 10 days)

### 2. TELEGRAM_BOT_TOKEN (Line 362)

```python
# In send_telegram():
url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"  ← HERE
payload = {
    'chat_id': TELEGRAM_CHAT_ID,  ← HERE
    'text': message,
    'parse_mode': 'HTML'
}
```

**Loaded from:** `BOTS/Bouncer/config/config.json`

### 3. FULL_SCAN_MINUTES (Line 119-120)

```python
def is_full_scan_time() -> bool:
    return datetime.now().minute in FULL_SCAN_MINUTES  ← HERE
```

**Used in main():**
```python
if force or is_full_scan_time():  ← Decides full scan vs quick check
    full_scan(kite, instruments)
else:
    quick_check(kite)
```

### 4. MIN_SCORE (Line 458)

```python
# After scoring zones:
zones = [z for z in zones if z['score'] >= MIN_SCORE]  ← HERE
```

**Effect:**
- 50 (default) = balanced (2-5 alerts/scan)
- 60 = stricter (1-2 alerts/scan)
- 40 = looser (5-10 alerts/scan)

### 5. PROXIMITY_PCT (Line 516)

```python
# In quick_check():
distance_pct = abs(ltp - zone_center) / zone_center * 100
if distance_pct <= PROXIMITY_PCT:  ← HERE
    # Send proximity alert
```

**Effect:**
- 2% (default) = alert when very close
- 5% = alert earlier (more alerts)
- 1% = alert only when touching zone

### 6. ALERT_COOLDOWN_HOURS (Line 231)

```python
def can_alert(symbol, zone_price, tracker):
    ...
    hours_since = (datetime.now() - last_alert).seconds / 3600
    return hours_since >= ALERT_COOLDOWN_HOURS  ← HERE
```

**Effect:**
- 1 hour (default) = re-alert after 1 hour
- 2 hours = less frequent re-alerts
- 0.5 hours = more frequent re-alerts

---

## 🎛️ CUSTOMIZATION EXAMPLES

### Example 1: More Historical Data
```python
LOOKBACK_DAYS = 60  # Instead of 30
```
**Result:** Zones based on 60 days of data (stronger zones)

### Example 2: Fewer But Stronger Alerts
```python
MIN_SCORE = 60       # Instead of 50
MIN_BOUNCES = 8      # Instead of 5
```
**Result:** Only alert for very strong zones

### Example 3: Earlier Proximity Alerts
```python
PROXIMITY_PCT = 5.0  # Instead of 2.0
```
**Result:** Alert when price within 5% of zone (earlier warning)

### Example 4: More Frequent Full Scans
```python
FULL_SCAN_MINUTES = [15, 30, 45, 0]  # Every 15 mins at quarter hours
```
**Result:** Zones updated 4 times per hour (instead of 3)

---

## 📊 DATA FLOW DIAGRAM

```
┌─────────────────────────────────────────────────────────────────┐
│                       SCANNER START                              │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │  Check Current Time  │
              └──────────┬───────────┘
                         │
         ┌───────────────┴────────────────┐
         │                                │
         ▼                                ▼
  ┌─────────────┐                 ┌──────────────┐
  │ :16,:31,:46 │                 │  Other mins  │
  └──────┬──────┘                 └──────┬───────┘
         │                                │
         ▼                                ▼
┌─────────────────┐              ┌──────────────────┐
│   FULL SCAN     │              │   QUICK CHECK    │
├─────────────────┤              ├──────────────────┤
│ 1. Get LTPs     │              │ 1. Load zones DB │
│ 2. Calc strikes │              │ 2. Get LTPs      │
│ 3. Fetch 30d    │◄─────────────┤ 3. Check prox    │
│    history      │   Saves to   │ 4. Send alerts   │
│ 4. Find zones   │   zones_db   │ 5. Save tracker  │
│ 5. Score zones  │              └──────────────────┘
│ 6. Save DB      │
└─────────────────┘
         │
         ▼
┌─────────────────────────┐
│   TELEGRAM ALERTS       │
│  (if score >= 50)       │
└─────────────────────────┘
```

---

**Summary:**
- All config in one place (lines 95-105)
- Telegram auto-loaded from Bouncer config
- 30 days configurable via LOOKBACK_DAYS
- Flow automatically switches based on time
- Complete tracking and cooldown management
