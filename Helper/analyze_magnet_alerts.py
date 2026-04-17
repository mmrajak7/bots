"""
Magnet WATCH alert analysis (spot only, no options).

For every WATCH alert ever fired, answer:
- When did it fire? At what price? What was the ST target?
- Did spot reach the ST target? If yes, how many days?
- If no, how far did it deviate (adverse move away from target)?

Dedupe: (stock, timeframe, signal_date) — same stock can re-alert across timeframes.
"""
import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from kiteconnect import KiteConnect


HERE = Path(__file__).resolve().parent
TOKEN_PATH = HERE.parent / 'data' / 'kite_access_token.json'
TRADES_PATH = HERE / 'logs' / 'magnet_trades.json'
OUT_CSV = HERE / 'logs' / 'magnet_alert_analysis.csv'

TODAY = datetime.now().date()


def load_kite():
    with open(TOKEN_PATH) as f:
        tok = json.load(f)
    k = KiteConnect(api_key=tok['api_key'])
    k.set_access_token(tok['access_token'])
    return k


def build_instrument_cache(kite):
    insts = kite.instruments('NSE')
    return {i['tradingsymbol']: i['instrument_token']
            for i in insts if i.get('segment') == 'NSE'}


def fetch_daily(kite, token, from_d, to_d):
    """Daily OHLC, list of dicts."""
    return kite.historical_data(token,
                                from_d.strftime('%Y-%m-%d'),
                                to_d.strftime('%Y-%m-%d'),
                                'day')


def analyze_alert(alert, candles_after):
    """Return dict with hit_date, days_to_target, max_adverse_pct, status."""
    side = alert['side']               # 'above' or 'below'
    signal_price = alert['signal_price']
    target = alert['target_spot']

    # Determine if/when target was hit
    hit_date = None
    days_to_target = None
    max_adverse_pct = 0.0
    last_close = None

    for c in candles_after:
        cdate = c['date'].date() if hasattr(c['date'], 'date') else \
                datetime.fromisoformat(str(c['date']).replace('+05:30', '')).date()
        last_close = c['close']

        if side == 'above':
            # target is BELOW signal — hit when day_low touches target
            if hit_date is None and c['low'] <= target:
                hit_date = cdate
                days_to_target = (cdate - alert['signal_date_obj']).days
            # adverse = price moved UP (away from below-target)
            adverse = (c['high'] - signal_price) / signal_price * 100
            if adverse > max_adverse_pct:
                max_adverse_pct = adverse
        else:  # 'below'
            # target is ABOVE signal — hit when day_high touches target
            if hit_date is None and c['high'] >= target:
                hit_date = cdate
                days_to_target = (cdate - alert['signal_date_obj']).days
            # adverse = price moved DOWN (away from above-target)
            adverse = (signal_price - c['low']) / signal_price * 100
            if adverse > max_adverse_pct:
                max_adverse_pct = adverse

    # Status
    if hit_date:
        status = 'TARGET_HIT'
    elif not candles_after:
        status = 'NO_DATA'
    else:
        # Did it move at all toward target?
        if side == 'above':
            best_progress = (signal_price - min(c['low'] for c in candles_after)) / (signal_price - target) * 100
        else:
            best_progress = (max(c['high'] for c in candles_after) - signal_price) / (target - signal_price) * 100
        status = 'PENDING' if best_progress > 0 else 'DEVIATED'

    # Current vs target
    cur_gap_pct = None
    if last_close is not None:
        cur_gap_pct = (last_close - target) / target * 100

    return {
        'hit_date': hit_date.isoformat() if hit_date else '',
        'days_to_target': days_to_target if days_to_target is not None else '',
        'max_adverse_pct': round(max_adverse_pct, 2),
        'last_close': round(last_close, 2) if last_close else '',
        'cur_gap_to_target_pct': round(cur_gap_pct, 2) if cur_gap_pct is not None else '',
        'status': status,
        'days_observed': len(candles_after),
    }


