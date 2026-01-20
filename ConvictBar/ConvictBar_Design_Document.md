# ConvictBar - Trading System Design Document

**Version**: 1.0  
**Date**: 16th January 2026  
**Author**: Raja (with system design by Claude)  
**Strategy Origin**: Tom Hougaard's DAX Open Analysis, adapted for NIFTY 50

---

## Executive Summary

ConvictBar is an algorithmic trading system that identifies high-probability entry points during the NIFTY 50 market open by analyzing bar conviction patterns. The core premise is that bars closing near their extremes (highs for bullish, lows for bearish) with substantial body size indicate strong participant commitment and tend to produce follow-through momentum.

The system generates real-time alerts with entry, stop-loss, and target levels, and includes a comprehensive backtesting engine to validate strategy performance across historical data.

---

## Table of Contents

1. [Strategy Philosophy](#1-strategy-philosophy)
2. [Core Concepts](#2-core-concepts)
3. [Technical Specifications](#3-technical-specifications)
4. [Entry Logic](#4-entry-logic)
5. [Exit Logic](#5-exit-logic)
6. [Risk Management](#6-risk-management)
7. [Alert System](#7-alert-system)
8. [Backtesting Engine](#8-backtesting-engine)
9. [Data Requirements](#9-data-requirements)
10. [System Architecture](#10-system-architecture)
11. [Configuration Parameters](#11-configuration-parameters)
12. [Output Formats](#12-output-formats)
13. [Edge Cases & Error Handling](#13-edge-cases--error-handling)
14. [Future Enhancements](#14-future-enhancements)

---

## 1. Strategy Philosophy

### 1.1 The Core Insight

Markets at the open are characterized by a burst of activity as overnight orders are filled and participants establish positions. The first few bars reveal the "mood" of the market:

- **Party Days**: Strong conviction bars with closes near extremes, followed by momentum continuation
- **Funeral Days**: Indecisive bars or conviction bars without follow-through, leading to choppy price action

### 1.2 What Makes a Conviction Bar

A conviction bar demonstrates that one side (bulls or bears) has dominated the time period convincingly. This is measured by:

1. **Body Size**: Large body relative to recent bars indicates strong participation
2. **Close Position**: Where the bar closes relative to its range is the critical factor
   - Close in top 20% of range = Bullish conviction
   - Close in bottom 20% of range = Bearish conviction
   - Close in middle 40-60% = Indecision/confusion

### 1.3 Follow-Through Validation

A conviction bar alone is not sufficient. The subsequent bar(s) must confirm the direction. This filters out "traps" where a strong bar is immediately reversed.

### 1.4 Psychological Edge

The strategy exploits a common retail trader mistake: buying cheap (during weakness) instead of buying strength. Evidence shows that buying after strength is demonstrated produces better outcomes than anticipating a reversal.

---

## 2. Core Concepts

### 2.1 Terminology

| Term | Definition |
|------|------------|
| **Conviction Bar** | A bar meeting both body size and close position criteria |
| **Signal Bar** | A conviction bar that triggers a potential trade setup |
| **Close Position** | Where the close is located within the bar's range, expressed as percentage from low (0%) to high (100%) |
| **Body Ratio** | The body size (open-close distance) as a percentage of the total range (high-low) |
| **Follow-Through** | The subsequent bar's behavior confirming or denying the signal bar's direction |
| **Trap** | A conviction bar that fails to produce follow-through and reverses |
| **Party** | A day showing conviction with follow-through (trend day) |
| **Funeral** | A day with failed conviction or indecision (choppy/range day) |

### 2.2 Bar Classification

Each bar is classified into one of the following categories:

```
STRONG_BULL    : Body Ratio >= 60%, Close Position >= 80%
MODERATE_BULL  : Body Ratio >= 50%, Close Position >= 70%
WEAK_BULL      : Close > Open, does not meet above criteria
DOJI           : Body Ratio < 20%
WEAK_BEAR      : Close < Open, does not meet below criteria
MODERATE_BEAR  : Body Ratio >= 50%, Close Position <= 30%
STRONG_BEAR    : Body Ratio >= 60%, Close Position <= 20%
```

### 2.3 Follow-Through Classification

```
STRONG_CONFIRMATION : Next bar closes beyond signal bar's extreme in signal direction
MODERATE_CONFIRMATION : Next bar closes in signal direction, within upper/lower third
NEUTRAL : Next bar closes near midpoint
DENIAL : Next bar closes against signal direction
TRAP : Next bar takes out signal bar's opposite extreme
```

---

## 3. Technical Specifications

### 3.1 Instrument

- **Primary**: NIFTY 50 Index Futures (current month contract)
- **Symbol**: NIFTY (or as per broker's naming convention)
- **Exchange**: NSE

### 3.2 Timeframe

- **Primary Chart**: 5-minute bars
- **Rationale**: 
  - Matches Hougaard's original DAX research
  - Filters micro-noise while capturing opening "mood"
  - Produces bar ranges of 15-35 points typically, suitable for practical stop-losses

### 3.3 Trading Session

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Market Open | 09:15 IST | NSE opening time |
| Analysis Start | 09:15 IST | First bar begins |
| Entry Window Start | 09:20 IST | After Bar 1 closes (earliest entry on Bar 2 open) |
| Entry Window End | 10:30 IST | Opening personality established by then |
| Position Hold Until | 15:15 IST | Or until target/SL hit |
| Hard Close | 15:20 IST | Mandatory exit if position still open |

### 3.4 Bar Numbering Convention

```
Bar 1: 09:15:00 - 09:19:59
Bar 2: 09:20:00 - 09:24:59
Bar 3: 09:25:00 - 09:29:59
Bar 4: 09:30:00 - 09:34:59
...
Bar 15: 10:25:00 - 10:29:59 (Last bar for new entries)
```

---

## 4. Entry Logic

### 4.1 Conviction Bar Detection

A bar qualifies as a **Conviction Bar** if ALL of the following conditions are met:

#### For Bullish Conviction:
```python
def is_bullish_conviction(bar, params):
    range_size = bar.high - bar.low
    body_size = bar.close - bar.open
    
    # Must be a green/white bar
    if bar.close <= bar.open:
        return False
    
    # Minimum range requirement (avoid tiny bars)
    if range_size < params.min_range_points:
        return False
    
    # Body ratio check
    body_ratio = body_size / range_size
    if body_ratio < params.min_body_ratio:
        return False
    
    # Close position check (percentage from low)
    close_position = (bar.close - bar.low) / range_size
    if close_position < params.min_close_position_bull:
        return False
    
    return True
```

#### For Bearish Conviction:
```python
def is_bearish_conviction(bar, params):
    range_size = bar.high - bar.low
    body_size = bar.open - bar.close
    
    # Must be a red/black bar
    if bar.close >= bar.open:
        return False
    
    # Minimum range requirement
    if range_size < params.min_range_points:
        return False
    
    # Body ratio check
    body_ratio = body_size / range_size
    if body_ratio < params.min_body_ratio:
        return False
    
    # Close position check (percentage from low - lower is more bearish)
    close_position = (bar.close - bar.low) / range_size
    if close_position > params.max_close_position_bear:
        return False
    
    return True
```

### 4.2 Default Parameters for Conviction Detection

| Parameter | Default Value | Description |
|-----------|---------------|-------------|
| `min_range_points` | 15 | Minimum bar range in points |
| `max_range_points` | 50 | Maximum bar range (skip if exceeded - too volatile) |
| `min_body_ratio` | 0.50 | Body must be at least 50% of range |
| `min_close_position_bull` | 0.75 | Close must be in top 25% for bullish |
| `max_close_position_bear` | 0.25 | Close must be in bottom 25% for bearish |

### 4.3 Follow-Through Validation

After a conviction bar is detected, wait for the next bar to close and validate:

#### For Bullish Follow-Through:
```python
def validate_bullish_followthrough(signal_bar, followthrough_bar, params):
    signal_midpoint = (signal_bar.high + signal_bar.low) / 2
    
    # Bar 2 must not close below signal bar's midpoint
    if followthrough_bar.close < signal_midpoint:
        return "DENIAL"
    
    # Bar 2 must not take out signal bar's low (trap check)
    if followthrough_bar.low < signal_bar.low - params.trap_buffer:
        return "TRAP"
    
    # Strong confirmation: closes above signal bar's high
    if followthrough_bar.close > signal_bar.high:
        return "STRONG_CONFIRMATION"
    
    # Moderate confirmation: closes in upper half
    if followthrough_bar.close >= signal_midpoint:
        return "MODERATE_CONFIRMATION"
    
    return "NEUTRAL"
```

### 4.4 Entry Trigger

**Primary Entry Method**: Break of Conviction Bar's Extreme

```python
def calculate_entry(signal_bar, direction, params):
    if direction == "LONG":
        entry_price = signal_bar.high + params.entry_buffer
    else:  # SHORT
        entry_price = signal_bar.low - params.entry_buffer
    
    return entry_price
```

| Parameter | Default Value | Description |
|-----------|---------------|-------------|
| `entry_buffer` | 2.0 | Points beyond signal bar extreme to confirm breakout |

**Entry Execution**: Place a stop-limit order at entry price. If price reaches entry level within the next 2 bars after follow-through confirmation, entry is triggered. If not reached within 2 bars, cancel the order (setup has expired).

### 4.5 Entry Conditions Summary

A LONG entry requires:
1. ✅ Bullish conviction bar detected (Bar N)
2. ✅ Bar N+1 provides at least MODERATE_CONFIRMATION
3. ✅ Price breaks above Bar N's high + buffer
4. ✅ Current time is within entry window (09:20 - 10:30)
5. ✅ No existing position
6. ✅ Daily trade limit not reached
7. ✅ Daily loss limit not breached

A SHORT entry requires:
1. ✅ Bearish conviction bar detected (Bar N)
2. ✅ Bar N+1 provides at least MODERATE_CONFIRMATION
3. ✅ Price breaks below Bar N's low - buffer
4. ✅ Current time is within entry window (09:20 - 10:30)
5. ✅ No existing position
6. ✅ Daily trade limit not reached
7. ✅ Daily loss limit not breached

### 4.6 Gap Day Adjustments

Large gaps can invalidate normal patterns. Apply these modifications:

```python
def calculate_gap(current_open, previous_close):
    return current_open - previous_close

def adjust_for_gap(gap_points, params):
    """
    Returns adjustment factors for gap days
    """
    if abs(gap_points) < params.small_gap_threshold:
        # Normal day, no adjustment
        return {
            "continuation_confidence": 1.0,
            "reversal_confidence": 1.0,
            "skip_trade": False
        }
    
    elif abs(gap_points) < params.large_gap_threshold:
        # Medium gap: be cautious with continuation
        return {
            "continuation_confidence": 0.7,
            "reversal_confidence": 1.2,
            "skip_trade": False
        }
    
    else:
        # Large gap: high reversal probability, skip or fade
        return {
            "continuation_confidence": 0.5,
            "reversal_confidence": 1.5,
            "skip_trade": params.skip_large_gap_days
        }
```

| Parameter | Default Value | Description |
|-----------|---------------|-------------|
| `small_gap_threshold` | 30 | Gap under 30 points is normal |
| `large_gap_threshold` | 75 | Gap over 75 points is significant |
| `skip_large_gap_days` | False | Whether to skip trading on large gap days |

---

## 5. Exit Logic

### 5.1 Stop Loss Calculation

**Method**: Signal Bar Based with Cap

```python
def calculate_stop_loss(signal_bar, entry_price, direction, params):
    if direction == "LONG":
        # SL below signal bar's low
        raw_sl = signal_bar.low - params.sl_buffer
        sl_distance = entry_price - raw_sl
        
        # Apply cap if SL is too wide
        if sl_distance > params.max_sl_points:
            adjusted_sl = entry_price - params.max_sl_points
            return adjusted_sl, True  # True indicates capped
        
        return raw_sl, False
    
    else:  # SHORT
        # SL above signal bar's high
        raw_sl = signal_bar.high + params.sl_buffer
        sl_distance = raw_sl - entry_price
        
        if sl_distance > params.max_sl_points:
            adjusted_sl = entry_price + params.max_sl_points
            return adjusted_sl, True
        
        return raw_sl, False
```

| Parameter | Default Value | Description |
|-----------|---------------|-------------|
| `sl_buffer` | 2.0 | Points beyond signal bar extreme for SL |
| `max_sl_points` | 35 | Maximum allowed SL distance |
| `min_sl_points` | 15 | Minimum SL distance (if less, use this) |

**Skip Trade Rule**: If the signal bar's range exceeds `max_sl_points`, skip the trade entirely (risk is too high).

### 5.2 Target Calculation

**Method**: Measured Move with Minimum

```python
def calculate_target(signal_bar, entry_price, direction, params):
    signal_range = signal_bar.high - signal_bar.low
    
    # Projected move = signal bar range * multiplier
    projected_move = signal_range * params.target_multiplier
    
    # Apply minimum target
    target_distance = max(projected_move, params.min_target_points)
    
    if direction == "LONG":
        target = entry_price + target_distance
    else:
        target = entry_price - target_distance
    
    return target, target_distance
```

| Parameter | Default Value | Description |
|-----------|---------------|-------------|
| `target_multiplier` | 1.0 | Multiply signal bar range for target |
| `min_target_points` | 25 | Minimum target distance |

### 5.3 Early Exit Conditions

#### 5.3.1 Follow-Through Failure Exit

If the bar after entry closes against the position beyond the signal bar's midpoint, exit immediately (don't wait for SL):

```python
def check_followthrough_failure(position, current_bar, signal_bar):
    signal_midpoint = (signal_bar.high + signal_bar.low) / 2
    
    if position.direction == "LONG":
        if current_bar.close < signal_midpoint:
            return True, "Followthrough failure - close below midpoint"
    
    else:  # SHORT
        if current_bar.close > signal_midpoint:
            return True, "Followthrough failure - close above midpoint"
    
    return False, None
```

#### 5.3.2 Time-Based Exit

```python
def check_time_exit(position, current_time, params):
    # Hard close at end of day
    if current_time >= params.hard_close_time:
        return True, "Hard close - end of day"
    
    # Optional: Exit if position held too long without reaching target
    hold_duration = current_time - position.entry_time
    if hold_duration > params.max_hold_duration:
        return True, "Max hold duration exceeded"
    
    return False, None
```

| Parameter | Default Value | Description |
|-----------|---------------|-------------|
| `hard_close_time` | 15:20 IST | Mandatory exit time |
| `max_hold_duration` | None | Optional max hold (disabled by default) |

### 5.4 Trailing Stop (Optional Enhancement)

Once position is in profit by 1x risk (1:1 achieved), move SL to breakeven:

```python
def update_trailing_stop(position, current_price, params):
    if not params.enable_trailing:
        return position.stop_loss
    
    risk = abs(position.entry_price - position.initial_stop_loss)
    
    if position.direction == "LONG":
        current_profit = current_price - position.entry_price
        
        # Move to breakeven at 1R
        if current_profit >= risk and position.stop_loss < position.entry_price:
            return position.entry_price + params.breakeven_buffer
        
        # Trail at 2R
        if current_profit >= 2 * risk:
            new_sl = current_price - risk
            return max(position.stop_loss, new_sl)
    
    else:  # SHORT
        current_profit = position.entry_price - current_price
        
        if current_profit >= risk and position.stop_loss > position.entry_price:
            return position.entry_price - params.breakeven_buffer
        
        if current_profit >= 2 * risk:
            new_sl = current_price + risk
            return min(position.stop_loss, new_sl)
    
    return position.stop_loss
```

| Parameter | Default Value | Description |
|-----------|---------------|-------------|
| `enable_trailing` | True | Enable trailing stop logic |
| `breakeven_buffer` | 2.0 | Points beyond entry for breakeven SL |

---

## 6. Risk Management

### 6.1 Position Sizing

**Fixed Lot Approach** (Default):

```python
def calculate_position_size(params):
    return params.fixed_lots
```

| Parameter | Default Value | Description |
|-----------|---------------|-------------|
| `fixed_lots` | 1 | Number of lots per trade |

**Risk-Based Approach** (Optional):

```python
def calculate_position_size_risk_based(sl_points, params):
    risk_amount = params.capital * params.risk_per_trade_pct
    point_value = params.lot_size * params.tick_value
    
    lots = risk_amount / (sl_points * point_value)
    lots = min(lots, params.max_lots)
    lots = max(1, int(lots))
    
    return lots
```

| Parameter | Default Value | Description |
|-----------|---------------|-------------|
| `capital` | 500000 | Trading capital in INR |
| `risk_per_trade_pct` | 0.01 | Risk 1% per trade |
| `lot_size` | 25 | NIFTY lot size (as of current contract) |
| `tick_value` | 0.05 | Minimum price movement |
| `max_lots` | 5 | Maximum lots per trade |

### 6.2 Daily Limits

```python
class DailyRiskManager:
    def __init__(self, params):
        self.max_trades_per_day = params.max_trades_per_day
        self.max_daily_loss_points = params.max_daily_loss_points
        self.trades_today = 0
        self.pnl_today = 0
    
    def can_trade(self):
        if self.trades_today >= self.max_trades_per_day:
            return False, "Daily trade limit reached"
        
        if self.pnl_today <= -self.max_daily_loss_points:
            return False, "Daily loss limit reached"
        
        return True, None
    
    def record_trade(self, pnl_points):
        self.trades_today += 1
        self.pnl_today += pnl_points
    
    def reset(self):
        self.trades_today = 0
        self.pnl_today = 0
```

| Parameter | Default Value | Description |
|-----------|---------------|-------------|
| `max_trades_per_day` | 3 | Maximum trades allowed per day |
| `max_daily_loss_points` | 60 | Stop trading if loss exceeds this |

### 6.3 Consecutive Loss Handling

```python
def check_consecutive_losses(trade_history, params):
    recent_trades = trade_history[-params.consecutive_loss_lookback:]
    
    consecutive_losses = 0
    for trade in reversed(recent_trades):
        if trade.pnl < 0:
            consecutive_losses += 1
        else:
            break
    
    if consecutive_losses >= params.max_consecutive_losses:
        return True, f"{consecutive_losses} consecutive losses - pause trading"
    
    return False, None
```

| Parameter | Default Value | Description |
|-----------|---------------|-------------|
| `max_consecutive_losses` | 3 | Pause after N consecutive losses |
| `consecutive_loss_lookback` | 10 | How many recent trades to check |

---

## 7. Alert System

### 7.1 Alert Types

| Alert Type | Trigger | Priority |
|------------|---------|----------|
| `CONVICTION_DETECTED` | Conviction bar closes | INFO |
| `FOLLOWTHROUGH_CONFIRMED` | Follow-through bar confirms | INFO |
| `ENTRY_SIGNAL` | All conditions met, order placed | HIGH |
| `ENTRY_FILLED` | Entry order executed | HIGH |
| `SL_HIT` | Stop loss triggered | HIGH |
| `TARGET_HIT` | Target reached | HIGH |
| `EARLY_EXIT` | Follow-through failure exit | HIGH |
| `TIME_EXIT` | Hard close triggered | MEDIUM |
| `TRADE_SKIPPED` | Trade skipped (gap/risk) | INFO |
| `DAILY_LIMIT_REACHED` | Trade or loss limit hit | MEDIUM |
| `SYSTEM_ERROR` | Error in system | CRITICAL |

### 7.2 Alert Content Structure

```python
@dataclass
class Alert:
    timestamp: datetime
    alert_type: str
    priority: str
    symbol: str
    direction: Optional[str]  # LONG or SHORT
    entry_price: Optional[float]
    stop_loss: Optional[float]
    target: Optional[float]
    signal_bar_data: Optional[dict]
    message: str
    metadata: dict
```

### 7.3 Alert Message Templates

#### Entry Signal Alert:
```
🟢 CONVICTBAR ENTRY SIGNAL 🟢

Symbol: NIFTY
Direction: LONG
Entry: 24,150.00 (Break of signal bar high + buffer)
Stop Loss: 24,115.00 (35 points)
Target: 24,185.00 (35 points)
Risk:Reward = 1:1

Signal Bar Analysis:
- Bar Time: 09:20-09:25
- Range: 35 points (24115-24150)
- Body Ratio: 65%
- Close Position: 82% (Strong bull close)

Follow-through: MODERATE_CONFIRMATION
- Bar 2 closed at 24,145 (above midpoint)

⏰ Order valid for next 2 bars
```

#### Exit Alert (Target Hit):
```
✅ TARGET HIT ✅

Symbol: NIFTY
Direction: LONG
Entry: 24,150.00
Exit: 24,185.00
P&L: +35 points

Trade Duration: 25 minutes
Bars Held: 5
```

#### Exit Alert (Stop Loss):
```
🔴 STOP LOSS HIT 🔴

Symbol: NIFTY
Direction: LONG
Entry: 24,150.00
Exit: 24,115.00
P&L: -35 points

Trade Duration: 10 minutes
Note: Signal bar low violated
```

### 7.4 Telegram Integration

```python
class TelegramNotifier:
    def __init__(self, bot_token, chat_id):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
    
    async def send_alert(self, alert: Alert):
        message = self.format_message(alert)
        
        # Add chart image if available
        if alert.metadata.get("chart_image"):
            await self.send_photo(alert.metadata["chart_image"], message)
        else:
            await self.send_message(message)
    
    def format_message(self, alert: Alert) -> str:
        # Format based on alert type
        template = ALERT_TEMPLATES.get(alert.alert_type)
        return template.format(**alert.__dict__)
    
    async def send_message(self, text: str):
        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        async with aiohttp.ClientSession() as session:
            await session.post(url, json=payload)
    
    async def send_photo(self, image_path: str, caption: str):
        url = f"{self.base_url}/sendPhoto"
        # Implementation for sending chart images
        pass
```

### 7.5 Configuration for Alerts

| Parameter | Default Value | Description |
|-----------|---------------|-------------|
| `telegram_bot_token` | (required) | Telegram bot API token |
| `telegram_chat_id` | (required) | Chat ID to send alerts |
| `send_conviction_alerts` | True | Alert on conviction bar detection |
| `send_chart_images` | True | Include chart screenshots |
| `alert_sound_enabled` | True | Enable sound notifications |

---

## 8. Backtesting Engine

### 8.1 Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    BACKTESTING ENGINE                        │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │ Data Loader  │───▶│ Strategy     │───▶│ Trade        │  │
│  │              │    │ Engine       │    │ Simulator    │  │
│  └──────────────┘    └──────────────┘    └──────────────┘  │
│         │                   │                   │           │
│         ▼                   ▼                   ▼           │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │ Bar Builder  │    │ Signal       │    │ Position     │  │
│  │              │    │ Generator    │    │ Manager      │  │
│  └──────────────┘    └──────────────┘    └──────────────┘  │
│                                                              │
│                      ┌──────────────┐                       │
│                      │ Analytics &  │                       │
│                      │ Reporting    │                       │
│                      └──────────────┘                       │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 8.2 Data Loader

```python
class DataLoader:
    def __init__(self, data_source: str):
        self.data_source = data_source
    
    def load_historical_data(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        timeframe: str = "5min"
    ) -> pd.DataFrame:
        """
        Load historical OHLCV data
        
        Returns DataFrame with columns:
        - datetime: Timestamp
        - open: Open price
        - high: High price
        - low: Low price
        - close: Close price
        - volume: Volume (optional)
        """
        pass
    
    def validate_data(self, df: pd.DataFrame) -> bool:
        """
        Validate data quality:
        - No missing bars during market hours
        - No invalid prices (negative, zero)
        - Proper OHLC relationship (L <= O,C <= H)
        """
        pass
```

### 8.3 Strategy Engine

```python
class ConvictBarStrategy:
    def __init__(self, params: StrategyParams):
        self.params = params
        self.position = None
        self.pending_order = None
        self.trade_history = []
        self.daily_manager = DailyRiskManager(params)
    
    def on_bar_close(self, bar: Bar, bar_number: int) -> List[Signal]:
        """
        Called when each bar closes
        Returns list of signals generated
        """
        signals = []
        
        # Check for exit signals first
        if self.position:
            exit_signal = self.check_exits(bar)
            if exit_signal:
                signals.append(exit_signal)
                return signals
        
        # Check for pending order fill
        if self.pending_order:
            fill_signal = self.check_order_fill(bar)
            if fill_signal:
                signals.append(fill_signal)
        
        # Check for new entry signals
        if not self.position and not self.pending_order:
            entry_signal = self.check_entry(bar, bar_number)
            if entry_signal:
                signals.append(entry_signal)
        
        return signals
    
    def check_entry(self, bar: Bar, bar_number: int) -> Optional[Signal]:
        """
        Check if current bar creates an entry opportunity
        """
        # Skip if outside entry window
        if not self.is_entry_window(bar.datetime):
            return None
        
        # Skip if daily limits reached
        can_trade, reason = self.daily_manager.can_trade()
        if not can_trade:
            return Signal("TRADE_SKIPPED", reason=reason)
        
        # Check for conviction bar
        if self.is_conviction_bar(bar):
            self.signal_bar = bar
            self.awaiting_followthrough = True
            return Signal("CONVICTION_DETECTED", bar=bar)
        
        # Check for follow-through if we have a signal bar
        if self.awaiting_followthrough and self.signal_bar:
            ft_result = self.validate_followthrough(self.signal_bar, bar)
            
            if ft_result in ["STRONG_CONFIRMATION", "MODERATE_CONFIRMATION"]:
                # Create pending order
                direction = "LONG" if self.signal_bar.close > self.signal_bar.open else "SHORT"
                entry, sl, target = self.calculate_levels(self.signal_bar, direction)
                
                self.pending_order = PendingOrder(
                    direction=direction,
                    entry_price=entry,
                    stop_loss=sl,
                    target=target,
                    signal_bar=self.signal_bar,
                    valid_until_bar=bar_number + 2
                )
                
                return Signal("ENTRY_SIGNAL", order=self.pending_order)
            
            elif ft_result == "TRAP":
                self.awaiting_followthrough = False
                self.signal_bar = None
                return Signal("TRAP_DETECTED")
        
        return None
```

### 8.4 Trade Simulator

```python
class TradeSimulator:
    def __init__(self, params: SimulatorParams):
        self.params = params
        self.slippage = params.slippage_points
        self.commission = params.commission_per_lot
    
    def simulate_entry(self, order: PendingOrder, bar: Bar) -> Optional[Trade]:
        """
        Check if pending order would be filled on this bar
        """
        if order.direction == "LONG":
            # Check if high reached entry price
            if bar.high >= order.entry_price:
                fill_price = order.entry_price + self.slippage
                return Trade(
                    direction="LONG",
                    entry_price=fill_price,
                    entry_time=bar.datetime,
                    stop_loss=order.stop_loss,
                    target=order.target,
                    signal_bar=order.signal_bar
                )
        else:
            if bar.low <= order.entry_price:
                fill_price = order.entry_price - self.slippage
                return Trade(
                    direction="SHORT",
                    entry_price=fill_price,
                    entry_time=bar.datetime,
                    stop_loss=order.stop_loss,
                    target=order.target,
                    signal_bar=order.signal_bar
                )
        
        return None
    
    def simulate_exit(self, trade: Trade, bar: Bar) -> Optional[TradeResult]:
        """
        Check if trade would be closed on this bar
        Returns TradeResult if closed, None if still open
        """
        if trade.direction == "LONG":
            # Check SL first (assume worst case)
            if bar.low <= trade.stop_loss:
                exit_price = trade.stop_loss - self.slippage
                return self.create_result(trade, exit_price, bar, "STOP_LOSS")
            
            # Check target
            if bar.high >= trade.target:
                exit_price = trade.target - self.slippage  # Conservative
                return self.create_result(trade, exit_price, bar, "TARGET")
        
        else:  # SHORT
            if bar.high >= trade.stop_loss:
                exit_price = trade.stop_loss + self.slippage
                return self.create_result(trade, exit_price, bar, "STOP_LOSS")
            
            if bar.low <= trade.target:
                exit_price = trade.target + self.slippage
                return self.create_result(trade, exit_price, bar, "TARGET")
        
        return None
    
    def create_result(self, trade, exit_price, bar, exit_reason) -> TradeResult:
        pnl_points = (exit_price - trade.entry_price) if trade.direction == "LONG" \
                     else (trade.entry_price - exit_price)
        
        pnl_amount = pnl_points * self.params.lot_size * self.params.lots - self.commission * 2
        
        return TradeResult(
            trade=trade,
            exit_price=exit_price,
            exit_time=bar.datetime,
            exit_reason=exit_reason,
            pnl_points=pnl_points,
            pnl_amount=pnl_amount
        )
```

### 8.5 Analytics & Reporting

```python
class BacktestAnalytics:
    def __init__(self, trade_results: List[TradeResult]):
        self.results = trade_results
        self.df = pd.DataFrame([r.__dict__ for r in trade_results])
    
    def calculate_metrics(self) -> dict:
        """Calculate comprehensive performance metrics"""
        
        total_trades = len(self.results)
        if total_trades == 0:
            return {"error": "No trades to analyze"}
        
        winners = [r for r in self.results if r.pnl_points > 0]
        losers = [r for r in self.results if r.pnl_points < 0]
        
        metrics = {
            # Basic Stats
            "total_trades": total_trades,
            "winning_trades": len(winners),
            "losing_trades": len(losers),
            "win_rate": len(winners) / total_trades * 100,
            
            # P&L Stats
            "total_pnl_points": sum(r.pnl_points for r in self.results),
            "total_pnl_amount": sum(r.pnl_amount for r in self.results),
            "average_pnl_points": np.mean([r.pnl_points for r in self.results]),
            "average_winner": np.mean([r.pnl_points for r in winners]) if winners else 0,
            "average_loser": np.mean([r.pnl_points for r in losers]) if losers else 0,
            "largest_winner": max([r.pnl_points for r in winners]) if winners else 0,
            "largest_loser": min([r.pnl_points for r in losers]) if losers else 0,
            
            # Risk Metrics
            "profit_factor": abs(sum(r.pnl_points for r in winners) / 
                               sum(r.pnl_points for r in losers)) if losers else float('inf'),
            "expectancy": self.calculate_expectancy(),
            "max_drawdown_points": self.calculate_max_drawdown(),
            "max_consecutive_wins": self.calculate_max_consecutive(True),
            "max_consecutive_losses": self.calculate_max_consecutive(False),
            
            # Exit Analysis
            "target_exits": len([r for r in self.results if r.exit_reason == "TARGET"]),
            "sl_exits": len([r for r in self.results if r.exit_reason == "STOP_LOSS"]),
            "early_exits": len([r for r in self.results if r.exit_reason == "EARLY_EXIT"]),
            "time_exits": len([r for r in self.results if r.exit_reason == "TIME_EXIT"]),
            
            # Time Analysis
            "avg_trade_duration_mins": self.calculate_avg_duration(),
            "best_hour": self.find_best_trading_hour(),
            "best_day": self.find_best_trading_day(),
        }
        
        return metrics
    
    def calculate_expectancy(self) -> float:
        """Expected points per trade"""
        if not self.results:
            return 0
        
        winners = [r for r in self.results if r.pnl_points > 0]
        losers = [r for r in self.results if r.pnl_points < 0]
        
        win_rate = len(winners) / len(self.results)
        avg_win = np.mean([r.pnl_points for r in winners]) if winners else 0
        avg_loss = abs(np.mean([r.pnl_points for r in losers])) if losers else 0
        
        return (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
    
    def calculate_max_drawdown(self) -> float:
        """Maximum peak-to-trough drawdown in points"""
        cumulative = np.cumsum([r.pnl_points for r in self.results])
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = running_max - cumulative
        return np.max(drawdowns) if len(drawdowns) > 0 else 0
    
    def generate_report(self, output_path: str):
        """Generate comprehensive HTML report"""
        metrics = self.calculate_metrics()
        
        # Create equity curve
        equity_curve = self.plot_equity_curve()
        
        # Create monthly breakdown
        monthly_stats = self.calculate_monthly_stats()
        
        # Create trade distribution
        distribution = self.plot_pnl_distribution()
        
        # Render HTML template
        html = self.render_report_template(
            metrics=metrics,
            equity_curve=equity_curve,
            monthly_stats=monthly_stats,
            distribution=distribution
        )
        
        with open(output_path, 'w') as f:
            f.write(html)
```

### 8.6 Backtest Runner

```python
class BacktestRunner:
    def __init__(
        self,
        strategy_params: StrategyParams,
        simulator_params: SimulatorParams
    ):
        self.strategy = ConvictBarStrategy(strategy_params)
        self.simulator = TradeSimulator(simulator_params)
        self.analytics = None
    
    def run(
        self,
        data: pd.DataFrame,
        start_date: date,
        end_date: date
    ) -> BacktestResult:
        """
        Run backtest over historical data
        """
        trade_results = []
        signals_log = []
        
        # Filter data to date range
        data = data[(data['datetime'].dt.date >= start_date) & 
                    (data['datetime'].dt.date <= end_date)]
        
        # Group by date for daily processing
        for trade_date, day_data in data.groupby(data['datetime'].dt.date):
            
            # Reset daily counters
            self.strategy.daily_manager.reset()
            
            # Process each bar
            for idx, row in day_data.iterrows():
                bar = Bar(
                    datetime=row['datetime'],
                    open=row['open'],
                    high=row['high'],
                    low=row['low'],
                    close=row['close'],
                    volume=row.get('volume', 0)
                )
                
                bar_number = self.get_bar_number(bar.datetime)
                
                # Get signals from strategy
                signals = self.strategy.on_bar_close(bar, bar_number)
                signals_log.extend(signals)
                
                # Process any trade results
                for signal in signals:
                    if signal.type == "TRADE_CLOSED":
                        trade_results.append(signal.result)
            
            # Handle end of day
            if self.strategy.position:
                # Force close at end of day
                result = self.force_close_position(day_data.iloc[-1])
                trade_results.append(result)
        
        # Generate analytics
        self.analytics = BacktestAnalytics(trade_results)
        
        return BacktestResult(
            trade_results=trade_results,
            signals_log=signals_log,
            metrics=self.analytics.calculate_metrics()
        )
    
    def get_bar_number(self, dt: datetime) -> int:
        """Calculate bar number from timestamp"""
        market_open = dt.replace(hour=9, minute=15, second=0)
        minutes_since_open = (dt - market_open).total_seconds() / 60
        return int(minutes_since_open // 5) + 1
```

---

## 9. Data Requirements

### 9.1 Historical Data

| Requirement | Specification |
|-------------|---------------|
| Symbol | NIFTY 50 Index / NIFTY Futures |
| Timeframe | 5-minute OHLCV |
| Minimum History | 6 months (recommended: 2 years) |
| Data Quality | No missing bars, clean OHLC |

### 9.2 Live Data

| Requirement | Specification |
|-------------|---------------|
| Latency | < 1 second from exchange |
| Updates | Real-time tick or 1-second bars |
| Fields | LTP, Open, High, Low, Volume |

### 9.3 Data Sources (India)

| Source | Type | API Available |
|--------|------|---------------|
| Zerodha Kite | Broker | Yes (KiteConnect) |
| Angel One | Broker | Yes (SmartAPI) |
| Upstox | Broker | Yes |
| TrueData | Data Vendor | Yes |
| Global DataFeeds | Data Vendor | Yes |

### 9.4 Sample Data Schema

```python
@dataclass
class Bar:
    datetime: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    
    @property
    def range(self) -> float:
        return self.high - self.low
    
    @property
    def body(self) -> float:
        return abs(self.close - self.open)
    
    @property
    def body_ratio(self) -> float:
        return self.body / self.range if self.range > 0 else 0
    
    @property
    def close_position(self) -> float:
        """0 = closed at low, 1 = closed at high"""
        return (self.close - self.low) / self.range if self.range > 0 else 0.5
    
    @property
    def is_bullish(self) -> bool:
        return self.close > self.open
    
    @property
    def is_bearish(self) -> bool:
        return self.close < self.open
```

---

## 10. System Architecture

### 10.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         CONVICTBAR SYSTEM                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌─────────────────┐         ┌─────────────────┐                   │
│  │   DATA LAYER    │         │   CONFIG LAYER  │                   │
│  │                 │         │                 │                   │
│  │ • Market Data   │         │ • Parameters    │                   │
│  │ • Historical    │         │ • Credentials   │                   │
│  │ • Real-time     │         │ • Settings      │                   │
│  └────────┬────────┘         └────────┬────────┘                   │
│           │                           │                             │
│           ▼                           ▼                             │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                      CORE ENGINE                             │   │
│  │                                                              │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │   │
│  │  │ Bar Builder  │─▶│ Conviction   │─▶│ Signal       │      │   │
│  │  │              │  │ Detector     │  │ Generator    │      │   │
│  │  └──────────────┘  └──────────────┘  └──────────────┘      │   │
│  │                                              │               │   │
│  │                                              ▼               │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │   │
│  │  │ Risk Manager │◀─│ Position     │◀─│ Order        │      │   │
│  │  │              │  │ Manager      │  │ Manager      │      │   │
│  │  └──────────────┘  └──────────────┘  └──────────────┘      │   │
│  │                                                              │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                              │                                      │
│           ┌──────────────────┼──────────────────┐                  │
│           ▼                  ▼                  ▼                  │
│  ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐      │
│  │  ALERT MODULE   │ │ BACKTEST MODULE │ │ LOGGING MODULE  │      │
│  │                 │ │                 │ │                 │      │
│  │ • Telegram      │ │ • Simulation    │ │ • Trade Log     │      │
│  │ • Sound         │ │ • Analytics     │ │ • Error Log     │      │
│  │ • Charts        │ │ • Reports       │ │ • Audit Trail   │      │
│  └─────────────────┘ └─────────────────┘ └─────────────────┘      │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 10.2 Directory Structure

```
convictbar/
├── README.md
├── requirements.txt
├── setup.py
├── config/
│   ├── default_params.yaml
│   ├── credentials.yaml.example
│   └── logging_config.yaml
├── src/
│   ├── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── bar.py              # Bar dataclass and utilities
│   │   ├── conviction.py       # Conviction detection logic
│   │   ├── followthrough.py    # Follow-through validation
│   │   ├── signals.py          # Signal generation
│   │   └── strategy.py         # Main strategy orchestrator
│   ├── risk/
│   │   ├── __init__.py
│   │   ├── position.py         # Position management
│   │   ├── orders.py           # Order management
│   │   └── daily_limits.py     # Daily risk limits
│   ├── data/
│   │   ├── __init__.py
│   │   ├── loader.py           # Historical data loading
│   │   ├── live_feed.py        # Real-time data handling
│   │   └── bar_builder.py      # Tick to bar conversion
│   ├── alerts/
│   │   ├── __init__.py
│   │   ├── telegram.py         # Telegram notifications
│   │   ├── templates.py        # Message templates
│   │   └── charts.py           # Chart generation
│   ├── backtest/
│   │   ├── __init__.py
│   │   ├── simulator.py        # Trade simulation
│   │   ├── runner.py           # Backtest orchestration
│   │   ├── analytics.py        # Performance metrics
│   │   └── reports.py          # Report generation
│   └── utils/
│       ├── __init__.py
│       ├── time_utils.py       # Time/session utilities
│       ├── logging.py          # Logging setup
│       └── validation.py       # Input validation
├── tests/
│   ├── __init__.py
│   ├── test_conviction.py
│   ├── test_followthrough.py
│   ├── test_strategy.py
│   ├── test_backtest.py
│   └── fixtures/
│       └── sample_data.csv
├── scripts/
│   ├── run_live.py             # Live trading runner
│   ├── run_backtest.py         # Backtest runner
│   └── download_data.py        # Data download utility
├── output/
│   ├── backtest_reports/
│   ├── trade_logs/
│   └── charts/
└── docs/
    ├── strategy_guide.md
    └── api_reference.md
```

### 10.3 Technology Stack

| Component | Technology | Rationale |
|-----------|------------|-----------|
| Language | Python 3.10+ | Extensive trading libraries |
| Data Handling | Pandas, NumPy | Standard for financial data |
| Async Operations | asyncio, aiohttp | Non-blocking I/O for alerts |
| Configuration | YAML, Pydantic | Type-safe configuration |
| Logging | Python logging | Standard, configurable |
| Testing | pytest | Comprehensive testing |
| Charting | mplfinance, plotly | Candlestick visualization |
| Reports | Jinja2, HTML | Flexible report generation |

---

## 11. Configuration Parameters

### 11.1 Complete Parameter Reference

```yaml
# config/default_params.yaml

# === CONVICTION DETECTION ===
conviction:
  min_range_points: 15          # Minimum bar range to consider
  max_range_points: 50          # Maximum bar range (skip if exceeded)
  min_body_ratio: 0.50          # Body must be >= 50% of range
  min_close_position_bull: 0.75 # Close in top 25% for bullish
  max_close_position_bear: 0.25 # Close in bottom 25% for bearish

# === FOLLOW-THROUGH ===
followthrough:
  require_confirmation: true     # Require follow-through bar
  min_confirmation_level: "MODERATE"  # MODERATE or STRONG
  trap_buffer: 2.0              # Points beyond signal bar = trap

# === ENTRY ===
entry:
  entry_buffer: 2.0             # Points beyond signal bar for entry
  order_valid_bars: 2           # Bars to wait for entry fill
  entry_window_start: "09:20"   # Earliest entry time (IST)
  entry_window_end: "10:30"     # Latest entry time (IST)

# === STOP LOSS ===
stop_loss:
  sl_buffer: 2.0                # Points beyond signal bar for SL
  min_sl_points: 15             # Minimum SL distance
  max_sl_points: 35             # Maximum SL distance (cap)
  skip_if_sl_exceeds_max: true  # Skip trade if SL too wide

# === TARGET ===
target:
  target_multiplier: 1.0        # Signal bar range * multiplier
  min_target_points: 25         # Minimum target distance

# === TRAILING STOP ===
trailing:
  enable_trailing: true         # Enable trailing stop
  breakeven_at_rr: 1.0          # Move to BE at 1R profit
  breakeven_buffer: 2.0         # Points beyond entry for BE
  trail_at_rr: 2.0              # Start trailing at 2R

# === RISK MANAGEMENT ===
risk:
  fixed_lots: 1                 # Lots per trade
  max_trades_per_day: 3         # Daily trade limit
  max_daily_loss_points: 60     # Daily loss limit
  max_consecutive_losses: 3     # Pause after N losses

# === GAP HANDLING ===
gap:
  small_gap_threshold: 30       # Gap < 30 = normal
  large_gap_threshold: 75       # Gap > 75 = significant
  skip_large_gap_days: false    # Skip trading on large gaps
  fade_large_gaps: false        # Look for reversals on gaps

# === TIME SETTINGS ===
time:
  market_open: "09:15"          # NSE open time
  hard_close: "15:20"           # Mandatory exit time
  timeframe_minutes: 5          # Bar timeframe

# === SIMULATION ===
simulation:
  slippage_points: 1.0          # Entry/exit slippage
  commission_per_lot: 40        # Brokerage + taxes per lot

# === ALERTS ===
alerts:
  telegram_enabled: true
  telegram_bot_token: ""        # Set in credentials.yaml
  telegram_chat_id: ""          # Set in credentials.yaml
  send_conviction_alerts: true
  send_followthrough_alerts: true
  send_chart_images: true
  chart_bars_to_show: 20

# === LOGGING ===
logging:
  level: "INFO"
  log_to_file: true
  log_directory: "output/logs"
  trade_log_format: "csv"       # csv or json
```

### 11.2 Parameter Validation Rules

```python
from pydantic import BaseModel, validator

class ConvictionParams(BaseModel):
    min_range_points: float
    max_range_points: float
    min_body_ratio: float
    min_close_position_bull: float
    max_close_position_bear: float
    
    @validator('min_body_ratio')
    def body_ratio_range(cls, v):
        if not 0.3 <= v <= 0.9:
            raise ValueError('min_body_ratio must be between 0.3 and 0.9')
        return v
    
    @validator('min_close_position_bull')
    def close_position_bull_range(cls, v):
        if not 0.6 <= v <= 0.95:
            raise ValueError('min_close_position_bull must be between 0.6 and 0.95')
        return v
    
    @validator('max_close_position_bear')
    def close_position_bear_range(cls, v):
        if not 0.05 <= v <= 0.4:
            raise ValueError('max_close_position_bear must be between 0.05 and 0.4')
        return v
```

---

## 12. Output Formats

### 12.1 Trade Log (CSV)

```csv
trade_id,date,direction,signal_bar_time,entry_time,exit_time,entry_price,exit_price,stop_loss,target,exit_reason,pnl_points,pnl_amount,bars_held,signal_bar_range,signal_bar_body_ratio,signal_bar_close_position,followthrough_type
1,2026-01-15,LONG,09:20,09:27,09:45,24150.00,24185.00,24115.00,24185.00,TARGET,35.00,875.00,4,35,0.65,0.82,MODERATE_CONFIRMATION
2,2026-01-15,SHORT,10:05,10:12,10:25,24200.00,24225.00,24230.00,24170.00,STOP_LOSS,-25.00,-625.00,3,30,0.58,0.18,STRONG_CONFIRMATION
```

### 12.2 Backtest Summary Report (JSON)

```json
{
  "backtest_info": {
    "symbol": "NIFTY",
    "timeframe": "5min",
    "start_date": "2025-07-01",
    "end_date": "2026-01-15",
    "trading_days": 128,
    "parameters": {
      "min_body_ratio": 0.50,
      "min_close_position_bull": 0.75,
      "max_sl_points": 35
    }
  },
  "performance_metrics": {
    "total_trades": 156,
    "winning_trades": 89,
    "losing_trades": 67,
    "win_rate_pct": 57.05,
    "total_pnl_points": 1245.50,
    "total_pnl_amount": 31137.50,
    "average_pnl_points": 7.98,
    "profit_factor": 1.72,
    "expectancy_points": 7.98,
    "max_drawdown_points": 185.00,
    "max_consecutive_wins": 7,
    "max_consecutive_losses": 4
  },
  "exit_analysis": {
    "target_exits": 89,
    "sl_exits": 52,
    "early_exits": 12,
    "time_exits": 3
  },
  "monthly_breakdown": [
    {"month": "2025-07", "trades": 22, "pnl": 145.50, "win_rate": 59.09},
    {"month": "2025-08", "trades": 24, "pnl": 210.00, "win_rate": 62.50}
  ]
}
```

### 12.3 Alert Message Formats

See Section 7.3 for detailed alert templates.

---

## 13. Edge Cases & Error Handling

### 13.1 Market Scenarios

| Scenario | Handling |
|----------|----------|
| **No conviction bar detected all day** | Log "No signal day", no trades |
| **Multiple conviction bars same direction** | Take first valid signal only |
| **Conviction bar exceeds max range** | Skip trade, log reason |
| **Gap opens beyond previous day range** | Apply gap handling rules |
| **Market halt during position** | Hold position, resume monitoring |
| **Data feed disconnection** | Alert, attempt reconnect, pause trading |
| **Entry order not filled within window** | Cancel order, log expiry |

### 13.2 Technical Errors

```python
class ErrorHandler:
    def __init__(self, alert_service):
        self.alert_service = alert_service
    
    def handle_data_error(self, error: Exception):
        """Handle data feed errors"""
        logging.error(f"Data error: {error}")
        self.alert_service.send_critical_alert(
            "DATA_ERROR",
            f"Data feed issue: {str(error)}"
        )
        # Attempt reconnection
        return self.attempt_reconnect()
    
    def handle_order_error(self, error: Exception, order: PendingOrder):
        """Handle order placement/execution errors"""
        logging.error(f"Order error: {error}")
        self.alert_service.send_critical_alert(
            "ORDER_ERROR",
            f"Order failed: {str(error)}\nOrder: {order}"
        )
        # Mark order as failed
        order.status = "FAILED"
        return False
    
    def handle_unknown_error(self, error: Exception):
        """Handle unexpected errors"""
        logging.critical(f"Unknown error: {error}", exc_info=True)
        self.alert_service.send_critical_alert(
            "SYSTEM_ERROR",
            f"Critical error: {str(error)}\nSystem pausing."
        )
        # Pause trading, require manual intervention
        return "PAUSE"
```

### 13.3 Data Validation

```python
def validate_bar(bar: Bar) -> Tuple[bool, Optional[str]]:
    """Validate bar data integrity"""
    
    # Check for invalid prices
    if any(p <= 0 for p in [bar.open, bar.high, bar.low, bar.close]):
        return False, "Invalid price (zero or negative)"
    
    # Check OHLC relationship
    if bar.low > bar.open or bar.low > bar.close:
        return False, "Low is higher than open/close"
    
    if bar.high < bar.open or bar.high < bar.close:
        return False, "High is lower than open/close"
    
    # Check for extreme moves (data error likely)
    if bar.range > 200:  # 200 points in 5 min is extreme
        return False, "Extreme range - possible data error"
    
    return True, None
```

---

## 14. Future Enhancements

### 14.1 Phase 2 Features

| Feature | Description | Priority |
|---------|-------------|----------|
| **Multi-instrument support** | Add BANKNIFTY, FINNIFTY | High |
| **Options integration** | Trade ATM options based on signals | High |
| **Machine learning filter** | ML model to filter low-quality signals | Medium |
| **Sentiment integration** | Factor in VIX, Put-Call ratio | Medium |
| **Auto-execution** | Direct broker integration for auto-trading | Medium |

### 14.2 Phase 3 Features

| Feature | Description | Priority |
|---------|-------------|----------|
| **Web dashboard** | Real-time monitoring interface | Low |
| **Mobile app** | Companion app for alerts | Low |
| **Multi-timeframe analysis** | Combine 5min with 15min context | Medium |
| **Portfolio mode** | Trade multiple strategies simultaneously | Low |

---

## Appendix A: Glossary

| Term | Definition |
|------|------------|
| ATR | Average True Range - measure of volatility |
| Bar | A single candlestick representing OHLC over a time period |
| Breakout | Price moving beyond a defined level |
| DXA | Document eXchange unit of measurement (1/20 of a point) |
| Follow-through | Continuation of price movement in the same direction |
| IST | Indian Standard Time (UTC+5:30) |
| NSE | National Stock Exchange of India |
| OHLC | Open, High, Low, Close |
| R | One unit of risk (equal to stop loss distance) |
| RR | Risk-Reward ratio |
| SL | Stop Loss |
| Trap | A false breakout that reverses |

---

## Appendix B: Sample Conviction Bar Analysis

### Example 1: Strong Bullish Conviction

```
Bar Data:
  Time: 09:20-09:25
  Open: 24,120
  High: 24,155
  Low: 24,115
  Close: 24,152

Calculations:
  Range: 24,155 - 24,115 = 40 points
  Body: 24,152 - 24,120 = 32 points
  Body Ratio: 32 / 40 = 0.80 (80%)
  Close Position: (24,152 - 24,115) / 40 = 0.925 (92.5%)

Assessment:
  ✅ Range >= 15 (40 >= 15)
  ✅ Range <= 50 (40 <= 50)
  ✅ Body Ratio >= 0.50 (0.80 >= 0.50)
  ✅ Close Position >= 0.75 (0.925 >= 0.75)
  
Result: STRONG BULLISH CONVICTION BAR
```

### Example 2: Failed Conviction (Close Position)

```
Bar Data:
  Time: 09:20-09:25
  Open: 24,120
  High: 24,160
  Low: 24,110
  Close: 24,135

Calculations:
  Range: 50 points
  Body: 15 points
  Body Ratio: 0.30 (30%)
  Close Position: 0.50 (50%)

Assessment:
  ✅ Range >= 15 (50 >= 15)
  ✅ Range <= 50 (50 <= 50)
  ❌ Body Ratio >= 0.50 (0.30 < 0.50)
  ❌ Close Position >= 0.75 (0.50 < 0.75)
  
Result: NOT A CONVICTION BAR (Doji-like, indecision)
```

---

## Document Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-01-16 | Raja/Claude | Initial document |

---

**End of Design Document**
