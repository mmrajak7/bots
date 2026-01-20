# ConvictBar Implementation Documentation

## Overview

ConvictBar is an algorithmic trading system based on Tom Hougaard's opening bar conviction strategy for NIFTY 50. The system identifies high-conviction bars early in the trading session and trades the follow-through confirmation.

## Project Structure

```
ConvictBar/
├── config/
│   └── params.yaml              # Strategy parameters (FROZEN)
├── scripts/
│   ├── run_backtest.py          # Backtest runner script
│   └── forward_test.py          # Live forward test script
├── src/
│   ├── __init__.py
│   ├── core/                    # Core strategy logic
│   │   ├── __init__.py
│   │   ├── bar.py               # Bar dataclass with computed properties
│   │   ├── conviction.py        # Conviction bar detection
│   │   ├── followthrough.py     # Follow-through validation
│   │   └── signals.py           # Entry/exit/trailing calculations
│   ├── backtest/                # Backtesting engine
│   │   ├── __init__.py
│   │   ├── data_loader.py       # Kite historical data loader
│   │   ├── simulator.py         # Trade simulation with slippage
│   │   ├── runner.py            # Main backtest orchestration
│   │   └── analytics.py         # Performance metrics and export
│   ├── live/                    # Live trading modules
│   │   ├── __init__.py
│   │   ├── ticker.py            # WebSocket ticker + bar builder
│   │   ├── alerts.py            # Telegram notifications
│   │   └── strategy.py          # Live strategy engine
│   └── utils/                   # Utilities
│       ├── __init__.py
│       ├── config.py            # YAML config loader
│       └── time_utils.py        # Market time utilities
├── state/                       # State persistence (created at runtime)
├── logs/                        # Log files (created at runtime)
├── output/                      # Backtest output (created at runtime)
└── cache/                       # Data cache (created at runtime)
```

## Strategy Logic

### 1. Conviction Bar Detection (`src/core/conviction.py`)

A conviction bar is identified when ALL conditions are met:
- **Range**: 15-50 points (not too small, not too large)
- **Body Ratio**: >= 50% (strong directional move)
- **Close Position**:
  - Bullish: Close in top 25% of range
  - Bearish: Close in bottom 25% of range

### 2. Follow-Through Validation (`src/core/followthrough.py`)

After a conviction bar, the next bar must confirm:
- **STRONG_CONFIRMATION**: Opens and closes beyond signal bar's close
- **MODERATE_CONFIRMATION**: Opens within signal bar but closes beyond
- **TRAP**: Price goes opposite direction beyond signal bar
- **DENIAL/NEUTRAL**: No confirmation

Minimum required: MODERATE_CONFIRMATION

### 3. Entry Rules (`src/core/signals.py`)

- **Entry Price**: Signal bar high/low + 2 points buffer
- **Stop Loss**: Signal bar low/high - 2 points buffer (capped at 35 pts)
- **Target**: Signal bar range × 1.0 multiplier (minimum 25 pts)
- **Order Valid**: 2 bars after follow-through confirmed
- **Entry Window**: 09:20 - 10:30 IST

### 4. Exit Rules

- **Stop Loss**: Hit during bar (pessimistic - if both SL/TGT hit, assume SL first)
- **Target**: Hit during bar
- **FT Failure**: First bar after entry fails to hold direction
- **Hard Close**: 15:20 IST mandatory exit
- **Trailing Stop**:
  - Move to breakeven at 1R profit
  - Trail at 2R profit
  - Update on bar close only

### 5. Filters

- **Bar 1 Exhaustion**: Skip day if Bar 1 range > 75 points
- **Gap Filter**: Skip day if opening gap > 75 points
- **Risk Limits**:
  - Max 3 trades per day
  - Max 60 points daily loss
  - Max 3 consecutive losses

## Module Details

### Core Modules

#### `bar.py`
```python
@dataclass
class Bar:
    datetime: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    bar_number: int

    @property
    def range(self) -> float          # high - low
    @property
    def body(self) -> float           # abs(close - open)
    @property
    def body_ratio(self) -> float     # body / range
    @property
    def close_position(self) -> float # (close - low) / range
    @property
    def is_bullish(self) -> bool      # close > open
```

#### `conviction.py`
```python
def detect_conviction(bar: Bar, params: ConvictionParams) -> ConvictionResult
# Returns: is_valid, conviction_type (BULLISH/BEARISH), metrics, skip_reason
```

#### `followthrough.py`
```python
def validate_followthrough(signal_bar, ft_bar, conviction_type, params) -> FTResult
# Returns: is_confirmed, ft_type, description
```

#### `signals.py`
```python
def calculate_trade_levels(signal_bar, conviction_type, entry_params, sl_params, tgt_params) -> TradeLevels
# Returns: direction, entry_price, stop_loss, target (or None if SL too wide)

def update_trailing_stop(position, current_price, trailing_params) -> float
# Returns: new stop loss level

def check_ft_failure_exit(position, bar) -> tuple[bool, str]
# Returns: should_exit, reason
```

