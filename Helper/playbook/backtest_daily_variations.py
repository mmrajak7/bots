"""
Backtest: Daily ST Magnet — Variation Grid
==========================================
Tests multiple parameter combinations in one run:
  - DTE: 10, 15, 20
  - Liquidity: Top 50 by volume vs All 208
  - Premium SL: 40%, 50%, 60%, None
  - Mode: Naked vs Spread (debit spread, short near ST)

Generates signals ONCE, then runs each variation as a fast simulation pass.

Usage:
    python backtest_daily_variations.py
    python backtest_daily_variations.py --top 30
"""

import csv
import json
import math
import sys
import argparse
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# =========================================================================
# Configuration
# =========================================================================
SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "backtest_cache"
NIFTY_FILE = CACHE_DIR / "_nifty50_daily.json"
FO_LIST_FILE = CACHE_DIR / "_fo_stocks.json"
LOT_SIZE_CSV = Path(__file__).resolve().parent.parent / "helper" / "nse_stocks_options.csv"

ST_PERIOD = 10
ST_MULTIPLIER = 3
MIN_PRICE = 100

MAX_GAP_PCT = 3.0
ENTRY_GAP_MIN = 0.3
DEDUP_GAP_DAYS = 7
MAX_HOLD_DAYS = 5
TOUCH_PCT = 0.5
REVERSE_PCT = 5.0
MAX_POSITIONS = 7
SLIPPAGE_PCT = 0.02
LOTS_PER_TRADE = 1
RISK_FREE_RATE = 0.07
MIN_IV = 0.20
MAX_IV = 1.00

BACKTEST_START = date(2022, 1, 1)
BACKTEST_END = date(2026, 3, 18)

SKIP_SUFFIXES = ("BEES", "ETF", "-RR", "-RE")
SKIP_EXACT = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "MIDCPNIFTY",
              "CNXIT", "IT", "TECH", "NIFTY50"}


# =========================================================================
# Black-Scholes
# =========================================================================
def norm_cdf(x):
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.3275911
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x / 2)
    return 0.5 * (1.0 + sign * y)


