#!/usr/bin/env python3
"""
Forward Test for Momentum Scanner (HH-HL Pattern)

Simulates the momentum scanner from a start date to see:
1. How many signals were generated
2. Win/Loss ratio
3. P&L analysis

Modes:
1. Live mode (default): Fetches data from Kite API
2. Offline mode (--offline): Uses pre-downloaded data from data/ folder

Usage:
    # Live mode (requires valid Kite token)
    python tests/momentum_forward_test/forward_test.py --start 2026-01-01 --end 2026-01-14

    # Offline mode (uses saved data)
    python tests/momentum_forward_test/forward_test.py --offline

    # Download data first (when token is valid)
    python tests/momentum_forward_test/download_data.py --start 2026-01-01 --end 2026-01-14
"""

import json
import csv
import argparse
from pathlib import Path
from datetime import datetime, timedelta, time as dt_time
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict

# Add parent paths
import sys
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
BOTS_DIR = PROJECT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))


# =============================================================================
# PATHS
# =============================================================================
DATA_DIR = SCRIPT_DIR / 'data'
RESULTS_DIR = SCRIPT_DIR / 'results'
TOKEN_FILE = BOTS_DIR / 'data' / 'kite_access_token.json'
INDEX_OPTIONS_FILE = BOTS_DIR / 'data' / 'index_options.csv'

# Saved data files
SPOT_DATA_FILE = DATA_DIR / 'spot_candles.json'
OPTION_DATA_FILE = DATA_DIR / 'option_candles.json'

DATA_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# =============================================================================
# CONFIGURATION
# =============================================================================
INDEX_CONFIG: Dict[str, Dict[str, Any]] = {
    'NIFTY': {
        'strike_gap': 50,
        'index_token': 256265,
        'exchange': 'NFO',
        'lot_size': 75,
    },
    'BANKNIFTY': {
        'strike_gap': 100,
        'index_token': 260105,
        'exchange': 'NFO',
        'lot_size': 30,
    },
    'SENSEX': {
        'strike_gap': 100,
        'index_token': 265,
        'exchange': 'BFO',
        'lot_size': 20,
    }
}

OTM_POSITIONS = [2, 3]
PATTERN_CANDLES = 3


# =============================================================================
# DATA CLASSES
# =============================================================================
@dataclass
class SimulatedTrade:
    """A simulated trade for backtesting."""
    id: str
    index: str
    option_symbol: str
    option_type: str
    strike: float
    otm_position: int

    entry_time: str  # ISO format
    entry_price: float
    initial_sl: float
    current_sl: float

    sl_updates: List[Tuple[str, float]] = field(default_factory=list)

    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None

    status: str = 'open'

    @property
    def pnl_points(self) -> float:
        if self.exit_price is None:
            return 0.0
        return self.exit_price - self.entry_price

    @property
    def pnl_pct(self) -> float:
        if self.exit_price is None or self.entry_price <= 0:
            return 0.0
        return ((self.exit_price / self.entry_price) - 1) * 100

    @property
    def is_winner(self) -> bool:
        return self.pnl_points > 0


@dataclass
class PatternCandles:
    """Store the 3 candles forming the pattern."""
    c1: dict
    c2: dict
    c3: dict


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================
def is_last_thursday_of_month(date_obj: datetime) -> bool:
    """Check if date is the last Thursday of its month."""
    if date_obj.weekday() != 3:
        return False
    next_thursday = date_obj + timedelta(days=7)
    return next_thursday.month != date_obj.month


def get_atm_strike(spot: float, strike_gap: int) -> int:
    """Calculate ATM strike."""
    return int(round(spot / strike_gap) * strike_gap)


def get_otm_strikes(spot: float, index: str) -> Dict[str, List[int]]:
    """Get 2nd and 3rd OTM strikes for CE and PE."""
    config = INDEX_CONFIG[index]
    strike_gap = int(config['strike_gap'])
    atm = get_atm_strike(spot, strike_gap)
    return {
        'CE': [atm + (i * strike_gap) for i in OTM_POSITIONS],
        'PE': [atm - (i * strike_gap) for i in OTM_POSITIONS]
    }


