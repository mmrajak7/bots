"""
SL Strategy Backtest for CROCODILE Weekly SuperTrend Bot
Compares 6 stop-loss strategies across 25 real SL-hit trades.
"""
import sqlite3, warnings, sys, os
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import yfinance as yf
from tabulate import tabulate
warnings.filterwarnings("ignore")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trading.db")
SIMULATION_WEEKS = 12
INITIAL_SL_PCT = 0.15
MIN_SL_DISTANCE_PCT = 0.05
PROFIT_THRESHOLD_PCT = 0.05
SUPERTREND_PERIOD = 10
SUPERTREND_MULTIPLIER = 3
ATR_PERIOD = 10
ATR_SL_MULTIPLIER = 2
STRATEGY_NAMES = {
    0: "S0: Baseline (Week LOW)",
    1: "S1: ATR-based (Close - 2*ATR)",
    2: "S2: Week LOW + 5% floor",
    3: "S3: 3-Week Swing Low",
    4: "S4: No trail until +5%",
    5: "S5: SuperTrend(10,3) as SL",
}


def get_sl_trades():
    conn = sqlite3.connect(DB_PATH)
    query = '''
        SELECT script, entry_date, entry_price, exit_date, exit_price,
               quantity, net_pnl, pnl_percent
        FROM closed_positions
        WHERE timeframe='W' AND exit_reason LIKE '%SL%'
        ORDER BY entry_date
    '''
    df = pd.read_sql_query(query, conn)
    conn.close()
    df["entry_date"] = pd.to_datetime(df["entry_date"])
    df["exit_date"] = pd.to_datetime(df["exit_date"])
    return df
def fetch_price_data(symbol, entry_date):
    ticker = f"{symbol}.NS"
    start = entry_date - timedelta(days=120)
    end = min(entry_date + timedelta(weeks=SIMULATION_WEEKS + 1), datetime.now())
    try:
        tk = yf.Ticker(ticker)
        daily = tk.history(start=start.strftime("%Y-%m-%d"),
                           end=end.strftime("%Y-%m-%d"),
                           interval="1d", auto_adjust=True)
        if daily.empty:
            print(f"  [WARN] No daily data for {ticker}")
            return None, None
        weekly = tk.history(start=start.strftime("%Y-%m-%d"),
                            end=end.strftime("%Y-%m-%d"),
                            interval="1wk", auto_adjust=True)
        if weekly.empty:
            print(f"  [WARN] No weekly data for {ticker}")
            return None, None
        daily.index = daily.index.tz_localize(None)
        weekly.index = weekly.index.tz_localize(None)
        return daily, weekly
    except Exception as e:
        print(f"  [ERROR] Failed to fetch {ticker}: {e}")
        return None, None
def compute_atr_wilders(df, period=10):
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    atr = pd.Series(index=df.index, dtype=float)
    if len(tr) < period:
        return atr
    atr.iloc[period - 1] = tr.iloc[:period].mean()
    for i in range(period, len(tr)):
        atr.iloc[i] = (atr.iloc[i - 1] * (period - 1) + tr.iloc[i]) / period
    return atr
def compute_supertrend(df, period=10, multiplier=3.0):
    df = df.copy()
    df["ATR"] = compute_atr_wilders(df, period)
    hl2 = (df["High"] + df["Low"]) / 2
    df["BasicUpper"] = hl2 + multiplier * df["ATR"]
    df["BasicLower"] = hl2 - multiplier * df["ATR"]
    df["FinalUpper"] = np.nan
    df["FinalLower"] = np.nan
    df["SuperTrend"] = np.nan
    df["ST_Direction"] = 1
    for i in range(len(df)):
        if i == 0:
            df.iloc[i, df.columns.get_loc("FinalUpper")] = df.iloc[i]["BasicUpper"]
            df.iloc[i, df.columns.get_loc("FinalLower")] = df.iloc[i]["BasicLower"]
            df.iloc[i, df.columns.get_loc("SuperTrend")] = df.iloc[i]["FinalLower"]
            df.iloc[i, df.columns.get_loc("ST_Direction")] = 1
            continue
        prev_fu = df.iloc[i - 1]["FinalUpper"]
        prev_fl = df.iloc[i - 1]["FinalLower"]
        prev_close_val = df.iloc[i - 1]["Close"]
        if df.iloc[i]["BasicUpper"] < prev_fu or prev_close_val > prev_fu:
            df.iloc[i, df.columns.get_loc("FinalUpper")] = df.iloc[i]["BasicUpper"]
        else:
            df.iloc[i, df.columns.get_loc("FinalUpper")] = prev_fu
        if df.iloc[i]["BasicLower"] > prev_fl or prev_close_val < prev_fl:
            df.iloc[i, df.columns.get_loc("FinalLower")] = df.iloc[i]["BasicLower"]
        else:
            df.iloc[i, df.columns.get_loc("FinalLower")] = prev_fl
        prev_st = df.iloc[i - 1]["SuperTrend"]
        cur_fu = df.iloc[i, df.columns.get_loc("FinalUpper")]
        cur_fl = df.iloc[i, df.columns.get_loc("FinalLower")]
        cur_close = df.iloc[i]["Close"]
        if prev_st == df.iloc[i - 1]["FinalUpper"]:
            if cur_close > cur_fu:
                df.iloc[i, df.columns.get_loc("SuperTrend")] = cur_fl
                df.iloc[i, df.columns.get_loc("ST_Direction")] = 1
            else:
                df.iloc[i, df.columns.get_loc("SuperTrend")] = cur_fu
                df.iloc[i, df.columns.get_loc("ST_Direction")] = -1
        else:
            if cur_close < cur_fl:
                df.iloc[i, df.columns.get_loc("SuperTrend")] = cur_fu
                df.iloc[i, df.columns.get_loc("ST_Direction")] = -1
            else:
                df.iloc[i, df.columns.get_loc("SuperTrend")] = cur_fl
                df.iloc[i, df.columns.get_loc("ST_Direction")] = 1
    return df


