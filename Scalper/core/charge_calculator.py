"""
F&O Options Charge Calculator for NSE

Calculates trading charges for round-trip (buy + sell) trades.
Brokerage is zero (Kotak Neo API).

Charge rates as per 2024:
- Exchange Transaction Charge: 0.0355% on total turnover
- STT (Securities Transaction Tax): 0.1% on sell side only
- Stamp Duty: 0.003% on buy side only
- SEBI Charges: ₹10 per crore (0.0001%)
- GST: 18% on (Exchange charges + SEBI charges)
"""

from dataclasses import dataclass
from typing import Optional


# Charge rates (as percentages, multiply by 100 for display)
EXCHANGE_TXN_RATE = 0.000355      # 0.0355%
STT_RATE = 0.001                   # 0.1% on sell side
STAMP_DUTY_RATE = 0.00003          # 0.003% on buy side
SEBI_RATE = 0.000001               # ₹10 per crore = 0.0001%
GST_RATE = 0.18                    # 18% on (exchange + SEBI)


@dataclass
class ChargeBreakdown:
    """Breakdown of all trading charges."""
    exchange_txn: float
    stt: float
    stamp_duty: float
    sebi: float
    gst: float
    total: float
    breakeven_points: float  # Points needed to cover charges


def calculate_charges(
    price: float,
    quantity: int,
    exit_price: Optional[float] = None
) -> ChargeBreakdown:
    """
    Calculate round-trip charges for F&O options trade.

    Args:
        price: Entry price (buy price)
        quantity: Total quantity (lot_size * lots)
        exit_price: Exit price (if None, assumes same as entry for breakeven calc)

    Returns:
        ChargeBreakdown with all charges and breakeven points
    """
    if exit_price is None:
        exit_price = price

    # Turnover calculations
    buy_turnover = price * quantity
    sell_turnover = exit_price * quantity
    total_turnover = buy_turnover + sell_turnover

    # Individual charges
    exchange_txn = total_turnover * EXCHANGE_TXN_RATE
    stt = sell_turnover * STT_RATE  # STT only on sell side
    stamp_duty = buy_turnover * STAMP_DUTY_RATE  # Stamp only on buy side
    sebi = total_turnover * SEBI_RATE

    # GST on exchange charges + SEBI (not on STT/stamp duty)
    gst = (exchange_txn + sebi) * GST_RATE

    # Total charges
    total = exchange_txn + stt + stamp_duty + sebi + gst

    # Breakeven points = charges / quantity
    breakeven_points = total / quantity if quantity > 0 else 0

    return ChargeBreakdown(
        exchange_txn=round(exchange_txn, 2),
        stt=round(stt, 2),
        stamp_duty=round(stamp_duty, 2),
        sebi=round(sebi, 2),
        gst=round(gst, 2),
        total=round(total, 2),
        breakeven_points=round(breakeven_points, 2)
    )


def get_breakeven_points(price: float, quantity: int) -> float:
    """
    Quick helper to get just the breakeven points.

    Args:
        price: Entry price
        quantity: Total quantity

    Returns:
        Breakeven points (rounded to 1 decimal)
    """
    charges = calculate_charges(price, quantity)
    return round(charges.breakeven_points, 1)