def is_green_candle(candle: dict) -> bool:
    """Check if candle is green (close > open)."""
    return float(candle['close']) > float(candle['open'])


def parse_candle_time(candle: dict) -> datetime:
    """Parse candle timestamp, returning timezone-naive datetime."""
    date_val = candle.get('date')
    if isinstance(date_val, datetime):
        # Remove timezone info to make naive
        if date_val.tzinfo is not None:
            return date_val.replace(tzinfo=None)
        return date_val
    if isinstance(date_val, str):
        # Handle ISO format
        dt = datetime.fromisoformat(date_val.replace('Z', '+00:00'))
        # Remove timezone info
        return dt.replace(tzinfo=None)
    return datetime.now()


def detect_hh_hl_pattern(candles: List[dict]) -> Optional[PatternCandles]:
    """
    Detect 3 consecutive COMPLETED candles with Higher Highs and Higher Lows.
    """
    if len(candles) < PATTERN_CANDLES + 1:
        return None

    c1, c2, c3 = candles[-4], candles[-3], candles[-2]

    # All must be green
    if not (is_green_candle(c1) and is_green_candle(c2) and is_green_candle(c3)):
        return None

    # Higher Highs
    if not (c2['high'] > c1['high'] and c3['high'] > c2['high']):
        return None

    # Higher Lows
    if not (c2['low'] > c1['low'] and c3['low'] > c2['low']):
        return None

    return PatternCandles(c1=c1, c2=c2, c3=c3)


def get_scan_times(start_date: datetime, end_date: datetime) -> List[datetime]:
    """Generate all scan times (9:30, 9:45, 10:00, ... 15:15) for the date range."""
    scan_times = []
    current_date = start_date.date()
    end_dt = end_date.date()

    while current_date <= end_dt:
        if current_date.weekday() < 5:  # Monday to Friday
            for hour in range(9, 16):
                for minute in [0, 15, 30, 45]:
                    scan_time = datetime.combine(current_date, dt_time(hour, minute))
                    if scan_time.time() < dt_time(9, 30):
                        continue
                    if scan_time.time() > dt_time(15, 15):
                        continue
                    if start_date <= scan_time <= end_date:
                        scan_times.append(scan_time)
        current_date += timedelta(days=1)

    return sorted(scan_times)


# =============================================================================
# OFFLINE DATA LOADER
# =============================================================================
class OfflineDataLoader:
    """Loads pre-downloaded data for offline testing."""

    def __init__(self):
        self.spot_data: Dict[str, List[dict]] = {}
        self.option_data: Dict[str, dict] = {}
        self.start_date: Optional[datetime] = None
        self.end_date: Optional[datetime] = None

    def load(self) -> bool:
        """Load saved data files."""
        if not SPOT_DATA_FILE.exists():
            print(f"ERROR: Spot data not found: {SPOT_DATA_FILE}")
            print("Run download_data.py first when Kite token is valid.")
            return False

        if not OPTION_DATA_FILE.exists():
            print(f"ERROR: Option data not found: {OPTION_DATA_FILE}")
            print("Run download_data.py first when Kite token is valid.")
            return False

        # Load spot data
        with open(SPOT_DATA_FILE) as f:
            spot_json = json.load(f)
            self.spot_data = spot_json.get('data', {})
            self.start_date = datetime.fromisoformat(spot_json.get('start', ''))
            self.end_date = datetime.fromisoformat(spot_json.get('end', ''))

        # Load option data
        with open(OPTION_DATA_FILE) as f:
            option_json = json.load(f)
            self.option_data = option_json.get('options', {})

        print(f"Loaded spot data: {sum(len(v) for v in self.spot_data.values())} candles")
        print(f"Loaded option data: {len(self.option_data)} contracts")

        return True

    def get_spot_candles(self, index: str) -> List[dict]:
        """Get spot candles for an index."""
        return self.spot_data.get(index, [])

    def get_option_candles(self, option_symbol: str) -> List[dict]:
        """Get candles for an option."""
        opt = self.option_data.get(option_symbol, {})
        return opt.get('candles', [])

    def get_option_info(self, option_symbol: str) -> Optional[dict]:
        """Get option metadata."""
        opt = self.option_data.get(option_symbol, {})
        return opt.get('info')

    def get_all_options(self) -> Dict[str, dict]:
        """Get all option info."""
        result = {}
        for symbol, data in self.option_data.items():
            if 'info' in data:
                result[symbol] = data['info']
        return result


