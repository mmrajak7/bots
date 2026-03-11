import sqlite3

conn = sqlite3.connect(r'C:\Users\mail2\Documents\Projects\BOTS\data\trading.db')
cur = conn.cursor()

cur.execute('''
    SELECT net_pnl, pnl_percent, days_held, gross_pnl, transaction_costs,
           exit_reason, capital_deployed, script, timeframe, entry_date, exit_date
    FROM closed_positions
    WHERE bot_instance_id LIKE '%crocodile%'
    ORDER BY exit_date DESC
''')
rows = cur.fetchall()

total_trades = len(rows)
wins = [r for r in rows if r[0] > 0]
losses = [r for r in rows if r[0] <= 0]
win_count = len(wins)
loss_count = len(losses)

real_trades = [r for r in rows if r[5] not in ('STALE_SYNC',)]
real_wins = [r for r in real_trades if r[0] > 0]
real_losses = [r for r in real_trades if r[0] <= 0]

total_net_pnl = sum(r[0] for r in rows)
total_gross_pnl = sum(r[3] for r in rows)
total_txn_costs = sum(r[4] for r in rows)
total_capital = sum(r[6] for r in rows)

avg_pnl_pct = sum(r[1] for r in rows) / total_trades if total_trades > 0 else 0
avg_win_pct = sum(r[1] for r in wins) / win_count if win_count > 0 else 0
avg_loss_pct = sum(r[1] for r in losses) / loss_count if loss_count > 0 else 0

avg_days_held = sum(r[2] for r in rows) / total_trades if total_trades > 0 else 0

biggest_win = max(rows, key=lambda r: r[1])
biggest_loss = min(rows, key=lambda r: r[1])

exit_reasons = {}
for r in rows:
    reason = r[5]
    if reason not in exit_reasons:
        exit_reasons[reason] = {'count': 0, 'net_pnl': 0}
    exit_reasons[reason]['count'] += 1
    exit_reasons[reason]['net_pnl'] += r[0]

tf_stats = {}
for r in rows:
    tf = r[8]
    if tf not in tf_stats:
        tf_stats[tf] = {'count': 0, 'wins': 0, 'losses': 0, 'net_pnl': 0, 'pnl_pcts': []}
    tf_stats[tf]['count'] += 1
    tf_stats[tf]['net_pnl'] += r[0]
    tf_stats[tf]['pnl_pcts'].append(r[1])
    if r[0] > 0:
        tf_stats[tf]['wins'] += 1
    else:
        tf_stats[tf]['losses'] += 1

weekly_trades = [r for r in rows if r[8] == 'W' and r[5] != 'STALE_SYNC']
daily_trades = [r for r in rows if r[8] == 'D' and r[5] != 'STALE_SYNC']

print('=' * 80)
print('CROCODILE BOT - TRADING PERFORMANCE SUMMARY')
print('=' * 80)

print('')
print(f'--- OVERALL (ALL {total_trades} trades) ---')
print(f'  Total Trades:          {total_trades}')
print(f'  Win Count:             {win_count}')
print(f'  Loss Count:            {loss_count}')
print(f'  Win Rate:              {win_count/total_trades*100:.1f}%')
print(f'  Total Gross PnL:       Rs {total_gross_pnl:,.2f}')
print(f'  Total Txn Costs:       Rs {total_txn_costs:,.2f}')
print(f'  Total Net PnL:         Rs {total_net_pnl:,.2f}')
print(f'  Avg PnL % per trade:   {avg_pnl_pct:.2f}%')
print(f'  Avg Win %:             {avg_win_pct:.2f}%')
print(f'  Avg Loss %:            {avg_loss_pct:.2f}%')
print(f'  Avg Holding Period:    {avg_days_held:.1f} days')
print(f'  Total Capital Deployed:{total_capital:,.2f}')
print(f'  Return on Capital:     {total_net_pnl/total_capital*100:.3f}%')
print('')
print(f'  Biggest Win:           {biggest_win[7]} ({biggest_win[1]:.2f}%, Rs {biggest_win[0]:,.2f}) on {biggest_win[10]}')
print(f'  Biggest Loss:          {biggest_loss[7]} ({biggest_loss[1]:.2f}%, Rs {biggest_loss[0]:,.2f}) on {biggest_loss[10]}')

if real_wins and real_losses:
    avg_real_win = sum(r[1] for r in real_wins) / len(real_wins)
    avg_real_loss = abs(sum(r[1] for r in real_losses) / len(real_losses))
    rr_ratio = avg_real_win / avg_real_loss if avg_real_loss > 0 else 0
    print('')
    print(f'  Avg Real Win %:        {avg_real_win:.2f}%')
    print(f'  Avg Real Loss %:       -{avg_real_loss:.2f}%')
    print(f'  Risk/Reward Ratio:     {rr_ratio:.2f}')

