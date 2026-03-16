"""
Regime Velocity Analysis
========================
Computes velocity (rate of change) of the 3 regime signals:
  1. RSI(14) velocity — how fast momentum is shifting
  2. Breadth velocity — how fast stocks are flipping above/below 50DMA
  3. DMA gap velocity — how fast NIFTY is approaching/departing its 50DMA

Analyzes patterns around known regime transitions to see if velocity
adds predictive signal beyond binary ON/OFF.

Usage:
    python regime_velocity.py                # Full analysis
    python regime_velocity.py --window 5     # 5-day velocity window (default)
    python regime_velocity.py --window 3     # 3-day velocity window
"""

import json
import os
import sys
import argparse
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "backtest_cache"
NIFTY_FILE = CACHE_DIR / "_nifty50_daily.json"

# Regime thresholds (same as backtest_v4_1.py)
RSI_PERIOD = 14
RSI_THRESHOLD = 50
BREADTH_THRESHOLD = 40
SMA_PERIOD = 50
UNPAUSE_DAYS = 7


# =========================================================================
# Reused computation functions (from backtest_v4_1.py)
# =========================================================================
def load_nifty():
    with open(NIFTY_FILE) as f:
        raw = json.load(f)
    parsed = []
    for c in raw:
        d = c["date"]
        if isinstance(d, str):
            d = datetime.fromisoformat(d.replace("+05:30", "")).date()
        parsed.append({
            "date": d,
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
        })
    parsed.sort(key=lambda x: x["date"])
    return parsed


def compute_rsi(data, period=14):
    r = [None] * len(data)
    ag = al = 0.0
    for i in range(1, len(data)):
        ch = data[i]["close"] - data[i - 1]["close"]
        g, l = max(ch, 0), abs(min(ch, 0))
        if i < period:
            continue
        if i == period:
            ag = sum(max(data[j]["close"] - data[j - 1]["close"], 0)
                     for j in range(1, period + 1)) / period
            al = sum(abs(min(data[j]["close"] - data[j - 1]["close"], 0))
                     for j in range(1, period + 1)) / period
        else:
            ag = (ag * (period - 1) + g) / period
            al = (al * (period - 1) + l) / period
        r[i] = round(100 - 100 / (1 + ag / al), 2) if al > 0 else 100.0
    return r


def compute_sma(data, period):
    r = [None] * len(data)
    for i in range(period - 1, len(data)):
        r[i] = sum(data[j]["close"] for j in range(i - period + 1, i + 1)) / period
    return r


def compute_breadth(cache_dir, min_date=None):
    stock_files = [f for f in os.listdir(cache_dir)
                   if f.endswith(".json") and not f.startswith("_")]
    if not min_date:
        min_date = date(2020, 1, 1)
    counts = defaultdict(lambda: {"above": 0, "total": 0})
    for sf in stock_files:
        try:
            with open(os.path.join(cache_dir, sf)) as f:
                sd = json.load(f)
        except Exception:
            continue
        parsed = []
        for c in sd:
            d = c["date"]
            if isinstance(d, str):
                d = datetime.fromisoformat(d.replace("+05:30", "")).date()
            parsed.append({"date": d, "close": float(c["close"])})
        parsed.sort(key=lambda x: x["date"])
        for i in range(50, len(parsed)):
            if parsed[i]["date"] < min_date:
                continue
            sma50 = sum(parsed[j]["close"] for j in range(i - 49, i + 1)) / 50
            counts[parsed[i]["date"]]["total"] += 1
            if parsed[i]["close"] > sma50:
                counts[parsed[i]["date"]]["above"] += 1
    breadth = {}
    for dt in sorted(counts):
        d = counts[dt]
        if d["total"] > 50:
            breadth[dt] = round(d["above"] / d["total"] * 100, 1)
    return breadth


