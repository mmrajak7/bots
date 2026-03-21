"""
Backtest: Magnet Strategy — Full Trade Simulation (D / W / M)
==============================================================
Simulates naked ATM option trades across all 3 timeframes using
the CURRENT production rules from magnet/config.py + monitor.py.

Rules mirrored:
  SIGNAL: Gap 2-5% (W/M) or 2-3% (D) from ST(10,3)
  ENTRY:  Gap shrinks to ≤2% → buy ATM option (ASK+2% slippage)
  EXIT PRIORITY:
    1. TP: spot wick crosses ST line
    2. Trail: premium drops below 50% of peak gain
    3. Cost SL: activated at 1% gap, floor = entry+0.10
    4. Premium SL: 25% (daily) / 40% (W/M)
    5. Hedge: gap widens to 3% (reversal) — net debit approach
    6. Spot SL: 5% gap from ST (hard backstop)
    7. Time SL: 5 trading days (W/M) / EOD exit (daily)
  FRESHNESS: Not in <2% zone in last 5 days
  DAILY VELOCITY: vel3d < -0.5, momentum >= 60%
  DEDUP: 30 days between signals for same stock+tf
  POSITION CAP: max 7 concurrent positions
  CAPITAL: Rs 10L, lot size adjusted per stock

Option premium estimation (no historical option data):
  Premium = ATR(14) × multiplier (CE/PE ATM proxy)
  Entry slippage: +2% of premium (ASK-side)
  Exit slippage: -2% of premium (BID-side)

Output: CSV with all trade details.

Usage:
    python backtest_magnet_trades.py
"""

import csv
import json
import sys
import math
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Paths ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "backtest_cache"
FO_LIST_FILE = CACHE_DIR / "_fo_stocks.json"
OUTPUT_CSV = SCRIPT_DIR / "backtest_magnet_trades.csv"

# ── Config (mirrors magnet/config.py) ────────────────────────────────────
ST_PERIOD = 10
ST_MULTIPLIER = 3
MIN_PRICE = 100

# Signal acceptance — 3% signal, 2% entry (all TFs unified)
SIGNAL_GAP_MAX_WM = 3.0       # % — W/M scanner range (tightened: 5% too wide, weak pull)
SIGNAL_GAP_MAX_D = 3.0        # % — Daily scanner range
ENTRY_GAP = 2.0               # % — enter when gap shrinks to this
ENTRY_GAP_MIN = 0.5           # % — too close, past entry zone
ENABLED_TFS = ("M", "W")      # Daily excluded — needs minute data for accurate sim

# Exit thresholds
COST_SL_GAP = 1.0             # % — activate cost SL here
HEDGE_GAP = 3.0               # % — reversal trigger for hedge
PREMIUM_SL_PCT_WM = 0.25      # 25% loss SL for W/M (tighter, cut losers faster)
PREMIUM_SL_PCT_D = 0.25       # 25% loss SL for daily
TRAIL_PCT = 0.50              # trail at 50% of peak gain
SL_GAP = 5.0                  # % — hard spot backstop
SL_TIME_DAYS = 5              # max trading days hold (W/M)
FRESHNESS_DAYS = 5            # bounce check window
DEDUP_GAP_DAYS = 30           # min days between signals for same stock+tf

# Daily velocity filters
DAILY_VEL_3D_MAX = -0.5
DAILY_MOMENTUM_MIN = 60

# Position cap
MAX_POSITIONS = 7
CAPITAL = 10_00_000            # Rs 10L

# Premium estimation: ATM option ≈ 2.5-3.5% of spot for ~20 DTE (realistic for F&O)
PREMIUM_PCT_OF_SPOT = 0.03    # 3% of spot price

# Position sizing: 1 lot per trade (matches production config)
# Lot value ~Rs 5-7L notional. Premium cost per lot ~Rs 10K-30K.
# With 7 positions, total capital at risk ~Rs 70K-210K.
LOTS_PER_TRADE = 7

# Slippage: 1% each way (realistic for liquid ATM F&O options)
ENTRY_SLIPPAGE = 1.01   # ASK + 1%
EXIT_SLIPPAGE = 0.99    # BID - 1%

BACKTEST_START = date(2025, 1, 1)
BACKTEST_END = date(2026, 3, 13)

SKIP_SUFFIXES = ("BEES", "ETF", "-RR", "-RE")
SKIP_EXACT = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "MIDCPNIFTY",
              "CNXIT", "IT", "TECH", "NIFTY50"}

# Typical lot sizes for F&O stocks (approximate — we'll adjust to fit capital)
# We'll compute lot_size = max(1, int(CAPITAL / MAX_POSITIONS / spot_price))
# Then round to nearest 50 or 100 for realism

