"""Multi-level entry simulation: 2% / 3% / 4% / 5% gap entries.

For each weekly+monthly alert, backtrack daily candles to find when the stock
first reached each gap level. Compute captured move (% from entry to last close
or target) at each level. Compare side-by-side.
"""
import csv, json, time
from datetime import datetime, timedelta
from pathlib import Path
from kiteconnect import KiteConnect

HERE = Path(__file__).resolve().parent
TOKEN_PATH = HERE.parent / 'data' / 'kite_access_token.json'

GAP_LEVELS = [0.02, 0.03, 0.04, 0.05]  # 2%, 3%, 4%, 5%
LOOKBACK_DAYS = 45


def load_kite():
    with open(TOKEN_PATH) as f:
        tok = json.load(f)
    k = KiteConnect(api_key=tok['api_key'])
    k.set_access_token(tok['access_token'])
    return k


def find_entry_at_gap(candles_before, target, side, gap_pct):
    """Most RECENT date in lookback where intraday range crossed the gap level
    on the approach toward target. This represents the natural 'earlier entry'
    in the same approach phase that led to the signal.

    Iterates candles in REVERSE (newest to oldest), returns first match.
    """
    for c in reversed(candles_before):
        if side == 'above':  # PE: target below signal, looking for price above target
            high_gap = (c['high'] - target) / target
            low_gap = (c['low'] - target) / target
            # Did the intraday range cross the gap_pct level?
            if low_gap <= gap_pct <= high_gap:
                return target * (1 + gap_pct), c['date'].date()
        else:  # CE: target above signal, looking for price below target
            high_gap = (target - c['low']) / target
            low_gap = (target - c['high']) / target
            if low_gap <= gap_pct <= high_gap:
                return target * (1 - gap_pct), c['date'].date()
    return None, None


def compute_move_pct(entry_price, end_price, side):
    """% move from entry to end. PE wants price down (positive=good), CE wants up."""
    if side == 'above':  # PE
        return (entry_price - end_price) / entry_price * 100
    else:  # CE
        return (end_price - entry_price) / entry_price * 100


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
        to_d = datetime.now().date()

        candles = None
        for attempt in range(3):
            try:
                time.sleep(0.4 * (attempt + 1))
                candles = kite.historical_data(token, from_d.strftime('%Y-%m-%d'),
                                               to_d.strftime('%Y-%m-%d'), 'day')
                break
            except Exception as e:
                if attempt == 2:
                    print(f'  [{i}/{len(rows)}] {stock}: fetch failed after 3 tries')
        if candles is None:
            continue

        for c in candles:
            if isinstance(c['date'], str):
                c['date'] = datetime.fromisoformat(c['date'].replace('+05:30', ''))

        before = [c for c in candles if c['date'].date() < sig_date]
        # End price: if hit, use target; else last close
        if r['status'] == 'TARGET_HIT':
            end_price = target
        else:
            after = [c for c in candles if c['date'].date() > sig_date]
            end_price = after[-1]['close'] if after else signal_price

        # For each gap level, find first entry date+price + compute move
        entry_results = {}
        for gap_pct in GAP_LEVELS:
            ep, ed = find_entry_at_gap(before, target, side, gap_pct)
            if ep is None and gap_pct == 0.02:
                # 2% baseline: use signal_price (alert entry point)
                ep, ed = signal_price, sig_date
            if ep is not None:
                move = compute_move_pct(ep, end_price, side)
                days_earlier = (sig_date - ed).days if ed else 0
                entry_results[gap_pct] = {'price': round(ep, 2), 'date': ed.isoformat() if ed else '',
                                          'move_pct': round(move, 2), 'days_earlier': days_earlier}
            else:
                entry_results[gap_pct] = None

        sim_rows.append({**r, 'end_price': round(end_price, 2),
                         'entry_2pct': entry_results.get(0.02),
                         'entry_3pct': entry_results.get(0.03),
                         'entry_4pct': entry_results.get(0.04),
                         'entry_5pct': entry_results.get(0.05)})

        if i % 15 == 0:
            print(f'  processed {i}/{len(rows)}')

    # Stats per gap level
    print('\n=== ENTRY LEVEL COMPARISON (n=64 weekly+monthly alerts) ===')
    print()
    print('Level   Coverage  Avg days early  Hits avg %  Losers avg %  Net total %')
    print('-' * 75)

    for gap_pct in GAP_LEVELS:
        key = f'entry_{int(gap_pct*100)}pct'
        results = [r[key] for r in sim_rows if r[key]]
        if not results:
            continue
        coverage = len(results)
        avg_days = sum(rr['days_earlier'] for rr in results) / coverage

        # Split by hit vs not
        winners_moves = []
        losers_moves = []
        for r in sim_rows:
            if not r[key]: continue
            mv = r[key]['move_pct']
            if r['status'] == 'TARGET_HIT':
                winners_moves.append(mv)
            elif r['status'] in ('DRIFTING','DEVIATED'):
                losers_moves.append(mv)

        wavg = sum(winners_moves)/len(winners_moves) if winners_moves else 0
        lavg = sum(losers_moves)/len(losers_moves) if losers_moves else 0
        total = sum(winners_moves) + sum(losers_moves)

        print(f'{int(gap_pct*100)}%      {coverage:>4}/64    {avg_days:>10.1f} days  {wavg:>10.2f}%  {lavg:>10.2f}%  {total:>+11.1f}%')

    # Net P&L delta vs 2% baseline
    print()
    print('=== DELTA vs 2% BASELINE (per alert) ===')
    base_key = 'entry_2pct'
    for gap_pct in GAP_LEVELS[1:]:
        key = f'entry_{int(gap_pct*100)}pct'
        deltas = []
        coverage = 0
        for r in sim_rows:
            if r[key] and r[base_key]:
                deltas.append(r[key]['move_pct'] - r[base_key]['move_pct'])
                coverage += 1
        if deltas:
            print(f'  {int(gap_pct*100)}% vs 2%:  n={coverage}  avg delta {sum(deltas)/len(deltas):+.2f}%/trade  total {sum(deltas):+.1f}%')

    # Save full per-alert breakdown
    out_csv = Path('logs/magnet_entry_levels_sim.csv')
    flat = []
    for r in sim_rows:
        row = {k: v for k, v in r.items() if not k.startswith('entry_')}
        for gap_pct in GAP_LEVELS:
            key = f'entry_{int(gap_pct*100)}pct'
            v = r.get(key)
            if v:
                row[f'{key}_price'] = v['price']
                row[f'{key}_date'] = v['date']
                row[f'{key}_move_pct'] = v['move_pct']
                row[f'{key}_days_earlier'] = v['days_earlier']
        flat.append(row)
    if flat:
        with open(out_csv, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(flat[0].keys()))
            w.writeheader(); w.writerows(flat)
        print(f'\nSaved per-alert breakdown to {out_csv}')


if __name__ == '__main__':
    main()
