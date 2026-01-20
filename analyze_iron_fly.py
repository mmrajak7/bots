"""
Iron Fly Trade Analysis Script
Analyzes closed iron fly positions from Zerodha
"""

import json
import sys
import io
import re
from datetime import datetime, timedelta
from kiteconnect import KiteConnect
from collections import defaultdict

# Fix Windows console encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# Load token
with open('data/kite_access_token.json', 'r') as f:
    token_data = json.load(f)

api_key = token_data['api_key']
access_token = token_data['access_token']

# Initialize Kite
kite = KiteConnect(api_key=api_key)
kite.set_access_token(access_token)

def parse_nifty_symbol(symbol):
    """Parse NIFTY option symbol to extract components."""
    # Format: NIFTY25D2324000CE = NIFTY + YY + MonthCode + Day + Strike + Type
    # MonthCode: 1-9 for Jan-Sep, O/N/D for Oct/Nov/Dec
    match = re.match(r'NIFTY(\d{2})([A-Z])(\d{2})(\d+)(CE|PE)', symbol)
    if match:
        year = int('20' + match.group(1))
        month_code = match.group(2)
        day = int(match.group(3))
        strike = int(match.group(4))
        opt_type = match.group(5)

        # Month code mapping
        month_map = {'1': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6,
                     '7': 7, '8': 8, '9': 9, 'O': 10, 'N': 11, 'D': 12}
        month = month_map.get(month_code, 12)

        try:
            expiry_date = datetime(year, month, day)
            expiry_str = expiry_date.strftime('%d-%b-%Y')
        except:
            expiry_str = f"{day}-{month_code}-{year}"

        return {
            'symbol': symbol,
            'expiry': expiry_str,
            'expiry_code': f"{match.group(1)}{month_code}{match.group(3)}",
            'strike': strike,
            'type': opt_type
        }
    return None

print("=" * 80)
print("IRON FLY TRADE ANALYSIS - " + datetime.now().strftime("%Y-%m-%d %H:%M"))
print("=" * 80)