# ── Data loading ─────────────────────────────────────────────────────────
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
            return sorted(json.load(f))
    return []


# ── Aggregation ──────────────────────────────────────────────────────────

def aggregate_to_weekly(daily_data, exclude_after=None):
    weeks = defaultdict(list)
    for c in daily_data:
        if exclude_after and c["date"] > exclude_after:
            continue
        weeks[c["date"].isocalendar()[:2]].append(c)
    # Exclude current (incomplete) week
    if exclude_after:
        wk = exclude_after.isocalendar()[:2]
        if wk in weeks:
            del weeks[wk]
    result = []
    for key in sorted(weeks.keys()):
        cs = weeks[key]
        result.append({
            "date": cs[0]["date"], "open": cs[0]["open"],
            "high": max(c["high"] for c in cs), "low": min(c["low"] for c in cs),
            "close": cs[-1]["close"],
        })
    return result


def aggregate_to_monthly(daily_data, exclude_after=None):
    months = defaultdict(list)
    for c in daily_data:
        if exclude_after and c["date"] > exclude_after:
            continue
        months[(c["date"].year, c["date"].month)].append(c)
    if exclude_after:
        mk = (exclude_after.year, exclude_after.month)
        if mk in months:
            del months[mk]
    result = []
    for key in sorted(months.keys()):
        cs = months[key]
        result.append({
            "date": cs[0]["date"], "open": cs[0]["open"],
            "high": max(c["high"] for c in cs), "low": min(c["low"] for c in cs),
            "close": cs[-1]["close"],
        })
    return result


# ── Supertrend ───────────────────────────────────────────────────────────

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
            st[i] = lb[i]
            d[i] = 1
            continue
        if not (lb[i] > lb[i - 1] or candles[i - 1]["close"] < lb[i - 1]):
            lb[i] = lb[i - 1]
        if not (ub[i] < ub[i - 1] or candles[i - 1]["close"] > ub[i - 1]):
            ub[i] = ub[i - 1]
        if st[i - 1] == lb[i - 1]:
            if candles[i]["close"] < lb[i]:
                d[i] = -1
                st[i] = ub[i]
            else:
                d[i] = 1
                st[i] = lb[i]
        else:
            if candles[i]["close"] > ub[i]:
                d[i] = 1
                st[i] = lb[i]
            else:
                d[i] = -1
                st[i] = ub[i]
    return [{"date": candles[i]["date"], "supertrend": round(st[i], 4),
             "direction": "UP" if d[i] == 1 else "DOWN", "atr": round(atr[i], 4)}
            for i in range(period - 1, n)]


