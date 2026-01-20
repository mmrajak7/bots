# Momentum Scanner Forward Test

Forward test framework for the HH-HL pattern momentum scanner on index options.

## Folder Structure

```
tests/momentum_forward_test/
├── README.md           # This file
├── forward_test.py     # Main forward test script
├── download_data.py    # Download historical data (run when token is valid)
├── data/               # Downloaded historical data
│   ├── spot_candles.json
│   └── option_candles.json
└── results/            # Test results
    ├── forward_test_YYYYMMDD_HHMMSS.json
    ├── trades_YYYYMMDD_HHMMSS.csv
    └── REPORT_YYYYMMDD_HHMMSS.md
```

## Usage

### Step 1: Download Historical Data (When Token is Valid)

First, ensure you have a valid Kite token, then download the data:

```bash
cd Bouncer

# Download data for Jan 1-14, 2026
python tests/momentum_forward_test/download_data.py --start 2026-01-01 --end 2026-01-14
```

This downloads:
- 15M spot candles for NIFTY, BANKNIFTY, SENSEX
- 15M option candles for 2nd and 3rd OTM CE/PE strikes

### Step 2: Run Forward Test

**Offline Mode (uses saved data):**
```bash
python tests/momentum_forward_test/forward_test.py --offline
```

**Live Mode (requires valid token):**
```bash
python tests/momentum_forward_test/forward_test.py --start 2026-01-01 --end 2026-01-14
```

## Test Methodology

1. **Pattern Detection**: 3 consecutive green candles with:
   - Higher Highs: c2.high > c1.high, c3.high > c2.high
   - Higher Lows: c2.low > c1.low, c3.low > c2.low

2. **Entry**: At close of the 3rd candle

3. **Stop Loss**:
   - Initial: Low of the 1st candle in pattern
   - Trailing: Updated to previous candle's low when it rises

4. **Exit**: When candle low breaches SL, or at test period end

5. **Instruments**:
   - 2nd and 3rd OTM CE/PE options
   - Monthly expiries only
   - NIFTY, BANKNIFTY, SENSEX

## Output

### Console
- Signal detection summary
- Trade-by-trade list
- Win rate and P&L analysis

### Files
- `results/forward_test_*.json` - Full test results
- `results/trades_*.csv` - Trade list for Excel/analysis
- `results/REPORT_*.md` - Markdown report

## Token Requirements

The Kite token expires daily at 6 AM IST. To run live tests or download data:

1. Check token validity:
   ```bash
   cat ../data/kite_access_token.json
   ```

2. If expired, refresh via SNAIL auth:
   ```bash
   cd ../SNAIL && python scripts/kite_auth.py
   ```

## Example Output

```
======================================================================
FORWARD TEST RESULTS
======================================================================

Test Period: 2026-01-01 to 2026-01-14
Mode: OFFLINE

SIGNAL DETECTION
----------------
Total Signals:     12
By Index:          {'NIFTY': 5, 'BANKNIFTY': 4, 'SENSEX': 3}
By Type:           {'CE': 7, 'PE': 5}

TRADE PERFORMANCE
-----------------
Total Trades:      12
Closed Trades:     12
Winners:           7
Losers:            5
Win Rate:          58.3%

P&L ANALYSIS
------------
Total P&L:         +42.50 points
Avg Win:           +15.20 points
Avg Loss:          -10.50 points
Risk/Reward:       1:1.45
```
