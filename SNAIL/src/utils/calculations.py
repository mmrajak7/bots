"""
SNAIL Calculations Module

Financial calculations for Iron Fly strategy including P&L, Greeks approximations,
and transaction charges.

@file        calculations.py
@description Financial calculations for Iron Fly strategy
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 8
"""

from datetime import date, datetime, timedelta
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass
from loguru import logger

from src.utils.config import get_charges_config, get_trading_config


# =============================================================================
# CONSTANTS
# =============================================================================

# NIFTY lot size
NIFTY_LOT_SIZE = 75

# Strike interval
NIFTY_STRIKE_INTERVAL = 100  # ATM rounds to nearest 100

# Default charges (overridden by config)
DEFAULT_STT_RATE = 0.000625  # 0.0625% on sell side premium
DEFAULT_EXCHANGE_TXN_RATE = 0.0000505  # 0.00505%
DEFAULT_GST_RATE = 0.18  # 18% on brokerage + exchange txn
DEFAULT_SEBI_RATE = 0.000001  # Rs 1 per crore
DEFAULT_STAMP_DUTY_RATE = 0.00003  # 0.003% on buy side


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class IronFlyMetrics:
    """
    Iron Fly position metrics.

    Attributes:
        entry_credit: Net premium received at entry
        max_profit: Maximum profit (entry credit)
        max_loss: Maximum loss (wing distance - entry credit)
        breakeven_upper: Upper breakeven point
        breakeven_lower: Lower breakeven point
        profit_target: 50% profit target
        stop_loss_level: 50% max loss level
        current_pnl: Current unrealized P&L
        pnl_percentage: P&L as percentage of max profit
    """
    entry_credit: float
    max_profit: float
    max_loss: float
    breakeven_upper: float
    breakeven_lower: float
    profit_target: float
    stop_loss_level: float
    current_pnl: float = 0.0
    pnl_percentage: float = 0.0


@dataclass
class TransactionCharges:
    """
    Transaction charges breakdown.

    Attributes:
        stt: Securities Transaction Tax
        exchange_txn: Exchange transaction charges
        gst: GST on charges
        sebi_charges: SEBI turnover charges
        stamp_duty: Stamp duty
        total: Total charges
    """
    stt: float
    exchange_txn: float
    gst: float
    sebi_charges: float
    stamp_duty: float
    total: float


# =============================================================================
# STRIKE CALCULATIONS
# =============================================================================

def calculate_atm_strike(spot_price: float, interval: int = NIFTY_STRIKE_INTERVAL) -> int:
    """
    Calculate ATM strike price.

    Args:
        spot_price: Current spot price
        interval: Strike interval (default 50)

    Returns:
        ATM strike price
    """
    return round(spot_price / interval) * interval


def calculate_wing_distance(
    ce_premium: float,
    pe_premium: float,
    round_to: int = 100,
    min_distance: int = 300
) -> int:
    """
    Calculate dynamic wing distance based on straddle premium.

    Wing distance = Straddle premium rounded to nearest 100, with minimum bound.

    Args:
        ce_premium: ATM CE premium
        pe_premium: ATM PE premium
        round_to: Rounding interval (default 100)
        min_distance: Minimum wing distance to prevent division by zero (default 200)

    Returns:
        Wing distance in points (at least min_distance)
    """
    straddle_premium = ce_premium + pe_premium
    calculated = round(straddle_premium / round_to) * round_to
    return max(calculated, min_distance)


def get_iron_fly_strikes(
    atm_strike: int,
    wing_distance: int
) -> Dict[str, int]:
    """
    Get all strikes for Iron Fly position.

    Args:
        atm_strike: ATM strike price
        wing_distance: Wing distance in points

    Returns:
        Dictionary with all strike prices
    """
    return {
        'atm_strike': atm_strike,
        'wing_ce_strike': atm_strike + wing_distance,
        'wing_pe_strike': atm_strike - wing_distance
    }


# =============================================================================
# P&L CALCULATIONS
# =============================================================================