def bs_call(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return max(S - K, 0)
    d1 = (math.log(S / K) + (r + sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)


def bs_put(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return max(K - S, 0)
    d1 = (math.log(S / K) + (r + sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


# =========================================================================
# Data Loading
# =========================================================================
_daily_cache = {}


def load_daily(sym):
    if sym in _daily_cache:
        return _daily_cache[sym]
    path = CACHE_DIR / f"{sym}.json"
    if not path.exists():
        _daily_cache[sym] = None
        return None
    with open(path) as f:
        data = json.load(f)
    parsed = []
    for c in data:
        d = c["date"]
        if isinstance(d, str):
            d = datetime.fromisoformat(d.replace("+05:30", "").replace("T00:00:00", "")).date()
        elif isinstance(d, datetime):
            d = d.date()
        parsed.append({
            "date": d, "open": float(c["open"]), "high": float(c["high"]),
            "low": float(c["low"]), "close": float(c["close"]),
            "volume": int(c.get("volume", 0)),
        })
    parsed.sort(key=lambda x: x["date"])
    _daily_cache[sym] = parsed
    return parsed


def load_fo_stocks():
    if FO_LIST_FILE.exists():
        with open(FO_LIST_FILE) as f:
            return set(json.load(f))
    return None


def load_lot_sizes():
    lot_sizes = {}
    try:
        with open(LOT_SIZE_CSV, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                sym = row.get("stock_symbol", "")
                ls = row.get("option_lot_size", "")
                if sym and ls and ls.isdigit() and sym not in lot_sizes:
                    lot_sizes[sym] = int(ls)
    except FileNotFoundError:
        pass
    return lot_sizes


def should_skip(sym):
    upper = sym.upper()
    if upper in SKIP_EXACT:
        return True
    for s in SKIP_SUFFIXES:
        if upper.endswith(s):
            return True
    return False


def compute_supertrend(candles, period=10, multiplier=3):
    n = len(candles)
    if n < period + 1:
        return []
    tr = []
    for i in range(n):
        h, l = candles[i]["high"], candles[i]["low"]
        if i == 0:
            tr.append(h - l)
        else:
            pc = candles[i - 1]["close"]
            tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = [0.0] * n
    atr[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    ub = [0.0] * n
    lb = [0.0] * n
    st = [0.0] * n
    d = [1] * n
    for i in range(period - 1, n):
        hl2 = (candles[i]["high"] + candles[i]["low"]) / 2
        ub[i] = hl2 + multiplier * atr[i]
        lb[i] = hl2 - multiplier * atr[i]
        if i == period - 1:
            st[i] = lb[i]; d[i] = 1; continue
        if not (lb[i] > lb[i - 1] or candles[i - 1]["close"] < lb[i - 1]):
            lb[i] = lb[i - 1]
        if not (ub[i] < ub[i - 1] or candles[i - 1]["close"] > ub[i - 1]):
            ub[i] = ub[i - 1]
        if st[i - 1] == lb[i - 1]:
            if candles[i]["close"] < lb[i]:
                d[i] = -1; st[i] = ub[i]
            else:
                d[i] = 1; st[i] = lb[i]
        else:
            if candles[i]["close"] > ub[i]:
                d[i] = 1; st[i] = lb[i]
            else:
                d[i] = -1; st[i] = ub[i]
    return [{"date": candles[i]["date"], "supertrend": round(st[i], 4),
             "direction": "UP" if d[i] == 1 else "DOWN", "atr": round(atr[i], 4)}
            for i in range(period - 1, n)]


def compute_hist_vol(daily, as_of_idx, lookback=20):
    if as_of_idx < lookback:
        return 0.35
    closes = [daily[i]["close"] for i in range(as_of_idx - lookback, as_of_idx + 1)]
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    if not returns:
        return 0.35
    mean_r = sum(returns) / len(returns)
    var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1) if len(returns) > 1 else 0
    daily_vol = math.sqrt(var)
    annual_vol = daily_vol * math.sqrt(252)
    return max(MIN_IV, min(MAX_IV, annual_vol))


def get_strike_interval(price):
    if price < 100: return 2.5
    elif price < 250: return 5
    elif price < 500: return 10
    elif price < 1000: return 20
    elif price < 2500: return 50
    elif price < 5000: return 100
    else: return 200


def round_to_strike(price, interval):
    return round(price / interval) * interval


# =========================================================================
# Liquidity Ranking
# =========================================================================
def rank_stocks_by_liquidity(fo_stocks, top_n=50):
    """Rank F&O stocks by average daily turnover (price × volume) in backtest period."""
    print(f"  Ranking {len(fo_stocks)} stocks by liquidity...")
    turnover = {}
    for sym in fo_stocks:
        if should_skip(sym):
            continue
        daily = load_daily(sym)
        if daily is None:
            continue
        # Compute avg daily turnover in backtest period
        period_data = [c for c in daily if BACKTEST_START <= c["date"] <= BACKTEST_END]
        if len(period_data) < 100:
            continue
        avg_to = sum(c["close"] * c["volume"] for c in period_data) / len(period_data)
        turnover[sym] = avg_to

    ranked = sorted(turnover.keys(), key=lambda s: turnover[s], reverse=True)
    top = set(ranked[:top_n])
    print(f"  Top {top_n} by turnover: {', '.join(ranked[:10])}...")
    print(f"  Turnover range: Rs {turnover[ranked[0]]/1e7:.0f}Cr - Rs {turnover[ranked[top_n-1]]/1e7:.0f}Cr (avg daily)")
    return top, turnover


# =========================================================================
# Signal Generation (one-time, all stocks)
# =========================================================================
def generate_all_signals():
    """Generate signals for ALL F&O stocks. Filter by liquidity later."""
    fo_stocks = load_fo_stocks()
    if not fo_stocks:
        return [], {}

    print(f"  Scanning {len(fo_stocks)} F&O stocks...")
    all_signals = []
    stock_st_maps = {}  # sym -> {date: st_info}

    scanned = 0
    for sym in sorted(fo_stocks):
        if should_skip(sym):
            continue
        daily = load_daily(sym)
        if daily is None or len(daily) < 200:
            continue
        scanned += 1
        if scanned % 50 == 0:
            print(f"  ... {scanned} stocks, {len(all_signals)} signals")

        st_data = compute_supertrend(daily, ST_PERIOD, ST_MULTIPLIER)
        if not st_data:
            continue

        st_map = {s["date"]: s for s in st_data}
        stock_st_maps[sym] = st_map
        idx_map = {c["date"]: i for i, c in enumerate(daily)}

        for c in daily:
            if c["date"] < BACKTEST_START or c["date"] > BACKTEST_END:
                continue
            if c["close"] < MIN_PRICE:
                continue
            st_info = st_map.get(c["date"])
            if st_info is None:
                continue

            st_val = st_info["supertrend"]
            st_dir = st_info["direction"]
            price = c["close"]
            idx = idx_map[c["date"]]

            if st_dir == "UP" and price > st_val:
                gap = (price - st_val) / st_val * 100
                if ENTRY_GAP_MIN < gap <= MAX_GAP_PCT:
                    all_signals.append({
                        "date": c["date"], "symbol": sym, "tf": "D",
                        "side": "BPS", "st_value": round(st_val, 2),
                        "st_dir": st_dir, "gap_pct": round(gap, 2),
                        "price": round(price, 2), "atr": st_info["atr"],
                        "idx": idx,
                    })
            elif st_dir == "DOWN" and price < st_val:
                gap = (st_val - price) / st_val * 100
                if ENTRY_GAP_MIN < gap <= MAX_GAP_PCT:
                    all_signals.append({
                        "date": c["date"], "symbol": sym, "tf": "D",
                        "side": "BCS", "st_value": round(st_val, 2),
                        "st_dir": st_dir, "gap_pct": round(gap, 2),
                        "price": round(price, 2), "atr": st_info["atr"],
                        "idx": idx,
                    })

    print(f"  Scanned: {scanned} stocks, raw signals: {len(all_signals)}")

    # Dedup
    all_signals.sort(key=lambda x: (x["symbol"], x["side"], x["date"]))
    deduped = []
    last_seen = {}
    for sig in all_signals:
        key = (sig["symbol"], sig["side"])
        if key in last_seen:
            if (sig["date"] - last_seen[key]).days < DEDUP_GAP_DAYS:
                continue
        last_seen[key] = sig["date"]
        deduped.append(sig)

    print(f"  After dedup: {len(deduped)}")
    return deduped, stock_st_maps


# =========================================================================
# Trade Simulation — Parameterized
# =========================================================================
def simulate_trade(sym, signal, daily_data, st_map, lot_size, dte, prem_sl_pct, mode):
    """
    mode: "naked" or "spread"
    prem_sl_pct: 0.40, 0.50, 0.60, or None (no premium SL)
    """
    signal_date = signal["date"]
    st_value = signal["st_value"]
    side = signal["side"]
    entry_idx = signal["idx"]
    entry_price = daily_data[entry_idx]["close"]

    if entry_price < MIN_PRICE:
        return None

    if side == "BPS":
        spread_width = entry_price - st_value
    else:
        spread_width = st_value - entry_price
    if spread_width <= 0:
        return None

    # Option pricing at entry
    iv = compute_hist_vol(daily_data, entry_idx)
    interval = get_strike_interval(entry_price)
    atm_strike = round_to_strike(entry_price, interval)
    T_entry = dte / 365.0

    if side == "BPS":
        long_prem = bs_put(entry_price, atm_strike, T_entry, RISK_FREE_RATE, iv)
        if mode == "spread":
            short_strike = round_to_strike(st_value, interval)
            if atm_strike <= short_strike:
                return None
            short_prem = bs_put(entry_price, short_strike, T_entry, RISK_FREE_RATE, iv)
        else:
            short_strike = None
            short_prem = 0
    else:
        long_prem = bs_call(entry_price, atm_strike, T_entry, RISK_FREE_RATE, iv)
        if mode == "spread":
            short_strike = round_to_strike(st_value, interval)
            if short_strike <= atm_strike:
                return None
            short_prem = bs_call(entry_price, short_strike, T_entry, RISK_FREE_RATE, iv)
        else:
            short_strike = None
            short_prem = 0

    if long_prem <= 0.5:
        return None

    # Entry cost
    if mode == "spread":
        entry_debit = (long_prem - short_prem) * (1 + SLIPPAGE_PCT)
        if entry_debit <= 0:
            return None
        actual_spread = abs(atm_strike - short_strike) if short_strike else spread_width
    else:
        entry_debit = long_prem * (1 + SLIPPAGE_PCT)
        actual_spread = spread_width

    entry_st_dir = "UP" if side == "BPS" else "DOWN"

    exit_date = None
    exit_debit = None
    exit_reason = None
    best_value = entry_debit
    trading_days = 0
    cost_sl_active = False

    for i in range(entry_idx + 1, min(entry_idx + MAX_HOLD_DAYS + 5, len(daily_data))):
        candle = daily_data[i]
        trading_days += 1

        cur_st_info = st_map.get(candle["date"])
        cur_st_val = cur_st_info["supertrend"] if cur_st_info else st_value
        cur_st_dir = cur_st_info["direction"] if cur_st_info else entry_st_dir

        T_now = max((dte - trading_days), 0) / 365.0

        # Current option values
        if side == "BPS":
            long_now = bs_put(candle["close"], atm_strike, T_now, RISK_FREE_RATE, iv)
            short_now = bs_put(candle["close"], short_strike, T_now, RISK_FREE_RATE, iv) if mode == "spread" else 0
            # Intraday best (at low for put)
            long_best = bs_put(candle["low"], atm_strike, T_now, RISK_FREE_RATE, iv)
            short_best = bs_put(candle["low"], short_strike, T_now, RISK_FREE_RATE, iv) if mode == "spread" else 0
        else:
            long_now = bs_call(candle["close"], atm_strike, T_now, RISK_FREE_RATE, iv)
            short_now = bs_call(candle["close"], short_strike, T_now, RISK_FREE_RATE, iv) if mode == "spread" else 0
            long_best = bs_call(candle["high"], atm_strike, T_now, RISK_FREE_RATE, iv)
            short_best = bs_call(candle["high"], short_strike, T_now, RISK_FREE_RATE, iv) if mode == "spread" else 0

        current_value = long_now - short_now
        best_intraday = long_best - short_best
        if best_intraday > best_value:
            best_value = best_intraday

        # Gap from ST
        if side == "BPS":
            current_gap = (candle["close"] - cur_st_val) / cur_st_val * 100 if cur_st_val > 0 else 99
        else:
            current_gap = (cur_st_val - candle["close"]) / cur_st_val * 100 if cur_st_val > 0 else 99

        # ── EXIT: Cost SL activation ──
        if current_gap <= 1.0 and not cost_sl_active:
            cost_sl_active = True

        # ── EXIT: ST FLIP ──
        if cur_st_dir != entry_st_dir:
            exit_debit = current_value * (1 - SLIPPAGE_PCT)
            exit_date = candle["date"]
            exit_reason = "ST_FLIP"
            break

        # ── EXIT: TOUCH ──
        if side == "BPS":
            low_gap = (candle["low"] - cur_st_val) / cur_st_val * 100 if cur_st_val > 0 else 99
            touched = low_gap <= TOUCH_PCT
        else:
            high_gap = (cur_st_val - candle["high"]) / cur_st_val * 100 if cur_st_val > 0 else 99
            touched = high_gap <= TOUCH_PCT

        if touched:
            # Use intraday best for touch (would exit AT the touch in live)
            exit_debit = best_intraday * (1 - SLIPPAGE_PCT)
            exit_date = candle["date"]
            exit_reason = "TOUCH"
            break

        # ── EXIT: Cost SL (breakeven floor after activation) ──
        if cost_sl_active and current_value < entry_debit * 0.95:
            exit_debit = current_value * (1 - SLIPPAGE_PCT)
            exit_date = candle["date"]
            exit_reason = "COST_SL"
            break

        # ── EXIT: Trail (50% of peak gain) ──
        peak_gain = best_value - entry_debit
        if peak_gain > entry_debit * 0.20:
            trail_floor = entry_debit + peak_gain * 0.50
            if current_value < trail_floor:
                exit_debit = current_value * (1 - SLIPPAGE_PCT)
                exit_date = candle["date"]
                exit_reason = "TRAIL"
                break

        # ── EXIT: Premium SL ──
        if prem_sl_pct is not None:
            if current_value < entry_debit * (1 - prem_sl_pct):
                exit_debit = current_value * (1 - SLIPPAGE_PCT)
                exit_date = candle["date"]
                exit_reason = "PREM_SL"
                break

        # ── EXIT: REVERSE ──
        if side == "BPS":
            away = (candle["high"] - st_value) / st_value * 100
        else:
            away = (st_value - candle["low"]) / st_value * 100
        if away >= REVERSE_PCT:
            exit_debit = current_value * (1 - SLIPPAGE_PCT)
            exit_date = candle["date"]
            exit_reason = "REVERSE"
            break

        # ── EXIT: TIME ──
        if trading_days >= MAX_HOLD_DAYS:
            exit_debit = current_value * (1 - SLIPPAGE_PCT)
            exit_date = candle["date"]
            exit_reason = "TIME"
            break

    if exit_date is None:
        if entry_idx < len(daily_data) - 1:
            last = daily_data[-1]
            T_end = max(0, (dte - trading_days)) / 365.0
            if side == "BPS":
                lp = bs_put(last["close"], atm_strike, T_end, RISK_FREE_RATE, iv)
                sp = bs_put(last["close"], short_strike, T_end, RISK_FREE_RATE, iv) if mode == "spread" else 0
            else:
                lp = bs_call(last["close"], atm_strike, T_end, RISK_FREE_RATE, iv)
                sp = bs_call(last["close"], short_strike, T_end, RISK_FREE_RATE, iv) if mode == "spread" else 0
            exit_debit = (lp - sp) * (1 - SLIPPAGE_PCT)
            exit_date = last["date"]
            exit_reason = "DATA_END"
        else:
            return None

    qty = lot_size * LOTS_PER_TRADE
    pnl_per_share = exit_debit - entry_debit
    pnl_rs = pnl_per_share * qty
    pnl_pct = (pnl_per_share / entry_debit * 100) if entry_debit > 0 else 0
    investment = entry_debit * qty

    # Cap spread max loss/gain
    if mode == "spread" and actual_spread > 0:
        max_profit = (actual_spread - entry_debit) * qty
        max_loss = -entry_debit * qty
        pnl_rs = max(max_loss, min(max_profit, pnl_rs))

    return {
        "symbol": sym, "side": side,
        "signal_date": signal_date,
        "entry_price": round(entry_price, 2),
        "st_value": round(st_value, 2),
        "gap_pct": round(signal["gap_pct"], 2),
        "iv": round(iv * 100, 1),
        "entry_debit": round(entry_debit, 2),
        "exit_value": round(exit_debit, 2) if exit_debit else 0,
        "pnl_rs": round(pnl_rs, 0),
        "pnl_pct": round(pnl_pct, 1),
        "investment": round(investment, 0),
        "exit_reason": exit_reason,
        "exit_date": exit_date,
        "days_held": (exit_date - signal_date).days,
        "trading_days": trading_days,
    }


# =========================================================================
# Run One Variation
# =========================================================================
def run_variation(signals, stock_st_maps, lot_sizes, stock_filter, dte, prem_sl_pct, mode):
    """Run one parameter combination. Returns (config_label, summary_stats, trades)."""
    # Filter signals to allowed stocks
    filtered = [s for s in signals if s["symbol"] in stock_filter]
    # Sort by date, gap
    filtered.sort(key=lambda x: (x["date"], x["gap_pct"]))

    trades = []
    open_positions = []
    skipped_cap = 0
    default_lot = 500

    for sig in filtered:
        sym = sig["symbol"]
        signal_date = sig["date"]

        # Close expired
        open_positions = [(ed, td) for ed, td in open_positions if ed > signal_date]
        if len(open_positions) >= MAX_POSITIONS:
            skipped_cap += 1
            continue

        lot_size = lot_sizes.get(sym, default_lot)
        daily = load_daily(sym)
        if daily is None:
            continue

        st_map = stock_st_maps.get(sym, {})
        trade = simulate_trade(sym, sig, daily, st_map, lot_size, dte, prem_sl_pct, mode)
        if trade is None:
            continue

        trades.append(trade)
        open_positions.append((trade["exit_date"], trade))

    return trades, skipped_cap


def summarize(trades):
    """Compute summary stats for a set of trades."""
    if not trades:
        return {"n": 0}
    n = len(trades)
    wins = [t for t in trades if t["pnl_rs"] > 0]
    total_pnl = sum(t["pnl_rs"] for t in trades)
    total_inv = sum(t["investment"] for t in trades)

    by_reason = defaultdict(lambda: {"n": 0, "pnl": 0})
    for t in trades:
        by_reason[t["exit_reason"]]["n"] += 1
        by_reason[t["exit_reason"]]["pnl"] += t["pnl_rs"]

    avg_win = sum(t["pnl_rs"] for t in wins) / len(wins) if wins else 0
    losses = [t for t in trades if t["pnl_rs"] <= 0]
    avg_loss = sum(t["pnl_rs"] for t in losses) / len(losses) if losses else 0

    return {
        "n": n,
        "win_pct": len(wins) / n * 100,
        "total_pnl": total_pnl,
        "avg_pnl_rs": total_pnl / n,
        "avg_pnl_pct": sum(t["pnl_pct"] for t in trades) / n,
        "roc": total_pnl / total_inv * 100 if total_inv > 0 else 0,
        "avg_days": sum(t["days_held"] for t in trades) / n,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "wl_ratio": abs(avg_win / avg_loss) if avg_loss != 0 else 0,
        "by_reason": dict(by_reason),
    }


# =========================================================================
# Main
# =========================================================================
def main():
    parser = argparse.ArgumentParser(description="Daily ST variation grid backtest")
    parser.add_argument("--top", type=int, default=50, help="Top N stocks by liquidity")
    args = parser.parse_args()

    print("=" * 80)
    print("  DAILY ST MAGNET — VARIATION GRID BACKTEST")
    print("  Testing: DTE × Liquidity × PremSL × Mode")
    print("=" * 80)

    # Step 1: Load lot sizes
    print(f"\n[1/5] Loading lot sizes...")
    lot_sizes = load_lot_sizes()
    print(f"  {len(lot_sizes)} symbols")

    # Step 2: Generate ALL signals once
    print(f"\n[2/5] Generating signals (one-time scan)...")
    signals, stock_st_maps = generate_all_signals()
    if not signals:
        print("No signals!")
        return

    # Step 3: Rank liquidity
    print(f"\n[3/5] Ranking stocks by liquidity...")
    fo_stocks = load_fo_stocks()
    top_stocks, turnover = rank_stocks_by_liquidity(fo_stocks, args.top)
    all_stocks = set(s["symbol"] for s in signals)
    print(f"  All tradeable: {len(all_stocks)}, Top {args.top}: {len(top_stocks)}")

    # Step 4: Define variation grid
    print(f"\n[4/5] Running variation grid...")

    dte_values = [10, 15, 20]
    liquidity_sets = [
        (f"Top{args.top}", top_stocks),
        ("All", all_stocks),
    ]
    prem_sl_values = [
        ("SL40%", 0.40),
        ("SL50%", 0.50),
        ("SL60%", 0.60),
        ("NoSL", None),
    ]
    modes = ["naked", "spread"]

    results = []
    total_combos = len(dte_values) * len(liquidity_sets) * len(prem_sl_values) * len(modes)
    combo_num = 0

    for dte in dte_values:
        for liq_label, liq_set in liquidity_sets:
            for sl_label, sl_pct in prem_sl_values:
                for mode in modes:
                    combo_num += 1
                    label = f"DTE{dte}|{liq_label}|{sl_label}|{mode.upper()}"
                    trades, skipped = run_variation(
                        signals, stock_st_maps, lot_sizes,
                        liq_set, dte, sl_pct, mode
                    )
                    s = summarize(trades)
                    s["label"] = label
                    s["dte"] = dte
                    s["liquidity"] = liq_label
                    s["prem_sl"] = sl_label
                    s["mode"] = mode
                    s["skipped_cap"] = skipped
                    results.append(s)

                    if combo_num % 8 == 0:
                        print(f"  [{combo_num}/{total_combos}] {label}: "
                              f"{s['n']} trades, Rs {s.get('total_pnl', 0):+,.0f}")

    # Step 5: Report
    print(f"\n[5/5] Results...")
    print(f"\n{'='*120}")
    print(f"  VARIATION GRID RESULTS — {total_combos} combinations tested")
    print(f"{'='*120}")

    # Sort by total P&L descending
    results.sort(key=lambda r: r.get("total_pnl", 0), reverse=True)

    print(f"\n  {'Label':<35} {'#':>5} {'Win%':>6} {'AvgPnL%':>8} {'Total PnL':>12} "
          f"{'ROC':>6} {'W/L':>5} {'AvgDays':>7}")
    print(f"  {'─'*95}")

    for r in results:
        if r["n"] == 0:
            continue
        print(f"  {r['label']:<35} {r['n']:>5} {r['win_pct']:>5.1f}% {r['avg_pnl_pct']:>+7.1f}% "
              f"{r['total_pnl']:>+11,.0f} {r['roc']:>+5.1f}% {r['wl_ratio']:>4.1f}x "
              f"{r['avg_days']:>6.1f}")

    # Best by category
    print(f"\n  {'='*80}")
    print(f"  BEST BY CATEGORY")
    print(f"  {'='*80}")

    # Best overall
    best = results[0]
    print(f"\n  BEST OVERALL: {best['label']}")
    print(f"    {best['n']} trades, Win {best['win_pct']:.1f}%, "
          f"PnL Rs {best['total_pnl']:+,.0f}, ROC {best['roc']:+.1f}%")

    # Best naked vs best spread
    best_naked = max([r for r in results if r["mode"] == "naked" and r["n"] > 0],
                     key=lambda r: r["total_pnl"], default=None)
    best_spread = max([r for r in results if r["mode"] == "spread" and r["n"] > 0],
                      key=lambda r: r["total_pnl"], default=None)
    if best_naked:
        print(f"\n  BEST NAKED: {best_naked['label']}")
        print(f"    {best_naked['n']} trades, PnL Rs {best_naked['total_pnl']:+,.0f}")
    if best_spread:
        print(f"\n  BEST SPREAD: {best_spread['label']}")
        print(f"    {best_spread['n']} trades, PnL Rs {best_spread['total_pnl']:+,.0f}")

    # Detailed breakdown of top 5
    print(f"\n  {'='*80}")
    print(f"  TOP 5 — EXIT REASON BREAKDOWN")
    print(f"  {'='*80}")

    for r in results[:5]:
        if r["n"] == 0:
            continue
        print(f"\n  {r['label']}  ({r['n']} trades, Rs {r['total_pnl']:+,.0f})")
        reasons = r.get("by_reason", {})
        print(f"    {'Reason':<12} {'#':>5} {'%':>6} {'PnL':>12}")
        print(f"    {'─'*38}")
        for reason in ["TOUCH", "ST_FLIP", "TRAIL", "COST_SL", "PREM_SL", "REVERSE", "TIME"]:
            if reason in reasons:
                rd = reasons[reason]
                pct = rd["n"] / r["n"] * 100
                print(f"    {reason:<12} {rd['n']:>5} {pct:>5.1f}% {rd['pnl']:>+11,.0f}")

    # DTE comparison (aggregate)
    print(f"\n  {'='*80}")
    print(f"  DTE COMPARISON (averaged across other params)")
    print(f"  {'='*80}")
    for dte in dte_values:
        dte_results = [r for r in results if r.get("dte") == dte and r["n"] > 0]
        if not dte_results:
            continue
        avg_pnl = sum(r["total_pnl"] for r in dte_results) / len(dte_results)
        avg_wr = sum(r["win_pct"] for r in dte_results) / len(dte_results)
        avg_roc = sum(r["roc"] for r in dte_results) / len(dte_results)
        print(f"  DTE {dte:>2}: avg PnL Rs {avg_pnl:>+11,.0f}, avg Win% {avg_wr:>5.1f}%, avg ROC {avg_roc:>+5.1f}%")

    # Mode comparison
    print(f"\n  {'='*80}")
    print(f"  MODE COMPARISON (averaged)")
    print(f"  {'='*80}")
    for mode in modes:
        mode_results = [r for r in results if r.get("mode") == mode and r["n"] > 0]
        if not mode_results:
            continue
        avg_pnl = sum(r["total_pnl"] for r in mode_results) / len(mode_results)
        avg_wr = sum(r["win_pct"] for r in mode_results) / len(mode_results)
        print(f"  {mode.upper():<8}: avg PnL Rs {avg_pnl:>+11,.0f}, avg Win% {avg_wr:>5.1f}%")

    # Liquidity comparison
    print(f"\n  {'='*80}")
    print(f"  LIQUIDITY COMPARISON (averaged)")
    print(f"  {'='*80}")
    for liq_label, _ in liquidity_sets:
        liq_results = [r for r in results if r.get("liquidity") == liq_label and r["n"] > 0]
        if not liq_results:
            continue
        avg_pnl = sum(r["total_pnl"] for r in liq_results) / len(liq_results)
        avg_wr = sum(r["win_pct"] for r in liq_results) / len(liq_results)
        print(f"  {liq_label:<8}: avg PnL Rs {avg_pnl:>+11,.0f}, avg Win% {avg_wr:>5.1f}%")

    # PremSL comparison
    print(f"\n  {'='*80}")
    print(f"  PREMIUM SL COMPARISON (averaged)")
    print(f"  {'='*80}")
    for sl_label, _ in prem_sl_values:
        sl_results = [r for r in results if r.get("prem_sl") == sl_label and r["n"] > 0]
        if not sl_results:
            continue
        avg_pnl = sum(r["total_pnl"] for r in sl_results) / len(sl_results)
        avg_wr = sum(r["win_pct"] for r in sl_results) / len(sl_results)
        print(f"  {sl_label:<8}: avg PnL Rs {avg_pnl:>+11,.0f}, avg Win% {avg_wr:>5.1f}%")

    # Export summary
    export_path = SCRIPT_DIR / "backtest_daily_variations_results.csv"
    with open(export_path, "w", newline="", encoding="utf-8") as f:
        fields = ["label", "dte", "liquidity", "prem_sl", "mode", "n", "win_pct",
                  "avg_pnl_pct", "total_pnl", "roc", "wl_ratio", "avg_days",
                  "avg_win", "avg_loss", "skipped_cap"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in results:
            row = {k: round(v, 2) if isinstance(v, float) else v
                   for k, v in r.items() if k in fields}
            writer.writerow(row)
    print(f"\n  Exported {len(results)} variations to {export_path}")


if __name__ == "__main__":
    main()
