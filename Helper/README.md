# Helper Scripts

Options data fetching and trade analysis utilities for NSE/BSE markets.

## Prerequisites

1. **Python 3.8+**
2. **Dependencies:** Install from SNAIL project
   ```bash
   cd ../SNAIL
   pip install -r requirements.txt
   ```
3. **Environment Variables:** Create `.env` file in SNAIL folder with Kite credentials:
   ```
   ZERODHA_API_KEY=your_api_key
   ZERODHA_API_SECRET=your_api_secret
   ZERODHA_USER_ID=your_user_id
   ZERODHA_PASSWORD=your_password
   ZERODHA_TOTP_SECRET=your_totp_secret
   ```

---

## 1. Options Data Fetcher

**Script:** `kite_nse_options.py`

Fetches all NSE stock options and index options (NIFTY, SENSEX) from Kite API.

### Usage

```bash
cd Helper
python kite_nse_options.py
```

### Output Files

| File | Description |
|------|-------------|
| `nse_stocks_options.csv` | All stock options (one row per option contract) |
| `nse_stocks_options_summary.csv` | Summary per stock (option counts, expiry range) |
| `index_options.csv` | NIFTY and SENSEX options with DTE |

### Sample Output

```
Fetching instruments from Kite API...
Total instruments fetched: 185432

============================================================
STOCK OPTIONS
============================================================
NSE Stocks found: 2891
Stocks with options: 185
Total stock options: 41939
Writing data to nse_stocks_options.csv...
Writing summary to nse_stocks_options_summary.csv...

============================================================
INDEX OPTIONS
============================================================
Index options found: 8524
  NIFTY: 7892 options (3946 CE, 3946 PE), 12 expiries
  SENSEX: 632 options (316 CE, 316 PE), 4 expiries
Writing index options to index_options.csv...

============================================================
SUMMARY
============================================================
Stock options: 41939 (from 185 stocks)
Index options: 8524
Total: 50463

Output files:
  - nse_stocks_options.csv (detailed stock options)
  - nse_stocks_options_summary.csv (stock summary)
  - index_options.csv (NIFTY/SENSEX options)

Done!
```

---

## 2. Butterfly Trade Analyzer

**Script:** `butterfly_analyzer.py`

Analyzes long butterfly trades with bid-ask based pricing and liquidity filters.

### Usage

```bash
# Basic: Stock/Index name + Direction (BUY/SELL)
python butterfly_analyzer.py RELIANCE BUY
python butterfly_analyzer.py NIFTY SELL
python butterfly_analyzer.py INFY BUY

# With options
python butterfly_analyzer.py NIFTY BUY --multiplier 1.2 --lots 2
```

### Arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `underlying` | Yes | Stock or index name (RELIANCE, NIFTY, INFY, SENSEX) |
| `direction` | Yes | BUY (bullish call butterfly) or SELL (bearish put butterfly) |
| `--multiplier`, `-m` | No | Wing distance multiplier (default: 1.0) |
| `--lots`, `-l` | No | Number of lots to trade (default: 1) |

### Direction Mapping

| Direction | Butterfly Type | View |
|-----------|---------------|------|
| BUY | Long Call Butterfly | Bullish (expect price to rise to ATM) |
| SELL | Long Put Butterfly | Bearish (expect price to fall to ATM) |

### Filters Applied

| Filter | Threshold | Action |
|--------|-----------|--------|
| Bid-Ask Spread | < Rs 1.50/leg | FAIL if exceeded |
| Open Interest (ATM) | > 5,000 contracts | WARN if low |
| Daily Volume | > 50,000 | WARN if low |
| DTE (Stock) | >= 20 days | FAIL if below |
| DTE (Index) | >= 6 days | FAIL if below |

### Sample Output