def main():
    with open(TRADES_PATH) as f:
        trades = json.load(f)

    # Dedupe by (stock, timeframe, signal_date) — keep first occurrence
    seen = {}
    for t in trades:
        key = (t['stock'], t['timeframe'], t['signal_date'])
        if key not in seen:
            seen[key] = t
    alerts = list(seen.values())
    print(f'Total trades: {len(trades)}, Unique alerts: {len(alerts)}')

    # Parse signal_date once
    for a in alerts:
        a['signal_date_obj'] = datetime.fromisoformat(a['signal_date']).date()

    # Group by stock so we fetch each stock once
    by_stock = {}
    for a in alerts:
        by_stock.setdefault(a['stock'], []).append(a)

    print(f'Unique stocks to fetch: {len(by_stock)}')

    print('Loading Kite + instrument cache...')
    kite = load_kite()
    inst_cache = build_instrument_cache(kite)
    print(f'Cached {len(inst_cache)} NSE instruments')

    rows = []
    skipped = []

    for i, (stock, stock_alerts) in enumerate(sorted(by_stock.items()), 1):
        token = inst_cache.get(stock)
        if not token:
            print(f'  [{i}/{len(by_stock)}] {stock}: SKIP (no instrument token)')
            for a in stock_alerts:
                skipped.append((a, 'no_instrument_token'))
            continue

        # Fetch from earliest signal_date to today
        earliest = min(a['signal_date_obj'] for a in stock_alerts)
        try:
            candles = fetch_daily(kite, token, earliest - timedelta(days=2), TODAY)
        except Exception as e:
            print(f'  [{i}/{len(by_stock)}] {stock}: FAILED {e}')
            for a in stock_alerts:
                skipped.append((a, f'fetch_failed:{e}'))
            continue

        # Normalize candle dates to date objects
        for c in candles:
            if isinstance(c['date'], str):
                c['date'] = datetime.fromisoformat(c['date'].replace('+05:30', ''))

        for a in stock_alerts:
            # Candles strictly AFTER signal_date (next trading day onward)
            after = [c for c in candles if c['date'].date() > a['signal_date_obj']]
            metrics = analyze_alert(a, after)

            rows.append({
                'stock': a['stock'],
                'timeframe': a['timeframe'],
                'direction': a['direction'],
                'side': a['side'],
                'signal_date': a['signal_date'],
                'signal_time': a['signal_time'],
                'signal_price': a['signal_price'],
                'target_spot': a['target_spot'],
                'signal_gap_pct': a['signal_gap_pct'],
                'st_direction': a['st_direction'],
                **metrics,
            })

        print(f'  [{i}/{len(by_stock)}] {stock}: {len(stock_alerts)} alert(s), {len(candles)} candles')

    # Sort by signal_date desc
    rows.sort(key=lambda r: (r['signal_date'], r['stock']), reverse=True)

    # Write CSV
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, 'w', newline='') as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    print(f'\nWrote {len(rows)} rows to {OUT_CSV}')

    # Summary stats
    total = len(rows)
    hit = sum(1 for r in rows if r['status'] == 'TARGET_HIT')
    pending = sum(1 for r in rows if r['status'] == 'PENDING')
    deviated = sum(1 for r in rows if r['status'] == 'DEVIATED')
    no_data = sum(1 for r in rows if r['status'] == 'NO_DATA')

    print('\n=== SUMMARY ===')
    print(f'Total unique alerts analyzed: {total}')
    print(f'  TARGET_HIT: {hit} ({hit/total*100:.1f}%)')
    print(f'  PENDING (still moving toward): {pending} ({pending/total*100:.1f}%)')
    print(f'  DEVIATED (moved away): {deviated} ({deviated/total*100:.1f}%)')
    print(f'  NO_DATA (alert today): {no_data}')

    if hit > 0:
        days = [r['days_to_target'] for r in rows if r['status'] == 'TARGET_HIT' and isinstance(r['days_to_target'], int)]
        if days:
            print(f'\nDays to target (hits only):')
            print(f'  min: {min(days)}, max: {max(days)}, avg: {sum(days)/len(days):.1f}, median: {sorted(days)[len(days)//2]}')

    # Breakdown by timeframe
    print('\n=== BY TIMEFRAME ===')
    for tf in ['daily', 'weekly', 'monthly']:
        sub = [r for r in rows if r['timeframe'] == tf]
        if not sub:
            continue
        h = sum(1 for r in sub if r['status'] == 'TARGET_HIT')
        print(f'  {tf}: {len(sub)} alerts, {h} hit ({h/len(sub)*100:.1f}%)')

    # Breakdown by direction
    print('\n=== BY DIRECTION ===')
    for d in ['CE', 'PE']:
        sub = [r for r in rows if r['direction'] == d]
        if not sub:
            continue
        h = sum(1 for r in sub if r['status'] == 'TARGET_HIT')
        print(f'  {d}: {len(sub)} alerts, {h} hit ({h/len(sub)*100:.1f}%)')

    if skipped:
        print(f'\nSkipped: {len(skipped)} alerts')
        for a, reason in skipped[:5]:
            print(f'  {a["stock"]} {a["signal_date"]}: {reason}')


if __name__ == '__main__':
    main()
