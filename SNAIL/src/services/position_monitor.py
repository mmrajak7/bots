"""
SNAIL Position Monitor

Continuous monitoring of active Iron Fly positions with alert generation.

@file        position_monitor.py
@description Position monitoring and alert service
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 7
"""

import time
import threading
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass, field
from enum import Enum
from loguru import logger

from src.api.kite_client import SNAILKiteClient, Quote, get_kite_client
from src.api.telegram_alerts import TelegramAlerts, get_telegram
from src.api.claude_client import SNAILClaudeClient, MarketContext, ClaudeDecision, get_claude_client
from src.services.exit_manager import ExitManager, ExitReason, get_exit_manager
from src.utils.calculations import (
    calculate_position_pnl,
    is_approaching_wing,
    is_within_trading_hours,
    detect_big_move,
    calculate_atr,
    BigMoveDetection
)
from src.utils.db import (
    Position,
    PositionLeg,
    PnLSnapshot,
    get_active_position,
    get_position_legs,
    save_pnl_snapshot,
    get_today_market_data
)
from src.utils.alert_dedup import should_send_alert
from src.utils.config import get_trading_config, get_monitoring_config, load_config


# =============================================================================
# CONSTANTS
# =============================================================================

# Monitoring intervals
DEFAULT_POLL_INTERVAL = 30  # seconds
FAST_POLL_INTERVAL = 10     # seconds when approaching limits
PNL_SNAPSHOT_INTERVAL = 300 # seconds (5 minutes)

# Alert thresholds
VIX_SPIKE_THRESHOLD = 2.0   # Points increase from entry


# =============================================================================
# ENUMS
# =============================================================================

class AlertType(Enum):
    """Types of monitoring alerts."""
    PNL_UPDATE = "pnl_update"
    WING_APPROACH = "wing_approach"
    STOP_LOSS_WARNING = "stop_loss_warning"
    PROFIT_TARGET_NEAR = "profit_target_near"
    VIX_SPIKE = "vix_spike"
    VIX_RANGE_EXIT = "vix_range_exit"
    GAP_OPEN = "gap_open"
    BIG_MOVE = "big_move"  # TDD Section 5.3


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class MonitorState:
    """
    Current monitoring state.

    Attributes:
        position: Active position
        legs: Position legs
        last_pnl: Last calculated P&L
        last_pnl_pct: Last P&L percentage
        last_check: Last check timestamp
        entry_vix: VIX at position entry
        alerts_sent: Alert types sent recently
    """
    position: Optional[Position] = None
    legs: List[PositionLeg] = field(default_factory=list)
    last_pnl: float = 0.0
    last_pnl_pct: float = 0.0
    last_check: Optional[datetime] = None
    entry_vix: float = 0.0
    alerts_sent: Dict[str, datetime] = field(default_factory=dict)


@dataclass
class MonitorSnapshot:
    """
    Point-in-time monitoring snapshot.

    Attributes:
        timestamp: Snapshot timestamp
        nifty_spot: NIFTY spot price
        india_vix: India VIX
        current_pnl: Current P&L
        pnl_percentage: P&L as % of max profit
        straddle_value: Current straddle value
        wing_value: Current wing value
        quotes: Leg quotes
    """
    timestamp: datetime
    nifty_spot: float
    india_vix: float
    current_pnl: float
    pnl_percentage: float
    straddle_value: float
    wing_value: float
    quotes: Dict[str, Quote] = field(default_factory=dict)


# =============================================================================
# POSITION MONITOR CLASS
# =============================================================================