total_win_pnl = sum(r[0] for r in wins)
total_loss_pnl = abs(sum(r[0] for r in losses))
profit_factor = total_win_pnl / total_loss_pnl if total_loss_pnl > 0 else float('inf')
print(f'  Profit Factor:         {profit_factor:.3f}')

expectancy = total_net_pnl / total_trades if total_trades > 0 else 0
print(f'  Expectancy per trade:  Rs {expectancy:,.2f}')

print('')
print(f'--- EXCLUDING STALE_SYNC ({len(real_trades)} real trades) ---')
real_win_count = len(real_wins)
real_loss_count = len(real_losses)
real_total_pnl = sum(r[0] for r in real_trades)
real_total_capital = sum(r[6] for r in real_trades)
print(f'  Real Trades:           {len(real_trades)}')
print(f'  Wins:                  {real_win_count}')
print(f'  Losses:                {real_loss_count}')
print(f'  Win Rate:              {real_win_count/len(real_trades)*100:.1f}%')
print(f'  Total Net PnL:         Rs {real_total_pnl:,.2f}')
print(f'  Avg PnL %:             {sum(r[1] for r in real_trades)/len(real_trades):.2f}%')
print(f'  Avg Days Held:         {sum(r[2] for r in real_trades)/len(real_trades):.1f} days')

print('')
print('--- EXIT REASON BREAKDOWN ---')
for reason, data in sorted(exit_reasons.items(), key=lambda x: -x[1]['count']):
    print(f'  {reason:<22} Count: {data["count"]:>3}   Net PnL: Rs {data["net_pnl"]:>10,.2f}')

print('')
print('--- TIMEFRAME BREAKDOWN ---')
for tf, data in sorted(tf_stats.items()):
    avg_pnl = sum(data['pnl_pcts']) / data['count']
    wr = data['wins'] / data['count'] * 100 if data['count'] > 0 else 0
    print(f'  {tf}: Trades={data["count"]}, Wins={data["wins"]}, Losses={data["losses"]}, WR={wr:.1f}%, Net PnL=Rs {data["net_pnl"]:,.2f}, Avg PnL%={avg_pnl:.2f}%')

print('')
print(f'--- WEEKLY TIMEFRAME (excl STALE_SYNC, {len(weekly_trades)} trades) ---')
if weekly_trades:
    w_wins = len([r for r in weekly_trades if r[0] > 0])
    w_losses = len([r for r in weekly_trades if r[0] <= 0])
    w_net = sum(r[0] for r in weekly_trades)
    w_avg_pnl = sum(r[1] for r in weekly_trades) / len(weekly_trades)
    w_avg_days = sum(r[2] for r in weekly_trades) / len(weekly_trades)
    print(f'  Trades: {len(weekly_trades)}, Wins: {w_wins}, Losses: {w_losses}, WR: {w_wins/len(weekly_trades)*100:.1f}%')
    print(f'  Net PnL: Rs {w_net:,.2f}, Avg PnL%: {w_avg_pnl:.2f}%, Avg Days: {w_avg_days:.1f}')

print('')
print(f'--- DAILY TIMEFRAME (excl STALE_SYNC, {len(daily_trades)} trades) ---')
if daily_trades:
    d_wins = len([r for r in daily_trades if r[0] > 0])
    d_losses = len([r for r in daily_trades if r[0] <= 0])
    d_net = sum(r[0] for r in daily_trades)
    d_avg_pnl = sum(r[1] for r in daily_trades) / len(daily_trades)
    d_avg_days = sum(r[2] for r in daily_trades) / len(daily_trades)
    print(f'  Trades: {len(daily_trades)}, Wins: {d_wins}, Losses: {d_losses}, WR: {d_wins/len(daily_trades)*100:.1f}%')
    print(f'  Net PnL: Rs {d_net:,.2f}, Avg PnL%: {d_avg_pnl:.2f}%, Avg Days: {d_avg_days:.1f}')

print('')
print('--- STREAK ANALYSIS ---')
sorted_by_exit = sorted(rows, key=lambda r: r[10])
max_win_streak = 0
max_loss_streak = 0
curr_win = 0
curr_loss = 0
for r in sorted_by_exit:
    if r[0] > 0:
        curr_win += 1
        curr_loss = 0
        max_win_streak = max(max_win_streak, curr_win)
    else:
        curr_loss += 1
        curr_win = 0
        max_loss_streak = max(max_loss_streak, curr_loss)
print(f'  Max Win Streak:        {max_win_streak}')
print(f'  Max Loss Streak:       {max_loss_streak}')

print('')
print('--- MONTHLY BREAKDOWN ---')
monthly = {}
for r in rows:
    month = r[10][:7]
    if month not in monthly:
        monthly[month] = {'count