# Fetch all trades for today
try:
    trades = kite.trades()
    orders = kite.orders()
    positions = kite.positions()

    print(f"\nTotal trades today: {len(trades)}")
    print(f"Total orders today: {len(orders)}")

    # Group trades by tradingsymbol
    trades_by_symbol = defaultdict(list)
    for trade in trades:
        trades_by_symbol[trade['tradingsymbol']].append(trade)

    # Identify NIFTY options trades
    nifty_trades = {k: v for k, v in trades_by_symbol.items() if 'NIFTY' in k and k != 'NIFTY 50'}
    print(f"NIFTY option trades: {len(nifty_trades)} symbols")

    # Parse all trades with option details
    parsed_trades = []
    for symbol, symbol_trades in nifty_trades.items():
        parsed = parse_nifty_symbol(symbol)
        if parsed:
            buy_qty = sum(t['quantity'] for t in symbol_trades if t['transaction_type'] == 'BUY')
            sell_qty = sum(t['quantity'] for t in symbol_trades if t['transaction_type'] == 'SELL')
            buy_value = sum(t['quantity'] * t['average_price'] for t in symbol_trades if t['transaction_type'] == 'BUY')
            sell_value = sum(t['quantity'] * t['average_price'] for t in symbol_trades if t['transaction_type'] == 'SELL')

            parsed['buy_qty'] = buy_qty
            parsed['sell_qty'] = sell_qty
            parsed['buy_avg'] = buy_value / buy_qty if buy_qty else 0
            parsed['sell_avg'] = sell_value / sell_qty if sell_qty else 0
            parsed['trades'] = symbol_trades
            parsed_trades.append(parsed)

    # Group by expiry to identify iron fly sets
    by_expiry = defaultdict(list)
    for pt in parsed_trades:
        by_expiry[pt['expiry']].append(pt)

    print("\n" + "=" * 80)
    print("IRON FLY STRUCTURES IDENTIFIED")
    print("=" * 80)

    # Get positions to understand if these are entry or exit trades
    net_positions = positions.get('net', [])
    positions_map = {p['tradingsymbol']: p for p in net_positions}

    # Analyze by expiry
    for expiry, legs in by_expiry.items():
        print(f"\n{'='*80}")
        print(f"EXPIRY: {expiry}")
        print(f"{'='*80}")

        # Sort by strike
        legs_sorted = sorted(legs, key=lambda x: x['strike'])
        strikes = sorted(set(l['strike'] for l in legs_sorted))

        print(f"\nStrikes traded: {strikes}")

        # Group trades by time to identify separate iron flies
        all_trades = []
        for leg in legs_sorted:
            for t in leg['trades']:
                all_trades.append({
                    'time': t['fill_timestamp'],
                    'symbol': leg['symbol'],
                    'strike': leg['strike'],
                    'type': leg['type'],
                    'txn': t['transaction_type'],
                    'qty': t['quantity'],
                    'price': t['average_price']
                })

        all_trades_sorted = sorted(all_trades, key=lambda x: x['time'])

        # Identify iron fly groups by time proximity (within 2 minutes)
        iron_flies = []
        current_fly = []
        last_time = None

        for trade in all_trades_sorted:
            trade_time = datetime.strptime(str(trade['time']), '%Y-%m-%d %H:%M:%S') if isinstance(trade['time'], str) else trade['time']

            if last_time is None or (trade_time - last_time).seconds < 120:
                current_fly.append(trade)
            else:
                if current_fly:
                    iron_flies.append(current_fly)
                current_fly = [trade]
            last_time = trade_time

        if current_fly:
            iron_flies.append(current_fly)

        print(f"\nNumber of Iron Fly sets: {len(iron_flies)}")

        # Analyze each iron fly
        for idx, fly_trades in enumerate(iron_flies, 1):
            print(f"\n{'-'*60}")
            print(f"IRON FLY #{idx}")
            print(f"{'-'*60}")

            # Get time range
            times = [t['time'] for t in fly_trades]
            print(f"Exit Time: {min(times)} to {max(times)}")

            # Organize by leg type
            legs_detail = {}
            total_exit_premium = 0
            lot_size = 75

            for t in fly_trades:
                key = f"{t['strike']}{t['type']}"
                if key not in legs_detail:
                    legs_detail[key] = {'strike': t['strike'], 'type': t['type'],
                                        'buy_qty': 0, 'sell_qty': 0, 'buy_value': 0, 'sell_value': 0}

                if t['txn'] == 'BUY':
                    legs_detail[key]['buy_qty'] += t['qty']
                    legs_detail[key]['buy_value'] += t['qty'] * t['price']
                else:
                    legs_detail[key]['sell_qty'] += t['qty']
                    legs_detail[key]['sell_value'] += t['qty'] * t['price']

            # Classify legs
            ce_legs = sorted([l for l in legs_detail.values() if l['type'] == 'CE'], key=lambda x: x['strike'])
            pe_legs = sorted([l for l in legs_detail.values() if l['type'] == 'PE'], key=lambda x: x['strike'])

            print("\nLEGS BREAKDOWN (Exit Trades):")
            print("-" * 60)

            # Calculate net cost/credit at exit
            total_debit = 0
            total_credit = 0

            for leg in sorted(legs_detail.values(), key=lambda x: (x['strike'], x['type'])):
                strike = leg['strike']
                opt_type = leg['type']
                buy_qty = leg['buy_qty']
                sell_qty = leg['sell_qty']
                buy_avg = leg['buy_value'] / buy_qty if buy_qty else 0
                sell_avg = leg['sell_value'] / sell_qty if sell_qty else 0

                if buy_qty > 0:
                    # Bought to close = was SHORT
                    print(f"  {strike} {opt_type}: BOUGHT {buy_qty:>3} @ {buy_avg:>7.2f} (closing SHORT)")
                    total_debit += leg['buy_value']
                if sell_qty > 0:
                    # Sold to close = was LONG
                    print(f"  {strike} {opt_type}: SOLD   {sell_qty:>3} @ {sell_avg:>7.2f} (closing LONG)")
                    total_credit += leg['sell_value']

            net_exit = total_credit - total_debit

            print(f"\nEXIT SUMMARY:")
            print(f"  Total Debit (bought to close):  Rs.{total_debit:>10,.2f}")
            print(f"  Total Credit (sold to close):   Rs.{total_credit:>10,.2f}")
            print(f"  Net Exit Cash Flow:             Rs.{net_exit:>10,.2f}")

            # Infer original position structure
            print(f"\nORIGINAL POSITION (before exit):")
            print("-" * 40)

            for leg in sorted(legs_detail.values(), key=lambda x: (x['strike'], x['type'])):
                strike = leg['strike']
                opt_type = leg['type']
                if leg['buy_qty'] > 0:
                    lots = leg['buy_qty'] // lot_size
                    print(f"  SHORT {lots} lot(s) {strike} {opt_type}")
                if leg['sell_qty'] > 0:
                    lots = leg['sell_qty'] // lot_size
                    print(f"  LONG  {lots} lot(s) {strike} {opt_type}")

    # Get P&L from positions
    print("\n" + "=" * 80)
    print("POSITION P&L (from Zerodha)")
    print("=" * 80)

    nifty_positions = [p for p in net_positions if 'NIFTY' in p.get('tradingsymbol', '')]

    total_realized_pnl = 0
    if nifty_positions:
        print(f"\n{'Symbol':<25} {'Qty':>6} {'Avg':>10} {'LTP':>10} {'P&L':>12}")
        print("-" * 70)
        for pos in sorted(nifty_positions, key=lambda x: x['tradingsymbol']):
            symbol = pos['tradingsymbol']
            qty = pos['quantity']
            avg = pos.get('average_price', 0)
            ltp = pos.get('last_price', 0)
            pnl = pos.get('pnl', 0)
            m2m = pos.get('m2m', 0)
            realized = pos.get('realised', 0)
            unrealized = pos.get('unrealised', 0)

            total_realized_pnl += realized

            parsed = parse_nifty_symbol(symbol)
            if parsed:
                strike = parsed['strike']
                opt_type = parsed['type']
                print(f"{strike:>5} {opt_type:<3} ({symbol[-12:]})  {qty:>6} {avg:>10.2f} {ltp:>10.2f} {pnl:>12,.2f}")

        print("-" * 70)
        print(f"{'TOTAL REALIZED P&L':>50}: Rs.{total_realized_pnl:>12,.2f}")
    else:
        print("\nAll positions closed - checking day positions for P&L...")
        day_positions = positions.get('day', [])
        nifty_day = [p for p in day_positions if 'NIFTY' in p.get('tradingsymbol', '')]

        if nifty_day:
            print(f"\n{'Symbol':<25} {'Qty':>6} {'Avg':>10} {'P&L':>12}")
            print("-" * 60)
            for pos in sorted(nifty_day, key=lambda x: x['tradingsymbol']):
                symbol = pos['tradingsymbol']
                qty = pos['quantity']
                avg = pos.get('average_price', 0)
                pnl = pos.get('pnl', 0)
                realized = pos.get('realised', 0)
                total_realized_pnl += realized
                print(f"{symbol:<25} {qty:>6} {avg:>10.2f} {pnl:>12,.2f}")
            print("-" * 60)
            print(f"{'TOTAL REALIZED P&L':>40}: Rs.{total_realized_pnl:>12,.2f}")

    # Order Timeline
    print("\n" + "=" * 80)
    print("ORDER TIMELINE")
    print("=" * 80)

    nifty_orders = [o for o in orders if 'NIFTY' in o.get('tradingsymbol', '') and o['tradingsymbol'] != 'NIFTY 50']

    print(f"\n{'Time':<20} {'Type':<5} {'Qty':>5} {'Symbol':<22} {'Price':>10} {'Status':<10}")
    print("-" * 80)

    for order in sorted(nifty_orders, key=lambda x: x.get('order_timestamp', '')):
        ts = str(order.get('order_timestamp', ''))[:19]
        symbol = order.get('tradingsymbol', '')
        txn = order.get('transaction_type', '')
        qty = order.get('quantity', 0)
        price = order.get('average_price', 0)
        status = order.get('status', '')

        if status == 'COMPLETE':
            print(f"{ts:<20} {txn:<5} {qty:>5} {symbol:<22} {price:>10.2f} {status:<10}")

    # Summary metrics
    print("\n" + "=" * 80)
    print("ANALYSIS SUMMARY")
    print("=" * 80)

    print(f"""
Note: The above shows EXIT trades from today. To get complete P&L analysis
including entry prices, we need the entry trade data from when positions
were opened (likely on an earlier day).

Key Observations:
- {len(iron_flies)} Iron Fly structure(s) were closed today
- Expiry: {list(by_expiry.keys())}
- Total symbols traded: {len(nifty_trades)}
""")

except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