### Backtest Modules

#### `data_loader.py`
- Loads historical data from Kite API
- Handles 60-day pagination limit
- Caches data locally for faster reruns
- Groups bars into DayData objects

#### `simulator.py`
- Simulates trade execution with slippage
- Checks entry fills and exits
- Tracks daily risk limits

#### `runner.py`
- Main orchestration loop
- Processes day by day, bar by bar
- Logs all signals and events
- Returns BacktestResult with all trades

#### `analytics.py`
- Calculates performance metrics
- Win rate, profit factor, max drawdown
- Exports to CSV and JSON

### Live Modules

#### `ticker.py`
```python
class BarBuilder:
    # Builds 5-min OHLCV bars from tick data
    def process_tick(ltp, timestamp, volume) -> Optional[Bar]

class LiveTicker:
    # Manages KiteTicker WebSocket connection
    def start() -> bool
    def stop()
    # Callbacks: on_tick, on_bar, on_connect, on_disconnect
```

#### `alerts.py`
```python
class TelegramAlerts:
    def send_conviction_detected(bar, conviction_type, details)
    def send_entry_signal(order, ft_confirmation)
    def send_entry_filled(position, fill_bar)
    def send_trailing_update(position, old_sl, new_sl, current_price, reason)
    def send_exit(result, exit_bar)
    def send_order_expired(order, current_bar)
    def send_trade_skipped(reason, bar)
    def send_day_skipped(reason, bar)
    def send_daily_summary(date, trades, total_pnl, signals, expired)
    def send_startup(tokens)
    def send_shutdown(reason)
    def send_error(error, context)
    def send_connection_status(connected, details)
```

#### `strategy.py`
```python
class LiveStrategy:
    # Same logic as BacktestRunner but for live bars
    def on_bar_complete(bar)  # Called by ticker when 5-min bar closes
    def start()               # Load state, begin processing
    def stop()                # Save state, send summary
    def get_status() -> dict  # Current strategy status
```

## Configuration (`config/params.yaml`)

All parameters are frozen from design review:

| Section | Parameter | Value | Description |
|---------|-----------|-------|-------------|
| conviction | min_range_points | 15 | Minimum bar range |
| conviction | max_range_points | 50 | Maximum bar range |
| conviction | min_body_ratio | 0.50 | Body >= 50% of range |
| conviction | min_close_position_bull | 0.75 | Bullish close in top 25% |
| conviction | max_close_position_bear | 0.25 | Bearish close in bottom 25% |
| entry | entry_buffer | 2.0 | Points beyond signal bar |
| entry | order_valid_bars | 2 | Bars to wait for fill |
| entry | entry_window_start | 09:20 | Earliest entry |
| entry | entry_window_end | 10:30 | Latest entry |
| stop_loss | sl_buffer | 2.0 | Points beyond signal bar |
| stop_loss | max_sl_points | 35 | Maximum SL distance |
| target | target_multiplier | 1.0 | Signal bar range × multiplier |
| target | min_target_points | 25 | Minimum target |
| trailing | breakeven_at_rr | 1.0 | Move to BE at 1R |
| trailing | trail_at_rr | 2.0 | Start trailing at 2R |
| trailing | update_on | bar_close | Only update on bar close |
| bar1_filter | max_bar1_range | 75 | Skip if Bar 1 > 75 pts |
| gap | large_gap_threshold | 75 | Skip if gap > 75 pts |
| risk | max_trades_per_day | 3 | Daily trade limit |
| risk | max_daily_loss_points | 60 | Daily loss limit |
| simulation | slippage_points | 1.0 | Entry/exit slippage |
| simulation | assume_sl_first | true | Pessimistic SL assumption |

## Usage

### Running Backtest
```bash
cd C:\Users\mail2\Documents\Projects\BOTS\ConvictBar
python scripts/run_backtest.py
```

Output:
- `output/backtest_trades_YYYYMMDD_HHMMSS.csv` - Trade details
- `output/backtest_summary_YYYYMMDD_HHMMSS.json` - Performance metrics

### Running Forward Test
```bash
cd C:\Users\mail2\Documents\Projects\BOTS\ConvictBar
python scripts/forward_test.py
```

Requirements:
- Valid Kite access token in `BOTS/data/kite_access_token.json`
- Telegram config in `BOTS/data/telegram_config.json`
- Run during market hours (09:15 - 15:30 IST)

## Dependencies

- Python 3.8+
- kiteconnect
- pyyaml
- requests (for Telegram)
- pandas (for analytics)

## Key Design Decisions

1. **Conservative Entry**: Wait for follow-through bar to close before entry
2. **Pessimistic Exits**: Assume SL hit first if both SL/TGT touched in same bar
3. **Bar Close Trailing**: Only update trailing stop on bar close (not intra-bar)
4. **Bar 1 Exhaustion Filter**: Skip days where opening momentum already captured
5. **State Persistence**: Live strategy saves state for restart recovery
6. **Paper Trading Only**: Forward test is paper trading with alerts, no live orders
