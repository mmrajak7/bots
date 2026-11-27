"""
ST Signals - Trader Analysis
- 10% SL, Multiple Targets (5%, 10%, 15%, 20%, 25%)
- By INDEX, TF, CE/PE
- Weekly breakdown (Dec was range, Jan was bearish fall)
- Think like a trader: where is the edge?
"""

import pandas as pd
import numpy as np
from datetime import datetime

pd.set_option('display.max_columns', None)
pd.set_option('display.width', None)

CSV_PATH = r'C:\Users\mail2\Documents\Python\Analysis\ETF_PI\backtest_st_signals_20251201_20260123.csv'

def get_week_label(date_str):
    """Convert date to week label"""
    dt = pd.to_datetime(date_str)
    if dt.month == 12:
        week_num = (dt.day - 1) // 7 + 1
        return f"Dec-W{week_num}"
    else:  # January
        week_num = (dt.day - 1) // 7 + 1
        return f"Jan-W{week_num}"

def calculate_targets(df):
    """Calculate outcomes for each target level with 10% SL"""
    SL_PCT = 0.10
    TARGETS = [0.05, 0.10, 0.15, 0.20, 0.25]

    results = []
    for idx, row in df.iterrows():
        entry = row['entry']
        high_after = row['high_after']
        low_after = row['low_after']

        sl_price = entry * (1 - SL_PCT)
        sl_hit = low_after <= sl_price

        result = {
            'date': row['date'],
            'week': get_week_label(row['date']),
            'index': row['index'],
            'opt_type': row['opt_type'],
            'tf': row['tf'],
            'regime': row['regime'],
            'entry': entry,
        }

        for target_pct in TARGETS:
            target_price = entry * (1 + target_pct)
            target_key = f't{int(target_pct*100)}'

            target_hit = high_after >= target_price

            if target_hit:
                result[f'{target_key}_out'] = 'WIN'
                result[f'{target_key}_pnl'] = target_pct * 100
            elif sl_hit:
                result[f'{target_key}_out'] = 'LOSS'
                result[f'{target_key}_pnl'] = -SL_PCT * 100
            else:
                result[f'{target_key}_out'] = 'OPEN'
                result[f'{target_key}_pnl'] = 0

        results.append(result)

    return pd.DataFrame(results)

def print_analysis(df_results, group_cols, group_name, filter_condition=None):
    """Print analysis for a group"""
    if filter_condition is not None:
        df = df_results[filter_condition].copy()
    else:
        df = df_results.copy()

    if len(df) == 0:
        return

    TARGETS = [5, 10, 15, 20, 25]

    print(f"\n{'='*100}")
    print(f"{group_name}")
    print(f"{'='*100}")

    # Header
    header = f"{'Group':<25} {'N':>6}"
    for t in TARGETS:
        header += f" | {t}%WR  {t}%P/T"
    print(header)
    print("-" * 100)

    if isinstance(group_cols, list):
        grouped = df.groupby(group_cols)
    else:
        grouped = df.groupby([group_cols])

    for name, group in grouped:
        n = len(group)
        if isinstance(name, tuple):
            label = " + ".join(str(x) for x in name)
        else:
            label = str(name)

        row = f"{label:<25} {n:>6}"

        for t in TARGETS:
            win = (group[f't{t}_out'] == 'WIN').sum()
            win_rate = win / n * 100 if n > 0 else 0
            total_pnl = group[f't{t}_pnl'].sum()
            pnl_per_trade = total_pnl / n if n > 0 else 0
            row += f" | {win_rate:>4.0f}% {pnl_per_trade:>+6.1f}%"

        print(row)

    # Total row
    n = len(df)
    row = f"{'TOTAL':<25} {n:>6}"
    for t in TARGETS:
        win = (df[f't{t}_out'] == 'WIN').sum()
        win_rate = win / n * 100 if n > 0 else 0
        total_pnl = df[f't{t}_pnl'].sum()
        pnl_per_trade = total_pnl / n if n > 0 else 0
        row += f" | {win_rate:>4.0f}% {pnl_per_trade:>+6.1f}%"
    print("-" * 100)
    print(row)