def calculate_iron_fly_pnl(
    entry_credit: float,
    current_straddle_value: float,
    current_wing_value: float,
    quantity: int
) -> float:
    """
    Calculate current Iron Fly P&L.

    P&L = Entry Credit - Current Cost to Close
    Current Cost = (Buy back straddle) - (Sell wings)

    Args:
        entry_credit: Net credit received at entry (per lot)
        current_straddle_value: Current straddle ask price (to buy back)
        current_wing_value: Current wing bid price (to sell)
        quantity: Position quantity

    Returns:
        Unrealized P&L in INR
    """
    # Cost to close = Buy straddle - Sell wings
    exit_debit = current_straddle_value - current_wing_value

    # P&L = Entry credit - Exit debit
    pnl_per_lot = entry_credit - exit_debit

    return pnl_per_lot * quantity


def calculate_iron_fly_metrics(
    atm_strike: int,
    wing_distance: int,
    ce_sell_price: float,
    pe_sell_price: float,
    wing_ce_buy_price: float,
    wing_pe_buy_price: float,
    quantity: int = NIFTY_LOT_SIZE
) -> IronFlyMetrics:
    """
    Calculate complete Iron Fly metrics.

    Args:
        atm_strike: ATM strike price
        wing_distance: Wing distance in points
        ce_sell_price: Straddle CE sell price
        pe_sell_price: Straddle PE sell price
        wing_ce_buy_price: Wing CE buy price
        wing_pe_buy_price: Wing PE buy price
        quantity: Position quantity

    Returns:
        Complete Iron Fly metrics
    """
    # Entry credit per share
    entry_credit = (ce_sell_price + pe_sell_price) - (wing_ce_buy_price + wing_pe_buy_price)

    # Max profit = entry credit (achieved if NIFTY expires at ATM)
    max_profit = entry_credit * quantity

    # Max loss = wing distance - entry credit (per share)
    max_loss = (wing_distance - entry_credit) * quantity

    # Breakeven points
    breakeven_upper = atm_strike + entry_credit
    breakeven_lower = atm_strike - entry_credit

    # Targets (50% rule)
    profit_target = max_profit * 0.5
    stop_loss_level = max_loss * 0.5

    return IronFlyMetrics(
        entry_credit=entry_credit * quantity,
        max_profit=max_profit,
        max_loss=max_loss,
        breakeven_upper=breakeven_upper,
        breakeven_lower=breakeven_lower,
        profit_target=profit_target,
        stop_loss_level=stop_loss_level
    )


def calculate_position_pnl(
    entry_straddle_credit: float,
    entry_wing_debit: float,
    current_straddle_ask: float,
    current_wing_bid: float,
    quantity: int
) -> Tuple[float, float]:
    """
    Calculate position P&L and percentage.

    Args:
        entry_straddle_credit: Credit received from selling straddle
        entry_wing_debit: Debit paid for buying wings
        current_straddle_ask: Current ask to buy back straddle
        current_wing_bid: Current bid to sell wings
        quantity: Position quantity

    Returns:
        (P&L in INR, P&L percentage of max profit)
    """
    entry_credit = entry_straddle_credit - entry_wing_debit
    exit_debit = current_straddle_ask - current_wing_bid

    pnl = (entry_credit - exit_debit) * quantity
    max_profit = entry_credit * quantity

    pnl_pct = (pnl / max_profit * 100) if max_profit > 0 else 0

    return pnl, pnl_pct


# =============================================================================
# TRANSACTION CHARGES
# =============================================================================

