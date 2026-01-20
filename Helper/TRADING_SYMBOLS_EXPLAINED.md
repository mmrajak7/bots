# Trading Symbols - Where They Come From

Complete explanation of how the scanner gets trading symbols.

---

## 🎯 QUICK ANSWER

**Trading symbols come from Kite API** via `kite.instruments(exchange)` call.

**Source:** Zerodha/Kite Connect API
**Method:** `kite.instruments('NFO')` and `kite.instruments('BFO')`
**Cached:** Daily (refreshed at first run each day)

---

## 📊 COMPLETE FLOW

### Step 1: Fetch from Kite API (Lines 150-174)

```python
def fetch_instruments(kite: KiteConnect) -> Dict:
    # Check cache first (if already fetched today)
    cached = load_instruments()
    if cached:
        return cached

    logger.info("Downloading instruments...")
    instruments = {}

    # Fetch from NFO (NIFTY, BANKNIFTY) and BFO (SENSEX)
    for exchange in ['NFO', 'BFO']:
        for inst in kite.instruments(exchange):  # ← API CALL HERE
            # Filter: Only options (CE/PE)
            if inst['instrument_type'] not in ('CE', 'PE'):
                continue

            # Filter: Only our indices
            if inst['name'] not in INDICES:  # BANKNIFTY, NIFTY, SENSEX
                continue

            # Store with key: (index, strike, type, expiry)
            key = (inst['name'], inst['strike'], inst['instrument_type'], inst['expiry'])
            instruments[key] = {
                'symbol': inst['tradingsymbol'],  # ← TRADING SYMBOL HERE
                'token': inst['instrument_token'],
                'exchange': inst['exchange']
            }

    # Cache to disk (valid for today)
    save_instruments(instruments)
    logger.info(f"Cached {len(instruments)} instruments")
    return instruments
```

---

## 🔍 WHAT KITE API RETURNS

### Raw API Response (Example)

When you call `kite.instruments('NFO')`, Kite returns a list of dictionaries:

```python
[
    {
        'instrument_token': 12639746,
        'exchange_token': '49374',
        'tradingsymbol': 'NIFTY26JAN25900PE',  # ← THIS IS WHAT WE NEED
        'name': 'NIFTY',
        'last_price': 0.0,
        'expiry': datetime.date(2026, 1, 27),
        'strike': 25900.0,
        'tick_size': 0.05,
        'lot_size': 25,
        'instrument_type': 'PE',
        'segment': 'NFO-OPT',
        'exchange': 'NFO'
    },
    {
        'instrument_token': 12345678,
        'tradingsymbol': 'BANKNIFTY26JAN60000CE',
        'name': 'BANKNIFTY',
        'expiry': datetime.date(2026, 1, 27),
        'strike': 60000.0,
        'instrument_type': 'CE',
        'exchange': 'NFO'
    },
    {
        'instrument_token': 87654321,
        'tradingsymbol': 'SENSEX26JAN84000PE',
        'name': 'SENSEX',
        'expiry': datetime.date(2026, 1, 29),
        'strike': 84000.0,
        'instrument_type': 'PE',
        'exchange': 'BFO'
    },
    # ... thousands more instruments
]
```

**Key Fields We Use:**
- `tradingsymbol` → Stored as `symbol` in our cache
- `instrument_token` → Used for historical data API calls
- `exchange` → Used for quote API calls (NFO or BFO)
- `name` → Index name (NIFTY, BANKNIFTY, SENSEX)
- `strike` → Strike price
- `instrument_type` → CE or PE
- `expiry` → Expiry date

---

## 💾 HOW INSTRUMENTS ARE CACHED

### Cache Structure (instruments.pkl)

After fetching from API, instruments are stored as a dictionary:

```python
{
    ('NIFTY', 25900.0, 'PE', datetime.date(2026, 1, 27)): {
        'symbol': 'NIFTY26JAN25900PE',
        'token': 12639746,
        'exchange': 'NFO'
    },
    ('NIFTY', 25900.0, 'CE', datetime.date(2026, 1, 27)): {
        'symbol': 'NIFTY26JAN25900CE',
        'token': 12639747,
        'exchange': 'NFO'
    },
    ('BANKNIFTY', 60000.0, 'CE', datetime.date(2026, 1, 27)): {
        'symbol': 'BANKNIFTY26JAN60000CE',
        'token': 12345678,
        'exchange': 'NFO'
    },
    ('SENSEX', 84000.0, 'PE', datetime.date(2026, 1, 29)): {
        'symbol': 'SENSEX26JAN84000PE',
        'token': 87654321,
        'exchange': 'BFO'
    },
    # ... all other strikes and expiries
}
```

