"""Backtest v2: Spot 15M ST FLIP as entry trigger for Magnet trades.

Key insight from v1: at entry time, spot 15M was ALWAYS in the right direction
(100% confirmed). That's because by the time gap shrinks to 2%, the move is
already underway.

v2 tests a DIFFERENT idea: use the 15M ST FLIP itself as the entry trigger.
- At SIGNAL time (stock detected at 3% from ST), check spot 15M direction
- Track when spot 15M flips to the RIGHT direction (DOWN→UP for CE, UP→DOWN for PE)
- This flip = "the reversal has started" = optimal entry point
- Compare: did the stock reach ST target after a fresh flip vs during an existing trend?

Also checks: how long had the 15M trend been established at entry? Fresh flips vs
exhausted trends might predict success.
"""

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Project paths
HELPER = Path(__file__).resolve().parent
BOTS = HELPER.parent
sys.path.insert(0, str(HELPER))

from playbook.compute_st import compute_supertrend


def get_kite():
    """Initialize Kite client from stored token."""
    from kiteconnect import KiteConnect
    token_path = BOTS / 'data' / 'kite_access_token.json'
    with open(token_path) as f:
        token_data = json.load(f)
    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


def fetch_spot_15m(kite, symbol, target_date_str, days_before=12, days_after=5):
    """Fetch 15M spot candles: enough before for ST warmup + after for flip tracking."""
    target_date = datetime.strptime(target_date_str, '%Y-%m-%d')
    from_date = target_date - timedelta(days=days_before + 5)
    to_date = target_date + timedelta(days=days_after + 2)
    # Cap to today
    if to_date.date() > datetime.now().date():
        to_date = datetime.now()

    nse_key = f'NSE:{symbol}'
    try:
        ltp_data = kite.ltp([nse_key])
        token = ltp_data[nse_key]['instrument_token']
    except Exception as e:
        print(f"  ERROR getting token for {symbol}: {e}")
        return [], None

    try:
        candles = kite.historical_data(
            token,
            from_date.strftime('%Y-%m-%d'),
            to_date.strftime('%Y-%m-%d'),
            '15minute'
        )
        return candles, token
    except Exception as e:
        print(f"  ERROR fetching 15M data for {symbol}: {e}")
        return [], None


def get_candle_datetime(candle_date):
    """Convert candle date to naive datetime for comparison."""
    if hasattr(candle_date, 'replace'):
        return candle_date.replace(tzinfo=None)
    return datetime.strptime(str(candle_date)[:19], '%Y-%m-%dT%H:%M:%S')


def find_st_at_time(st_data, target_dt):
    """Find ST point at or just before target_dt."""
    best = None
    for pt in st_data:
        pt_dt = get_candle_datetime(pt['date'])
        if pt_dt <= target_dt:
            best = pt
    return best


def find_all_flips(st_data, start_date, end_date=None):
    """Find all direction flips in a date range. Returns list of (flip_point, from_dir, to_dir)."""
    flips = []
    for i in range(1, len(st_data)):
        pt = st_data[i]
        prev = st_data[i - 1]
        pt_dt = get_candle_datetime(pt['date'])
        if pt_dt.date() < start_date:
            continue
        if end_date and pt_dt.date() > end_date:
            break
        if pt['direction'] != prev['direction']:
            flips.append({
                'time': pt_dt,
                'from_dir': prev['direction'],
                'to_dir': pt['direction'],
                'close': pt['close'],
                'st_value': pt['supertrend'],
            })
    return flips


def count_trend_duration(st_data, target_dt):
    """Count how many consecutive 15M candles had the same direction before target_dt.
    Returns (direction, count, hours)."""
    # Find the index at target_dt
    best_idx = None
    for i, pt in enumerate(st_data):
        pt_dt = get_candle_datetime(pt['date'])
        if pt_dt <= target_dt:
            best_idx = i

    if best_idx is None:
        return None, 0, 0

    current_dir = st_data[best_idx]['direction']
    count = 0
    for j in range(best_idx, -1, -1):
        if st_data[j]['direction'] == current_dir:
            count += 1
        else:
            break

    hours = count * 0.25  # each candle = 15 min = 0.25 hr
    return current_dir, count, hours