def calculate_transaction_charges(
    buy_value: float,
    sell_value: float,
    config: Optional[Dict] = None
) -> TransactionCharges:
    """
    Calculate all transaction charges for options trade.

    Args:
        buy_value: Total buy side value (premium paid)
        sell_value: Total sell side value (premium received)
        config: Optional charges config (uses default if None)

    Returns:
        TransactionCharges breakdown
    """
    if config is None:
        config = get_charges_config()

    # Get rates from config (keys match config/config.yaml)
    stt_rate = config.get('stt_sell_rate', DEFAULT_STT_RATE)
    exchange_rate = config.get('exchange_txn_rate', DEFAULT_EXCHANGE_TXN_RATE)
    gst_rate = config.get('gst_rate', DEFAULT_GST_RATE)
    sebi_rate = config.get('sebi_per_crore', DEFAULT_SEBI_RATE) / 10000000  # Convert Rs per crore to rate
    stamp_rate = config.get('stamp_duty_buy_rate', DEFAULT_STAMP_DUTY_RATE)
    brokerage_per_order = config.get('brokerage_per_order', 0)

    turnover = buy_value + sell_value

    # STT on sell side premium only (for options)
    stt = sell_value * stt_rate

    # Exchange transaction charges on turnover
    exchange_txn = turnover * exchange_rate

    # SEBI charges
    sebi_charges = turnover * sebi_rate

    # Stamp duty on buy side only
    stamp_duty = buy_value * stamp_rate

    # GST on brokerage + exchange charges
    gst = (brokerage_per_order + exchange_txn) * gst_rate

    total = stt + exchange_txn + gst + sebi_charges + stamp_duty + brokerage_per_order

    return TransactionCharges(
        stt=round(stt, 2),
        exchange_txn=round(exchange_txn, 2),
        gst=round(gst, 2),
        sebi_charges=round(sebi_charges, 2),
        stamp_duty=round(stamp_duty, 2),
        total=round(total, 2)
    )


def calculate_iron_fly_charges(
    straddle_ce_premium: float,
    straddle_pe_premium: float,
    wing_ce_premium: float,
    wing_pe_premium: float,
    quantity: int
) -> TransactionCharges:
    """
    Calculate transaction charges for Iron Fly entry.

    Iron Fly entry:
    - SELL CE + PE (receive premium - sell side)
    - BUY Wing CE + PE (pay premium - buy side)

    Args:
        straddle_ce_premium: CE straddle premium
        straddle_pe_premium: PE straddle premium
        wing_ce_premium: CE wing premium
        wing_pe_premium: PE wing premium
        quantity: Position quantity

    Returns:
        Transaction charges for entry
    """
    sell_value = (straddle_ce_premium + straddle_pe_premium) * quantity
    buy_value = (wing_ce_premium + wing_pe_premium) * quantity

    return calculate_transaction_charges(buy_value, sell_value)


# =============================================================================
# POSITION SIZING
# =============================================================================

def calculate_lot_size(
    allocated_capital: float,
    margin_per_lot: float,
    max_lots: int = 10
) -> int:
    """
    Calculate number of lots based on capital.

    Args:
        allocated_capital: Capital allocated for trading
        margin_per_lot: Margin required per lot
        max_lots: Maximum lots allowed

    Returns:
        Number of lots to trade
    """
    if margin_per_lot <= 0:
        return 1

    lots = int(allocated_capital / margin_per_lot)
    return min(max(lots, 1), max_lots)


def calculate_margin_requirement(
    wing_distance: int,
    quantity: int,
    margin_factor: float = 0.8
) -> float:
    """
    Estimate margin requirement for Iron Fly.

    This is an approximation. Actual margin depends on exchange rules.

    Args:
        wing_distance: Wing distance in points
        quantity: Position quantity
        margin_factor: Margin factor (default 0.8 for Iron Fly)

    Returns:
        Estimated margin requirement in INR
    """
    # Max loss = wing distance per share
    max_loss = wing_distance * quantity

    # Iron Fly typically requires less margin due to defined risk
    return max_loss * margin_factor


# =============================================================================
# TIME & EXPIRY CALCULATIONS
# =============================================================================

def calculate_dte(expiry: date, from_date: Optional[date] = None) -> int:
    """
    Calculate days to expiry.

    Args:
        expiry: Expiry date
        from_date: Date to calculate from (default today)

    Returns:
        Days to expiry
    """
    if from_date is None:
        from_date = date.today()

    return (expiry - from_date).days


def is_within_trading_hours(
    time_obj: Optional[datetime] = None,
    market_open: str = "09:15",
    market_close: str = "15:30"
) -> bool:
    """
    Check if current time is within trading hours.

    Args:
        time_obj: Time to check (default now)
        market_open: Market open time
        market_close: Market close time

    Returns:
        True if within trading hours
    """
    if time_obj is None:
        time_obj = datetime.now()

    current_time = time_obj.time()

    open_time = datetime.strptime(market_open, "%H:%M").time()
    close_time = datetime.strptime(market_close, "%H:%M").time()

    return open_time <= current_time <= close_time