class PositionMonitor:
    """
    Monitors active Iron Fly positions.

    Features:
    - Real-time P&L tracking
    - Exit condition monitoring
    - Wing approach alerts
    - VIX spike detection
    - P&L snapshots for analysis

    Attributes:
        kite: Kite client
        telegram: Telegram client
        claude: Claude client
        exit_manager: Exit manager
        config: Configuration
        state: Current monitoring state
        running: Whether monitor is active
    """

    def __init__(
        self,
        kite: Optional[SNAILKiteClient] = None,
        telegram: Optional[TelegramAlerts] = None,
        claude: Optional[SNAILClaudeClient] = None,
        exit_manager: Optional[ExitManager] = None,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize position monitor.

        Args:
            kite: Kite client
            telegram: Telegram client
            claude: Claude client
            exit_manager: Exit manager
            config: Configuration
        """
        self.config = config or load_config()
        self.trading_config = get_trading_config()
        self.monitoring_config = get_monitoring_config()

        self.kite = kite or get_kite_client(self.config)
        self.telegram = telegram or get_telegram()
        self.claude = claude or get_claude_client()
        self.exit_manager = exit_manager or get_exit_manager()

        self.state = MonitorState()
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._last_snapshot_time: Optional[datetime] = None

        # Callbacks for external handling
        self._on_exit_trigger: Optional[Callable[[ExitReason, MonitorSnapshot], None]] = None

        logger.info("Position monitor initialized")

    # =========================================================================
    # STATE MANAGEMENT
    # =========================================================================

    def refresh_position(self) -> bool:
        """
        Refresh position state from database.

        Returns:
            True if active position exists
        """
        self.state.position = get_active_position()

        if self.state.position:
            self.state.legs = get_position_legs(self.state.position.id)
            return True

        self.state.legs = []
        return False

    # =========================================================================
    # QUOTE FETCHING
    # =========================================================================

    def get_current_quotes(self) -> Dict[str, Quote]:
        """
        Fetch current quotes for all position legs.

        Returns:
            Dict of quotes by leg type (empty dict on error)
        """
        if not self.state.legs:
            return {}

        try:
            instruments = []
            leg_map = {}

            for leg in self.state.legs:
                inst = f"NFO:{leg.tradingsymbol}"
                instruments.append(inst)
                leg_map[inst] = leg.leg_type

            raw_quotes = self.kite.quote(instruments)

            if not raw_quotes:
                logger.warning("Empty quote response from Kite API")
                return {}

            quotes = {}
            for inst, quote in raw_quotes.items():
                leg_type = leg_map.get(inst)
                if leg_type:
                    quotes[leg_type] = quote

            return quotes

        except Exception as e:
            logger.error(f"Error fetching quotes from Kite API: {e}")
            return {}

    # =========================================================================
    # SNAPSHOT GENERATION
    # =========================================================================

    def take_snapshot(self) -> Optional[MonitorSnapshot]:
        """
        Take a monitoring snapshot of current position state.

        Returns:
            MonitorSnapshot or None if no position
        """
        if not self.state.position:
            return None

        # Validate position lot_size
        if not self.state.position.lot_size or self.state.position.lot_size <= 0:
            logger.error("Position has invalid lot_size")
            return None

        # Validate legs exist and have entry prices
        if not self.state.legs:
            logger.warning("No legs found for position")
            return None

        for leg in self.state.legs:
            if leg.entry_price is None:
                logger.error(f"Leg {leg.leg_type} has no entry_price")
                return None

        try:
            # Get market data
            nifty_spot = self.kite.get_nifty_spot()
            india_vix = self.kite.get_india_vix()

            if nifty_spot is None or india_vix is None:
                logger.warning("Could not fetch market data (nifty_spot or india_vix is None)")
                return None

            # Get quotes
            quotes = self.get_current_quotes()
            if not quotes:
                logger.warning("Could not fetch quotes for P&L calculation")
                return None

            # Validate all legs have quotes with valid prices
            # Side is determined by leg_type: straddle_* = SHORT (sell), wing_* = LONG (buy)
            for leg in self.state.legs:
                if leg.leg_type not in quotes:
                    logger.warning(f"Missing quote for leg: {leg.leg_type}")
                    return None
                quote = quotes[leg.leg_type]
                is_short = leg.leg_type.startswith('straddle')
                if is_short and (quote.ask is None or quote.ask <= 0):
                    logger.warning(f"Invalid ask price for SHORT leg {leg.leg_type}: {quote.ask}")
                    return None
                if not is_short and (quote.bid is None or quote.bid <= 0):
                    logger.warning(f"Invalid bid price for LONG leg {leg.leg_type}: {quote.bid}")
                    return None

            # Calculate P&L
            # straddle legs (straddle_ce, straddle_pe) are SHORT - we sold them
            # wing legs (wing_ce, wing_pe) are LONG - we bought them
            entry_straddle = sum(l.entry_price for l in self.state.legs if l.leg_type.startswith('straddle'))
            entry_wing = sum(l.entry_price for l in self.state.legs if l.leg_type.startswith('wing'))

            current_straddle = sum(
                quotes[l.leg_type].ask for l in self.state.legs
                if l.leg_type.startswith('straddle') and l.leg_type in quotes
            )
            current_wing = sum(
                quotes[l.leg_type].bid for l in self.state.legs
                if l.leg_type.startswith('wing') and l.leg_type in quotes
            )

            current_pnl, pnl_pct = calculate_position_pnl(
                entry_straddle, entry_wing, current_straddle, current_wing,
                self.state.position.lot_size,
                self.state.position.margin_deployed
            )

            # Update state
            self.state.last_pnl = current_pnl
            self.state.last_pnl_pct = pnl_pct
            self.state.last_check = datetime.now()

            return MonitorSnapshot(
                timestamp=datetime.now(),
                nifty_spot=nifty_spot,
                india_vix=india_vix,
                current_pnl=current_pnl,
                pnl_percentage=pnl_pct,
                straddle_value=current_straddle,
                wing_value=current_wing,
                quotes=quotes
            )

        except Exception as e:
            logger.error(f"Error taking snapshot: {e}")
            return None

    def save_snapshot_to_db(self, snapshot: MonitorSnapshot) -> None:
        """Save P&L snapshot to database."""
        if not self.state.position:
            logger.warning("Cannot save snapshot: no active position")
            return

        # Update margin from Zerodha (changes daily based on SPAN)
        self._update_margin()

        # Extract bid/ask from quotes
        quotes = snapshot.quotes
        ce_quote = quotes.get('straddle_ce')
        pe_quote = quotes.get('straddle_pe')
        wing_ce_quote = quotes.get('wing_ce')
        wing_pe_quote = quotes.get('wing_pe')

        pnl_snapshot = PnLSnapshot(
            id=0,
            position_id=self.state.position.id,
            current_pnl=snapshot.current_pnl,
            pnl_percent=snapshot.pnl_percentage,
            nifty_spot=snapshot.nifty_spot,
            vix=snapshot.india_vix,
            ce_bid=ce_quote.bid if ce_quote else 0.0,
            ce_ask=ce_quote.ask if ce_quote else 0.0,
            pe_bid=pe_quote.bid if pe_quote else 0.0,
            pe_ask=pe_quote.ask if pe_quote else 0.0,
            wing_ce_bid=wing_ce_quote.bid if wing_ce_quote else None,
            wing_ce_ask=wing_ce_quote.ask if wing_ce_quote else None,
            wing_pe_bid=wing_pe_quote.bid if wing_pe_quote else None,
            wing_pe_ask=wing_pe_quote.ask if wing_pe_quote else None
        )
        save_pnl_snapshot(pnl_snapshot)

    def _update_margin(self) -> None:
        """Update margin_deployed from Zerodha (SPAN margin changes daily)."""
        if not self.state.position:
            return

        try:
            current_margin = self.kite.get_margin_utilised()
            if current_margin > 0 and current_margin != self.state.position.margin_deployed:
                # Update in database
                from src.utils.db import get_connection
                conn = get_connection()
                conn.execute(
                    "UPDATE positions SET margin_deployed = ? WHERE id = ?",
                    (current_margin, self.state.position.id)
                )
                conn.commit()
                logger.debug(f"Margin updated: ₹{current_margin:,.0f}")
                # Update local state
                self.state.position.margin_deployed = current_margin
        except Exception as e:
            logger.debug(f"Could not update margin: {e}")

    # =========================================================================
    # ALERT CHECKS
    # =========================================================================

    def check_and_alert(self, snapshot: MonitorSnapshot) -> Optional[ExitReason]:
        """
        Check monitoring conditions and send alerts.

        Args:
            snapshot: Current monitoring snapshot

        Returns:
            ExitReason if exit should be triggered, None otherwise
        """
        position = self.state.position

        # Guard against None position
        if position is None:
            logger.warning("check_and_alert called with no active position")
            return None

        # Validate required position attributes
        if not all([
            position.atm_strike is not None,
            position.wing_distance is not None,
            position.max_profit is not None,
            position.max_loss is not None
        ]):
            logger.error("Position missing required attributes (atm_strike, wing_distance, max_profit, max_loss)")
            return None

        # Check profit target (50%)
        profit_target_pct = self.trading_config.get('exit', {}).get('profit_target_pct', 50)
        if snapshot.pnl_percentage >= profit_target_pct:
            logger.info(f"Profit target reached: {snapshot.pnl_percentage:.1f}%")
            return ExitReason.PROFIT_TARGET

        # Check near profit target (40%)
        if snapshot.pnl_percentage >= (profit_target_pct - 10):
            if should_send_alert('profit_target_near', f"pnl_{snapshot.pnl_percentage:.0f}"):
                self.telegram.send_pnl_update(
                    current_pnl=snapshot.current_pnl,
                    pnl_percent=snapshot.pnl_percentage,
                    nifty_spot=snapshot.nifty_spot,
                    vix=snapshot.india_vix,
                    max_profit=position.max_profit,
                    max_loss=position.max_loss
                )

        # Check stop loss (50% of max loss)
        stop_loss_pct = self.trading_config.get('exit', {}).get('stop_loss_pct', 50)
        loss_pct = abs(snapshot.pnl_percentage) if snapshot.pnl_percentage < 0 else 0

        if loss_pct >= stop_loss_pct:
            # At stop loss level - need Claude advisory
            if should_send_alert('stop_loss_warning', f"sl_{loss_pct:.0f}"):
                # Calculate distance to nearest wing
                ce_wing = position.atm_strike + position.wing_distance
                pe_wing = position.atm_strike - position.wing_distance
                distance_to_wing = min(
                    abs(snapshot.nifty_spot - ce_wing),
                    abs(snapshot.nifty_spot - pe_wing)
                )

                self.telegram.send_stop_loss_alert(
                    current_pnl=snapshot.current_pnl,
                    loss_pct_of_max=loss_pct,
                    nifty_spot=snapshot.nifty_spot,
                    distance_to_wing=distance_to_wing
                    # claude_advice is optional, will show "Awaiting Claude analysis..."
                )
                # Don't auto-exit, let Claude advisor handle this
                return None

        # Check wing approach (75% of wing distance)
        approaching, direction = is_approaching_wing(
            spot_price=snapshot.nifty_spot,
            atm_strike=position.atm_strike,
            wing_distance=position.wing_distance,
            threshold_pct=0.75
        )

        if approaching:
            if should_send_alert('wing_approach', f"wing_{direction}"):
                # Calculate wing proximity percentage and distance
                wing_strike = position.atm_strike + (position.wing_distance if direction == 'CE' else -position.wing_distance)
                distance_to_wing = abs(snapshot.nifty_spot - wing_strike)
                wing_proximity = ((position.wing_distance - distance_to_wing) / position.wing_distance) * 100 if position.wing_distance > 0 else 0

                self.telegram.send_wing_approach_alert(
                    direction=direction,
                    wing_proximity=wing_proximity,
                    distance_to_wing=distance_to_wing,
                    nifty_spot=snapshot.nifty_spot
                    # claude_advice is optional, will show "Awaiting Claude analysis..."
                )

        # Check VIX spike
        vix_config = self.trading_config.get('entry', {}).get('vix_range', {})
        vix_max = vix_config.get('max', 16)

        if snapshot.india_vix > vix_max:
            if should_send_alert('vix_spike', f"vix_{snapshot.india_vix:.0f}"):
                # Calculate VIX change from entry
                vix_change = snapshot.india_vix - self.state.entry_vix if self.state.entry_vix > 0 else 0

                self.telegram.send_vix_warning(
                    current_vix=snapshot.india_vix,
                    vix_change=vix_change
                    # claude_advice is optional, will show "Awaiting Claude analysis..."
                )

        # Check Big Move (TDD Section 5.3)
        # Requires: Day open, Day high/low, ATR from market_data
        market_data = get_today_market_data()
        if market_data and market_data.day_open:
            big_move_result = detect_big_move(
                nifty_spot=snapshot.nifty_spot,
                day_open=market_data.day_open,
                day_high=market_data.day_high or snapshot.nifty_spot,
                day_low=market_data.day_low or snapshot.nifty_spot,
                atr_14=market_data.atr_14 or 150,  # Default 150 if not set
                atm_strike=position.atm_strike,
                wing_distance=position.wing_distance
            )

            if big_move_result.is_big_move:
                # Only alert once per severity level
                alert_key = f"big_move_{big_move_result.severity}"
                if should_send_alert('big_move', alert_key):
                    conditions_text = "\n".join(f"• {c}" for c in big_move_result.conditions_met)

                    severity_emoji = {
                        'WARNING': '⚠️',
                        'CRITICAL': '🚨'
                    }.get(big_move_result.severity, '📊')

                    self.telegram.send(
                        f"{severity_emoji} *Big Move Detected*\n\n"
                        f"Severity: *{big_move_result.severity}*\n\n"
                        f"Triggers:\n{conditions_text}\n\n"
                        f"Metrics:\n"
                        f"• Day Range: {big_move_result.day_range:.0f} pts\n"
                        f"• ATR(14): {big_move_result.atr_14:.0f} pts\n"
                        f"• Wing Proximity: {big_move_result.wing_proximity:.1f}%\n"
                        f"• Move from Open: {big_move_result.move_from_open:.2f}%\n\n"
                        f"_Claude advisory recommended_"
                    )

                    # If CRITICAL, could trigger Claude advisory here
                    if big_move_result.severity == 'CRITICAL':
                        logger.warning(f"CRITICAL big move detected: {big_move_result.conditions_met}")

        # Periodic P&L update (every 30 minutes during market hours)
        if should_send_alert('pnl_update', f"pnl_periodic"):
            self.telegram.send_pnl_update(
                current_pnl=snapshot.current_pnl,
                pnl_percent=snapshot.pnl_percentage,
                nifty_spot=snapshot.nifty_spot,
                vix=snapshot.india_vix,
                max_profit=position.max_profit,
                max_loss=position.max_loss
            )

        return None

    # =========================================================================
    # MONITORING LOOP
    # =========================================================================

    def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        logger.info("Starting position monitoring loop")

        while self._running:
            try:
                # Only monitor during trading hours
                if not is_within_trading_hours():
                    time.sleep(60)  # Check every minute outside hours
                    continue

                # Refresh position
                if not self.refresh_position():
                    time.sleep(DEFAULT_POLL_INTERVAL)
                    continue

                # Take snapshot
                snapshot = self.take_snapshot()
                if not snapshot:
                    time.sleep(DEFAULT_POLL_INTERVAL)
                    continue

                # Save snapshot periodically
                now = datetime.now()
                if (self._last_snapshot_time is None or
                    (now - self._last_snapshot_time).total_seconds() >= PNL_SNAPSHOT_INTERVAL):
                    self.save_snapshot_to_db(snapshot)
                    self._last_snapshot_time = now

                # Check alerts and exit conditions
                exit_reason = self.check_and_alert(snapshot)

                if exit_reason:
                    logger.info(f"Exit condition triggered: {exit_reason.value}")

                    # Execute exit
                    result = self.exit_manager.execute_exit(
                        reason=exit_reason,
                        position=self.state.position
                    )

                    if result.success:
                        self.refresh_position()  # Clear position state

                    # Call external handler if registered
                    if self._on_exit_trigger:
                        self._on_exit_trigger(exit_reason, snapshot)

                # Determine poll interval
                poll_interval = DEFAULT_POLL_INTERVAL

                # Faster polling when near limits
                if abs(snapshot.pnl_percentage) >= 40:
                    poll_interval = FAST_POLL_INTERVAL

                time.sleep(poll_interval)

            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(DEFAULT_POLL_INTERVAL)

        logger.info("Position monitoring loop stopped")

    def start(self) -> None:
        """Start monitoring thread."""
        if self._running:
            return

        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="PositionMonitor",
            daemon=True
        )
        self._monitor_thread.start()
        logger.info("Position monitor started")

    def stop(self) -> None:
        """Stop monitoring thread."""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=10)
        logger.info("Position monitor stopped")

    def is_running(self) -> bool:
        """Check if monitor is running."""
        return self._running

    # =========================================================================
    # CALLBACKS
    # =========================================================================

    def on_exit_trigger(self, callback: Callable[[ExitReason, MonitorSnapshot], None]) -> None:
        """
        Register callback for exit triggers.

        Args:
            callback: Function to call when exit is triggered
        """
        self._on_exit_trigger = callback

    # =========================================================================
    # MANUAL CHECKS
    # =========================================================================

    def check_now(self) -> Optional[MonitorSnapshot]:
        """
        Perform immediate check and return snapshot.

        Returns:
            Current snapshot or None
        """
        if not self.refresh_position():
            return None

        return self.take_snapshot()

    def get_status(self) -> Dict[str, Any]:
        """
        Get current monitor status.

        Returns:
            Status dictionary
        """
        return {
            'running': self._running,
            'has_position': self.state.position is not None,
            'position_id': self.state.position.id if self.state.position else None,
            'last_pnl': self.state.last_pnl,
            'last_pnl_pct': self.state.last_pnl_pct,
            'last_check': self.state.last_check.isoformat() if self.state.last_check else None
        }


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_position_monitor: Optional[PositionMonitor] = None


def get_position_monitor(config: Optional[Dict] = None) -> PositionMonitor:
    """Get or create singleton position monitor."""
    global _position_monitor

    if _position_monitor is None:
        _position_monitor = PositionMonitor(config=config)

    return _position_monitor


def reset_position_monitor() -> None:
    """Reset singleton position monitor."""
    global _position_monitor
    if _position_monitor:
        _position_monitor.stop()
    _position_monitor = None


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    import sys
    from dotenv import load_dotenv

    load_dotenv()

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    print("\n" + "=" * 60)
    print("SNAIL Position Monitor Test")
    print("=" * 60)

    try:
        monitor = PositionMonitor()

        # Check for position
        print("\n[1] Checking for active position...")
        has_position = monitor.refresh_position()

        if has_position:
            print(f"    Position ID: {monitor.state.position.id}")
            print(f"    Legs: {len(monitor.state.legs)}")

            # Take snapshot
            print("\n[2] Taking snapshot...")
            snapshot = monitor.take_snapshot()

            if snapshot:
                print(f"    NIFTY: {snapshot.nifty_spot:,.2f}")
                print(f"    VIX: {snapshot.india_vix:.2f}")
                print(f"    P&L: ₹{snapshot.current_pnl:,.2f}")
                print(f"    P&L %: {snapshot.pnl_percentage:.1f}%")
            else:
                print("    Failed to take snapshot")

            # Get status
            print("\n[3] Monitor status:")
            status = monitor.get_status()
            for key, value in status.items():
                print(f"    {key}: {value}")

        else:
            print("    No active position found")

        print("\n" + "=" * 60)
        print("Position monitor test complete!")
        print("=" * 60 + "\n")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
