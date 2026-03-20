"""
Backtest: Daily ST(10,3) Magnet — Naked Option with Position Cap
================================================================
Buy 1 lot ATM option when price is within 2-3% of Daily ST.
Black-Scholes pricing with historical volatility.
Max 7 concurrent positions.

Entry: Buy ATM option (PUT for BPS, CALL for BCS) at ASK + 2% slippage
Exit:  Sell at BID - 2% slippage on TOUCH / ST_FLIP / REVERSE / TIME

Usage:
    python backtest_daily_naked.py
    python backtest_daily_naked.py --max-gap 2.0 --max-pos 5
    python backtest_daily_naked.py --side BPS --dte 10
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

# Entry filters
MAX_GAP_PCT = 3.0
ENTRY_GAP_MIN = 0.3
DEDUP_GAP_DAYS = 7

# Trade simulation
MAX_HOLD_DAYS = 5       # trading days
TOUCH_PCT = 0.5         # within 0.5% = touched
REVERSE_PCT = 5.0       # 5% away = thesis dead
MAX_POSITIONS = 7       # concurrent position cap

# Options pricing
RISK_FREE_RATE = 0.07
DTE = 7                 # days to expiry for daily signals
MIN_IV = 0.20
MAX_IV = 1.00
SLIPPAGE_PCT = 0.02     # 2% bid-ask slippage each way
LOTS_PER_TRADE = 1      # 1 lot per trade

# Backtest date range
BACKTEST_START = date(2022, 1, 1)
BACKTEST_END = date(2026, 3, 18)

# Skip filters
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
    d1 = (math.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)


def bs_put(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return max(K - S, 0)
    d1 = (math.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * math.sqrt(T))
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
    """Compute annualized historical volatility at a given index."""
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
# Regime Timeline
# =========================================================================
def compute_rsi(closes, period=14):
    rsi = [None] * len(closes)
    if len(closes) < period + 1:
        return rsi
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi[period] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi[i + 1] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return rsi


def compute_breadth_series(target_dates):
    stock_files = sorted(CACHE_DIR.glob("*.json"))
    stock_files = [f for f in stock_files if not f.name.startswith("_")]
    all_stocks = {}
    for sf in stock_files[:500]:
        sym = sf.stem
        if should_skip(sym):
            continue
        daily = load_daily(sym)
        if daily is None or len(daily) < 100:
            continue
        closes_list = [(c["date"], c["close"]) for c in daily]
        all_stocks[sym] = closes_list
    target_set = set(target_dates)
    breadth = {}
    for sym, closes in all_stocks.items():
        for i in range(49, len(closes)):
            dt = closes[i][0]
            if dt not in target_set:
                continue
            dma = sum(c[1] for c in closes[i - 49:i + 1]) / 50
            if dt not in breadth:
                breadth[dt] = {"above": 0, "total": 0}
            breadth[dt]["total"] += 1
            if closes[i][1] > dma:
                breadth[dt]["above"] += 1
    return {dt: c["above"] / c["total"] * 100 for dt, c in breadth.items() if c["total"] > 0}


def build_regime_timeline():
    if not NIFTY_FILE.exists():
        print("WARNING: NIFTY data not found")
        return {}
    with open(NIFTY_FILE) as f:
        nifty_raw = json.load(f)
    nifty = []
    for c in nifty_raw:
        d = c["date"]
        if isinstance(d, str):
            d = datetime.fromisoformat(d.replace("+05:30", "")).date()
        nifty.append({"date": d, "close": float(c["close"]),
                       "high": float(c["high"]), "low": float(c["low"])})
    nifty.sort(key=lambda x: x["date"])
    dates = [c["date"] for c in nifty]
    closes = [c["close"] for c in nifty]
    rsi_series = compute_rsi(closes, 14)
    dma50 = [None] * len(closes)
    for i in range(49, len(closes)):
        dma50[i] = sum(closes[i - 49:i + 1]) / 50
    breadth = compute_breadth_series(dates)
    regime = {}
    consecutive_on = 0
    is_unpaused = True
    for i in range(50, len(dates)):
        dt = dates[i]
        r = rsi_series[i]
        d50 = dma50[i]
        b = breadth.get(dt)
        if r is None or d50 is None or b is None:
            regime[dt] = {"status": "UNKNOWN", "signals_on": 0}
            continue
        sig_on = 0
        if r > 50: sig_on += 1
        if b > 40: sig_on += 1
        if closes[i] > d50: sig_on += 1
        if sig_on == 0:
            consecutive_on = 0
            is_unpaused = False
        elif sig_on >= 2:
            consecutive_on += 1
            if consecutive_on >= 7:
                is_unpaused = True
        else:
            consecutive_on = 0
        regime[dt] = {"status": "UNPAUSED" if is_unpaused else "PAUSED", "signals_on": sig_on}
    return regime


# =========================================================================
# Signal Generation — Daily ST
# =========================================================================
def generate_daily_signals(max_gap, min_gap):
    fo_stocks = load_fo_stocks()
    if not fo_stocks:
        print("  ERROR: No F&O stock list")
        return []

    print(f"  Scanning {len(fo_stocks)} F&O stocks for daily ST signals...")

    all_signals = []
    no_data = 0
    scanned = 0

    for sym in sorted(fo_stocks):
        if should_skip(sym):
            continue
        daily = load_daily(sym)
        if daily is None or len(daily) < 200:
            no_data += 1
            continue

        scanned += 1
        if scanned % 50 == 0:
            print(f"  ... scanned {scanned} stocks, {len(all_signals)} signals so far")

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

            if st_dir == "UP" and price > st_val:
                gap = (price - st_val) / st_val * 100
                if min_gap < gap <= max_gap:
                    all_signals.append({
                        "date": c["date"], "symbol": sym, "tf": "D",
                        "side": "BPS", "st_value": round(st_val, 2),
                        "st_dir": st_dir, "gap_pct": round(gap, 2),
                        "price": round(price, 2), "atr": st_info["atr"],
                        "idx": idx,
                    })
            elif st_dir == "DOWN" and price < st_val:
                gap = (st_val - price) / st_val * 100
                if min_gap < gap <= max_gap:
                    all_signals.append({
                        "date": c["date"], "symbol": sym, "tf": "D",
                        "side": "BCS", "st_value": round(st_val, 2),
                        "st_dir": st_dir, "gap_pct": round(gap, 2),
                        "price": round(price, 2), "atr": st_info["atr"],
                        "idx": idx,
                    })

    print(f"  Scanned: {scanned} stocks (no data: {no_data})")
    print(f"  Raw signals: {len(all_signals)}")

    # Dedup: keep first signal per (symbol, side) within DEDUP_GAP_DAYS
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

    print(f"  After dedup ({DEDUP_GAP_DAYS}d): {len(deduped)}")
    return deduped


# =========================================================================
# Naked Option Trade Simulation
# =========================================================================
def simulate_naked_trade(sym, signal, daily_data, lot_size, max_hold, dte):
    """
    Simulate naked ATM option trade with BS pricing.
    Returns trade dict or None.
    """
    signal_date = signal["date"]
    st_value = signal["st_value"]
    side = signal["side"]
    entry_idx = signal["idx"]
    entry_price = daily_data[entry_idx]["close"]

    if entry_price < MIN_PRICE:
        return None

    # Spread width (for context)
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
        option_fair = bs_put(entry_price, atm_strike, T_entry, RISK_FREE_RATE, iv)
    else:
        option_fair = bs_call(entry_price, atm_strike, T_entry, RISK_FREE_RATE, iv)

    if option_fair <= 0.5:
        return None

    # Entry at ASK + slippage
    entry_premium = option_fair * (1 + SLIPPAGE_PCT)

    # Compute ST for all forward days
    st_data = compute_supertrend(daily_data, ST_PERIOD, ST_MULTIPLIER)
    st_map = {s["date"]: s for s in st_data}
    entry_st_dir = "UP" if side == "BPS" else "DOWN"

    exit_date = None
    exit_price = None
    exit_reason = None
    exit_premium = None
    best_premium = entry_premium
    peak_spot_move = 0
    trading_days = 0
    cost_sl_active = False
    cost_sl_price = entry_premium * 1.0  # breakeven

    for i in range(entry_idx + 1, min(entry_idx + max_hold + 5, len(daily_data))):
        candle = daily_data[i]
        trading_days += 1

        cur_st = st_map.get(candle["date"])
        cur_st_val = cur_st["supertrend"] if cur_st else st_value
        cur_st_dir = cur_st["direction"] if cur_st else entry_st_dir

        T_now = max((dte - trading_days), 0) / 365.0

        # Price option at current candle close
        if side == "BPS":
            current_premium = bs_put(candle["close"], atm_strike, T_now, RISK_FREE_RATE, iv)
            # Also check intraday: premium at low (best for put)
            intraday_best = bs_put(candle["low"], atm_strike, T_now, RISK_FREE_RATE, iv)
            spot_move = entry_price - candle["low"]
        else:
            current_premium = bs_call(candle["close"], atm_strike, T_now, RISK_FREE_RATE, iv)
            intraday_best = bs_call(candle["high"], atm_strike, T_now, RISK_FREE_RATE, iv)
            spot_move = candle["high"] - entry_price

        if spot_move > peak_spot_move:
            peak_spot_move = spot_move

        if intraday_best > best_premium:
            best_premium = intraday_best

        # ── EXIT 1: Cost SL (gap ≤ 1%) ──
        # When price gets within 1% of ST, lock in breakeven
        if side == "BPS":
            current_gap = (candle["close"] - cur_st_val) / cur_st_val * 100 if cur_st_val > 0 else 99
        else:
            current_gap = (cur_st_val - candle["close"]) / cur_st_val * 100 if cur_st_val > 0 else 99

        if current_gap <= 1.0 and not cost_sl_active:
            cost_sl_active = True
            cost_sl_price = entry_premium + 0.10  # breakeven + buffer

        # ── EXIT 2: ST flipped ──
        if cur_st_dir != entry_st_dir:
            # Price blew through ST — option should be very profitable
            if side == "BPS":
                exit_prem = bs_put(candle["close"], atm_strike, T_now, RISK_FREE_RATE, iv)
            else:
                exit_prem = bs_call(candle["close"], atm_strike, T_now, RISK_FREE_RATE, iv)
            exit_premium = exit_prem * (1 - SLIPPAGE_PCT)  # sell at BID
            exit_date = candle["date"]
            exit_price = candle["close"]
            exit_reason = "ST_FLIP"
            break

        # ── EXIT 3: TOUCH ──
        if side == "BPS":
            low_gap = (candle["low"] - cur_st_val) / cur_st_val * 100 if cur_st_val > 0 else 99
            if low_gap <= TOUCH_PCT:
                # Price touched ST — take profit
                exit_prem = bs_put(candle["close"], atm_strike, T_now, RISK_FREE_RATE, iv)
                exit_premium = exit_prem * (1 - SLIPPAGE_PCT)
                exit_date = candle["date"]
                exit_price = candle["close"]
                exit_reason = "TOUCH"
                break
        else:
            high_gap = (cur_st_val - candle["high"]) / cur_st_val * 100 if cur_st_val > 0 else 99
            if high_gap <= TOUCH_PCT:
                exit_prem = bs_call(candle["close"], atm_strike, T_now, RISK_FREE_RATE, iv)
                exit_premium = exit_prem * (1 - SLIPPAGE_PCT)
                exit_date = candle["date"]
                exit_price = candle["close"]
                exit_reason = "TOUCH"
                break

        # ── EXIT 4: Cost SL triggered (premium drops below cost after activation) ──
        if cost_sl_active and current_premium < cost_sl_price:
            exit_premium = current_premium * (1 - SLIPPAGE_PCT)
            exit_date = candle["date"]
            exit_price = candle["close"]
            exit_reason = "COST_SL"
            break

        # ── EXIT 5: Trail SL (premium drops below 50% of peak gain) ──
        peak_gain = best_premium - entry_premium
        if peak_gain > entry_premium * 0.20:  # meaningful gain (>20% of entry)
            trail_floor = entry_premium + peak_gain * 0.50
            if current_premium < trail_floor:
                exit_premium = current_premium * (1 - SLIPPAGE_PCT)
                exit_date = candle["date"]
                exit_price = candle["close"]
                exit_reason = "TRAIL"
                break

        # ── EXIT 6: Premium SL (40% loss) ──
        if current_premium < entry_premium * 0.60:
            exit_premium = current_premium * (1 - SLIPPAGE_PCT)
            exit_date = candle["date"]
            exit_price = candle["close"]
            exit_reason = "PREM_SL"
            break

        # ── EXIT 7: REVERSE (spot moves 5% away) ──
        if side == "BPS":
            away_gap = (candle["high"] - st_value) / st_value * 100
        else:
            away_gap = (st_value - candle["low"]) / st_value * 100
        if away_gap >= REVERSE_PCT:
            exit_premium = current_premium * (1 - SLIPPAGE_PCT)
            exit_date = candle["date"]
            exit_price = candle["close"]
            exit_reason = "REVERSE"
            break

        # ── EXIT 8: TIME ──
        if trading_days >= max_hold:
            exit_premium = current_premium * (1 - SLIPPAGE_PCT)
            exit_date = candle["date"]
            exit_price = candle["close"]
            exit_reason = "TIME"
            break

    if exit_date is None:
        if entry_idx < len(daily_data) - 1:
            last = daily_data[-1]
            T_end = max((dte - trading_days), 0) / 365.0
            if side == "BPS":
                ep = bs_put(last["close"], atm_strike, T_end, RISK_FREE_RATE, iv)
            else:
                ep = bs_call(last["close"], atm_strike, T_end, RISK_FREE_RATE, iv)
            exit_premium = ep * (1 - SLIPPAGE_PCT)
            exit_date = last["date"]
            exit_price = last["close"]
            exit_reason = "DATA_END"
        else:
            return None

    # P&L
    qty = lot_size * LOTS_PER_TRADE
    pnl_per_share = exit_premium - entry_premium
    pnl_rs = pnl_per_share * qty
    pnl_pct = (pnl_per_share / entry_premium * 100) if entry_premium > 0 else 0
    investment = entry_premium * qty

    days_held = (exit_date - signal_date).days

    return {
        "symbol": sym, "side": side,
        "signal_date": signal_date,
        "entry_price": round(entry_price, 2),
        "exit_spot": round(exit_price, 2),
        "st_value": round(st_value, 2),
        "gap_pct": round(signal["gap_pct"], 2),
        "spread_width": round(spread_width, 2),
        "iv": round(iv * 100, 1),
        "atm_strike": atm_strike,
        "entry_premium": round(entry_premium, 2),
        "exit_premium": round(exit_premium, 2),
        "best_premium": round(best_premium, 2),
        "pnl_per_share": round(pnl_per_share, 2),
        "pnl_rs": round(pnl_rs, 0),
        "pnl_pct": round(pnl_pct, 1),
        "investment": round(investment, 0),
        "lot_size": lot_size,
        "qty": qty,
        "exit_reason": exit_reason,
        "exit_date": exit_date,
        "days_held": days_held,
        "trading_days": trading_days,
    }


# =========================================================================
# Processing with Position Cap
# =========================================================================
def classify_market_phase(dt):
    if date(2022, 1, 1) <= dt <= date(2022, 6, 30): return "BEAR_2022"
    if date(2022, 7, 1) <= dt <= date(2023, 3, 31): return "RECOVERY_2022_23"
    if date(2023, 4, 1) <= dt <= date(2023, 12, 31): return "BULL_2023"
    if date(2024, 1, 1) <= dt <= date(2024, 9, 30): return "BULL_2024"
    if date(2024, 10, 1) <= dt <= date(2025, 3, 31): return "CORRECTION_2024_25"
    if date(2025, 4, 1) <= dt <= date(2025, 6, 30): return "RECOVERY_2025"
    if date(2025, 7, 1) <= dt <= date(2025, 9, 30): return "CHOP_2025"
    if date(2025, 10, 1) <= dt <= date(2026, 1, 31): return "BEAR_2025_26"
    if date(2026, 2, 1) <= dt <= date(2026, 3, 31): return "CRASH_2026"
    return "OTHER"


def process_with_position_cap(signals, regime, lot_sizes, max_gap, max_hold, dte,
                               max_pos, side_filter=None):
    """
    Process signals chronologically, respecting max concurrent position cap.
    Sort signals by date (priority: smaller gap first on same date).
    Track open positions and skip when cap hit.
    """
    # Sort by date, then by gap (prefer closer to ST)
    signals.sort(key=lambda x: (x["date"], x["gap_pct"]))

    trades = []
    open_positions = []  # list of (exit_date, trade_dict)
    skipped = {"gap": 0, "sim_fail": 0, "side_filter": 0, "capacity": 0, "no_lot": 0}

    default_lot = 500  # fallback lot size

    for i, sig in enumerate(signals):
        sym = sig["symbol"]
        signal_date = sig["date"]
        side = sig["side"]

        if side_filter and side != side_filter:
            skipped["side_filter"] += 1
            continue

        if sig["gap_pct"] > max_gap or sig["gap_pct"] <= ENTRY_GAP_MIN:
            skipped["gap"] += 1
            continue

        # Close expired positions
        open_positions = [(ed, td) for ed, td in open_positions if ed > signal_date]

        # Capacity check
        if len(open_positions) >= max_pos:
            skipped["capacity"] += 1
            continue

        lot_size = lot_sizes.get(sym, default_lot)

        daily = load_daily(sym)
        if daily is None:
            skipped["sim_fail"] += 1
            continue

        trade = simulate_naked_trade(sym, sig, daily, lot_size, max_hold, dte)
        if trade is None:
            skipped["sim_fail"] += 1
            continue

        # Add regime context
        r_info = regime.get(signal_date)
        if not r_info:
            for offset in range(-3, 4):
                d = signal_date + timedelta(days=offset)
                if d in regime:
                    r_info = regime[d]
                    break
        trade["regime_status"] = r_info["status"] if r_info else "UNKNOWN"
        trade["market_phase"] = classify_market_phase(signal_date)

        trades.append(trade)
        open_positions.append((trade["exit_date"], trade))

        if (i + 1) % 2000 == 0:
            print(f"  Processed {i+1}/{len(signals)}, {len(trades)} trades, "
                  f"{len(open_positions)} open...")

    print(f"\n  Total trades: {len(trades)}")
    print(f"  Skipped: {skipped}")
    return trades


# =========================================================================
# Reporting
# =========================================================================
def safe_pct(num, den):
    return num / den * 100 if den > 0 else 0


def stats(subset):
    n = len(subset)
    if n == 0:
        return None
    wins = [t for t in subset if t["pnl_rs"] > 0]
    total_pnl = sum(t["pnl_rs"] for t in subset)
    total_inv = sum(t["investment"] for t in subset)
    avg_pnl_pct = sum(t["pnl_pct"] for t in subset) / n
    avg_days = sum(t["days_held"] for t in subset) / n
    avg_iv = sum(t["iv"] for t in subset) / n
    avg_prem = sum(t["entry_premium"] for t in subset) / n
    return {
        "n": n, "win_pct": safe_pct(len(wins), n),
        "total_pnl": total_pnl, "avg_pnl_pct": avg_pnl_pct,
        "avg_days": avg_days, "avg_pnl_rs": total_pnl / n,
        "avg_iv": avg_iv, "avg_prem": avg_prem,
        "total_inv": total_inv,
    }


def report(trades, max_gap, max_hold, max_pos, dte):
    if not trades:
        print("No trades!")
        return

    bps = [t for t in trades if t["side"] == "BPS"]
    bcs = [t for t in trades if t["side"] == "BCS"]

    print(f"\n{'='*95}")
    print(f"  DAILY ST MAGNET — NAKED OPTION BACKTEST (1 lot, {max_pos} max positions)")
    print(f"  Gap {ENTRY_GAP_MIN}-{max_gap}% | Hold {max_hold}d | DTE {dte} | "
          f"Touch {TOUCH_PCT}% | Slip {SLIPPAGE_PCT*100:.0f}%")
    print(f"  {BACKTEST_START} to {BACKTEST_END}")
    print(f"{'='*95}")
    print(f"\n  Total: {len(trades)} trades | BPS: {len(bps)} | BCS: {len(bcs)}")
    d_range = f"{min(t['signal_date'] for t in trades)} to {max(t['signal_date'] for t in trades)}"
    print(f"  Range: {d_range}")
    print(f"  Unique symbols: {len(set(t['symbol'] for t in trades))}")

    total_pnl = sum(t["pnl_rs"] for t in trades)
    total_inv = sum(t["investment"] for t in trades)
    print(f"  Total P&L: Rs {total_pnl:+,.0f}")
    print(f"  Total capital deployed: Rs {total_inv:,.0f}")
    print(f"  Return on capital: {total_pnl/total_inv*100:.1f}%" if total_inv > 0 else "")

    # ── BPS vs BCS ──
    print(f"\n  {'='*85}")
    print(f"  BPS vs BCS — HEAD TO HEAD")
    print(f"  {'='*85}")
    print(f"  {'Side':<6} {'#':>6} {'Win%':>7} {'AvgPnL%':>9} {'AvgPnL Rs':>10} {'Total Rs':>12} {'AvgIV':>7} {'AvgPrem':>8}")
    print(f"  {'─'*75}")
    for label, subset in [("BPS", bps), ("BCS", bcs), ("ALL", trades)]:
        s = stats(subset)
        if s:
            print(f"  {label:<6} {s['n']:>6} {s['win_pct']:>6.1f}% {s['avg_pnl_pct']:>+8.1f}% "
                  f"{s['avg_pnl_rs']:>+9,.0f} {s['total_pnl']:>+11,.0f} {s['avg_iv']:>6.1f}% {s['avg_prem']:>7.1f}")

    # ── By exit reason ──
    print(f"\n  {'='*85}")
    print(f"  BY EXIT REASON")
    print(f"  {'='*85}")
    print(f"  {'Reason':<12} {'#':>6} {'%':>6} {'Win%':>7} {'AvgPnL%':>9} {'Total Rs':>12}")
    print(f"  {'─'*55}")
    reasons = ["TOUCH", "ST_FLIP", "TRAIL", "COST_SL", "PREM_SL", "REVERSE", "TIME", "DATA_END"]
    for reason in reasons:
        subset = [t for t in trades if t["exit_reason"] == reason]
        if not subset:
            continue
        s = stats(subset)
        pct_of_total = len(subset) / len(trades) * 100
        print(f"  {reason:<12} {s['n']:>6} {pct_of_total:>5.1f}% {s['win_pct']:>6.1f}% "
              f"{s['avg_pnl_pct']:>+8.1f}% {s['total_pnl']:>+11,.0f}")

    # ── By gap zone ──
    print(f"\n  {'='*85}")
    print(f"  BY ENTRY GAP")
    print(f"  {'='*85}")
    gap_zones = [(f"{lo:.1f}-{hi:.1f}%", lo, hi)
                 for lo, hi in [(0.3, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 2.5), (2.5, 3.0)]]
    print(f"  {'Gap':<10} {'#':>6} {'Win%':>7} {'AvgPnL%':>9} {'Total Rs':>12} {'AvgIV':>7}")
    print(f"  {'─'*55}")
    for label, lo, hi in gap_zones:
        subset = [t for t in trades if lo <= t["gap_pct"] < hi]
        s = stats(subset)
        if s:
            print(f"  {label:<10} {s['n']:>6} {s['win_pct']:>6.1f}% {s['avg_pnl_pct']:>+8.1f}% "
                  f"{s['total_pnl']:>+11,.0f} {s['avg_iv']:>6.1f}%")

    # ── By market phase ──
    print(f"\n  {'='*85}")
    print(f"  BY MARKET PHASE")
    print(f"  {'='*85}")
    print(f"  {'Phase':<22} {'#':>6} {'Win%':>7} {'AvgPnL%':>9} {'Total Rs':>12}")
    print(f"  {'─'*60}")
    phases = sorted(set(t["market_phase"] for t in trades),
                    key=lambda p: min(t["signal_date"] for t in trades if t["market_phase"] == p))
    for phase in phases:
        subset = [t for t in trades if t["market_phase"] == phase]
        s = stats(subset)
        if s:
            print(f"  {phase:<22} {s['n']:>6} {s['win_pct']:>6.1f}% {s['avg_pnl_pct']:>+8.1f}% "
                  f"{s['total_pnl']:>+11,.0f}")

    # ── By regime ──
    print(f"\n  {'='*85}")
    print(f"  BY REGIME STATUS")
    print(f"  {'='*85}")
    print(f"  {'Status':<12} {'#':>6} {'Win%':>7} {'AvgPnL%':>9} {'Total Rs':>12}")
    print(f"  {'─'*50}")
    for status in ["UNPAUSED", "PAUSED", "UNKNOWN"]:
        subset = [t for t in trades if t["regime_status"] == status]
        s = stats(subset)
        if s:
            print(f"  {status:<12} {s['n']:>6} {s['win_pct']:>6.1f}% {s['avg_pnl_pct']:>+8.1f}% "
                  f"{s['total_pnl']:>+11,.0f}")

    # ── Quarterly P&L ──
    print(f"\n  {'='*85}")
    print(f"  QUARTERLY P&L")
    print(f"  {'='*85}")
    sorted_trades = sorted(trades, key=lambda t: t["signal_date"])
    by_quarter = defaultdict(lambda: {"pnl": 0, "n": 0, "wins": 0, "inv": 0})
    for t in sorted_trades:
        q = f"{t['signal_date'].year}-Q{(t['signal_date'].month-1)//3+1}"
        by_quarter[q]["pnl"] += t["pnl_rs"]
        by_quarter[q]["n"] += 1
        by_quarter[q]["inv"] += t["investment"]
        if t["pnl_rs"] > 0:
            by_quarter[q]["wins"] += 1

    print(f"  {'Quarter':<12} {'#':>5} {'Win%':>7} {'PnL':>12} {'Cumul':>12} {'ROC':>7}")
    print(f"  {'─'*60}")
    cumul = 0
    for q in sorted(by_quarter.keys()):
        qd = by_quarter[q]
        cumul += qd["pnl"]
        wr = qd["wins"] / qd["n"] * 100 if qd["n"] > 0 else 0
        roc = qd["pnl"] / qd["inv"] * 100 if qd["inv"] > 0 else 0
        print(f"  {q:<12} {qd['n']:>5} {wr:>6.0f}% {qd['pnl']:>+11,.0f} {cumul:>+11,.0f} {roc:>+6.1f}%")

    print(f"\n  Grand Total: Rs {total_pnl:+,.0f} over {len(trades)} trades")
    print(f"  Avg PnL/trade: Rs {total_pnl/len(trades):+,.0f}")

    # ── Monthly detail ──
    print(f"\n  {'='*85}")
    print(f"  MONTHLY DETAIL")
    print(f"  {'='*85}")
    print(f"  {'Month':<10} {'BPS':>4} {'BCS':>4} {'Tot':>4} {'Win%':>7} {'PnL':>11} {'Phase':<20}")
    print(f"  {'─'*70}")
    monthly = defaultdict(list)
    for t in trades:
        ym = f"{t['signal_date'].year}-{t['signal_date'].month:02d}"
        monthly[ym].append(t)
    for ym in sorted(monthly.keys()):
        mts = monthly[ym]
        n_bps = len([t for t in mts if t["side"] == "BPS"])
        n_bcs = len([t for t in mts if t["side"] == "BCS"])
        s = stats(mts)
        phase = mts[0]["market_phase"]
        print(f"  {ym:<10} {n_bps:>4} {n_bcs:>4} {s['n']:>4} {s['win_pct']:>6.1f}% "
              f"{s['total_pnl']:>+10,.0f} {phase:<20}")

    # ── Top/Bottom ──
    print(f"\n  {'='*85}")
    print(f"  TOP 15 TRADES")
    print(f"  {'='*85}")
    by_pnl = sorted(trades, key=lambda t: t["pnl_rs"], reverse=True)
    print(f"  {'Symbol':<14} {'Side':<5} {'Date':>12} {'Gap%':>6} {'PnL%':>8} {'Rs PnL':>10} "
          f"{'Entry':>7} {'Exit':>7} {'IV':>5} {'Days':>5} {'Reason':>8}")
    print(f"  {'─'*100}")
    for t in by_pnl[:15]:
        print(f"  {t['symbol']:<14} {t['side']:<5} {str(t['signal_date']):>12} {t['gap_pct']:>5.1f}% "
              f"{t['pnl_pct']:>+7.1f}% {t['pnl_rs']:>+9,.0f} "
              f"{t['entry_premium']:>7.1f} {t['exit_premium']:>7.1f} {t['iv']:>4.0f}% {t['days_held']:>5} {t['exit_reason']:>8}")

    print(f"\n  BOTTOM 15 TRADES")
    print(f"  {'─'*100}")
    for t in by_pnl[-15:]:
        print(f"  {t['symbol']:<14} {t['side']:<5} {str(t['signal_date']):>12} {t['gap_pct']:>5.1f}% "
              f"{t['pnl_pct']:>+7.1f}% {t['pnl_rs']:>+9,.0f} "
              f"{t['entry_premium']:>7.1f} {t['exit_premium']:>7.1f} {t['iv']:>4.0f}% {t['days_held']:>5} {t['exit_reason']:>8}")

    # ── Summary stats ──
    wins = [t for t in trades if t["pnl_rs"] > 0]
    losses = [t for t in trades if t["pnl_rs"] <= 0]
    print(f"\n  {'='*85}")
    print(f"  SUMMARY STATISTICS")
    print(f"  {'='*85}")
    print(f"  Winners: {len(wins)} ({len(wins)/len(trades)*100:.1f}%) | "
          f"Avg win: Rs {sum(t['pnl_rs'] for t in wins)/len(wins):+,.0f}" if wins else "")
    if losses:
        print(f"  Losers:  {len(losses)} ({len(losses)/len(trades)*100:.1f}%) | "
              f"Avg loss: Rs {sum(t['pnl_rs'] for t in losses)/len(losses):+,.0f}")
    if wins and losses:
        avg_win = sum(t["pnl_rs"] for t in wins) / len(wins)
        avg_loss = abs(sum(t["pnl_rs"] for t in losses) / len(losses))
        print(f"  Win/Loss ratio: {avg_win/avg_loss:.2f}x" if avg_loss > 0 else "")
    print(f"  Avg entry premium: Rs {sum(t['entry_premium'] for t in trades)/len(trades):.1f}")
    print(f"  Avg IV at entry: {sum(t['iv'] for t in trades)/len(trades):.1f}%")
    print(f"  Avg days held: {sum(t['days_held'] for t in trades)/len(trades):.1f}")
    print(f"  Max concurrent positions: {max_pos}")


# =========================================================================
# Export & Main
# =========================================================================
def export_csv(trades, path):
    if not trades:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
        writer.writeheader()
        for t in trades:
            row = {k: (str(v) if isinstance(v, date) else v) for k, v in t.items()}
            writer.writerow(row)
    print(f"\n  Exported {len(trades)} trades to {path}")


def _apply_config(dedup, touch, min_gap):
    global DEDUP_GAP_DAYS, TOUCH_PCT, ENTRY_GAP_MIN
    DEDUP_GAP_DAYS = dedup
    TOUCH_PCT = touch
    ENTRY_GAP_MIN = min_gap


def main():
    parser = argparse.ArgumentParser(description="Daily ST naked option backtest")
    parser.add_argument("--side", choices=["BPS", "BCS"], default=None)
    parser.add_argument("--max-gap", type=float, default=MAX_GAP_PCT)
    parser.add_argument("--min-gap", type=float, default=ENTRY_GAP_MIN)
    parser.add_argument("--max-hold", type=int, default=MAX_HOLD_DAYS)
    parser.add_argument("--max-pos", type=int, default=MAX_POSITIONS)
    parser.add_argument("--dte", type=int, default=DTE)
    parser.add_argument("--dedup", type=int, default=DEDUP_GAP_DAYS)
    parser.add_argument("--touch", type=float, default=TOUCH_PCT)
    parser.add_argument("--export", type=str, default=None)
    args = parser.parse_args()

    _apply_config(args.dedup, args.touch, args.min_gap)

    print("=" * 70)
    print(f"  DAILY ST MAGNET — NAKED OPTION BACKTEST")
    print(f"  Gap: {args.min_gap:.1f}%-{args.max_gap:.1f}% | Hold: {args.max_hold}d | "
          f"DTE: {args.dte} | Pos cap: {args.max_pos}")
    print(f"  Touch: {args.touch:.1f}% | Slip: {SLIPPAGE_PCT*100:.0f}% | "
          f"Lots: {LOTS_PER_TRADE}")
    print(f"  {BACKTEST_START} to {BACKTEST_END}")
    print("=" * 70)

    print(f"\n[1/5] Loading lot sizes...")
    lot_sizes = load_lot_sizes()
    print(f"  Found lot sizes for {len(lot_sizes)} symbols")

    print(f"\n[2/5] Generating daily ST signals...")
    signals = generate_daily_signals(args.max_gap, args.min_gap)
    if not signals:
        print("No signals!")
        return

    bps_sigs = len([s for s in signals if s["side"] == "BPS"])
    bcs_sigs = len([s for s in signals if s["side"] == "BCS"])
    print(f"  BPS signals: {bps_sigs}, BCS signals: {bcs_sigs}")

    print(f"\n[3/5] Building regime timeline...")
    regime = build_regime_timeline()
    if regime:
        print(f"  Built for {len(regime)} trading days")

    print(f"\n[4/5] Simulating trades (max {args.max_pos} concurrent)...")
    trades = process_with_position_cap(signals, regime, lot_sizes, args.max_gap,
                                        args.max_hold, args.dte, args.max_pos,
                                        side_filter=args.side)

    print(f"\n[5/5] Report...")
    report(trades, args.max_gap, args.max_hold, args.max_pos, args.dte)

    export_path = args.export or (SCRIPT_DIR / "backtest_daily_naked_results.csv")
    export_csv(trades, export_path)


if __name__ == "__main__":
    main()
