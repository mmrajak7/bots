"""
Backtest: Daily ST Magnet — Velocity Analysis
==============================================
Does the SPEED of approach toward ST predict touch rate and P&L?

Velocity = gap change over last N days before entry signal.
  - FAST approach: gap shrank 2%+ in 3 days (momentum pulling toward ST)
  - SLOW approach: gap shrank <0.5% in 3 days (drifting sideways)
  - ACCELERATING: velocity today > velocity 3 days ago (picking up speed)

Tests velocity over 3-day and 5-day lookbacks.
Uses best config: DTE 10, All stocks, No premium SL, Naked.

Usage:
    python backtest_daily_velocity.py
"""

import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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
RISK_FREE_RATE = 0.07
MIN_IV = 0.20
MAX_IV = 1.00
DTE = 10

BACKTEST_START = date(2022, 1, 1)
BACKTEST_END = date(2026, 3, 18)

SKIP_SUFFIXES = ("BEES", "ETF", "-RR", "-RE")
SKIP_EXACT = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "MIDCPNIFTY",
              "CNXIT", "IT", "TECH", "NIFTY50"}


# =========================================================================
# Black-Scholes + Data Loading (same as variations)
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
    if T <= 0 or sigma <= 0: return max(S - K, 0)
    d1 = (math.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)