**Key:** `(index_name, strike, option_type, expiry_date)`
**Value:** `{symbol, token, exchange}`

**Cache File:** `helper/data/cache/instruments.pkl`
**Cache Duration:** Until midnight (refreshes daily)

---

## 🔎 HOW SYMBOLS ARE LOOKED UP

### During Full Scan (Lines 423-438)

```python
for index, ltp in index_ltps.items():  # For each index (NIFTY, BANKNIFTY, SENSEX)

    # Step 1: Calculate ATM strike
    atm = calculate_atm(ltp, index)
    # Example: NIFTY LTP 25876 → ATM 25900

    # Step 2: Find monthly expiry
    expiry = get_monthly_expiry(index, instruments)
    # Example: 27-Jan-2026

    # Step 3: Look up both CE and PE
    for opt_type in ['CE', 'PE']:

        # Build lookup key
        key = (index, atm, opt_type, expiry)
        # Example: ('NIFTY', 25900.0, 'PE', date(2026, 1, 27))

        # Look up in instruments cache
        if key not in instruments:
            continue  # Skip if not found

        # Get instrument details
        inst = instruments[key]
        symbol = inst['symbol']  # ← TRADING SYMBOL RETRIEVED
        # Example: 'NIFTY26JAN25900PE'

        # Now use this symbol for API calls:
        quote = kite.quote(f"{inst['exchange']}:{symbol}")
        # Example: kite.quote("NFO:NIFTY26JAN25900PE")

        data = get_historical_data(kite, inst['token'])
        # Uses token for historical data
```

---

## 📅 TRADING SYMBOL FORMAT

Kite uses a standard format for option symbols:

### Format Breakdown

```
NIFTY26JAN25900PE
│    ││││ │││││││
│    ││││ │││││└─ Option Type (CE/PE)
│    ││││ │││└└─ Strike Price (25900)
│    ││││ └└└─ Strike Price continued
│    │└└└─ Expiry Month (JAN)
│    └─ Expiry Year (26 = 2026)
└─ Index Name (NIFTY)

BANKNIFTY26JAN60000CE
│        ││││ │││││││
│        ││││ │││││└─ Option Type (CE)
│        ││││ │││└└─ Strike Price (60000)
│        ││││ └└└─ Strike Price continued
│        │└└└─ Expiry Month (JAN)
│        └─ Expiry Year (26)
└─ Index Name (BANKNIFTY)

SENSEX26JAN84000PE
│     ││││ │││││││
│     ││││ │││││└─ Option Type (PE)
│     ││││ │││└└─ Strike Price (84000)
│     ││││ └└└─ Strike Price continued
│     │└└└─ Expiry Month (JAN)
│     └─ Expiry Year (26)
└─ Index Name (SENSEX)
```

**Why We Don't Hardcode:**
- Symbol format can change (e.g., 25JAN → 26JAN)
- New expiries added daily
- Strike prices dynamic based on underlying
- Lot sizes change

**Always fetch from Kite API to ensure accuracy!**

---

## 🔄 COMPLETE DATA FLOW

