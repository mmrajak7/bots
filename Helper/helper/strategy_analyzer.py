#!/usr/bin/env python3
"""Comprehensive options strategy analyzer for ICICIBANK bullish to 1394."""

import json
from pathlib import Path
from kiteconnect import KiteConnect

TOKEN_FILE = Path('.').resolve().parent / 'data' / 'kite_access_token.json'
with open(TOKEN_FILE) as f:
    token_data = json.load(f)

kite = KiteConnect(api_key=token_data['api_key'])
kite.set_access_token(token_data['access_token'])

# Get spot price
spot = kite.ltp(['NSE:ICICIBANK'])
ltp = spot['NSE:ICICIBANK']['last_price']

# Fetch all required options
symbols = [
    'NFO:ICICIBANK26JAN1300CE', 'NFO:ICICIBANK26JAN1310CE',
    'NFO:ICICIBANK26JAN1320CE', 'NFO:ICICIBANK26JAN1330CE',
    'NFO:ICICIBANK26JAN1340CE', 'NFO:ICICIBANK26JAN1350CE',
    'NFO:ICICIBANK26JAN1360CE', 'NFO:ICICIBANK26JAN1370CE',
    'NFO:ICICIBANK26JAN1380CE', 'NFO:ICICIBANK26JAN1390CE',
    'NFO:ICICIBANK26JAN1400CE', 'NFO:ICICIBANK26JAN1410CE',
    'NFO:ICICIBANK26JAN1420CE',
    'NFO:ICICIBANK26JAN1300PE', 'NFO:ICICIBANK26JAN1310PE',
    'NFO:ICICIBANK26JAN1320PE', 'NFO:ICICIBANK26JAN1330PE',
    'NFO:ICICIBANK26JAN1340PE', 'NFO:ICICIBANK26JAN1350PE',
    'NFO:ICICIBANK26JAN1360PE', 'NFO:ICICIBANK26JAN1370PE',
    'NFO:ICICIBANK26JAN1380PE', 'NFO:ICICIBANK26JAN1390PE',
    'NFO:ICICIBANK26JAN1400PE',
]
quotes = kite.quote(symbols)
lot = 700

def gb(sym):
    """Get bid/ask prices."""
    q = quotes.get(sym, {})
    if not q:
        return 0, 0
    bid = q['depth']['buy'][0]['price'] if q['depth']['buy'] else 0
    ask = q['depth']['sell'][0]['price'] if q['depth']['sell'] else 0
    return bid, ask

def get_oi(sym):
    """Get open interest."""
    q = quotes.get(sym, {})
    return q.get('oi', 0) if q else 0

print(f'ICICIBANK Spot: Rs {ltp:.2f}')
print(f'Target: Rs 1394 (+{((1394/ltp)-1)*100:.1f}%)')
print(f'Lot Size: {lot}')
print()

# Calculate days to expiry (Jan 30, 2026)
from datetime import date
expiry = date(2026, 1, 30)
today = date.today()
dte = (expiry - today).days
print(f'Expiry: {expiry} ({dte} DTE)')
print()

print('='*90)
print('STRATEGY COMPARISON - ALL PERSPECTIVES')
print('='*90)

strategies = []

# ============================================================================
# STRATEGY 1: BULL CALL SPREAD (1350/1400)
# ============================================================================
_, c1350_ask = gb('NFO:ICICIBANK26JAN1350CE')
c1400_bid, _ = gb('NFO:ICICIBANK26JAN1400CE')
bcs_cost = c1350_ask - c1400_bid
bcs_max_profit = 50 - bcs_cost  # Spread width - cost
bcs_breakeven = 1350 + bcs_cost

def bcs_pnl(price):
    val = max(0, price-1350) - max(0, price-1400)
    return (val - bcs_cost) * lot

strategies.append({
    'name': 'Bull Call Spread 1350/1400',
    'type': 'DEBIT',
    'cost': bcs_cost * lot,
    'max_profit': bcs_max_profit * lot,
    'max_loss': bcs_cost * lot,
    'breakeven': [bcs_breakeven],
    'pnl_at_target': bcs_pnl(1394),
    'pnl_at_spot': bcs_pnl(ltp),
    'delta': 'Moderate (+0.4 to +0.6)',
    'theta': 'Negative (time decay hurts)',
    'margin': bcs_cost * lot,
    'pnl_func': bcs_pnl,
})

