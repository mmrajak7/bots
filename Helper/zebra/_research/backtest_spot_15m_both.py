"""Backtest: Spot 15M ST -- EXIT on reversal + ENTRY on flip.

Two strategies tested against all magnet paper trades:

STRATEGY A -- EXIT RULE:
  Enter as current system does. But EXIT when spot 15M ST flips AGAINST
  our direction (instead of waiting for premium SL / spot SL / time SL).
  Simulated: fetch option 15M data, get option price at reversal time.

STRATEGY B -- ENTRY FILTER (wait for pullback->flip):
  Don't enter immediately. Wait for spot 15M to flip AGAINST our direction
  (pullback), then enter when it flips BACK (fresh momentum).
  Simulated: find flip sequence, get option price at re-flip time, track to TP/SL.

Both compared against ACTUAL results from paper trading.
"""

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HELPER = Path(__file__).resolve().parent
BOTS = HELPER.parent
sys.path.insert(0, str(HELPER))

from playbook.compute_st import compute_supertrend


def get_kite():
    from kiteconnect import KiteConnect
    token_path = BOTS / 'data' / 'kite_access_token.json'
    with open(token_path) as f:
        td = json.load(f)
    kite = KiteConnect(api_key=td['api_key'])
    kite.set_access_token(td['access_token'])
    return kite


def naive(dt):
    """Convert datetime to naive (no tz)."""
    if dt is None:
        return None
    if hasattr(dt, 'replace') and dt.tzinfo:
        return dt.replace(tzinfo=None)
    if isinstance(dt, str):
        return datetime.strptime(dt[:19], '%Y-%m-%dT%H:%M:%S')
    return dt


def fetch_15m(kite, nse_key, from_date_str, to_date_str):
    """Fetch 15M candles. nse_key like 'NSE:RELIANCE' or 'NFO:RELIANCE26APR1200CE'."""
    try:
        ltp_data = kite.ltp([nse_key])
        token = ltp_data[nse_key]['instrument_token']
    except Exception as e:
        return []

    try:
        candles = kite.historical_data(
            token, from_date_str, to_date_str, '15minute')
        return candles
    except Exception:
        return []


def compute_15m_st(candles):
    """Compute 15M ST(10,3) from candle list."""
    if not candles or len(candles) < 15:
        return []
    return compute_supertrend(candles, 10, 3)


def find_st_at(st_data, target_dt):
    """Find ST point at or just before target_dt (naive)."""
    best = None
    for pt in st_data:
        if naive(pt['date']) <= target_dt:
            best = pt
    return best


def find_option_price_at(opt_candles, target_dt):
    """Find the option's close price at the 15M candle closest to target_dt."""
    best = None
    for c in opt_candles:
        if naive(c['date']) <= target_dt:
            best = c
    if best:
        return best['close']
    return None


def find_first_reversal(st_data, after_dt, against_dir):
    """Find first time ST flips TO against_dir after after_dt."""
    for i in range(1, len(st_data)):
        pt_dt = naive(st_data[i]['date'])
        if pt_dt <= after_dt:
            continue
        if (st_data[i]['direction'] == against_dir and
                st_data[i - 1]['direction'] != against_dir):
            return st_data[i]
    return None


def find_pullback_then_reentry(st_data, after_dt, right_dir):
    """Find the pullback->re-entry pattern after signal:
    1. First, 15M must flip to WRONG direction (pullback)
    2. Then flip back to RIGHT direction (fresh entry signal)
    Returns (pullback_point, reentry_point) or (None, None).
    """
    wrong_dir = 'DOWN' if right_dir == 'UP' else 'UP'

    # Phase 1: find first flip to wrong direction
    pullback = None
    for i in range(1, len(st_data)):
        pt_dt = naive(st_data[i]['date'])
        if pt_dt <= after_dt:
            continue
        if (st_data[i]['direction'] == wrong_dir and
                st_data[i - 1]['direction'] != wrong_dir):
            pullback = st_data[i]
            break

    if not pullback:
        return None, None

    pullback_dt = naive(pullback['date'])

    # Phase 2: find flip back to right direction after pullback
    for i in range(1, len(st_data)):
        pt_dt = naive(st_data[i]['date'])
        if pt_dt <= pullback_dt:
            continue
        if (st_data[i]['direction'] == right_dir and
                st_data[i - 1]['direction'] != right_dir):
            return pullback, st_data[i]

    return pullback, None


