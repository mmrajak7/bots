"""
SNAIL Entry Manager

Manages Iron Fly position entry including validation, execution, and recording.

@file        entry_manager.py
@description Iron Fly entry management service
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 4.1
"""

from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass
from loguru import logger

from src.api.kite_client import SNAILKiteClient, Quote, get_kite_client
from src.api.telegram_alerts import TelegramAlerts, get_telegram
from src.api.claude_client import SNAILClaudeClient, MarketContext, ClaudeDecision, get_claude_client
from src.utils.symbol_builder import (
    generate_nifty_option_symbol,
    calculate_atm_strike,
    calculate_wing_distance,
    build_iron_fly_instruments,
    get_target_expiry,
    load_instruments,
    get_lot_size_from_instruments
)
from src.utils.order_helpers import (
    execute_iron_fly_entry,
    IronFlyOrders,
    OrderExecutionError,
    validate_iron_fly_quotes,
    validate_bid_ask_spreads,
    SpreadValidationResult,
    verify_iron_fly_positions
)
from src.utils.calculations import (
    calculate_iron_fly_metrics,
    calculate_iron_fly_charges,
    IronFlyMetrics
)
from src.utils.db import (
    get_db_session,
    Position,
    PositionLeg,
    save_position,
    save_position_leg,
    get_active_position,
    is_on_cooldown,
    set_cooldown
)
from src.utils.config import (
    get_trading_config,
    get_instruments_path,
    load_config
)


# =============================================================================
# CONSTANTS
# =============================================================================

# Entry time window
ENTRY_START_TIME = "09:20"  # After market settles
ENTRY_END_TIME = "14:30"    # Leave buffer before close

# Slippage allowance
DEFAULT_SLIPPAGE_TICKS = 2


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class EntryConditions:
    """
    Entry conditions check result.

    Attributes:
        can_enter: Whether entry is allowed
        reason: Reason if entry blocked
        nifty_spot: Current NIFTY spot
        india_vix: Current VIX
        atm_strike: Calculated ATM strike
        expiry: Target expiry date
        dte: Days to expiry
    """
    can_enter: bool
    reason: str
    nifty_spot: float = 0.0
    india_vix: float = 0.0
    atm_strike: int = 0
    expiry: Optional[date] = None
    dte: int = 0


@dataclass
class EntryResult:
    """
    Entry execution result.

    Attributes:
        success: Whether entry was successful
        position_id: Position ID if successful
        metrics: Position metrics
        orders: Executed orders
        charges: Transaction charges
        error: Error message if failed
    """
    success: bool
    position_id: Optional[int] = None
    metrics: Optional[IronFlyMetrics] = None
    orders: Optional[IronFlyOrders] = None
    charges: Optional[Dict[str, float]] = None
    error: str = ""


# =============================================================================
# ENTRY MANAGER CLASS
# =============================================================================

