"""
Monte Carlo Simulation: Active Equity Basket (v2)
Determines optimal number of holdings and per-stock allocation cap.

Strategy modeled:
- Entry: Monthly Supertrend touch (price at ST support in uptrend)
- SL: Intraday low of entry month (applied from Month 2)
- Trail: Each month's intraday low replaces SL if higher
- Exit: When price breaks below trailing SL

R:R Framework (from Indian large-cap F&O stocks bouncing off Monthly ST):
- MAX LOSS: 8-12% (entry at ST line, SL = intraday low of that month)
- WINNERS: Right-skewed distribution:
  - Small win (30%):  +10% to +25%  (stock bounces, trails out in 2-4 months)
  - Medium win (40%): +25% to +50%  (stock trends for 4-8 months)
  - Big win (20%):    +50% to +100% (strong trend, held 6-12 months)
  - Monster win (10%):+100% to +200% (rare, multi-month breakout runs)
- Typical R:R range: 1:2 to 1:10+
"""

import random
import statistics
import sys
from dataclasses import dataclass


def flush_print(*args, **kwargs):
    """Print with immediate flush."""
    print(*args, **kwargs)
    sys.stdout.flush()


@dataclass
class SimConfig:
    total_capital: float          # Total equity basket capital (Rs)
    max_holdings: int             # Max simultaneous positions
    per_stock_cap_pct: float      # Max % of capital per stock
    signals_per_year: int = 18    # Avg Monthly ST touch signals/year
    sim_years: int = 10           # Simulation horizon
    win_rate: float = 0.42        # % of trades that become winners

    # Loss parameters (CAPPED by SL design)
    sl_distance_min: float = 0.08   # Min SL distance (8%)
    sl_distance_max: float = 0.12   # Max SL distance (12%)
    loser_hold_months: int = 2      # Losers exit in month 2


def simulate_trade(config: SimConfig, rng: random.Random) -> tuple:
    """
    Simulate a single trade with realistic R:R.
    Returns (return_pct, holding_months).

    Loss: Capped at -8% to -12% (SL hit in month 2)
    Win: Right-skewed distribution modeled from Indian large-cap ST bounces
    """
    is_winner = rng.random() < config.win_rate

    if not is_winner:
        # LOSER: Exit at SL, loss is 8-12%
        loss_pct = rng.uniform(config.sl_distance_min, config.sl_distance_max)
        # Add small slippage (0-1% extra)
        slippage = rng.uniform(0, 0.01)
        return (-(loss_pct + slippage), config.loser_hold_months)

    # WINNER: Right-skewed return distribution
    # Model as: which tier of winner?
    tier_roll = rng.random()

    if tier_roll < 0.30:
        # Small win (30% of winners): +10% to +25%, held 2-4 months
        gain = rng.uniform(0.10, 0.25)
        hold = rng.randint(2, 4)
    elif tier_roll < 0.70:
        # Medium win (40% of winners): +25% to +50%, held 4-8 months
        gain = rng.uniform(0.25, 0.50)
        hold = rng.randint(4, 8)
    elif tier_roll < 0.90:
        # Big win (20% of winners): +50% to +100%, held 6-12 months
        gain = rng.uniform(0.50, 1.00)
        hold = rng.randint(6, 12)
    else:
        # Monster win (10% of winners): +100% to +200%, held 8-14 months
        gain = rng.uniform(1.00, 2.00)
        hold = rng.randint(8, 14)

    # Trailing SL gives back some of the gain (exit isn't at the peak)
    # Typically give back 15-30% of the gain due to trailing SL lag
    giveback = rng.uniform(0.15, 0.30)
    actual_gain = gain * (1 - giveback)

    return (actual_gain, hold)


