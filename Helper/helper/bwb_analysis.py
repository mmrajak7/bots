#!/usr/bin/env python3
"""Deep dive analysis of Broken Wing Butterfly."""

import json
from pathlib import Path
from kiteconnect import KiteConnect

TOKEN_FILE = Path('.').resolve().parent / 'data' / 'kite_access_token.json'
with open(TOKEN_FILE) as f:
    token_data = json.load(f)

kite = KiteConnect(api_key=token_data['api_key'])
kite.set_access_token(token_data['access_token'])

spot = kite.ltp(['NSE:ICICIBANK'])
ltp = spot['NSE:ICICIBANK']['last_price']

symbols = [
    'NFO:ICICIBANK26JAN1340CE', 'NFO:ICICIBANK26JAN1350CE',
    'NFO:ICICIBANK26JAN1360CE', 'NFO:ICICIBANK26JAN1370CE',
    'NFO:ICICIBANK26JAN1380CE', 'NFO:ICICIBANK26JAN1390CE',
    'NFO:ICICIBANK26JAN1400CE', 'NFO:ICICIBANK26JAN1410CE',
    'NFO:ICICIBANK26JAN1420CE',
]
quotes = kite.quote(symbols)
lot = 700

def gb(q):
    bid = q['depth']['buy'][0]['price'] if q['depth']['buy'] else 0
    ask = q['depth']['sell'][0]['price'] if q['depth']['sell'] else 0
    return bid, ask

print(f'ICICIBANK Spot: Rs {ltp:.2f}')
print()

print('='*80)
print('BROKEN WING BUTTERFLY (BWB) - EXPLAINED')
print('='*80)

print('''
WHAT IS A BROKEN WING BUTTERFLY?

A standard butterfly has EQUAL wings:

  Price:    1360 ----------- 1390 ----------- 1420
  Wing:          30 pts            30 pts

A BROKEN WING butterfly has UNEQUAL wings:

  Price:    1360 ----------- 1390 ------- 1410
  Wing:          30 pts          20 pts  (BROKEN - narrower)

By making one wing narrower, you SHIFT RISK from one side to the other.
''')

print('='*80)
print('HOW IT CHANGES THE PAYOFF')
print('='*80)

# Get prices
_, c1360 = gb(quotes['NFO:ICICIBANK26JAN1360CE'])
c1390_bid, _ = gb(quotes['NFO:ICICIBANK26JAN1390CE'])
_, c1410 = gb(quotes['NFO:ICICIBANK26JAN1410CE'])
_, c1420 = gb(quotes['NFO:ICICIBANK26JAN1420CE'])

# Standard: 1360/1390/1420
std_cost = c1360 - 2*c1390_bid + c1420

# BWB: 1360/1390/1410
bwb_cost = c1360 - 2*c1390_bid + c1410

print()
print('LIVE QUOTES:')
print(f'  1360 CE Ask: {c1360:.2f}')
print(f'  1390 CE Bid: {c1390_bid:.2f}')
print(f'  1410 CE Ask: {c1410:.2f}')
print(f'  1420 CE Ask: {c1420:.2f}')
print()

print('COST COMPARISON:')
print(f'  Standard (1360/1390/1420): Rs {std_cost:.2f} x {lot} = Rs {std_cost*lot:,.0f}')
print(f'  BWB (1360/1390/1410):      Rs {bwb_cost:.2f} x {lot} = Rs {bwb_cost*lot:,.0f}')
print(f'  BWB costs Rs {(bwb_cost-std_cost)*lot:,.0f} MORE')
print()

print('='*80)
print('PAYOFF TABLE AT EXPIRY')
print('='*80)
print()
print(f"{'ICICI @':>10} {'Std Bfly':>12} {'BWB':>12} {'Bull Call':>12}")
print(f"{'Expiry':>10} {'1360/90/1420':>12} {'1360/90/1410':>12} {'1350/1400':>12}")
print('-'*50)

# Bull call spread cost
_, bcs_buy = gb(quotes['NFO:ICICIBANK26JAN1350CE'])
bcs_sell, _ = gb(quotes['NFO:ICICIBANK26JAN1400CE'])
bcs_cost = bcs_buy - bcs_sell

prices = [1300, 1320, 1340, 1360, 1375, 1390, 1394, 1400, 1410, 1420, 1440]

for p in prices:
    # Standard 1360/1390/1420
    std_val = max(0, p-1360) - 2*max(0, p-1390) + max(0, p-1420)
    std_pnl = (std_val - std_cost) * lot

    # BWB 1360/1390/1410
    bwb_val = max(0, p-1360) - 2*max(0, p-1390) + max(0, p-1410)
    bwb_pnl = (bwb_val - bwb_cost) * lot

    # Bull call spread 1350/1400
    bcs_val = max(0, p-1350) - max(0, p-1400)
    bcs_pnl = (bcs_val - bcs_cost) * lot

    marker = ' <-- TARGET' if p == 1394 else (' <-- MAX' if p == 1390 else '')
    print(f'{p:>10} {std_pnl:>+12,.0f} {bwb_pnl:>+12,.0f} {bcs_pnl:>+12,.0f}{marker}')