def get_week_low_from_daily(daily_df, friday_date, entry_date):
    monday = friday_date - timedelta(days=friday_date.weekday())
    start = max(monday, entry_date)
    mask = (daily_df.index >= pd.Timestamp(start)) & (daily_df.index <= pd.Timestamp(friday_date))
    week_data = daily_df.loc[mask]
    if week_data.empty:
        return np.nan
    return week_data["Low"].min()
def get_week_close_from_daily(daily_df, friday_date):
    monday = friday_date - timedelta(days=friday_date.weekday())
    mask = (daily_df.index >= pd.Timestamp(monday)) & (daily_df.index <= pd.Timestamp(friday_date))
    week_data = daily_df.loc[mask]
    if week_data.empty:
        return np.nan
    return week_data["Close"].iloc[-1]
def get_fridays_in_range(daily_df, start_date, end_date):
    mask = (daily_df.index >= pd.Timestamp(start_date)) & (daily_df.index <= pd.Timestamp(end_date))
    sub = daily_df.loc[mask]
    if sub.empty:
        return []
    fridays = []
    iso = sub.index.isocalendar()
    for _, group in sub.groupby([iso.year, iso.week]):
        fridays.append(group.index[-1])
    return sorted(fridays)
def get_weekly_data_at_friday(weekly_df, friday_date):
    target_monday = friday_date - timedelta(days=friday_date.weekday())
    for idx in weekly_df.index:
        if abs((idx - pd.Timestamp(target_monday)).days) <= 3:
            return weekly_df.loc[idx]
    return None