def run_single_simulation(config: SimConfig, rng: random.Random) -> dict:
    """
    Run one full simulation over sim_years.
    Tracks capital deployment, concurrent positions, and equity curve.
    """
    capital = config.total_capital
    initial_capital = config.total_capital
    peak_capital = capital
    max_drawdown = 0.0
    total_months = config.sim_years * 12

    # Active trades: list of (exit_month, position_size, pnl)
    active_trades = []
    completed_trades = []

    total_signals = 0
    signals_skipped_full = 0
    signals_skipped_capital = 0

    per_stock_max = initial_capital * config.per_stock_cap_pct

    for month in range(total_months):
        # Process exits this month
        still_active = []
        for trade in active_trades:
            if trade['exit_month'] <= month:
                capital += trade['position_size'] + trade['pnl']
                completed_trades.append(trade)
            else:
                still_active.append(trade)
        active_trades = still_active

        # Generate signals this month (0-3 per month, avg ~1.5 = 18/yr)
        signals_this_month = rng.randint(0, 3)
        total_signals += signals_this_month

        for _ in range(signals_this_month):
            open_positions = len(active_trades)

            if open_positions >= config.max_holdings:
                signals_skipped_full += 1
                continue

            # Position size (recalculate based on current total portfolio value)
            # Total value = free capital + deployed capital
            deployed = sum(t['position_size'] for t in active_trades)
            total_value = capital + deployed
            position_size = min(total_value * config.per_stock_cap_pct, capital)

            if position_size < per_stock_max * 0.3:
                signals_skipped_capital += 1
                continue

            # Simulate trade
            return_pct, hold_months = simulate_trade(config, rng)
            pnl = position_size * return_pct

            capital -= position_size

            active_trades.append({
                'entry_month': month,
                'exit_month': month + hold_months,
                'position_size': position_size,
                'return_pct': return_pct,
                'pnl': pnl,
                'hold_months': hold_months,
            })

        # Track equity curve (end of month)
        deployed_value = 0
        for trade in active_trades:
            progress = min(1.0, (month - trade['entry_month']) / max(1, trade['exit_month'] - trade['entry_month']))
            current_val = trade['position_size'] * (1 + trade['return_pct'] * progress)
            deployed_value += current_val

        total_value = capital + deployed_value
        if total_value > peak_capital:
            peak_capital = total_value
        dd = (peak_capital - total_value) / peak_capital if peak_capital > 0 else 0
        if dd > max_drawdown:
            max_drawdown = dd

    # Close remaining trades
    for trade in active_trades:
        capital += trade['position_size'] + trade['pnl']
        completed_trades.append(trade)

    final_capital = capital
    total_return = (final_capital - initial_capital) / initial_capital
    cagr = (final_capital / initial_capital) ** (1 / config.sim_years) - 1 if final_capital > 0 else -1

    wins = [t for t in completed_trades if t['return_pct'] > 0]
    losses = [t for t in completed_trades if t['return_pct'] <= 0]

    avg_win = statistics.mean([t['return_pct'] for t in wins]) if wins else 0
    avg_loss = statistics.mean([abs(t['return_pct']) for t in losses]) if losses else 0
    actual_win_rate = len(wins) / len(completed_trades) if completed_trades else 0

    gross_profit = sum(t['pnl'] for t in wins) if wins else 0
    gross_loss = abs(sum(t['pnl'] for t in losses)) if losses else 1
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    # R:R ratio (avg win / avg loss)
    rr_ratio = avg_win / avg_loss if avg_loss > 0 else float('inf')

    return {
        'final_capital': final_capital,
        'total_return_pct': total_return * 100,
        'cagr_pct': cagr * 100,
        'max_drawdown_pct': max_drawdown * 100,
        'total_trades': len(completed_trades),
        'win_rate': actual_win_rate * 100,
        'avg_win_pct': avg_win * 100,
        'avg_loss_pct': avg_loss * 100,
        'rr_ratio': rr_ratio,
        'profit_factor': profit_factor,
        'signals_total': total_signals,
        'signals_skipped_full': signals_skipped_full,
        'signals_skipped_capital': signals_skipped_capital,
    }


def run_monte_carlo(config: SimConfig, n_sims: int = 2000, seed: int = 42) -> dict:
    """Run n_sims simulations and aggregate results."""
    results = []
    base_rng = random.Random(seed)

    for i in range(n_sims):
        sim_seed = base_rng.randint(0, 10**9)
        rng = random.Random(sim_seed)
        result = run_single_simulation(config, rng)
        results.append(result)

    def percentile(values, pct):
        sorted_v = sorted(values)
        idx = int(len(sorted_v) * pct / 100)
        return sorted_v[min(idx, len(sorted_v) - 1)]

    finals = [r['final_capital'] for r in results]
    cagrs = [r['cagr_pct'] for r in results]
    drawdowns = [r['max_drawdown_pct'] for r in results]
    pfs = [r['profit_factor'] for r in results]
    rrs = [r['rr_ratio'] for r in results]

    return {
        'config': {
            'max_holdings': config.max_holdings,
            'per_stock_cap_pct': config.per_stock_cap_pct * 100,
            'total_capital': config.total_capital,
            'sim_years': config.sim_years,
            'win_rate_input': config.win_rate * 100,
        },
        'n_sims': n_sims,
        'final_capital': {
            'median': statistics.median(finals),
            'mean': statistics.mean(finals),
            'p10': percentile(finals, 10),
            'p25': percentile(finals, 25),
            'p75': percentile(finals, 75),
            'p90': percentile(finals, 90),
        },
        'cagr_pct': {
            'median': statistics.median(cagrs),
            'mean': statistics.mean(cagrs),
            'p10': percentile(cagrs, 10),
            'p90': percentile(cagrs, 90),
        },
        'max_drawdown_pct': {
            'median': statistics.median(drawdowns),
            'mean': statistics.mean(drawdowns),
            'p90': percentile(drawdowns, 90),
            'p99': percentile(drawdowns, 99),
        },
        'profit_factor': {
            'median': statistics.median(pfs),
            'mean': statistics.mean(pfs),
        },
        'rr_ratio': {
            'median': statistics.median(rrs),
            'mean': statistics.mean(rrs),
        },
        'ruin_rate': len([r for r in results if r['final_capital'] < config.total_capital * 0.5]) / n_sims * 100,
    }


