# SL Comparison: Current System vs S5 (SuperTrend as SL)
import sqlite3, os, sys
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trading.db')
SELECTED_TRADES = ['WAAREEENER', 'DELHIVERY', 'POWERGRID', 'PAYTM', 'HDFCAMC', 'CGPOWER']
DUMMY_SL_PCT = 0.15

def fetch_trade_data(script):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    q = ('SELECT script, entry_date, exit_date, entry_price, exit_price, '
         'quantity, exit_reason, net_pnl, pnl_percent, days_held, '
         'highest_sl_achieved '
         'FROM closed_positions '
         "WHERE timeframe='W' AND exit_reason LIKE '%SL%' AND script=?")
    cur.execute(q, (script,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return dict(script=row[0], entry_date=row[1], exit_date=row[2],
                entry_price=row[3], exit_price=row[4], quantity=row[5],
                exit_reason=row[6], net_pnl=row[7], pnl_percent=row[8],
                days_held=row[9], highest_sl=row[10])


def fetch_ohlc(symbol, entry_date_str, weeks_before=104, weeks_after=14):
    entry_date = pd.Timestamp(entry_date_str)
    start_date = entry_date - timedelta(weeks=weeks_before)
    end_date = entry_date + timedelta(weeks=weeks_after)
    yf_symbol = symbol + '.NS'
    ticker = yf.Ticker(yf_symbol)
    weekly_df = ticker.history(start=start_date.strftime('%Y-%m-%d'),
                               end=end_date.strftime('%Y-%m-%d'), interval='1wk')
    daily_start = entry_date - timedelta(days=5)
    daily_df = ticker.history(start=daily_start.strftime('%Y-%m-%d'),
                               end=end_date.strftime('%Y-%m-%d'), interval='1d')
    return weekly_df, daily_df


def calculate_supertrend(df, period=10, multiplier=3):
    df = df.copy()
    high = df['High'].values
    low = df['Low'].values
    close = df['Close'].values
    n = len(df)
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
    atr = np.zeros(n)
    if n >= period:
        atr[period - 1] = np.mean(tr[:period])
        for i in range(period, n):
            atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
    hl2 = (high + low) / 2.0
    basic_upper = hl2 + multiplier * atr
    basic_lower = hl2 - multiplier * atr
    final_upper = np.zeros(n)
    final_lower = np.zeros(n)
    supertrend = np.zeros(n)
    trend = np.zeros(n, dtype=int)
    si = period - 1
    if si < n:
        final_upper[si] = basic_upper[si]
        final_lower[si] = basic_lower[si]
        if close[si] > final_upper[si]:
            trend[si] = 1
            supertrend[si] = final_lower[si]
        else:
            trend[si] = -1
            supertrend[si] = final_upper[si]
    for i in range(si + 1, n):
        if basic_upper[i] < final_upper[i-1] or close[i-1] > final_upper[i-1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i-1]
        if basic_lower[i] > final_lower[i-1] or close[i-1] < final_lower[i-1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i-1]
        if trend[i-1] == 1:
            trend[i] = -1 if close[i] < final_lower[i] else 1
        else:
            trend[i] = 1 if close[i] > final_upper[i] else -1
        supertrend[i] = final_lower[i] if trend[i] == 1 else final_upper[i]
    df['ATR'] = atr
    df['SuperTrend'] = supertrend
    df['ST_Trend'] = trend
    return df


def find_sl_hit_day(daily_df, sl_level, start_date, end_date=None):
    start_ts = pd.Timestamp(start_date)
    if daily_df.index.tz:
        start_ts = start_ts.tz_localize(daily_df.index.tz)
    mask = daily_df.index >= start_ts
    if end_date is not None:
        end_ts = pd.Timestamp(end_date)
        if daily_df.index.tz:
            end_ts = end_ts.tz_localize(daily_df.index.tz)
        mask = mask & (daily_df.index <= end_ts)
    subset = daily_df[mask]
    for idx_d, row_d in subset.iterrows():
        if row_d['Low'] <= sl_level:
            return (idx_d, row_d['Low'])
    return (None, None)


def get_week_ending_friday(dt):
    if isinstance(dt, str):
        dt = pd.Timestamp(dt)
    days_ahead = (4 - dt.weekday()) % 7
    return dt if days_ahead == 0 else dt + timedelta(days=days_ahead)


def analyze_trade(trade):
    script = trade['script']
    entry_date = pd.Timestamp(trade['entry_date'])
    entry_price = trade['entry_price']
    exit_price = trade['exit_price']
    quantity = trade['quantity']
    print(f'\n  Fetching data for {script}.NS ...')
    weekly_df, daily_df = fetch_ohlc(script, trade['entry_date'])
    if weekly_df.empty or daily_df.empty:
        print(f'  ERROR: No data for {script}.')
        return None
    if weekly_df.index.tz:
        weekly_df.index = weekly_df.index.tz_localize(None)
    if daily_df.index.tz:
        daily_df.index = daily_df.index.tz_localize(None)
    weekly_st = calculate_supertrend(weekly_df.copy(), period=10, multiplier=3)
    entry_ts = pd.Timestamp(entry_date)
    post_entry = weekly_st[weekly_st.index >= entry_ts - timedelta(days=6)]
    entry_week_idx = None
    for i, idx_w in enumerate(post_entry.index):
        if idx_w <= entry_ts <= idx_w + timedelta(days=6):
            entry_week_idx = i
            break
    if entry_week_idx is None:
        for i, idx_w in enumerate(post_entry.index):
            if idx_w >= entry_ts - timedelta(days=1):
                entry_week_idx = i
                break
        if entry_week_idx is None:
            entry_week_idx = 0
    analysis_weeks = post_entry.iloc[entry_week_idx:entry_week_idx + 14]
    if len(analysis_weeks) == 0:
        print(f'  ERROR: No weekly data for {script} post-entry.')
        return None
    dummy_sl = round(entry_price * (1 - DUMMY_SL_PCT), 2)
    current_sl = dummy_sl
    current_sl_hit = False
    current_sl_hit_week = None
    current_sl_hit_date = None
    s5_sl = dummy_sl
    s5_sl_hit = False
    s5_sl_hit_week = None
    s5_sl_hit_date = None
    s5_final_sl = dummy_sl
    week_rows = []
    for week_num, (idx_w, row_w) in enumerate(analysis_weeks.iterrows()):
        w_open = row_w['Open']
        w_high = row_w['High']
        w_low = row_w['Low']
        w_close = row_w['Close']
        st_val = row_w['SuperTrend']
        st_trend = row_w['ST_Trend']
        w_friday = get_week_ending_friday(idx_w)
        event = 'ENTRY' if week_num == 0 else ''
        ws = idx_w
        we = idx_w + timedelta(days=6)
        # Current system SL
        if current_sl_hit:
            csl_disp = '[exited]'
        else:
            if week_num == 0:
                hd, hp = find_sl_hit_day(daily_df, current_sl, ws, we)
                if hd is not None:
                    current_sl_hit = True
                    current_sl_hit_week = week_num
                    current_sl_hit_date = hd
                    csl_disp = f'{current_sl:.2f}(ini)'
                    event = 'CURR SL HIT!'
                else:
                    current_sl = max(current_sl, w_low)
                    csl_disp = f'{current_sl:.2f}(ini)'
            else:
                hd, hp = find_sl_hit_day(daily_df, current_sl, ws, we)
                if hd is not None:
                    current_sl_hit = True
                    current_sl_hit_week = week_num
                    current_sl_hit_date = hd
                    csl_disp = f'{current_sl:.2f}'
                    event = 'CURR SL HIT!' if not event else event + '+CSL'
                else:
                    current_sl = max(current_sl, w_low)
                    csl_disp = f'{current_sl:.2f}'
        # S5 system SL
        if s5_sl_hit:
            s5_disp = '[exited]'
        else:
            if week_num == 0:
                hd5, hp5 = find_sl_hit_day(daily_df, s5_sl, ws, we)
                if hd5 is not None:
                    s5_sl_hit = True
                    s5_sl_hit_week = week_num
                    s5_sl_hit_date = hd5
                    s5_final_sl = s5_sl
                    s5_disp = f'{s5_sl:.2f}(ini)'
                    event = event + '+S5 HIT!' if event else 'S5 SL HIT!'
                else:
                    if st_trend == 1 and st_val > 0:
                        s5_sl = max(s5_sl, st_val)
                    s5_disp = f'{s5_sl:.2f}(ini)'
            else:
                hd5, hp5 = find_sl_hit_day(daily_df, s5_sl, ws, we)
                if hd5 is not None:
                    s5_sl_hit = True
                    s5_sl_hit_week = week_num
                    s5_sl_hit_date = hd5
                    s5_final_sl = s5_sl
                    s5_disp = f'{s5_sl:.2f}'
                    event = event + '+S5 HIT!' if event else 'S5 SL HIT!'
                else:
                    if st_trend == 1 and st_val > 0:
                        s5_sl = max(s5_sl, st_val)
                    s5_disp = f'{s5_sl:.2f}'
        week_rows.append(dict(wn=week_num, we=w_friday.strftime('%Y-%m-%d'),
            o=w_open, h=w_high, l=w_low, c=w_close, csl=csl_disp,
            stv=f'{st_val:.2f}' if st_val > 0 else 'warm-up',
            stt='UP' if st_trend == 1 else 'DN', s5=s5_disp, ev=event))
        if current_sl_hit and s5_sl_hit:
            rem = analysis_weeks.iloc[week_num + 1: week_num + 4]
            for en, (ei, er) in enumerate(rem.iterrows()):
                ef = get_week_ending_friday(ei)
                est = er['SuperTrend']
                week_rows.append(dict(wn=week_num + en + 1, we=ef.strftime('%Y-%m-%d'),
                    o=er['Open'], h=er['High'], l=er['Low'], c=er['Close'],
                    csl='[exited]', stv=f'{est:.2f}' if est > 0 else 'warm-up',
                    stt='UP' if er['ST_Trend'] == 1 else 'DN',
                    s5='[exited]', ev='POST-EXIT'))
            break
    curr_pnl_pct = trade['pnl_percent']
    curr_pnl_rs = trade['net_pnl']
    if s5_sl_hit:
        s5_exit_price = s5_final_sl
        s5_pnl_pct = ((s5_exit_price - entry_price) / entry_price) * 100
        s5_pnl_rs = (s5_exit_price - entry_price) * quantity
        s5_dt = s5_sl_hit_date.strftime('%Y-%m-%d') if s5_sl_hit_date else 'N/A'
        s5_status = f'SL hit {s5_dt} @ Rs.{s5_exit_price:.2f}'
    else:
        last_r = analysis_weeks.iloc[-1]
        s5_exit_price = last_r['Close']
        s5_pnl_pct = ((s5_exit_price - entry_price) / entry_price) * 100
        s5_pnl_rs = (s5_exit_price - entry_price) * quantity
        s5_status = f'Still OPEN (MTM @ Rs.{s5_exit_price:.2f})'
    max_post = None
    max_post_dt = None
    mvmt_pct = 0
    if current_sl_hit_date is not None:
        psd = daily_df[daily_df.index > current_sl_hit_date]
        cutoff = current_sl_hit_date + timedelta(weeks=12)
        psd = psd[psd.index <= cutoff]
        if not psd.empty:
            max_post = psd['High'].max()
            max_post_dt = psd['High'].idxmax()
            mvmt_pct = ((max_post - exit_price) / exit_price) * 100
    # === PRINT RESULTS ===
    print()
    print('=' * 122)
    print(f'  TRADE: {script} (Weekly)')
    ed = trade['entry_date']
    xd = trade['exit_date']
    print(f'  Entry: {ed} @ Rs.{entry_price:.2f} | Qty: {quantity} | Capital: Rs.{entry_price * quantity:.2f}')
    print('=' * 122)
    print()
    print(f'  CURRENT SYSTEM: Exited {xd} @ Rs.{exit_price:.2f} | P/L: {curr_pnl_pct:+.2f}% (Rs.{curr_pnl_rs:+.2f})')
    print(f'  S5 (SuperTrend SL): {s5_status} | P/L: {s5_pnl_pct:+.2f}% (Rs.{s5_pnl_rs:+.2f})')
    if s5_pnl_rs > curr_pnl_rs:
        print(f'  --> S5 ADVANTAGE: Rs.{s5_pnl_rs - curr_pnl_rs:+.2f} better')
    elif s5_pnl_rs < curr_pnl_rs:
        print(f'  --> CURRENT better by Rs.{curr_pnl_rs - s5_pnl_rs:+.2f}')
    else:
        print('  --> Equal performance')
    if max_post is not None and mvmt_pct > 0:
        ltt = (max_post - exit_price) * quantity
        mpd = max_post_dt.strftime('%Y-%m-%d')
        print(f'  Post-SL peak: Rs.{max_post:.2f} on {mpd} (+{mvmt_pct:.1f}% from exit)')
        print(f'  Money left on table: Rs.{ltt:.2f}')
    print()
    print('  Week-by-Week Breakdown:')
    sep = '  ' + '-' * 120
    print(sep)
    hdr = '  {:>3} | {:>10} | {:>9} | {:>9} | {:>9} | {:>9} | {:>14} | {:>10} | {:>14} | {:<18}'.format(
        'Wk', 'Week End', 'Open', 'High', 'Low', 'Close', 'Curr SL', 'ST(10,3)', 'S5 SL', 'Event')
    print(hdr)
    print(sep)
    for wr in week_rows:
        ln = '  {:>3} | {:>10} | {:>9.2f} | {:>9.2f} | {:>9.2f} | {:>9.2f} | {:>14} | {:>10} | {:>14} | {:<18}'.format(
            wr['wn'], wr['we'], wr['o'], wr['h'], wr['l'], wr['c'],
            wr['csl'], wr['stv'], wr['s5'], wr['ev'])
        print(ln)
    print(sep)
    # Daily data around SL hits
    if current_sl_hit_date is not None:
        print()
        chd = current_sl_hit_date.strftime('%Y-%m-%d')
        print(f'  Daily around CURRENT SL hit ({chd}):')
        ht = pd.Timestamp(current_sl_hit_date)
        dw = daily_df[(daily_df.index >= ht - timedelta(days=5)) & (daily_df.index <= ht + timedelta(days=5))]
        print('  {:>12} | {:>9} | {:>9} | {:>9} | {:>9} | {}'.format('Date', 'Open', 'High', 'Low', 'Close', 'Note'))
        print('  ' + '-' * 75)
        for di, dr in dw.iterrows():
            nt = '<-- SL HIT' if di.date() == ht.date() else ''
            ds = di.strftime('%Y-%m-%d')
            print('  {:>12} | {:>9.2f} | {:>9.2f} | {:>9.2f} | {:>9.2f} | {}'.format(
                ds, dr['Open'], dr['High'], dr['Low'], dr['Close'], nt))
    if s5_sl_hit and s5_sl_hit_date is not None:
        show_s5 = True
        if current_sl_hit_date is not None and s5_sl_hit_date.date() == current_sl_hit_date.date():
            show_s5 = False
        if show_s5:
            print()
            s5d = s5_sl_hit_date.strftime('%Y-%m-%d')
            print(f'  Daily around S5 SL hit ({s5d}), SL={s5_final_sl:.2f}:')
            ht = pd.Timestamp(s5_sl_hit_date)
            dw = daily_df[(daily_df.index >= ht - timedelta(days=5)) & (daily_df.index <= ht + timedelta(days=5))]
            print('  {:>12} | {:>9} | {:>9} | {:>9} | {:>9} | {}'.format('Date', 'Open', 'High', 'Low', 'Close', 'Note'))
            print('  ' + '-' * 75)
            for di, dr in dw.iterrows():
                nt = '<-- S5 SL HIT' if di.date() == ht.date() else ''
                ds = di.strftime('%Y-%m-%d')
                print('  {:>12} | {:>9.2f} | {:>9.2f} | {:>9.2f} | {:>9.2f} | {}'.format(
                    ds, dr['Open'], dr['High'], dr['Low'], dr['Close'], nt))
    print()
    return dict(script=script, entry_price=entry_price, exit_price=exit_price,
                curr_pnl_pct=curr_pnl_pct, curr_pnl_rs=curr_pnl_rs,
                s5_pnl_pct=s5_pnl_pct, s5_pnl_rs=s5_pnl_rs,
                s5_status=s5_status, s5_hit=s5_sl_hit)


def main():
    print()
    print('#' * 122)
    print('#' + 'SL COMPARISON ANALYSIS'.center(120) + '#')
    print('#' + 'Current System (Weekly Low Trail) vs S5 (SuperTrend as SL)'.center(120) + '#')
    print('#' * 122)
    print()
    print('  Parameters:')
    print('    SuperTrend: Period=10, Multiplier=3 (on weekly candles)')
    print(f'    Initial SL (dummy): {DUMMY_SL_PCT*100:.0f}% below entry')
    print('    Current System: SL trails up to weekly LOW, never down')
    print('    S5 System: SL trails up to SuperTrend value, never down')
    print('    Window: up to 12 weeks from entry')
    print()
    results = []
    for scr in SELECTED_TRADES:
        trade = fetch_trade_data(scr)
        if trade is None:
            print(f'  {scr} not found. Skipping.')
            continue
        try:
            r = analyze_trade(trade)
            if r:
                results.append(r)
        except Exception as e:
            print(f'  ERROR {scr}: {e}')
            import traceback
            traceback.print_exc()
    if results:
        print()
        print('=' * 122)
        print('  FINAL SUMMARY')
        print('=' * 122)
        print('  {:<15} | {:>10} | {:>10} | {:>10} | {:>11} | {:>9} | {:>11} | {:<10}'.format(
            'Script', 'Entry', 'Curr Exit', 'Curr P/L%', 'Curr Rs', 'S5 P/L%', 'S5 Rs', 'Winner'))
        print('  ' + '-' * 100)
        tc = 0
        ts5 = 0
        for r in results:
            w = 'S5' if r['s5_pnl_rs'] > r['curr_pnl_rs'] else ('CURRENT' if r['s5_pnl_rs'] < r['curr_pnl_rs'] else 'TIE')
            tc += r['curr_pnl_rs']
            ts5 += r['s5_pnl_rs']
            print('  {:<15} | {:>10.2f} | {:>10.2f} | {:>+10.2f}% | {:>+11.2f} | {:>+9.2f}% | {:>+11.2f} | {:<10}'.format(
                r['script'], r['entry_price'], r['exit_price'],
                r['curr_pnl_pct'], r['curr_pnl_rs'],
                r['s5_pnl_pct'], r['s5_pnl_rs'], w))
        print('  ' + '-' * 100)
        tw = 'S5' if ts5 > tc else 'CURRENT'
        print('  {:<15} | {:>10} | {:>10} | {:>11} | {:>+11.2f} | {:>10} | {:>+11.2f} | {:<10}'.format(
            'TOTAL', '', '', '', tc, '', ts5, tw))
        print()
        print(f'  Net S5 advantage: Rs.{ts5 - tc:+.2f}')
        print()


if __name__ == '__main__':
    main()
