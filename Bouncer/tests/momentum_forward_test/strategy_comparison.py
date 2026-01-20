#!/usr/bin/env python3
"""
Strategy Comparison Test - Multiple Variations

Tests different strategy modifications against baseline:
1. BASELINE: Original 3 HH-HL pattern on options
2. INDEX_PATTERN: Detect pattern on INDEX, trade options
3. PULLBACK: Wait for pullback before entry
4. TREND_FILTER: Only trade with trend (EMA)
5. TIME_FILTER: Skip first/last 30 mins
6. PROFIT_TARGET: Add fixed 1.5:1 target
7. COMBINED: All improvements together

Usage:
    python tests/momentum_forward_test/strategy_comparison.py
"""

import json
import csv
from pathlib import Path
from datetime import datetime, timedelta, time as dt_time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict
import sys

# Paths
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
BOTS_DIR = PROJECT_DIR.parent
RESULTS_DIR = SCRIPT_DIR / 'results'
TOKEN_FILE = BOTS_DIR / 'data' / 'kite_access_token.json'

RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# Add scripts to path
sys.path.insert(0, str(PROJECT_DIR / 'scripts'))

from kiteconnect import KiteConnect
from kite_instruments import fetch_index_options, INDEX_CONFIG, get_otm_strikes, get_atm_strike

# =============================================================================
# CONFIGURATION
# =============================================================================
PATTERN_CANDLES = 3
OTM_POSITIONS = [2, 3]

# Strategy variations to test
STRATEGIES = [
    'BASELINE',        # Original logic
    'INDEX_PATTERN',   # Pattern on index, not options
    'PULLBACK',        # Wait for pullback entry
    'TREND_FILTER',    # EMA trend alignment
    'TIME_FILTER',     # Skip volatile periods
    'PROFIT_TARGET',   # Fixed 1.5:1 R:R target
    'ATM_STRIKE',      # Use ATM instead of 2nd/3rd OTM
    'COMBINED',        # All improvements
]


# =============================================================================
# DATA CLASSES
# =============================================================================
@dataclass
class Trade:
    id: str
    strategy: str
    index: str
    option_symbol: str
    option_type: str
    strike: float

    entry_time: datetime
    entry_price: float
    initial_sl: float
    current_sl: float
    target_price: Optional[float] = None

    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    status: str = 'open'

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return self.exit_price - self.entry_price

    @property
    def is_winner(self) -> bool:
        return self.pnl > 0


@dataclass
class StrategyResult:
    name: str
    trades: List[Trade] = field(default_factory=list)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def winners(self) -> int:
        return sum(1 for t in self.trades if t.is_winner)

    @property
    def losers(self) -> int:
        return sum(1 for t in self.trades if not t.is_winner and t.status == 'closed')

    @property
    def win_rate(self) -> float:
        closed = [t for t in self.trades if t.status == 'closed']
        if not closed:
            return 0.0
        return (self.winners / len(closed)) * 100

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def avg_win(self) -> float:
        wins = [t.pnl for t in self.trades if t.is_winner]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [t.pnl for t in self.trades if not t.is_winner and t.status == 'closed']
        return sum(losses) / len(losses) if losses else 0.0

    @property
    def expectancy(self) -> float:
        """Expected value per trade"""
        if not self.trades:
            return 0.0
        return self.total_pnl / len(self.trades)


# =============================================================================
# KITE CONNECTION
# =============================================================================
def get_kite() -> KiteConnect:
    with open(TOKEN_FILE) as f:
        token_data = json.load(f)
    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


# =============================================================================
# HELPERS
# =============================================================================
def parse_candle_time(candle: dict) -> datetime:
    date_val = candle.get('date')
    if isinstance(date_val, datetime):
        if date_val.tzinfo is not None:
            return date_val.replace(tzinfo=None)
        return date_val
    if isinstance(date_val, str):
        dt = datetime.fromisoformat(date_val.replace('Z', '+00:00'))
        return dt.replace(tzinfo=None)
    return datetime.now()


def is_green_candle(candle: dict) -> bool:
    return float(candle['close']) > float(candle['open'])


