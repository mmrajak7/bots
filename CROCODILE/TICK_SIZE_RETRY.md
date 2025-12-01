# Tick Size Error Retry Mechanism

## Overview

Automatic retry mechanism that detects tick size errors from Zerodha, corrects the price, and retries the order placement.

## How It Works

### 1. Primary Protection (Proactive)
The system first attempts to use the correct tick size based on NSE rules:
- Price < Rs.1000 → tick size = 0.05
- Price ≥ Rs.1000 → tick size = 0.10

### 2. Fallback Protection (Reactive)
If Zerodha still rejects the order with a tick size error:

```
Step 1: Catch the error
   ↓
Step 2: Parse Zerodha's error message to extract required tick size
   Example: "Tick size for this script is 0.10. Kindly enter..."
   Extracted: 0.10
   ↓
Step 3: Re-round the price with correct tick size
   Original: Rs.1945.05 → Corrected: Rs.1945.10
   ↓
Step 4: Retry order placement (one time only)
   ↓
Step 5: Success or fail with original error
```

## Benefits

1. **Self-Healing**: Learns from Zerodha's actual requirements
2. **Safety Net**: Handles edge cases and special tick size rules
3. **Future-Proof**: Works even if NSE changes tick size rules
4. **No Manual Intervention**: Automatically corrects and retries

## Implementation Details

**Location:** `src/services/entry_manager.py:533-592`

**Key Features:**
- Only retries LIMIT orders (not MARKET orders)
- Only retries once (prevents infinite loops)
- Logs the correction for audit trail
- Regex pattern: `r'tick size.*?is\s+(0\.\d+)'`

## Test Results

Error message parsing success rate: **75%** (3 out of 4 common formats)

Supported error formats:
- ✅ "Tick size for this script is 0.10. Kindly enter price..."
- ✅ "Tick size for this script is 0.05. Kindly enter price..."
- ✅ "The tick size for this symbol is 0.10. Please adjust..."
- ❌ "Invalid tick size. Expected 0.05" (alternative format)

## Example Log Output

```
2025-12-01 10:30:15 | WARNING | Tick size error for SBILIFE.
                                Zerodha requires 0.1, retrying with corrected price...
2025-12-01 10:30:15 | INFO    | Corrected price: Rs.1945.05 → Rs.1945.10 (tick size: 0.1)
2025-12-01 10:30:16 | INFO    | Order placed successfully: 251201190242915
```

## Usage

No configuration needed - the retry mechanism is automatic and transparent.

## Limitations

- Only 1 retry attempt (prevents excessive API calls)
- Only works for LIMIT orders (MARKET orders don't have price validation)
- Relies on Zerodha's error message format remaining consistent