def build_regime_timeline(nifty, rsi_vals, sma_vals, breadth):
    regime = {}
    state = "UNPAUSED"
    consecutive_2of3 = 0
    for i in range(len(nifty)):
        dt = nifty[i]["date"]
        r = rsi_vals[i]
        s = sma_vals[i]
        close = nifty[i]["close"]
        br = breadth.get(dt, None)
        if r is None or s is None or br is None:
            regime[dt] = state
            continue
        sig_count = ((1 if r > RSI_THRESHOLD else 0) +
                     (1 if br > BREADTH_THRESHOLD else 0) +
                     (1 if close > s else 0))
        if state == "UNPAUSED":
            if sig_count == 0:
                state = "PAUSED"
                consecutive_2of3 = 0
        else:
            if sig_count >= 2:
                consecutive_2of3 += 1
                if consecutive_2of3 >= UNPAUSE_DAYS:
                    state = "UNPAUSED"
                    consecutive_2of3 = 0
            else:
                consecutive_2of3 = 0
        regime[dt] = state
    return regime


# =========================================================================
# Velocity computation
# =========================================================================
def compute_velocities(nifty, rsi_vals, sma_vals, breadth, window=5):
    """Compute velocity (rate of change over `window` days) for each signal.

    Returns list of dicts (one per trading day) with:
      date, close, rsi, breadth, dma_gap_pct,
      rsi_vel, breadth_vel, dma_gap_vel,
      composite_vel, sig_count, regime
    """
    rows = []
    for i in range(len(nifty)):
        dt = nifty[i]["date"]
        close = nifty[i]["close"]
        r = rsi_vals[i]
        s = sma_vals[i]
        br = breadth.get(dt, None)

        if r is None or s is None or br is None:
            continue

        dma_gap_pct = round((close / s - 1) * 100, 2)  # % above/below 50DMA

        sig_count = ((1 if r > RSI_THRESHOLD else 0) +
                     (1 if br > BREADTH_THRESHOLD else 0) +
                     (1 if close > s else 0))

        rows.append({
            "idx": i,
            "date": dt,
            "close": close,
            "rsi": r,
            "breadth": br,
            "dma_gap_pct": dma_gap_pct,
            "sma50": round(s, 2),
            "sig_count": sig_count,
            # velocities filled below
            "rsi_vel": None,
            "breadth_vel": None,
            "dma_gap_vel": None,
            "composite_vel": None,
        })

    # Compute velocities as deltas over `window` trading days
    for j in range(window, len(rows)):
        rows[j]["rsi_vel"] = round(rows[j]["rsi"] - rows[j - window]["rsi"], 2)
        rows[j]["breadth_vel"] = round(rows[j]["breadth"] - rows[j - window]["breadth"], 1)
        rows[j]["dma_gap_vel"] = round(rows[j]["dma_gap_pct"] - rows[j - window]["dma_gap_pct"], 2)

        # Composite: normalize each velocity to comparable scale
        # RSI moves ~0-100 (typical velocity -20 to +20)
        # Breadth moves ~0-100% (typical velocity -15 to +15)
        # DMA gap moves ~-15% to +15% (typical velocity -5 to +5)
        rv = rows[j]["rsi_vel"] / 10.0       # scale to ~±2
        bv = rows[j]["breadth_vel"] / 10.0   # scale to ~±1.5
        dv = rows[j]["dma_gap_vel"] / 2.0    # scale to ~±2.5
        rows[j]["composite_vel"] = round(rv + bv + dv, 2)

    return rows


# =========================================================================
# Analysis functions
# =========================================================================
def find_regime_transitions(regime_timeline):
    """Find dates where regime flips PAUSED->UNPAUSED or UNPAUSED->PAUSED."""
    dates = sorted(regime_timeline.keys())
    transitions = []
    for i in range(1, len(dates)):
        prev = regime_timeline[dates[i - 1]]
        curr = regime_timeline[dates[i]]
        if prev != curr:
            transitions.append({
                "date": dates[i],
                "from": prev,
                "to": curr,
            })
    return transitions