# =============================================================================
# LIVE DATA LOADER
# =============================================================================
class LiveDataLoader:
    """Loads data from Kite API."""

    def __init__(self):
        self.kite = None
        self.spot_cache: Dict[str, List[dict]] = {}
        self.option_cache: Dict[int, List[dict]] = {}
        self.index_options: Dict[str, List[dict]] = {}

    def connect(self) -> bool:
        """Connect to Kite."""
        try:
            from kiteconnect import KiteConnect

            if not TOKEN_FILE.exists():
                print(f"ERROR: Token file not found: {TOKEN_FILE}")
                return False

            with open(TOKEN_FILE) as f:
                token_data = json.load(f)

            self.kite = KiteConnect(api_key=token_data['api_key'])
            self.kite.set_access_token(token_data['access_token'])
            print("Kite connected!")
            return True
        except Exception as e:
            print(f"ERROR connecting to Kite: {e}")
            return False

    def load_index_options(self, target_date: datetime) -> bool:
        """Fetch index options from Kite public API."""
        try:
            # Add scripts dir to path for import
            import sys
            scripts_dir = PROJECT_DIR / 'scripts'
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))

            from kite_instruments import fetch_index_options

            self.index_options = fetch_index_options(
                indices=['NIFTY', 'BANKNIFTY', 'SENSEX'],
                monthly_only=False,  # Include weekly for testing
                min_dte=0,
                max_dte=30
            )
            return True
        except Exception as e:
            print(f"ERROR fetching index options: {e}")
            return False

    def fetch_spot_candles(self, index: str, start: datetime, end: datetime) -> List[dict]:
        """Fetch spot candles."""
        if index in self.spot_cache:
            return self.spot_cache[index]

        token = INDEX_CONFIG[index]['index_token']
        try:
            data = self.kite.historical_data(
                instrument_token=token,
                from_date=start - timedelta(days=1),
                to_date=end + timedelta(days=1),
                interval='15minute'
            )
            self.spot_cache[index] = data
            return data
        except Exception as e:
            print(f"  ERROR fetching {index} spot: {e}")
            return []

    def fetch_option_candles(self, option_token: int, start: datetime, end: datetime) -> List[dict]:
        """Fetch option candles."""
        if option_token in self.option_cache:
            return self.option_cache[option_token]

        try:
            data = self.kite.historical_data(
                instrument_token=option_token,
                from_date=start - timedelta(days=1),
                to_date=end + timedelta(days=1),
                interval='15minute'
            )
            self.option_cache[option_token] = data
            return data
        except Exception:
            return []

    def find_option(self, index: str, strike: float, opt_type: str) -> Optional[dict]:
        """Find option by strike and type."""
        for opt in self.index_options.get(index, []):
            if opt['strike'] == strike and opt['option_type'] == opt_type:
                return opt
        return None