def get_candles_up_to(candles: List[dict], target_time: datetime) -> List[dict]:
    result = []
    for c in candles:
        ct = parse_candle_time(c)
        if ct <= target_time:
            result.append(c)
    return result


def calculate_ema(candles: List[dict], period: int = 20) -> Optional[float]:
    """Calculate EMA from candle closes."""
    if len(candles) < period:
        return None

    closes = [float(c['close']) for c in candles[-period*2:]]
    if len(closes) < period:
        return None

    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period

    for close in closes[period:]:
        ema = (close - ema) * multiplier + ema

    return ema


def detect_hh_hl_pattern(candles: List[dict]) -> Optional[Tuple[dict, dict, dict]]:
    """Detect 3 HH-HL candles. Returns (c1, c2, c3) or None."""
    if len(candles) < 4:
        return None

    c1, c2, c3 = candles[-4], candles[-3], candles[-2]

    # All green
    if not (is_green_candle(c1) and is_green_candle(c2) and is_green_candle(c3)):
        return None

    # Higher highs
    if not (c2['high'] > c1['high'] and c3['high'] > c2['high']):
        return None

    # Higher lows
    if not (c2['low'] > c1['low'] and c3['low'] > c2['low']):
        return None

    return (c1, c2, c3)


def get_scan_times(start: datetime, end: datetime) -> List[datetime]:
    """Generate scan times (15M intervals, 9:30-15:15, weekdays)."""
    times = []
    current = start.date()

    while current <= end.date():
        if current.weekday() < 5:
            for hour in range(9, 16):
                for minute in [0, 15, 30, 45]:
                    t = datetime.combine(current, dt_time(hour, minute))
                    if dt_time(9, 30) <= t.time() <= dt_time(15, 15):
                        if start <= t <= end:
                            times.append(t)
        current += timedelta(days=1)

    return sorted(times)


# =============================================================================
# STRATEGY ENGINES
# =============================================================================
class StrategyEngine:
    """Base strategy engine with common logic."""

    def __init__(self, kite: KiteConnect, name: str):
        self.kite = kite
        self.name = name
        self.result = StrategyResult(name=name)
        self.trade_counter = 0

        # Caches
        self.spot_cache: Dict[str, List[dict]] = {}
        self.option_cache: Dict[int, List[dict]] = {}
        self.index_options: Dict[str, List[dict]] = {}

    def fetch_spot_candles(self, index: str, start: datetime, end: datetime) -> List[dict]:
        if index in self.spot_cache:
            return self.spot_cache[index]

        token = INDEX_CONFIG[index]['index_token']
        try:
            data = self.kite.historical_data(
                instrument_token=token,
                from_date=start - timedelta(days=2),
                to_date=end + timedelta(days=1),
                interval='15minute'
            )
            self.spot_cache[index] = data
            return data
        except Exception as e:
            print(f"  Error fetching {index} spot: {e}")
            return []

    def fetch_option_candles(self, token: int, start: datetime, end: datetime) -> List[dict]:
        if token in self.option_cache:
            return self.option_cache[token]

        try:
            data = self.kite.historical_data(
                instrument_token=token,
                from_date=start - timedelta(days=2),
                to_date=end + timedelta(days=1),
                interval='15minute'
            )
            self.option_cache[token] = data
            return data
        except:
            return []

    def find_option(self, index: str, strike: float, opt_type: str) -> Optional[dict]:
        for opt in self.index_options.get(index, []):
            if opt['strike'] == strike and opt['option_type'] == opt_type:
                return opt
        return None

    def get_spot_at_time(self, index: str, t: datetime, start: datetime, end: datetime) -> Optional[float]:
        candles = self.fetch_spot_candles(index, start, end)
        for c in reversed(candles):
            if parse_candle_time(c) <= t:
                return float(c['close'])
        return None

    def should_take_signal(self, scan_time: datetime, index: str, opt_type: str,
                          spot_candles: List[dict], start: datetime, end: datetime) -> bool:
        """Override in subclasses for filtering."""
        return True

    def get_entry_price(self, pattern: Tuple[dict, dict, dict],
                       option_candles: List[dict], scan_time: datetime) -> Optional[float]:
        """Override for modified entry logic."""
        c1, c2, c3 = pattern
        return float(c3['close'])

    def get_sl_price(self, pattern: Tuple[dict, dict, dict]) -> float:
        """Override for modified SL logic."""
        c1, c2, c3 = pattern
        return float(c1['low'])

    def get_target_price(self, entry: float, sl: float) -> Optional[float]:
        """Override for target-based strategies."""
        return None

    def get_otm_positions(self) -> List[int]:
        """Override for different strike selection."""
        return [2, 3]

    def check_exit(self, trade: Trade, candles: List[dict], scan_time: datetime) -> Optional[Tuple[float, str]]:
        """Check if trade should exit. Returns (exit_price, reason) or None."""
        current_candles = get_candles_up_to(candles, scan_time)
        if not current_candles:
            return None

        latest = current_candles[-1]
        candle_low = float(latest['low'])
        candle_high = float(latest['high'])
        ltp = float(latest['close'])

        # Check target first
        if trade.target_price and candle_high >= trade.target_price:
            return (trade.target_price, 'TARGET')

        # Check SL
        if candle_low < trade.current_sl:
            return (ltp, 'SL_HIT')

        return None

    def update_trailing_sl(self, trade: Trade, candles: List[dict], scan_time: datetime) -> None:
        """Update trailing SL."""
        current_candles = get_candles_up_to(candles, scan_time)
        if len(current_candles) >= 2:
            prev_low = float(current_candles[-2]['low'])
            if prev_low > trade.current_sl:
                trade.current_sl = prev_low


