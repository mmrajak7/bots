#!/usr/bin/env python3
"""
Enhanced Position Checker for Butterfly Trades

Checks P&L of open butterfly positions with comprehensive analysis:
- Entry info (date, time, days in trade)
- Time decay analysis (DTE, theta burned)
- P&L breakdown by leg
- Position health assessment
- Actionable recommendations

Uses token from BOTS/data/kite_access_token.json
Always uses BID-ASK pricing (never LTP)

Usage:
    python position_checker.py           # Check all open positions
    python position_checker.py ICICIBANK # Check specific position

@author   Helper Bot
@created  2025-12-31
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Dict

# Paths
SCRIPT_DIR = Path(__file__).parent.resolve()
TOKEN_FILE = SCRIPT_DIR.parent / 'data' / 'kite_access_token.json'
ANALYSES_FILE = SCRIPT_DIR / 'logs' / 'analyses.json'


def load_kite_client():
    """Load Kite client using token from BOTS/data."""
    from kiteconnect import KiteConnect

    if not TOKEN_FILE.exists():
        print(f"ERROR: Token file not found at {TOKEN_FILE}")
        print("Run SNAIL authentication first.")
        sys.exit(1)

    with open(TOKEN_FILE) as f:
        token_data = json.load(f)

    # Check if token is from today
    generated = datetime.fromisoformat(token_data['generated_at'])
    if generated.date() != date.today():
        print(f"WARNING: Token is from {generated.date()}, may be expired")

    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])

    return kite


def load_open_positions() -> List[Dict]:
    """Load executed positions from analyses.json."""
    if not ANALYSES_FILE.exists():
        return []

    with open(ANALYSES_FILE) as f:
        analyses = json.load(f)

    # Filter to executed positions only
    return [a for a in analyses if a.get('executed') and a.get('execution_details')]


def get_spot_symbol(underlying: str) -> str:
    """Get spot symbol for underlying."""
    if underlying == 'NIFTY':
        return 'NSE:NIFTY 50'
    elif underlying == 'BANKNIFTY':
        return 'NSE:NIFTY BANK'
    elif underlying == 'SENSEX':
        return 'BSE:SENSEX'
    else:
        return f'NSE:{underlying}'


def check_position(kite, position: Dict) -> Dict:
    """Check current status for a single position with full analysis."""
    exec_details = position['execution_details']
    orders = exec_details.get('orders', [])

    if not orders:
        return {'error': 'No orders in execution details'}

    # Get quotes
    symbols = [f"NFO:{o['tradingsymbol']}" for o in orders]
    try:
        quotes = kite.quote(symbols)
    except Exception as e:
        return {'error': str(e), 'underlying': position['underlying']}

    # Entry info
    entry_dt = datetime.fromisoformat(exec_details['executed_at'])
    days_in_trade = (datetime.now() - entry_dt).days

    # Expiry & DTE
    exp_date = datetime.strptime(position['expiry'], '%Y-%m-%d').date()
    dte = (exp_date - date.today()).days
    original_dte = position.get('dte', dte + days_in_trade)
    theta_days_passed = original_dte - dte

    # Calculate leg details and P&L
    entry_debit = exec_details.get('total_debit_actual', 0)
    close_value = 0
    legs = []

    for order in orders:
        sym = f"NFO:{order['tradingsymbol']}"
        q = quotes.get(sym, {})

        if not q:
            continue

        bid = q['depth']['buy'][0]['price'] if q.get('depth', {}).get('buy') else 0
        ask = q['depth']['sell'][0]['price'] if q.get('depth', {}).get('sell') else 0

        # Calculate leg P&L
        if order['transaction_type'] == 'BUY':
            leg_pnl = (bid - order['fill_price']) * order['quantity']
            close_value += bid * order['quantity']
        else:
            leg_pnl = (order['fill_price'] - ask) * order['quantity']
            close_value -= ask * order['quantity']

        legs.append({
            'leg': order['leg'],
            'symbol': order['tradingsymbol'],
            'side': order['transaction_type'],
            'qty': order['quantity'],
            'entry': order['fill_price'],
            'bid': bid,
            'ask': ask,
            'pnl': leg_pnl
        })

    # Overall P&L
    pnl = close_value - entry_debit
    pnl_pct = (pnl / entry_debit) * 100 if entry_debit else 0

    # Get spot price
    spot_sym = get_spot_symbol(position['underlying'])
    try:
        spot = kite.ltp([spot_sym])
        spot_price = spot.get(spot_sym, {}).get('last_price', 0)
    except:
        spot_price = 0

    # Strike analysis
    atm = position['strikes']['atm']
    itm = position['strikes']['itm']
    otm = position['strikes']['otm']

    # Breakeven from metrics
    be_lower = position.get('metrics', {}).get('breakeven_lower', itm)
    be_upper = position.get('metrics', {}).get('breakeven_upper', otm)

    max_profit = position.get('metrics', {}).get('max_profit', 0) or position.get('totals', {}).get('total_max_profit', 0)

    # Distance calculations
    dist_from_atm = spot_price - atm
    dist_from_atm_pct = (dist_from_atm / atm) * 100 if atm else 0
    dist_to_lower_be = spot_price - be_lower
    dist_to_upper_be = be_upper - spot_price
    min_buffer = min(dist_to_lower_be, dist_to_upper_be)
    min_buffer_pct = (min_buffer / spot_price) * 100 if spot_price else 0

    return {
        'underlying': position['underlying'],
        'direction': position['direction'],
        'expiry': position['expiry'],
        'expiry_weekday': exp_date.strftime('%A'),

        # Entry info
        'entry_datetime': entry_dt,
        'days_in_trade': days_in_trade,
        'entry_debit': entry_debit,

        # Time decay
        'dte': dte,
        'original_dte': original_dte,
        'theta_days_passed': theta_days_passed,

        # Legs
        'legs': legs,

        # P&L
        'close_value': close_value,
        'pnl': pnl,
        'pnl_pct': pnl_pct,
        'max_profit': max_profit,

        # Position analysis
        'spot_price': spot_price,
        'atm_strike': atm,
        'itm_strike': itm,
        'otm_strike': otm,
        'be_lower': be_lower,
        'be_upper': be_upper,
        'dist_from_atm': dist_from_atm,
        'dist_from_atm_pct': dist_from_atm_pct,
        'dist_to_lower_be': dist_to_lower_be,
        'dist_to_upper_be': dist_to_upper_be,
        'min_buffer': min_buffer,
        'min_buffer_pct': min_buffer_pct,
    }


def print_position(result: Dict):
    """Print enhanced position analysis."""
    if 'error' in result:
        print(f"\nError checking {result.get('underlying', 'position')}: {result['error']}")
        return

    W = 75  # Width

    print(f"\n{'='*W}")
    print(f" {result['underlying']} {result['direction']} BUTTERFLY")
    print(f"{'='*W}")

    # Entry Info
    print(f"\n+{'-'*73}+")
    print(f"| {'ENTRY INFO':<71} |")
    print(f"+{'-'*73}+")
    print(f"| Entry Date:     {result['entry_datetime'].strftime('%Y-%m-%d %H:%M:%S'):<55} |")
    print(f"| Days in Trade:  {result['days_in_trade']} days{' ':<58} |")
    print(f"| Entry Debit:    Rs {result['entry_debit']:,.2f}{' ':<52} |")
    print(f"+{'-'*73}+")

    # Time Decay
    print(f"\n+{'-'*73}+")
    print(f"| {'TIME DECAY':<71} |")
    print(f"+{'-'*73}+")
    print(f"| Expiry:         {result['expiry']} ({result['expiry_weekday']}){' ':<41} |")
    print(f"| DTE Remaining:  {result['dte']} days{' ':<58} |")

    theta_pct = (result['theta_days_passed'] / result['original_dte'] * 100) if result['original_dte'] > 0 else 0
    print(f"| Theta Burned:   {result['theta_days_passed']} days ({theta_pct:.0f}% of original {result['original_dte']} DTE){' ':<30} |")

    # Theta zone assessment
    if result['dte'] <= 7:
        print(f"| {'*** GAMMA ZONE: Last week - max profit/loss acceleration!':<71} |")
    elif result['dte'] <= 14:
        print(f"| {'>> THETA ZONE: Accelerated decay - monitor closely':<71} |")
    else:
        print(f"| {'[OK] Safe Zone: Theta decay gradual':<71} |")
    print(f"+{'-'*73}+")

    # Leg Details
    print(f"\n+{'-'*73}+")
    print(f"| {'LEG DETAILS (BID-ASK Pricing)':<71} |")
    print(f"+{'-'*73}+")
    print(f"| {'Leg':<6} {'Symbol':<25} {'Entry':>8} {'Bid':>8} {'Ask':>8} {'P&L':>10} |")
    print(f"| {'-'*71} |")

    for leg in result['legs']:
        side = 'L' if leg['side'] == 'BUY' else 'S'
        print(f"| {leg['leg']:<3}({side}) {leg['symbol']:<25} {leg['entry']:>8.2f} {leg['bid']:>8.2f} {leg['ask']:>8.2f} {leg['pnl']:>+10,.0f} |")
    print(f"+{'-'*73}+")

    # P&L Summary
    print(f"\n+{'-'*73}+")
    print(f"| {'P&L SUMMARY':<71} |")
    print(f"+{'-'*73}+")
    print(f"| Entry Cost:      Rs {result['entry_debit']:>10,.2f}{' ':<48} |")
    print(f"| Current Value:   Rs {result['close_value']:>10,.2f}{' ':<48} |")
    print(f"| {'-'*71} |")

    if result['pnl'] >= 0:
        print(f"| >>> PROFIT:      Rs {result['pnl']:>+10,.2f}  ({result['pnl_pct']:>+6.1f}%){' ':<34} |")
    else:
        print(f"| >>> LOSS:        Rs {result['pnl']:>+10,.2f}  ({result['pnl_pct']:>+6.1f}%){' ':<34} |")

    if result['max_profit'] > 0:
        profit_captured = (result['pnl'] / result['max_profit']) * 100 if result['pnl'] > 0 else 0
        print(f"| Max Profit:      Rs {result['max_profit']:>10,.2f}{' ':<48} |")
        if result['pnl'] > 0:
            print(f"| Profit Captured: {profit_captured:>10.1f}%{' ':<51} |")
    print(f"+{'-'*73}+")

    # Position Analysis
    print(f"\n+{'-'*73}+")
    print(f"| {'POSITION ANALYSIS':<71} |")
    print(f"+{'-'*73}+")
    print(f"| Spot Price:      Rs {result['spot_price']:>10,.2f}{' ':<48} |")
    print(f"| ATM Strike:         {result['atm_strike']:>10}{' ':<49} |")
    print(f"| Distance from ATM:  {result['dist_from_atm']:>+10.2f} pts ({result['dist_from_atm_pct']:+.2f}%){' ':<27} |")
    print(f"| {'-'*71} |")
    print(f"| Profit Zone:     {result['be_lower']:.2f} <---- {result['atm_strike']} ----> {result['be_upper']:.2f}{' ':<22} |")
    print(f"| Buffer to Lower:    {result['dist_to_lower_be']:>+10.2f} pts{' ':<38} |")
    print(f"| Buffer to Upper:    {result['dist_to_upper_be']:>+10.2f} pts{' ':<38} |")
    print(f"| {'-'*71} |")

    # Risk Assessment
    if result['min_buffer'] < 0:
        print(f"| {'!!! CRITICAL: Price OUTSIDE profit zone!':<71} |")
        recommendation = "EXIT NOW"
        risk_level = "CRITICAL"
    elif result['min_buffer_pct'] < 0.5:
        print(f"| {'!!! HIGH RISK: Only':<16} {result['min_buffer']:.1f} pts ({result['min_buffer_pct']:.2f}%) to breakeven{' ':<23} |")
        recommendation = "WATCH CLOSELY"
        risk_level = "HIGH"
    elif result['min_buffer_pct'] < 1.0:
        print(f"| {'>> MODERATE:':<12} {result['min_buffer']:.1f} pts ({result['min_buffer_pct']:.2f}%) buffer - monitor{' ':<27} |")
        recommendation = "MONITOR"
        risk_level = "MODERATE"
    elif abs(result['dist_from_atm_pct']) < 0.5:
        print(f"| {'[IDEAL] Price near ATM with':<28} {result['min_buffer']:.1f} pts buffer{' ':<25} |")
        recommendation = "HOLD - IDEAL"
        risk_level = "LOW"
    else:
        print(f"| {'[OK] Safe:':<11} {result['min_buffer']:.1f} pts ({result['min_buffer_pct']:.2f}%) buffer{' ':<35} |")
        recommendation = "HOLD"
        risk_level = "LOW"
    print(f"+{'-'*73}+")

    # Recommendation
    print(f"\n+{'-'*73}+")
    print(f"| {'RECOMMENDATION':<71} |")
    print(f"+{'-'*73}+")

    factors = []

    # Time factor
    if result['dte'] <= 3:
        factors.append("EXPIRY: Imminent - close unless at max profit")
    elif result['dte'] <= 7:
        factors.append("TIME: Final week - gamma risk elevated")

    # P&L factor
    if result['pnl_pct'] >= 50:
        factors.append("PROFIT: 50%+ captured - consider booking")
    elif result['pnl_pct'] >= 30:
        factors.append("PROFIT: 30%+ captured - trail or book partial")
    elif result['pnl_pct'] <= -40:
        factors.append("LOSS: 40%+ loss - review exit strategy")
    elif result['pnl_pct'] <= -25:
        factors.append("LOSS: 25%+ loss - set stop loss")

    # Position factor
    if abs(result['dist_from_atm_pct']) > 2.0:
        factors.append(f"DRIFT: Price moved {abs(result['dist_from_atm_pct']):.1f}% from ATM")
    elif abs(result['dist_from_atm_pct']) > 1.0:
        factors.append(f"DRIFT: Price drifting ({abs(result['dist_from_atm_pct']):.1f}% from ATM)")

    if not factors:
        factors.append("STATUS: Position healthy - continue holding")

    for factor in factors:
        print(f"| -> {factor:<68} |")

    print(f"| {'-'*71} |")
    print(f"| ACTION: {recommendation:<62} |")
    print(f"| RISK:   {risk_level:<62} |")
    print(f"+{'-'*73}+")

    return result


def main():
    # Parse arguments
    filter_underlying = None
    if len(sys.argv) > 1:
        filter_underlying = sys.argv[1].upper()

    # Load positions
    positions = load_open_positions()

    if not positions:
        print("No open positions found in logs/analyses.json")
        return

    # Filter if specified
    if filter_underlying:
        positions = [p for p in positions if p['underlying'].upper() == filter_underlying]
        if not positions:
            print(f"No open positions found for {filter_underlying}")
            return

    W = 75

    print(f"\n{'='*W}")
    print(f"{'BUTTERFLY PORTFOLIO STATUS':^{W}}")
    print(f"{'='*W}")
    print(f"As of: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Open Positions: {len(positions)}")

    print(f"\nLoading token from {TOKEN_FILE}...")
    kite = load_kite_client()

    total_pnl = 0
    total_entry = 0
    results = []

    for position in positions:
        result = check_position(kite, position)
        print_position(result)
        results.append(result)

        if 'pnl' in result:
            total_pnl += result['pnl']
            total_entry += result['entry_debit']

    # Portfolio Summary
    if len(positions) >= 1:
        print(f"\n{'='*W}")
        print(f"{'PORTFOLIO SUMMARY':^{W}}")
        print(f"{'='*W}")
        print(f"\n+{'-'*73}+")
        print(f"| Total Positions:    {len(positions):<51} |")
        print(f"| Total Capital:      Rs {total_entry:>10,.2f}{' ':<46} |")
        print(f"| {'-'*71} |")

        total_pnl_pct = (total_pnl / total_entry) * 100 if total_entry else 0
        if total_pnl >= 0:
            print(f"| >>> TOTAL P&L:      Rs {total_pnl:>+10,.2f}  ({total_pnl_pct:>+6.1f}%){' ':<30} |")
        else:
            print(f"| >>> TOTAL P&L:      Rs {total_pnl:>+10,.2f}  ({total_pnl_pct:>+6.1f}%){' ':<30} |")
        print(f"+{'-'*73}+")
        print(f"\n{'='*W}")


if __name__ == "__main__":
    main()