# ============================================================================
# STRATEGY 2: BULL PUT SPREAD (1320/1300) - Credit
# ============================================================================
c1320p_bid, _ = gb('NFO:ICICIBANK26JAN1320PE')
_, c1300p_ask = gb('NFO:ICICIBANK26JAN1300PE')
bps_credit = c1320p_bid - c1300p_ask
bps_max_loss = 20 - bps_credit  # Spread width - credit
bps_breakeven = 1320 - bps_credit

def bps_pnl(price):
    if price >= 1320:
        return bps_credit * lot
    elif price <= 1300:
        return (bps_credit - 20) * lot
    else:
        return (bps_credit - (1320 - price)) * lot

strategies.append({
    'name': 'Bull Put Spread 1320/1300',
    'type': 'CREDIT',
    'cost': -bps_credit * lot,  # Negative = credit received
    'max_profit': bps_credit * lot,
    'max_loss': bps_max_loss * lot,
    'breakeven': [bps_breakeven],
    'pnl_at_target': bps_pnl(1394),
    'pnl_at_spot': bps_pnl(ltp),
    'delta': 'Low (+0.1 to +0.2)',
    'theta': 'Positive (time decay helps)',
    'margin': bps_max_loss * lot,
    'pnl_func': bps_pnl,
})

# ============================================================================
# STRATEGY 3: LONG CALL (1350 CE)
# ============================================================================
_, c1350_ask = gb('NFO:ICICIBANK26JAN1350CE')
lc_cost = c1350_ask
lc_breakeven = 1350 + lc_cost

def lc_pnl(price):
    return (max(0, price - 1350) - lc_cost) * lot

strategies.append({
    'name': 'Long Call 1350 CE',
    'type': 'DEBIT',
    'cost': lc_cost * lot,
    'max_profit': 'Unlimited',
    'max_loss': lc_cost * lot,
    'breakeven': [lc_breakeven],
    'pnl_at_target': lc_pnl(1394),
    'pnl_at_spot': lc_pnl(ltp),
    'delta': 'High (+0.5 to +0.7)',
    'theta': 'Negative (time decay hurts)',
    'margin': lc_cost * lot,
    'pnl_func': lc_pnl,
})

# ============================================================================
# STRATEGY 4: SYNTHETIC LONG (Risk Reversal) - Sell 1320 PE, Buy 1380 CE
# ============================================================================
c1320p_bid, _ = gb('NFO:ICICIBANK26JAN1320PE')
_, c1380_ask = gb('NFO:ICICIBANK26JAN1380CE')
rr_cost = c1380_ask - c1320p_bid  # Could be debit or credit

def rr_pnl(price):
    call_val = max(0, price - 1380)
    put_val = -max(0, 1320 - price)  # Short put
    return (call_val + put_val - rr_cost) * lot

rr_breakeven = 1380 + rr_cost if rr_cost > 0 else 1380

strategies.append({
    'name': 'Risk Reversal (Sell 1320P/Buy 1380C)',
    'type': 'DEBIT' if rr_cost > 0 else 'CREDIT',
    'cost': rr_cost * lot,
    'max_profit': 'Unlimited above 1380',
    'max_loss': f'Unlimited below 1320 (stock put to you at 1320)',
    'breakeven': [rr_breakeven, 1320],
    'pnl_at_target': rr_pnl(1394),
    'pnl_at_spot': rr_pnl(ltp),
    'delta': 'Very High (+0.7 to +0.9)',
    'theta': 'Near Zero (balanced)',
    'margin': '~Rs 50,000 (naked put margin)',
    'pnl_func': rr_pnl,
})

# ============================================================================
# STRATEGY 5: JADE LIZARD - Sell 1320 PE + Sell 1400/1420 Call Spread
# ============================================================================
c1320p_bid, _ = gb('NFO:ICICIBANK26JAN1320PE')
c1400_bid, _ = gb('NFO:ICICIBANK26JAN1400CE')
_, c1420_ask = gb('NFO:ICICIBANK26JAN1420CE')
jl_credit = c1320p_bid + (c1400_bid - c1420_ask)

def jl_pnl(price):
    put_pnl = -max(0, 1320 - price)
    call_spread = -max(0, price - 1400) + max(0, price - 1420)
    return (put_pnl + call_spread + jl_credit) * lot

jl_lower_be = 1320 - jl_credit