def minutes_since_market_open(
    time_obj: Optional[datetime] = None,
    market_open: str = "09:15"
) -> int:
    """
    Calculate minutes since market open.

    Args:
        time_obj: Time to check (default now)
        market_open: Market open time

    Returns:
        Minutes since open (negative if before open)
    """
    if time_obj is None:
        time_obj = datetime.now()

    open_time = datetime.strptime(market_open, "%H:%M").time()
    open_datetime = datetime.combine(time_obj.date(), open_time)

    diff = time_obj - open_datetime
    return int(diff.total_seconds() / 60)


def minutes_until_market_close(
    time_obj: Optional[datetime] = None,
    market_close: str = "15:30"
) -> int:
    """
    Calculate minutes until market close.

    Args:
        time_obj: Time to check (default now)
        market_close: Market close time

    Returns:
        Minutes until close (negative if after close)
    """
    if time_obj is None:
        time_obj = datetime.now()

    close_time = datetime.strptime(market_close, "%H:%M").time()
    close_datetime = datetime.combine(time_obj.date(), close_time)

    diff = close_datetime - time_obj
    return int(diff.total_seconds() / 60)


# =============================================================================
# RISK CALCULATIONS
# =============================================================================

def calculate_risk_reward(max_profit: float, max_loss: float) -> float:
    """
    Calculate risk-reward ratio.

    Args:
        max_profit: Maximum potential profit
        max_loss: Maximum potential loss

    Returns:
        Risk-reward ratio (e.g., 0.5 means 1:2 reward:risk)
    """
    if max_loss <= 0:
        return 0

    return max_profit / max_loss


def calculate_probability_of_profit(
    entry_credit: float,
    wing_distance: int
) -> float:
    """
    Estimate probability of profit for Iron Fly.

    Simple approximation based on breakeven width.

    Args:
        entry_credit: Net credit received per share
        wing_distance: Wing distance in points

    Returns:
        Estimated probability of profit (0-1)
    """
    # Profit zone is 2x entry credit wide (both sides of ATM)
    profit_zone = 2 * entry_credit

    # Total possible range is 2x wing distance
    total_range = 2 * wing_distance

    if total_range <= 0:
        return 0

    return min(profit_zone / total_range, 1.0)


def is_approaching_wing(
    spot_price: float,
    atm_strike: int,
    wing_distance: int,
    threshold_pct: float = 0.75
) -> Tuple[bool, str]:
    """
    Check if spot price is approaching wing strikes.

    Args:
        spot_price: Current spot price
        atm_strike: ATM strike price
        wing_distance: Wing distance in points
        threshold_pct: Threshold percentage of wing distance

    Returns:
        (is_approaching, direction: "CE" or "PE" or "")
    """
    threshold = wing_distance * threshold_pct

    ce_wing = atm_strike + wing_distance
    pe_wing = atm_strike - wing_distance

    distance_to_ce = ce_wing - spot_price
    distance_to_pe = spot_price - pe_wing

    if distance_to_ce <= (wing_distance - threshold):
        return True, "CE"
    elif distance_to_pe <= (wing_distance - threshold):
        return True, "PE"

    return False, ""


# =============================================================================
# ATR CALCULATION & BIG MOVE DETECTION (TDD Section 5.3)
# =============================================================================

@dataclass
class BigMoveDetection:
    """
    Big move detection result.

    TDD Section 5.3: Big Move is SOFT exit trigger requiring Claude advisory.

    Attributes:
        is_big_move: Whether any big move condition is met
        conditions_met: List of conditions that triggered
        day_range: Current day range in points
        atr_14: 14-day ATR
        wing_proximity: Percentage distance to nearest wing
        move_from_open: Percentage move from day open
        severity: 'NORMAL', 'WARNING', or 'CRITICAL'
    """
    is_big_move: bool
    conditions_met: list
    day_range: float
    atr_14: float
    wing_proximity: float
    move_from_open: float
    severity: str