```
Analyzing NIFTY BUY butterfly...
Wing multiplier: 1.0x, Lots: 1

======================================================================
BUTTERFLY TRADE ANALYSIS: NIFTY
======================================================================

Underlying:      NIFTY (INDEX)
Current Price:   Rs23,850.45
Direction:       BUY (Call Butterfly)
Expiry:          2025-01-02 (4 DTE)

--- STRIKES ---
ITM Strike:      23700
ATM Strike:      23850 (sell 2x)
OTM Strike:      24000
Wing Distance:   150

--- LEGS (using Bid-Ask) ---
Leg        Symbol                    Bid        Ask     Spread           OI
-----------------------------------------------------------------------------
ITM        NIFTY25JAN23700CE       185.50     187.25       1.75      125,432
ATM        NIFTY25JAN23850CE        95.20      96.50       1.30      892,156
OTM        NIFTY25JAN24000CE        42.30      43.75       1.45      654,231

--- PRICING (per lot of 75) ---
ITM Buy Cost:    Rs14,043.75 (ask)
ATM Sell Credit: Rs14,280.00 (bid x 2)
OTM Buy Cost:    Rs3,281.25 (ask)
Net Debit:       Rs3,045.00

--- P&L METRICS (per lot) ---
Max Profit:      Rs8,205.00 (at ATM strike)
Max Loss:        Rs3,045.00 (beyond wings)
Risk:Reward:     0.37:1
Breakeven:       Rs23,740.60 - Rs23,959.40

--- TOTAL (1 lot) ---
Total Debit:     Rs3,045.00
Total Max Profit:Rs8,205.00
Total Max Loss:  Rs3,045.00
Capital Required:Rs3,045.00

--- FILTER RESULTS ---
Status: PASSED

Warnings:
  - ITM spread Rs1.75 approaching limit

--- RECOMMENDATION ---
PROCEED WITH CAUTION: ITM spread Rs1.75 approaching limit

======================================================================
```

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success, all filters passed |
| 1 | Filters failed or error occurred |
| 130 | Aborted by user (Ctrl+C) |

---

## Workflow

### Daily Routine

```bash
# Step 1: Refresh options data (run once daily, market hours)
cd Helper
python kite_nse_options.py

# Step 2: Analyze potential trades
python butterfly_analyzer.py NIFTY BUY
python butterfly_analyzer.py RELIANCE SELL --multiplier 1.2
python butterfly_analyzer.py TCS BUY --lots 2
```

### Quick Reference

```bash
# Index butterflies (min 6 DTE)
python butterfly_analyzer.py NIFTY BUY
python butterfly_analyzer.py SENSEX SELL

# Stock butterflies (min 20 DTE)
python butterfly_analyzer.py RELIANCE BUY
python butterfly_analyzer.py INFY SELL
python butterfly_analyzer.py TCS BUY --multiplier 1.5

# Multiple lots
python butterfly_analyzer.py NIFTY BUY --lots 3
```

---

## Troubleshooting

### "Options CSV not found"
Run the options fetcher first:
```bash
python kite_nse_options.py
```

### "Could not import SNAIL Kite client"
Ensure SNAIL folder exists and dependencies are installed:
```bash
cd ../SNAIL
pip install -r requirements.txt
```

### "Error authenticating with Kite"
1. Check `.env` file in SNAIL folder has correct credentials
2. Ensure market hours (9:15 AM - 3:30 PM IST)
3. Verify TOTP secret is correct

### "No options found for XYZ"
1. Verify the stock has F&O options (not all stocks do)
2. Check spelling matches exactly (case-insensitive)
3. Refresh the CSV: `python kite_nse_options.py`

---

## Configuration

Edit thresholds in `butterfly_analyzer.py`:

```python
FILTERS = {
    'bid_ask_spread_max': 1.50,  # Max Rs per leg
    'oi_atm_min': 5000,          # Min OI for liquidity
    'volume_min': 50000,         # Min daily volume
    'dte_min_stock': 20,         # Min DTE for stocks
    'dte_min_index': 6,          # Min DTE for indices
}
```

---

## Future Enhancements

- [ ] Debit spreads (Bull Call / Bear Put)
- [ ] Iron condor analysis
- [ ] Historical backtest integration
- [x] Telegram alerts for opportunities (scanner.py - COMPLETED)
