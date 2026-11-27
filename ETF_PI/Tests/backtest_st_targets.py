"""
ST Signals Backtest - Multiple Profit Targets Analysis
- Uses existing ST signals data (no API needed)
- 10% SL
- Multiple profit targets: 5%, 10%, 15%, 20%, 25%
- Analyze by regime, index, timeframe
"""

import pandas as pd
import numpy as np

pd.set_option('display.max_columns', None)
pd.set_option('display.width', None)

# Load the existing ST signals data
CSV_PATH = r'C:\Users\mail2\Documents\Python\Analysis\ETF_PI\backtest_st_signals_20251201_20260123.csv'

def analyze_st_signals():
    """Analyze ST signals with multiple profit targets"""

    print("=" * 100)
    print("ST SIGNALS BACKTEST - MULTIPLE PROFIT TARGETS")
    print("=" * 100)
    print()

    # Load data
    df = pd.read_csv(CSV_PATH)
    print(f"Total ST signals loaded: {len(df)}")
    print()

    # Define targets and SL
    SL_PCT = 0.10  # 10% SL
    TARGETS = [0.05, 0.10, 0.15, 0.20, 0.25]  # 5%, 10%, 15%, 20%, 25%

    # Calculate outcomes for each target level
    results = []

    for idx, row in df.iterrows():
        entry = row['entry']
        high_after = row['high_after']
        low_after = row['low_after']

        # Calculate SL and target prices
        sl_price = entry * (1 - SL_PCT)

        result = {
            'date': row['date'],
            'index': row['index'],
            'symbol': row['symbol'],
            'opt_type': row['opt_type'],
            'tf': row['tf'],
            'entry': entry,
            'sl_price': sl_price,
            'high_after': high_after,
            'low_after': low_after,
            'regime': row['regime'],
            'underlying_up_count': row['underlying_up_count'],
        }

        # Check if SL was hit first (low went below SL)
        sl_hit = low_after <= sl_price

        # Calculate max profit achieved (before SL)
        if high_after > entry:
            max_profit_pct = (high_after - entry) / entry * 100
        else:
            max_profit_pct = 0

        result['max_profit_pct'] = max_profit_pct
        result['sl_hit'] = sl_hit

        # For each target level, check if it was hit
        for target_pct in TARGETS:
            target_price = entry * (1 + target_pct)
            target_key = f'target_{int(target_pct*100)}'

            # Target hit if high_after >= target_price
            target_hit = high_after >= target_price

            # Outcome: TARGET if target hit, SL if SL hit (and target not), else OPEN
            if target_hit:
                result[target_key] = 'TARGET'
                result[f'{target_key}_pnl'] = target_pct * 100
            elif sl_hit:
                result[target_key] = 'SL'
                result[f'{target_key}_pnl'] = -SL_PCT * 100
            else:
                result[target_key] = 'OPEN'
                result[f'{target_key}_pnl'] = 0  # Still open

        results.append(result)

    df_results = pd.DataFrame(results)

    # Print overall statistics
    print("=" * 100)
    print("OVERALL STATISTICS")
    print("=" * 100)
    print()

    total = len(df_results)

    # Create summary table for each target
    print(f"SL: 10% | Total Signals: {total}")
    print()
    print(f"{'Target':<10} {'Signals':<10} {'Target Hit':<12} {'SL Hit':<10} {'Open':<10} {'Win Rate':<12} {'Total P&L':<12} {'P&L/Trade':<12}")
    print("-" * 90)

    for target_pct in TARGETS:
        target_key = f'target_{int(target_pct*100)}'
        pnl_key = f'{target_key}_pnl'

        target_hit = (df_results[target_key] == 'TARGET').sum()
        sl_hit = (df_results[target_key] == 'SL').sum()
        open_count = (df_results[target_key] == 'OPEN').sum()
        win_rate = target_hit / total * 100 if total > 0 else 0
        total_pnl = df_results[pnl_key].sum()
        pnl_per_trade = total_pnl / total if total > 0 else 0

        print(f"{int(target_pct*100)}%{'':<7} {total:<10} {target_hit:<12} {sl_hit:<10} {open_count:<10} {win_rate:>6.1f}%{'':<5} {total_pnl:>+8.1f}%{'':<3} {pnl_per_trade:>+8.2f}%")

    print()

    # Analysis by REGIME
    print("=" * 100)
    print("BY REGIME")
    print("=" * 100)
    print()

    for regime in ['BEARISH', 'BULLISH', 'RANGE']:
        df_regime = df_results[df_results['regime'] == regime]
        if len(df_regime) == 0:
            continue

        total_r = len(df_regime)
        print(f"\n{regime} ({total_r} signals)")
        print(f"{'Target':<10} {'Signals':<10} {'Target Hit':<12} {'SL Hit':<10} {'Win Rate':<12} {'Total P&L':<12} {'P&L/Trade':<12}")
        print("-" * 80)

        for target_pct in TARGETS:
            target_key = f'target_{int(target_pct*100)}'
            pnl_key = f'{target_key}_pnl'

            target_hit = (df_regime[target_key] == 'TARGET').sum()
            sl_hit = (df_regime[target_key] == 'SL').sum()
            win_rate = target_hit / total_r * 100 if total_r > 0 else 0
            total_pnl = df_regime[pnl_key].sum()
            pnl_per_trade = total_pnl / total_r if total_r > 0 else 0

            print(f"{int(target_pct*100)}%{'':<7} {total_r:<10} {target_hit:<12} {sl_hit:<10} {win_rate:>6.1f}%{'':<5} {total_pnl:>+8.1f}%{'':<3} {pnl_per_trade:>+8.2f}%")

    print()

    # Analysis by REGIME + CE/PE
    print("=" * 100)
    print("BY REGIME + OPTION TYPE")
    print("=" * 100)
    print()

    for regime in ['BEARISH', 'BULLISH', 'RANGE']:
        for opt_type in ['CE', 'PE']:
            df_combo = df_results[(df_results['regime'] == regime) & (df_results['opt_type'] == opt_type)]
            if len(df_combo) == 0:
                continue

            total_c = len(df_combo)
            print(f"\n{regime} + {opt_type} ({total_c} signals)")
            print(f"{'Target':<10} {'Signals':<10} {'Target Hit':<12} {'SL Hit':<10} {'Win Rate':<12} {'Total P&L':<12} {'P&L/Trade':<12}")
            print("-" * 80)

            for target_pct in TARGETS:
                target_key = f'target_{int(target_pct*100)}'
                pnl_key = f'{target_key}_pnl'

                target_hit = (df_combo[target_key] == 'TARGET').sum()
                sl_hit = (df_combo[target_key] == 'SL').sum()
                win_rate = target_hit / total_c * 100 if total_c > 0 else 0
                total_pnl = df_combo[pnl_key].sum()
                pnl_per_trade = total_pnl / total_c if total_c > 0 else 0

                print(f"{int(target_pct*100)}%{'':<7} {total_c:<10} {target_hit:<12} {sl_hit:<10} {win_rate:>6.1f}%{'':<5} {total_pnl:>+8.1f}%{'':<3} {pnl_per_trade:>+8.2f}%")

    print()

    # Analysis by INDEX
    print("=" * 100)
    print("BY INDEX")
    print("=" * 100)
    print()

    for index in df_results['index'].unique():
        df_idx = df_results[df_results['index'] == index]
        total_i = len(df_idx)

        print(f"\n{index} ({total_i} signals)")
        print(f"{'Target':<10} {'Signals':<10} {'Target Hit':<12} {'SL Hit':<10} {'Win Rate':<12} {'Total P&L':<12} {'P&L/Trade':<12}")
        print("-" * 80)

        for target_pct in TARGETS:
            target_key = f'target_{int(target_pct*100)}'
            pnl_key = f'{target_key}_pnl'

            target_hit = (df_idx[target_key] == 'TARGET').sum()
            sl_hit = (df_idx[target_key] == 'SL').sum()
            win_rate = target_hit / total_i * 100 if total_i > 0 else 0
            total_pnl = df_idx[pnl_key].sum()
            pnl_per_trade = total_pnl / total_i if total_i > 0 else 0

            print(f"{int(target_pct*100)}%{'':<7} {total_i:<10} {target_hit:<12} {sl_hit:<10} {win_rate:>6.1f}%{'':<5} {total_pnl:>+8.1f}%{'':<3} {pnl_per_trade:>+8.2f}%")

    print()

    # Analysis by TIMEFRAME
    print("=" * 100)
    print("BY TIMEFRAME")
    print("=" * 100)
    print()

    for tf in ['3M', '5M', '10M', '15M']:
        df_tf = df_results[df_results['tf'] == tf]
        if len(df_tf) == 0:
            continue
        total_t = len(df_tf)

        print(f"\n{tf} ({total_t} signals)")
        print(f"{'Target':<10} {'Signals':<10} {'Target Hit':<12} {'SL Hit':<10} {'Win Rate':<12} {'Total P&L':<12} {'P&L/Trade':<12}")
        print("-" * 80)

        for target_pct in TARGETS:
            target_key = f'target_{int(target_pct*100)}'
            pnl_key = f'{target_key}_pnl'

            target_hit = (df_tf[target_key] == 'TARGET').sum()
            sl_hit = (df_tf[target_key] == 'SL').sum()
            win_rate = target_hit / total_t * 100 if total_t > 0 else 0
            total_pnl = df_tf[pnl_key].sum()
            pnl_per_trade = total_pnl / total_t if total_t > 0 else 0

            print(f"{int(target_pct*100)}%{'':<7} {total_t:<10} {target_hit:<12} {sl_hit:<10} {win_rate:>6.1f}%{'':<5} {total_pnl:>+8.1f}%{'':<3} {pnl_per_trade:>+8.2f}%")

    print()

    # Best filter combinations: BEARISH+PE and BULLISH+CE
    print("=" * 100)
    print("BEST FILTER COMBINATIONS (Based on Regime)")
    print("=" * 100)
    print()

    # BEARISH + PE
    df_bear_pe = df_results[(df_results['regime'] == 'BEARISH') & (df_results['opt_type'] == 'PE')]
    if len(df_bear_pe) > 0:
        total_bp = len(df_bear_pe)
        print(f"\nBEARISH + PE ({total_bp} signals) - RECOMMENDED FOR BEARISH MARKET")
        print(f"{'Target':<10} {'Signals':<10} {'Target Hit':<12} {'SL Hit':<10} {'Win Rate':<12} {'Total P&L':<12} {'P&L/Trade':<12}")
        print("-" * 80)

        for target_pct in TARGETS:
            target_key = f'target_{int(target_pct*100)}'
            pnl_key = f'{target_key}_pnl'

            target_hit = (df_bear_pe[target_key] == 'TARGET').sum()
            sl_hit = (df_bear_pe[target_key] == 'SL').sum()
            win_rate = target_hit / total_bp * 100 if total_bp > 0 else 0
            total_pnl = df_bear_pe[pnl_key].sum()
            pnl_per_trade = total_pnl / total_bp if total_bp > 0 else 0

            print(f"{int(target_pct*100)}%{'':<7} {total_bp:<10} {target_hit:<12} {sl_hit:<10} {win_rate:>6.1f}%{'':<5} {total_pnl:>+8.1f}%{'':<3} {pnl_per_trade:>+8.2f}%")

    # BULLISH + CE
    df_bull_ce = df_results[(df_results['regime'] == 'BULLISH') & (df_results['opt_type'] == 'CE')]
    if len(df_bull_ce) > 0:
        total_bc = len(df_bull_ce)
        print(f"\nBULLISH + CE ({total_bc} signals) - RECOMMENDED FOR BULLISH MARKET")
        print(f"{'Target':<10} {'Signals':<10} {'Target Hit':<12} {'SL Hit':<10} {'Win Rate':<12} {'Total P&L':<12} {'P&L/Trade':<12}")
        print("-" * 80)

        for target_pct in TARGETS:
            target_key = f'target_{int(target_pct*100)}'
            pnl_key = f'{target_key}_pnl'

            target_hit = (df_bull_ce[target_key] == 'TARGET').sum()
            sl_hit = (df_bull_ce[target_key] == 'SL').sum()
            win_rate = target_hit / total_bc * 100 if total_bc > 0 else 0
            total_pnl = df_bull_ce[pnl_key].sum()
            pnl_per_trade = total_pnl / total_bc if total_bc > 0 else 0

            print(f"{int(target_pct*100)}%{'':<7} {total_bc:<10} {target_hit:<12} {sl_hit:<10} {win_rate:>6.1f}%{'':<5} {total_pnl:>+8.1f}%{'':<3} {pnl_per_trade:>+8.2f}%")

    # Combined aligned filter
    df_aligned = pd.concat([df_bear_pe, df_bull_ce])
    if len(df_aligned) > 0:
        total_a = len(df_aligned)
        print(f"\nALIGNED FILTER (BEAR+PE / BULL+CE) ({total_a} signals)")
        print(f"{'Target':<10} {'Signals':<10} {'Target Hit':<12} {'SL Hit':<10} {'Win Rate':<12} {'Total P&L':<12} {'P&L/Trade':<12}")
        print("-" * 80)

        for target_pct in TARGETS:
            target_key = f'target_{int(target_pct*100)}'
            pnl_key = f'{target_key}_pnl'

            target_hit = (df_aligned[target_key] == 'TARGET').sum()
            sl_hit = (df_aligned[target_key] == 'SL').sum()
            win_rate = target_hit / total_a * 100 if total_a > 0 else 0
            total_pnl = df_aligned[pnl_key].sum()
            pnl_per_trade = total_pnl / total_a if total_a > 0 else 0

            print(f"{int(target_pct*100)}%{'':<7} {total_a:<10} {target_hit:<12} {sl_hit:<10} {win_rate:>6.1f}%{'':<5} {total_pnl:>+8.1f}%{'':<3} {pnl_per_trade:>+8.2f}%")

    print()

    # Optimal target recommendation
    print("=" * 100)
    print("OPTIMAL TARGET RECOMMENDATION")
    print("=" * 100)
    print()

    # Find the target with best P&L/trade for aligned filter
    best_target = None
    best_pnl_per_trade = -999

    for target_pct in TARGETS:
        pnl_key = f'target_{int(target_pct*100)}_pnl'
        pnl_per_trade = df_aligned[pnl_key].sum() / len(df_aligned) if len(df_aligned) > 0 else 0
        if pnl_per_trade > best_pnl_per_trade:
            best_pnl_per_trade = pnl_per_trade
            best_target = target_pct

    print(f"For ALIGNED filter (BEAR+PE / BULL+CE):")
    print(f"  Best Target: {int(best_target*100)}% with P&L/Trade of {best_pnl_per_trade:+.2f}%")
    print()

    # Save detailed results
    output_path = r'C:\Users\mail2\Documents\Python\Analysis\ETF_PI\backtest_st_targets_analysis.csv'
    df_results.to_csv(output_path, index=False)
    print(f"Detailed results saved to: {output_path}")

    print()
    print("=" * 100)
    print("BACKTEST COMPLETE")
    print("=" * 100)

    return df_results


if __name__ == "__main__":
    analyze_st_signals()