def calculate_atr(
    historical_data: list,
    period: int = 14
) -> float:
    """
    Calculate Average True Range (ATR) from historical OHLC data.

    TDD Section 5.3: ATR used for Big Move Detection.

    True Range = max(High-Low, abs(High-PrevClose), abs(Low-PrevClose))
    ATR = Simple Moving Average of True Range over period

    Args:
        historical_data: List of dicts with 'high', 'low', 'close' keys
            Expected format from Kite historical_data API
        period: ATR period (default 14)

    Returns:
        ATR value in points

    Example:
        >>> data = kite.historical_data(256265, from_date, to_date, "day")
        >>> atr = calculate_atr(data, period=14)
    """
    if not historical_data or len(historical_data) < period + 1:
        logger.warning(f"Insufficient data for {period}-day ATR: {len(historical_data)} candles")
        return 0.0

    true_ranges = []

    for i in range(1, len(historical_data)):
        current = historical_data[i]
        previous = historical_data[i - 1]

        high = current.get('high', 0)
        low = current.get('low', 0)
        prev_close = previous.get('close', 0)

        # True Range = max of three values
        tr1 = high - low
        tr2 = abs(high - prev_close)
        tr3 = abs(low - prev_close)

        true_range = max(tr1, tr2, tr3)
        true_ranges.append(true_range)

    # Take last 'period' true ranges for ATR
    if len(true_ranges) < period:
        return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0

    recent_tr = true_ranges[-period:]
    atr = sum(recent_tr) / period

    return round(atr, 2)


def detect_big_move(
    nifty_spot: float,
    day_open: float,
    day_high: float,
    day_low: float,
    atr_14: float,
    atm_strike: int,
    wing_distance: int
) -> BigMoveDetection:
    """
    Detect big market move that may require exit advisory.

    TDD Section 5.3: Big Move is SOFT exit trigger.

    Big Move Conditions (any triggers advisory):
    1. Volatility Spike: Day range > 2 × ATR(14)
    2. Wing Proximity: Spot within 30% of wing distance from wing strike
    3. Sudden Move: Spot moved ≥1% from day open

    Args:
        nifty_spot: Current NIFTY spot price
        day_open: Day's opening price
        day_high: Day's high
        day_low: Day's low
        atr_14: 14-day ATR
        atm_strike: Position ATM strike
        wing_distance: Position wing distance

    Returns:
        BigMoveDetection result with all metrics

    Example:
        >>> result = detect_big_move(
        ...     nifty_spot=24500,
        ...     day_open=24400,
        ...     day_high=24550,
        ...     day_low=24350,
        ...     atr_14=150,
        ...     atm_strike=24400,
        ...     wing_distance=300
        ... )
        >>> if result.is_big_move:
        ...     trigger_claude_advisory(result)
    """
    conditions_met = []
    severity = 'NORMAL'

    # Calculate metrics
    day_range = day_high - day_low
    upper_wing = atm_strike + wing_distance
    lower_wing = atm_strike - wing_distance

    # Wing proximity calculation
    distance_to_upper = abs(upper_wing - nifty_spot)
    distance_to_lower = abs(nifty_spot - lower_wing)
    distance_to_nearest_wing = min(distance_to_upper, distance_to_lower)
    wing_proximity = ((wing_distance - distance_to_nearest_wing) / wing_distance) * 100 if wing_distance > 0 else 0

    # Move from open calculation
    move_from_open = abs(nifty_spot - day_open) / day_open * 100 if day_open > 0 else 0

    # Condition 1: Volatility Spike (Day range > 2 × ATR)
    if atr_14 > 0 and day_range > 2 * atr_14:
        conditions_met.append(f"Volatility spike: Day range ({day_range:.0f}) > 2×ATR ({2*atr_14:.0f})")
        severity = 'WARNING'

    # Condition 2: Wing Proximity (≥30%)
    if wing_proximity >= 30:
        conditions_met.append(f"Wing proximity: {wing_proximity:.1f}% (threshold: 30%)")
        if wing_proximity >= 40:
            severity = 'CRITICAL'
        else:
            severity = max(severity, 'WARNING', key=['NORMAL', 'WARNING', 'CRITICAL'].index)

    # Condition 3: Sudden Move (≥1% from open)
    if move_from_open >= 1.0:
        conditions_met.append(f"Sudden move: {move_from_open:.2f}% from open (threshold: 1%)")
        severity = max(severity, 'WARNING', key=['NORMAL', 'WARNING', 'CRITICAL'].index)

    is_big_move = len(conditions_met) > 0

    return BigMoveDetection(
        is_big_move=is_big_move,
        conditions_met=conditions_met,
        day_range=day_range,
        atr_14=atr_14,
        wing_proximity=wing_proximity,
        move_from_open=move_from_open,
        severity=severity
    )