```
┌──────────────────────────────────────────────────────────────────┐
│  SCANNER START                                                   │
└────────────────┬─────────────────────────────────────────────────┘
                 │
                 ▼
         ┌───────────────┐
         │ Check Cache   │
         │ Valid today?  │
         └───┬───────┬───┘
             │       │
            YES     NO
             │       │
             │       ▼
             │  ┌─────────────────────────────┐
             │  │ FETCH FROM KITE API         │
             │  ├─────────────────────────────┤
             │  │ kite.instruments('NFO')     │ ← API CALL 1
             │  │ kite.instruments('BFO')     │ ← API CALL 2
             │  │                             │
             │  │ Returns ~5000 instruments   │
             │  │ with tradingsymbols         │
             │  └──────────┬──────────────────┘
             │             │
             │             ▼
             │  ┌─────────────────────────────┐
             │  │ FILTER & CACHE              │
             │  ├─────────────────────────────┤
             │  │ Keep only CE/PE             │
             │  │ Keep only our indices       │
             │  │ Store as dict:              │
             │  │   key = (name,strike,type,  │
             │  │          expiry)             │
             │  │   val = {symbol,token,exch} │
             │  │                             │
             │  │ Save to instruments.pkl     │
             │  └──────────┬──────────────────┘
             │             │
             └─────────────┘
                 │
                 ▼
         ┌───────────────┐
         │ GET INDEX LTP │
         └───────┬───────┘
                 │
                 ▼
         ┌───────────────┐
         │ CALC ATM      │
         │ NIFTY: 25876  │
         │ → ATM: 25900  │
         └───────┬───────┘
                 │
                 ▼
         ┌───────────────────────┐
         │ LOOKUP SYMBOL         │
         ├───────────────────────┤
         │ key = ('NIFTY',       │
         │        25900,          │
         │        'PE',           │
         │        date(2026,1,27))│
         │                       │
         │ instruments[key]      │
         │ = {                   │
         │   'symbol':           │
         │   'NIFTY26JAN25900PE',│ ← TRADING SYMBOL
         │   'token': 12639746,  │
         │   'exchange': 'NFO'   │
         │ }                     │
         └───────┬───────────────┘
                 │
                 ▼
         ┌───────────────────────┐
         │ USE SYMBOL FOR:       │
         ├───────────────────────┤
         │ 1. Get quote (LTP)    │
         │    kite.quote(        │
         │    "NFO:NIFTY26JAN... │
         │                       │
         │ 2. Get historical     │
         │    kite.historical(   │
         │    token=12639746...  │
         │                       │
         │ 3. Store in zones DB  │
         │ 4. Display in alerts  │
         └───────────────────────┘
```

---

## ⏱️ WHEN INSTRUMENTS ARE FETCHED

### Daily Refresh (Lines 137-144)

```python
def load_instruments() -> Optional[Dict]:
    if not INSTRUMENTS_CACHE.exists():
        return None  # No cache, need to fetch

    cache_time = datetime.fromtimestamp(INSTRUMENTS_CACHE.stat().st_mtime)
    if cache_time.date() < datetime.now().date():
        return None  # Cache is from yesterday, need to refresh

    # Cache is from today, use it
    with open(INSTRUMENTS_CACHE, 'rb') as f:
        return pickle.load(f)
```

**Refresh Logic:**
- First run of the day → Fetch from API (~5 seconds)
- Subsequent runs same day → Use cache (~instant)
- Next day → Auto-refresh

**Why Daily?**
- New expiries added weekly
- New strikes added based on underlying movement
- Ensures always have latest symbols

---

## 🔍 EXAMPLE: COMPLETE SYMBOL RESOLUTION

### Scenario: Scanner runs at 10:30 AM

**Step 1: Get NIFTY LTP**
```python
quote = kite.quote('NSE:NIFTY 50')
ltp = quote['NSE:NIFTY 50']['last_price']
# Result: 25876.85
```

**Step 2: Calculate ATM**
```python
atm = calculate_atm(25876.85, 'NIFTY')
# Round to nearest 100: 25900
```

**Step 3: Find Monthly Expiry**
```python
expiry = get_monthly_expiry('NIFTY', instruments)
# Result: datetime.date(2026, 1, 27)  # 27-Jan-2026
```

**Step 4: Look Up PE Symbol**
```python
key = ('NIFTY', 25900.0, 'PE', datetime.date(2026, 1, 27))
inst = instruments[key]

print(inst)
# Output:
# {
#   'symbol': 'NIFTY26JAN25900PE',
#   'token': 12639746,
#   'exchange': 'NFO'
# }
```

**Step 5: Use Symbol for API Calls**
```python
# Get current LTP
quote = kite.quote('NFO:NIFTY26JAN25900PE')
opt_ltp = quote['NFO:NIFTY26JAN25900PE']['last_price']
# Result: 171.25

# Get historical data (uses token, not symbol)
data = kite.historical_data(
    instrument_token=12639746,
    from_date=datetime.now() - timedelta(days=30),
    to_date=datetime.now(),
    interval='15minute'
)
# Returns ~2000 candles
```

