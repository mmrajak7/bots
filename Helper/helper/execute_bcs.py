#!/usr/bin/env python3
"""Execute Bull Call Spread 1340/1390 for ICICIBANK."""

import json
import time
from pathlib import Path
from datetime import datetime
from kiteconnect import KiteConnect

TOKEN_FILE = Path('.').resolve().parent / 'data' / 'kite_access_token.json'
with open(TOKEN_FILE) as f:
    token_data = json.load(f)

kite = KiteConnect(api_key=token_data['api_key'])
kite.set_access_token(token_data['access_token'])

# Trade parameters
UNDERLYING = 'ICICIBANK'
EXPIRY = '26JAN'
LONG_STRIKE = 1340
SHORT_STRIKE = 1390
LOT_SIZE = 700
LOTS = 1
QUANTITY = LOT_SIZE * LOTS
SLIPPAGE_TICKS = 2  # 2 ticks slippage for entry
TICK_SIZE = 0.05

# Symbols
LONG_SYMBOL = f'NFO:{UNDERLYING}{EXPIRY}{LONG_STRIKE}CE'
SHORT_SYMBOL = f'NFO:{UNDERLYING}{EXPIRY}{SHORT_STRIKE}CE'

print('='*60)
print('BULL CALL SPREAD EXECUTION')
print('='*60)
print(f'Strategy: Buy {LONG_STRIKE} CE / Sell {SHORT_STRIKE} CE')
print(f'Quantity: {QUANTITY} ({LOTS} lot)')
print(f'Slippage: {SLIPPAGE_TICKS} ticks')
print()

# Get fresh quotes
print('Fetching fresh quotes...')
quotes = kite.quote([LONG_SYMBOL, SHORT_SYMBOL])

long_quote = quotes[LONG_SYMBOL]
short_quote = quotes[SHORT_SYMBOL]

long_ask = long_quote['depth']['sell'][0]['price'] if long_quote['depth']['sell'] else 0
short_bid = short_quote['depth']['buy'][0]['price'] if short_quote['depth']['buy'] else 0

print(f'{LONG_SYMBOL}:')
print(f'  Ask: {long_ask:.2f}')
print(f'{SHORT_SYMBOL}:')
print(f'  Bid: {short_bid:.2f}')
print()

net_debit = long_ask - short_bid
print(f'Expected Net Debit: Rs {net_debit:.2f} x {QUANTITY} = Rs {net_debit * QUANTITY:,.0f}')
print()

# Calculate limit prices with slippage
long_limit = long_ask + (SLIPPAGE_TICKS * TICK_SIZE)
short_limit = short_bid - (SLIPPAGE_TICKS * TICK_SIZE)

print('='*60)
print('ORDER EXECUTION (Margin-Optimized Sequence)')
print('='*60)
print()

# Step 1: BUY Long Call FIRST
print(f'STEP 1: BUY {LONG_STRIKE} CE (Long leg first)')
print(f'  Limit Price: {long_limit:.2f} (ask {long_ask:.2f} + {SLIPPAGE_TICKS} ticks)')
print()

try:
    order1 = kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=kite.EXCHANGE_NFO,
        tradingsymbol=f'{UNDERLYING}{EXPIRY}{LONG_STRIKE}CE',
        transaction_type=kite.TRANSACTION_TYPE_BUY,
        quantity=QUANTITY,
        product=kite.PRODUCT_NRML,
        order_type=kite.ORDER_TYPE_LIMIT,
        price=long_limit,
    )
    print(f'  Order placed! Order ID: {order1}')

    # Wait for order to fill
    print('  Waiting for fill...')
    time.sleep(2)

    # Check order status
    orders = kite.orders()
    order1_status = next((o for o in orders if o['order_id'] == order1), None)

    if order1_status:
        print(f'  Status: {order1_status["status"]}')
        if order1_status['status'] == 'COMPLETE':
            print(f'  Fill Price: {order1_status["average_price"]:.2f}')
            long_fill = order1_status['average_price']
        elif order1_status['status'] == 'REJECTED':
            print(f'  REJECTED: {order1_status.get("status_message", "Unknown")}')
            print('  ABORTING - No positions opened')
            exit(1)
        else:
            print(f'  Order pending... Status: {order1_status["status"]}')
            long_fill = long_limit  # Assume limit price
    else:
        print('  Could not fetch order status')
        long_fill = long_limit

