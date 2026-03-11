"""
Weekly SuperTrend SL Analysis Script
"""

import sqlite3
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
from typing import Optional
import warnings
import time
import os

warnings.filterwarnings("ignore")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trading.db")


def supertrend(df, period=10, multiplier=3):
    df = df.copy()
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = pd.Series(index=df.index, dtype=float)
    atr.iloc[period - 1] = tr.iloc[:period].mean()
    for i in range(period, len(tr)):
        atr.iloc[i] = (atr.iloc[i - 1] * (period - 1) + tr.iloc[i]) / period
    hl2 = (high + low) / 2
    basic_upper = hl2 + multiplier * atr
    basic_lower = hl2 - multiplier * atr
    final_upper = pd.Series(index=df.index, dtype=float)
    final_lower = pd.Series(index=df.index, dtype=float)
    st_val = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=float)
    for i in range(period, len(df)):
        if i == period:
            final_upper.iloc[i] = basic_upper.iloc[i]
            final_lower.iloc[i] = basic_lower.iloc[i]
            direction.iloc[i] = 1 if close.iloc[i] > basic_upper.iloc[i] else -1
        else:
            final_upper.iloc[i] = (basic_upper.iloc[i]
                                   if (basic_upper.iloc[i] < final_upper.iloc[i - 1]
                                       or close.iloc[i - 1] > final_upper.iloc[i - 1])
                                   else final_upper.iloc[i - 1])
            final_lower.iloc[i] = (basic_lower.iloc[i]
                                   if (basic_lower.iloc[i] > final_lower.iloc[i - 1]
                                       or close.iloc[i - 1] < final_lower.iloc[i - 1])
                                   else final_lower.iloc[i - 1])
            if direction.iloc[i - 1] == 1:
                direction.iloc[i] = -1 if close.iloc[i] < final_lower.iloc[i] else 1
            else:
                direction.iloc[i] = 1 if close.iloc[i] > final_upper.iloc[i] else -1
        st_val.iloc[i] = final_lower.iloc[i] if direction.iloc[i] == 1 else final_upper.iloc[i]
    df["SuperTrend"] = st_val
    df["ST_Direction"] = direction
    df["ATR"] = atr
    return df

def fetch_weekly_data(symbol, entry_date):
    ticker = f"{symbol}.NS"
    entry_dt = pd.Timestamp(entry_date)
    start = entry_dt - timedelta(days=800)
    end = entry_dt + timedelta(days=120)
    try:
        df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                         end=end.strftime("%Y-%m-%d"), interval="1wk",
                         progress=False, auto_adjust=True)
        if df.empty:
            print(f"  WARNING: No weekly data for {ticker}")
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        print(f"  ERROR weekly {ticker}: {e}")
        return None


def fetch_daily_data(symbol, entry_date):
    ticker = f"{symbol}.NS"
    entry_dt = pd.Timestamp(entry_date)
    start = entry_dt - timedelta(days=60)
    end = entry_dt + timedelta(days=120)
    try:
        df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                         end=end.strftime("%Y-%m-%d"), interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty:
            print(f"  WARNING: No daily data for {ticker}")
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        print(f"  ERROR daily {ticker}: {e}")
        return None


def load_trades():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT * FROM closed_positions WHERE timeframe='W' AND exit_reason LIKE '%SL%' ORDER BY entry_date",
        conn)
    conn.close()
    return df