def simulate_strategy(strategy_id, entry_price, entry_date, daily_df, weekly_st_df):
    initial_sl = entry_price * (1 - INITIAL_SL_PCT)
    current_sl = initial_sl
    sim_end = entry_date + timedelta(weeks=SIMULATION_WEEKS)
    max_date = daily_df.index.max()
    sim_end = min(pd.Timestamp(sim_end), max_date)
    mask = (daily_df.index >= entry_date) & (daily_df.index <= sim_end)
    sim_daily = daily_df.loc[mask]
    if sim_daily.empty:
        return {
            "sl_hit": False, "exit_date": None, "exit_price": None,
            "pnl_pct": 0.0, "max_price_seen": entry_price,
            "max_drawdown_from_entry": 0.0, "days_held": 0,
            "final_price": entry_price, "final_pnl_pct": 0.0,
        }
    fridays = get_fridays_in_range(daily_df, entry_date, sim_end)
    friday_data = {}
    for fri in fridays:
        week_low = get_week_low_from_daily(daily_df, fri, entry_date)
        week_close = get_week_close_from_daily(daily_df, fri)
        wk_row = get_weekly_data_at_friday(weekly_st_df, fri)
        wk_atr = None
        wk_st = None
        if wk_row is not None:
            a_val = wk_row.get("ATR", np.nan)
            s_val = wk_row.get("SuperTrend", np.nan)
            if not np.isnan(a_val):
                wk_atr = a_val
            if not np.isnan(s_val):
                wk_st = s_val
        fri_idx = fridays.index(fri)
        three_week_lows = []
        for offset in range(3):
            if fri_idx - offset >= 0:
                past_fri = fridays[fri_idx - offset]
                past_low = get_week_low_from_daily(daily_df, past_fri, entry_date)
                if not np.isnan(past_low):
                    three_week_lows.append(past_low)
        friday_data[fri] = {
            "week_low": week_low, "week_close": week_close,
            "wk_atr": wk_atr, "wk_st": wk_st,
            "three_week_lows": three_week_lows,
        }
    max_price = entry_price
    min_price = entry_price
    sl_hit = False
    exit_date_result = None
    exit_price_result = None
    days_held = 0
    for day_ts, day_row in sim_daily.iterrows():
        day_low = day_row["Low"]
        day_high = day_row["High"]
        max_price = max(max_price, day_high)
        min_price = min(min_price, day_low)
        days_held = (day_ts - entry_date).days
        if day_low <= current_sl:
            sl_hit = True
            exit_date_result = day_ts
            exit_price_result = current_sl
            break
        if day_ts in friday_data:
            fd = friday_data[day_ts]
            wl = fd["week_low"]
            wc = fd["week_close"]
            wa = fd["wk_atr"]
            ws = fd["wk_st"]
            twl = fd["three_week_lows"]
            if np.isnan(wl) or np.isnan(wc):
                continue
            new_sl = current_sl
            if strategy_id == 0:
                new_sl = wl
            elif strategy_id == 1:
                if wa is not None and wa > 0:
                    new_sl = wc - ATR_SL_MULTIPLIER * wa
            elif strategy_id == 2:
                new_sl = wl
                min_allowed = wc * (1 - MIN_SL_DISTANCE_PCT)
                if new_sl > min_allowed:
                    new_sl = min_allowed
            elif strategy_id == 3:
                if twl:
                    new_sl = min(twl)
            elif strategy_id == 4:
                if wc >= entry_price * (1 + PROFIT_THRESHOLD_PCT):
                    new_sl = wl
            elif strategy_id == 5:
                if ws is not None and ws > 0:
                    new_sl = ws
            current_sl = max(current_sl, new_sl)
    final_price = sim_daily["Close"].iloc[-1] if not sim_daily.empty else entry_price
    max_dd = (min_price - entry_price) / entry_price * 100
    if sl_hit:
        pnl_pct = (exit_price_result - entry_price) / entry_price * 100
        return {
            "sl_hit": True, "exit_date": exit_date_result,
            "exit_price": round(exit_price_result, 2),
            "pnl_pct": round(pnl_pct, 2),
            "max_price_seen": round(max_price, 2),
            "max_drawdown_from_entry": round(max_dd, 2),
            "days_held": days_held,
            "final_price": None, "final_pnl_pct": None,
        }
    else:
        final_pnl = (final_price - entry_price) / entry_price * 100
        return {
            "sl_hit": False, "exit_date": None, "exit_price": None,
            "pnl_pct": round(final_pnl, 2),
            "max_price_seen": round(max_price, 2),
            "max_drawdown_from_entry": round(max_dd, 2),
            "days_held": (sim_daily.index[-1] - entry_date).days,
            "final_price": round(final_price, 2),
            "final_pnl_pct": round(final_pnl, 2),
        }