def analyze_transition_velocity(vel_rows, transition_date, lookback=20, lookforward=10):
    """Get velocity data around a regime transition."""
    # Build date index
    date_idx = {r["date"]: i for i, r in enumerate(vel_rows)}
    if transition_date not in date_idx:
        # Find nearest
        for delta in range(5):
            for d in [transition_date + timedelta(days=delta),
                      transition_date - timedelta(days=delta)]:
                if d in date_idx:
                    transition_date = d
                    break
            if transition_date in date_idx:
                break
    if transition_date not in date_idx:
        return None

    idx = date_idx[transition_date]
    start = max(0, idx - lookback)
    end = min(len(vel_rows), idx + lookforward + 1)
    return vel_rows[start:end], idx - start  # data, pivot_index


def print_section(title):
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")


def print_transition_table(data, pivot_idx, transition):
    """Print velocity data around a transition."""
    print(f"\n  {transition['from']} -> {transition['to']} on {transition['date']}")
    print(f"  {'Day':>4}  {'Date':>12}  {'Close':>8}  {'RSI':>6}  {'Brth%':>6}  "
          f"{'DMA%':>7}  {'RSIv':>6}  {'Brthv':>6}  {'DMAv':>7}  {'CompV':>6}  {'Sig':>3}")
    print(f"  {'----':>4}  {'----':>12}  {'-----':>8}  {'---':>6}  {'-----':>6}  "
          f"{'----':>7}  {'----':>6}  {'-----':>6}  {'----':>7}  {'-----':>6}  {'---':>3}")
    for j, row in enumerate(data):
        day_label = j - pivot_idx
        marker = " <<" if j == pivot_idx else ""
        rv = f"{row['rsi_vel']:+.1f}" if row['rsi_vel'] is not None else "  n/a"
        bv = f"{row['breadth_vel']:+.1f}" if row['breadth_vel'] is not None else "  n/a"
        dv = f"{row['dma_gap_vel']:+.2f}" if row['dma_gap_vel'] is not None else "   n/a"
        cv = f"{row['composite_vel']:+.2f}" if row['composite_vel'] is not None else "   n/a"
        print(f"  {day_label:+4d}  {row['date'].isoformat():>12}  {row['close']:>8.1f}  "
              f"{row['rsi']:>6.1f}  {row['breadth']:>6.1f}  {row['dma_gap_pct']:>+7.2f}  "
              f"{rv:>6}  {bv:>6}  {dv:>7}  {cv:>6}  {row['sig_count']:>3}{marker}")


def velocity_distribution(vel_rows, regime_timeline):
    """Show velocity distribution for PAUSED vs UNPAUSED periods."""
    paused_cv = []
    unpaused_cv = []
    for row in vel_rows:
        if row["composite_vel"] is None:
            continue
        state = regime_timeline.get(row["date"], "UNPAUSED")
        if state == "PAUSED":
            paused_cv.append(row["composite_vel"])
        else:
            unpaused_cv.append(row["composite_vel"])
    return paused_cv, unpaused_cv


def percentile(data, pct):
    if not data:
        return 0
    s = sorted(data)
    k = (len(s) - 1) * pct / 100
    f = int(k)
    c = f + 1
    if c >= len(s):
        return s[f]
    return s[f] + (k - f) * (s[c] - s[f])