print()
print('='*80)
print('KEY OBSERVATIONS')
print('='*80)
print()
print('1. BELOW 1360 (Stock crashes):')
print(f'   Standard: Lose Rs {std_cost*lot:,.0f}')
print(f'   BWB:      Lose Rs {bwb_cost*lot:,.0f}')
print(f'   Bull Call: Lose Rs {bcs_cost*lot:,.0f}')
print('   --> BWB loses MOST on downside')
print()

print('2. AT 1390 (Sweet spot):')
std_max = (30 - std_cost) * lot
bwb_max = (30 - bwb_cost) * lot
print(f'   Standard: +Rs {std_max:,.0f} (MAX)')
print(f'   BWB:      +Rs {bwb_max:,.0f} (MAX)')
print(f'   Bull Call: +Rs {(40-bcs_cost)*lot:,.0f}')
print('   --> Both butterflies peak here')
print()

print('3. AT 1394 (Your target):')
std_1394 = (26 - std_cost) * lot  # 30 - 4 = 26
bwb_1394 = (26 - bwb_cost) * lot
bcs_1394 = (44 - bcs_cost) * lot
print(f'   Standard: +Rs {std_1394:,.0f}')
print(f'   BWB:      +Rs {bwb_1394:,.0f}')
print(f'   Bull Call: +Rs {bcs_1394:,.0f}')
print('   --> Bull Call Spread wins at your target!')
print()

print('4. ABOVE 1410 (Stock rallies hard):')
print('   Standard: Still has protection (1420 wing)')
print('   BWB:      Loses faster (only 1410 wing)')
print('   Bull Call: Maxes out at 1400')
print('   --> BWB is WORST for big rally')
print()

print('='*80)
print('RISK:REWARD COMPARISON')
print('='*80)
print()
print(f"{'Strategy':<28} {'Cost':>10} {'Max Profit':>12} {'R:R':>8}")
print('-'*60)
print(f"{'Standard Butterfly':<28} {std_cost*lot:>10,.0f} {std_max:>12,.0f} {'1:'+str(round(std_max/(std_cost*lot),1)):>8}")
print(f"{'Broken Wing Butterfly':<28} {bwb_cost*lot:>10,.0f} {bwb_max:>12,.0f} {'1:'+str(round(bwb_max/(bwb_cost*lot),1)):>8}")
print(f"{'Bull Call Spread':<28} {bcs_cost*lot:>10,.0f} {(50-bcs_cost)*lot:>12,.0f} {'1:'+str(round((50-bcs_cost)*lot/(bcs_cost*lot),1)):>8}")
print()

print('='*80)
print('WHEN TO USE EACH STRATEGY')
print('='*80)
print('''
STANDARD BUTTERFLY:
  Best when: Expecting stock to PIN at specific price
  Risk: Equal on both sides
  Reward: Highest R:R ratio

BROKEN WING BUTTERFLY:
  Best when: Want asymmetric payoff (shift risk to one side)
  Risk: Higher on the "broken" side, lower/zero on other
  Reward: Lower than standard (you pay for the asymmetry)

BULL CALL SPREAD:
  Best when: Expecting DIRECTIONAL move
  Risk: Full premium paid
  Reward: Profits anywhere above breakeven to max

For target 1394 (+4% move):
  --> BULL CALL SPREAD is optimal
  --> Butterfly strategies are for PINNING, not trending
''')

print('='*80)
print('BWB SWEET SPOT USE CASE')
print('='*80)
print('''
BWB works best when:

1. You expect stock to move TOWARD the narrower wing
   (So the risk on that side doesn't matter)

2. You want ZERO risk on one side and willing to accept
   MORE risk on the other side

3. You can get it for CREDIT (rare but possible)
   Then zero risk on one side = free money

For ICICIBANK bullish to 1394:
  - BWB with narrow UPPER wing (1360/1390/1410) is WRONG
  - Because you expect UP move, but narrow upper wing hurts UP move

  - BWB with narrow LOWER wing using PUTS might work better:
    Buy 1340 PE, Sell 2x 1370 PE, Buy 1390 PE
    This has zero risk ABOVE 1390, risk only below 1340
''')

print('='*80)
print('FINAL VERDICT')
print('='*80)
print()
print('For ICICIBANK target 1394:')
print()
print('  1. BULL CALL SPREAD - BEST')
print('     Designed for directional moves')
print(f'     Cost: Rs {bcs_cost*lot:,.0f}')
print(f'     At 1394: Rs {bcs_1394:,.0f} profit')
print()
print('  2. STANDARD BUTTERFLY - SECOND')
print('     Better R:R than BWB, same max profit')
print(f'     Cost: Rs {std_cost*lot:,.0f}')
print(f'     At 1394: Rs {std_1394:,.0f} profit')
print()
print('  3. BROKEN WING BUTTERFLY - THIRD')
print('     Costs more, profits less, wrong risk profile for up move')
print(f'     Cost: Rs {bwb_cost*lot:,.0f}')
print(f'     At 1394: Rs {bwb_1394:,.0f} profit')
print()