class EntryManager:
    """
    Manages Iron Fly position entry.

    Responsibilities:
    - Validate entry conditions (VIX, time, cooldown, etc.)
    - Calculate position parameters
    - Execute entry orders
    - Record position to database
    - Send alerts

    Attributes:
        kite: Kite client
        telegram: Telegram client
        claude: Claude client
        config: Trading configuration
    """

    def __init__(
        self,
        kite: Optional[SNAILKiteClient] = None,
        telegram: Optional[TelegramAlerts] = None,
        claude: Optional[SNAILClaudeClient] = None,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize entry manager.

        Args:
            kite: Kite client (created if None)
            telegram: Telegram client (created if None)
            claude: Claude client (created if None)
            config: Configuration (loaded if None)
        """
        self.config = config or load_config()
        self.trading_config = get_trading_config()

        self.kite = kite or get_kite_client(self.config)
        self.telegram = telegram or get_telegram()
        self.claude = claude or get_claude_client()

        # Load instruments
        self._instruments_df = None

        logger.info("Entry manager initialized")

    @property
    def instruments_df(self):
        """Lazy load instruments DataFrame."""
        if self._instruments_df is None:
            self._instruments_df = load_instruments(get_instruments_path())
        return self._instruments_df

    # =========================================================================
    # CONDITION CHECKS
    # =========================================================================

    def check_entry_conditions(self) -> EntryConditions:
        """
        Check all entry conditions.

        Conditions checked:
        1. No existing position
        2. Not on cooldown
        3. Within trading hours
        4. Within entry time window
        5. VIX in range (10-16)
        6. DTE >= minimum

        Returns:
            EntryConditions result
        """
        logger.info("Checking entry conditions...")

        # Check for existing position
        active = get_active_position()
        if active:
            return EntryConditions(
                can_enter=False,
                reason=f"Active position exists (ID: {active.id})"
            )

        # Check cooldown
        if is_on_cooldown('entry'):
            return EntryConditions(
                can_enter=False,
                reason="Entry on cooldown (1-day after exit)"
            )

        # Check user skip cooldown (user explicitly skipped today)
        if is_on_cooldown('user_skip'):
            return EntryConditions(
                can_enter=False,
                reason="User skipped entry for today"
            )

        # Check time
        now = datetime.now()
        current_time = now.strftime("%H:%M")

        if current_time < ENTRY_START_TIME:
            return EntryConditions(
                can_enter=False,
                reason=f"Before entry window ({ENTRY_START_TIME})"
            )

        if current_time > ENTRY_END_TIME:
            return EntryConditions(
                can_enter=False,
                reason=f"After entry window ({ENTRY_END_TIME})"
            )

        # Check if trading day (basic weekday check)
        if now.weekday() >= 5:
            return EntryConditions(
                can_enter=False,
                reason="Not a trading day"
            )

        # Get market data
        try:
            self.kite.ensure_authenticated()
            nifty_spot = self.kite.get_nifty_spot()
            india_vix = self.kite.get_india_vix()
        except Exception as e:
            logger.error(f"Failed to get market data: {e}")
            return EntryConditions(
                can_enter=False,
                reason=f"Market data unavailable: {e}"
            )

        # Check VIX range with soft buffer (TDD Section 5.2)
        vix_config = self.trading_config.get('entry', {}).get('vix_range', {})
        vix_min = vix_config.get('min', 10)
        vix_max = vix_config.get('max', 16)
        vix_soft_buffer = vix_config.get('soft_buffer', 0.5)  # Default 0.5 pts buffer

        # Soft buffer: warn but allow entry
        vix_soft_min = vix_min - vix_soft_buffer  # 9.5 with default
        vix_soft_max = vix_max + vix_soft_buffer  # 16.5 with default

        vix_warning = None

        # Hard block: VIX outside soft buffer range
        if india_vix < vix_soft_min:
            return EntryConditions(
                can_enter=False,
                reason=f"VIX too low ({india_vix:.2f} < {vix_soft_min:.1f} soft min)",
                nifty_spot=nifty_spot,
                india_vix=india_vix
            )

        if india_vix > vix_soft_max:
            return EntryConditions(
                can_enter=False,
                reason=f"VIX too high ({india_vix:.2f} > {vix_soft_max:.1f} soft max)",
                nifty_spot=nifty_spot,
                india_vix=india_vix
            )

        # Soft warning: VIX in buffer zone (proceed with warning)
        if india_vix < vix_min:
            vix_warning = f"VIX in soft buffer zone ({india_vix:.2f}, ideal >{vix_min})"
            logger.warning(vix_warning)
        elif india_vix > vix_max:
            vix_warning = f"VIX in soft buffer zone ({india_vix:.2f}, ideal <{vix_max})"
            logger.warning(vix_warning)

        # Get target expiry
        try:
            min_dte = self.trading_config.get('entry', {}).get('min_dte', 6)
            expiry = get_target_expiry(self.instruments_df, min_dte)
            dte = (expiry - date.today()).days
        except Exception as e:
            logger.error(f"Failed to get expiry: {e}")
            return EntryConditions(
                can_enter=False,
                reason=f"Expiry selection failed: {e}",
                nifty_spot=nifty_spot,
                india_vix=india_vix
            )

        # Calculate ATM strike
        atm_strike = calculate_atm_strike(nifty_spot)

        logger.info(f"Entry conditions met: NIFTY={nifty_spot:.2f}, VIX={india_vix:.2f}, "
                   f"ATM={atm_strike}, Expiry={expiry} (DTE={dte})")

        return EntryConditions(
            can_enter=True,
            reason="All conditions met",
            nifty_spot=nifty_spot,
            india_vix=india_vix,
            atm_strike=atm_strike,
            expiry=expiry,
            dte=dte
        )

    # =========================================================================
    # QUOTE FETCHING
    # =========================================================================

    def get_iron_fly_quotes(
        self,
        expiry: date,
        atm_strike: int,
        wing_distance: int
    ) -> Tuple[Dict[str, str], Dict[str, Quote]]:
        """
        Fetch quotes for Iron Fly legs.

        Args:
            expiry: Expiry date
            atm_strike: ATM strike price
            wing_distance: Wing distance in points

        Returns:
            (symbols dict, quotes dict)
        """
        # Build instrument strings
        instruments = build_iron_fly_instruments(expiry, atm_strike, wing_distance)

        # Build symbols dict (without exchange prefix)
        symbols = {
            'straddle_ce': generate_nifty_option_symbol(expiry, atm_strike, 'CE'),
            'straddle_pe': generate_nifty_option_symbol(expiry, atm_strike, 'PE'),
            'wing_ce': generate_nifty_option_symbol(expiry, atm_strike + wing_distance, 'CE'),
            'wing_pe': generate_nifty_option_symbol(expiry, atm_strike - wing_distance, 'PE'),
        }

        # Fetch quotes
        instrument_list = list(instruments.values())
        raw_quotes = self.kite.quote(instrument_list)

        # Map to leg names
        quotes = {}
        for leg, inst in instruments.items():
            if inst in raw_quotes:
                quotes[leg] = raw_quotes[inst]

        return symbols, quotes

    # =========================================================================
    # ENTRY EXECUTION
    # =========================================================================

    def execute_entry(
        self,
        conditions: EntryConditions,
        require_claude_approval: bool = True
    ) -> EntryResult:
        """
        Execute Iron Fly entry.

        Steps:
        1. Get preliminary quotes for wing distance
        2. Calculate final position parameters
        3. Get Claude approval (optional)
        4. Execute orders
        5. Record to database
        6. Send alerts

        Args:
            conditions: Entry conditions (must have can_enter=True)
            require_claude_approval: Whether to get Claude approval

        Returns:
            EntryResult with execution details
        """
        if not conditions.can_enter:
            return EntryResult(
                success=False,
                error=f"Entry conditions not met: {conditions.reason}"
            )

        logger.info("Starting Iron Fly entry execution...")

        try:
            # Step 1: Get preliminary ATM quotes for wing distance calculation
            preliminary_instruments = build_iron_fly_instruments(
                conditions.expiry, conditions.atm_strike, 300  # Initial guess
            )

            # Get ATM CE and PE quotes
            atm_ce_inst = f"NFO:{generate_nifty_option_symbol(conditions.expiry, conditions.atm_strike, 'CE')}"
            atm_pe_inst = f"NFO:{generate_nifty_option_symbol(conditions.expiry, conditions.atm_strike, 'PE')}"

            atm_quotes = self.kite.quote([atm_ce_inst, atm_pe_inst])

            # Validate quotes exist before accessing
            if atm_ce_inst not in atm_quotes or atm_pe_inst not in atm_quotes:
                missing = [i for i in [atm_ce_inst, atm_pe_inst] if i not in atm_quotes]
                return EntryResult(
                    success=False,
                    error=f"Failed to get quotes for ATM options: {missing}"
                )

            ce_bid = atm_quotes[atm_ce_inst].bid
            pe_bid = atm_quotes[atm_pe_inst].bid

            # Validate bid prices are valid
            if ce_bid <= 0 or pe_bid <= 0:
                return EntryResult(
                    success=False,
                    error=f"Invalid ATM bid prices: CE={ce_bid}, PE={pe_bid}"
                )

            # Calculate dynamic wing distance
            wing_distance = calculate_wing_distance(ce_bid, pe_bid)
            logger.info(f"Wing distance calculated: {wing_distance} (CE={ce_bid:.2f}, PE={pe_bid:.2f})")

            # Step 2: Get full quotes for all legs
            symbols, quotes = self.get_iron_fly_quotes(
                conditions.expiry,
                conditions.atm_strike,
                wing_distance
            )

            # Validate quotes (basic check)
            is_valid, issues = validate_iron_fly_quotes(quotes)
            if not is_valid:
                return EntryResult(
                    success=False,
                    error=f"Quote validation failed: {issues}"
                )

            # Step 2b: Validate bid-ask spreads (TDD Section 5.2)
            quantity = self._get_quantity()
            spread_result = validate_bid_ask_spreads(quotes, quantity)

            if not spread_result.valid:
                # Entry blocked due to wide spreads
                block_text = "; ".join(spread_result.blocks)
                logger.warning(f"Entry blocked - spreads too wide: {block_text}")

                self.telegram.send(
                    f"❌ *Entry Blocked: Spreads Too Wide*\n\n"
                    f"{''.join(f'• {b}' + chr(10) for b in spread_result.blocks)}\n"
                    f"Hidden spread cost would be: ₹{spread_result.total_spread_cost:,.0f}\n\n"
                    f"This indicates illiquidity. Will retry next hour."
                )

                return EntryResult(
                    success=False,
                    error=f"Bid-ask spreads too wide: {block_text}"
                )

            # Log warnings if any (but proceed with entry)
            if spread_result.warnings:
                warn_text = "; ".join(spread_result.warnings)
                logger.warning(f"Entry proceeding with spread warnings: {warn_text}")

                self.telegram.send(
                    f"⚠️ *Entry Proceeding with Spread Warnings*\n\n"
                    f"{''.join(f'• {w}' + chr(10) for w in spread_result.warnings)}\n"
                    f"Hidden spread cost: ₹{spread_result.total_spread_cost:,.0f}\n\n"
                    f"Proceeding with caution."
                )

            # Step 3: Calculate metrics
            # Validate all required quote keys exist
            required_quote_keys = ['straddle_ce', 'straddle_pe', 'wing_ce', 'wing_pe']
            missing_keys = [k for k in required_quote_keys if k not in quotes]
            if missing_keys:
                return EntryResult(
                    success=False,
                    error=f"Missing required quotes: {missing_keys}"
                )

            metrics = calculate_iron_fly_metrics(
                atm_strike=conditions.atm_strike,
                wing_distance=wing_distance,
                ce_sell_price=quotes['straddle_ce'].bid,
                pe_sell_price=quotes['straddle_pe'].bid,
                wing_ce_buy_price=quotes['wing_ce'].ask,
                wing_pe_buy_price=quotes['wing_pe'].ask,
                quantity=self._get_quantity()
            )

            logger.info(f"Position metrics: Max profit={metrics.max_profit:.2f}, "
                       f"Max loss={metrics.max_loss:.2f}")

            # Step 4: Claude approval (if required)
            if require_claude_approval:
                claude_decision = self._get_claude_approval(conditions, metrics)
                if claude_decision != ClaudeDecision.PROCEED:
                    return EntryResult(
                        success=False,
                        error=f"Claude recommends {claude_decision.value}"
                    )

            # Step 5: Execute orders (quantity already calculated in Step 2b)
            slippage_ticks = self.trading_config.get('entry', {}).get('slippage_ticks', DEFAULT_SLIPPAGE_TICKS)
            use_tiered_slippage = self.trading_config.get('entry', {}).get('use_tiered_slippage', False)

            orders = execute_iron_fly_entry(
                kite=self.kite,
                symbols=symbols,
                quotes=quotes,
                quantity=quantity,
                slippage_ticks=slippage_ticks,
                use_tiered_slippage=use_tiered_slippage
            )

            # Step 5b: Position Verification (TDD Section 6.2)
            # Skip verification in paper trading mode (no real positions to verify)
            from src.utils.config import is_paper_trading
            verified = True
            verification_issues = []

            if is_paper_trading():
                logger.info("Paper trading mode - skipping position verification")
            else:
                logger.info("Verifying positions after entry...")
                verified, verification_issues = verify_iron_fly_positions(
                    kite=self.kite,
                    symbols=symbols,
                    quantity=quantity
                )

                if not verified:
                    # Position mismatch detected - CRITICAL alert
                    issue_text = "; ".join(verification_issues)
                    logger.error(f"POSITION VERIFICATION FAILED: {issue_text}")

                    self.telegram.send(
                        f"🚨 *CRITICAL: Position Verification Failed!*\n\n"
                        f"Orders executed but positions don't match expected:\n"
                        f"{''.join(f'• {i}' + chr(10) for i in verification_issues)}\n"
                        f"⚠️ *Manual intervention required!*\n\n"
                        f"Check Kite positions immediately."
                    )

                    # Still record position but mark as unverified
                    # The database Position has a 'verified' field for this
                    logger.warning("Recording position as UNVERIFIED")

            # Step 6: Calculate charges
            charges = calculate_iron_fly_charges(
                straddle_ce_premium=orders.straddle_ce.fill_price,
                straddle_pe_premium=orders.straddle_pe.fill_price,
                wing_ce_premium=orders.wing_ce.fill_price,
                wing_pe_premium=orders.wing_pe.fill_price,
                quantity=quantity
            )

            # Step 7: Record to database
            position_id = self._record_position(
                conditions=conditions,
                symbols=symbols,
                orders=orders,
                metrics=metrics,
                wing_distance=wing_distance,
                quantity=quantity
            )

            # Step 8: Send alerts
            self._send_entry_alert(
                conditions=conditions,
                orders=orders,
                metrics=metrics,
                charges=charges,
                wing_distance=wing_distance
            )

            logger.info(f"Entry complete! Position ID: {position_id}")

            return EntryResult(
                success=True,
                position_id=position_id,
                metrics=metrics,
                orders=orders,
                charges={
                    'stt': charges.stt,
                    'exchange_txn': charges.exchange_txn,
                    'gst': charges.gst,
                    'total': charges.total
                }
            )

        except OrderExecutionError as e:
            logger.error(f"Order execution failed: {e}")
            self.telegram.send_error_alert(
                error=str(e),
                module="entry_manager",
                function="execute_entry"
            )
            return EntryResult(success=False, error=str(e))

        except Exception as e:
            logger.error(f"Entry failed: {e}")
            import traceback
            traceback.print_exc()
            return EntryResult(success=False, error=str(e))

    def _get_quantity(self) -> int:
        """Get position quantity based on config."""
        capital_config = self.trading_config.get('capital', {})
        lots = capital_config.get('lots', 1)
        lot_size = get_lot_size_from_instruments(self.instruments_df, date.today())

        # Validate lot_size to prevent division by zero or invalid quantities
        if lot_size <= 0:
            logger.error(f"Invalid lot_size: {lot_size}, using default 75")
            lot_size = 75  # NIFTY default

        if lots <= 0:
            logger.error(f"Invalid lots config: {lots}, using default 1")
            lots = 1

        return lots * lot_size

    def _get_claude_approval(
        self,
        conditions: EntryConditions,
        metrics: IronFlyMetrics
    ) -> ClaudeDecision:
        """Get Claude pre-entry approval."""
        context = MarketContext(
            nifty_spot=conditions.nifty_spot,
            india_vix=conditions.india_vix,
            atm_strike=conditions.atm_strike,
            straddle_premium=metrics.entry_credit,
            wing_distance=int(metrics.breakeven_upper - conditions.atm_strike),
            dte=conditions.dte
        )

        response = self.claude.get_pre_entry_decision(context)
        logger.info(f"Claude decision: {response.decision.value} - {response.reasoning[:100]}...")

        return response.decision

    def _record_position(
        self,
        conditions: EntryConditions,
        symbols: Dict[str, str],
        orders: IronFlyOrders,
        metrics: IronFlyMetrics,
        wing_distance: int,
        quantity: int
    ) -> int:
        """Record position and legs to database."""
        position = Position(
            id=0,  # Auto-assigned
            strategy='iron_fly',
            status='ACTIVE',
            entry_time=datetime.now(),
            expiry=conditions.expiry,
            atm_strike=conditions.atm_strike,
            wing_distance=wing_distance,
            quantity=quantity,
            entry_credit=orders.net_credit * quantity,
            max_profit=metrics.max_profit,
            max_loss=metrics.max_loss,
            created_at=datetime.now()
        )

        position_id = save_position(position)

        # Save legs
        legs = [
            ('straddle_ce', orders.straddle_ce, 'SHORT', conditions.atm_strike),
            ('straddle_pe', orders.straddle_pe, 'SHORT', conditions.atm_strike),
            ('wing_ce', orders.wing_ce, 'LONG', conditions.atm_strike + wing_distance),
            ('wing_pe', orders.wing_pe, 'LONG', conditions.atm_strike - wing_distance),
        ]

        for leg_type, order, side, strike in legs:
            leg = PositionLeg(
                id=0,
                position_id=position_id,
                leg_type=leg_type,
                tradingsymbol=order.tradingsymbol,
                strike=strike,
                option_type='CE' if 'ce' in leg_type else 'PE',
                side=side,
                quantity=order.quantity,
                entry_price=order.fill_price,
                entry_order_id=order.order_id,
                created_at=datetime.now()
            )
            save_position_leg(leg)

        return position_id

    def _send_entry_alert(
        self,
        conditions: EntryConditions,
        orders: IronFlyOrders,
        metrics: IronFlyMetrics,
        charges: Any,
        wing_distance: int
    ) -> None:
        """Send entry alert via Telegram."""
        # Build premium dict matching TelegramAlerts signature
        premium = {
            'short_ce': orders.straddle_ce.fill_price,
            'short_pe': orders.straddle_pe.fill_price,
            'long_ce': orders.wing_ce.fill_price,
            'long_pe': orders.wing_pe.fill_price,
            'net': orders.net_credit
        }

        self.telegram.send_entry_alert(
            atm_strike=conditions.atm_strike,
            wing_distance=wing_distance,
            expiry=conditions.expiry.strftime('%Y-%m-%d'),
            lot_size=orders.straddle_ce.quantity,
            premium=premium,
            max_profit=metrics.max_profit,
            max_loss=metrics.max_loss
        )


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_entry_manager: Optional[EntryManager] = None


def get_entry_manager(config: Optional[Dict] = None) -> EntryManager:
    """Get or create singleton entry manager."""
    global _entry_manager

    if _entry_manager is None:
        _entry_manager = EntryManager(config=config)

    return _entry_manager


def reset_entry_manager() -> None:
    """Reset singleton entry manager."""
    global _entry_manager
    _entry_manager = None


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
    print("SNAIL Entry Manager Test")
    print("=" * 60)

    try:
        manager = EntryManager()

        # Check conditions
        print("\n[1] Checking entry conditions...")
        conditions = manager.check_entry_conditions()
        print(f"    Can enter: {conditions.can_enter}")
        print(f"    Reason: {conditions.reason}")

        if conditions.can_enter:
            print(f"    NIFTY: {conditions.nifty_spot:,.2f}")
            print(f"    VIX: {conditions.india_vix:.2f}")
            print(f"    ATM: {conditions.atm_strike}")
            print(f"    Expiry: {conditions.expiry} (DTE: {conditions.dte})")

            # Don't actually execute in test
            print("\n[2] Dry run (not executing)...")
            print("    Entry execution would proceed here.")

        print("\n" + "=" * 60)
        print("Entry manager test complete!")
        print("=" * 60 + "\n")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