def early_warning_analysis(vel_rows, regime_timeline):
    """Check: could velocity have given earlier warning of PAUSE?
    Look for composite velocity dropping below threshold BEFORE all 3 go OFF."""
    transitions = find_regime_transitions(regime_timeline)
    pause_transitions = [t for t in transitions if t["to"] == "PAUSED"]

    date_idx = {r["date"]: i for i, r in enumerate(vel_rows)}

    print("\n  EARLY WARNING: Velocity signal BEFORE regime PAUSED")
    print(f"  {'Pause Date':>12}  {'Days Early':>10}  {'CompV at Warning':>16}  {'Threshold':>10}")
    print(f"  {'----------':>12}  {'----------':>10}  {'----------------':>16}  {'---------':>10}")

    thresholds = [-2.0, -3.0, -4.0]
    for t in pause_transitions:
        if t["date"] not in date_idx:
            continue
        pause_idx = date_idx[t["date"]]
        # Look back up to 30 days for velocity crossing threshold
        for thresh in thresholds:
            earliest_warning = None
            for lookback in range(1, min(31, pause_idx)):
                row = vel_rows[pause_idx - lookback]
                if row["composite_vel"] is not None and row["composite_vel"] <= thresh:
                    earliest_warning = (lookback, row["composite_vel"], row["date"])
                else:
                    break  # want consecutive, so stop when it breaks
            if earliest_warning:
                days, cv, warn_date = earliest_warning
                print(f"  {t['date'].isoformat():>12}  {days:>10d}  {cv:>+16.2f}  {thresh:>+10.1f}")


def recovery_velocity_analysis(vel_rows, regime_timeline):
    """Analyze velocity patterns during recoveries (PAUSED -> UNPAUSED).
    Key question: do faster recoveries correlate with better trade outcomes?"""
    transitions = find_regime_transitions(regime_timeline)
    unpause_transitions = [t for t in transitions if t["to"] == "UNPAUSED"]

    date_idx = {r["date"]: i for i, r in enumerate(vel_rows)}

    print("\n  RECOVERY VELOCITY: Speed of regime recovery")
    print(f"  {'Unpause':>12}  {'Peak CompV':>10}  {'Peak RSIv':>9}  {'Peak Brthv':>10}  "
          f"{'Days Pause':>10}  {'NIFTY at Unpause':>16}")
    print(f"  {'-------':>12}  {'----------':>10}  {'---------':>9}  {'----------':>10}  "
          f"{'----------':>10}  {'----------------':>16}")

    for t in unpause_transitions:
        if t["date"] not in date_idx:
            continue
        unpause_idx = date_idx[t["date"]]

        # Find matching pause start
        pause_start = None
        for t2 in transitions:
            if t2["to"] == "PAUSED" and t2["date"] < t["date"]:
                pause_start = t2["date"]

        pause_days = (t["date"] - pause_start).days if pause_start else 0

        # Peak velocities in the 15 days leading to unpause
        peak_cv = peak_rv = peak_bv = -999
        for lb in range(0, min(16, unpause_idx)):
            row = vel_rows[unpause_idx - lb]
            if row["composite_vel"] is not None:
                peak_cv = max(peak_cv, row["composite_vel"])
            if row["rsi_vel"] is not None:
                peak_rv = max(peak_rv, row["rsi_vel"])
            if row["breadth_vel"] is not None:
                peak_bv = max(peak_bv, row["breadth_vel"])

        nifty_close = vel_rows[unpause_idx]["close"]
        print(f"  {t['date'].isoformat():>12}  {peak_cv:>+10.2f}  {peak_rv:>+9.1f}  "
              f"{peak_bv:>+10.1f}  {pause_days:>10d}  {nifty_close:>16.1f}")