def analyze_part1(trades, weekly_data_cache):
    results = []
    for _, trade in trades.iterrows():
        symbol = trade["script"]
        entry_date = trade["entry_date"]
        entry_price = trade["entry_price"]
        pnl_pct = trade["pnl_percent"]

        wdf = weekly_data_cache.get(symbol)
        if wdf is None:
            results.append({"symbol": symbol, "entry_date": entry_date,
                "entry_price": entry_price, "pnl_pct": pnl_pct,
                "category": "UNK", "st_value": None, "st_direction": None,
                "atr_at_entry": None, "st_vs_entry_pct": None,
                "s5_sl": None, "s5_sl_pct": None, "reached_5pct": None, "lowest_12w": None})
            continue

        wdf_st = supertrend(wdf)
        entry_dt = pd.Timestamp(entry_date)
        mask = wdf_st.index <= entry_dt
        if not mask.any():
            results.append({"symbol": symbol, "entry_date": entry_date,
                "entry_price": entry_price, "pnl_pct": pnl_pct,
                "category": "UNK", "st_value": None, "st_direction": None,
                "atr_at_entry": None, "st_vs_entry_pct": None,
                "s5_sl": None, "s5_sl_pct": None, "reached_5pct": None, "lowest_12w": None})
            continue

        idx = wdf_st.index[mask][-1]
        st_val = wdf_st.loc[idx, "SuperTrend"]
        st_dir = wdf_st.loc[idx, "ST_Direction"]
        atr_val = wdf_st.loc[idx, "ATR"]

        if pd.isna(st_val) or pd.isna(st_dir):
            category = "UNK"
        elif st_dir == 1:
            category = "A"
        else:
            category = "B"

        st_vs = ((st_val - entry_price) / entry_price * 100) if not pd.isna(st_val) else None

        if not pd.isna(st_val) and st_dir == 1:
            s5_sl = max(st_val, entry_price * 0.85)
        else:
            s5_sl = entry_price * 0.85
        s5_sl_pct = (s5_sl - entry_price) / entry_price * 100

        future_bars = wdf_st[wdf_st.index > entry_dt].head(12)
        reached_5pct = False
        lowest_low = None
        if not future_bars.empty:
            reached_5pct = future_bars["High"].max() >= entry_price * 1.05
            lowest_low = future_bars["Low"].min()

        results.append({"symbol": symbol, "entry_date": entry_date,
            "entry_price": entry_price, "pnl_pct": pnl_pct,
            "category": category, "st_value": st_val, "st_direction": st_dir,
            "atr_at_entry": atr_val, "st_vs_entry_pct": st_vs,
            "s5_sl": s5_sl, "s5_sl_pct": s5_sl_pct,
            "reached_5pct": reached_5pct, "lowest_12w": lowest_low})
    return results

def analyze_part2(trades, weekly_data_cache, part1_results):
    results = []
    for i, (_, trade) in enumerate(trades.iterrows()):
        symbol = trade["script"]
        entry_date = trade["entry_date"]
        entry_price = trade["entry_price"]
        entry_dt = pd.Timestamp(entry_date)
        atr = part1_results[i].get("atr_at_entry")

        if atr is None or pd.isna(atr):
            results.append({"symbol": symbol, "entry_date": entry_date,
                "entry_price": entry_price, "atr": None, "atr_pct": None,
                "sl_levels": {}, "lowest_12w": None})
            continue

        atr_pct = atr / entry_price * 100
        sl_levels = {
            "1.5xATR": entry_price - 1.5 * atr,
            "2.0xATR": entry_price - 2.0 * atr,
            "2.5xATR": entry_price - 2.5 * atr,
            "3.0xATR": entry_price - 3.0 * atr,
            "5pct": entry_price * 0.95,
            "7pct": entry_price * 0.93,
            "10pct": entry_price * 0.90,
        }

        wdf = weekly_data_cache.get(symbol)
        future_bars = pd.DataFrame()
        if wdf is not None:
            future_bars = wdf[wdf.index > entry_dt].head(12)

        lowest_12w = future_bars["Low"].min() if not future_bars.empty else None

        sl_analysis = {}
        for name, sl_val in sl_levels.items():
            hit = False
            hit_week = None
            sl_pct = (sl_val - entry_price) / entry_price * 100
            if not future_bars.empty:
                for week_num, (widx, row) in enumerate(future_bars.iterrows(), 1):
                    if row["Low"] <= sl_val:
                        hit = True
                        hit_week = week_num
                        break
            sl_analysis[name] = {"sl_value": sl_val, "sl_pct": sl_pct,
                                 "hit": hit, "hit_week": hit_week}

        results.append({"symbol": symbol, "entry_date": entry_date,
            "entry_price": entry_price, "atr": atr, "atr_pct": atr_pct,
            "sl_levels": sl_analysis, "lowest_12w": lowest_12w})
    return results

