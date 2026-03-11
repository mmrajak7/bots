import sqlite3
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import warnings
import time
import sys

warnings.filterwarnings("ignore")

DB_PATH = "C:/Users/mail2/Documents/Projects/BOTS/data/trading.db"
TODAY = datetime.today().date()


def get_sl_trades():
    conn = sqlite3.connect(DB_PATH)
    query = """
        SELECT script, entry_date, entry_price, exit_date, exit_price,
               net_pnl, pnl_percent, highest_sl_achieved, sl_movements,
               exit_reason, days_held, quantity, capital_deployed
        FROM closed_positions
        WHERE timeframe = 'W' AND exit_reason LIKE '%SL%'
        ORDER BY exit_date
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    df["entry_date"] = pd.to_datetime(df["entry_date"]).dt.date
    df["exit_date"] = pd.to_datetime(df["exit_date"]).dt.date
    return df

def fetch_post_sl_data(symbol, exit_date, entry_price, exit_price):
    yf_symbol = f"{symbol}.NS"
    start = datetime.combine(exit_date, datetime.min.time()) - timedelta(days=1)
    end_8w = datetime.combine(exit_date, datetime.min.time()) + timedelta(weeks=10)
    end = max(end_8w, datetime.combine(TODAY, datetime.min.time()) + timedelta(days=1))

    result = {
        "yf_symbol": yf_symbol, "fetch_success": False,
        "max_2w": None, "max_4w": None, "max_8w": None,
        "current_price": None, "min_1d": None, "min_2d": None,
        "recovery_3pct": None, "recovery_5pct": None,
        "recovery_7pct": None, "recovery_10pct": None,
        "missed_profit_max_4w": None, "missed_profit_max_8w": None,
        "went_above_entry": None, "max_price_ever_after": None,
        "max_price_ever_pct_from_entry": None, "days_to_recover_entry": None,
    }

    try:
        ticker = yf.Ticker(yf_symbol)
        hist = ticker.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
        if hist.empty or len(hist) < 2:
            return result
        result["fetch_success"] = True
        hist.index = hist.index.tz_localize(None)
        exit_dt = pd.Timestamp(exit_date)
        post_exit = hist[hist.index > exit_dt]
        if post_exit.empty:
            post_exit = hist[hist.index >= exit_dt]
            if not post_exit.empty and len(post_exit) > 1:
                post_exit = post_exit.iloc[1:]
            else:
                return result
        if post_exit.empty:
            return result

        result["current_price"] = round(float(post_exit["Close"].iloc[-1]), 2)
        post_2w = post_exit.iloc[:10] if len(post_exit) >= 10 else post_exit
        result["max_2w"] = round(float(post_2w["High"].max()), 2)
        post_4w = post_exit.iloc[:20] if len(post_exit) >= 20 else post_exit
        result["max_4w"] = round(float(post_4w["High"].max()), 2)
        post_8w = post_exit.iloc[:40] if len(post_exit) >= 40 else post_exit
        result["max_8w"] = round(float(post_8w["High"].max()), 2)

        result["max_price_ever_after"] = round(float(post_exit["High"].max()), 2)
        result["max_price_ever_pct_from_entry"] = round(
            (result["max_price_ever_after"] - entry_price) / entry_price * 100, 2)
        result["went_above_entry"] = bool(post_exit["High"].max() > entry_price)

        above_entry = post_exit[post_exit["High"] >= entry_price]
        if not above_entry.empty:
            first_recovery = above_entry.index[0].date()
            result["days_to_recover_entry"] = (first_recovery - exit_date).days

        if len(post_exit) >= 1:
            result["min_1d"] = round(float(post_exit.iloc[:1]["Low"].min()), 2)
        if len(post_exit) >= 2:
            result["min_2d"] = round(float(post_exit.iloc[:2]["Low"].min()), 2)

        for wider_pct, key in [(3, "recovery_3pct"), (5, "recovery_5pct"),
                                (7, "recovery_7pct"), (10, "recovery_10pct")]:
            wider_sl_level = exit_price * (1 - wider_pct / 100)
            check_window = post_exit.iloc[:5] if len(post_exit) >= 5 else post_exit
            never_breached = bool(check_window["Low"].min() > wider_sl_level)
            recovered = bool(check_window["High"].max() > exit_price)
            result[key] = never_breached and recovered

        if result["max_4w"] is not None:
            result["missed_profit_max_4w"] = round(
                (result["max_4w"] - entry_price) / entry_price * 100, 2)
        if result["max_8w"] is not None:
            result["missed_profit_max_8w"] = round(
                (result["max_8w"] - entry_price) / entry_price * 100, 2)

    except Exception as e:
        result["error"] = str(e)
    return result


def sep(char="=", width=140):
    print(char * width)

def header(title):
    print()
    sep()
    print(f"  {title}")
    sep()

def main():
    header("CROCODILE WEEKLY TRADES - POST-SL ANALYSIS")
    print(f"  Analysis Date: {TODAY}")
    print(f"  Database: {DB_PATH}")
    print()

    trades = get_sl_trades()
    print(f"  Total Weekly SL-hit trades found: {len(trades)}")
    print()

    if trades.empty:
        print("  No SL trades found. Exiting.")
        return

    header("SECTION 1: ALL WEEKLY SL TRADES FROM DATABASE")
    print()
    fmt_h = "  {:<3} {:<15} {:<12} {:<10} {:<12} {:<10} {:<8} {:<12} {:<10} {:<6}"
    print(fmt_h.format("#", "Script", "EntryDate", "Entry", "ExitDate", "Exit", "PnL%", "Net PnL", "SL Moves", "Days"))
    print(fmt_h.format("-"*3, "-"*15, "-"*12, "-"*10, "-"*12, "-"*10, "-"*8, "-"*12, "-"*10, "-"*6))

    for i, row in trades.iterrows():
        pnl_str = f"{row['net_pnl']:>+10.2f}"
        pct_str = f"{row['pnl_percent']:>+.2f}%"
        sl_m = row['sl_movements'] if row['sl_movements'] else 0
        print(f"  {i+1:<3} {row['script']:<15} {str(row['entry_date']):<12} "
              f"{row['entry_price']:<10.2f} {str(row['exit_date']):<12} "
              f"{row['exit_price']:<10.2f} {pct_str:<8} {pnl_str:<12} "
              f"{sl_m:<10} {row['days_held']:<6}")

    total_net_pnl = trades["net_pnl"].sum()
    avg_pnl_pct = trades["pnl_percent"].mean()
    total_capital = trades["capital_deployed"].sum()
    print()
    print(f"  TOTALS: Net PnL = {total_net_pnl:+,.2f} INR | Avg PnL% = {avg_pnl_pct:+.2f}% | "
          f"Total Capital Deployed = {total_capital:,.2f} INR")

    header("SECTION 2: FETCHING POST-SL PRICE DATA (via yfinance)")
    print()

    results = []
    failed_symbols = []

    for i, row in trades.iterrows():
        symbol = row["script"]
        sys.stdout.write(f"  [{i+1}/{len(trades)}] Fetching {symbol}...")
        sys.stdout.flush()

        post_data = fetch_post_sl_data(
            symbol, row["exit_date"], row["entry_price"], row["exit_price"]
        )

        if post_data["fetch_success"]:
            print(f" OK (current: {post_data['current_price']})")
        else:
            print(f" FAILED ({post_data.get('error', 'No data')})")
            failed_symbols.append(symbol)

        result_row = {
            "script": symbol,
            "entry_date": row["entry_date"],
            "entry_price": row["entry_price"],
            "exit_date": row["exit_date"],
            "exit_price": row["exit_price"],
            "pnl_percent": row["pnl_percent"],
            "net_pnl": row["net_pnl"],
            "quantity": row["quantity"],
            "sl_movements": row["sl_movements"],
            "days_held": row["days_held"],
            "capital_deployed": row["capital_deployed"],
        }
        result_row.update(post_data)
        results.append(result_row)
        time.sleep(0.3)

    df = pd.DataFrame(results)
    successful = df[df["fetch_success"] == True].copy()

    if failed_symbols:
        print(chr(10) + "  WARNING: Failed to fetch data for: " + ", ".join(failed_symbols))
    print(chr(10) + "  Successfully fetched: " + str(len(successful)) + "/" + str(len(trades)) + " trades")

    if successful.empty:
        print("  No successful fetches. Cannot continue analysis.")
        return

    header("SECTION 3: POST-SL PRICE MOVEMENT ANALYSIS")
    print()
    print(f"  {'Script':<13} {'Exit':<9} {'Max2W':<9} {'Max4W':<9} {'Max8W':<9} "
          f"{'MaxEver':<9} {'Current':<9} {'MissedP%':<10} {'Recov?':<8} {'Days2Rec':<9}")
    print(f"  {'-'*13} {'-'*9} {'-'*9} {'-'*9} {'-'*9} "
          f"{'-'*9} {'-'*9} {'-'*10} {'-'*8} {'-'*9}")

    for _, row in successful.iterrows():
        went_above = "YES" if row.get("went_above_entry") else "NO"
        days_rec = str(int(row["days_to_recover_entry"])) if pd.notna(row.get("days_to_recover_entry")) else "-"
        missed = f"{row.get('missed_profit_max_8w', 0):>+.2f}%" if pd.notna(row.get("missed_profit_max_8w")) else "N/A"

        print(f"  {row['script']:<13} "
              f"{row['exit_price']:<9.2f} "
              f"{(row.get('max_2w') or 0):<9.2f} "
              f"{(row.get('max_4w') or 0):<9.2f} "
              f"{(row.get('max_8w') or 0):<9.2f} "
              f"{(row.get('max_price_ever_after') or 0):<9.2f} "
              f"{(row.get('current_price') or 0):<9.2f} "
              f"{missed:<10} "
              f"{went_above:<8} "
              f"{days_rec:<9}")

    header("SECTION 4: WIDER SL SURVIVAL ANALYSIS")
    print("  Would a wider SL have prevented the exit?")
    print("  (price held above wider SL level AND recovered above exit price within 5 days)")
    print()
    print(f"  {'Script':<13} {'Exit Date':<12} {'SL PnL%':<9} "
          f"{'3%Wider':<9} {'5%Wider':<9} {'7%Wider':<9} {'10%Wider':<9}")
    print(f"  {'-'*13} {'-'*12} {'-'*9} "
          f"{'-'*9} {'-'*9} {'-'*9} {'-'*9}")

    for _, row in successful.iterrows():
        r3 = "SAVED" if row.get("recovery_3pct") else "NO"
        r5 = "SAVED" if row.get("recovery_5pct") else "NO"
        r7 = "SAVED" if row.get("recovery_7pct") else "NO"
        r10 = "SAVED" if row.get("recovery_10pct") else "NO"

        print(f"  {row['script']:<13} {str(row['exit_date']):<12} "
              f"{row['pnl_percent']:>+6.2f}%  "
              f"{r3:<9} {r5:<9} {r7:<9} {r10:<9}")

    header("SECTION 5: MISSED PROFIT ANALYSIS (INR)")
    print("  How much profit was left on the table after premature SL exit?")
    print()
    print(f"  {'Script':<13} {'Qty':<6} {'Entry':<9} {'Exit(SL)':<10} "
          f"{'Max8W':<9} {'MaxEver':<9} {'SL Loss':<12} {'Missed(8W)':<12} {'Missed(Max)':<12}")
    print(f"  {'-'*13} {'-'*6} {'-'*9} {'-'*10} "
          f"{'-'*9} {'-'*9} {'-'*12} {'-'*12} {'-'*12}")

    total_sl_loss = 0
    total_missed_8w = 0
    total_missed_max = 0
    trades_went_above = 0

    for _, row in successful.iterrows():
        qty = row["quantity"]
        entry = row["entry_price"]
        exit_p = row["exit_price"]
        max_8w = row.get("max_8w") or exit_p
        max_ever = row.get("max_price_ever_after") or exit_p

        sl_loss_inr = (exit_p - entry) * qty
        missed_8w_inr = max((max_8w - entry) * qty, 0)
        missed_max_inr = max((max_ever - entry) * qty, 0)

        total_sl_loss += sl_loss_inr
        total_missed_8w += missed_8w_inr
        total_missed_max += missed_max_inr

        if row.get("went_above_entry"):
            trades_went_above += 1

        print(f"  {row['script']:<13} {qty:<6} {entry:<9.2f} {exit_p:<10.2f} "
              f"{max_8w:<9.2f} {max_ever:<9.2f} "
              f"{sl_loss_inr:>+11,.2f} {missed_8w_inr:>+11,.2f} {missed_max_inr:>+11,.2f}")

    print(f"  {'-'*13} {'-'*6} {'-'*9} {'-'*10} {'-'*9} {'-'*9} {'-'*12} {'-'*12} {'-'*12}")
    print(f"  {'TOTAL':<49} "
          f"{total_sl_loss:>+11,.2f} {total_missed_8w:>+11,.2f} {total_missed_max:>+11,.2f}")

    header("SECTION 6: GRAND SUMMARY")
    print()

    n_total = len(successful)
    n_above = trades_went_above
    n_3pct = int(successful["recovery_3pct"].sum()) if "recovery_3pct" in successful.columns else 0
    n_5pct = int(successful["recovery_5pct"].sum()) if "recovery_5pct" in successful.columns else 0
    n_7pct = int(successful["recovery_7pct"].sum()) if "recovery_7pct" in successful.columns else 0
    n_10pct = int(successful["recovery_10pct"].sum()) if "recovery_10pct" in successful.columns else 0

    avg_missed_8w = successful["missed_profit_max_8w"].mean() if "missed_profit_max_8w" in successful.columns else 0
    avg_missed_max = successful["max_price_ever_pct_from_entry"].mean() if "max_price_ever_pct_from_entry" in successful.columns else 0

    recovered = successful[successful["days_to_recover_entry"].notna()]
    avg_days_to_recover = recovered["days_to_recover_entry"].mean() if not recovered.empty else 0

    print(f"  Total Weekly SL trades analyzed:         {n_total}")
    print(f"  Failed to fetch:                         {len(failed_symbols)} ({', '.join(failed_symbols) if failed_symbols else 'None'})")
    print()
    print(f"  Trades that eventually went ABOVE entry: {n_above}/{n_total} ({n_above/n_total*100:.1f}%)")
    print(f"  Avg days to recover above entry:         {avg_days_to_recover:.1f} days (for those that recovered)")
    print()
    print(f"  --- WIDER SL SURVIVAL RATES ---")
    print(f"  Trades saved by  3% wider SL:  {n_3pct:>3}/{n_total} ({n_3pct/n_total*100:.1f}%)")
    print(f"  Trades saved by  5% wider SL:  {n_5pct:>3}/{n_total} ({n_5pct/n_total*100:.1f}%)")
    print(f"  Trades saved by  7% wider SL:  {n_7pct:>3}/{n_total} ({n_7pct/n_total*100:.1f}%)")
    print(f"  Trades saved by 10% wider SL:  {n_10pct:>3}/{n_total} ({n_10pct/n_total*100:.1f}%)")
    print()
    print(f"  --- MISSED PROFIT ---")
    print(f"  Avg missed profit (8-week max from entry):  {avg_missed_8w:+.2f}%")
    print(f"  Avg max move after SL (from entry):         {avg_missed_max:+.2f}%")
    print(f"  Total SL loss (INR):                        {total_sl_loss:>+15,.2f}")
    print(f"  Total missed profit 8W (INR):               {total_missed_8w:>+15,.2f}")
    print(f"  Total missed profit max (INR):              {total_missed_max:>+15,.2f}")
    print()

    print(f"  --- VERDICT ---")
    if n_total > 0:
        if n_above / n_total > 0.5:
            print(f"  WARNING: {n_above/n_total*100:.0f}% of SL-hit trades eventually recovered above entry!")
            print(f"  This suggests SLs may be too tight for Weekly timeframe trades.")
        else:
            print(f"  {n_above/n_total*100:.0f}% of SL-hit trades recovered. SL placement seems reasonable.")

        if n_5pct / n_total > 0.3:
            print(f"  SUGGESTION: A 5% wider SL would have saved {n_5pct/n_total*100:.0f}% of trades.")
            print(f"  Consider widening SL or using ATR-based SL for Weekly trades.")

    print()

    header("SECTION 7: BIGGEST MISSED OPPORTUNITIES (Top 10)")
    print()

    top_missed = successful.nlargest(10, "max_price_ever_pct_from_entry")

    for _, row in top_missed.iterrows():
        print(f"  {row['script']} (Entered: {row['entry_date']} at {row['entry_price']:.2f})")
        print(f"    SL Hit: {row['exit_date']} at {row['exit_price']:.2f} | Loss: {row['pnl_percent']:+.2f}%")
        m2w = row.get('max_2w', '-')
        m4w = row.get('max_4w', '-')
        m8w = row.get('max_8w', '-')
        mev = row.get('max_price_ever_after', '-')
        print(f"    Post-SL Max (2W/4W/8W/Ever): {m2w}/{m4w}/{m8w}/{mev}")
        print(f"    Max move from entry: {row.get('max_price_ever_pct_from_entry', 0):+.2f}%")
        went = "YES" if row.get("went_above_entry") else "NO"
        days = int(row["days_to_recover_entry"]) if pd.notna(row.get("days_to_recover_entry")) else "-"
        print(f"    Recovered above entry: {went} | Days to recover: {days}")
        print(f"    Current price: {row.get('current_price', '-')}")
        print()

    sep()
    print("  Analysis complete.")
    sep()


if __name__ == "__main__":
    main()
