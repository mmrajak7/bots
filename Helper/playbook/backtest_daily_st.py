"""
Backtest: Daily ST(10,3) Magnet Pull
====================================
Does price get attracted to its Daily Supertrend line the same way
it does to Weekly/Monthly ST?

Signal: Price within 2-3% of Daily ST(10,3)
  - BPS: ST UP (support), price above ST approaching down
  - BCS: ST DOWN (resistance), price below ST approaching up

Exit:
  - TOUCH: price reaches within 0.5% of ST -> full capture
  - TIME: max 5 trading days
  - ST_FLIP: ST direction changes mid-trade -> exit (thesis dead)
  - REVERSE: price moves 5% AWAY from ST -> exit (thesis dead)

Uses same data as bidirectional backtest (backtest_cache/).

Usage:
    python backtest_daily_st.py
    python backtest_daily_st.py --max-gap 2.0
    python backtest_daily_st.py --side BPS
    python backtest_daily_st.py --max-hold 7
"""

import csv
import json
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

ST_PERIOD = 10
ST_MULTIPLIER = 3
MIN_PRICE = 100

# Entry filters
MAX_GAP_PCT = 3.0       # signal fires within this %
ENTRY_GAP_MIN = 0.3     # too close = already touching
DEDUP_GAP_DAYS = 7      # min days between same (symbol, side) signals

# Trade simulation
MAX_HOLD_DAYS = 5        # trading days (not calendar)
TOUCH_PCT = 0.5          # within 0.5% of ST = touched
REVERSE_PCT = 5.0        # price moves 5% away = thesis dead
SPREAD_DEBIT_PCT = 40    # typical net debit as % of spread width

# Backtest date range
BACKTEST_START = date(2022, 1, 1)
BACKTEST_END = date(2026, 3, 18)

# Skip filters
SKIP_SUFFIXES = ("BEES", "ETF", "-RR", "-RE")
SKIP_EXACT = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "MIDCPNIFTY",
              "CNXIT", "IT", "TECH", "NIFTY50"}


# =========================================================================
# Core Utilities
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


def should_skip(sym):
    upper = sym.upper()
    if upper in SKIP_EXACT:
        return True
    for s in SKIP_SUFFIXES:
        if upper.endswith(s):
            return True
    return False


def load_fo_stocks():
    if FO_LIST_FILE.exists():
        with open(FO_LIST_FILE) as f:
            return set(json.load(f))
    return None


# =========================================================================
# Regime Timeline (reused from bidirectional)
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

        regime[dt] = {
            "status": "UNPAUSED" if is_unpaused else "PAUSED",
            "signals_on": sig_on,
            "rsi": round(r, 1) if r else None,
            "breadth": round(b, 1) if b else None,
        }

    return regime