def calculate_wing_proximity(
    spot_price: float,
    atm_strike: int,
    wing_distance: int
) -> Tuple[float, str]:
    """
    Calculate proximity to nearest wing as percentage.

    TDD Section 5.3:
    - 30% proximity: Early warning
    - 40% proximity: Standalone advisory trigger

    Args:
        spot_price: Current spot price
        atm_strike: ATM strike
        wing_distance: Wing distance

    Returns:
        (proximity_pct, direction) where direction is 'UP' or 'DOWN'
    """
    upper_wing = atm_strike + wing_distance
    lower_wing = atm_strike - wing_distance

    distance_to_upper = upper_wing - spot_price
    distance_to_lower = spot_price - lower_wing

    if distance_to_upper < distance_to_lower:
        # Closer to upper wing
        proximity = ((wing_distance - distance_to_upper) / wing_distance) * 100
        return proximity, 'UP'
    else:
        # Closer to lower wing
        proximity = ((wing_distance - distance_to_lower) / wing_distance) * 100
        return proximity, 'DOWN'


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("SNAIL Calculations Test")
    print("=" * 60)

    # Test ATM strike calculation
    print("\n[1] ATM Strike Calculation:")
    spots = [24073.45, 24150.00, 24199.99, 24200.01]
    for spot in spots:
        atm = calculate_atm_strike(spot)
        print(f"    Spot {spot:,.2f} -> ATM {atm}")

    # Test wing distance
    print("\n[2] Wing Distance Calculation:")
    test_cases = [(170, 130), (210, 190), (150, 150)]
    for ce, pe in test_cases:
        wing = calculate_wing_distance(ce, pe)
        print(f"    CE={ce}, PE={pe} -> Wing distance: {wing}")

    # Test Iron Fly metrics
    print("\n[3] Iron Fly Metrics:")
    metrics = calculate_iron_fly_metrics(
        atm_strike=24150,
        wing_distance=300,
        ce_sell_price=170,
        pe_sell_price=130,
        wing_ce_buy_price=45,
        wing_pe_buy_price=35,
        quantity=75
    )
    print(f"    Entry credit: ₹{metrics.entry_credit:,.2f}")
    print(f"    Max profit: ₹{metrics.max_profit:,.2f}")
    print(f"    Max loss: ₹{metrics.max_loss:,.2f}")
    print(f"    Profit target (50%): ₹{metrics.profit_target:,.2f}")
    print(f"    Stop loss (50%): ₹{metrics.stop_loss_level:,.2f}")
    print(f"    Breakeven: {metrics.breakeven_lower:.2f} - {metrics.breakeven_upper:.2f}")

    # Test transaction charges
    print("\n[4] Transaction Charges:")
    charges = calculate_iron_fly_charges(
        straddle_ce_premium=170,
        straddle_pe_premium=130,
        wing_ce_premium=45,
        wing_pe_premium=35,
        quantity=75
    )
    print(f"    STT: ₹{charges.stt:,.2f}")
    print(f"    Exchange: ₹{charges.exchange_txn:,.2f}")
    print(f"    GST: ₹{charges.gst:,.2f}")
    print(f"    SEBI: ₹{charges.sebi_charges:,.2f}")
    print(f"    Stamp: ₹{charges.stamp_duty:,.2f}")
    print(f"    Total: ₹{charges.total:,.2f}")

    # Test time calculations
    print("\n[5] Time Calculations:")
    print(f"    Within trading hours: {is_within_trading_hours()}")
    print(f"    Minutes since open: {minutes_since_market_open()}")
    print(f"    Minutes until close: {minutes_until_market_close()}")

    # Test probability
    print("\n[6] Probability of Profit:")
    pop = calculate_probability_of_profit(220, 300)
    print(f"    Entry credit=220, Wing=300: {pop:.1%}")

    print("\n" + "=" * 60)
    print("Calculations test complete!")
    print("=" * 60 + "\n")