strategies.append({
    'name': 'Jade Lizard (Sell 1320P + 1400/1420 CS)',
    'type': 'CREDIT',
    'cost': -jl_credit * lot,
    'max_profit': jl_credit * lot,
    'max_loss': f'Below {jl_lower_be:.0f}: unlimited | Above 1420: Rs {(20-jl_credit)*lot:,.0f}',
    'breakeven': [jl_lower_be, 'No upside BE if credit > 20'],
    'pnl_at_target': jl_pnl(1394),
    'pnl_at_spot': jl_pnl(ltp),
    'delta': 'Low Positive (+0.2)',
    'theta': 'Positive (time decay helps)',
    'margin': '~Rs 50,000',
    'pnl_func': jl_pnl,
})

# ============================================================================
# STRATEGY 6: CALL RATIO SPREAD - Buy 1 x 1350 CE, Sell 2 x 1400 CE
# ============================================================================
_, c1350_ask = gb('NFO:ICICIBANK26JAN1350CE')
c1400_bid, _ = gb('NFO:ICICIBANK26JAN1400CE')
crs_cost = c1350_ask - 2 * c1400_bid

def crs_pnl(price):
    long_val = max(0, price - 1350)
    short_val = -2 * max(0, price - 1400)
    return (long_val + short_val - crs_cost) * lot

crs_max_profit = 50 - crs_cost  # At 1400
crs_upper_be = 1400 + (50 - crs_cost)  # Where it goes back to zero

strategies.append({
    'name': 'Call Ratio Spread (1x1350/2x1400)',
    'type': 'DEBIT' if crs_cost > 0 else 'CREDIT',
    'cost': crs_cost * lot,
    'max_profit': crs_max_profit * lot,
    'max_loss': f'Below 1350: Rs {crs_cost*lot:,.0f} | Above {crs_upper_be:.0f}: Unlimited',
    'breakeven': [1350 + crs_cost, crs_upper_be],
    'pnl_at_target': crs_pnl(1394),
    'pnl_at_spot': crs_pnl(ltp),
    'delta': 'Moderate then negative above 1400',
    'theta': 'Mixed (positive near ATM)',
    'margin': '~Rs 40,000 (naked call margin)',
    'pnl_func': crs_pnl,
})

# ============================================================================
# STRATEGY 7: PROTECTIVE PUT BUTTERFLY (Put BWB for bullish)
# Buy 1340 PE, Sell 2x 1370 PE, Buy 1390 PE
# ============================================================================
_, c1340p_ask = gb('NFO:ICICIBANK26JAN1340PE')
c1370p_bid, _ = gb('NFO:ICICIBANK26JAN1370PE')
_, c1390p_ask = gb('NFO:ICICIBANK26JAN1390PE')
pbwb_cost = c1390p_ask - 2*c1370p_bid + c1340p_ask

def pbwb_pnl(price):
    val = max(0, 1390-price) - 2*max(0, 1370-price) + max(0, 1340-price)
    return (val - pbwb_cost) * lot

strategies.append({
    'name': 'Put BWB (1340/1370/1390) Bullish',
    'type': 'DEBIT',
    'cost': pbwb_cost * lot,
    'max_profit': (30 - pbwb_cost) * lot,  # At 1370
    'max_loss': pbwb_cost * lot,  # Above 1390
    'breakeven': [1390 - pbwb_cost, 1340 + pbwb_cost],
    'pnl_at_target': pbwb_pnl(1394),
    'pnl_at_spot': pbwb_pnl(ltp),
    'delta': 'Near zero above 1390',
    'theta': 'Positive above 1390 (puts decay)',
    'margin': pbwb_cost * lot,
    'pnl_func': pbwb_pnl,
})

# ============================================================================
# STRATEGY 8: STANDARD BUTTERFLY (1360/1390/1420)
# ============================================================================
_, c1360_ask = gb('NFO:ICICIBANK26JAN1360CE')
c1390_bid, _ = gb('NFO:ICICIBANK26JAN1390CE')
_, c1420_ask = gb('NFO:ICICIBANK26JAN1420CE')
sbf_cost = c1360_ask - 2*c1390_bid + c1420_ask

def sbf_pnl(price):
    val = max(0, price-1360) - 2*max(0, price-1390) + max(0, price-1420)
    return (val - sbf_cost) * lot

strategies.append({
    'name': 'Standard Butterfly 1360/1390/1420',
    'type': 'DEBIT',
    'cost': sbf_cost * lot,
    'max_profit': (30 - sbf_cost) * lot,
    'max_loss': sbf_cost * lot,
    'breakeven': [1360 + sbf_cost, 1420 - sbf_cost],
    'pnl_at_target': sbf_pnl(1394),
    'pnl_at_spot': sbf_pnl(ltp),
    'delta': 'Near zero (neutral)',
    'theta': 'Positive near max profit',
    'margin': sbf_cost * lot,
    'pnl_func': sbf_pnl,
})