def main():
    print("=" * 100)
    print("ST SIGNALS - TRADER ANALYSIS")
    print("10% SL | Targets: 5%, 10%, 15%, 20%, 25%")
    print("=" * 100)

    # Load and process data
    df = pd.read_csv(CSV_PATH)
    df_results = calculate_targets(df)

    print(f"\nTotal Signals: {len(df_results)}")
    print(f"Date Range: {df_results['date'].min()} to {df_results['date'].max()}")
    print(f"Indexes: {df_results['index'].unique().tolist()}")
    print(f"Timeframes: {df_results['tf'].unique().tolist()}")

    # 1. BY WEEK - Most important for understanding market phases
    print_analysis(df_results, 'week', "BY WEEK (Market Phase)")

    # 2. BY WEEK + CE/PE
    print_analysis(df_results, ['week', 'opt_type'], "BY WEEK + CE/PE")

    # 3. BY INDEX
    print_analysis(df_results, 'index', "BY INDEX")

    # 4. BY INDEX + CE/PE
    print_analysis(df_results, ['index', 'opt_type'], "BY INDEX + CE/PE")

    # 5. BY TIMEFRAME
    print_analysis(df_results, 'tf', "BY TIMEFRAME (ST Signal TF)")

    # 6. BY TF + CE/PE
    print_analysis(df_results, ['tf', 'opt_type'], "BY TIMEFRAME + CE/PE")

    # 7. BY REGIME
    print_analysis(df_results, 'regime', "BY REGIME")

    # 8. BY REGIME + CE/PE - The most important one
    print_analysis(df_results, ['regime', 'opt_type'], "BY REGIME + CE/PE (KEY INSIGHT)")

    # 9. BEST COMBINATIONS
    print("\n" + "=" * 100)
    print("BEST TRADING SETUPS (Regime Aligned)")
    print("=" * 100)

    # BEARISH + PE
    bear_pe = df_results[(df_results['regime'] == 'BEARISH') & (df_results['opt_type'] == 'PE')]
    if len(bear_pe) > 0:
        print(f"\nBEARISH + PE ({len(bear_pe)} signals)")
        print("-" * 60)
        for t in [5, 10, 15, 20, 25]:
            win = (bear_pe[f't{t}_out'] == 'WIN').sum()
            wr = win / len(bear_pe) * 100
            pnl = bear_pe[f't{t}_pnl'].sum()
            ppt = pnl / len(bear_pe)
            print(f"  Target {t:>2}%: Win Rate {wr:>5.1f}% | Total P&L {pnl:>+8.0f}% | P&L/Trade {ppt:>+6.2f}%")

    # BULLISH + CE
    bull_ce = df_results[(df_results['regime'] == 'BULLISH') & (df_results['opt_type'] == 'CE')]
    if len(bull_ce) > 0:
        print(f"\nBULLISH + CE ({len(bull_ce)} signals)")
        print("-" * 60)
        for t in [5, 10, 15, 20, 25]:
            win = (bull_ce[f't{t}_out'] == 'WIN').sum()
            wr = win / len(bull_ce) * 100
            pnl = bull_ce[f't{t}_pnl'].sum()
            ppt = pnl / len(bull_ce)
            print(f"  Target {t:>2}%: Win Rate {wr:>5.1f}% | Total P&L {pnl:>+8.0f}% | P&L/Trade {ppt:>+6.2f}%")

    # 10. WORST COMBINATIONS (What to AVOID)
    print("\n" + "=" * 100)
    print("AVOID THESE SETUPS (Counter-Trend)")
    print("=" * 100)

    # BEARISH + CE - The killer
    bear_ce = df_results[(df_results['regime'] == 'BEARISH') & (df_results['opt_type'] == 'CE')]
    if len(bear_ce) > 0:
        print(f"\nBEARISH + CE ({len(bear_ce)} signals) - AVOID!")
        print("-" * 60)
        for t in [5, 10, 15, 20, 25]:
            win = (bear_ce[f't{t}_out'] == 'WIN').sum()
            wr = win / len(bear_ce) * 100
            pnl = bear_ce[f't{t}_pnl'].sum()
            ppt = pnl / len(bear_ce)
            print(f"  Target {t:>2}%: Win Rate {wr:>5.1f}% | Total P&L {pnl:>+8.0f}% | P&L/Trade {ppt:>+6.2f}%")

    # 11. WEEKLY DEEP DIVE - December vs January
    print("\n" + "=" * 100)
    print("DECEMBER vs JANUARY COMPARISON")
    print("=" * 100)

    dec = df_results[df_results['week'].str.startswith('Dec')]
    jan = df_results[df_results['week'].str.startswith('Jan')]

    print(f"\nDECEMBER (Range-Bound Market) - {len(dec)} signals")
    print("-" * 60)
    for opt in ['CE', 'PE']:
        subset = dec[dec['opt_type'] == opt]
        if len(subset) == 0:
            continue
        print(f"  {opt}:")
        for t in [10, 15, 20, 25]:
            win = (subset[f't{t}_out'] == 'WIN').sum()
            wr = win / len(subset) * 100
            ppt = subset[f't{t}_pnl'].sum() / len(subset)
            print(f"    {t}% Target: WR {wr:>5.1f}%, P&L/Trade {ppt:>+6.2f}%")

    print(f"\nJANUARY (Bearish Fall) - {len(jan)} signals")
    print("-" * 60)
    for opt in ['CE', 'PE']:
        subset = jan[jan['opt_type'] == opt]
        if len(subset) == 0:
            continue
        print(f"  {opt}:")
        for t in [10, 15, 20, 25]:
            win = (subset[f't{t}_out'] == 'WIN').sum()
            wr = win / len(subset) * 100
            ppt = subset[f't{t}_pnl'].sum() / len(subset)
            print(f"    {t}% Target: WR {wr:>5.1f}%, P&L/Trade {ppt:>+6.2f}%")

    # 12. INDEX + TF MATRIX for BEARISH+PE
    print("\n" + "=" * 100)
    print("INDEX x TF MATRIX (BEARISH + PE only) - Best Target: 25%")
    print("=" * 100)

    bear_pe = df_results[(df_results['regime'] == 'BEARISH') & (df_results['opt_type'] == 'PE')]

    print(f"\n{'INDEX':<12}", end="")
    for tf in ['3M', '5M', '10M', '15M']:
        print(f" | {tf:>12}", end="")
    print(" | TOTAL")
    print("-" * 75)

    for idx in ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX']:
        print(f"{idx:<12}", end="")
        for tf in ['3M', '5M', '10M', '15M']:
            subset = bear_pe[(bear_pe['index'] == idx) & (bear_pe['tf'] == tf)]
            if len(subset) > 0:
                wr = (subset['t25_out'] == 'WIN').sum() / len(subset) * 100
                ppt = subset['t25_pnl'].sum() / len(subset)
                print(f" | {len(subset):>3}s {ppt:>+5.1f}%", end="")
            else:
                print(f" | {'-':>12}", end="")
        # Total for index
        idx_total = bear_pe[bear_pe['index'] == idx]
        if len(idx_total) > 0:
            ppt = idx_total['t25_pnl'].sum() / len(idx_total)
            print(f" | {len(idx_total):>3}s {ppt:>+5.1f}%")
        else:
            print(f" | -")

    # 13. TRADING RECOMMENDATIONS
    print("\n" + "=" * 100)
    print("TRADING RECOMMENDATIONS")
    print("=" * 100)

    print("""
    1. REGIME FILTER IS CRITICAL:
       - BEARISH market: Trade PE ONLY (25% target = +9.78%/trade)
       - BULLISH market: Trade CE ONLY
       - RANGE market: Best to skip (lower win rates)

    2. TARGET SELECTION:
       - For BEARISH+PE: 25% target gives best P&L/trade
       - Higher targets = lower win rate but better risk-adjusted returns
       - 10% SL with 25% target = 2.5:1 R:R ratio

    3. INDEX SELECTION (in bearish market with PE):
       - FINNIFTY: Best performer
       - SENSEX: Good performer
       - NIFTY: Moderate
       - BANKNIFTY/MIDCPNIFTY: Avoid if possible

    4. TIMEFRAME:
       - All TFs perform similarly with regime filter
       - 15M has slightly higher win rate but fewer signals

    5. KEY RULE:
       - NEVER trade CE in BEARISH market
       - NEVER trade PE in BULLISH market
    """)

    # Save detailed results
    output_path = r'C:\Users\mail2\Documents\Python\Analysis\ETF_PI\backtest_st_trader_results.csv'
    df_results.to_csv(output_path, index=False)
    print(f"\nDetailed results saved to: {output_path}")

    print("\n" + "=" * 100)
    print("ANALYSIS COMPLETE")
    print("=" * 100)

if __name__ == "__main__":
    main()