def main():
    print("=" * 80)
    print("BACKTEST v2: Spot 15M ST FLIP as Entry Signal")
    print("=" * 80)

    with open(HELPER / 'logs' / 'magnet_trades.json') as f:
        all_trades = json.load(f)

    # All entered trades (exited with P&L, excluding cleanup)
    trades = [t for t in all_trades
              if t.get('entry_date') and t.get('entry_spot')
              and t['status'] == 'exited'
              and t.get('pnl') is not None
              and 'cleanup' not in (t.get('exit_reason') or '').lower()]

    print(f"\nAnalyzing {len(trades)} exited trades")
    print(f"Fetching 15M spot data for each...\n")

    kite = get_kite()

    # Cache per stock (wider date range)
    cache = {}  # stock -> (candles, st_data)

    results = []

    for t in trades:
        stock = t['stock']
        signal_date = t['signal_date']
        entry_date = t['entry_date']
        entry_time = t.get('entry_time', '09:30:00')
        signal_time = t.get('signal_time', '09:15:00')
        direction = t['direction']
        required_dir = 'UP' if direction == 'CE' else 'DOWN'
        opposite_dir = 'DOWN' if direction == 'CE' else 'UP'

        if stock not in cache:
            print(f"  Fetching 15M for {stock} (signal {signal_date})...")
            candles, token = fetch_spot_15m(kite, stock, signal_date)
            if candles:
                st_data = compute_supertrend(candles, 10, 3)
            else:
                st_data = []
            cache[stock] = (candles, st_data)
            time.sleep(0.35)

        candles, st_data = cache[stock]

        if not st_data:
            results.append({'trade': t, 'skip': True, 'reason': 'NO_DATA'})
            continue

        # Parse times
        try:
            sig_dt = datetime.strptime(
                f'{signal_date} {signal_time[:8]}', '%Y-%m-%d %H:%M:%S')
        except Exception:
            sig_dt = datetime.strptime(f'{signal_date} 09:15:00', '%Y-%m-%d %H:%M:%S')

        try:
            ent_dt = datetime.strptime(
                f'{entry_date} {entry_time[:8]}', '%Y-%m-%d %H:%M:%S')
        except Exception:
            ent_dt = datetime.strptime(f'{entry_date} 09:30:00', '%Y-%m-%d %H:%M:%S')

        # --- Analysis at SIGNAL time ---
        st_at_signal = find_st_at_time(st_data, sig_dt)
        sig_dir = st_at_signal['direction'] if st_at_signal else '?'
        sig_confirmed = (sig_dir == required_dir)

        # --- Analysis at ENTRY time ---
        st_at_entry = find_st_at_time(st_data, ent_dt)
        ent_dir = st_at_entry['direction'] if st_at_entry else '?'
        ent_confirmed = (ent_dir == required_dir)

        # --- Trend duration at entry (how long has the 15M been in this direction?) ---
        trend_dir, trend_candles, trend_hours = count_trend_duration(st_data, ent_dt)

        # --- Find flips on signal date and days after ---
        sig_date_obj = datetime.strptime(signal_date, '%Y-%m-%d').date()
        exit_date_str = t.get('exit_date', entry_date)
        try:
            exit_date_obj = datetime.strptime(exit_date_str, '%Y-%m-%d').date()
        except Exception:
            exit_date_obj = sig_date_obj + timedelta(days=5)

        all_flips = find_all_flips(st_data, sig_date_obj, exit_date_obj)

        # Find first flip TO required direction after signal
        first_right_flip = None
        for f in all_flips:
            if f['to_dir'] == required_dir and f['time'] >= sig_dt:
                first_right_flip = f
                break

        # Find first flip AGAINST direction after entry (reversal = danger)
        first_wrong_flip_after_entry = None
        for f in all_flips:
            if f['to_dir'] == opposite_dir and f['time'] >= ent_dt:
                first_wrong_flip_after_entry = f
                break

        # Count total flips on entry day
        entry_date_obj = datetime.strptime(entry_date, '%Y-%m-%d').date()
        entry_day_flips = [f for f in all_flips
                           if f['time'].date() == entry_date_obj]

        r = {
            'trade': t,
            'skip': False,
            'sig_dir': sig_dir,
            'sig_confirmed': sig_confirmed,
            'ent_dir': ent_dir,
            'ent_confirmed': ent_confirmed,
            'required_dir': required_dir,
            'trend_candles': trend_candles,
            'trend_hours': trend_hours,
            'first_right_flip': first_right_flip,
            'first_wrong_flip': first_wrong_flip_after_entry,
            'entry_day_flips': len(entry_day_flips),
            'total_flips': len(all_flips),
        }
        results.append(r)

        pnl = t.get('pnl', 0) or 0
        flip_tag = ""
        if first_right_flip:
            ft = first_right_flip['time'].strftime('%H:%M')
            flip_tag = f" flip@{ft}"
        wrong_tag = ""
        if first_wrong_flip_after_entry:
            wt = first_wrong_flip_after_entry['time'].strftime('%m-%d %H:%M')
            wrong_tag = f" reversal@{wt}"

        print(f"  #{t['id']:3d} {stock:15s} {direction} "
              f"sig={sig_dir:5s}({'OK' if sig_confirmed else 'XX'}) "
              f"ent={ent_dir:5s}({'OK' if ent_confirmed else 'XX'}) "
              f"trend={trend_hours:.1f}h({trend_candles}c) "
              f"flips={len(entry_day_flips)} "
              f"pnl={pnl:>8,.0f}{flip_tag}{wrong_tag}")

    # =========================================================================
    # ANALYSIS
    # =========================================================================
    valid = [r for r in results if not r.get('skip')]

    print(f"\n{'='*80}")
    print(f"ANALYSIS 1: Signal-Time Direction (at WATCHING, gap 2-3%)")
    print(f"{'='*80}")

    sig_conf = [r for r in valid if r['sig_confirmed']]
    sig_unconf = [r for r in valid if not r['sig_confirmed']]

    def pnl_stats(label, rlist):
        if not rlist:
            print(f"  {label}: 0 trades")
            return
        pnls = [(r['trade'].get('pnl', 0) or 0) for r in rlist]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p < 0]
        total = sum(pnls)
        wr = len(winners) / len(pnls) * 100
        avg = total / len(pnls)
        print(f"  {label}: {len(rlist)} trades | "
              f"Rs {total:>10,.0f} total | {wr:.0f}% win | "
              f"Rs {avg:>8,.0f} avg | "
              f"{len(winners)}W/{len(losers)}L")
        return total, wr

    print(f"\n  At signal time, was spot 15M ST already in the right direction?")
    print(f"  (CE needs UP, PE needs DOWN)")
    print()
    pnl_stats("CONFIRMED at signal ", sig_conf)
    pnl_stats("UNCONFIRMED at signal", sig_unconf)

    if sig_unconf:
        print(f"\n  Unconfirmed trades (signal-time direction was wrong):")
        for r in sig_unconf:
            t = r['trade']
            pnl = t.get('pnl', 0) or 0
            flip = r['first_right_flip']
            flip_info = ""
            if flip:
                flip_info = (f" -> flipped {r['required_dir']} at "
                             f"{flip['time'].strftime('%m-%d %H:%M')} "
                             f"spot={flip['close']:.1f}")
            else:
                flip_info = " -> NEVER flipped to right dir"
            print(f"    #{t['id']:3d} {t['stock']:15s} {t['direction']} "
                  f"spot15M={r['sig_dir']} need={r['required_dir']} "
                  f"pnl={pnl:>8,.0f}{flip_info}")

    print(f"\n{'='*80}")
    print(f"ANALYSIS 2: Trend Duration at Entry (fresh vs exhausted)")
    print(f"{'='*80}")

    # Bucket by trend duration
    fresh = [r for r in valid if r['trend_hours'] <= 1.0]     # <= 1 hr
    medium = [r for r in valid if 1.0 < r['trend_hours'] <= 4.0]
    old = [r for r in valid if r['trend_hours'] > 4.0]

    print(f"\n  How long had spot 15M trend been running at entry?")
    print()
    pnl_stats("FRESH  (<=1hr trend)  ", fresh)
    pnl_stats("MEDIUM (1-4hr trend)  ", medium)
    pnl_stats("OLD    (>4hr trend)   ", old)

    print(f"\n  Detail by trend duration:")
    print(f"  {'Stock':15s} {'Dir':4s} {'TrendHr':>8s} {'Candles':>8s} {'PnL':>10s} {'Exit':20s}")
    print(f"  {'-'*15} {'-'*4} {'-'*8} {'-'*8} {'-'*10} {'-'*20}")
    for r in sorted(valid, key=lambda x: x['trend_hours']):
        t = r['trade']
        pnl = t.get('pnl', 0) or 0
        exit_r = (t.get('exit_reason') or '?')[:20]
        print(f"  {t['stock']:15s} {t['direction']:4s} {r['trend_hours']:>7.1f}h "
              f"{r['trend_candles']:>7d}c Rs {pnl:>8,.0f}  {exit_r}")

    print(f"\n{'='*80}")
    print(f"ANALYSIS 3: Post-Entry Reversal (15M flips against us)")
    print(f"{'='*80}")

    had_reversal = [r for r in valid if r['first_wrong_flip']]
    no_reversal = [r for r in valid if not r['first_wrong_flip']]

    print(f"\n  After entry, did spot 15M ever flip against our direction?")
    print()
    pnl_stats("HAD REVERSAL (flipped against)", had_reversal)
    pnl_stats("NO REVERSAL  (held direction) ", no_reversal)

    # Time to reversal
    if had_reversal:
        print(f"\n  Reversal timing (time from entry to adverse 15M flip):")
        print(f"  {'Stock':15s} {'Dir':4s} {'Entry':>6s} {'RvrsAt':>12s} {'Hrs':>6s} {'PnL':>10s}")
        print(f"  {'-'*15} {'-'*4} {'-'*6} {'-'*12} {'-'*6} {'-'*10}")
        for r in sorted(had_reversal, key=lambda x: x['trade'].get('pnl', 0) or 0):
            t = r['trade']
            pnl = t.get('pnl', 0) or 0
            ent_t = (t.get('entry_time') or '?')[:5]
            rvrs = r['first_wrong_flip']
            rvrs_t = rvrs['time'].strftime('%m-%d %H:%M')
            try:
                ent_dt2 = datetime.strptime(
                    f"{t['entry_date']} {(t.get('entry_time') or '09:30:00')[:8]}",
                    '%Y-%m-%d %H:%M:%S')
                hrs = (rvrs['time'] - ent_dt2).total_seconds() / 3600
            except Exception:
                hrs = 0
            print(f"  {t['stock']:15s} {t['direction']:4s} {ent_t:>6s} "
                  f"{rvrs_t:>12s} {hrs:>5.1f}h Rs {pnl:>8,.0f}")

    print(f"\n{'='*80}")
    print(f"ANALYSIS 4: Flip Count (choppy vs trending days)")
    print(f"{'='*80}")

    # Bucket by flip count on entry day
    low_flip = [r for r in valid if r['entry_day_flips'] <= 1]
    med_flip = [r for r in valid if 2 <= r['entry_day_flips'] <= 3]
    high_flip = [r for r in valid if r['entry_day_flips'] >= 4]

    print(f"\n  Number of 15M ST flips on the entry day (choppiness indicator):")
    print()
    pnl_stats("LOW CHOP  (0-1 flips) ", low_flip)
    pnl_stats("MED CHOP  (2-3 flips) ", med_flip)
    pnl_stats("HIGH CHOP (4+ flips)  ", high_flip)

    # =========================================================================
    # COMPOSITE VERDICT
    # =========================================================================
    print(f"\n{'='*80}")
    print("COMPOSITE VERDICT")
    print("="*80)

    # What filter combination works best?
    # Try: signal confirmed + fresh trend + low chop
    best_filter = [r for r in valid
                   if r['sig_confirmed']
                   and r['trend_hours'] <= 2.0
                   and r['entry_day_flips'] <= 2]
    worst_filter = [r for r in valid
                    if not r['sig_confirmed']
                    or r['trend_hours'] > 4.0
                    or r['entry_day_flips'] >= 4]

    print(f"\n  BEST COMBO (sig confirmed + trend <=2h + <=2 flips):")
    pnl_stats("  PASS", best_filter)

    rejected = [r for r in valid if r not in best_filter]
    print(f"\n  REJECTED by combo filter:")
    pnl_stats("  FAIL", rejected)

    print(f"\n  CURRENT (no filter):")
    pnl_stats("  ALL ", valid)

    # Save
    output = {
        'run_date': datetime.now().isoformat(),
        'total_trades': len(valid),
        'analyses': [{
            'id': r['trade']['id'],
            'stock': r['trade']['stock'],
            'direction': r['trade']['direction'],
            'timeframe': r['trade']['timeframe'],
            'pnl': r['trade'].get('pnl'),
            'exit_reason': r['trade'].get('exit_reason'),
            'sig_dir': r['sig_dir'],
            'ent_dir': r['ent_dir'],
            'sig_confirmed': r['sig_confirmed'],
            'ent_confirmed': r['ent_confirmed'],
            'required_dir': r['required_dir'],
            'trend_hours': r['trend_hours'],
            'trend_candles': r['trend_candles'],
            'entry_day_flips': r['entry_day_flips'],
            'first_right_flip': (r['first_right_flip']['time'].isoformat()
                                 if r['first_right_flip'] else None),
            'first_wrong_flip': (r['first_wrong_flip']['time'].isoformat()
                                 if r['first_wrong_flip'] else None),
        } for r in valid]
    }

    out_path = HELPER / 'logs' / 'backtest_spot_15m_st_v2.json'
    with open(out_path, 'w') as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Results saved to {out_path}")


if __name__ == '__main__':
    main()