---

## 📂 WHERE SYMBOLS ARE STORED

### Runtime Storage

```
helper/data/cache/instruments.pkl
```

**Contents:** Dictionary with ~5000+ option instruments

**Size:** ~500 KB

**Structure:**
```python
{
    # NIFTY options
    ('NIFTY', 25800.0, 'CE', date(2026, 1, 27)): {...},
    ('NIFTY', 25800.0, 'PE', date(2026, 1, 27)): {...},
    ('NIFTY', 25850.0, 'CE', date(2026, 1, 27)): {...},
    ...

    # BANKNIFTY options
    ('BANKNIFTY', 59000.0, 'CE', date(2026, 1, 27)): {...},
    ('BANKNIFTY', 59000.0, 'PE', date(2026, 1, 27)): {...},
    ...

    # SENSEX options
    ('SENSEX', 83000.0, 'CE', date(2026, 1, 29)): {...},
    ('SENSEX', 83000.0, 'PE', date(2026, 1, 29)): {...},
    ...
}
```

---

## 🔧 DEBUGGING SYMBOL RESOLUTION

### Check Cached Instruments

```bash
cd helper

# List cache file
ls -lh data/cache/instruments.pkl

# Check cache age
stat data/cache/instruments.pkl

# Dump instrument keys (Python)
python3 -c "
import pickle
with open('data/cache/instruments.pkl', 'rb') as f:
    inst = pickle.load(f)

# Show first 10 NIFTY options
nifty_opts = [k for k in inst.keys() if k[0] == 'NIFTY'][:10]
for key in nifty_opts:
    print(f'{key} → {inst[key][\"symbol\"]}')"
```

### Manually Fetch Instruments

```bash
python3 -c "
from kiteconnect import KiteConnect
import json

# Load token
with open('../data/kite_access_token.json') as f:
    token = json.load(f)

kite = KiteConnect(api_key=token['api_key'])
kite.set_access_token(token['access_token'])

# Fetch instruments
instruments = kite.instruments('NFO')

# Show NIFTY 25900 PE for Jan expiry
for inst in instruments:
    if (inst['name'] == 'NIFTY' and
        inst['strike'] == 25900.0 and
        inst['instrument_type'] == 'PE' and
        inst['expiry'].month == 1):
        print(inst['tradingsymbol'], inst['expiry'])"
```

### Test Symbol Lookup

```bash
cd helper
python3 -c "
import pickle
from datetime import date

with open('data/cache/instruments.pkl', 'rb') as f:
    instruments = pickle.load(f)

# Test lookup
key = ('NIFTY', 25900.0, 'PE', date(2026, 1, 27))
if key in instruments:
    print('Found:', instruments[key]['symbol'])
else:
    print('Not found. Available expiries:')
    nifty_pe_25900 = [k for k in instruments.keys()
                      if k[0] == 'NIFTY' and k[1] == 25900.0 and k[2] == 'PE']
    for k in nifty_pe_25900:
        print(f'  {k[3]} → {instruments[k][\"symbol\"]}')"
```

---

## 🎯 SUMMARY

### Where Trading Symbols Come From

```
SOURCE: Kite Connect API
METHOD: kite.instruments('NFO') and kite.instruments('BFO')
FORMAT: NIFTY26JAN25900PE (from API response 'tradingsymbol' field)
CACHED: Daily in instruments.pkl (~500KB, ~5000 instruments)
LOOKUP: By (index, strike, type, expiry) tuple
USED FOR: Quote API, historical API, alerts
```

### Why This Approach?

✅ **Always Accurate:** Symbols come directly from exchange
✅ **No Hardcoding:** Format changes handled automatically
✅ **Daily Refresh:** New expiries/strikes auto-detected
✅ **Fast Lookup:** O(1) dictionary lookup by key
✅ **Efficient:** Cached, only fetched once per day

**Never construct trading symbols manually!** Always use Kite API as source of truth.

---

**Complete transparency into symbol resolution!** 🎯