# =========================================================================
# Daily ST Signal Generation
# =========================================================================
def generate_daily_signals(max_gap, min_gap):
    """
    For each F&O stock, compute Daily ST(10,3) on all candles up to day D-1,
    then check day D's close vs ST. If within gap range -> signal.
    """
    fo_stocks = load_fo_stocks()
    if not fo_stocks:
        print("  ERROR: No F&O stock list")
        return []

    print(f"  Scanning {len(fo_stocks)} F&O stocks for daily ST signals...")
    print(f"  Gap range: {min_gap:.1f}% - {max_gap:.1f}%")

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

        # Compute ST on ALL daily data at once (efficient)
        st_data = compute_supertrend(daily, ST_PERIOD, ST_MULTIPLIER)
        if not st_data:
            continue

        # Build date->ST lookup
        st_map = {s["date"]: s for s in st_data}

        # Scan each day in backtest range
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

            if st_dir == "UP" and price > st_val:
                # BPS: price above ST support
                gap = (price - st_val) / st_val * 100
                if min_gap < gap <= max_gap:
                    all_signals.append({
                        "date": c["date"], "symbol": sym, "tf": "D",
                        "side": "BPS", "st_value": round(st_val, 2),
                        "st_dir": st_dir, "gap_pct": round(gap, 2),
                        "price": round(price, 2), "atr": st_info["atr"],
                    })

            elif st_dir == "DOWN" and price < st_val:
                # BCS: price below ST resistance
                gap = (st_val - price) / st_val * 100
                if min_gap < gap <= max_gap:
                    all_signals.append({
                        "date": c["date"], "symbol": sym, "tf": "D",
                        "side": "BCS", "st_value": round(st_val, 2),
                        "st_dir": st_dir, "gap_pct": round(gap, 2),
                        "price": round(price, 2), "atr": st_info["atr"],
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
# Trade Simulation — Daily ST
# =========================================================================
def simulate_daily_trade(sym, signal_date, daily_data, st_value, side, max_hold):
    """
    Simulate trade with daily ST-specific exits:
    - TOUCH: price reaches within TOUCH_PCT% of ST
    - ST_FLIP: ST direction changes (thesis invalidated)
    - REVERSE: price moves REVERSE_PCT% away from ST
    - TIME: max_hold trading days
    """
    idx_map = {c["date"]: i for i, c in enumerate(daily_data)}
    if signal_date not in idx_map:
        for offset in range(1, 5):
            d = signal_date + timedelta(days=offset)
            if d in idx_map:
                signal_date = d
                break
        else:
            return None

    entry_idx = idx_map[signal_date]
    entry_price = daily_data[entry_idx]["close"]

    if entry_price < MIN_PRICE:
        return None

    # Spread width = distance between entry price and ST
    if side == "BPS":
        spread_width = entry_price - st_value
    else:
        spread_width = st_value - entry_price

    if spread_width <= 0:
        return None

    # Recompute ST for forward days (we need real-time ST during the trade)
    # Use the full daily data up to each day to get the ST at that point
    st_data = compute_supertrend(daily_data, ST_PERIOD, ST_MULTIPLIER)
    st_map = {s["date"]: s for s in st_data}

    entry_st_dir = "UP" if side == "BPS" else "DOWN"

    touched = False
    touch_date = None
    touch_price = None
    best_move = 0
    best_move_date = signal_date
    exit_date = None
    exit_price = None
    exit_reason = None
    trading_days = 0

    for i in range(entry_idx + 1, len(daily_data)):
        candle = daily_data[i]
        trading_days += 1

        # Get current day's ST
        cur_st = st_map.get(candle["date"])
        cur_st_val = cur_st["supertrend"] if cur_st else st_value
        cur_st_dir = cur_st["direction"] if cur_st else entry_st_dir

        # EXIT 1: ST flipped direction
        if cur_st_dir != entry_st_dir:
            exit_date = candle["date"]
            exit_price = candle["close"]
            exit_reason = "ST_FLIP"
            # Calculate move at exit
            if side == "BPS":
                move = entry_price - candle["close"]
            else:
                move = candle["close"] - entry_price
            if move > best_move:
                best_move = move
            break

        if side == "BPS":
            # BPS profits from decline toward ST
            move = entry_price - candle["low"]
            if move > best_move:
                best_move = move
                best_move_date = candle["date"]

            # Touch: low reaches within TOUCH_PCT of ST
            if cur_st_val > 0:
                low_gap = (candle["low"] - cur_st_val) / cur_st_val * 100
                if low_gap <= TOUCH_PCT and not touched:
                    touched = True
                    touch_date = candle["date"]
                    touch_price = candle["low"]
                    exit_date = candle["date"]
                    exit_price = candle["close"]
                    exit_reason = "TOUCH"
                    break

            # Reverse: price moves away
            high_gap = (candle["high"] - st_value) / st_value * 100
            if high_gap >= REVERSE_PCT:
                exit_date = candle["date"]
                exit_price = candle["close"]
                exit_reason = "REVERSE"
                break

        else:  # BCS
            move = candle["high"] - entry_price
            if move > best_move:
                best_move = move
                best_move_date = candle["date"]

            # Touch: high reaches within TOUCH_PCT of ST
            if cur_st_val > 0:
                high_gap = (cur_st_val - candle["high"]) / cur_st_val * 100
                if high_gap <= TOUCH_PCT and not touched:
                    touched = True
                    touch_date = candle["date"]
                    touch_price = candle["high"]
                    exit_date = candle["date"]
                    exit_price = candle["close"]
                    exit_reason = "TOUCH"
                    break

            # Reverse: price moves away
            low_gap = (st_value - candle["low"]) / st_value * 100
            if low_gap >= REVERSE_PCT:
                exit_date = candle["date"]
                exit_price = candle["close"]
                exit_reason = "REVERSE"
                break

        # EXIT: Time limit
        if trading_days >= max_hold:
            exit_date = candle["date"]
            exit_price = candle["close"]
            exit_reason = "TIME"
            break

    if exit_date is None:
        if entry_idx < len(daily_data) - 1:
            exit_date = daily_data[-1]["date"]
            exit_price = daily_data[-1]["close"]
            exit_reason = "DATA_END"
        else:
            return None

    days_held = (exit_date - signal_date).days

    # P&L calculation
    spread_capture = min(best_move / spread_width, 1.0) if spread_width > 0 else 0
    net_debit_pct = SPREAD_DEBIT_PCT / 100

    if touched:
        pnl_pct = (1 - net_debit_pct) * 100  # full spread captured
    else:
        pnl_pct = (spread_capture - net_debit_pct) * 100

    pnl_per_share = pnl_pct / 100 * spread_width

    # Also compute naked option P&L estimate
    # Entry: buy ATM option. Premium ~ 3-5% of stock price for near-term
    # Approximate with move as % of entry price
    if side == "BPS":
        actual_move = entry_price - exit_price  # positive if price fell (good)
    else:
        actual_move = exit_price - entry_price  # positive if price rose (good)
    move_pct = actual_move / entry_price * 100

    return {
        "symbol": sym, "tf": "D", "side": side,
        "signal_date": signal_date,
        "entry_price": round(entry_price, 2),
        "st_value": round(st_value, 2),
        "spread_width": round(spread_width, 2),
        "gap_pct": round(spread_width / st_value * 100, 2),
        "touched": touched, "touch_date": touch_date,
        "touch_price": round(touch_price, 2) if touch_price else None,
        "best_move": round(best_move, 2),
        "best_move_date": best_move_date,
        "exit_date": exit_date, "exit_price": round(exit_price, 2),
        "exit_reason": exit_reason,
        "days_held": days_held,
        "trading_days": trading_days,
        "spread_capture_pct": round(spread_capture * 100, 1),
        "pnl_pct": round(pnl_pct, 1),
        "pnl_per_share": round(pnl_per_share, 2),
        "move_pct": round(move_pct, 2),
    }


# =========================================================================
# Processing
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


def process_signals(signals, regime, max_gap, max_hold, side_filter=None):
    trades = []
    skipped = {
        "gap": 0, "sim_fail": 0, "side_filter": 0,
    }

    for i, sig in enumerate(signals):
        sym = sig["symbol"]
        signal_date = sig["date"]
        side = sig["side"]
        st_value = sig["st_value"]

        if side_filter and side != side_filter:
            skipped["side_filter"] += 1
            continue

        if sig["gap_pct"] > max_gap or sig["gap_pct"] <= ENTRY_GAP_MIN:
            skipped["gap"] += 1
            continue

        daily = load_daily(sym)
        if daily is None:
            skipped["sim_fail"] += 1
            continue

        trade = simulate_daily_trade(sym, signal_date, daily, st_value, side, max_hold)
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
        trade["regime_signals_on"] = r_info["signals_on"] if r_info else None
        trade["market_phase"] = classify_market_phase(signal_date)
        trades.append(trade)

        if (i + 1) % 2000 == 0:
            print(f"  Processed {i+1}/{len(signals)}, {len(trades)} trades...")

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
    touched = [t for t in subset if t["touched"]]
    wins = [t for t in subset if t["pnl_pct"] > 0]
    avg_pnl = sum(t["pnl_pct"] for t in subset) / n
    avg_days = sum(t["days_held"] for t in subset) / n
    avg_trading = sum(t["trading_days"] for t in subset) / n
    total_rs = sum(t["pnl_per_share"] * int(500_000 / t["entry_price"]) for t in subset)
    return {
        "n": n, "touch_pct": safe_pct(len(touched), n),
        "win_pct": safe_pct(len(wins), n), "avg_pnl": avg_pnl,
        "avg_days": avg_days, "avg_trading": avg_trading,
        "total_rs": total_rs,
    }


def report(trades, max_gap, max_hold):
    if not trades:
        print("No trades!")
        return

    bps = [t for t in trades if t["side"] == "BPS"]
    bcs = [t for t in trades if t["side"] == "BCS"]

    print(f"\n{'='*90}")
    print(f"  DAILY ST(10,3) MAGNET PULL BACKTEST")
    print(f"  gap {ENTRY_GAP_MIN}-{max_gap}% | dedup {DEDUP_GAP_DAYS}d | "
          f"max hold {max_hold} trading days | touch <{TOUCH_PCT}%")
    print(f"  {BACKTEST_START} to {BACKTEST_END}")
    print(f"{'='*90}")
    print(f"\n  Total: {len(trades)} trades | BPS: {len(bps)} | BCS: {len(bcs)}")
    d_range = f"{min(t['signal_date'] for t in trades)} to {max(t['signal_date'] for t in trades)}"
    print(f"  Range: {d_range}")
    print(f"  Unique symbols: {len(set(t['symbol'] for t in trades))}")

    # ── BPS vs BCS ──
    print(f"\n  {'='*80}")
    print(f"  BPS vs BCS — HEAD TO HEAD")
    print(f"  {'='*80}")
    print(f"  {'Side':<6} {'#':>6} {'Touch%':>8} {'Win%':>7} {'AvgPnL':>9} {'AvgDays':>8} {'TrdDays':>8} {'Total Rs':>12}")
    print(f"  {'─'*70}")
    for label, subset in [("BPS", bps), ("BCS", bcs), ("ALL", trades)]:
        s = stats(subset)
        if s:
            print(f"  {label:<6} {s['n']:>6} {s['touch_pct']:>7.1f}% {s['win_pct']:>6.1f}% "
                  f"{s['avg_pnl']:>+8.1f}% {s['avg_days']:>7.1f} {s['avg_trading']:>7.1f} {s['total_rs']:>+11,.0f}")

    # ── By exit reason ──
    print(f"\n  {'='*80}")
    print(f"  BY EXIT REASON")
    print(f"  {'='*80}")
    print(f"  {'Reason':<12} {'#':>6} {'%':>6} {'Win%':>7} {'AvgPnL':>9} {'AvgDays':>8} {'Total Rs':>12}")
    print(f"  {'─'*65}")
    reasons = ["TOUCH", "ST_FLIP", "REVERSE", "TIME", "DATA_END"]
    for reason in reasons:
        subset = [t for t in trades if t["exit_reason"] == reason]
        if not subset:
            continue
        s = stats(subset)
        pct_of_total = len(subset) / len(trades) * 100
        print(f"  {reason:<12} {s['n']:>6} {pct_of_total:>5.1f}% {s['win_pct']:>6.1f}% "
              f"{s['avg_pnl']:>+8.1f}% {s['avg_days']:>7.1f} {s['total_rs']:>+11,.0f}")

    # ── By gap zone ──
    print(f"\n  {'='*80}")
    print(f"  BY ENTRY GAP")
    print(f"  {'='*80}")
    gap_zones = [(f"{lo:.1f}-{hi:.1f}%", lo, hi)
                 for lo, hi in [(0.3, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 2.5), (2.5, 3.0)]]
    print(f"  {'Gap':<10} {'#':>6} {'Touch%':>8} {'Win%':>7} {'AvgPnL':>9} {'Total Rs':>12}")
    print(f"  {'─'*55}")
    for label, lo, hi in gap_zones:
        subset = [t for t in trades if lo <= t["gap_pct"] < hi]
        s = stats(subset)
        if s:
            print(f"  {label:<10} {s['n']:>6} {s['touch_pct']:>7.1f}% {s['win_pct']:>6.1f}% "
                  f"{s['avg_pnl']:>+8.1f}% {s['total_rs']:>+11,.0f}")

    # ── By market phase ──
    print(f"\n  {'='*80}")
    print(f"  BY MARKET PHASE")
    print(f"  {'='*80}")
    print(f"  {'Phase':<22} {'#':>6} {'Touch%':>8} {'Win%':>7} {'AvgPnL':>9} {'Total Rs':>12}")
    print(f"  {'─'*70}")
    phases = sorted(set(t["market_phase"] for t in trades),
                    key=lambda p: min(t["signal_date"] for t in trades if t["market_phase"] == p))
    for phase in phases:
        subset = [t for t in trades if t["market_phase"] == phase]
        s = stats(subset)
        if s:
            print(f"  {phase:<22} {s['n']:>6} {s['touch_pct']:>7.1f}% {s['win_pct']:>6.1f}% "
                  f"{s['avg_pnl']:>+8.1f}% {s['total_rs']:>+11,.0f}")

    # ── By regime ──
    print(f"\n  {'='*80}")
    print(f"  BY REGIME STATUS")
    print(f"  {'='*80}")
    print(f"  {'Status':<12} {'#':>6} {'Touch%':>8} {'Win%':>7} {'AvgPnL':>9} {'Total Rs':>12}")
    print(f"  {'─'*55}")
    for status in ["UNPAUSED", "PAUSED", "UNKNOWN"]:
        subset = [t for t in trades if t["regime_status"] == status]
        s = stats(subset)
        if s:
            print(f"  {status:<12} {s['n']:>6} {s['touch_pct']:>7.1f}% {s['win_pct']:>6.1f}% "
                  f"{s['avg_pnl']:>+8.1f}% {s['total_rs']:>+11,.0f}")

    # ── Quarterly P&L ──
    print(f"\n  {'='*80}")
    print(f"  QUARTERLY P&L (Rs 5L notional per trade)")
    print(f"  {'='*80}")
    sorted_trades = sorted(trades, key=lambda t: t["signal_date"])
    by_quarter = defaultdict(lambda: {"pnl": 0, "n": 0, "wins": 0})

    for t in sorted_trades:
        qty = int(500_000 / t["entry_price"])
        trade_pnl = t["pnl_per_share"] * qty
        q = f"{t['signal_date'].year}-Q{(t['signal_date'].month-1)//3+1}"
        by_quarter[q]["pnl"] += trade_pnl
        by_quarter[q]["n"] += 1
        if t["pnl_pct"] > 0:
            by_quarter[q]["wins"] += 1

    print(f"  {'Quarter':<12} {'#':>5} {'Win%':>7} {'PnL':>12} {'Cumul':>12}")
    print(f"  {'─'*50}")
    cumul = 0
    for q in sorted(by_quarter.keys()):
        qd = by_quarter[q]
        cumul += qd["pnl"]
        wr = qd["wins"] / qd["n"] * 100 if qd["n"] > 0 else 0
        print(f"  {q:<12} {qd['n']:>5} {wr:>6.0f}% {qd['pnl']:>+11,.0f} {cumul:>+11,.0f}")

    total_pnl = sum(t["pnl_per_share"] * int(500_000 / t["entry_price"]) for t in trades)
    print(f"\n  Grand Total: Rs {total_pnl:+,.0f} over {len(trades)} trades")
    print(f"  Avg PnL/trade: Rs {total_pnl/len(trades):+,.0f}")

    # ── Monthly detail ──
    print(f"\n  {'='*80}")
    print(f"  MONTHLY DETAIL")
    print(f"  {'='*80}")
    print(f"  {'Month':<10} {'BPS':>4} {'BCS':>4} {'Tot':>4} {'Touch%':>7} {'Win%':>7} {'AvgPnL':>9} {'Phase':<20}")
    print(f"  {'─'*75}")
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
        print(f"  {ym:<10} {n_bps:>4} {n_bcs:>4} {s['n']:>4} {s['touch_pct']:>6.1f}% "
              f"{s['win_pct']:>6.1f}% {s['avg_pnl']:>+8.1f}% {phase:<20}")

    # ── Top/Bottom trades ──
    print(f"\n  {'='*80}")
    print(f"  TOP 15 TRADES")
    print(f"  {'='*80}")
    by_pnl = sorted(trades, key=lambda t: t["pnl_per_share"] * int(500_000/t["entry_price"]), reverse=True)
    print(f"  {'Symbol':<14} {'Side':<5} {'Date':>12} {'Gap%':>6} {'PnL%':>7} {'Rs PnL':>10} {'Days':>5} {'Exit':>8} {'Phase':<18}")
    print(f"  {'─'*90}")
    for t in by_pnl[:15]:
        qty = int(500_000 / t["entry_price"])
        rs = t["pnl_per_share"] * qty
        print(f"  {t['symbol']:<14} {t['side']:<5} {str(t['signal_date']):>12} {t['gap_pct']:>5.1f}% "
              f"{t['pnl_pct']:>+6.1f}% {rs:>+9,.0f} {t['days_held']:>5} {t['exit_reason']:>8} {t['market_phase']:<18}")

    print(f"\n  BOTTOM 15 TRADES")
    print(f"  {'─'*90}")
    for t in by_pnl[-15:]:
        qty = int(500_000 / t["entry_price"])
        rs = t["pnl_per_share"] * qty
        print(f"  {t['symbol']:<14} {t['side']:<5} {str(t['signal_date']):>12} {t['gap_pct']:>5.1f}% "
              f"{t['pnl_pct']:>+6.1f}% {rs:>+9,.0f} {t['days_held']:>5} {t['exit_reason']:>8} {t['market_phase']:<18}")

    # ── Comparison hint ──
    print(f"\n  {'='*80}")
    print(f"  DAILY vs WEEKLY/MONTHLY COMPARISON NOTES")
    print(f"  {'='*80}")
    touch_trades = [t for t in trades if t["exit_reason"] == "TOUCH"]
    time_trades = [t for t in trades if t["exit_reason"] == "TIME"]
    flip_trades = [t for t in trades if t["exit_reason"] == "ST_FLIP"]
    print(f"  Touch rate: {len(touch_trades)/len(trades)*100:.1f}% ({len(touch_trades)}/{len(trades)})")
    print(f"  Time exits: {len(time_trades)/len(trades)*100:.1f}% ({len(time_trades)}/{len(trades)})")
    print(f"  ST flip exits: {len(flip_trades)/len(trades)*100:.1f}% ({len(flip_trades)}/{len(trades)})")
    if touch_trades:
        avg_touch_days = sum(t["trading_days"] for t in touch_trades) / len(touch_trades)
        print(f"  Avg trading days to touch: {avg_touch_days:.1f}")
    print(f"  Avg spread width: Rs {sum(t['spread_width'] for t in trades)/len(trades):.1f}")
    print(f"  Avg gap at entry: {sum(t['gap_pct'] for t in trades)/len(trades):.2f}%")


# =========================================================================
# Export & Main
# =========================================================================
def _apply_config(dedup, touch, min_gap):
    global DEDUP_GAP_DAYS, TOUCH_PCT, ENTRY_GAP_MIN
    DEDUP_GAP_DAYS = dedup
    TOUCH_PCT = touch
    ENTRY_GAP_MIN = min_gap


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


def main():
    parser = argparse.ArgumentParser(description="Daily ST magnet pull backtest")
    parser.add_argument("--side", choices=["BPS", "BCS"], default=None, help="Filter side")
    parser.add_argument("--max-gap", type=float, default=MAX_GAP_PCT, help="Max gap %% (default 3.0)")
    parser.add_argument("--min-gap", type=float, default=ENTRY_GAP_MIN, help="Min gap %% (default 0.3)")
    parser.add_argument("--max-hold", type=int, default=MAX_HOLD_DAYS, help="Max hold trading days (default 5)")
    parser.add_argument("--dedup", type=int, default=DEDUP_GAP_DAYS, help="Dedup days (default 7)")
    parser.add_argument("--touch", type=float, default=TOUCH_PCT, help="Touch threshold %% (default 0.5)")
    parser.add_argument("--export", type=str, default=None)
    args = parser.parse_args()

    # Apply CLI overrides to module-level config
    _apply_config(args.dedup, args.touch, args.min_gap)

    print("=" * 70)
    print(f"  DAILY ST(10,3) MAGNET PULL BACKTEST")
    print(f"  Gap: {args.min_gap:.1f}%-{args.max_gap:.1f}% | Hold: {args.max_hold}d | "
          f"Touch: {args.touch:.1f}% | Dedup: {args.dedup}d")
    print(f"  {BACKTEST_START} to {BACKTEST_END}")
    print("=" * 70)

    print(f"\n[1/4] Generating daily ST signals...")
    signals = generate_daily_signals(args.max_gap, args.min_gap)
    if not signals:
        print("No signals!")
        return

    bps_sigs = len([s for s in signals if s["side"] == "BPS"])
    bcs_sigs = len([s for s in signals if s["side"] == "BCS"])
    print(f"  BPS signals: {bps_sigs}, BCS signals: {bcs_sigs}")

    print(f"\n[2/4] Building regime timeline...")
    regime = build_regime_timeline()
    if regime:
        print(f"  Built for {len(regime)} trading days")

    print(f"\n[3/4] Simulating trades...")
    trades = process_signals(signals, regime, args.max_gap, args.max_hold,
                             side_filter=args.side)

    print(f"\n[4/4] Report...")
    report(trades, args.max_gap, args.max_hold)

    export_path = args.export or (SCRIPT_DIR / "backtest_daily_st_results.csv")
    export_csv(trades, export_path)


if __name__ == "__main__":
    main()