def signal_convergence_analysis(vel_rows):
    """Track how signals cross their thresholds — together or sequentially?
    When multiple signals cross within a short window = strong move."""
    print("\n  SIGNAL CROSSINGS: When individual signals cross thresholds")
    print(f"  {'Date':>12}  {'Signal':>8}  {'Direction':>10}  {'Value':>8}  "
          f"{'CompV':>7}  {'Days Since Last Cross':>22}")

    crossings = []
    prev = {"rsi_on": False, "breadth_on": False, "dma_on": False}
    for row in vel_rows:
        curr_rsi_on = row["rsi"] > RSI_THRESHOLD
        curr_breadth_on = row["breadth"] > BREADTH_THRESHOLD
        curr_dma_on = row["dma_gap_pct"] > 0

        if curr_rsi_on != prev["rsi_on"]:
            crossings.append({
                "date": row["date"], "signal": "RSI",
                "direction": "ON" if curr_rsi_on else "OFF",
                "value": row["rsi"],
                "composite_vel": row["composite_vel"],
            })
        if curr_breadth_on != prev["breadth_on"]:
            crossings.append({
                "date": row["date"], "signal": "Breadth",
                "direction": "ON" if curr_breadth_on else "OFF",
                "value": row["breadth"],
                "composite_vel": row["composite_vel"],
            })
        if curr_dma_on != prev["dma_on"]:
            crossings.append({
                "date": row["date"], "signal": "DMA",
                "direction": "ON" if curr_dma_on else "OFF",
                "value": row["dma_gap_pct"],
                "composite_vel": row["composite_vel"],
            })

        prev = {"rsi_on": curr_rsi_on, "breadth_on": curr_breadth_on, "dma_on": curr_dma_on}

    # Print with days-between
    for i, cr in enumerate(crossings):
        days_gap = (cr["date"] - crossings[i - 1]["date"]).days if i > 0 else 0
        cv = f"{cr['composite_vel']:+.2f}" if cr['composite_vel'] is not None else "  n/a"
        print(f"  {cr['date'].isoformat():>12}  {cr['signal']:>8}  {cr['direction']:>10}  "
              f"{cr['value']:>8.1f}  {cv:>7}  {days_gap:>22d}")

    # Find clusters: 3 signals crossing same direction within 5 days
    print("\n  CONVERGENCE CLUSTERS: 3 signals crossing same direction within 5 trading days")
    on_crossings = [c for c in crossings if c["direction"] == "ON"]
    off_crossings = [c for c in crossings if c["direction"] == "OFF"]

    for label, clist in [("ON clusters", on_crossings), ("OFF clusters", off_crossings)]:
        clusters = []
        i = 0
        while i < len(clist) - 2:
            window = clist[i:i + 3]
            if len(set(c["signal"] for c in window)) == 3:  # all 3 different signals
                span = (window[2]["date"] - window[0]["date"]).days
                if span <= 10:  # within ~5 trading days
                    clusters.append({
                        "start": window[0]["date"],
                        "end": window[2]["date"],
                        "span_days": span,
                        "signals": [c["signal"] for c in window],
                    })
                    i += 3
                    continue
            i += 1

        if clusters:
            print(f"\n  {label}:")
            for cl in clusters:
                print(f"    {cl['start']} to {cl['end']} ({cl['span_days']}d span) "
                      f"— {' -> '.join(cl['signals'])}")
        else:
            print(f"\n  {label}: None found")


def velocity_extremes(vel_rows):
    """Find days with extreme velocity — potential regime change predictors."""
    print("\n  EXTREME VELOCITY DAYS (|composite_vel| > 4.0)")
    print(f"  {'Date':>12}  {'Close':>8}  {'RSI':>6}  {'Brth%':>6}  "
          f"{'CompV':>7}  {'RSIv':>6}  {'Brthv':>6}  {'Sig':>3}")
    print(f"  {'----':>12}  {'-----':>8}  {'---':>6}  {'-----':>6}  "
          f"{'-----':>7}  {'----':>6}  {'-----':>6}  {'---':>3}")

    extremes = [r for r in vel_rows if r["composite_vel"] is not None
                and abs(r["composite_vel"]) > 4.0]
    for row in extremes:
        print(f"  {row['date'].isoformat():>12}  {row['close']:>8.1f}  {row['rsi']:>6.1f}  "
              f"{row['breadth']:>6.1f}  {row['composite_vel']:>+7.2f}  "
              f"{row['rsi_vel']:>+6.1f}  {row['breadth_vel']:>+6.1f}  {row['sig_count']:>3}")