except Exception as e:
    print(f'  ERROR placing order: {e}')
    print('  ABORTING - No positions opened')
    exit(1)

print()

# Step 2: SELL Short Call AFTER long is in
print(f'STEP 2: SELL {SHORT_STRIKE} CE (Short leg after long)')
print(f'  Limit Price: {short_limit:.2f} (bid {short_bid:.2f} - {SLIPPAGE_TICKS} ticks)')
print()

try:
    order2 = kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=kite.EXCHANGE_NFO,
        tradingsymbol=f'{UNDERLYING}{EXPIRY}{SHORT_STRIKE}CE',
        transaction_type=kite.TRANSACTION_TYPE_SELL,
        quantity=QUANTITY,
        product=kite.PRODUCT_NRML,
        order_type=kite.ORDER_TYPE_LIMIT,
        price=short_limit,
    )
    print(f'  Order placed! Order ID: {order2}')

    # Wait for order to fill
    print('  Waiting for fill...')
    time.sleep(2)

    # Check order status
    orders = kite.orders()
    order2_status = next((o for o in orders if o['order_id'] == order2), None)

    if order2_status:
        print(f'  Status: {order2_status["status"]}')
        if order2_status['status'] == 'COMPLETE':
            print(f'  Fill Price: {order2_status["average_price"]:.2f}')
            short_fill = order2_status['average_price']
        elif order2_status['status'] == 'REJECTED':
            print(f'  REJECTED: {order2_status.get("status_message", "Unknown")}')
            print('  WARNING: Long leg is OPEN! Manual intervention needed.')
            short_fill = 0
        else:
            print(f'  Order pending... Status: {order2_status["status"]}')
            short_fill = short_limit
    else:
        print('  Could not fetch order status')
        short_fill = short_limit

except Exception as e:
    print(f'  ERROR placing order: {e}')
    print('  WARNING: Long leg is OPEN! Manual intervention needed.')
    short_fill = 0

print()
print('='*60)
print('EXECUTION SUMMARY')
print('='*60)
print()
print(f'Strategy: Bull Call Spread {LONG_STRIKE}/{SHORT_STRIKE}')
print(f'Underlying: {UNDERLYING}')
print(f'Expiry: {EXPIRY}')
print(f'Quantity: {QUANTITY}')
print()

if 'long_fill' in dir() and 'short_fill' in dir() and short_fill > 0:
    actual_debit = long_fill - short_fill
    print(f'Long {LONG_STRIKE} CE @ {long_fill:.2f}')
    print(f'Short {SHORT_STRIKE} CE @ {short_fill:.2f}')
    print(f'Net Debit: Rs {actual_debit:.2f} x {QUANTITY} = Rs {actual_debit * QUANTITY:,.0f}')
    print()
    print(f'Max Profit: Rs {(SHORT_STRIKE - LONG_STRIKE - actual_debit) * QUANTITY:,.0f} (at {SHORT_STRIKE}+)')
    print(f'Max Loss: Rs {actual_debit * QUANTITY:,.0f} (below {LONG_STRIKE})')
    print(f'Breakeven: Rs {LONG_STRIKE + actual_debit:.2f}')
    print()

    # Log to analyses.json
    log_file = Path('.').resolve() / 'logs' / 'analyses.json'
    try:
        with open(log_file) as f:
            logs = json.load(f)
    except:
        logs = []

    logs.append({
        'timestamp': datetime.now().isoformat(),
        'strategy': 'Bull Call Spread',
        'underlying': UNDERLYING,
        'expiry': EXPIRY,
        'strikes': {'long': LONG_STRIKE, 'short': SHORT_STRIKE},
        'quantity': QUANTITY,
        'fills': {'long': long_fill, 'short': short_fill},
        'net_debit': actual_debit,
        'total_cost': actual_debit * QUANTITY,
        'max_profit': (SHORT_STRIKE - LONG_STRIKE - actual_debit) * QUANTITY,
        'breakeven': LONG_STRIKE + actual_debit,
        'order_ids': [order1, order2],
    })

    with open(log_file, 'w') as f:
        json.dump(logs, f, indent=2)
    print(f'Trade logged to {log_file}')

print()
print('DONE')