# =============================================================================
# FORWARD TEST ENGINE
# =============================================================================
class ForwardTestEngine:
    """Engine to run forward test simulation."""

    def __init__(self, start_date: datetime, end_date: datetime, offline: bool = False):
        self.start_date = start_date
        self.end_date = end_date
        self.offline = offline

        self.trades: List[SimulatedTrade] = []
        self.trade_counter = 0

        # Stats
        self.signals_detected = 0
        self.signals_by_index: Dict[str, int] = defaultdict(int)
        self.signals_by_type: Dict[str, int] = defaultdict(int)

        # Data loader
        self.data_loader = None
        self.option_symbol_map: Dict[str, dict] = {}  # symbol -> info

    def initialize(self) -> bool:
        """Initialize data loader."""
        if self.offline:
            loader = OfflineDataLoader()
            if not loader.load():
                return False
            self.data_loader = loader
            self.start_date = loader.start_date
            self.end_date = loader.end_date
            self.option_symbol_map = loader.get_all_options()
        else:
            loader = LiveDataLoader()
            if not loader.connect():
                return False
            if not loader.load_index_options(self.start_date):
                return False
            self.data_loader = loader
        return True

    def get_candles_up_to(self, all_candles: List[dict], scan_time: datetime) -> List[dict]:
        """Get candles up to a specific time."""
        result = []
        for c in all_candles:
            candle_time = parse_candle_time(c)
            if candle_time <= scan_time:
                result.append(c)
        return result

    def get_spot_at_time(self, index: str, target_time: datetime) -> Optional[float]:
        """Get spot price at a specific time."""
        if self.offline:
            candles = self.data_loader.get_spot_candles(index)
        else:
            candles = self.data_loader.fetch_spot_candles(index, self.start_date, self.end_date)

        best_candle = None
        for c in candles:
            candle_time = parse_candle_time(c)
            if candle_time <= target_time:
                best_candle = c
            elif candle_time > target_time:
                break

        return float(best_candle['close']) if best_candle else None

    def get_option_candles(self, option_symbol: str) -> List[dict]:
        """Get option candles."""
        if self.offline:
            return self.data_loader.get_option_candles(option_symbol)
        else:
            opt = self.data_loader.find_option_by_symbol(option_symbol)
            if opt:
                return self.data_loader.fetch_option_candles(
                    opt['option_token'], self.start_date, self.end_date
                )
            return []

    def find_option_for_strike(self, index: str, strike: float, opt_type: str) -> Optional[str]:
        """Find option symbol for a strike."""
        if self.offline:
            for symbol, info in self.option_symbol_map.items():
                if (info.get('strike') == strike and
                    info.get('option_type') == opt_type and
                    symbol.startswith(index)):
                    return symbol
            return None
        else:
            opt = self.data_loader.find_option(index, strike, opt_type)
            return opt['option_symbol'] if opt else None

    def run(self) -> Dict[str, Any]:
        """Run the forward test."""
        print("\n" + "="*70)
        print("FORWARD TEST: Momentum Scanner (HH-HL Pattern)")
        print("="*70)
        print(f"Mode: {'OFFLINE (saved data)' if self.offline else 'LIVE (Kite API)'}")
        print(f"Period: {self.start_date.date()} to {self.end_date.date()}")
        print()

        # Generate scan times
        scan_times = get_scan_times(self.start_date, self.end_date)
        print(f"Total scan intervals: {len(scan_times)}")

        if self.offline:
            # Get available option symbols from loaded data
            available_options = list(self.option_symbol_map.keys())
            print(f"Available option contracts: {len(available_options)}")

            # Group by index
            for index in INDEX_CONFIG:
                index_opts = [s for s in available_options if s.startswith(index)]
                print(f"  {index}: {len(index_opts)} options")

        print("\nScanning for patterns...")

        for scan_time in scan_times:
            # Check each index
            for index in INDEX_CONFIG:
                spot = self.get_spot_at_time(index, scan_time)
                if not spot:
                    continue

                otm_strikes = get_otm_strikes(spot, index)

                # Check CE and PE
                for opt_type in ['CE', 'PE']:
                    for idx, strike in enumerate(otm_strikes[opt_type]):
                        otm_pos = OTM_POSITIONS[idx]

                        option_symbol = self.find_option_for_strike(index, strike, opt_type)
                        if not option_symbol:
                            continue

                        # Skip if already have open position
                        has_open = any(
                            t.option_symbol == option_symbol and t.status == 'open'
                            for t in self.trades
                        )
                        if has_open:
                            continue

                        # Get candles
                        if self.offline:
                            all_candles = self.data_loader.get_option_candles(option_symbol)
                        else:
                            opt = self.data_loader.find_option(index, strike, opt_type)
                            if not opt:
                                continue
                            all_candles = self.data_loader.fetch_option_candles(
                                opt['option_token'], self.start_date, self.end_date
                            )

                        candles = self.get_candles_up_to(all_candles, scan_time)
                        if len(candles) < PATTERN_CANDLES + 1:
                            continue

                        # Detect pattern
                        pattern = detect_hh_hl_pattern(candles)
                        if not pattern:
                            continue

                        # Pattern found!
                        self.signals_detected += 1
                        self.signals_by_index[index] += 1
                        self.signals_by_type[opt_type] += 1

                        entry_price = float(pattern.c3['close'])
                        sl_price = float(pattern.c1['low'])

                        self.trade_counter += 1
                        trade = SimulatedTrade(
                            id=f"MT_{self.trade_counter:04d}",
                            index=index,
                            option_symbol=option_symbol,
                            option_type=opt_type,
                            strike=strike,
                            otm_position=otm_pos,
                            entry_time=scan_time.isoformat(),
                            entry_price=entry_price,
                            initial_sl=sl_price,
                            current_sl=sl_price,
                        )
                        self.trades.append(trade)

                        print(f"  [{scan_time.strftime('%Y-%m-%d %H:%M')}] SIGNAL: {option_symbol} @ {entry_price:.2f}, SL={sl_price:.2f}")

            # Update open trades
            self._update_open_trades(scan_time)

        # Close remaining
        self._close_remaining_trades()

        return self.generate_results()

    def _update_open_trades(self, scan_time: datetime) -> None:
        """Check SL and trail for open trades."""
        for trade in self.trades:
            if trade.status != 'open':
                continue

            if self.offline:
                all_candles = self.data_loader.get_option_candles(trade.option_symbol)
            else:
                opt = self.data_loader.find_option(trade.index, trade.strike, trade.option_type)
                if not opt:
                    continue
                all_candles = self.data_loader.fetch_option_candles(
                    opt['option_token'], self.start_date, self.end_date
                )

            candles = self.get_candles_up_to(all_candles, scan_time)
            if len(candles) < 1:
                continue

            latest = candles[-1]
            candle_low = float(latest['low'])

            # Check SL hit
            if candle_low < trade.current_sl:
                trade.status = 'closed'
                trade.exit_time = scan_time.isoformat()
                trade.exit_price = float(latest['close'])
                trade.exit_reason = 'SL_HIT'
                print(f"  [{scan_time.strftime('%Y-%m-%d %H:%M')}] SL HIT: {trade.option_symbol} @ {trade.exit_price:.2f}")
                continue

            # Trail SL
            if len(candles) >= 2:
                prev_low = float(candles[-2]['low'])
                if prev_low > trade.current_sl:
                    trade.sl_updates.append((scan_time.isoformat(), prev_low))
                    trade.current_sl = prev_low

    def _close_remaining_trades(self) -> None:
        """Close trades that are still open at end of test."""
        print("\nClosing remaining open trades...")
        for trade in self.trades:
            if trade.status == 'open':
                if self.offline:
                    all_candles = self.data_loader.get_option_candles(trade.option_symbol)
                else:
                    all_candles = []

                if all_candles:
                    last_candle = all_candles[-1]
                    trade.exit_price = float(last_candle['close'])
                else:
                    trade.exit_price = trade.entry_price  # Flat exit

                trade.status = 'closed'
                trade.exit_time = self.end_date.isoformat()
                trade.exit_reason = 'TEST_END'

    def generate_results(self) -> Dict[str, Any]:
        """Generate test results."""
        closed_trades = [t for t in self.trades if t.status == 'closed']
        winners = [t for t in closed_trades if t.is_winner]
        losers = [t for t in closed_trades if not t.is_winner]

        total_pnl = sum(t.pnl_points for t in closed_trades)
        win_rate = (len(winners) / len(closed_trades) * 100) if closed_trades else 0
        avg_win = sum(t.pnl_points for t in winners) / len(winners) if winners else 0
        avg_loss = sum(t.pnl_points for t in losers) / len(losers) if losers else 0
        risk_reward = abs(avg_win / avg_loss) if avg_loss != 0 else 0

        return {
            'test_period': {
                'start': self.start_date.isoformat(),
                'end': self.end_date.isoformat(),
                'mode': 'offline' if self.offline else 'live'
            },
            'summary': {
                'total_signals': self.signals_detected,
                'total_trades': len(self.trades),
                'closed_trades': len(closed_trades),
                'winners': len(winners),
                'losers': len(losers),
                'win_rate_pct': round(win_rate, 2),
                'total_pnl_points': round(total_pnl, 2),
                'avg_win_points': round(avg_win, 2),
                'avg_loss_points': round(avg_loss, 2),
                'risk_reward_ratio': round(risk_reward, 2),
            },
            'by_index': dict(self.signals_by_index),
            'by_type': dict(self.signals_by_type),
            'trades': [asdict(t) for t in self.trades],
        }