def bs_put(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0: return max(K - S, 0)
    d1 = (math.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


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
    if upper in SKIP_EXACT: return True
    for s in SKIP_SUFFIXES:
        if upper.endswith(s): return True
    return False


def compute_supertrend(candles, period=10, multiplier=3):
    n = len(candles)
    if n < period + 1: return []
    tr = []
    for i in range(n):
        h, l = candles[i]["high"], candles[i]["low"]
        if i == 0: tr.append(h - l)
        else:
            pc = candles[i-1]["close"]
            tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = [0.0] * n
    atr[period-1] = sum(tr[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i-1] * (period-1) + tr[i]) / period
    ub = [0.0] * n; lb = [0.0] * n; st = [0.0] * n; d = [1] * n
    for i in range(period-1, n):
        hl2 = (candles[i]["high"] + candles[i]["low"]) / 2
        ub[i] = hl2 + multiplier * atr[i]
        lb[i] = hl2 - multiplier * atr[i]
        if i == period-1: st[i] = lb[i]; d[i] = 1; continue
        if not (lb[i] > lb[i-1] or candles[i-1]["close"] < lb[i-1]): lb[i] = lb[i-1]
        if not (ub[i] < ub[i-1] or candles[i-1]["close"] > ub[i-1]): ub[i] = ub[i-1]
        if st[i-1] == lb[i-1]:
            if candles[i]["close"] < lb[i]: d[i] = -1; st[i] = ub[i]
            else: d[i] = 1; st[i] = lb[i]
        else:
            if candles[i]["close"] > ub[i]: d[i] = 1; st[i] = lb[i]
            else: d[i] = -1; st[i] = ub[i]
    return [{"date": candles[i]["date"], "supertrend": round(st[i], 4),
             "direction": "UP" if d[i] == 1 else "DOWN", "atr": round(atr[i], 4)}
            for i in range(period-1, n)]


def compute_hist_vol(daily, as_of_idx, lookback=20):
    if as_of_idx < lookback: return 0.35
    closes = [daily[i]["close"] for i in range(as_of_idx - lookback, as_of_idx + 1)]
    returns = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
    if not returns: return 0.35
    mean_r = sum(returns) / len(returns)
    var = sum((r - mean_r)**2 for r in returns) / (len(returns)-1) if len(returns) > 1 else 0
    return max(MIN_IV, min(MAX_IV, math.sqrt(var) * math.sqrt(252)))


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
# Velocity Computation
# =========================================================================
def compute_gap_series(daily_data, st_map, side):
    """Compute gap% series for every day. Returns {date: gap_pct}."""
    gaps = {}
    for c in daily_data:
        st_info = st_map.get(c["date"])
        if st_info is None:
            continue
        st_val = st_info["supertrend"]
        st_dir = st_info["direction"]
        price = c["close"]

        if side == "BPS" and st_dir == "UP" and price > st_val and st_val > 0:
            gaps[c["date"]] = (price - st_val) / st_val * 100
        elif side == "BCS" and st_dir == "DOWN" and price < st_val and st_val > 0:
            gaps[c["date"]] = (st_val - price) / st_val * 100
        # else: gap is N/A (wrong direction or ST flipped)
    return gaps


def compute_velocity(daily_data, st_map, signal_idx, signal_date, side, lookback=3):
    """
    Compute approach velocity: how fast gap was closing.
    Returns:
      velocity_pct: gap change over lookback days (negative = approaching = good)
      velocity_per_day: velocity / lookback
      acceleration: velocity change (is approach speeding up?)
      gap_N_days_ago: gap N days before entry
      consecutive_closing_days: how many consecutive days gap was shrinking
    """
    gaps = compute_gap_series(daily_data, st_map, side)

    # Get gap values for last N trading days before signal
    dates_before = []
    for i in range(signal_idx, max(signal_idx - lookback * 3, -1), -1):
        if i < 0:
            break
        d = daily_data[i]["date"]
        if d in gaps:
            dates_before.append((d, gaps[d]))
        if len(dates_before) >= lookback + 1:
            break

    if len(dates_before) < 2:
        return None

    # Current gap and gap N days ago
    gap_now = dates_before[0][1]  # signal day
    gap_ago = dates_before[min(lookback, len(dates_before) - 1)][1]

    # Velocity = gap_change over N days (negative = approaching ST)
    velocity = gap_now - gap_ago  # negative means gap shrank (good)
    days_span = max(1, (dates_before[0][0] - dates_before[min(lookback, len(dates_before) - 1)][0]).days)
    velocity_per_day = velocity / days_span

    # Acceleration: compare recent velocity vs older velocity
    if len(dates_before) >= lookback + 1:
        mid = lookback // 2
        recent_vel = dates_before[0][1] - dates_before[min(mid, len(dates_before) - 1)][1]
        older_vel = dates_before[min(mid, len(dates_before) - 1)][1] - dates_before[min(lookback, len(dates_before) - 1)][1]
        acceleration = recent_vel - older_vel  # negative = accelerating toward ST
    else:
        acceleration = 0

    # Consecutive days of gap closing
    consec = 0
    for i in range(len(dates_before) - 1):
        if dates_before[i][1] < dates_before[i + 1][1]:
            consec += 1
        else:
            break

    # Price momentum (RSI-like: % of recent days that were closing the gap)
    closing_days = 0
    total_days = min(5, len(dates_before) - 1)
    for i in range(total_days):
        if dates_before[i][1] < dates_before[i + 1][1]:
            closing_days += 1
    momentum_pct = closing_days / total_days * 100 if total_days > 0 else 50

    return {
        "velocity_3d": round(velocity, 3),
        "velocity_per_day": round(velocity_per_day, 3),
        "acceleration": round(acceleration, 3),
        "gap_ago": round(gap_ago, 2),
        "gap_now": round(gap_now, 2),
        "gap_change": round(gap_now - gap_ago, 2),
        "consecutive_closing": consec,
        "momentum_pct": round(momentum_pct, 1),
    }


# =========================================================================
# Signal Generation + Velocity Enrichment
# =========================================================================
def generate_signals_with_velocity():
    fo_stocks = load_fo_stocks()
    if not fo_stocks:
        return []

    print(f"  Scanning {len(fo_stocks)} stocks with velocity computation...")
    all_signals = []
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

            side = None
            gap = None
            if st_dir == "UP" and price > st_val:
                gap = (price - st_val) / st_val * 100
                side = "BPS"
            elif st_dir == "DOWN" and price < st_val:
                gap = (st_val - price) / st_val * 100
                side = "BCS"

            if side is None or gap is None:
                continue
            if not (ENTRY_GAP_MIN < gap <= MAX_GAP_PCT):
                continue

            # Compute velocity
            vel_3d = compute_velocity(daily, st_map, idx, c["date"], side, lookback=3)
            vel_5d = compute_velocity(daily, st_map, idx, c["date"], side, lookback=5)

            if vel_3d is None:
                continue

            all_signals.append({
                "date": c["date"], "symbol": sym, "side": side,
                "st_value": round(st_val, 2), "st_dir": st_dir,
                "gap_pct": round(gap, 2), "price": round(price, 2),
                "atr": st_info["atr"], "idx": idx,
                # 3-day velocity
                "vel_3d": vel_3d["velocity_3d"],
                "vel_3d_per_day": vel_3d["velocity_per_day"],
                "accel_3d": vel_3d["acceleration"],
                "consec_closing": vel_3d["consecutive_closing"],
                "momentum_pct": vel_3d["momentum_pct"],
                "gap_3d_ago": vel_3d["gap_ago"],
                # 5-day velocity
                "vel_5d": vel_5d["velocity_3d"] if vel_5d else None,
                "vel_5d_per_day": vel_5d["velocity_per_day"] if vel_5d else None,
            })

    print(f"  Scanned: {scanned}, raw: {len(all_signals)}")

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
    return deduped


# =========================================================================
# Trade Simulation (DTE10, No PremSL, Naked — best config)
# =========================================================================
def simulate_trade(sym, signal, daily_data, st_map, lot_size):
    signal_date = signal["date"]
    st_value = signal["st_value"]
    side = signal["side"]
    entry_idx = signal["idx"]
    entry_price = daily_data[entry_idx]["close"]

    if side == "BPS":
        spread_width = entry_price - st_value
    else:
        spread_width = st_value - entry_price
    if spread_width <= 0:
        return None

    iv = compute_hist_vol(daily_data, entry_idx)
    interval = get_strike_interval(entry_price)
    atm_strike = round_to_strike(entry_price, interval)
    T_entry = DTE / 365.0

    if side == "BPS":
        long_prem = bs_put(entry_price, atm_strike, T_entry, RISK_FREE_RATE, iv)
    else:
        long_prem = bs_call(entry_price, atm_strike, T_entry, RISK_FREE_RATE, iv)

    if long_prem <= 0.5:
        return None

    entry_debit = long_prem * (1 + SLIPPAGE_PCT)
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
        T_now = max((DTE - trading_days), 0) / 365.0

        if side == "BPS":
            long_now = bs_put(candle["close"], atm_strike, T_now, RISK_FREE_RATE, iv)
            long_best = bs_put(candle["low"], atm_strike, T_now, RISK_FREE_RATE, iv)
            current_gap = (candle["close"] - cur_st_val) / cur_st_val * 100 if cur_st_val > 0 else 99
        else:
            long_now = bs_call(candle["close"], atm_strike, T_now, RISK_FREE_RATE, iv)
            long_best = bs_call(candle["high"], atm_strike, T_now, RISK_FREE_RATE, iv)
            current_gap = (cur_st_val - candle["close"]) / cur_st_val * 100 if cur_st_val > 0 else 99

        if long_best > best_value:
            best_value = long_best

        if current_gap <= 1.0 and not cost_sl_active:
            cost_sl_active = True

        # ST FLIP
        if cur_st_dir != entry_st_dir:
            exit_debit = long_now * (1 - SLIPPAGE_PCT)
            exit_date = candle["date"]
            exit_reason = "ST_FLIP"
            break

        # TOUCH
        if side == "BPS":
            low_gap = (candle["low"] - cur_st_val) / cur_st_val * 100 if cur_st_val > 0 else 99
            touched = low_gap <= TOUCH_PCT
        else:
            high_gap = (cur_st_val - candle["high"]) / cur_st_val * 100 if cur_st_val > 0 else 99
            touched = high_gap <= TOUCH_PCT

        if touched:
            exit_debit = long_best * (1 - SLIPPAGE_PCT)
            exit_date = candle["date"]
            exit_reason = "TOUCH"
            break

        # COST SL
        if cost_sl_active and long_now < entry_debit * 0.95:
            exit_debit = long_now * (1 - SLIPPAGE_PCT)
            exit_date = candle["date"]
            exit_reason = "COST_SL"
            break

        # TRAIL
        peak_gain = best_value - entry_debit
        if peak_gain > entry_debit * 0.20:
            trail_floor = entry_debit + peak_gain * 0.50
            if long_now < trail_floor:
                exit_debit = long_now * (1 - SLIPPAGE_PCT)
                exit_date = candle["date"]
                exit_reason = "TRAIL"
                break

        # REVERSE
        if side == "BPS":
            away = (candle["high"] - st_value) / st_value * 100
        else:
            away = (st_value - candle["low"]) / st_value * 100
        if away >= REVERSE_PCT:
            exit_debit = long_now * (1 - SLIPPAGE_PCT)
            exit_date = candle["date"]
            exit_reason = "REVERSE"
            break

        # TIME
        if trading_days >= MAX_HOLD_DAYS:
            exit_debit = long_now * (1 - SLIPPAGE_PCT)
            exit_date = candle["date"]
            exit_reason = "TIME"
            break

    if exit_date is None:
        if entry_idx < len(daily_data) - 1:
            last = daily_data[-1]
            T_end = max(0, (DTE - trading_days)) / 365.0
            if side == "BPS":
                lp = bs_put(last["close"], atm_strike, T_end, RISK_FREE_RATE, iv)
            else:
                lp = bs_call(last["close"], atm_strike, T_end, RISK_FREE_RATE, iv)
            exit_debit = lp * (1 - SLIPPAGE_PCT)
            exit_date = last["date"]
            exit_reason = "DATA_END"
        else:
            return None

    qty = lot_size
    pnl_per_share = exit_debit - entry_debit
    pnl_rs = pnl_per_share * qty
    pnl_pct = (pnl_per_share / entry_debit * 100) if entry_debit > 0 else 0

    return {
        "pnl_rs": round(pnl_rs, 0),
        "pnl_pct": round(pnl_pct, 1),
        "exit_reason": exit_reason,
        "days_held": (exit_date - signal_date).days,
        "trading_days": trading_days,
        "touched": exit_reason == "TOUCH",
        "entry_debit": round(entry_debit, 2),
        "investment": round(entry_debit * qty, 0),
    }


# =========================================================================
# Main Analysis
# =========================================================================
def safe_pct(num, den):
    return num / den * 100 if den > 0 else 0


def bucket_stats(trades):
    if not trades:
        return None
    n = len(trades)
    wins = [t for t in trades if t["pnl_rs"] > 0]
    touch = [t for t in trades if t["touched"]]
    flip = [t for t in trades if t["exit_reason"] == "ST_FLIP"]
    total_pnl = sum(t["pnl_rs"] for t in trades)
    return {
        "n": n,
        "touch_pct": safe_pct(len(touch), n),
        "flip_pct": safe_pct(len(flip), n),
        "hit_pct": safe_pct(len(touch) + len(flip), n),  # touch + flip = "magnet worked"
        "win_pct": safe_pct(len(wins), n),
        "avg_pnl_pct": sum(t["pnl_pct"] for t in trades) / n,
        "total_pnl": total_pnl,
        "avg_pnl_rs": total_pnl / n,
    }


def main():
    print("=" * 80)
    print("  DAILY ST MAGNET — VELOCITY ANALYSIS")
    print("  Does approach speed predict touch rate and P&L?")
    print("  Config: DTE 10, All stocks, No PremSL, Naked")
    print("=" * 80)

    lot_sizes = load_lot_sizes()
    default_lot = 500

    print(f"\n[1/3] Generating signals with velocity...")
    signals = generate_signals_with_velocity()
    if not signals:
        print("No signals!")
        return

    print(f"\n[2/3] Simulating trades (max {MAX_POSITIONS} concurrent)...")

    # Process with position cap
    signals.sort(key=lambda x: (x["date"], x["gap_pct"]))
    trades = []
    open_positions = []

    for sig in signals:
        open_positions = [(ed, _) for ed, _ in open_positions if ed > sig["date"]]
        if len(open_positions) >= MAX_POSITIONS:
            continue

        daily = load_daily(sig["symbol"])
        if daily is None:
            continue

        st_data = compute_supertrend(daily, ST_PERIOD, ST_MULTIPLIER)
        st_map = {s["date"]: s for s in st_data}

        lot_size = lot_sizes.get(sig["symbol"], default_lot)
        trade = simulate_trade(sig["symbol"], sig, daily, st_map, lot_size)
        if trade is None:
            continue

        # Merge signal velocity data into trade
        trade.update({
            "symbol": sig["symbol"], "side": sig["side"],
            "signal_date": sig["date"], "gap_pct": sig["gap_pct"],
            "vel_3d": sig["vel_3d"],
            "vel_3d_per_day": sig["vel_3d_per_day"],
            "accel_3d": sig["accel_3d"],
            "consec_closing": sig["consec_closing"],
            "momentum_pct": sig["momentum_pct"],
            "gap_3d_ago": sig["gap_3d_ago"],
            "vel_5d": sig["vel_5d"],
            "vel_5d_per_day": sig["vel_5d_per_day"],
        })
        trades.append(trade)
        exit_date = sig["date"] + timedelta(days=trade["days_held"])
        open_positions.append((exit_date, trade))

    print(f"  Total trades: {len(trades)}")
    total_pnl = sum(t["pnl_rs"] for t in trades)
    print(f"  Total P&L: Rs {total_pnl:+,.0f}")

    # =========================================================================
    # [3/3] Velocity Analysis
    # =========================================================================
    print(f"\n[3/3] Velocity Analysis...")

    # ── 3-Day Velocity Buckets ──
    print(f"\n  {'='*90}")
    print(f"  3-DAY VELOCITY (gap change over 3 days before entry)")
    print(f"  Negative = gap closing = price approaching ST")
    print(f"  {'='*90}")

    vel_buckets_3d = [
        ("FAST CLOSE (<-1.5%/3d)", lambda t: t["vel_3d"] is not None and t["vel_3d"] < -1.5),
        ("MOD CLOSE (-1.5 to -0.5)", lambda t: t["vel_3d"] is not None and -1.5 <= t["vel_3d"] < -0.5),
        ("SLOW CLOSE (-0.5 to 0)", lambda t: t["vel_3d"] is not None and -0.5 <= t["vel_3d"] < 0),
        ("STALLED (0 to +0.5)", lambda t: t["vel_3d"] is not None and 0 <= t["vel_3d"] < 0.5),
        ("WIDENING (>+0.5)", lambda t: t["vel_3d"] is not None and t["vel_3d"] >= 0.5),
    ]

    print(f"\n  {'Bucket':<28} {'#':>5} {'Touch%':>7} {'Flip%':>6} {'Hit%':>6} {'Win%':>6} {'AvgPnL%':>8} {'Total Rs':>12}")
    print(f"  {'─'*85}")
    for label, filter_fn in vel_buckets_3d:
        subset = [t for t in trades if filter_fn(t)]
        s = bucket_stats(subset)
        if s:
            print(f"  {label:<28} {s['n']:>5} {s['touch_pct']:>6.1f}% {s['flip_pct']:>5.1f}% "
                  f"{s['hit_pct']:>5.1f}% {s['win_pct']:>5.1f}% {s['avg_pnl_pct']:>+7.1f}% {s['total_pnl']:>+11,.0f}")

    # ── 5-Day Velocity ──
    print(f"\n  {'='*90}")
    print(f"  5-DAY VELOCITY")
    print(f"  {'='*90}")

    vel_buckets_5d = [
        ("FAST (<-2.0%/5d)", lambda t: t["vel_5d"] is not None and t["vel_5d"] < -2.0),
        ("MOD (-2.0 to -1.0)", lambda t: t["vel_5d"] is not None and -2.0 <= t["vel_5d"] < -1.0),
        ("SLOW (-1.0 to 0)", lambda t: t["vel_5d"] is not None and -1.0 <= t["vel_5d"] < 0),
        ("STALLED (0 to +0.5)", lambda t: t["vel_5d"] is not None and 0 <= t["vel_5d"] < 0.5),
        ("WIDENING (>+0.5)", lambda t: t["vel_5d"] is not None and t["vel_5d"] >= 0.5),
    ]

    print(f"\n  {'Bucket':<28} {'#':>5} {'Touch%':>7} {'Flip%':>6} {'Hit%':>6} {'Win%':>6} {'AvgPnL%':>8} {'Total Rs':>12}")
    print(f"  {'─'*85}")
    for label, filter_fn in vel_buckets_5d:
        subset = [t for t in trades if filter_fn(t)]
        s = bucket_stats(subset)
        if s:
            print(f"  {label:<28} {s['n']:>5} {s['touch_pct']:>6.1f}% {s['flip_pct']:>5.1f}% "
                  f"{s['hit_pct']:>5.1f}% {s['win_pct']:>5.1f}% {s['avg_pnl_pct']:>+7.1f}% {s['total_pnl']:>+11,.0f}")

    # ── Consecutive Closing Days ──
    print(f"\n  {'='*90}")
    print(f"  CONSECUTIVE DAYS OF GAP CLOSING (before entry)")
    print(f"  {'='*90}")
    print(f"\n  {'Days':<28} {'#':>5} {'Touch%':>7} {'Flip%':>6} {'Hit%':>6} {'Win%':>6} {'AvgPnL%':>8} {'Total Rs':>12}")
    print(f"  {'─'*85}")
    for days in range(6):
        label = f"{days} consecutive" if days < 5 else "5+ consecutive"
        if days < 5:
            subset = [t for t in trades if t["consec_closing"] == days]
        else:
            subset = [t for t in trades if t["consec_closing"] >= 5]
        s = bucket_stats(subset)
        if s:
            print(f"  {label:<28} {s['n']:>5} {s['touch_pct']:>6.1f}% {s['flip_pct']:>5.1f}% "
                  f"{s['hit_pct']:>5.1f}% {s['win_pct']:>5.1f}% {s['avg_pnl_pct']:>+7.1f}% {s['total_pnl']:>+11,.0f}")

    # ── Momentum % (% of last 5 days that closed the gap) ──
    print(f"\n  {'='*90}")
    print(f"  MOMENTUM (% of last 5 days gap was closing)")
    print(f"  {'='*90}")
    print(f"\n  {'Momentum':<28} {'#':>5} {'Touch%':>7} {'Flip%':>6} {'Hit%':>6} {'Win%':>6} {'AvgPnL%':>8} {'Total Rs':>12}")
    print(f"  {'─'*85}")
    for label, lo, hi in [("0-20% (stalled)", 0, 20.1), ("20-40%", 20.1, 40.1),
                           ("40-60%", 40.1, 60.1), ("60-80%", 60.1, 80.1),
                           ("80-100% (strong)", 80.1, 100.1)]:
        subset = [t for t in trades if lo <= t["momentum_pct"] <= hi]
        s = bucket_stats(subset)
        if s:
            print(f"  {label:<28} {s['n']:>5} {s['touch_pct']:>6.1f}% {s['flip_pct']:>5.1f}% "
                  f"{s['hit_pct']:>5.1f}% {s['win_pct']:>5.1f}% {s['avg_pnl_pct']:>+7.1f}% {s['total_pnl']:>+11,.0f}")

    # ── Acceleration ──
    print(f"\n  {'='*90}")
    print(f"  ACCELERATION (is approach speeding up?)")
    print(f"  Negative = accelerating toward ST")
    print(f"  {'='*90}")
    print(f"\n  {'Acceleration':<28} {'#':>5} {'Touch%':>7} {'Flip%':>6} {'Hit%':>6} {'Win%':>6} {'AvgPnL%':>8} {'Total Rs':>12}")
    print(f"  {'─'*85}")
    accel_buckets = [
        ("STRONG ACCEL (<-0.5)", lambda t: t["accel_3d"] < -0.5),
        ("MILD ACCEL (-0.5 to 0)", lambda t: -0.5 <= t["accel_3d"] < 0),
        ("DECELERATE (0 to +0.5)", lambda t: 0 <= t["accel_3d"] < 0.5),
        ("REVERSING (>+0.5)", lambda t: t["accel_3d"] >= 0.5),
    ]
    for label, filter_fn in accel_buckets:
        subset = [t for t in trades if filter_fn(t)]
        s = bucket_stats(subset)
        if s:
            print(f"  {label:<28} {s['n']:>5} {s['touch_pct']:>6.1f}% {s['flip_pct']:>5.1f}% "
                  f"{s['hit_pct']:>5.1f}% {s['win_pct']:>5.1f}% {s['avg_pnl_pct']:>+7.1f}% {s['total_pnl']:>+11,.0f}")

    # ── Gap 3 days ago (where was price 3 days ago?) ──
    print(f"\n  {'='*90}")
    print(f"  GAP 3 DAYS AGO (how far was price from ST 3 days before entry?)")
    print(f"  {'='*90}")
    print(f"\n  {'Gap 3d ago':<28} {'#':>5} {'Touch%':>7} {'Flip%':>6} {'Hit%':>6} {'Win%':>6} {'AvgPnL%':>8} {'Total Rs':>12}")
    print(f"  {'─'*85}")
    for label, lo, hi in [("Was >5% (big move in)", 5, 100),
                           ("Was 3-5%", 3, 5),
                           ("Was 2-3% (already close)", 2, 3),
                           ("Was 1-2%", 1, 2),
                           ("Was <1% (bounced back)", 0, 1)]:
        subset = [t for t in trades if t["gap_3d_ago"] is not None and lo <= t["gap_3d_ago"] < hi]
        s = bucket_stats(subset)
        if s:
            print(f"  {label:<28} {s['n']:>5} {s['touch_pct']:>6.1f}% {s['flip_pct']:>5.1f}% "
                  f"{s['hit_pct']:>5.1f}% {s['win_pct']:>5.1f}% {s['avg_pnl_pct']:>+7.1f}% {s['total_pnl']:>+11,.0f}")

    # ── COMBO: Best velocity + gap filter ──
    print(f"\n  {'='*90}")
    print(f"  COMBINED FILTERS: VELOCITY + GAP + MOMENTUM")
    print(f"  {'='*90}")
    combos = [
        ("BEST: fast+close gap+high mom",
         lambda t: t["vel_3d"] < -0.5 and t["gap_pct"] < 2.0 and t["momentum_pct"] >= 60),
        ("Fast velocity only (vel<-0.5)",
         lambda t: t["vel_3d"] < -0.5),
        ("Close gap only (gap<1.5%)",
         lambda t: t["gap_pct"] < 1.5),
        ("High momentum only (mom>=60%)",
         lambda t: t["momentum_pct"] >= 60),
        ("Fast + accelerating",
         lambda t: t["vel_3d"] < -0.5 and t["accel_3d"] < 0),
        ("Came from far (gap3d>4%)+close now",
         lambda t: t["gap_3d_ago"] is not None and t["gap_3d_ago"] > 4 and t["gap_pct"] < 2.0),
        ("3+ consec closing + gap<2%",
         lambda t: t["consec_closing"] >= 3 and t["gap_pct"] < 2.0),
        ("ALL (baseline)",
         lambda t: True),
    ]

    print(f"\n  {'Filter':<38} {'#':>5} {'Touch%':>7} {'Flip%':>6} {'Hit%':>6} {'Win%':>6} {'AvgPnL%':>8} {'AvgRs':>8} {'Total Rs':>12}")
    print(f"  {'─'*105}")
    for label, filter_fn in combos:
        subset = [t for t in trades if filter_fn(t)]
        s = bucket_stats(subset)
        if s:
            print(f"  {label:<38} {s['n']:>5} {s['touch_pct']:>6.1f}% {s['flip_pct']:>5.1f}% "
                  f"{s['hit_pct']:>5.1f}% {s['win_pct']:>5.1f}% {s['avg_pnl_pct']:>+7.1f}% "
                  f"{s['avg_pnl_rs']:>+7,.0f} {s['total_pnl']:>+11,.0f}")

    # ── BPS vs BCS velocity ──
    print(f"\n  {'='*90}")
    print(f"  BPS vs BCS — VELOCITY INTERACTION")
    print(f"  {'='*90}")
    for side in ["BPS", "BCS"]:
        print(f"\n  {side}:")
        print(f"  {'Velocity':<28} {'#':>5} {'Touch%':>7} {'Hit%':>6} {'Win%':>6} {'AvgPnL%':>8} {'Total Rs':>12}")
        print(f"  {'─'*80}")
        for label, filter_fn in vel_buckets_3d:
            subset = [t for t in trades if t["side"] == side and filter_fn(t)]
            s = bucket_stats(subset)
            if s:
                print(f"  {label:<28} {s['n']:>5} {s['touch_pct']:>6.1f}% {s['hit_pct']:>5.1f}% "
                      f"{s['win_pct']:>5.1f}% {s['avg_pnl_pct']:>+7.1f}% {s['total_pnl']:>+11,.0f}")

    # Export
    export_path = SCRIPT_DIR / "backtest_daily_velocity_results.csv"
    with open(export_path, "w", newline="", encoding="utf-8") as f:
        fields = [k for k in trades[0].keys()]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for t in trades:
            row = {k: (str(v) if isinstance(v, date) else v) for k, v in t.items()}
            writer.writerow(row)
    print(f"\n  Exported {len(trades)} trades to {export_path}")


if __name__ == "__main__":
    main()