def main():
    TOTAL_CAPITAL = 1_20_00_000  # 1.2 Cr (40% of 3 Cr)
    SIM_YEARS = 10
    N_SIMS = 2000

    # Grid search
    holdings_options = [5, 8, 10, 13, 15, 20]
    cap_options = [0.03, 0.05, 0.07, 0.10]

    # Win rates to test
    win_rates = [0.35, 0.40, 0.45]

    all_results = []

    flush_print("=" * 110)
    flush_print(f"  EQUITY BASKET MONTE CARLO v2 -- {N_SIMS} sims x {SIM_YEARS} years")
    flush_print(f"  Capital: Rs {TOTAL_CAPITAL/100000:.0f}L | Signals: ~18/yr")
    flush_print(f"  LOSS: Capped 8-12% (SL = intraday low of entry month)")
    flush_print(f"  WIN distribution: 30% small(10-25%) | 40% med(25-50%) | 20% big(50-100%) | 10% monster(100-200%)")
    flush_print(f"  Trailing SL giveback: 15-30% of peak gain")
    flush_print("=" * 110)

    # First show R:R stats from a sample
    flush_print(f"\n  SAMPLE TRADE STATS (1000 trades at 42% win rate):")
    sample_rng = random.Random(99)
    sample_cfg = SimConfig(total_capital=TOTAL_CAPITAL, max_holdings=10, per_stock_cap_pct=0.05, win_rate=0.42)
    wins_r, losses_r = [], []
    for _ in range(1000):
        ret, hold = simulate_trade(sample_cfg, sample_rng)
        if ret > 0:
            wins_r.append(ret * 100)
        else:
            losses_r.append(abs(ret) * 100)
    flush_print(f"  Avg Loss: -{statistics.mean(losses_r):.1f}% | Avg Win: +{statistics.mean(wins_r):.1f}%")
    flush_print(f"  Median Loss: -{statistics.median(losses_r):.1f}% | Median Win: +{statistics.median(wins_r):.1f}%")
    flush_print(f"  Max Loss: -{max(losses_r):.1f}% | Max Win: +{max(wins_r):.1f}%")
    flush_print(f"  R:R ratio (avg): 1:{statistics.mean(wins_r)/statistics.mean(losses_r):.1f}")
    flush_print(f"  Win count: {len(wins_r)} | Loss count: {len(losses_r)}")

    for win_rate in win_rates:
        flush_print(f"\n{'#' * 110}")
        flush_print(f"  WIN RATE: {win_rate*100:.0f}%")
        flush_print(f"{'#' * 110}")

        flush_print(f"\n  {'Hold':>5} {'Cap%':>5} {'MedCAGR':>8} {'P10':>8} {'P90':>8} "
              f"{'MedDD':>6} {'P90DD':>6} {'Final':>10} {'P10Fin':>10} {'P90Fin':>10} "
              f"{'PF':>5} {'R:R':>5} {'Trades':>6} {'Ruin%':>6}")
        flush_print(f"  {'-' * 105}")

        for n_holdings in holdings_options:
            for cap_pct in cap_options:
                if n_holdings * cap_pct > 1.0:
                    continue
                if n_holdings * cap_pct < 0.15:
                    continue

                config = SimConfig(
                    total_capital=TOTAL_CAPITAL,
                    max_holdings=n_holdings,
                    per_stock_cap_pct=cap_pct,
                    sim_years=SIM_YEARS,
                    win_rate=win_rate,
                )

                result = run_monte_carlo(config, N_SIMS)
                all_results.append(result)

                c = result['config']
                flush_print(f"  {c['max_holdings']:>5} {c['per_stock_cap_pct']:>4.0f}% "
                      f"{result['cagr_pct']['median']:>+7.1f}% "
                      f"{result['cagr_pct']['p10']:>+7.1f}% "
                      f"{result['cagr_pct']['p90']:>+7.1f}% "
                      f"{result['max_drawdown_pct']['median']:>5.1f}% "
                      f"{result['max_drawdown_pct']['p90']:>5.1f}% "
                      f"{result['final_capital']['median']/100000:>9.1f}L "
                      f"{result['final_capital']['p10']/100000:>9.1f}L "
                      f"{result['final_capital']['p90']/100000:>9.1f}L "
                      f"{result['profit_factor']['median']:>5.2f} "
                      f"{result['rr_ratio']['median']:>4.1f} "
                      f"{result['trades_per_sim']:>6.0f} " if 'trades_per_sim' in result else "",
                      end="")
                # Get trade count from a sample run
                sample = run_single_simulation(config, random.Random(42))
                flush_print(f"{sample['total_trades']:>5} "
                      f"{result['ruin_rate']:>5.1f}%")

    # Optimal configs
    flush_print(f"\n\n{'=' * 110}")
    flush_print("  OPTIMAL CONFIGURATIONS (by Calmar ratio = CAGR / MaxDD)")
    flush_print(f"{'=' * 110}")

    scored = []
    for r in all_results:
        if r['max_drawdown_pct']['median'] > 0:
            calmar = r['cagr_pct']['median'] / r['max_drawdown_pct']['median']
        else:
            calmar = 0
        scored.append((calmar, r))

    scored.sort(key=lambda x: x[0], reverse=True)

    flush_print(f"\n  {'#':>3} {'Hold':>5} {'Cap%':>5} {'WR':>4} {'CAGR':>7} {'MaxDD':>6} {'Calmar':>7} {'Final':>10} {'Ruin%':>6}")
    flush_print(f"  {'-' * 60}")

    for i, (calmar, r) in enumerate(scored[:20]):
        c = r['config']
        flush_print(f"  {i+1:>3} {c['max_holdings']:>5} {c['per_stock_cap_pct']:>4.0f}% "
              f"{c['win_rate_input']:>3.0f}% "
              f"{r['cagr_pct']['median']:>+6.1f}% "
              f"{r['max_drawdown_pct']['median']:>5.1f}% "
              f"{calmar:>6.2f} "
              f"{r['final_capital']['median']/100000:>9.1f}L "
              f"{r['ruin_rate']:>5.1f}%")

    # Best by absolute return
    flush_print(f"\n  TOP 5 BY ABSOLUTE RETURN (Median CAGR):")
    by_cagr = sorted(all_results, key=lambda r: r['cagr_pct']['median'], reverse=True)
    for i, r in enumerate(by_cagr[:5]):
        c = r['config']
        flush_print(f"  {i+1}. {c['max_holdings']} holdings x {c['per_stock_cap_pct']:.0f}% cap "
              f"(WR {c['win_rate_input']:.0f}%) -> CAGR {r['cagr_pct']['median']:+.1f}%, "
              f"DD {r['max_drawdown_pct']['median']:.1f}%, "
              f"Final {r['final_capital']['median']/100000:.1f}L, "
              f"P10 {r['final_capital']['p10']/100000:.1f}L")

    # Best risk-adjusted with low ruin
    flush_print(f"\n  TOP 5 SAFEST (lowest drawdown, positive CAGR):")
    positive = [r for r in all_results if r['cagr_pct']['median'] > 0]
    by_dd = sorted(positive, key=lambda r: r['max_drawdown_pct']['median'])
    for i, r in enumerate(by_dd[:5]):
        c = r['config']
        flush_print(f"  {i+1}. {c['max_holdings']} holdings x {c['per_stock_cap_pct']:.0f}% cap "
              f"(WR {c['win_rate_input']:.0f}%) -> DD {r['max_drawdown_pct']['median']:.1f}%, "
              f"CAGR {r['cagr_pct']['median']:+.1f}%, "
              f"Final {r['final_capital']['median']/100000:.1f}L")

    # RECOMMENDATION
    flush_print(f"\n\n{'=' * 110}")
    flush_print("  RECOMMENDATION")
    flush_print(f"{'=' * 110}")
    flush_print(f"  Based on {len(all_results)} configurations tested:")
    flush_print(f"  Look for configs with: Calmar > 0.3, Ruin < 1%, CAGR > 5%")
    flush_print(f"  The sweet spot balances return vs drawdown vs capital utilization")


if __name__ == '__main__':
    main()