# =============================================================================
# OUTPUT FUNCTIONS
# =============================================================================
def save_results(results: Dict[str, Any], output_dir: Path) -> None:
    """Save results to files."""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    json_file = output_dir / f'forward_test_{timestamp}.json'
    with open(json_file, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to: {json_file}")

    if results.get('trades'):
        csv_file = output_dir / f'trades_{timestamp}.csv'
        trades = results['trades']

        with open(csv_file, 'w', newline='') as f:
            fieldnames = ['id', 'index', 'option_symbol', 'option_type', 'strike',
                          'otm_position', 'entry_time', 'entry_price', 'initial_sl',
                          'exit_time', 'exit_price', 'exit_reason', 'pnl_points', 'pnl_pct']
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for trade in trades:
                entry = trade.get('entry_price', 0)
                exit_p = trade.get('exit_price', 0) or 0
                pnl = exit_p - entry
                pnl_pct = ((exit_p / entry) - 1) * 100 if entry > 0 else 0

                writer.writerow({
                    'id': trade['id'],
                    'index': trade['index'],
                    'option_symbol': trade['option_symbol'],
                    'option_type': trade['option_type'],
                    'strike': trade['strike'],
                    'otm_position': trade['otm_position'],
                    'entry_time': trade['entry_time'],
                    'entry_price': entry,
                    'initial_sl': trade['initial_sl'],
                    'exit_time': trade.get('exit_time', ''),
                    'exit_price': exit_p,
                    'exit_reason': trade.get('exit_reason', ''),
                    'pnl_points': round(pnl, 2),
                    'pnl_pct': round(pnl_pct, 2),
                })

        print(f"Trades saved to: {csv_file}")


def print_summary(results: Dict[str, Any]) -> None:
    """Print summary report."""
    print("\n" + "="*70)
    print("FORWARD TEST RESULTS")
    print("="*70)

    summary = results.get('summary', {})

    print(f"""
Test Period: {results['test_period']['start'][:10]} to {results['test_period']['end'][:10]}
Mode: {results['test_period'].get('mode', 'live').upper()}

SIGNAL DETECTION
----------------
Total Signals:     {summary.get('total_signals', 0)}
By Index:          {dict(results.get('by_index', {}))}
By Type:           {dict(results.get('by_type', {}))}

TRADE PERFORMANCE
-----------------
Total Trades:      {summary.get('total_trades', 0)}
Closed Trades:     {summary.get('closed_trades', 0)}
Winners:           {summary.get('winners', 0)}
Losers:            {summary.get('losers', 0)}
Win Rate:          {summary.get('win_rate_pct', 0):.1f}%

P&L ANALYSIS
------------
Total P&L:         {summary.get('total_pnl_points', 0):+.2f} points
Avg Win:           {summary.get('avg_win_points', 0):+.2f} points
Avg Loss:          {summary.get('avg_loss_points', 0):+.2f} points
Risk/Reward:       1:{summary.get('risk_reward_ratio', 0):.2f}
""")

    trades = results.get('trades', [])
    if trades:
        print("\nTRADE LIST")
        print("-" * 100)
        print(f"{'ID':<10} {'Symbol':<25} {'Entry':<10} {'Exit':<10} {'P&L':>10} {'Reason':<12}")
        print("-" * 100)

        for t in trades:
            entry = t.get('entry_price', 0)
            exit_p = t.get('exit_price', 0) or 0
            pnl = exit_p - entry

            print(f"{t['id']:<10} {t['option_symbol']:<25} "
                  f"{entry:>10.2f} {exit_p:>10.2f} "
                  f"{pnl:>+10.2f} {t.get('exit_reason', 'OPEN'):<12}")


def generate_markdown_report(results: Dict[str, Any], output_dir: Path) -> None:
    """Generate markdown report."""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    report_file = output_dir / f'REPORT_{timestamp}.md'

    summary = results.get('summary', {})
    trades = results.get('trades', [])

    report = f"""# Forward Test Report - Momentum Scanner

**Test Period:** {results['test_period']['start'][:10]} to {results['test_period']['end'][:10]}
**Mode:** {results['test_period'].get('mode', 'live').upper()}
**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

---

## Executive Summary

| Metric | Value |
|--------|-------|
| Total Signals | {summary.get('total_signals', 0)} |
| Total Trades | {summary.get('total_trades', 0)} |
| Winners | {summary.get('winners', 0)} |
| Losers | {summary.get('losers', 0)} |
| **Win Rate** | **{summary.get('win_rate_pct', 0):.1f}%** |
| Total P&L | {summary.get('total_pnl_points', 0):+.2f} points |
| Risk/Reward | 1:{summary.get('risk_reward_ratio', 0):.2f} |

---

## Signal Distribution

### By Index
| Index | Signals |
|-------|---------|
"""

    for idx, count in results.get('by_index', {}).items():
        report += f"| {idx} | {count} |\n"

    report += """
### By Option Type
| Type | Signals |
|------|---------|
"""

    for opt_type, count in results.get('by_type', {}).items():
        report += f"| {opt_type} | {count} |\n"

    report += """
---

## Trade Details

| ID | Symbol | Entry Time | Entry | Exit | P&L | Reason |
|----|--------|------------|-------|------|-----|--------|
"""

    for t in trades:
        entry = t.get('entry_price', 0)
        exit_p = t.get('exit_price', 0) or 0
        pnl = exit_p - entry
        entry_time = t.get('entry_time', '')[:16] if t.get('entry_time') else ''

        report += f"| {t['id']} | {t['option_symbol']} | {entry_time} | {entry:.2f} | {exit_p:.2f} | {pnl:+.2f} | {t.get('exit_reason', 'OPEN')} |\n"

    report += """
---

## Methodology

1. **Pattern Detection**: 3 consecutive green candles with Higher Highs AND Higher Lows
2. **Entry**: At close of the 3rd candle
3. **Initial SL**: Low of the 1st candle in the pattern
4. **Trailing SL**: Updated to previous candle's low when it rises above current SL
5. **Exit**: When candle low breaches SL, or at test period end

---

*Report generated by Bouncer Forward Test Engine*
"""

    with open(report_file, 'w') as f:
        f.write(report)

    print(f"Report saved to: {report_file}")


# =============================================================================
# MAIN
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description='Forward Test for Momentum Scanner')
    parser.add_argument('--start', type=str, default='2026-01-01', help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, default=None, help='End date (YYYY-MM-DD)')
    parser.add_argument('--offline', action='store_true', help='Use saved data instead of Kite API')
    args = parser.parse_args()

    start_date = datetime.strptime(args.start, '%Y-%m-%d')
    end_date = datetime.strptime(args.end, '%Y-%m-%d') if args.end else datetime.now()
    end_date = end_date.replace(hour=15, minute=30)

    try:
        engine = ForwardTestEngine(start_date, end_date, offline=args.offline)

        if not engine.initialize():
            print("Failed to initialize. Exiting.")
            return

        results = engine.run()

        if results:
            print_summary(results)
            save_results(results, RESULTS_DIR)
            generate_markdown_report(results, RESULTS_DIR)
        else:
            print("No results generated.")

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    main()
