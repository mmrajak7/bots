#!/usr/bin/env python3
"""Check liquidity (bid-ask spread, OI, volume) for far OTM ICICIBANK CE options."""

import json
from pathlib import Path
from kiteconnect import KiteConnect

TOKEN_FILE = Path(__file__).resolve().parent.parent.parent / 'data' / 'kite_access_token.json'
with open(TOKEN_FILE) as f:
    token_data = json.load(f)

kite = KiteConnect(api_key=token_data['api_key'])
kite.set_access_token(token_data['access_token'])

# Get spot
spot_q = kite.ltp(['NSE:ICICIBANK'])
spot = spot_q['NSE:ICICIBANK']['last_price']
print(f"ICICIBANK Spot: {spot}")
print()

# Feb expiry - check all strikes from current BCS short (1410) to deep OTM
strikes = [1410, 1420, 1430, 1440, 1450, 1460, 1470, 1480, 1490, 1500, 1520]
symbols = [f"NFO:ICICIBANK26FEB{s}CE" for s in strikes]

quotes = kite.quote(symbols)

print(f"{'Strike':<8} {'Bid':>8} {'BidQty':>8} {'Ask':>8} {'AskQty':>8} {'Spread':>8} {'Sprd%':>8} {'LTP':>8} {'OI':>10} {'Volume':>10} {'OTM%':>8}")
print("-" * 110)

for s in strikes:
    sym = f"NFO:ICICIBANK26FEB{s}CE"
    q = quotes.get(sym, {})
    if not q:
        print(f"{s:<8} -- NO DATA --")
        continue

    depth = q.get('depth', {})
    best_bid = depth['buy'][0] if depth.get('buy') and depth['buy'][0]['price'] > 0 else {'price': 0, 'quantity': 0}
    best_ask = depth['sell'][0] if depth.get('sell') and depth['sell'][0]['price'] > 0 else {'price': 0, 'quantity': 0}

    bid = best_bid['price']
    bid_qty = best_bid['quantity']
    ask = best_ask['price']
    ask_qty = best_ask['quantity']
    ltp = q.get('last_price', 0)
    oi = q.get('oi', 0)
    vol = q.get('volume', 0)

    ba_spread = ask - bid if bid > 0 and ask > 0 else 0
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else ltp
    spread_pct = (ba_spread / mid * 100) if mid > 0 else 0
    otm_pct = ((s - spot) / spot * 100) if spot > 0 else 0

    print(f"{s:<8} {bid:>8.2f} {bid_qty:>8} {ask:>8.2f} {ask_qty:>8} {ba_spread:>8.2f} {spread_pct:>7.1f}% {ltp:>8.2f} {oi:>10,} {vol:>10,} {otm_pct:>7.1f}%")

# Also check full 5-level depth for a couple key strikes
print()
print("=" * 80)
print("FULL 5-LEVEL DEPTH FOR KEY OVERLAY STRIKES")
print("=" * 80)

for s in [1450, 1460, 1470, 1480]:
    sym = f"NFO:ICICIBANK26FEB{s}CE"
    q = quotes.get(sym, {})
    if not q:
        continue

    depth = q.get('depth', {})
    print(f"\n--- {s}CE ---")
    print(f"  BUY DEPTH (bids):")
    total_bid_qty = 0
    for level in depth.get('buy', []):
        if level['price'] > 0:
            total_bid_qty += level['quantity']
            print(f"    {level['price']:>8.2f} x {level['quantity']:>8,}")
    print(f"    Total bid depth: {total_bid_qty:,}")

    print(f"  SELL DEPTH (asks):")
    total_ask_qty = 0
    for level in depth.get('sell', []):
        if level['price'] > 0:
            total_ask_qty += level['quantity']
            print(f"    {level['price']:>8.2f} x {level['quantity']:>8,}")
    print(f"    Total ask depth: {total_ask_qty:,}")

# Now check what credit we'd ACTUALLY get for the overlay spreads
print()
print("=" * 80)
print("REALISTIC OVERLAY CREDIT (using actual bid/ask)")
print("=" * 80)
print()

overlays = [
    ("A: 1440/1460", 1440, 1460),
    ("B: 1450/1470", 1450, 1470),
    ("C: 1460/1480", 1460, 1480),
    ("B2: 1450/1480", 1450, 1480),
]

for label, short_k, long_k in overlays:
    short_sym = f"NFO:ICICIBANK26FEB{short_k}CE"
    long_sym = f"NFO:ICICIBANK26FEB{long_k}CE"

    sq = quotes.get(short_sym, {})
    lq = quotes.get(long_sym, {})

    s_depth = sq.get('depth', {})
    l_depth = lq.get('depth', {})

    # To SELL the short strike, we hit the BID
    s_bid = s_depth['buy'][0]['price'] if s_depth.get('buy') and s_depth['buy'][0]['price'] > 0 else 0
    s_bid_qty = s_depth['buy'][0]['quantity'] if s_depth.get('buy') and s_depth['buy'][0]['price'] > 0 else 0

    # To BUY the long strike, we hit the ASK
    l_ask = l_depth['sell'][0]['price'] if l_depth.get('sell') and l_depth['sell'][0]['price'] > 0 else 0
    l_ask_qty = l_depth['sell'][0]['quantity'] if l_depth.get('sell') and l_depth['sell'][0]['price'] > 0 else 0

    credit = s_bid - l_ask if s_bid > 0 and l_ask > 0 else 0
    credit_total = credit * 700
    width = long_k - short_k
    max_risk = (width - credit) * 700 if credit > 0 else width * 700

    # Slippage estimate: if you need to cross spread on both legs
    s_ask = s_depth['sell'][0]['price'] if s_depth.get('sell') and s_depth['sell'][0]['price'] > 0 else 0
    l_bid = l_depth['buy'][0]['price'] if l_depth.get('buy') and l_depth['buy'][0]['price'] > 0 else 0
    slippage_cost = (s_ask - s_bid) + (l_ask - l_bid) if s_bid > 0 and l_bid > 0 else 0

    print(f"{label}:")
    print(f"  Sell {short_k}CE @ bid {s_bid:.2f} (qty: {s_bid_qty:,})")
    print(f"  Buy  {long_k}CE @ ask {l_ask:.2f} (qty: {l_ask_qty:,})")
    print(f"  Credit/share:     {credit:>6.2f}")
    print(f"  Credit (700 qty): Rs {credit_total:>,.0f}")
    print(f"  Spread width:     {width}")
    print(f"  Bid-ask slippage: {slippage_cost:.2f}/share (both legs combined)")
    print(f"  Credit after worst-case slippage: {max(0, credit - slippage_cost):.2f}/share")
    if credit > 0 and slippage_cost > 0:
        print(f"  Slippage as % of credit: {slippage_cost/credit*100:.0f}%")
    print()