# ============================================================================
# STRATEGY 9: AGGRESSIVE BULL CALL SPREAD (1340/1380)
# ============================================================================
_, c1340_ask = gb('NFO:ICICIBANK26JAN1340CE')
c1380_bid, _ = gb('NFO:ICICIBANK26JAN1380CE')
abcs_cost = c1340_ask - c1380_bid
abcs_max_profit = 40 - abcs_cost

def abcs_pnl(price):
    val = max(0, price-1340) - max(0, price-1380)
    return (val - abcs_cost) * lot

strategies.append({
    'name': 'Aggressive Bull Call 1340/1380',
    'type': 'DEBIT',
    'cost': abcs_cost * lot,
    'max_profit': abcs_max_profit * lot,
    'breakeven': [1340 + abcs_cost],
    'max_loss': abcs_cost * lot,
    'pnl_at_target': abcs_pnl(1394),
    'pnl_at_spot': abcs_pnl(ltp),
    'delta': 'High (+0.6 to +0.8)',
    'theta': 'Negative',
    'margin': abcs_cost * lot,
    'pnl_func': abcs_pnl,
})

# ============================================================================
# STRATEGY 10: DEEP ITM BULL CALL SPREAD (1300/1350)
# ============================================================================
_, c1300_ask = gb('NFO:ICICIBANK26JAN1300CE')
c1350_bid, _ = gb('NFO:ICICIBANK26JAN1350CE')
ditm_cost = c1300_ask - c1350_bid
ditm_max_profit = 50 - ditm_cost

def ditm_pnl(price):
    val = max(0, price-1300) - max(0, price-1350)
    return (val - ditm_cost) * lot

strategies.append({
    'name': 'Deep ITM Bull Call 1300/1350',
    'type': 'DEBIT',
    'cost': ditm_cost * lot,
    'max_profit': ditm_max_profit * lot,
    'breakeven': [1300 + ditm_cost],
    'max_loss': ditm_cost * lot,
    'pnl_at_target': ditm_pnl(1394),
    'pnl_at_spot': ditm_pnl(ltp),
    'delta': 'Very High (+0.8 to +0.95)',
    'theta': 'Low negative',
    'margin': ditm_cost * lot,
    'pnl_func': ditm_pnl,
})

# ============================================================================
# PRINT COMPARISON TABLE
# ============================================================================
print()
print('STRATEGY METRICS SUMMARY')
print('-'*90)
print(f"{'#':<3} {'Strategy':<35} {'Cost/Credit':>12} {'Max Profit':>12} {'At 1394':>12}")
print('-'*90)

for i, s in enumerate(strategies, 1):
    cost_str = f"{s['cost']:+,.0f}" if isinstance(s['cost'], (int, float)) else str(s['cost'])
    max_p = s['max_profit']
    max_p_str = f"{max_p:,.0f}" if isinstance(max_p, (int, float)) else str(max_p)
    target_pnl = s['pnl_at_target']
    target_str = f"{target_pnl:+,.0f}" if isinstance(target_pnl, (int, float)) else str(target_pnl)
    print(f"{i:<3} {s['name']:<35} {cost_str:>12} {max_p_str:>12} {target_str:>12}")

print()
print('='*90)
print('DETAILED ANALYSIS BY PERSPECTIVE')
print('='*90)

# ============================================================================
# PERSPECTIVE 1: MAXIMUM PROFIT AT TARGET (1394)
# ============================================================================
print()
print('1. BEST PROFIT AT TARGET (Rs 1394)')
print('-'*50)
ranked = sorted([s for s in strategies if isinstance(s['pnl_at_target'], (int, float))],
                key=lambda x: x['pnl_at_target'], reverse=True)
for i, s in enumerate(ranked[:5], 1):
    print(f"   {i}. {s['name']}: Rs {s['pnl_at_target']:+,.0f}")

# ============================================================================
# PERSPECTIVE 2: MINIMUM CAPITAL AT RISK
# ============================================================================
print()
print('2. MINIMUM CAPITAL AT RISK')
print('-'*50)
ranked = sorted([s for s in strategies if isinstance(s['cost'], (int, float))],
                key=lambda x: abs(x['cost']))
for i, s in enumerate(ranked[:5], 1):
    cost = s['cost']
    type_str = '(Credit)' if cost < 0 else '(Debit)'
    print(f"   {i}. {s['name']}: Rs {abs(cost):,.0f} {type_str}")