def simulate_forward(spot_st_data, opt_candles, entry_dt, entry_opt_price,
                     target_spot, sl_spot, direction, max_days=5):
    """Simulate from entry to exit using:
    - Spot TP: spot reaches target_spot
    - Spot SL: spot hits sl_spot
    - 15M Exit: spot 15M flips against direction
    - Time SL: max_days trading days
    Returns (exit_reason, exit_opt_price, exit_dt, days_held).
    """
    right_dir = 'UP' if direction == 'CE' else 'DOWN'
    against_dir = 'DOWN' if direction == 'CE' else 'UP'

    deadline = entry_dt + timedelta(days=max_days + 2)  # buffer for weekends
    last_opt_price = entry_opt_price
    prev_dir = right_dir

    for i in range(1, len(spot_st_data)):
        pt = spot_st_data[i]
        pt_dt = naive(pt['date'])

        if pt_dt <= entry_dt:
            continue
        if pt_dt > deadline:
            break

        spot = pt['close']
        opt_price = find_option_price_at(opt_candles, pt_dt)
        if opt_price and opt_price > 0:
            last_opt_price = opt_price

        # TP check
        if direction == 'CE' and spot >= target_spot:
            return 'sim_tp', last_opt_price, pt_dt, (pt_dt - entry_dt).days
        if direction == 'PE' and spot <= target_spot:
            return 'sim_tp', last_opt_price, pt_dt, (pt_dt - entry_dt).days

        # SL check
        if direction == 'CE' and spot <= sl_spot:
            return 'sim_sl_spot', last_opt_price, pt_dt, (pt_dt - entry_dt).days
        if direction == 'PE' and spot >= sl_spot:
            return 'sim_sl_spot', last_opt_price, pt_dt, (pt_dt - entry_dt).days

        # 15M reversal exit
        if pt['direction'] == against_dir and prev_dir == right_dir:
            return 'sim_15m_exit', last_opt_price, pt_dt, (pt_dt - entry_dt).days

        prev_dir = pt['direction']

    # Time SL
    return 'sim_time_sl', last_opt_price, deadline, max_days