class BaselineStrategy(StrategyEngine):
    """Original strategy - pattern on options."""
    pass


class IndexPatternStrategy(StrategyEngine):
    """Detect pattern on INDEX, trade options."""

    def detect_pattern_on_index(self, index: str, scan_time: datetime,
                                start: datetime, end: datetime) -> Optional[str]:
        """Returns 'CE' or 'PE' based on index pattern."""
        spot_candles = self.fetch_spot_candles(index, start, end)
        current = get_candles_up_to(spot_candles, scan_time)

        pattern = detect_hh_hl_pattern(current)
        if pattern:
            return 'CE'  # Bullish pattern = buy calls

        # Check for LL-LH (bearish) pattern
        if len(current) >= 4:
            c1, c2, c3 = current[-4], current[-3], current[-2]
            if (is_green_candle(c1) == False and is_green_candle(c2) == False and is_green_candle(c3) == False):
                if (c2['high'] < c1['high'] and c3['high'] < c2['high']):
                    if (c2['low'] < c1['low'] and c3['low'] < c2['low']):
                        return 'PE'  # Bearish pattern = buy puts

        return None


class PullbackStrategy(StrategyEngine):
    """Wait for pullback to candle 3 low before entry."""

    def get_entry_price(self, pattern: Tuple[dict, dict, dict],
                       option_candles: List[dict], scan_time: datetime) -> Optional[float]:
        c1, c2, c3 = pattern
        c3_low = float(c3['low'])

        # Look at next candle after pattern
        current = get_candles_up_to(option_candles, scan_time)
        if len(current) < 5:
            return None

        next_candle = current[-1]  # Current candle

        # Check if price pulled back to c3 low
        if float(next_candle['low']) <= c3_low * 1.01:  # Within 1%
            # And bounced (closed above low)
            if float(next_candle['close']) > float(next_candle['low']):
                return float(next_candle['close'])

        return None  # No pullback yet

    def get_sl_price(self, pattern: Tuple[dict, dict, dict]) -> float:
        c1, c2, c3 = pattern
        # Tighter SL - below c3 low
        return float(c3['low']) * 0.98


class TrendFilterStrategy(StrategyEngine):
    """Only trade with trend (above/below 20 EMA)."""

    def should_take_signal(self, scan_time: datetime, index: str, opt_type: str,
                          spot_candles: List[dict], start: datetime, end: datetime) -> bool:
        current = get_candles_up_to(spot_candles, scan_time)
        if len(current) < 25:
            return False

        ema = calculate_ema(current, 20)
        if ema is None:
            return False

        spot = float(current[-1]['close'])

        # CE only if above EMA, PE only if below
        if opt_type == 'CE' and spot > ema:
            return True
        if opt_type == 'PE' and spot < ema:
            return True

        return False