# ============================================================================
# PERSPECTIVE 3: BEST RISK:REWARD RATIO
# ============================================================================
print()
print('3. BEST RISK:REWARD RATIO')
print('-'*50)
rr_list = []
for s in strategies:
    if isinstance(s['max_profit'], (int, float)) and isinstance(s['cost'], (int, float)):
        risk = abs(s['cost']) if s['cost'] > 0 else abs(s['max_loss']) if isinstance(s['max_loss'], (int, float)) else 0
        if risk > 0:
            rr = s['max_profit'] / risk
            rr_list.append((s['name'], rr, risk, s['max_profit']))
ranked = sorted(rr_list, key=lambda x: x[1], reverse=True)
for i, (name, rr, risk, profit) in enumerate(ranked[:5], 1):
    print(f"   {i}. {name}: 1:{rr:.1f} (Risk Rs {risk:,.0f} -> Max Rs {profit:,.0f})")

# ============================================================================
# PERSPECTIVE 4: PROBABILITY OF PROFIT (Based on breakeven distance)
# ============================================================================
print()
print('4. HIGHEST PROBABILITY OF PROFIT (Breakeven analysis)')
print('-'*50)
prob_list = []
for s in strategies:
    be = s['breakeven']
    if isinstance(be, list) and len(be) > 0 and isinstance(be[0], (int, float)):
        # Distance from current price to breakeven
        dist = abs(be[0] - ltp) / ltp * 100
        prob_list.append((s['name'], be[0], dist, s['type']))
ranked = sorted(prob_list, key=lambda x: x[2])  # Closest breakeven = highest prob
for i, (name, be, dist, typ) in enumerate(ranked[:5], 1):
    direction = 'above' if be > ltp else 'below'
    print(f"   {i}. {name}: BE @ {be:.0f} ({dist:.1f}% {direction} current)")

# ============================================================================
# PERSPECTIVE 5: THETA POSITIVE (Time decay helps)
# ============================================================================
print()
print('5. THETA POSITIVE STRATEGIES (Time decay helps)')
print('-'*50)
theta_pos = [s for s in strategies if 'Positive' in s['theta']]
for s in theta_pos:
    cost = s['cost']
    cost_str = f"Credit Rs {abs(cost):,.0f}" if cost < 0 else f"Debit Rs {cost:,.0f}"
    print(f"   - {s['name']}: {cost_str}")

# ============================================================================
# PERSPECTIVE 6: IF STOCK STAYS FLAT
# ============================================================================
print()
print('6. IF STOCK STAYS AT CURRENT LEVEL (Rs {:.0f})'.format(ltp))
print('-'*50)
flat_ranked = sorted([s for s in strategies if isinstance(s['pnl_at_spot'], (int, float))],
                     key=lambda x: x['pnl_at_spot'], reverse=True)
for i, s in enumerate(flat_ranked[:5], 1):
    print(f"   {i}. {s['name']}: Rs {s['pnl_at_spot']:+,.0f}")

# ============================================================================
# PERSPECTIVE 7: MARGIN EFFICIENCY
# ============================================================================
print()
print('7. MARGIN EFFICIENCY (Return per Rs blocked)')
print('-'*50)
margin_eff = []
for s in strategies:
    if isinstance(s['max_profit'], (int, float)) and isinstance(s.get('margin'), (int, float)):
        if s['margin'] > 0:
            eff = s['max_profit'] / s['margin']
            margin_eff.append((s['name'], eff, s['margin'], s['max_profit']))
ranked = sorted(margin_eff, key=lambda x: x[1], reverse=True)
for i, (name, eff, margin, profit) in enumerate(ranked[:5], 1):
    print(f"   {i}. {name}: {eff:.1f}x (Rs {margin:,.0f} margin -> Rs {profit:,.0f} max)")

# ============================================================================
# PAYOFF TABLE
# ============================================================================
print()
print('='*90)
print('PAYOFF TABLE AT EXPIRY')
print('='*90)
print()

prices = [1300, 1320, 1340, 1350, 1360, 1370, 1380, 1390, 1394, 1400, 1420, 1440]

# Select top 6 strategies for display
display_strats = [
    strategies[0],  # Bull Call Spread
    strategies[1],  # Bull Put Spread
    strategies[3],  # Risk Reversal
    strategies[5],  # Call Ratio
    strategies[8],  # Aggressive Bull Call
    strategies[9],  # Deep ITM
]