def main():
    print("=" * 80)
    print("  CROCODILE SL STRATEGY BACKTEST")
    print("  Comparing 6 strategies across weekly SL-hit trades")
    print("=" * 80)
    print()

    trades_df = get_sl_trades()
    print(f"Loaded {len(trades_df)} SL-hit trades from database.")
    print()

    all_results = []
    skipped = []

    for idx, trade in trades_df.iterrows():
        symbol = trade["script"]
        entry_date = trade["entry_date"]
        entry_price = trade["entry_price"]
        quantity = trade["quantity"]
        actual_pnl_pct = trade["pnl_percent"]

        msg = f"[{idx+1:2d}/{len(trades_df)}] {symbol:15s} | Entry: {entry_date.date()} @ {entry_price:.2f} | Actual: {actual_pnl_pct:+.2f}%"
        print(msg)

        daily_df, weekly_df = fetch_price_data(symbol, entry_date)
        if daily_df is None or weekly_df is None:
            print("  -> SKIPPED (no data)")
            print()
            skipped.append(symbol)
            continue

        weekly_st_df = compute_supertrend(weekly_df, SUPERTREND_PERIOD, SUPERTREND_MULTIPLIER)

        trade_results = {
            "symbol": symbol, "entry_date": entry_date,
            "entry_price": entry_price, "quantity": quantity,
            "actual_exit_price": trade["exit_price"],
            "actual_pnl_pct": actual_pnl_pct,
            "actual_net_pnl": trade["net_pnl"],
        }

        for strat_id in range(6):
            result = simulate_strategy(strat_id, entry_price, entry_date, daily_df, weekly_st_df)
            trade_results[f"S{strat_id}"] = result
            status = "SL HIT" if result["sl_hit"] else "SURVIVED"
            pnl = result["pnl_pct"]
            sname = STRATEGY_NAMES[strat_id]
            print(f"    {sname:35s} -> {status:8s} | P&L: {pnl:+7.2f}%")

        all_results.append(trade_results)
        print()

    if not all_results:
        print("No trades to analyse. Exiting.")
        return

    print()
    print("=" * 100)
    print("  STRATEGY COMPARISON SUMMARY")
    print("=" * 100)

    summary_rows = []
    strategy_details = {}

    for strat_id in range(6):
        sl_hit_trades = []
        survived_trades = []

        for tr in all_results:
            res = tr[f"S{strat_id}"]
            ep = tr["entry_price"]
            qty = tr["quantity"]
            if res["sl_hit"]:
                gp = (res["exit_price"] - ep) * qty
                sl_hit_trades.append({"symbol": tr["symbol"], "entry_date": tr["entry_date"],
                    "entry_price": ep, "exit_price": res["exit_price"],
                    "pnl_pct": res["pnl_pct"], "gross_pnl": gp,
                    "days_held": res["days_held"], "max_price": res["max_price_seen"]})
            else:
                mp = res["final_price"] if res["final_price"] else ep
                gp = (mp - ep) * qty
                survived_trades.append({"symbol": tr["symbol"], "entry_date": tr["entry_date"],
                    "entry_price": ep, "final_price": mp,
                    "pnl_pct": res["pnl_pct"], "gross_pnl": gp,
                    "days_held": res["days_held"], "max_price": res["max_price_seen"]})

        strategy_details[strat_id] = {"sl_hit": sl_hit_trades, "survived": survived_trades}

        total = len(sl_hit_trades) + len(survived_trades)
        ns = len(survived_trades)
        nh = len(sl_hit_trades)
        alp = np.mean([t["pnl_pct"] for t in sl_hit_trades]) if sl_hit_trades else 0
        agp = np.mean([t["pnl_pct"] for t in survived_trades]) if survived_trades else 0
        ap_list = [t["gross_pnl"] for t in sl_hit_trades] + [t["gross_pnl"] for t in survived_trades]
        tp = sum(ap_list)
        apcts = [t["pnl_pct"] for t in sl_hit_trades] + [t["pnl_pct"] for t in survived_trades]
        npos = sum(1 for p in apcts if p > 0)
        wr = npos / total * 100 if total > 0 else 0
        avgp = np.mean(apcts) if apcts else 0
        gpr = sum(p for p in ap_list if p > 0)
        gls = abs(sum(p for p in ap_list if p < 0))
        pf = gpr / gls if gls > 0 else float("inf")
        ml = min(apcts) if apcts else 0
        bg = max(apcts) if apcts else 0
        ad = [t["days_held"] for t in sl_hit_trades] + [t["days_held"] for t in survived_trades]
        avgd = np.mean(ad) if ad else 0

        surv_label = f"{agp:+.2f}%" if ns > 0 else "N/A"
        pf_label = f"{pf:.2f}" if pf < 100 else "INF"

        summary_rows.append({
            "Strategy": STRATEGY_NAMES[strat_id],
            "Survived": ns, "SL_Hit": nh,
            "AvgLoss%(SL)": f"{alp:+.2f}%",
            "AvgGain%(Surv)": surv_label,
            "TotalPnL(Rs)": f"{tp:+,.0f}",
            "WinRate": f"{wr:.1f}%",
            "AvgPnL%": f"{avgp:+.2f}%",
            "PF": pf_label,
            "MaxLoss%": f"{ml:+.2f}%",
            "BestGain%": f"{bg:+.2f}%",
            "AvgDays": f"{avgd:.0f}",
        })

    print()
    print(tabulate(summary_rows, headers="keys", tablefmt="grid", stralign="right"))
    print()

    atp = sum(tr["actual_net_pnl"] for tr in all_results)
    aap = np.mean([tr["actual_pnl_pct"] for tr in all_results])
    print(f"  [ACTUAL SYSTEM] Total Net P&L: Rs {atp:+,.0f} | Avg P&L%: {aap:+.2f}% | {len(all_results)} trades")
    print()

    strat_scores = []
    for sid in range(6):
        sp = [tr[f"S{sid}"]["pnl_pct"] for tr in all_results]
        strat_scores.append((sid, np.mean(sp)))
    strat_scores.sort(key=lambda x: x[1], reverse=True)
    top_2 = [strat_scores[0][0], strat_scores[1][0]]

    print("=" * 100)
    print("  DETAILED TRADE-BY-TRADE: TOP 2 STRATEGIES")
    print("=" * 100)

    for sid in top_2:
        sep = "~" * 90
        print()
        print(sep)
        print(f"  {STRATEGY_NAMES[sid]}")
        print(sep)
        drows = []
        for tr in all_results:
            res = tr[f"S{sid}"]
            act = tr["actual_pnl_pct"]
            if res["sl_hit"]:
                exi = "SL@" + str(res["exit_price"])
                edt = res["exit_date"].strftime("%Y-%m-%d") if res["exit_date"] else "-"
            else:
                exi = "MTM@" + str(res["final_price"])
                edt = "SURVIVED"
            imp = res["pnl_pct"] - act
            drows.append({
                "Symbol": tr["symbol"],
                "Entry": tr["entry_date"].strftime("%Y-%m-%d"),
                "EntPx": str(round(tr["entry_price"], 2)),
                "Exit": exi, "ExitDate": edt,
                "PnL%": f"{res['pnl_pct']:+.2f}%",
                "Actual%": f"{act:+.2f}%",
                "Improv": f"{imp:+.2f}%",
                "Days": res["days_held"],
                "MaxPx": str(res["max_price_seen"]),
            })
        print(tabulate(drows, headers="keys", tablefmt="grid", stralign="right"))

    print()
    print("=" * 100)
    print("  TRADE-BY-TRADE: ALL STRATEGIES COMPARISON")
    print("=" * 100)
    crows = []
    for tr in all_results:
        row = {"Symbol": tr["symbol"],
               "Entry": tr["entry_date"].strftime("%m/%d"),
               "Actual%": f"{tr['actual_pnl_pct']:+.2f}"}
        for sid in range(6):
            res = tr[f"S{sid}"]
            tag = "*" if not res["sl_hit"] else ""
            row[f"S{sid}%"] = f"{res['pnl_pct']:+.2f}" + tag
        crows.append(row)
    print("(* = survived, no SL hit)")
    print()
    print(tabulate(crows, headers="keys", tablefmt="grid", stralign="right"))

    print()
    print("=" * 100)
    print("  RECOMMENDATION")
    print("=" * 100)
    print()
    print("  Strategy Rankings (best to worst):")
    print()
    rrows = []
    for sid, ap_val in strat_scores:
        det = strategy_details[sid]
        ns2 = len(det["survived"])
        nh2 = len(det["sl_hit"])
        ap2 = [t["pnl_pct"] for t in det["sl_hit"]] + [t["pnl_pct"] for t in det["survived"]]
        ml2 = min(ap2) if ap2 else 0
        wr2 = sum(1 for p in ap2 if p > 0) / len(ap2) * 100 if ap2 else 0
        rrows.append({
            "Rank": len(rrows) + 1,
            "Strategy": STRATEGY_NAMES[sid],
            "AvgPnL%": f"{ap_val:+.2f}%",
            "Survived": ns2, "SL_Hit": nh2,
            "WinRate": f"{wr2:.1f}%",
            "MaxLoss%": f"{ml2:+.2f}%",
        })
    print(tabulate(rrows, headers="keys", tablefmt="grid", stralign="right"))

    bid = strat_scores[0][0]
    sid2 = strat_scores[1][0]
    print()
    print(f"  BEST STRATEGY:    {STRATEGY_NAMES[bid]}")
    print(f"  RUNNER-UP:        {STRATEGY_NAMES[sid2]}")
    print()
    print("  Key Observations:")
    print("  1. Baseline uses week LOW as trailing SL - very tight.")
    print("  2. Strategies with more SL room allow trades to survive dips.")
    print("  3. No-trail-until-+5% keeps wide initial SL until trade proves itself.")
    print("  4. The 5% floor prevents SL from getting too close to current price.")
    print()
    print(f"  RECOMMENDATION: Implement {STRATEGY_NAMES[bid]}")
    print(f"  FALLBACK:       {STRATEGY_NAMES[sid2]}")
    print("  Forward-test for 4-6 weeks before full deployment.")
    print()

    if skipped:
        print(f"  NOTE: {len(skipped)} trades skipped (no data): {skipped}")

    print("=" * 100)
    print("  BACKTEST COMPLETE")
    print("=" * 100)


if __name__ == "__main__":
    main()