class TimeFilterStrategy(StrategyEngine):
    """Skip first and last 30 mins."""

    def should_take_signal(self, scan_time: datetime, index: str, opt_type: str,
                          spot_candles: List[dict], start: datetime, end: datetime) -> bool:
        t = scan_time.time()
        # Skip 9:15-9:45 and 15:00-15:15
        if t < dt_time(10, 0):
            return False
        if t >= dt_time(14, 30):
            return False
        return True


class ProfitTargetStrategy(StrategyEngine):
    """Add fixed 1.5:1 risk-reward target."""

    def get_target_price(self, entry: float, sl: float) -> Optional[float]:
        risk = entry - sl
        return entry + (risk * 1.5)


class ATMStrikeStrategy(StrategyEngine):
    """Use ATM and 1st OTM instead of 2nd/3rd."""

    def get_otm_positions(self) -> List[int]:
        return [0, 1]  # ATM and 1st OTM


class CombinedStrategy(StrategyEngine):
    """All improvements combined."""

    def get_otm_positions(self) -> List[int]:
        return [0, 1]  # ATM and 1st OTM

    def should_take_signal(self, scan_time: datetime, index: str, opt_type: str,
                          spot_candles: List[dict], start: datetime, end: datetime) -> bool:
        # Time filter
        t = scan_time.time()
        if t < dt_time(10, 0) or t >= dt_time(14, 30):
            return False

        # Trend filter
        current = get_candles_up_to(spot_candles, scan_time)
        if len(current) < 25:
            return False

        ema = calculate_ema(current, 20)
        if ema is None:
            return False

        spot = float(current[-1]['close'])

        if opt_type == 'CE' and spot <= ema:
            return False
        if opt_type == 'PE' and spot >= ema:
            return False

        return True

    def get_target_price(self, entry: float, sl: float) -> Optional[float]:
        risk = entry - sl
        return entry + (risk * 1.5)

    def get_sl_price(self, pattern: Tuple[dict, dict, dict]) -> float:
        # Tighter SL - candle 2 low instead of candle 1
        c1, c2, c3 = pattern
        return float(c2['low'])


# =============================================================================
# MAIN TEST RUNNER
# =============================================================================
def run_strategy_test(
    strategy_class: type,
    name: str,
    kite: KiteConnect,
    index_options: Dict[str, List[dict]],
    start: datetime,
    end: datetime
) -> StrategyResult:
    """Run a single strategy test."""

    engine = strategy_class(kite, name)
    engine.index_options = index_options

    scan_times = get_scan_times(start, end)

    for scan_time in scan_times:
        # Update existing trades
        for trade in engine.result.trades:
            if trade.status != 'open':
                continue

            opt = engine.find_option(trade.index, trade.strike, trade.option_type)
            if not opt:
                continue

            candles = engine.fetch_option_candles(opt['option_token'], start, end)

            exit_result = engine.check_exit(trade, candles, scan_time)
            if exit_result:
                trade.exit_price, trade.exit_reason = exit_result
                trade.exit_time = scan_time
                trade.status = 'closed'
            else:
                engine.update_trailing_sl(trade, candles, scan_time)

        # Look for new signals
        for index in ['NIFTY', 'BANKNIFTY', 'SENSEX']:
            spot_candles = engine.fetch_spot_candles(index, start, end)
            spot = engine.get_spot_at_time(index, scan_time, start, end)
            if not spot:
                continue

            strike_gap = INDEX_CONFIG[index]['strike_gap']
            atm = get_atm_strike(spot, strike_gap)
            otm_positions = engine.get_otm_positions()

            for opt_type in ['CE', 'PE']:
                if opt_type == 'CE':
                    strikes = [atm + (i * strike_gap) for i in otm_positions]
                else:
                    strikes = [atm - (i * strike_gap) for i in otm_positions]

                for strike in strikes:
                    opt = engine.find_option(index, strike, opt_type)
                    if not opt:
                        continue

                    # Skip if already have open position
                    has_open = any(
                        t.option_symbol == opt['option_symbol'] and t.status == 'open'
                        for t in engine.result.trades
                    )
                    if has_open:
                        continue

                    # Get option candles and detect pattern
                    opt_candles = engine.fetch_option_candles(opt['option_token'], start, end)
                    current = get_candles_up_to(opt_candles, scan_time)

                    pattern = detect_hh_hl_pattern(current)
                    if not pattern:
                        continue

                    # Apply filters
                    if not engine.should_take_signal(scan_time, index, opt_type, spot_candles, start, end):
                        continue

                    # Get entry
                    entry = engine.get_entry_price(pattern, opt_candles, scan_time)
                    if entry is None:
                        continue

                    sl = engine.get_sl_price(pattern)
                    target = engine.get_target_price(entry, sl)

                    # Create trade
                    engine.trade_counter += 1
                    trade = Trade(
                        id=f"{name}_{engine.trade_counter:04d}",
                        strategy=name,
                        index=index,
                        option_symbol=opt['option_symbol'],
                        option_type=opt_type,
                        strike=strike,
                        entry_time=scan_time,
                        entry_price=entry,
                        initial_sl=sl,
                        current_sl=sl,
                        target_price=target
                    )
                    engine.result.trades.append(trade)

    # Close remaining open trades
    for trade in engine.result.trades:
        if trade.status == 'open':
            trade.status = 'closed'
            trade.exit_time = end
            trade.exit_price = trade.entry_price  # Flat
            trade.exit_reason = 'TEST_END'

    return engine.result