def current_velocity_status(vel_rows, regime_timeline):
    """Show current velocity status — where are we now?"""
    last = vel_rows[-1]
    prev5 = vel_rows[-6] if len(vel_rows) > 5 else vel_rows[0]
    prev10 = vel_rows[-11] if len(vel_rows) > 10 else vel_rows[0]

    state = regime_timeline.get(last["date"], "UNKNOWN")

    print(f"\n  CURRENT STATUS ({last['date'].isoformat()}):")
    print(f"  Regime: {state}")
    print(f"  NIFTY:  {last['close']:.1f}  (50DMA: {last['sma50']:.1f}, gap: {last['dma_gap_pct']:+.2f}%)")
    print(f"  RSI:    {last['rsi']:.1f}  (threshold: {RSI_THRESHOLD})")
    print(f"  Breadth:{last['breadth']:.1f}%  (threshold: {BREADTH_THRESHOLD}%)")
    print(f"  Sig ON: {last['sig_count']}/3")
    print()
    print(f"  Velocities (last {5} days):")
    cv = last["composite_vel"]
    print(f"    RSI velocity:     {last['rsi_vel']:+.1f}" if last['rsi_vel'] is not None else "    RSI velocity:     n/a")
    print(f"    Breadth velocity: {last['breadth_vel']:+.1f}" if last['breadth_vel'] is not None else "    Breadth velocity: n/a")
    print(f"    DMA gap velocity: {last['dma_gap_vel']:+.2f}" if last['dma_gap_vel'] is not None else "    DMA gap velocity: n/a")
    print(f"    Composite:        {cv:+.2f}" if cv is not None else "    Composite:        n/a")

    # Distance to thresholds
    rsi_dist = RSI_THRESHOLD - last["rsi"]
    breadth_dist = BREADTH_THRESHOLD - last["breadth"]
    dma_dist = -last["dma_gap_pct"]  # positive = below 50DMA

    print(f"\n  Distance to thresholds:")
    print(f"    RSI needs:     {rsi_dist:+.1f} pts  (currently {last['rsi']:.1f}, need >{RSI_THRESHOLD})")
    print(f"    Breadth needs: {breadth_dist:+.1f} ppts (currently {last['breadth']:.1f}%, need >{BREADTH_THRESHOLD}%)")
    print(f"    DMA gap:       {dma_dist:+.2f}%  (currently {last['dma_gap_pct']:+.2f}%, need >0)")

    # At current velocity, ETA to thresholds
    if last["rsi_vel"] is not None and last["rsi_vel"] > 0 and rsi_dist > 0:
        eta_rsi = rsi_dist / last["rsi_vel"] * 5  # trading days
        print(f"    RSI ETA:       ~{eta_rsi:.0f} trading days at current velocity")
    elif rsi_dist <= 0:
        print(f"    RSI:           ALREADY ABOVE threshold")
    else:
        print(f"    RSI:           Moving AWAY (velocity negative)")

    if last["breadth_vel"] is not None and last["breadth_vel"] > 0 and breadth_dist > 0:
        eta_b = breadth_dist / last["breadth_vel"] * 5
        print(f"    Breadth ETA:   ~{eta_b:.0f} trading days at current velocity")
    elif breadth_dist <= 0:
        print(f"    Breadth:       ALREADY ABOVE threshold")
    else:
        print(f"    Breadth:       Moving AWAY (velocity negative)")


