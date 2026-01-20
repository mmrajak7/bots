# Iron Fly Trade Analysis - 19 December 2025

## Executive Summary

| Trade | ATM Strike | Entry | Exit | Days Held | Net P&L | ROI |
|-------|------------|-------|------|-----------|---------|-----|
| Iron Fly #1 | 25950 | 17-Dec 09:31 | 19-Dec 15:03 | 2 | +Rs.2,889 | +2.6% |
| Iron Fly #2 | 26000 | 15-Dec 09:30 | 19-Dec 15:24 | 4 | +Rs.5,211 | +5.0% |
| **TOTAL** | | | | **3 avg** | **+Rs.8,100** | **+3.8%** |

---

## Iron Fly #1 (25950 ATM)

### Structure
- **Type:** Iron Fly (Short ATM Straddle + Long OTM Wings)
- **ATM Strike:** 25950 (Straddle - SOLD)
- **Lower Wing:** 25700 PE (BOUGHT)
- **Upper Wing:** 26200 CE (BOUGHT)
- **Wing Width:** 250 points
- **Lot Size:** 75
- **Expiry:** 23-Dec-2025

### Timing
- **Entry:** 2025-12-17 09:31
- **Exit:** 2025-12-19 15:03
- **Days Held:** 2

### Leg-by-Leg P&L

| Leg | Entry | Exit | Qty | P&L |
|-----|-------|------|-----|-----|
| SHORT 25950 CE | 116.20 | 90.50 | 75 | +1,927.50 |
| SHORT 25950 PE | 131.80 | 64.45 | 75 | +5,051.25 |
| LONG 25700 PE | 44.30 | 10.85 | 75 | -2,508.75 |
| LONG 26200 CE | 32.10 | 12.35 | 75 | -1,481.25 |
| **Straddle P&L** | | | | **+6,978.75** |
| **Wings P&L** | | | | **-3,990.00** |
| **GROSS P&L** | | | | **+2,988.75** |
| Less: Charges | | | | -99.63 |
| **NET P&L** | | | | **+2,889.12** |

### Entry Premiums
- Straddle Credit (sold): Rs.18,600.00
- Wing Debit (bought): Rs.5,730.00
- Net Entry Premium: Rs.12,870.00

### Performance Metrics
| Metric | Value |
|--------|-------|
| Margin Deployed | Rs.1,10,212.25 |
| Max Profit Possible | Rs.12,828.75 |
| Max Loss Possible | Rs.5,921.25 |
| Gross P&L | +Rs.2,988.75 |
| Net P&L | +Rs.2,889.12 |
| Profit Captured | 23.3% of max |
| ROI on Margin | +2.62% |
| Annualized ROI | +478.4% |

---

## Iron Fly #2 (26000 ATM)

### Structure
- **Type:** Iron Fly (Short ATM Straddle + Long OTM Wings)
- **ATM Strike:** 26000 (Straddle - SOLD)
- **Lower Wing:** 25700 PE (BOUGHT)
- **Upper Wing:** 26300 CE (BOUGHT)
- **Wing Width:** 300 points
- **Lot Size:** 75
- **Expiry:** 23-Dec-2025

### Timing
- **Entry:** 2025-12-15 ~09:30 (Monday)
- **Exit:** 2025-12-19 15:24 (Thursday)
- **Days Held:** 4

### Leg-by-Leg P&L

| Leg | Entry | Exit | Qty | P&L |
|-----|-------|------|-----|-----|
| SHORT 26000 CE | 137.35 | 65.00 | 75 | +5,426.25 |
| SHORT 26000 PE | 169.15 | 87.55 | 75 | +6,120.00 |
| LONG 25700 PE | 60.75 | 11.65 | 75 | -3,682.50 |
| LONG 26300 CE | 38.90 | 5.00 | 75 | -2,542.50 |
| **Straddle P&L** | | | | **+11,546.25** |
| **Wings P&L** | | | | **-6,225.00** |
| **GROSS P&L** | | | | **+5,321.25** |
| Less: Charges | | | | -110.00 |
| **NET P&L** | | | | **+5,211.25** |

### Entry Premiums
- Straddle Credit (sold): Rs.22,987.50
- Wing Debit (bought): Rs.7,473.75
- Net Entry Premium: Rs.15,513.75

### Performance Metrics
| Metric | Value |
|--------|-------|
| Margin Deployed (est) | Rs.1,05,000.00 |
| Max Profit Possible | Rs.15,513.75 |
| Max Loss Possible | Rs.6,986.25 |
| Gross P&L | +Rs.5,321.25 |
| Net P&L | +Rs.5,211.25 |
| Profit Captured | 34.3% of max |
| ROI on Margin | +4.96% |
| Annualized ROI | +452.9% |

---

## Combined Analysis

### Capital & Returns
| Metric | Value |
|--------|-------|
| Total Margin Deployed | Rs.2,15,212.25 |
| Total Net P&L | +Rs.8,100.37 |
| Combined ROI | +3.76% |
| Average Days Held | 3 days |

### P&L Breakdown
| Component | Iron Fly #1 | Iron Fly #2 | Total |
|-----------|-------------|-------------|-------|
| Straddle Gains | +6,978.75 | +11,546.25 | +18,525.00 |
| Wing Costs | -3,990.00 | -6,225.00 | -10,215.00 |
| Charges | -99.63 | -110.00 | -209.63 |
| **Net P&L** | **+2,889.12** | **+5,211.25** | **+8,100.37** |

### Key Observations
1. **Both trades profitable** - 100% win rate for this session
2. **Theta decay captured** - Straddle legs generated Rs.18,525 in premium decay
3. **Wing protection cost** - Wings eroded Rs.10,215 (55% of straddle gains)
4. **Exit timing** - Both exited before expiry (4 days early) via manual exit
5. **Iron Fly #2 outperformed** - Higher profit capture (34% vs 23%) due to longer holding period
6. **Margin efficiency** - ~3.8% return on Rs.2.15L capital in 3 days average

### Data Sources
- Iron Fly #1: Entry/exit from SNAIL database
- Iron Fly #2: Entry from Kite historical API (Dec 15, 09:30), exit from Zerodha trades

---

*Generated: 2025-12-19*
