"""
SNAIL Position Reconciliation

Cross-checks Kite positions against DB records to detect mismatches.
Alert-only - does not auto-fix. Called at startup and after failures.

@file        reconciliation.py
@description Kite-DB position reconciliation utility
@author      SNAIL Development Team
@created     2026-02-24
@version     1.0.0
"""

from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from loguru import logger


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class PositionMismatch:
    """A single mismatch between Kite and DB."""
    mismatch_type: str  # 'orphaned_on_kite', 'ghost_in_db', 'quantity_mismatch'
    tradingsymbol: str
    kite_quantity: int = 0
    db_quantity: int = 0
    db_position_id: Optional[int] = None
    db_leg_type: Optional[str] = None
    details: str = ""


@dataclass
class ReconciliationResult:
    """Result of position reconciliation."""
    has_mismatches: bool = False
    mismatches: List[PositionMismatch] = field(default_factory=list)
    kite_nifty_positions: int = 0
    db_positions_checked: int = 0
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def summary(self) -> str:
        """Human-readable summary of mismatches."""
        if not self.mismatches:
            return "No mismatches"

        orphaned = [m for m in self.mismatches if m.mismatch_type == 'orphaned_on_kite']
        ghosts = [m for m in self.mismatches if m.mismatch_type == 'ghost_in_db']
        qty_mismatches = [m for m in self.mismatches if m.mismatch_type == 'quantity_mismatch']

        parts = []
        if orphaned:
            symbols = [m.tradingsymbol for m in orphaned]
            parts.append(f"Orphaned on Kite: {', '.join(symbols)}")
        if ghosts:
            symbols = [f"{m.tradingsymbol}(pos={m.db_position_id})" for m in ghosts]
            parts.append(f"Ghost in DB: {', '.join(symbols)}")
        if qty_mismatches:
            details = [
                f"{m.tradingsymbol}(kite={m.kite_quantity},db={m.db_quantity})"
                for m in qty_mismatches
            ]
            parts.append(f"Qty mismatch: {', '.join(details)}")

        return " | ".join(parts)


# =============================================================================
# RECONCILIATION FUNCTION
# =============================================================================

def reconcile_positions(
    kite: Any,
    telegram: Optional[Any] = None
) -> ReconciliationResult:
    """
    Cross-check Kite positions against DB records.

    Checks:
    1. NIFTY options on Kite with non-zero quantity
    2. Active/partial/exiting positions in DB with their legs
    3. Compares: symbol + quantity

    Reports mismatches but does NOT auto-fix.

    Args:
        kite: SNAILKiteClient instance
        telegram: Optional TelegramAlerts instance for notifications

    Returns:
        ReconciliationResult with any mismatches found
    """
    from src.utils.db import get_position_legs, get_db_session, _row_to_position

    result = ReconciliationResult()

    try:
        # Step 1: Get NIFTY options from Kite with non-zero quantity
        kite_positions = kite.positions()
        net_positions = kite_positions.get('net', [])

        kite_nifty = {}
        for p in net_positions:
            symbol = p.get('tradingsymbol', '')
            qty = p.get('quantity', 0)
            if symbol.startswith('NIFTY') and qty != 0:
                kite_nifty[symbol] = qty

        result.kite_nifty_positions = len(kite_nifty)

        # Step 2: Get all non-closed positions from DB
        with get_db_session() as conn:
            cursor = conn.execute(
                "SELECT * FROM positions WHERE status IN ('active', 'partial', 'exiting')"
            )
            db_positions = [_row_to_position(row) for row in cursor.fetchall()]

        result.db_positions_checked = len(db_positions)

        # Step 3: Build expected symbols from DB position legs
        db_symbols = {}  # symbol -> (quantity, position_id, leg_type)
        for pos in db_positions:
            legs = get_position_legs(pos.id)
            for leg in legs:
                # Determine expected quantity sign from leg type
                # Straddle legs are SHORT (negative on Kite), wings are LONG (positive)
                if leg.leg_type.startswith('straddle'):
                    expected_qty = -leg.quantity  # Short
                else:
                    expected_qty = leg.quantity  # Long

                db_symbols[leg.tradingsymbol] = (expected_qty, pos.id, leg.leg_type)

        # Step 4: Compare
        all_symbols = set(kite_nifty.keys()) | set(db_symbols.keys())

        for symbol in all_symbols:
            kite_qty = kite_nifty.get(symbol, 0)
            db_entry = db_symbols.get(symbol)

            if db_entry is None:
                # On Kite but not in DB
                result.mismatches.append(PositionMismatch(
                    mismatch_type='orphaned_on_kite',
                    tradingsymbol=symbol,
                    kite_quantity=kite_qty,
                    details=f"Position on Kite ({kite_qty} qty) with no matching DB leg"
                ))
            elif kite_qty == 0:
                # In DB but not on Kite
                db_qty, pos_id, leg_type = db_entry
                result.mismatches.append(PositionMismatch(
                    mismatch_type='ghost_in_db',
                    tradingsymbol=symbol,
                    db_quantity=db_qty,
                    db_position_id=pos_id,
                    db_leg_type=leg_type,
                    details=f"DB leg (pos={pos_id}, {leg_type}) but 0 qty on Kite"
                ))
            else:
                # Both exist - check quantity
                db_qty, pos_id, leg_type = db_entry
                if kite_qty != db_qty:
                    result.mismatches.append(PositionMismatch(
                        mismatch_type='quantity_mismatch',
                        tradingsymbol=symbol,
                        kite_quantity=kite_qty,
                        db_quantity=db_qty,
                        db_position_id=pos_id,
                        db_leg_type=leg_type,
                        details=f"Kite qty={kite_qty} vs DB qty={db_qty} (pos={pos_id})"
                    ))

        result.has_mismatches = len(result.mismatches) > 0

        # Step 5: Log and alert
        if result.has_mismatches:
            logger.critical(
                f"POSITION RECONCILIATION MISMATCH: {len(result.mismatches)} issues found"
            )
            for m in result.mismatches:
                logger.critical(f"  [{m.mismatch_type}] {m.tradingsymbol}: {m.details}")

            if telegram:
                alert_msg = (
                    f"🚨 *Position Reconciliation Mismatch*\n\n"
                    f"Found {len(result.mismatches)} discrepancies "
                    f"between Kite and DB:\n\n"
                )
                for m in result.mismatches:
                    alert_msg += f"• *{m.mismatch_type}*: {m.tradingsymbol}\n"
                    alert_msg += f"  {m.details}\n\n"
                alert_msg += "⚠️ *Manual verification required!*"
                try:
                    telegram.send(alert_msg)
                except Exception as e:
                    logger.error(f"Failed to send reconciliation alert: {e}")
        else:
            logger.info(
                f"Position reconciliation OK: {result.kite_nifty_positions} Kite positions, "
                f"{result.db_positions_checked} DB positions checked"
            )

    except Exception as e:
        logger.error(f"Reconciliation failed: {e}")
        result.has_mismatches = False  # Don't block on reconciliation errors

    return result