# =========================================================================
# Main
# =========================================================================
def main():
    parser = argparse.ArgumentParser(description="Regime velocity analysis")
    parser.add_argument("--window", type=int, default=5,
                        help="Velocity window in trading days (default: 5)")
    parser.add_argument("--from-date", type=str, default="2020-01-01",
                        help="Start date for analysis (default: 2020-01-01)")
    args = parser.parse_args()

    print("Loading NIFTY data...")
    nifty = load_nifty()
    print(f"  {len(nifty)} daily candles ({nifty[0]['date']} to {nifty[-1]['date']})")

    print("Computing RSI(14)...")
    rsi_vals = compute_rsi(nifty, RSI_PERIOD)

    print("Computing 50DMA...")
    sma_vals = compute_sma(nifty, SMA_PERIOD)

    print("Computing breadth (scanning 800+ stocks)...")
    breadth = compute_breadth(str(CACHE_DIR), min_date=date.fromisoformat(args.from_date))
    print(f"  Breadth data: {min(breadth.keys())} to {max(breadth.keys())} ({len(breadth)} days)")

    print("Building regime timeline...")
    regime = build_regime_timeline(nifty, rsi_vals, sma_vals, breadth)

    print(f"Computing velocities (window={args.window})...")
    vel_rows = compute_velocities(nifty, rsi_vals, sma_vals, breadth, window=args.window)
    # Filter to analysis period
    from_dt = date.fromisoformat(args.from_date)
    vel_rows = [r for r in vel_rows if r["date"] >= from_dt]
    print(f"  {len(vel_rows)} velocity data points")

    # =====================================================================
    # Section 1: Current Status
    # =====================================================================
    print_section("1. CURRENT VELOCITY STATUS")
    current_velocity_status(vel_rows, regime)

    # =====================================================================
    # Section 2: Velocity around regime transitions
    # =====================================================================
    print_section("2. VELOCITY AROUND REGIME TRANSITIONS")
    transitions = find_regime_transitions(regime)
    transitions = [t for t in transitions if t["date"] >= from_dt]
    print(f"\n  Found {len(transitions)} regime transitions since {args.from_date}")

    for t in transitions:
        result = analyze_transition_velocity(vel_rows, t["date"], lookback=15, lookforward=10)
        if result:
            data, pivot = result
            print_transition_table(data, pivot, t)

    # =====================================================================
    # Section 3: Velocity distribution PAUSED vs UNPAUSED
    # =====================================================================
    print_section("3. VELOCITY DISTRIBUTION")
    paused_cv, unpaused_cv = velocity_distribution(vel_rows, regime)
    print(f"\n  PAUSED periods ({len(paused_cv)} days):")
    if paused_cv:
        print(f"    Composite velocity: min={min(paused_cv):+.2f}  "
              f"p25={percentile(paused_cv, 25):+.2f}  "
              f"median={percentile(paused_cv, 50):+.2f}  "
              f"p75={percentile(paused_cv, 75):+.2f}  "
              f"max={max(paused_cv):+.2f}")
        avg = sum(paused_cv) / len(paused_cv)
        print(f"    Mean: {avg:+.2f}")

    print(f"\n  UNPAUSED periods ({len(unpaused_cv)} days):")
    if unpaused_cv:
        print(f"    Composite velocity: min={min(unpaused_cv):+.2f}  "
              f"p25={percentile(unpaused_cv, 25):+.2f}  "
              f"median={percentile(unpaused_cv, 50):+.2f}  "
              f"p75={percentile(unpaused_cv, 75):+.2f}  "
              f"max={max(unpaused_cv):+.2f}")
        avg = sum(unpaused_cv) / len(unpaused_cv)
        print(f"    Mean: {avg:+.2f}")

    # =====================================================================
    # Section 4: Early warning analysis
    # =====================================================================
    print_section("4. EARLY WARNING (velocity drop before PAUSE)")
    early_warning_analysis(vel_rows, regime)

    # =====================================================================
    # Section 5: Recovery velocity
    # =====================================================================
    print_section("5. RECOVERY VELOCITY (speed of recovery before UNPAUSE)")
    recovery_velocity_analysis(vel_rows, regime)

    # =====================================================================
    # Section 6: Signal convergence
    # =====================================================================
    print_section("6. SIGNAL CONVERGENCE (how signals cross thresholds)")
    signal_convergence_analysis(vel_rows)

    # =====================================================================
    # Section 7: Extreme velocity days
    # =====================================================================
    print_section("7. EXTREME VELOCITY DAYS")
    velocity_extremes(vel_rows)

    print(f"\n{'=' * 70}")
    print("  Analysis complete.")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