def simulate_s4_refined(symbol, entry_date, entry_price, daily_data, weekly_data, atr_at_entry):
    entry_dt = pd.Timestamp(entry_date)
    end_dt = entry_dt + timedelta(days=84)
    if atr_at_entry is None or pd.isna(atr_at_entry):
        return {"status": "NO_ATR", "exit_price": None, "pnl_pct": None}

    initial_sl = entry_price - 2.0 * atr_at_entry
    trailing_threshold = entry_price + 1.5 * atr_at_entry
    current_sl = initial_sl
    trailing_active = False
    trailing_start_date = None

    mask = (daily_data.index > entry_dt) & (daily_data.index <= end_dt)
    sim_data = daily_data[mask]
    if sim_data.empty:
        return {"status": "NO_DATA", "exit_price": None, "pnl_pct": None}

    wdf_st = supertrend(weekly_data)
    sl_history = [{"date": entry_date, "sl": current_sl, "event": "INITIAL"}]

    for idx, row in sim_data.iterrows():
        if row["Low"] <= current_sl:
            pnl_pct = (current_sl - entry_price) / entry_price * 100
            return {"status": "SL_HIT", "exit_price": current_sl, "pnl_pct": pnl_pct,
                "exit_date": str(idx.date()), "initial_sl": initial_sl,
                "initial_sl_pct": (initial_sl - entry_price) / entry_price * 100,
                "trailing_started": trailing_active, "trailing_start_date": trailing_start_date,
                "final_sl": current_sl, "sl_history": sl_history,
                "days_held": (idx - entry_dt).days}

        if not trailing_active and row["High"] >= trailing_threshold:
            trailing_active = True
            trailing_start_date = str(idx.date())
            sl_history.append({"date": str(idx.date()), "sl": current_sl, "event": "TRAILING_ON"})

        if trailing_active and idx.weekday() == 4:
            w_mask = wdf_st.index <= idx
            if w_mask.any():
                w_idx = wdf_st.index[w_mask][-1]
                curr_atr = wdf_st.loc[w_idx, "ATR"]
                if not pd.isna(curr_atr):
                    new_sl = row["Close"] - 2.0 * curr_atr
                    if new_sl > current_sl:
                        current_sl = new_sl
                        sl_history.append({"date": str(idx.date()), "sl": current_sl, "event": "TRAIL_UP"})

    last_close = sim_data["Close"].iloc[-1]
    pnl_pct = (last_close - entry_price) / entry_price * 100
    return {"status": "SURVIVED", "exit_price": last_close, "pnl_pct": pnl_pct,
        "exit_date": str(sim_data.index[-1].date()), "initial_sl": initial_sl,
        "initial_sl_pct": (initial_sl - entry_price) / entry_price * 100,
        "trailing_started": trailing_active, "trailing_start_date": trailing_start_date,
        "final_sl": current_sl, "sl_history": sl_history,
        "days_held": (sim_data.index[-1] - entry_dt).days}

def simulate_s4_simple(symbol, entry_date, entry_price, daily_data):
    entry_dt = pd.Timestamp(entry_date)
    end_dt = entry_dt + timedelta(days=84)
    initial_sl = entry_price * 0.93
    current_sl = initial_sl
    trailing_active = False
    trailing_start_date = None

    mask = (daily_data.index > entry_dt) & (daily_data.index <= end_dt)
    sim_data = daily_data[mask]
    if sim_data.empty:
        return {"status": "NO_DATA", "exit_price": None, "pnl_pct": None}

    sl_history = [{"date": entry_date, "sl": current_sl, "event": "INITIAL"}]

    for idx, row in sim_data.iterrows():
        if row["Low"] <= current_sl:
            pnl_pct = (current_sl - entry_price) / entry_price * 100
            return {"status": "SL_HIT", "exit_price": current_sl, "pnl_pct": pnl_pct,
                "exit_date": str(idx.date()), "initial_sl": initial_sl,
                "initial_sl_pct": -7.0,
                "trailing_started": trailing_active, "trailing_start_date": trailing_start_date,
                "final_sl": current_sl, "sl_history": sl_history,
                "days_held": (idx - entry_dt).days}

        if idx.weekday() == 4:
            if not trailing_active and row["Close"] >= entry_price * 1.05:
                trailing_active = True
                trailing_start_date = str(idx.date())
                sl_history.append({"date": str(idx.date()), "sl": current_sl, "event": "TRAILING_ON"})
            if trailing_active:
                new_sl = row["Close"] * 0.93
                if new_sl > current_sl:
                    current_sl = new_sl
                    sl_history.append({"date": str(idx.date()), "sl": current_sl, "event": "TRAIL_UP"})

    last_close = sim_data["Close"].iloc[-1]
    pnl_pct = (last_close - entry_price) / entry_price * 100
    return {"status": "SURVIVED", "exit_price": last_close, "pnl_pct": pnl_pct,
        "exit_date": str(sim_data.index[-1].date()), "initial_sl": initial_sl,
        "initial_sl_pct": -7.0,
        "trailing_started": trailing_active, "trailing_start_date": trailing_start_date,
        "final_sl": current_sl, "sl_history": sl_history,
        "days_held": (sim_data.index[-1] - entry_dt).days}