def main():
    print("="*80)
    print("STRATEGY COMPARISON TEST")
    print("="*80)

    start_date = datetime(2026, 1, 1)
    end_date = datetime(2026, 1, 14, 15, 30)

    print(f"Period: {start_date.date()} to {end_date.date()}")
    print()

    # Connect
    print("Connecting to Kite...")
    kite = get_kite()
    print("Connected!")

    # Fetch options
    print("\nFetching index options...")
    index_options = fetch_index_options(
        indices=['NIFTY', 'BANKNIFTY', 'SENSEX'],
        monthly_only=False,
        min_dte=0,
        max_dte=30
    )

    # Strategy classes
    strategy_map = {
        'BASELINE': BaselineStrategy,
        'INDEX_PATTERN': IndexPatternStrategy,
        'PULLBACK': PullbackStrategy,
        'TREND_FILTER': TrendFilterStrategy,
        'TIME_FILTER': TimeFilterStrategy,
        'PROFIT_TARGET': ProfitTargetStrategy,
        'ATM_STRIKE': ATMStrikeStrategy,
        'COMBINED': CombinedStrategy,
    }

    results: Dict[str, StrategyResult] = {}

    # Run each strategy
    for name in STRATEGIES:
        print(f"\n{'='*60}")
        print(f"Testing: {name}")
        print('='*60)

        strategy_class = strategy_map[name]
        result = run_strategy_test(
            strategy_class, name, kite, index_options, start_date, end_date
        )
        results[name] = result

        print(f"  Trades: {result.total_trades}")
        print(f"  Winners: {result.winners}")
        print(f"  Win Rate: {result.win_rate:.1f}%")
        print(f"  Total P&L: {result.total_pnl:+.2f}")

    # Comparison table
    print("\n")
    print("="*100)
    print("COMPARISON RESULTS")
    print("="*100)
    print()
    print(f"{'Strategy':<20} {'Trades':>8} {'Winners':>8} {'Losers':>8} {'Win%':>8} {'Total P&L':>12} {'Avg Win':>10} {'Avg Loss':>10} {'Expect':>10}")
    print("-"*100)

    for name in STRATEGIES:
        r = results[name]
        print(f"{name:<20} {r.total_trades:>8} {r.winners:>8} {r.losers:>8} {r.win_rate:>7.1f}% {r.total_pnl:>+12.2f} {r.avg_win:>+10.2f} {r.avg_loss:>+10.2f} {r.expectancy:>+10.2f}")

    print("-"*100)

    # Find best
    best_by_winrate = max(results.values(), key=lambda x: x.win_rate)
    best_by_pnl = max(results.values(), key=lambda x: x.total_pnl)
    best_by_expectancy = max(results.values(), key=lambda x: x.expectancy)

    print()
    print("RANKINGS:")
    print(f"  Best Win Rate:    {best_by_winrate.name} ({best_by_winrate.win_rate:.1f}%)")
    print(f"  Best Total P&L:   {best_by_pnl.name} ({best_by_pnl.total_pnl:+.2f})")
    print(f"  Best Expectancy:  {best_by_expectancy.name} ({best_by_expectancy.expectancy:+.2f} per trade)")

    # Save results
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    # JSON
    json_file = RESULTS_DIR / f'comparison_{timestamp}.json'
    json_data = {
        'test_period': {'start': start_date.isoformat(), 'end': end_date.isoformat()},
        'results': {}
    }

    for name, r in results.items():
        json_data['results'][name] = {
            'total_trades': r.total_trades,
            'winners': r.winners,
            'losers': r.losers,
            'win_rate': round(r.win_rate, 2),
            'total_pnl': round(r.total_pnl, 2),
            'avg_win': round(r.avg_win, 2),
            'avg_loss': round(r.avg_loss, 2),
            'expectancy': round(r.expectancy, 2),
        }

    with open(json_file, 'w') as f:
        json.dump(json_data, f, indent=2)

    print(f"\nResults saved to: {json_file}")

    # CSV with all trades
    csv_file = RESULTS_DIR / f'all_trades_{timestamp}.csv'
    with open(csv_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['strategy', 'id', 'index', 'symbol', 'type', 'strike',
                        'entry_time', 'entry', 'sl', 'target', 'exit_time', 'exit', 'reason', 'pnl'])

        for name, r in results.items():
            for t in r.trades:
                writer.writerow([
                    t.strategy, t.id, t.index, t.option_symbol, t.option_type, t.strike,
                    t.entry_time.isoformat() if t.entry_time else '',
                    round(t.entry_price, 2),
                    round(t.initial_sl, 2),
                    round(t.target_price, 2) if t.target_price else '',
                    t.exit_time.isoformat() if t.exit_time else '',
                    round(t.exit_price, 2) if t.exit_price else '',
                    t.exit_reason or '',
                    round(t.pnl, 2)
                ])

    print(f"Trades saved to: {csv_file}")

    # Markdown report
    md_file = RESULTS_DIR / f'COMPARISON_{timestamp}.md'

    report = f"""# Strategy Comparison Report

**Test Period:** {start_date.date()} to {end_date.date()}
**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

---

## Results Summary

| Strategy | Trades | Win% | Total P&L | Expectancy |
|----------|--------|------|-----------|------------|
"""

    for name in STRATEGIES:
        r = results[name]
        report += f"| {name} | {r.total_trades} | {r.win_rate:.1f}% | {r.total_pnl:+.2f} | {r.expectancy:+.2f} |\n"

    report += f"""
---

## Best Performers

| Metric | Strategy | Value |
|--------|----------|-------|
| Win Rate | {best_by_winrate.name} | {best_by_winrate.win_rate:.1f}% |
| Total P&L | {best_by_pnl.name} | {best_by_pnl.total_pnl:+.2f} |
| Expectancy | {best_by_expectancy.name} | {best_by_expectancy.expectancy:+.2f} |

---

## Strategy Descriptions

1. **BASELINE**: Original 3 HH-HL pattern on option candles, SL at candle 1 low
2. **INDEX_PATTERN**: Detect pattern on INDEX, not options (cleaner signal)
3. **PULLBACK**: Wait for pullback to candle 3 low before entry
4. **TREND_FILTER**: Only CE above 20 EMA, PE below 20 EMA
5. **TIME_FILTER**: Skip 9:15-10:00 and 14:30-15:15
6. **PROFIT_TARGET**: Add 1.5:1 risk-reward target
7. **ATM_STRIKE**: Use ATM + 1st OTM instead of 2nd/3rd OTM
8. **COMBINED**: All improvements together

---

## Recommendations

Based on the results, the recommended approach is **{best_by_expectancy.name}** with:
- Expectancy: {best_by_expectancy.expectancy:+.2f} per trade
- Win Rate: {best_by_expectancy.win_rate:.1f}%
- Total P&L: {best_by_expectancy.total_pnl:+.2f}

---

*Report generated by Strategy Comparison Test*
"""

    with open(md_file, 'w') as f:
        f.write(report)

    print(f"Report saved to: {md_file}")


if __name__ == '__main__':
    main()
