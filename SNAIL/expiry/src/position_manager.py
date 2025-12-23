"""
Position Manager for Expiry Trading System

Handles P&L calculation and exit condition checking.

@file        position_manager.py
@description Position tracking and P&L monitoring
"""

from datetime import datetime
from typing import Dict, Optional, Any, Tuple
from dataclasses import dataclass
from enum import Enum
from loguru import logger

from src.state_manager import Position


class ExitReason(str, Enum):
    """Reason for position exit."""
    TARGET_HIT = "TARGET_HIT"
    STOP_LOSS = "STOP_LOSS"
    TIME_EXIT = "TIME_EXIT"
    MANUAL = "MANUAL"
    ERROR = "ERROR"


@dataclass
class PnLSnapshot:
    """P&L snapshot at a point in time."""
    timestamp: str
    spot: float
    vix: float
    mtm_pnl: float
    pnl_pct: float  # Percentage of target
    short_ce_value: float
    short_pe_value: float
    long_ce_value: float
    long_pe_value: float
    distance_to_ce_wing: float
    distance_to_pe_wing: float


@dataclass
class ExitSignal:
    """Exit signal with reason."""
    should_exit: bool
    reason: Optional[ExitReason] = None
    message: str = ""


class PositionManager:
    """
    Manage open position and calculate P&L.

    Monitors position for:
    - Target hit (50% premium decay)
    - Stop loss (2x credit loss)
    - Time exit (3:15 PM)
    - Wing approach warnings
    - VIX spike alerts
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize position manager.

        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.strategy_config = config.get('strategy', {})
        self.telegram_config = config.get('telegram', {})

        self.target_decay_pct = self.strategy_config.get('target_decay_pct', 50)
        self.sl_multiplier = self.strategy_config.get('stop_loss_multiplier', 2.0)
        self.hard_exit_time = self.strategy_config.get('hard_exit_time', '15:15')
        self.wing_approach_pct = self.telegram_config.get('wing_approach_pct', 70)
        self.vix_spike_threshold = self.telegram_config.get('vix_spike_threshold', 1.0)

    def calculate_mtm_pnl(
        self,
        position: Position,
        quotes: Dict[str, Any]
    ) -> Tuple[float, PnLSnapshot]:
        """
        Calculate mark-to-market P&L.

        Entry Credit - Current Exit Cost = MTM P&L

        Args:
            position: Current position
            quotes: Current quotes from Kite

        Returns:
            Tuple of (mtm_pnl, pnl_snapshot)
        """
        def get_quote(symbol: str) -> Any:
            return quotes.get(f"NFO:{symbol}")

        # Current option values
        short_ce_value = 0.0
        short_pe_value = 0.0
        long_ce_value = 0.0
        long_pe_value = 0.0

        for leg in position.legs:
            quote = get_quote(leg.symbol)
            if not quote:
                logger.warning(f"No quote for {leg.symbol}")
                continue

            # For MTM: use mid-price for fair value
            mid_price = (quote.bid + quote.ask) / 2

            if leg.option_type == 'CE':
                if leg.transaction_type == 'SELL':
                    short_ce_value = mid_price
                else:
                    long_ce_value = mid_price
            else:  # PE
                if leg.transaction_type == 'SELL':
                    short_pe_value = mid_price
                else:
                    long_pe_value = mid_price

        # Entry credit (per share)
        entry_credit = sum(
            leg.entry_price for leg in position.legs
            if leg.transaction_type == 'SELL'
        ) - sum(
            leg.entry_price for leg in position.legs
            if leg.transaction_type == 'BUY'
        )

        # Current exit cost (per share)
        current_debit = (short_ce_value + short_pe_value) - (long_ce_value + long_pe_value)

        # MTM P&L per share
        pnl_per_share = entry_credit - current_debit

        # Total P&L
        total_qty = position.num_lots * position.lot_size
        mtm_pnl = pnl_per_share * total_qty

        # Calculate distances to wings (protection strikes)
        spot_quote = quotes.get('NSE:NIFTY 50')
        if spot_quote and hasattr(spot_quote, 'ltp') and spot_quote.ltp > 0:
            spot_price = spot_quote.ltp
        else:
            spot_price = position.spot_at_entry
            logger.warning(f"Using stale spot price: {spot_price}")

        # Find wing strikes from position legs
        # Iron Condor structure:
        #   legs[0] = Short CE (SELL), legs[1] = Short PE (SELL)
        #   legs[2] = Long CE (BUY),   legs[3] = Long PE (BUY)
        # Wings are the LONG strikes (protection level)
        if not position.legs or len(position.legs) < 4:
            logger.error(f"Invalid position: expected 4 legs, got {len(position.legs) if position.legs else 0}")
            atm_strike = round(spot_price / 50) * 50
            ce_wing_strike = atm_strike + position.wing_distance
            pe_wing_strike = atm_strike - position.wing_distance
        else:
            # Find long strikes (the wings/protection)
            long_ce_strike = None
            long_pe_strike = None

            for leg in position.legs:
                if leg.transaction_type == 'BUY' and leg.option_type == 'CE':
                    long_ce_strike = leg.strike
                elif leg.transaction_type == 'BUY' and leg.option_type == 'PE':
                    long_pe_strike = leg.strike
                # Note: short strikes not needed for wing distance calculation
                # Wing boundaries are defined by long strikes (protection level)

            # Use actual long strikes as wing boundaries
            if long_ce_strike and long_pe_strike:
                ce_wing_strike = long_ce_strike
                pe_wing_strike = long_pe_strike
            else:
                # Fallback: calculate from short strikes
                logger.warning("Could not find long strikes, using calculation")
                atm_strike = round(spot_price / 50) * 50
                ce_wing_strike = atm_strike + position.wing_distance
                pe_wing_strike = atm_strike - position.wing_distance

        distance_to_ce = ce_wing_strike - spot_price
        distance_to_pe = spot_price - pe_wing_strike

        # Get VIX
        vix_quote = quotes.get('NSE:INDIA VIX')
        if vix_quote and hasattr(vix_quote, 'ltp') and vix_quote.ltp > 0:
            current_vix = vix_quote.ltp
        else:
            current_vix = position.vix_at_entry
            logger.warning(f"Using stale VIX: {current_vix}")

        # P&L as percentage of target
        pnl_pct = (mtm_pnl / position.target_pnl) * 100 if position.target_pnl > 0 else 0

        snapshot = PnLSnapshot(
            timestamp=datetime.now().strftime("%H:%M:%S"),
            spot=spot_price,
            vix=current_vix,
            mtm_pnl=mtm_pnl,
            pnl_pct=pnl_pct,
            short_ce_value=short_ce_value,
            short_pe_value=short_pe_value,
            long_ce_value=long_ce_value,
            long_pe_value=long_pe_value,
            distance_to_ce_wing=distance_to_ce,
            distance_to_pe_wing=distance_to_pe
        )

        return mtm_pnl, snapshot

    def check_exit_conditions(
        self,
        position: Position,
        mtm_pnl: float,
        current_time: Optional[datetime] = None
    ) -> ExitSignal:
        """
        Check if any exit condition is met.

        Args:
            position: Current position
            mtm_pnl: Current MTM P&L
            current_time: Current time (for testing)

        Returns:
            ExitSignal indicating if exit needed
        """
        if current_time is None:
            current_time = datetime.now()

        current_time_str = current_time.strftime("%H:%M")

        # Check time exit
        if current_time_str >= self.hard_exit_time:
            return ExitSignal(
                should_exit=True,
                reason=ExitReason.TIME_EXIT,
                message=f"Hard exit time {self.hard_exit_time} reached"
            )

        # Check target hit (50% of premium decay = 50% of max profit)
        if mtm_pnl >= position.target_pnl:
            return ExitSignal(
                should_exit=True,
                reason=ExitReason.TARGET_HIT,
                message=f"Target hit: {mtm_pnl:.0f} >= {position.target_pnl:.0f}"
            )

        # Check stop loss
        if mtm_pnl <= position.stop_loss_pnl:
            return ExitSignal(
                should_exit=True,
                reason=ExitReason.STOP_LOSS,
                message=f"Stop loss hit: {mtm_pnl:.0f} <= {position.stop_loss_pnl:.0f}"
            )

        return ExitSignal(should_exit=False)

    def check_wing_approach(
        self,
        position: Position,
        snapshot: PnLSnapshot
    ) -> Optional[Tuple[str, float]]:
        """
        Check if spot is approaching wings.

        Args:
            position: Current position
            snapshot: Current P&L snapshot

        Returns:
            Tuple of (direction, proximity_pct) if approaching, None otherwise
        """
        wing_distance = position.wing_distance

        # Calculate proximity as percentage of wing distance consumed
        ce_proximity = (wing_distance - snapshot.distance_to_ce_wing) / wing_distance * 100
        pe_proximity = (wing_distance - snapshot.distance_to_pe_wing) / wing_distance * 100

        # Check if either wing is being approached
        if ce_proximity >= self.wing_approach_pct:
            return ('CE', ce_proximity)
        if pe_proximity >= self.wing_approach_pct:
            return ('PE', pe_proximity)

        return None

    def check_vix_spike(
        self,
        current_vix: float,
        entry_vix: float
    ) -> Optional[float]:
        """
        Check if VIX has spiked from entry.

        Args:
            current_vix: Current VIX
            entry_vix: VIX at entry

        Returns:
            VIX change if spike detected, None otherwise
        """
        vix_change = current_vix - entry_vix

        if vix_change >= self.vix_spike_threshold:
            return vix_change

        return None

    def calculate_charges(
        self,
        position: Position,
        exit_turnover: float
    ) -> float:
        """
        Calculate transaction charges.

        Args:
            position: Position details
            exit_turnover: Total exit turnover (sum of all leg exit values)

        Returns:
            Total charges in rupees
        """
        charges_config = self.config.get('charges', {})

        total_qty = position.num_lots * position.lot_size

        # Entry turnover = sum of ALL leg entry values (not net credit)
        # This is the actual turnover for exchange charges calculation
        entry_turnover = sum(leg.entry_price * total_qty for leg in position.legs)

        # Brokerage: Rs.20 per order × 8 orders (4 entry + 4 exit)
        brokerage = charges_config.get('brokerage_per_order', 20) * 8

        # STT: 0.0625% on sell side (entry short + exit long)
        stt_rate = charges_config.get('stt_sell_rate', 0.000625)
        # Entry: short CE + short PE (sell)
        # Exit: long CE + long PE (sell) - buying back shorts has no STT
        sell_value: float = 0.0
        for leg in position.legs:
            if leg.transaction_type == 'SELL':
                # Entry sell (short positions)
                sell_value += leg.entry_price * total_qty
            else:
                # Exit sell (closing long positions)
                sell_value += leg.exit_price * total_qty if leg.exit_price > 0 else 0.0
        stt = sell_value * stt_rate

        # Exchange charges: 0.0495% on total turnover (both entry and exit)
        exchange_rate = charges_config.get('exchange_txn_rate', 0.0005)
        exchange_charges = (entry_turnover + exit_turnover) * exchange_rate

        # GST: 18% on brokerage + exchange charges
        gst_rate = charges_config.get('gst_rate', 0.18)
        gst = (brokerage + exchange_charges) * gst_rate

        # Stamp duty: 0.003% on buy side (entry longs + exit closing shorts)
        stamp_rate = charges_config.get('stamp_duty_rate', 0.00003)
        # Entry buy (long positions)
        entry_buy_value = sum(
            leg.entry_price * total_qty for leg in position.legs
            if leg.transaction_type == 'BUY'
        )
        # Exit buy (closing short positions)
        exit_buy_value = sum(
            leg.exit_price * total_qty for leg in position.legs
            if leg.transaction_type == 'SELL' and leg.exit_price > 0
        )
        stamp_duty = (entry_buy_value + exit_buy_value) * stamp_rate

        total_charges = brokerage + stt + exchange_charges + gst + stamp_duty

        logger.info(
            f"Charges: Brokerage={brokerage:.0f}, STT={stt:.0f}, "
            f"Exchange={exchange_charges:.0f}, GST={gst:.0f}, "
            f"Stamp={stamp_duty:.0f}, Total={total_charges:.0f}"
        )

        return total_charges

    def get_position_summary(
        self,
        position: Position,
        snapshot: PnLSnapshot
    ) -> Dict[str, Any]:
        """
        Get position summary for display/logging.

        Args:
            position: Current position
            snapshot: Current P&L snapshot

        Returns:
            Summary dictionary
        """
        # Calculate ATM from spot at entry
        atm_strike = round(position.spot_at_entry / 50) * 50

        # Find short strikes for summary
        short_ce_strike = None
        short_pe_strike = None
        if position.legs:
            for leg in position.legs:
                if leg.transaction_type == 'SELL' and leg.option_type == 'CE':
                    short_ce_strike = leg.strike
                elif leg.transaction_type == 'SELL' and leg.option_type == 'PE':
                    short_pe_strike = leg.strike

        return {
            'entry_time': position.entry_time,
            'spot_at_entry': position.spot_at_entry,
            'current_spot': snapshot.spot,
            'vix_at_entry': position.vix_at_entry,
            'current_vix': snapshot.vix,
            'wing_distance': position.wing_distance,
            'atm_strike': atm_strike,
            'short_ce_strike': short_ce_strike,
            'short_pe_strike': short_pe_strike,
            'total_credit': position.total_credit,
            'target_pnl': position.target_pnl,
            'stop_loss_pnl': position.stop_loss_pnl,
            'current_pnl': snapshot.mtm_pnl,
            'pnl_pct_of_target': snapshot.pnl_pct,
            'distance_to_ce_wing': snapshot.distance_to_ce_wing,
            'distance_to_pe_wing': snapshot.distance_to_pe_wing,
            'num_lots': position.num_lots,
            'lot_size': position.lot_size
        }