def simulate_s0_baseline(symbol, entry_date, entry_price, weekly_data):
    entry_dt = pd.Timestamp(entry_date)
    wdf_st = supertrend(weekly_data)
    w_mask = wdf_st.index <= entry_dt
    if not w_mask.any():
        return {"status": "NO_DATA", "exit_price": None, "pnl_pct": None}

    w_idx_val = wdf_st.index[w_mask][-1]
    initial_sl = wdf_st.loc[w_idx_val, "SuperTrend"]
    if pd.isna(initial_sl):
        return {"status": "NO_DATA", "exit_price": None, "pnl_pct": None}

    future_w = wdf_st[wdf_st.index > entry_dt].head(12)
    for w_idx, w_row in future_w.iterrows():
        if w_row["ST_Direction"] == -1:
            exit_price = w_row["Close"]
            pnl_pct = (exit_price - entry_price) / entry_price * 100
            return {"status": "ST_EXIT", "exit_price": exit_price, "pnl_pct": pnl_pct,
                "exit_date": str(w_idx.date()),
                "initial_sl": initial_sl,
                "initial_sl_pct": (initial_sl - entry_price) / entry_price * 100,
                "days_held": (w_idx - entry_dt).days}

    if not future_w.empty:
        last_close = future_w["Close"].iloc[-1]
        pnl_pct = (last_close - entry_price) / entry_price * 100
        return {"status": "SURVIVED", "exit_price": last_close, "pnl_pct": pnl_pct,
            "exit_date": str(future_w.index[-1].date()),
            "initial_sl": initial_sl,
            "initial_sl_pct": (initial_sl - entry_price) / entry_price * 100,
            "days_held": (future_w.index[-1] - entry_dt).days}
    return {"status": "NO_DATA", "exit_price": None, "pnl_pct": None}


def simulate_s4_original(symbol, entry_date, entry_price, daily_data):
    entry_dt = pd.Timestamp(entry_date)
    end_dt = entry_dt + timedelta(days=84)
    initial_sl = entry_price * 0.85
    current_sl = initial_sl
    trailing_active = False
    trailing_start_date = None

    mask = (daily_data.index > entry_dt) & (daily_data.index <= end_dt)
    sim_data = daily_data[mask]
    if sim_data.empty:
        return {"status": "NO_DATA", "exit_price": None, "pnl_pct": None}

    for idx, row in sim_data.iterrows():
        if row["Low"] <= current_sl:
            pnl_pct = (current_sl - entry_price) / entry_price * 100
            return {"status": "SL_HIT", "exit_price": current_sl, "pnl_pct": pnl_pct,
                "exit_date": str(idx.date()), "initial_sl": initial_sl,
                "initial_sl_pct": -15.0,
                "trailing_started": trailing_active, "trailing_start_date": trailing_start_date,
                "final_sl": current_sl, "days_held": (idx - entry_dt).days}

        if idx.weekday() == 4:
            if not trailing_active and row["Close"] >= entry_price * 1.05:
                trailing_active = True
                trailing_start_date = str(idx.date())
            if trailing_active:
                new_sl = row["Close"] * 0.95
                if new_sl > current_sl:
                    current_sl = new_sl

    last_close = sim_data["Close"].iloc[-1]
    pnl_pct = (last_close - entry_price) / entry_price * 100
    return {"status": "SURVIVED", "exit_price": last_close, "pnl_pct": pnl_pct,
        "exit_date": str(sim_data.index[-1].date()), "initial_sl": initial_sl,
        "initial_sl_pct": -15.0,
        "trailing_started": trailing_active, "trailing_start_date": trailing_start_date,
        "final_sl": current_sl, "days_held": (sim_data.index[-1] - entry_dt).days}

def sep(char="=", width=120):
    print(char * width)

def header(title):
    print()
    sep()
    print(f"  {title}")
    sep()
    print()

def compute_stats(results):
    valid = [r for r in results if r.get("pnl_pct") is not None]
    if not valid:
        return {}
    pnls = [r["pnl_pct"] for r in valid]
    sl_hits = [r for r in valid if r.get("status") == "SL_HIT"]
    survivors = [r for r in valid if r.get("status") in ("SURVIVED", "ST_EXIT")]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0.001
    days_list = [r.get("days_held", 0) for r in valid if r.get("days_held")]
    return {
        "total": len(valid), "sl_hits": len(sl_hits), "survivors": len(survivors),
        "avg_pnl": np.mean(pnls), "median_pnl": np.median(pnls),
        "win_rate": len(wins) / len(valid) * 100 if valid else 0,
        "total_pnl": sum(pnls),
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else float("inf"),
        "worst_loss": min(pnls), "best_win": max(pnls),
        "avg_days": np.mean(days_list) if days_list else 0}

