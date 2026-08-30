"""Simulate: what if magnet entered at 4-5% gap instead of 2%?

For each weekly+monthly alert that fired at ~2-3% gap, look backwards to find
when the stock was at 4-5% gap. Compute:
  - Extra move captured (if winner)
  - Extra drawdown (if loser drifted further)

Output: aggregate P&L delta if we had widened the watch window.
"""
import csv, json, time
from datetime import datetime, timedelta
from pathlib import Path
from kiteconnect import KiteConnect

HERE = Path(__file__).resolve().parent
TOKEN_PATH = HERE.parent / 'data' / 'kite_access_token.json'

# Target wider-entry gap band
WIDER_ENTRY_LO = 0.04  # 4%
WIDER_ENTRY_HI = 0.05  # 5%
LOOKBACK_DAYS = 30


def load_kite():
    with open(TOKEN_PATH) as f:
        tok = json.load(f)
    k = KiteConnect(api_key=tok['api_key'])
    k.set_access_token(tok['access_token'])
    return k


def main():
    rows = list(csv.DictReader(open('logs/magnet_alert_analysis_v2.csv')))
    print(f'Total weekly+monthly alerts: {len(rows)}')

    kite = load_kite()
    insts = kite.instruments('NSE')
    inst_cache = {i['tradingsymbol']: i['instrument_token']
                  for i in insts if i.get('segment') == 'NSE'}

    sim_rows = []
    for i, r in enumerate(rows, 1):
        stock = r['stock']
        token = inst_cache.get(stock)
        if not token:
            continue

        side = r['side']
        sig_date = datetime.fromisoformat(r['signal_date']).date()
        target = float(r['target_spot'])
        signal_price = float(r['signal_price'])

        from_d = sig_date - timedelta(days=LOOKBACK_DAYS)
        to_d = sig_date + timedelta(days=1)  # include signal day

        try:
            time.sleep(0.4)  # rate limit
            candles = kite.historical_data(token, from_d.strftime('%Y-%m-%d'),
                                           to_d.strftime('%Y-%m-%d'), 'day')
        except Exception as e:
            print(f'  [{i}/{len(rows)}] {stock}: fetch failed {e}')
            continue

        for c in candles:
            if isinstance(c['date'], str):
                c['date'] = datetime.fromisoformat(c['date'].replace('+05:30', ''))

        # Find the earliest candle in lookback where gap was within 4-5%
        # (both high and low define the intraday gap range)
        earlier_entry_price = None
        earlier_entry_date = None
        for c in candles:
            if c['date'].date() >= sig_date:
                break
            if side == 'above':
                # PE: target below signal — gap positive means price above target
                high_gap = (c['high'] - target) / target
                low_gap = (c['low'] - target) / target
                # Did intraday range touch the 4-5% band?
                if WIDER_ENTRY_LO <= max(high_gap, low_gap) and min(high_gap, low_gap) <= WIDER_ENTRY_HI:
                    # Pick a price within the band — use close if in range, else midpoint
                    desired = target * (1 + (WIDER_ENTRY_LO + WIDER_ENTRY_HI) / 2)
                    earlier_entry_price = desired
                    earlier_entry_date = c['date'].date()
                    break
            else:  # 'below'
                # CE: target above signal — gap positive means target above price
                high_gap = (target - c['low']) / target
                low_gap = (target - c['high']) / target
                if WIDER_ENTRY_LO <= max(high_gap, low_gap) and min(high_gap, low_gap) <= WIDER_ENTRY_HI:
                    desired = target * (1 - (WIDER_ENTRY_LO + WIDER_ENTRY_HI) / 2)
                    earlier_entry_price = desired
                    earlier_entry_date = c['date'].date()
                    break

        if earlier_entry_price is None:
            # No 4-5% touch in lookback → wider entry wouldn't have helped/hurt differently
            sim_rows.append({**r, 'wider_entry_price': '', 'wider_entry_date': '',
                             'old_move_pct': '', 'wider_move_pct': '',
                             'delta_move_pct': '', 'earlier_days': ''})
            continue

        # Move captured at current ENTRY (2% gap) = signal_price → target (or last_close)
        old_end = target if r['status'] == 'TARGET_HIT' else float(r['last_close']) if r['last_close'] else signal_price
        if side == 'above':
            old_move = (signal_price - old_end) / signal_price * 100  # PE: price falls
            wider_move = (earlier_entry_price - old_end) / earlier_entry_price * 100
        else:
            old_move = (old_end - signal_price) / signal_price * 100  # CE: price rises
            wider_move = (old_end - earlier_entry_price) / earlier_entry_price * 100

        delta = wider_move - old_move
        earlier_days = (sig_date - earlier_entry_date).days

        sim_rows.append({**r,
                         'wider_entry_price': round(earlier_entry_price, 2),
                         'wider_entry_date': earlier_entry_date.isoformat(),
                         'old_move_pct': round(old_move, 2),
                         'wider_move_pct': round(wider_move, 2),
                         'delta_move_pct': round(delta, 2),
                         'earlier_days': earlier_days})

        if i % 10 == 0:
            print(f'  processed {i}/{len(rows)}')

    # Save simulation
    out_csv = Path('logs/magnet_wider_entry_sim.csv')
    with open(out_csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(sim_rows[0].keys()))
        w.writeheader(); w.writerows(sim_rows)

    # Stats
    has_earlier = [r for r in sim_rows if r['wider_entry_date']]
    winners = [r for r in has_earlier if r['status'] == 'TARGET_HIT']
    losers = [r for r in has_earlier if r['status'] in ('DRIFTING', 'DEVIATED')]
    pending = [r for r in has_earlier if r['status'] == 'PENDING']

    print(f'\n=== WIDER ENTRY (4-5% gap) SIMULATION ===')
    print(f'Total weekly+monthly alerts: {len(sim_rows)}')
    print(f'Had 4-5% window before 2% signal: {len(has_earlier)}')
    print(f'  → {len(winners)} winners, {len(losers)} losers, {len(pending)} pending')
    print()

    if winners:
        wd = [float(r['delta_move_pct']) for r in winners]
        ed = [int(r['earlier_days']) for r in winners]
        print(f'WINNERS (n={len(winners)}): if entered earlier at 4-5% gap:')
        print(f'  extra move: avg +{sum(wd)/len(wd):.2f}%  total +{sum(wd):.1f}%')
        print(f'  earlier entry: avg {sum(ed)/len(ed):.1f} days before 2% signal')

    if losers:
        ld = [float(r['delta_move_pct']) for r in losers]
        print(f'\nLOSERS (n={len(losers)}): if entered earlier at 4-5% gap:')
        print(f'  delta move: avg {sum(ld)/len(ld):+.2f}%  total {sum(ld):+.1f}%')

    if pending:
        pd_ = [float(r['delta_move_pct']) for r in pending]
        print(f'\nPENDING (n={len(pending)}): delta so far:')
        print(f'  delta move: avg {sum(pd_)/len(pd_):+.2f}%  total {sum(pd_):+.1f}%')

    # Net impact
    all_deltas = [float(r['delta_move_pct']) for r in has_earlier]
    print(f'\nAGGREGATE across all {len(has_earlier)} alerts:')
    print(f'  net move delta: {sum(all_deltas):+.1f}% (sum of individual move%)')
    print(f'  avg per alert:  {sum(all_deltas)/len(all_deltas):+.2f}%')


if __name__ == '__main__':
    main()