def compute_atr14(daily_data, idx):
    """Compute ATR(14) at given daily index."""
    if idx < 14:
        return None
    trs = []
    for i in range(idx - 13, idx + 1):
        h, l = daily_data[i]["high"], daily_data[i]["low"]
        if i == 0:
            trs.append(h - l)
        else:
            pc = daily_data[i - 1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


# ── Velocity Filter ─────────────────────────────────────────────────────

def compute_velocity(daily, idx, st_value, side, lookback=5):
    """Compute gap velocity for signal validation."""
    if idx < lookback + 1:
        return None
    gaps = []
    for i in range(idx - lookback - 1, idx + 1):
        if i < 0:
            continue
        p = daily[i]["close"]
        if st_value <= 0:
            continue
        if side == "above" and p > st_value:
            gaps.append((daily[i]["date"], (p - st_value) / st_value * 100))
        elif side == "below" and p < st_value:
            gaps.append((daily[i]["date"], (st_value - p) / st_value * 100))
    if len(gaps) < 2:
        return None
    lb = min(3, len(gaps) - 1)
    gap_now = gaps[-1][1]
    gap_ago = gaps[-(lb + 1)][1]
    velocity = gap_now - gap_ago  # negative = approaching
    check = min(5, len(gaps) - 1)
    closing = sum(1 for i in range(1, check + 1) if gaps[-i][1] < gaps[-(i + 1)][1])
    momentum = closing / check * 100 if check > 0 else 0
    return {"velocity": round(velocity, 3), "momentum": round(momentum, 1)}


def check_freshness(daily, idx, st_value, entry_gap=ENTRY_GAP):
    """Check if signal is fresh (not a bounce from <2% zone)."""
    if idx < FRESHNESS_DAYS:
        return True
    for i in range(max(0, idx - FRESHNESS_DAYS), idx):
        p = daily[i]["close"]
        gap_pct = abs(p - st_value) / st_value * 100
        low_gap = abs(daily[i]["low"] - st_value) / st_value * 100
        if gap_pct < entry_gap or low_gap < entry_gap:
            return False
    return True


# ── Premium Estimation ───────────────────────────────────────────────────

def estimate_premium(atr, spot_price):
    """Estimate ATM option premium.

    Uses the higher of:
    - ATR-based: ATR(14) × 0.7 (volatility-driven)
    - Spot-based: 3% of spot (floor for low-vol stocks)

    Realistic for 15-30 DTE ATM options on F&O stocks.
    """
    atr_premium = (atr * 0.70) if atr and atr > 0 else 0
    spot_premium = spot_price * PREMIUM_PCT_OF_SPOT
    premium = max(atr_premium, spot_premium)
    return max(premium, 2.0)  # minimum Rs 2


def estimate_lot_size(spot_price):
    """Estimate F&O lot size for a stock at given price.

    Exchange lots target ~Rs 5-7L notional per lot.
    Returns qty for LOTS_PER_TRADE lots.
    """
    # Base lot: typical exchange lot giving ~Rs 5-7L notional
    if spot_price >= 5000:
        base_lot = max(25, round(600_000 / spot_price / 25) * 25)
    elif spot_price >= 1000:
        base_lot = max(50, round(500_000 / spot_price / 50) * 50)
    elif spot_price >= 500:
        base_lot = max(100, round(500_000 / spot_price / 100) * 100)
    elif spot_price >= 200:
        base_lot = max(200, round(500_000 / spot_price / 100) * 100)
    else:
        base_lot = max(500, round(500_000 / spot_price / 500) * 500)

    return base_lot * LOTS_PER_TRADE


# ── Signal Scanner ───────────────────────────────────────────────────────

def scan_all_signals(fo_stocks):
    """Scan all F&O stocks across D/W/M timeframes for the backtest period.

    Returns list of signals sorted by date, with all metadata needed for simulation.
    """
    all_signals = []
    total = len(fo_stocks)

    for idx_s, sym in enumerate(fo_stocks):
        if should_skip(sym):
            continue
        daily = load_daily(sym)
        if daily is None or len(daily) < 300:
            continue

        if (idx_s + 1) % 50 == 0:
            print(f"  Scanning {idx_s + 1}/{total}...")

        # Build daily index lookup
        date_to_idx = {c["date"]: i for i, c in enumerate(daily)}

        # ── MONTHLY signals ──────────────────────────────────────────────
        months_in_range = set()
        for c in daily:
            if BACKTEST_START <= c["date"] <= BACKTEST_END:
                months_in_range.add((c["date"].year, c["date"].month))

        for year, month in sorted(months_in_range):
            cutoff = date(year, month, 1)
            if cutoff < date(2020, 1, 1):
                continue
            monthly = aggregate_to_monthly(daily, exclude_after=cutoff)
            if len(monthly) < ST_PERIOD + 2:
                continue
            st_data = compute_supertrend(monthly, ST_PERIOD, ST_MULTIPLIER)
            if not st_data:
                continue
            last_st = st_data[-1]
            st_value = last_st["supertrend"]
            st_dir = last_st["direction"]

            month_days = [c for c in daily
                          if c["date"].year == year and c["date"].month == month
                          and BACKTEST_START <= c["date"] <= BACKTEST_END]

            signal_found = False
            for c in month_days:
                if signal_found:
                    break
                price = c["close"]
                if price < MIN_PRICE:
                    continue
                didx = date_to_idx.get(c["date"])
                if didx is None:
                    continue

                side = None
                gap = None
                if st_dir == "UP" and price > st_value:
                    gap = (price - st_value) / st_value * 100
                    side = "above"
                elif st_dir == "DOWN" and price < st_value:
                    gap = (st_value - price) / st_value * 100
                    side = "below"

                if side and gap and ENTRY_GAP <= gap <= SIGNAL_GAP_MAX_WM:
                    if not check_freshness(daily, didx, st_value):
                        continue
                    all_signals.append({
                        "date": c["date"], "symbol": sym, "tf": "M",
                        "side": side, "direction": "PE" if side == "above" else "CE",
                        "st_value": round(st_value, 2), "st_dir": st_dir,
                        "gap_pct": round(gap, 2), "signal_price": round(price, 2),
                        "daily_idx": didx,
                    })
                    signal_found = True

        # ── WEEKLY signals ───────────────────────────────────────────────
        weeks_in_range = set()
        for c in daily:
            if BACKTEST_START <= c["date"] <= BACKTEST_END:
                weeks_in_range.add(c["date"].isocalendar()[:2])

        for iso_year, iso_week in sorted(weeks_in_range):
            try:
                week_start = date.fromisocalendar(iso_year, iso_week, 1)
            except Exception:
                continue
            if week_start < date(2020, 6, 1):
                continue
            weekly = aggregate_to_weekly(daily, exclude_after=week_start)
            if len(weekly) < ST_PERIOD + 2:
                continue
            st_data = compute_supertrend(weekly, ST_PERIOD, ST_MULTIPLIER)
            if not st_data:
                continue
            last_st = st_data[-1]
            st_value = last_st["supertrend"]
            st_dir = last_st["direction"]

            week_days = [c for c in daily
                         if c["date"].isocalendar()[:2] == (iso_year, iso_week)
                         and BACKTEST_START <= c["date"] <= BACKTEST_END]
            if not week_days:
                continue

            c = week_days[0]  # first day of week
            price = c["close"]
            if price < MIN_PRICE:
                continue
            didx = date_to_idx.get(c["date"])
            if didx is None:
                continue

            side = None
            gap = None
            if st_dir == "UP" and price > st_value:
                gap = (price - st_value) / st_value * 100
                side = "above"
            elif st_dir == "DOWN" and price < st_value:
                gap = (st_value - price) / st_value * 100
                side = "below"

            if side and gap and ENTRY_GAP <= gap <= SIGNAL_GAP_MAX_WM:
                if not check_freshness(daily, didx, st_value):
                    continue
                all_signals.append({
                    "date": c["date"], "symbol": sym, "tf": "W",
                    "side": side, "direction": "PE" if side == "above" else "CE",
                    "st_value": round(st_value, 2), "st_dir": st_dir,
                    "gap_pct": round(gap, 2), "signal_price": round(price, 2),
                    "daily_idx": didx,
                })

        # ── DAILY signals (skipped — needs minute data for accuracy) ────
        # Daily TF excluded from backtest. Enable in production with live
        # minute-level monitoring where intraday entry/exit is accurate.

    # Sort by date
    all_signals.sort(key=lambda x: x["date"])
    return all_signals


# ── Trade Simulation ─────────────────────────────────────────────────────

def simulate_trade(signal, daily_data):
    """Simulate a single naked option trade following all exit rules.

    Returns trade dict or None if entry conditions not met.
    """
    entry_idx = signal["daily_idx"]
    sym = signal["symbol"]
    st_value = signal["st_value"]
    side = signal["side"]
    tf = signal["tf"]
    signal_price = signal["signal_price"]

    # Find entry day: first day after signal where gap <= 2%
    # For daily TF: entry happens intraday at the 2% gap level, not at close
    # For W/M: entry at close (approximation — overnight position)
    entry_day_idx = None
    entry_spot = None
    for i in range(entry_idx, min(entry_idx + 10, len(daily_data))):
        c = daily_data[i]
        close_gap = abs(c["close"] - st_value) / st_value * 100

        # Check if intraday range reached 2% gap zone
        if side == "above":
            # Price above ST, approaching from above → low is closest
            low_gap = (c["low"] - st_value) / st_value * 100 if c["low"] > st_value else 0
            intraday_hit = 0 < low_gap <= ENTRY_GAP
        else:
            # Price below ST, approaching from below → high is closest
            high_gap = (st_value - c["high"]) / st_value * 100 if c["high"] < st_value else 0
            intraday_hit = 0 < high_gap <= ENTRY_GAP

        if close_gap <= ENTRY_GAP or intraday_hit:
            entry_day_idx = i
            if tf == "D":
                # Daily: entry happens at the 2% gap level (mid-day trigger)
                if side == "above":
                    entry_spot = st_value * (1 + ENTRY_GAP / 100)  # 2% above ST
                else:
                    entry_spot = st_value * (1 - ENTRY_GAP / 100)  # 2% below ST
            else:
                # W/M: entry at close (overnight hold)
                entry_spot = c["close"]
            break

    if entry_day_idx is None:
        return None  # Never reached entry zone

    entry_date = daily_data[entry_day_idx]["date"]
    if entry_date > BACKTEST_END:
        return None

    # Compute ATR(14) at entry for premium estimation
    atr = compute_atr14(daily_data, entry_day_idx)
    if atr is None:
        atr = entry_spot * 0.03  # fallback

    # Estimate premium and apply entry slippage
    raw_premium = estimate_premium(atr, entry_spot)
    entry_premium = round(raw_premium * ENTRY_SLIPPAGE, 2)  # ASK + slippage

    # Lot size — 1 lot per trade (production config)
    qty = estimate_lot_size(entry_spot)

    # SL spot: 5% from ST
    if side == "above":
        sl_spot = st_value * (1 + SL_GAP / 100)
    else:
        sl_spot = st_value * (1 - SL_GAP / 100)

    # ── Simulate day-by-day ──────────────────────────────────────────────
    max_hold_days = SL_TIME_DAYS  # 5 biz days for all TFs
    peak_premium = entry_premium
    cost_sl_active = False
    cost_sl_level = entry_premium + 0.10
    hedged = False
    exit_premium = 0
    exit_reason = None
    exit_date = None
    exit_spot = None
    days_held = 0

    # For daily TF: check intraday on entry day itself, then EOD exit
    # For W/M: check subsequent days up to max_hold_days
    start_idx = entry_day_idx if tf == "D" else entry_day_idx + 1
    end_idx = min(entry_day_idx + max_hold_days + 5, len(daily_data))

    for i in range(start_idx, end_idx):
        c = daily_data[i]
        if c["date"] > BACKTEST_END:
            break

        price = c["close"]
        day_high = c["high"]
        day_low = c["low"]
        days_held = _biz_days(entry_date, c["date"])

        # For daily: skip if this is a future day (daily = intraday only)
        if tf == "D" and c["date"] > entry_date:
            break

        # Gap from ST
        gap = abs(price - st_value) / st_value * 100

        # ── Premium estimation: delta model with theta decay ──
        # Movement toward target
        if side == "above":
            # PUT: gains when spot declines toward ST
            favorable_move = entry_spot - price  # positive = good
            best_intraday = entry_spot - day_low  # intraday best for TP
        else:
            # CALL: gains when spot rallies toward ST
            favorable_move = price - entry_spot
            best_intraday = day_high - entry_spot

        # Delta: starts ~0.50 ATM, increases as ITM. Use 0.50 for simplicity.
        delta = 0.50
        prem_change = favorable_move * delta

        # Theta: ~2.5% per trading day (realistic for 15-20 DTE ATM options)
        # ZERO theta for daily (intraday trade, same-day entry/exit)
        if tf == "D":
            time_decay = 0  # intraday — negligible theta
        else:
            theta_per_day = entry_premium * 0.025
            time_decay = theta_per_day * days_held

        current_premium = max(0.05, entry_premium + prem_change - time_decay)

        # Track peak (using close-based premium)
        if current_premium > peak_premium:
            peak_premium = current_premium

        # === TP: spot wick crosses ST ===
        tp_hit = False
        if side == "above":
            tp_hit = day_low <= st_value
        else:
            tp_hit = day_high >= st_value

        if tp_hit:
            # At TP, the option captured the full gap move from entry to ST
            full_gap_move = abs(entry_spot - st_value)
            tp_premium = entry_premium + full_gap_move * delta - time_decay
            tp_premium = max(tp_premium, entry_premium * 0.80)  # floor: at least 80% retained
            exit_premium = round(tp_premium * EXIT_SLIPPAGE, 2)  # BID slippage
            exit_reason = "TP"
            exit_date = c["date"]
            exit_spot = price
            break

        # === COST SL: gap <= 1% → lock breakeven ===
        if not cost_sl_active and gap <= COST_SL_GAP:
            cost_sl_active = True

        # === TRAILING SL: 50% of peak gain (only when gain > 20% of entry) ===
        if peak_premium > entry_premium * 1.20:
            gain = peak_premium - entry_premium
            trail_level = entry_premium + gain * TRAIL_PCT
            effective_trail = max(trail_level, cost_sl_level if cost_sl_active else 0)
            if current_premium <= effective_trail:
                exit_premium = round(current_premium * EXIT_SLIPPAGE, 2)
                exit_reason = "TRAIL"
                exit_date = c["date"]
                exit_spot = price
                break

        # === COST SL: premium drops to cost ===
        if cost_sl_active and current_premium <= cost_sl_level:
            exit_premium = round(cost_sl_level * EXIT_SLIPPAGE, 2)
            exit_reason = "COST_SL"
            exit_date = c["date"]
            exit_spot = price
            break

        # === PREMIUM SL ===
        sl_pct = PREMIUM_SL_PCT_D if tf == "D" else PREMIUM_SL_PCT_WM
        prem_sl = entry_premium * (1 - sl_pct)
        if not cost_sl_active and not hedged and current_premium <= prem_sl:
            exit_premium = round(prem_sl * EXIT_SLIPPAGE, 2)
            exit_reason = "PREM_SL"
            exit_date = c["date"]
            exit_spot = price
            break

        # === SPOT SL: 5% gap ===
        spot_sl_hit = False
        if side == "above" and price >= sl_spot:
            spot_sl_hit = True
        elif side == "below" and price <= sl_spot:
            spot_sl_hit = True
        if spot_sl_hit:
            exit_premium = round(max(current_premium * 0.98, 0.05), 2)
            exit_reason = "SPOT_SL"
            exit_date = c["date"]
            exit_spot = price
            break

        # === TIME SL (W/M only — daily exits via EOD below) ===
        if tf != "D" and days_held >= max_hold_days:
            exit_premium = round(current_premium * EXIT_SLIPPAGE, 2)
            exit_reason = "TIME_SL"
            exit_date = c["date"]
            exit_spot = price
            break

    # === DAILY EOD EXIT: if no earlier exit, force close at EOD ===
    if tf == "D" and exit_date is None:
        c = daily_data[entry_day_idx]
        price = c["close"]
        # Entry was at 2% gap (mid-day). EOD = close. Delta-based P&L.
        if side == "above":
            # PUT: gains if close < entry_spot (moved toward ST)
            move = entry_spot - price
        else:
            # CALL: gains if close > entry_spot (moved toward ST)
            move = price - entry_spot
        # Pure delta move, no theta for same-day. Entry already has 2% slippage.
        eod_premium = max(0.05, entry_premium + move * 0.50)
        exit_premium = round(eod_premium * EXIT_SLIPPAGE, 2)  # BID slippage
        exit_reason = "EOD"
        exit_date = c["date"]
        exit_spot = round(price, 2)
        days_held = 0

    # If no exit found (data ended)
    if exit_date is None:
        if entry_day_idx + 1 < len(daily_data):
            last_c = daily_data[min(entry_day_idx + max_hold_days + 3, len(daily_data) - 1)]
            exit_date = last_c["date"]
            exit_spot = last_c["close"]
            exit_premium = round(max(entry_premium * 0.70, 0.05), 2)
            exit_reason = "DATA_END"
            days_held = _biz_days(entry_date, exit_date)
        else:
            return None

    # P&L calculation
    pnl_per_share = exit_premium - entry_premium
    pnl_rs = round(pnl_per_share * qty, 0)
    pnl_pct = round(pnl_per_share / entry_premium * 100, 1) if entry_premium > 0 else 0

    return {
        "symbol": sym,
        "tf": tf,
        "direction": signal["direction"],
        "side": side,
        "signal_date": signal["date"].isoformat(),
        "signal_price": signal["signal_price"],
        "signal_gap_pct": signal["gap_pct"],
        "st_value": st_value,
        "st_dir": signal["st_dir"],
        "entry_date": entry_date.isoformat(),
        "entry_spot": round(entry_spot, 2),
        "entry_premium": entry_premium,
        "qty": qty,
        "position_cost": round(entry_premium * qty, 0),
        "exit_date": exit_date.isoformat(),
        "exit_spot": round(exit_spot, 2),
        "exit_premium": exit_premium,
        "exit_reason": exit_reason,
        "days_held": days_held,
        "pnl_per_share": round(pnl_per_share, 2),
        "pnl_rs": pnl_rs,
        "pnl_pct": pnl_pct,
        "peak_premium": round(peak_premium, 2),
        "cost_sl_hit": cost_sl_active,
        "velocity": signal.get("velocity"),
        "momentum": signal.get("momentum"),
    }


def _biz_days(d1, d2):
    """Count business days between two dates."""
    if isinstance(d1, str):
        d1 = date.fromisoformat(d1)
    if isinstance(d2, str):
        d2 = date.fromisoformat(d2)
    count = 0
    d = d1 + timedelta(days=1)
    while d <= d2:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


# ── Main Backtest Engine ─────────────────────────────────────────────────

def run_backtest():
    print("=" * 72)
    print("  MAGNET STRATEGY BACKTEST — Full Trade Simulation (D/W/M)")
    print(f"  Period: {BACKTEST_START} to {BACKTEST_END}")
    print(f"  Capital: Rs {CAPITAL:,.0f} | Max positions: {MAX_POSITIONS}")
    print("=" * 72)

    fo_stocks = load_fo_stocks()
    if not fo_stocks:
        print("ERROR: No F&O stock list found!")
        return

    print(f"\n  F&O stocks: {len(fo_stocks)}")
    print("  Scanning signals across D/W/M timeframes...")

    signals = scan_all_signals(fo_stocks)
    print(f"\n  Total raw signals: {len(signals)}")

    # Dedup: min DEDUP_GAP_DAYS between signals for same stock+tf
    deduped = []
    last_seen = {}
    for sig in signals:
        key = (sig["symbol"], sig["tf"])
        if key in last_seen:
            if (sig["date"] - last_seen[key]).days < DEDUP_GAP_DAYS:
                continue
        last_seen[key] = sig["date"]
        deduped.append(sig)

    print(f"  After dedup: {len(deduped)} signals")

    # Simulate trades with position cap
    print("\n  Simulating trades with 7-position cap...")
    trades = []
    open_positions = []  # list of {exit_date, symbol}

    for sig in deduped:
        # Remove expired positions
        open_positions = [p for p in open_positions if p["exit_date"] >= sig["date"]]

        # Check capacity
        if len(open_positions) >= MAX_POSITIONS:
            continue

        # Check no duplicate stock in open positions
        if sig["symbol"] in {p["symbol"] for p in open_positions}:
            continue

        # Simulate
        daily = load_daily(sig["symbol"])
        if not daily:
            continue

        trade = simulate_trade(sig, daily)
        if trade is None:
            continue

        trades.append(trade)
        exit_dt = date.fromisoformat(trade["exit_date"])
        open_positions.append({"exit_date": exit_dt, "symbol": sig["symbol"]})

    print(f"  Completed trades: {len(trades)}")

    # Write CSV
    if trades:
        fieldnames = [
            "symbol", "tf", "direction", "side",
            "signal_date", "signal_price", "signal_gap_pct",
            "st_value", "st_dir",
            "entry_date", "entry_spot", "entry_premium",
            "qty", "position_cost",
            "exit_date", "exit_spot", "exit_premium", "exit_reason",
            "days_held", "pnl_per_share", "pnl_rs", "pnl_pct",
            "peak_premium", "cost_sl_hit",
            "velocity", "momentum",
        ]
        with open(OUTPUT_CSV, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for t in trades:
                writer.writerow(t)
        print(f"\n  CSV written: {OUTPUT_CSV}")

    # ── Print Summary ────────────────────────────────────────────────────
    print_summary(trades)


def print_summary(trades):
    """Print comprehensive backtest summary."""
    if not trades:
        print("\n  No trades to summarize.")
        return

    print(f"\n{'=' * 72}")
    print("  BACKTEST RESULTS")
    print(f"{'=' * 72}")

    total_pnl = sum(t["pnl_rs"] for t in trades)
    wins = [t for t in trades if t["pnl_rs"] > 0]
    losses = [t for t in trades if t["pnl_rs"] <= 0]

    print(f"\n  Total trades:   {len(trades)}")
    print(f"  Winners:        {len(wins)} ({len(wins)/len(trades)*100:.1f}%)")
    print(f"  Losers:         {len(losses)} ({len(losses)/len(trades)*100:.1f}%)")
    print(f"  Total P&L:      Rs {total_pnl:+,.0f}")
    print(f"  Avg P&L/trade:  Rs {total_pnl/len(trades):+,.0f}")
    print(f"  Avg days held:  {sum(t['days_held'] for t in trades)/len(trades):.1f}")

    if wins:
        print(f"  Avg win:        Rs {sum(t['pnl_rs'] for t in wins)/len(wins):+,.0f}")
    if losses:
        print(f"  Avg loss:       Rs {sum(t['pnl_rs'] for t in losses)/len(losses):+,.0f}")

    best = max(trades, key=lambda t: t["pnl_rs"])
    worst = min(trades, key=lambda t: t["pnl_rs"])
    print(f"\n  Best trade:     {best['symbol']} [{best['tf']}] Rs {best['pnl_rs']:+,.0f} ({best['exit_reason']})")
    print(f"  Worst trade:    {worst['symbol']} [{worst['tf']}] Rs {worst['pnl_rs']:+,.0f} ({worst['exit_reason']})")

    # By timeframe
    print(f"\n  {'TF':<4} {'#':>5} {'Win%':>6} {'AvgPnL':>10} {'TotalPnL':>12} {'AvgDays':>8}")
    print(f"  {'-' * 50}")
    for tf in ["M", "W", "D"]:
        subset = [t for t in trades if t["tf"] == tf]
        if not subset:
            continue
        n = len(subset)
        w = len([t for t in subset if t["pnl_rs"] > 0])
        pnl = sum(t["pnl_rs"] for t in subset)
        avg = pnl / n
        days = sum(t["days_held"] for t in subset) / n
        print(f"  {tf:<4} {n:>5} {w/n*100:>5.1f}% {avg:>+10,.0f} {pnl:>+12,.0f} {days:>7.1f}")

    # By exit reason
    print(f"\n  {'Exit':>10} {'#':>5} {'Win%':>6} {'AvgPnL%':>8} {'TotalRs':>12}")
    print(f"  {'-' * 45}")
    reasons = sorted(set(t["exit_reason"] for t in trades))
    for r in reasons:
        subset = [t for t in trades if t["exit_reason"] == r]
        n = len(subset)
        w = len([t for t in subset if t["pnl_rs"] > 0])
        pnl = sum(t["pnl_rs"] for t in subset)
        avg_pct = sum(t["pnl_pct"] for t in subset) / n
        print(f"  {r:>10} {n:>5} {w/n*100:>5.1f}% {avg_pct:>+7.1f}% {pnl:>+12,.0f}")

    # By direction
    print(f"\n  {'Dir':<4} {'#':>5} {'Win%':>6} {'TotalPnL':>12}")
    print(f"  {'-' * 30}")
    for d in ["CE", "PE"]:
        subset = [t for t in trades if t["direction"] == d]
        if not subset:
            continue
        n = len(subset)
        w = len([t for t in subset if t["pnl_rs"] > 0])
        pnl = sum(t["pnl_rs"] for t in subset)
        print(f"  {d:<4} {n:>5} {w/n*100:>5.1f}% {pnl:>+12,.0f}")

    # Monthly P&L breakdown
    print(f"\n  MONTHLY P&L BREAKDOWN")
    print(f"  {'Month':>8} {'#':>4} {'W':>3} {'L':>3} {'PnL':>12} {'Cumul':>12}")
    print(f"  {'-' * 40}")
    months = defaultdict(list)
    for t in trades:
        m = t["entry_date"][:7]  # YYYY-MM
        months[m].append(t)

    cumul = 0
    for m in sorted(months.keys()):
        subset = months[m]
        n = len(subset)
        w = len([t for t in subset if t["pnl_rs"] > 0])
        l = n - w
        pnl = sum(t["pnl_rs"] for t in subset)
        cumul += pnl
        print(f"  {m:>8} {n:>4} {w:>3} {l:>3} {pnl:>+12,.0f} {cumul:>+12,.0f}")

    # W/M only summary (daily simulation is unreliable with daily OHLC)
    wm_trades = [t for t in trades if t["tf"] in ("M", "W")]
    if wm_trades:
        wm_pnl = sum(t["pnl_rs"] for t in wm_trades)
        wm_wins = len([t for t in wm_trades if t["pnl_rs"] > 0])
        print(f"\n  === W+M ONLY (daily excluded — needs minute data for accuracy) ===")
        print(f"  Trades: {len(wm_trades)} | Win%: {wm_wins/len(wm_trades)*100:.1f}%"
              f" | Total P&L: Rs {wm_pnl:+,.0f}"
              f" | Avg: Rs {wm_pnl/len(wm_trades):+,.0f}/trade")

        # W/M capital efficiency
        avg_cost_wm = sum(t.get("position_cost", 0) for t in wm_trades) / len(wm_trades)
        if avg_cost_wm > 0:
            roi = wm_pnl / (avg_cost_wm * len(wm_trades)) * 100
            print(f"  Avg position cost: Rs {avg_cost_wm:,.0f} | ROI on deployed: {roi:+.1f}%")

    # Top 10 trades
    print(f"\n  TOP 10 TRADES (by P&L)")
    print(f"  {'Symbol':<15} {'TF':<3} {'Dir':<3} {'Entry':>10} {'Exit':>10} {'PnL Rs':>10} {'Reason':<10}")
    print(f"  {'-' * 65}")
    sorted_trades = sorted(trades, key=lambda t: t["pnl_rs"], reverse=True)
    for t in sorted_trades[:10]:
        print(f"  {t['symbol']:<15} {t['tf']:<3} {t['direction']:<3} "
              f"{t['entry_date']:>10} {t['exit_date']:>10} "
              f"{t['pnl_rs']:>+10,.0f} {t['exit_reason']:<10}")

    # Bottom 10
    print(f"\n  BOTTOM 10 TRADES (worst losses)")
    print(f"  {'Symbol':<15} {'TF':<3} {'Dir':<3} {'Entry':>10} {'Exit':>10} {'PnL Rs':>10} {'Reason':<10}")
    print(f"  {'-' * 65}")
    for t in sorted_trades[-10:]:
        print(f"  {t['symbol']:<15} {t['tf']:<3} {t['direction']:<3} "
              f"{t['entry_date']:>10} {t['exit_date']:>10} "
              f"{t['pnl_rs']:>+10,.0f} {t['exit_reason']:<10}")


if __name__ == "__main__":
    run_backtest()