def main():
    print("=" * 90)
    print("BACKTEST: Spot 15M ST -- EXIT on reversal + ENTRY on pullback-flip")
    print("=" * 90)

    with open(HELPER / 'logs' / 'magnet_trades.json') as f:
        all_trades = json.load(f)

    # Exited trades with P&L (real results to compare against)
    trades = [t for t in all_trades
              if t.get('entry_date') and t.get('entry_spot')
              and t.get('option_symbol')
              and t.get('option_premium', 0) > 0
              and t['status'] == 'exited'
              and t.get('pnl') is not None
              and 'cleanup' not in (t.get('exit_reason') or '').lower()]

    # Also include open trades for "until today" simulation
    open_trades = [t for t in all_trades
                   if t.get('entry_date') and t.get('option_symbol')
                   and t['status'] == 'entered'
                   and t.get('option_premium', 0) > 0]

    print(f"\n  Exited trades with P&L: {len(trades)}")
    print(f"  Open trades (simulate to today): {len(open_trades)}")
    print(f"\n  Fetching spot + option 15M data...\n")

    kite = get_kite()

    # Data cache: stock -> spot 15M candles + ST
    spot_cache = {}   # stock -> (candles, st_data)
    opt_cache = {}    # option_symbol -> candles

    def get_spot_data(stock, sig_date):
        if stock in spot_cache:
            return spot_cache[stock]
        from_d = (datetime.strptime(sig_date, '%Y-%m-%d') - timedelta(days=17)).strftime('%Y-%m-%d')
        to_d = datetime.now().strftime('%Y-%m-%d')
        print(f"    Fetching spot 15M: {stock} ({from_d} to {to_d})")
        candles = fetch_15m(kite, f'NSE:{stock}', from_d, to_d)
        st = compute_15m_st(candles)
        spot_cache[stock] = (candles, st)
        time.sleep(0.35)
        return candles, st

    def get_opt_data(opt_sym, sig_date):
        if opt_sym in opt_cache:
            return opt_cache[opt_sym]
        from_d = (datetime.strptime(sig_date, '%Y-%m-%d') - timedelta(days=5)).strftime('%Y-%m-%d')
        to_d = datetime.now().strftime('%Y-%m-%d')
        print(f"    Fetching opt  15M: {opt_sym}")
        candles = fetch_15m(kite, f'NFO:{opt_sym}', from_d, to_d)
        opt_cache[opt_sym] = candles
        time.sleep(0.35)
        return candles

    # =========================================================================
    # STRATEGY A: EXIT on spot 15M reversal
    # =========================================================================
    print("=" * 90)
    print("STRATEGY A: EXIT when spot 15M reverses against our direction")
    print("=" * 90)

    strat_a_results = []

    for t in trades + open_trades:
        stock = t['stock']
        direction = t['direction']
        right_dir = 'UP' if direction == 'CE' else 'DOWN'
        against_dir = 'DOWN' if direction == 'CE' else 'UP'
        opt_sym = t['option_symbol']
        entry_premium = t.get('option_premium', 0)
        qty = t.get('quantity', 0)
        actual_pnl = t.get('pnl', 0) or 0

        try:
            ent_dt = datetime.strptime(
                f"{t['entry_date']} {(t.get('entry_time') or '09:30:00')[:8]}",
                '%Y-%m-%d %H:%M:%S')
        except Exception:
            ent_dt = datetime.strptime(f"{t['entry_date']} 09:30:00", '%Y-%m-%d %H:%M:%S')

        # Get data
        _, spot_st = get_spot_data(stock, t['signal_date'])
        opt_candles = get_opt_data(opt_sym, t['signal_date'])

        if not spot_st:
            print(f"  #{t['id']:3d} {stock:15s} SKIP (no spot data)")
            continue

        # Find first spot 15M reversal after entry
        reversal = find_first_reversal(spot_st, ent_dt, against_dir)

        if reversal:
            rev_dt = naive(reversal['date'])
            rev_opt_price = find_option_price_at(opt_candles, rev_dt)

            if rev_opt_price is not None and rev_opt_price > 0:
                # Simulate: exit at reversal
                if direction in ('CE', 'PE'):
                    # Both CE and PE: we buy option, so P&L = (exit - entry) * qty
                    sim_pnl = (rev_opt_price - entry_premium) * qty
                else:
                    sim_pnl = actual_pnl  # fallback

                hours_to_rev = (rev_dt - ent_dt).total_seconds() / 3600
                improvement = sim_pnl - actual_pnl

                strat_a_results.append({
                    'id': t['id'], 'stock': stock, 'direction': direction,
                    'entry_date': t['entry_date'],
                    'actual_pnl': actual_pnl,
                    'sim_pnl': sim_pnl,
                    'improvement': improvement,
                    'rev_time': rev_dt.strftime('%m-%d %H:%M'),
                    'hours_to_rev': hours_to_rev,
                    'entry_premium': entry_premium,
                    'exit_premium_sim': rev_opt_price,
                    'exit_premium_actual': t.get('exit_premium', 0),
                    'qty': qty,
                    'status': t['status'],
                    'actual_exit': t.get('exit_reason', 'open'),
                })

                tag = f"rev@{rev_dt.strftime('%m-%d %H:%M')} {hours_to_rev:.1f}h"
                print(f"  #{t['id']:3d} {stock:15s} {direction} "
                      f"actual={actual_pnl:>8,.0f} sim={sim_pnl:>8,.0f} "
                      f"delta={improvement:>+8,.0f} {tag}")
            else:
                # No option price at reversal time (likely expired)
                strat_a_results.append({
                    'id': t['id'], 'stock': stock, 'direction': direction,
                    'entry_date': t['entry_date'],
                    'actual_pnl': actual_pnl,
                    'sim_pnl': actual_pnl,
                    'improvement': 0,
                    'rev_time': rev_dt.strftime('%m-%d %H:%M'),
                    'hours_to_rev': (rev_dt - ent_dt).total_seconds() / 3600,
                    'entry_premium': entry_premium,
                    'exit_premium_sim': None,
                    'exit_premium_actual': t.get('exit_premium', 0),
                    'qty': qty,
                    'status': t['status'],
                    'actual_exit': t.get('exit_reason', 'open'),
                })
                print(f"  #{t['id']:3d} {stock:15s} {direction} "
                      f"actual={actual_pnl:>8,.0f} (no opt price at rev)")
        else:
            # No reversal found -- trade would proceed as normal
            strat_a_results.append({
                'id': t['id'], 'stock': stock, 'direction': direction,
                'entry_date': t['entry_date'],
                'actual_pnl': actual_pnl,
                'sim_pnl': actual_pnl,
                'improvement': 0,
                'rev_time': None,
                'hours_to_rev': None,
                'entry_premium': entry_premium,
                'exit_premium_sim': t.get('exit_premium', 0),
                'exit_premium_actual': t.get('exit_premium', 0),
                'qty': qty,
                'status': t['status'],
                'actual_exit': t.get('exit_reason', 'open'),
            })
            print(f"  #{t['id']:3d} {stock:15s} {direction} "
                  f"actual={actual_pnl:>8,.0f} (no reversal, same exit)")

    # =========================================================================
    # STRATEGY B: ENTRY on pullback->flip
    # =========================================================================
    print(f"\n{'=' * 90}")
    print("STRATEGY B: ENTER only after pullback -> re-flip on spot 15M")
    print("  (wait for wrong direction, then re-flip to right direction)")
    print("=" * 90)

    # For Strategy B, we look at ALL signals (not just entered ones)
    all_signals = [t for t in all_trades
                   if t.get('signal_date') and t.get('option_symbol')
                   and t.get('option_premium', 0) > 0
                   and t.get('pnl') is not None
                   and 'cleanup' not in (t.get('exit_reason') or '').lower()
                   and t.get('entry_date')]

    strat_b_results = []

    for t in all_signals:
        stock = t['stock']
        direction = t['direction']
        right_dir = 'UP' if direction == 'CE' else 'DOWN'
        opt_sym = t['option_symbol']
        entry_premium = t.get('option_premium', 0)
        actual_pnl = t.get('pnl', 0) or 0
        qty = t.get('quantity', 0)
        target_spot = t.get('target_spot', 0)
        sl_spot = t.get('sl_spot', 0)

        try:
            sig_dt = datetime.strptime(
                f"{t['signal_date']} {(t.get('signal_time') or '09:15:00')[:8]}",
                '%Y-%m-%d %H:%M:%S')
        except Exception:
            sig_dt = datetime.strptime(f"{t['signal_date']} 09:15:00", '%Y-%m-%d %H:%M:%S')

        _, spot_st = get_spot_data(stock, t['signal_date'])
        opt_candles = get_opt_data(opt_sym, t['signal_date'])

        if not spot_st:
            continue

        # Find pullback->re-entry pattern
        pullback, reentry = find_pullback_then_reentry(spot_st, sig_dt, right_dir)

        if reentry:
            reentry_dt = naive(reentry['date'])
            reentry_opt_price = find_option_price_at(opt_candles, reentry_dt)

            if reentry_opt_price and reentry_opt_price > 0:
                # Simulate forward from re-entry point using same TP/SL
                sim_exit_reason, sim_exit_opt, sim_exit_dt, sim_days = simulate_forward(
                    spot_st, opt_candles, reentry_dt, reentry_opt_price,
                    target_spot, sl_spot, direction, max_days=5)

                sim_pnl = (sim_exit_opt - reentry_opt_price) * qty if sim_exit_opt else 0
                wait_hours = (reentry_dt - sig_dt).total_seconds() / 3600

                pb_dt = naive(pullback['date'])
                strat_b_results.append({
                    'id': t['id'], 'stock': stock, 'direction': direction,
                    'signal_date': t['signal_date'],
                    'actual_pnl': actual_pnl,
                    'sim_pnl': sim_pnl,
                    'improvement': sim_pnl - actual_pnl,
                    'actual_entry_premium': entry_premium,
                    'sim_entry_premium': reentry_opt_price,
                    'sim_exit_premium': sim_exit_opt,
                    'sim_exit_reason': sim_exit_reason,
                    'pullback_time': pb_dt.strftime('%m-%d %H:%M'),
                    'reentry_time': reentry_dt.strftime('%m-%d %H:%M'),
                    'wait_hours': wait_hours,
                    'qty': qty,
                    'actual_exit': t.get('exit_reason', '?'),
                })

                print(f"  #{t['id']:3d} {stock:15s} {direction} "
                      f"actual={actual_pnl:>8,.0f} sim={sim_pnl:>8,.0f} "
                      f"delta={sim_pnl - actual_pnl:>+8,.0f} "
                      f"wait={wait_hours:.0f}h "
                      f"pb@{pb_dt.strftime('%m-%d %H:%M')} "
                      f"re@{reentry_dt.strftime('%m-%d %H:%M')} "
                      f"exit={sim_exit_reason}")
            else:
                # Option expired/no data at re-entry time
                strat_b_results.append({
                    'id': t['id'], 'stock': stock, 'direction': direction,
                    'signal_date': t['signal_date'],
                    'actual_pnl': actual_pnl,
                    'sim_pnl': 0,
                    'improvement': -actual_pnl,
                    'sim_entry_premium': None,
                    'sim_exit_reason': 'no_opt_data',
                    'pullback_time': naive(pullback['date']).strftime('%m-%d %H:%M'),
                    'reentry_time': reentry_dt.strftime('%m-%d %H:%M'),
                    'wait_hours': (reentry_dt - sig_dt).total_seconds() / 3600,
                    'qty': qty,
                    'actual_exit': t.get('exit_reason', '?'),
                })
                print(f"  #{t['id']:3d} {stock:15s} {direction} "
                      f"actual={actual_pnl:>8,.0f} (no opt data at re-entry)")
        elif pullback and not reentry:
            # Pullback happened but never flipped back -> SKIP trade
            pb_dt = naive(pullback['date'])
            strat_b_results.append({
                'id': t['id'], 'stock': stock, 'direction': direction,
                'signal_date': t['signal_date'],
                'actual_pnl': actual_pnl,
                'sim_pnl': 0,
                'improvement': -actual_pnl,
                'sim_exit_reason': 'skipped_no_reflip',
                'pullback_time': pb_dt.strftime('%m-%d %H:%M'),
                'reentry_time': None,
                'wait_hours': None,
                'qty': qty,
                'actual_exit': t.get('exit_reason', '?'),
            })
            print(f"  #{t['id']:3d} {stock:15s} {direction} "
                  f"actual={actual_pnl:>8,.0f} SKIP (pullback but no re-flip)")
        else:
            # No pullback found -> no entry signal fires, skip trade
            strat_b_results.append({
                'id': t['id'], 'stock': stock, 'direction': direction,
                'signal_date': t['signal_date'],
                'actual_pnl': actual_pnl,
                'sim_pnl': 0,
                'improvement': -actual_pnl,
                'sim_exit_reason': 'skipped_no_pullback',
                'pullback_time': None,
                'reentry_time': None,
                'wait_hours': None,
                'qty': qty,
                'actual_exit': t.get('exit_reason', '?'),
            })
            print(f"  #{t['id']:3d} {stock:15s} {direction} "
                  f"actual={actual_pnl:>8,.0f} SKIP (no pullback found, trend held)")

    # =========================================================================
    # COMBINED RESULTS
    # =========================================================================
    print(f"\n{'=' * 90}")
    print("RESULTS SUMMARY")
    print("=" * 90)

    # Strategy A summary
    a_actual = sum(r['actual_pnl'] for r in strat_a_results)
    a_sim = sum(r['sim_pnl'] for r in strat_a_results)
    a_improved = [r for r in strat_a_results if r['improvement'] > 0]
    a_worse = [r for r in strat_a_results if r['improvement'] < 0]
    a_same = [r for r in strat_a_results if r['improvement'] == 0]

    a_sim_winners = [r for r in strat_a_results if r['sim_pnl'] > 0]
    a_sim_losers = [r for r in strat_a_results if r['sim_pnl'] < 0]
    a_act_winners = [r for r in strat_a_results if r['actual_pnl'] > 0]

    print(f"\n--- STRATEGY A: Exit on Spot 15M Reversal ---")
    print(f"  Trades analyzed:    {len(strat_a_results)}")
    print(f"  Actual total P&L:   Rs {a_actual:>12,.0f}")
    print(f"  Simulated P&L:      Rs {a_sim:>12,.0f}")
    print(f"  IMPROVEMENT:        Rs {a_sim - a_actual:>+12,.0f}")
    print(f"  Actual win rate:    {len(a_act_winners)}/{len(strat_a_results)} "
          f"= {len(a_act_winners)/len(strat_a_results)*100:.0f}%")
    print(f"  Sim win rate:       {len(a_sim_winners)}/{len(strat_a_results)} "
          f"= {len(a_sim_winners)/len(strat_a_results)*100:.0f}%")
    print(f"  Better than actual: {len(a_improved)} trades")
    print(f"  Worse than actual:  {len(a_worse)} trades")
    print(f"  Same:               {len(a_same)} trades")

    # Show individual improvements
    print(f"\n  {'Stock':15s} {'Dir':4s} {'Actual':>10s} {'Sim':>10s} {'Delta':>10s} "
          f"{'Rev@':>12s} {'Hrs':>6s} {'ActExit':>15s}")
    print(f"  {'-'*15} {'-'*4} {'-'*10} {'-'*10} {'-'*10} {'-'*12} {'-'*6} {'-'*15}")
    for r in sorted(strat_a_results, key=lambda x: x['improvement'], reverse=True):
        rev_t = r['rev_time'] or '-'
        hrs = f"{r['hours_to_rev']:.0f}h" if r['hours_to_rev'] else '-'
        act_exit = (r['actual_exit'] or '?')[:15]
        print(f"  {r['stock']:15s} {r['direction']:4s} "
              f"Rs {r['actual_pnl']:>8,.0f} Rs {r['sim_pnl']:>8,.0f} "
              f"Rs {r['improvement']:>+8,.0f} {rev_t:>12s} {hrs:>6s} {act_exit:>15s}")

    # Strategy B summary
    print(f"\n--- STRATEGY B: Enter on Pullback->Re-flip ---")
    b_actual = sum(r['actual_pnl'] for r in strat_b_results)
    b_sim = sum(r['sim_pnl'] for r in strat_b_results)
    b_entered = [r for r in strat_b_results if r.get('sim_exit_reason', '').startswith('sim_')]
    b_skipped = [r for r in strat_b_results if 'skipped' in r.get('sim_exit_reason', '')]
    b_nodata = [r for r in strat_b_results if r.get('sim_exit_reason') == 'no_opt_data']

    b_sim_winners = [r for r in strat_b_results if r['sim_pnl'] > 0]
    b_act_winners = [r for r in strat_b_results if r['actual_pnl'] > 0]

    print(f"  Trades analyzed:    {len(strat_b_results)}")
    print(f"  Would have entered: {len(b_entered)}")
    print(f"  Would have SKIPPED: {len(b_skipped)} (no pullback or no re-flip)")
    print(f"  No option data:     {len(b_nodata)}")
    print(f"  Actual total P&L:   Rs {b_actual:>12,.0f}")
    print(f"  Simulated P&L:      Rs {b_sim:>12,.0f}")
    print(f"  IMPROVEMENT:        Rs {b_sim - b_actual:>+12,.0f}")

    if b_entered:
        b_ent_pnl = sum(r['sim_pnl'] for r in b_entered)
        b_ent_wins = [r for r in b_entered if r['sim_pnl'] > 0]
        print(f"  Entered sim P&L:    Rs {b_ent_pnl:>12,.0f} ({len(b_entered)} trades)")
        print(f"  Entered win rate:   {len(b_ent_wins)}/{len(b_entered)} "
              f"= {len(b_ent_wins)/len(b_entered)*100:.0f}%")

    if b_skipped:
        skip_actual_pnl = sum(r['actual_pnl'] for r in b_skipped)
        print(f"  Skipped trades' actual P&L: Rs {skip_actual_pnl:>12,.0f} (avoided)")

    print(f"\n  {'Stock':15s} {'Dir':4s} {'Actual':>10s} {'Sim':>10s} {'Delta':>10s} "
          f"{'Wait':>6s} {'SimExit':>18s}")
    print(f"  {'-'*15} {'-'*4} {'-'*10} {'-'*10} {'-'*10} {'-'*6} {'-'*18}")
    for r in sorted(strat_b_results, key=lambda x: x['improvement'], reverse=True):
        wait = f"{r['wait_hours']:.0f}h" if r.get('wait_hours') else '-'
        sim_exit = (r.get('sim_exit_reason') or '?')[:18]
        print(f"  {r['stock']:15s} {r['direction']:4s} "
              f"Rs {r['actual_pnl']:>8,.0f} Rs {r['sim_pnl']:>8,.0f} "
              f"Rs {r['improvement']:>+8,.0f} {wait:>6s} {sim_exit:>18s}")

    # =========================================================================
    # FINAL COMPARISON
    # =========================================================================
    print(f"\n{'=' * 90}")
    print("FINAL COMPARISON: Current vs Strategy A vs Strategy B")
    print("=" * 90)

    print(f"\n  {'':30s} {'CURRENT':>15s} {'A: 15M Exit':>15s} {'B: Flip Entry':>15s}")
    print(f"  {'-'*30} {'-'*15} {'-'*15} {'-'*15}")
    print(f"  {'Total P&L':30s} Rs {a_actual:>12,.0f} Rs {a_sim:>12,.0f} Rs {b_sim:>12,.0f}")
    print(f"  {'Trades taken':30s} {len(strat_a_results):>15d} "
          f"{len(strat_a_results):>15d} {len(b_entered):>15d}")
    print(f"  {'Win rate':30s} "
          f"{len(a_act_winners)/max(len(strat_a_results),1)*100:>14.0f}% "
          f"{len(a_sim_winners)/max(len(strat_a_results),1)*100:>14.0f}% "
          f"{len(b_ent_wins)/max(len(b_entered),1)*100 if b_entered else 0:>14.0f}%")
    print(f"  {'vs Current':30s} {'baseline':>15s} "
          f"Rs {a_sim - a_actual:>+11,.0f} Rs {b_sim - b_actual:>+11,.0f}")

    # Save
    output = {
        'run_date': datetime.now().isoformat(),
        'strategy_a': strat_a_results,
        'strategy_b': strat_b_results,
        'summary': {
            'current_pnl': a_actual,
            'strat_a_pnl': a_sim,
            'strat_b_pnl': b_sim,
            'strat_a_improvement': a_sim - a_actual,
            'strat_b_improvement': b_sim - b_actual,
        }
    }

    out_path = HELPER / 'logs' / 'backtest_spot_15m_both.json'
    with open(out_path, 'w') as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Results saved to {out_path}")


if __name__ == '__main__':
    main()