headers = ['Price'] + [s['name'][:20] for s in display_strats]
print(f"{headers[0]:>8}", end='')
for h in headers[1:]:
    print(f"{h:>22}", end='')
print()
print('-'*140)

for p in prices:
    print(f"{p:>8}", end='')
    for s in display_strats:
        pnl = s['pnl_func'](p)
        print(f"{pnl:>+22,.0f}", end='')
    marker = ' <-- TARGET' if p == 1394 else (' <-- CURRENT' if p == int(ltp) else '')
    print(marker)

# ============================================================================
# FINAL RECOMMENDATIONS
# ============================================================================
print()
print('='*90)
print('FINAL RECOMMENDATIONS BY SCENARIO')
print('='*90)
print()

print('SCENARIO A: HIGH CONVICTION (Stock will definitely reach 1394)')
print('-'*60)
print('  BEST: Aggressive Bull Call 1340/1380')
print(f'        Cost: Rs {strategies[8]["cost"]:,.0f}')
print(f'        At 1394: Rs {strategies[8]["pnl_at_target"]:+,.0f}')
print('        Why: Maximizes gain if target is hit')
print()

print('SCENARIO B: MODERATE CONVICTION (Think it will go up, not 100% sure)')
print('-'*60)
print('  BEST: Bull Call Spread 1350/1400')
print(f'        Cost: Rs {strategies[0]["cost"]:,.0f}')
print(f'        At 1394: Rs {strategies[0]["pnl_at_target"]:+,.0f}')
print('        Why: Good balance of cost vs potential')
print()

print('SCENARIO C: WANT INCOME + PROTECTION (High prob, limited upside ok)')
print('-'*60)
print('  BEST: Bull Put Spread 1320/1300')
print(f'        Credit: Rs {abs(strategies[1]["cost"]):,.0f}')
print(f'        At 1394: Rs {strategies[1]["pnl_at_target"]:+,.0f}')
print('        Why: Profit if stock stays above 1320, no upfront cost')
print()

print('SCENARIO D: LEVERAGE PLAY (High risk, high reward)')
print('-'*60)
print('  BEST: Risk Reversal (Sell 1320P/Buy 1380C)')
print(f'        Cost: Rs {strategies[3]["cost"]:,.0f}')
print(f'        At 1394: Rs {strategies[3]["pnl_at_target"]:+,.0f}')
print('        Why: Synthetic long, unlimited upside, but stock can be put to you')
print()

print('SCENARIO E: STOCK-LIKE EXPOSURE (Want to participate fully)')
print('-'*60)
print('  BEST: Deep ITM Bull Call 1300/1350')
print(f'        Cost: Rs {strategies[9]["cost"]:,.0f}')
print(f'        At 1394: Rs {strategies[9]["pnl_at_target"]:+,.0f}')
print('        Why: High delta, moves almost like stock')
print()

print('SCENARIO F: DEFENSIVE (Protect capital, still want upside)')
print('-'*60)
print('  BEST: Jade Lizard')
print(f'        Credit: Rs {abs(strategies[4]["cost"]):,.0f}')
print(f'        At 1394: Rs {strategies[4]["pnl_at_target"]:+,.0f}')
print('        Why: Collect premium, no risk on upside if credit > call spread width')
print()

print('='*90)
print('KEY TRADE-OFFS TO CONSIDER')
print('='*90)
print('''
1. COST vs PROBABILITY
   - Cheap strategies (butterflies) need PRECISE price targeting
   - Expensive strategies (spreads) profit over WIDE range

2. DEFINED vs UNDEFINED RISK
   - Spreads have capped losses (sleep well)
   - Naked options have unlimited risk (need monitoring)

3. TIME DECAY
   - Credit strategies BENEFIT from time decay
   - Debit strategies SUFFER from time decay
   - With 26 DTE, theta starts mattering more

4. DELTA EXPOSURE
   - High delta = moves with stock = needs to be right on direction
   - Low delta = less movement = more forgiving but lower profit

5. BREAKEVEN DISTANCE
   - Closer breakeven = higher probability of profit
   - Bull Put Spread breakeven at 1300s = 98%+ win rate
   - Long Call breakeven at 1367 = needs 2% move just to breakeven
''')

print()
print('YOUR CURRENT POSITION: Butterfly centered at 1360')
print('CONFLICT: You are SHORT gamma at 1360, but expect move to 1394')
print('SUGGESTION: Consider ADDING a directional trade to offset butterfly theta decay')
print('           Or EXIT butterfly if not expecting pin at 1360')
print()