def main():
    header("WEEKLY SUPERTREND SL ANALYSIS")
    print("Loading trades from database...")
    trades = load_trades()
    print(f"Found {len(trades)} Weekly SL-hit trades")
    print()

    print("Fetching market data...")
    weekly_cache = {}
    daily_cache = {}
    symbols = trades["script"].unique()
    for i, sym in enumerate(symbols):
        entries = trades[trades["script"] == sym]["entry_date"].values
        earliest = min(entries)
        print(f"  [{i+1}/{len(symbols)}] {sym}...", end=" ", flush=True)
        wdf = fetch_weekly_data(sym, earliest)
        if wdf is not None:
            weekly_cache[sym] = wdf
            print(f"w={len(wdf)}", end=" ", flush=True)
        ddf = fetch_daily_data(sym, earliest)
        if ddf is not None:
            daily_cache[sym] = ddf
            print(f"d={len(ddf)}", end="", flush=True)
        print()
        time.sleep(0.3)

    # === PART 1 ===
    header("PART 1: TRADE CATEGORIZATION & S4/S5 DUMMY SL EXPOSURE")
    p1 = analyze_part1(trades, weekly_cache)

    hdr = f"{chr(35):>3} {chr(83)+'ymbol':<15} {chr(69)+'ntryDate':<12} {chr(69)+'ntry':>9} {chr(67)+'at':>4} {chr(83)+'TDir':>5} "
    hdr += f"{chr(83)+'T_Val':>9} {chr(83)+'T_vs_E%':>9} {chr(83)+'5_SL':>9} {chr(83)+'5%':>7} {'+5%?':>5} {chr(80)+'nL%':>7}"
    print(hdr)
    print("-" * 115)
    cat_a, cat_b, cat_u = [], [], []
    for i, r in enumerate(p1):
        c = r["category"]
        if c == "A":
            cat_a.append(r)
        elif c == "B":
            cat_b.append(r)
        else:
            cat_u.append(r)
        sv = f"{r['st_value']:.1f}" if r["st_value"] is not None and not pd.isna(r.get("st_value", np.nan)) else "N/A"
        sp = f"{r['st_vs_entry_pct']:.2f}" if r["st_vs_entry_pct"] is not None else "N/A"
        s5v = f"{r['s5_sl']:.1f}" if r.get("s5_sl") is not None else "N/A"
        s5p = f"{r['s5_sl_pct']:.1f}" if r.get("s5_sl_pct") is not None else "N/A"
        sd = f"{int(r['st_direction'])}" if r.get("st_direction") is not None and not pd.isna(r.get("st_direction", np.nan)) else "N/A"
        r5 = "Y" if r.get("reached_5pct") else "N"
        print(f"{i+1:>3} {r['symbol']:<15} {r['entry_date']:<12} {r['entry_price']:>9.2f} "
              f"{c:>4} {sd:>5} {sv:>9} {sp:>9} {s5v:>9} {s5p:>7} {r5:>5} {r['pnl_pct']:>7.2f}")

    print()
    print("  CATEGORY SUMMARY:")
    print(f"    Cat A (Bullish ST, ST < price): {len(cat_a)} trades")
    print(f"    Cat B (Bearish ST, ST > price): {len(cat_b)} trades")
    print(f"    Unknown:                        {len(cat_u)} trades")

    print()
    print("  --- S5 ANALYSIS ---")
    print("  S5 formula: SL = max(ST_value, Entry * 0.85)")
    if cat_a:
        a_st = [r["st_vs_entry_pct"] for r in cat_a if r["st_vs_entry_pct"] is not None]
        a_s5 = [r["s5_sl_pct"] for r in cat_a if r["s5_sl_pct"] is not None]
        print(f"    Cat A ({len(cat_a)} trades): ST is BELOW entry (bullish)")
        if a_st:
            print(f"      Avg ST distance from entry: {np.mean(a_st):.2f}% (negative = below)")
        if a_s5:
            print(f"      Avg S5 SL from entry: {np.mean(a_s5):.2f}%")
        print(f"      ISSUE: ST is right at entry, so S5 SL = tiny buffer = stopped quickly")
    if cat_b:
        b_s5 = [r["s5_sl_pct"] for r in cat_b if r["s5_sl_pct"] is not None]
        print(f"    Cat B ({len(cat_b)} trades): ST is ABOVE entry (bearish)")
        print(f"      S5 SL = Entry * 0.85 (dummy) since ST above entry is useless for long SL")
        if b_s5:
            print(f"      Avg S5 SL from entry: {np.mean(b_s5):.2f}%")
        print(f"      ISSUE: S5 success is purely from 15% dummy cushion")

    print()
    print("  --- S4 ANALYSIS ---")
    print("  S4: 15% dummy SL + trailing after +5%")
    r5_yes = [r for r in p1 if r.get("reached_5pct")]
    r5_no = [r for r in p1 if r.get("reached_5pct") is not None and not r.get("reached_5pct")]
    print(f"    Reached +5% (trailing activated): {len(r5_yes)} trades")
    print(f"    NEVER reached +5% (on 15% dummy): {len(r5_no)} trades")
    if r5_no:
        dummy_hit = [r for r in r5_no if r["pnl_pct"] <= -14.5]
        dummy_surv = [r for r in r5_no if r["pnl_pct"] > -14.5]
        print(f"      Hit 15% dummy: {len(dummy_hit)} | Survived (loss < 15%): {len(dummy_surv)}")
        print(f"      CONCLUSION: S4 survival = wide 15% stop never triggered")

    # === PART 2 ===
    header("PART 2: ATR-BASED SL LEVEL ANALYSIS")
    p2 = analyze_part2(trades, weekly_cache, p1)

    valid_atrs = [r for r in p2 if r["atr"] is not None]
    if valid_atrs:
        ap = [r["atr_pct"] for r in valid_atrs]
        print(f"  Weekly ATR(10) Stats ({len(valid_atrs)} trades):")
        print(f"    Min: {min(ap):.2f}%  Max: {max(ap):.2f}%  Mean: {np.mean(ap):.2f}%  Median: {np.median(ap):.2f}%")
        print()

    sl_names = ["1.5xATR", "2.0xATR", "2.5xATR", "3.0xATR", "5pct", "7pct", "10pct"]
    sl_labels = ["1.5xATR", "2.0xATR", "2.5xATR", "3.0xATR", "5%fix", "7%fix", "10%fix"]

    print(f"{'N':>3} {'Symbol':<15} {'Entry':>9} {'ATR':>7} {'ATR%':>6} ", end="")
    for lb in sl_labels:
        print(f"{lb:>8}", end="")
    print(f" {'Low12w':>9}")
    print("-" * 128)
    for i, r in enumerate(p2):
        if r["atr"] is None:
            print(f"{i+1:>3} {r['symbol']:<15} {r['entry_price']:>9.2f} {'N/A':>7}")
            continue
        s = r["sl_levels"]
        l12 = f"{r['lowest_12w']:.1f}" if r["lowest_12w"] is not None else "N/A"
        print(f"{i+1:>3} {r['symbol']:<15} {r['entry_price']:>9.2f} {r['atr']:>7.1f} {r['atr_pct']:>5.1f}% ", end="")
        for sn in sl_names:
            print(f"{s[sn]['sl_value']:>8.1f}", end="")
        print(f" {l12:>9}")

    print()
    print("  HIT DETAIL (Wn=hit week n, -=survived):")
    print(f"  {'N':>3} {'Symbol':<15}", end="")
    for lb in sl_labels:
        print(f" {lb:>8}", end="")
    print()
    print(f"  {'-'*85}")
    for i, r in enumerate(p2):
        if not r["sl_levels"]:
            continue
        print(f"  {i+1:>3} {r['symbol']:<15}", end="")
        for sn in sl_names:
            info = r["sl_levels"].get(sn, {})
            val = "W" + str(info["hit_week"]) if info.get("hit") else "-"
            print(f" {val:>8}", end="")
        print()

    print()
    print("  SURVIVAL SUMMARY:")
    print(f"  {'SL Level':<15} {'Surv':>6} {'Hit':>6} {'Surv%':>8} {'AvgHitWk':>10}")
    print(f"  {'-'*50}")
    for j, sn in enumerate(sl_names):
        surv = hit = 0
        hw = []
        for r in p2:
            if r["sl_levels"] and sn in r["sl_levels"]:
                si = r["sl_levels"][sn]
                if si["hit"]:
                    hit += 1
                    if si["hit_week"]:
                        hw.append(si["hit_week"])
                else:
                    surv += 1
        t = surv + hit
        sp = surv / t * 100 if t > 0 else 0
        aw = f"{np.mean(hw):.1f}" if hw else "-"
        print(f"  {sl_labels[j]:<15} {surv:>6} {hit:>6} {sp:>7.1f}% {aw:>10}")

    # === PART 3 ===
    header("PART 3: STRATEGY SIMULATIONS")
    s0_res, s4o_res, s4r_res, s4s_res = [], [], [], []

    for i, (_, trade) in enumerate(trades.iterrows()):
        sym = trade["script"]
        ed = trade["entry_date"]
        ep = trade["entry_price"]
        atr = p1[i].get("atr_at_entry")
        wdf = weekly_cache.get(sym)
        ddf = daily_cache.get(sym)
        print(f"  Sim {sym} ({ed})...", flush=True)

        if wdf is not None:
            s0_res.append(simulate_s0_baseline(sym, ed, ep, wdf))
        else:
            s0_res.append({"status": "NO_DATA", "exit_price": None, "pnl_pct": None})

        if ddf is not None:
            s4o_res.append(simulate_s4_original(sym, ed, ep, ddf))
        else:
            s4o_res.append({"status": "NO_DATA", "exit_price": None, "pnl_pct": None})

        if ddf is not None and wdf is not None and atr is not None and not pd.isna(atr):
            s4r_res.append(simulate_s4_refined(sym, ed, ep, ddf, wdf, atr))
        else:
            s4r_res.append({"status": "NO_DATA", "exit_price": None, "pnl_pct": None})

        if ddf is not None:
            s4s_res.append(simulate_s4_simple(sym, ed, ep, ddf))
        else:
            s4s_res.append({"status": "NO_DATA", "exit_price": None, "pnl_pct": None})

    # === TABLE 1 ===
    header("TABLE 1: CATEGORY BREAKDOWN - What S4/S5 Actually Did")
    print(f"{'N':>3} {'Symbol':<15} {'Cat':>4} {'PnL%':>8} {'S5_SL%':>8} {'S5=Dummy?':>12} {'+5%?':>5} {'S4=Dummy?':>10}")
    print("-" * 80)
    for i, r in enumerate(p1):
        if r["category"] == "B":
            s5d = "YES(15%)"
        elif r["category"] == "A":
            stp = abs(r.get("st_vs_entry_pct", 0) or 0)
            s5d = f"~Ent({stp:.1f}%)" if stp < 5 else f"Real({stp:.1f}%)"
        else:
            s5d = "N/A"
        s4d = "YES(15%)" if not r.get("reached_5pct") else "Trail"
        s5p = f"{r['s5_sl_pct']:.1f}%" if r.get("s5_sl_pct") is not None else "N/A"
        print(f"{i+1:>3} {r['symbol']:<15} {r['category']:>4} {r['pnl_pct']:>8.2f} "
              f"{s5p:>8} {s5d:>12} {'Y' if r.get('reached_5pct') else 'N':>5} {s4d:>10}")

    s5dc = sum(1 for r in p1 if r["category"] == "B")
    s5ne = sum(1 for r in p1 if r["category"] == "A" and abs(r.get("st_vs_entry_pct", 0) or 0) < 5)
    s4dc = sum(1 for r in p1 if not r.get("reached_5pct"))
    print()
    print("  VERDICT:")
    print(f"    S5: {s5dc} used 15% dummy | {s5ne} had ST within 5% of entry (meaningless SL)")
    print(f"    S4: {s4dc}/{len(p1)} NEVER reached +5%, entirely on 15% dummy")

    # === TABLE 2 ===
    header("TABLE 2: ATR ANALYSIS - Which SL Levels Survive")
    print(f"  {'SL Method':<15} {'AvgDist%':>9} {'Surv':>6} {'Hit':>5} {'Surv%':>7} {'AvgHitWk':>9} {'Verdict':<28}")
    print(f"  {'-'*80}")
    for j, sn in enumerate(sl_names):
        surv = hit = 0
        hw = []
        sp_list = []
        for r in p2:
            if r["sl_levels"] and sn in r["sl_levels"]:
                si = r["sl_levels"][sn]
                sp_list.append(si["sl_pct"])
                if si["hit"]:
                    hit += 1
                    if si["hit_week"]:
                        hw.append(si["hit_week"])
                else:
                    surv += 1
        t = surv + hit
        svp = surv / t * 100 if t > 0 else 0
        avgp = np.mean(sp_list) if sp_list else 0
        aw = f"{np.mean(hw):.1f}" if hw else "-"
        if svp >= 80:
            v = "TOO WIDE"
        elif svp >= 60:
            v = "GOOD - balanced"
        elif svp >= 40:
            v = "MODERATE"
        else:
            v = "TIGHT - mostly hit"
        print(f"  {sl_labels[j]:<15} {avgp:>8.1f}% {surv:>6} {hit:>5} {svp:>6.1f}% {aw:>9} {v:<28}")

    # === TABLE 3 ===
    header("TABLE 3: STRATEGY SIMULATION RESULTS")
    strats = {"S0(ST only)": s0_res, "S4(original)": s4o_res, "S4-refined": s4r_res, "S4-simple(7%)": s4s_res}
    print(f"  {'Strategy':<16} {'N':>4} {'SL':>4} {'Srv':>4} {'Avg%':>7} {'Med%':>7} "
          f"{'Win%':>6} {'Tot%':>8} {'PF':>6} {'Worst':>7} {'Best':>7} {'Days':>6}")
    print(f"  {'-'*90}")
    for name, res in strats.items():
        st = compute_stats(res)
        if not st:
            print(f"  {name:<16} NO DATA")
            continue
        pf = f"{st['profit_factor']:.2f}" if st["profit_factor"] < 100 else "INF"
        print(f"  {name:<16} {st['total']:>4} {st['sl_hits']:>4} {st['survivors']:>4} "
              f"{st['avg_pnl']:>7.2f} {st['median_pnl']:>7.2f} "
              f"{st['win_rate']:>5.1f}% {st['total_pnl']:>8.2f} "
              f"{pf:>6} {st['worst_loss']:>7.2f} {st['best_win']:>7.2f} {st['avg_days']:>6.1f}")

    print()
    print("  Per-Trade PnL% Comparison:")
    print(f"  {'N':>3} {'Symbol':<15} {'S0':>8} {'S4orig':>8} {'S4ref':>8} {'S4simp':>8}")
    print(f"  {'-'*55}")
    for i, (_, t) in enumerate(trades.iterrows()):
        vals = [s0_res[i], s4o_res[i], s4r_res[i], s4s_res[i]]
        strs = []
        for v in vals:
            p = v.get("pnl_pct")
            strs.append(f"{p:.2f}" if p is not None else "N/A")
        print(f"  {i+1:>3} {t['script']:<15} {strs[0]:>8} {strs[1]:>8} {strs[2]:>8} {strs[3]:>8}")

    # === TABLE 4 ===
    header("TABLE 4: TRADE-BY-TRADE - S4-Refined")
    print(f"{'N':>3} {'Symbol':<14} {'Entry':>8} {'InitSL':>8} {'SL%':>6} {'Trl':>4} "
          f"{'TrlDate':<11} {'FinSL':>8} {'Exit':>8} {'Status':<9} {'PnL%':>7} {'D':>4}")
    print("-" * 110)
    for i, (_, t) in enumerate(trades.iterrows()):
        r = s4r_res[i]
        if r.get("status") in ("NO_DATA", "NO_ATR"):
            print(f"{i+1:>3} {t['script']:<14} {t['entry_price']:>8.1f} {'N/A':>8}")
            continue
        td = r.get("trailing_start_date") or "-"
        tr_flag = "Y" if r.get("trailing_started") else "N"
        print(f"{i+1:>3} {t['script']:<14} {t['entry_price']:>8.1f} {r['initial_sl']:>8.1f} "
              f"{r['initial_sl_pct']:>5.1f}% {tr_flag:>4} "
              f"{td:<11} {r['final_sl']:>8.1f} "
              f"{r['exit_price']:>8.1f} {r['status']:<9} {r['pnl_pct']:>7.2f} {r.get('days_held',0):>4}")

    # === TABLE 5 ===
    header("TABLE 5: TRADE-BY-TRADE - S4-Simple (7% fixed)")
    print(f"{'N':>3} {'Symbol':<14} {'Entry':>8} {'InitSL':>8} {'SL%':>6} {'Trl':>4} "
          f"{'TrlDate':<11} {'FinSL':>8} {'Exit':>8} {'Status':<9} {'PnL%':>7} {'D':>4}")
    print("-" * 110)
    for i, (_, t) in enumerate(trades.iterrows()):
        r = s4s_res[i]
        if r.get("status") == "NO_DATA":
            print(f"{i+1:>3} {t['script']:<14} {t['entry_price']:>8.1f} {'N/A':>8}")
            continue
        td = r.get("trailing_start_date") or "-"
        tr_flag = "Y" if r.get("trailing_started") else "N"
        print(f"{i+1:>3} {t['script']:<14} {t['entry_price']:>8.1f} {r['initial_sl']:>8.1f} "
              f"{r['initial_sl_pct']:>5.1f}% {tr_flag:>4} "
              f"{td:<11} {r['final_sl']:>8.1f} "
              f"{r['exit_price']:>8.1f} {r['status']:<9} {r['pnl_pct']:>7.2f} {r.get('days_held',0):>4}")

    # === FINAL SUMMARY ===
    header("FINAL ANALYSIS SUMMARY")
    print("  FINDING 1: WHY S4 AND S5 WORKED")
    print("  " + "-" * 40)
    print("  Both relied on the 15% dummy stop-loss:")
    print("  - S5: For bullish entries, ST value is near entry price (tiny buffer = useless SL).")
    print("        For bearish entries, S5 = 15% dummy always.")
    print("  - S4: Most trades never reached +5% to activate trailing.")
    print("        They sat on the 15% dummy. Survival = loss < 15% (not a strategy).")
    print()
    print("  FINDING 2: THE REAL PROBLEM")
    print("  " + "-" * 40)
    print("  At SuperTrend entry, price is RIGHT AT the ST line:")
    print("  - ST as SL = SL at entry = instant stop on minor dips")
    print("  - Need an INDEPENDENT SL method, not derived from the entry signal")
    print()
    print("  FINDING 3: RECOMMENDED APPROACH")
    print("  " + "-" * 40)
    print("  1. S4-refined: Initial SL = Entry - 2*ATR, trail after Entry + 1.5*ATR")
    print("     -> Adapts to each stock volatility")
    print("  2. S4-simple: Initial SL = 7% below, trail after +5% with 7% distance")
    print("     -> Simple, predictable, no computation needed")
    print()
    print("  Either beats a 15% dummy because it cuts truly failed trades earlier")
    print("  while still giving room for weekly timeframe noise.")
    print()
    print("=" * 120)
    print("  Analysis complete.")
    print("=" * 120)


if __name__ == "__main__":
    main()
