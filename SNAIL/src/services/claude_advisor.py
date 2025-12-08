"""
SNAIL Claude Advisor

AI-powered trading decision advisor using Claude for complex scenarios.

@file        claude_advisor.py
@description Claude AI advisory service for trading decisions
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 5
"""

from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from loguru import logger

from src.api.kite_client import SNAILKiteClient, get_kite_client
from src.api.telegram_alerts import TelegramAlerts, get_telegram, escape_markdown
from src.api.claude_client import (
    SNAILClaudeClient,
    MarketContext,
    ClaudeDecision,
    ClaudeResponse,
    DecisionType,
    get_claude_client
)
from src.utils.db import (
    Position,
    PositionLeg,
    get_active_position,
    get_position_legs,
    save_claude_decision,
    set_cooldown
)
from src.utils.symbol_builder import generate_nifty_option_symbol
from src.utils.calculations import calculate_position_pnl
from src.utils.config import get_trading_config, load_config
from src.utils.market_events_scraper import get_events_compact, get_news_compact


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class AdvisoryRequest:
    """
    Request for Claude advisory.

    Attributes:
        scenario: Type of scenario
        position: Current position (if any)
        nifty_spot: Current NIFTY spot
        india_vix: Current VIX
        additional_context: Extra context for prompt
    """
    scenario: str
    position: Optional[Position] = None
    nifty_spot: float = 0.0
    india_vix: float = 0.0
    additional_context: Dict[str, Any] = None

    def __post_init__(self):
        if self.additional_context is None:
            self.additional_context = {}


@dataclass
class AdvisoryResult:
    """
    Result of Claude advisory.

    Attributes:
        decision: Claude's decision
        reasoning: Reasoning explanation
        confidence: Confidence level
        action_required: Whether action is recommended
        suggested_action: Suggested action if any
    """
    decision: ClaudeDecision
    reasoning: str
    confidence: float
    action_required: bool
    suggested_action: str = ""


# =============================================================================
# CLAUDE ADVISOR CLASS
# =============================================================================

class ClaudeAdvisor:
    """
    AI-powered trading decision advisor.

    Uses Claude for:
    - Pre-entry validation
    - Stop loss decisions (hold vs exit)
    - Wing approach advisory
    - Friday close decisions
    - VIX spike responses
    - Gap open handling
    - Position adjustments

    Attributes:
        kite: Kite client
        telegram: Telegram client
        claude: Claude client
        config: Configuration
    """

    def __init__(
        self,
        kite: Optional[SNAILKiteClient] = None,
        telegram: Optional[TelegramAlerts] = None,
        claude: Optional[SNAILClaudeClient] = None,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize Claude advisor.

        Args:
            kite: Kite client
            telegram: Telegram client
            claude: Claude client
            config: Configuration
        """
        self.config = config or load_config()
        self.trading_config = get_trading_config()

        self.kite = kite or get_kite_client(self.config)
        self.telegram = telegram or get_telegram()
        self.claude = claude or get_claude_client()

        logger.info("Claude advisor initialized")

    # =========================================================================
    # CONTEXT BUILDING
    # =========================================================================

    def _build_context(
        self,
        position: Optional[Position] = None,
        current_pnl: float = 0.0
    ) -> MarketContext:
        """
        Build market context for Claude prompts.

        Args:
            position: Current position (if any)
            current_pnl: Current position P&L

        Returns:
            MarketContext for prompts
        """
        # Get market data
        nifty_spot = self.kite.get_nifty_spot()
        india_vix = self.kite.get_india_vix()

        # Build context
        context = MarketContext(
            nifty_spot=nifty_spot,
            india_vix=india_vix
        )

        if position:
            context.atm_strike = position.atm_strike
            context.wing_distance = position.wing_distance
            context.position_pnl = current_pnl
            context.dte = (position.expiry_date - date.today()).days if position.expiry_date else 0

            # Add straddle premium if available
            if position.entry_premium and position.lot_size and position.lot_size > 0:
                context.straddle_premium = position.entry_premium / position.lot_size

        return context

    def _get_position_pnl(self, position: Position, legs: List[PositionLeg]) -> float:
        """Calculate current position P&L."""
        if not legs:
            return 0.0

        # Get quotes
        instruments = []
        leg_map = {}

        for leg in legs:
            inst = f"NFO:{leg.tradingsymbol}"
            instruments.append(inst)
            leg_map[inst] = leg

        quotes = self.kite.quote(instruments)

        # Calculate P&L
        entry_straddle = sum(l.entry_price for l in legs if l.side == 'SHORT')
        entry_wing = sum(l.entry_price for l in legs if l.side == 'LONG')

        current_straddle = 0.0
        current_wing = 0.0

        for inst, quote in quotes.items():
            leg = leg_map.get(inst)
            if leg:
                if leg.side == 'SHORT':
                    current_straddle += quote.ask
                else:
                    current_wing += quote.bid

        pnl, _ = calculate_position_pnl(
            entry_straddle, entry_wing, current_straddle, current_wing,
            position.quantity
        )

        return pnl

    def _record_decision(
        self,
        response: ClaudeResponse,
        trigger_type: str,
        prompt: str,
        position_id: Optional[int] = None
    ) -> None:
        """Record Claude decision to database."""
        try:
            # Log warning if truncation occurs
            if len(prompt) > 2000:
                logger.warning(f"Truncating prompt from {len(prompt)} to 2000 chars")
            if len(response.reasoning) > 2000:
                logger.warning(f"Truncating response from {len(response.reasoning)} to 2000 chars")

            save_claude_decision(
                position_id=position_id,
                trigger_type=trigger_type,
                prompt=prompt[:2000],
                response=response.reasoning[:2000],
                decision=response.decision.value if response.decision else None,
                model_used='haiku',
                tokens_used=None
            )
        except Exception as e:
            logger.warning(f"Failed to record Claude decision: {e}")

    # =========================================================================
    # HELPER METHODS
    # =========================================================================

    def _get_option_quotes_table(self, atm_strike: int, wing_distance: int, expiry_date: date) -> str:
        """
        Fetch actual option quotes and format as table.

        Args:
            atm_strike: ATM strike price
            wing_distance: Wing distance from ATM
            expiry_date: Option expiry date

        Returns:
            Formatted table string for Telegram
        """
        try:
            # Use provided expiry
            expiry = expiry_date
            expiry_str = expiry.strftime('%d-%b')

            # Calculate wing strikes
            upper_wing = atm_strike + wing_distance
            lower_wing = atm_strike - wing_distance

            # Build symbols
            short_ce_sym = generate_nifty_option_symbol(expiry, atm_strike, 'CE')
            short_pe_sym = generate_nifty_option_symbol(expiry, atm_strike, 'PE')
            long_ce_sym = generate_nifty_option_symbol(expiry, upper_wing, 'CE')
            long_pe_sym = generate_nifty_option_symbol(expiry, lower_wing, 'PE')

            # Fetch quotes
            instruments = [
                f"NFO:{short_ce_sym}",
                f"NFO:{short_pe_sym}",
                f"NFO:{long_ce_sym}",
                f"NFO:{long_pe_sym}"
            ]

            quotes = self.kite.quote(instruments)

            # Build table
            rows = []
            rows.append(f"{'Leg':<6} {'Strike':>6} {'LTP':>7} {'Bid':>7} {'Ask':>7}")
            rows.append("-" * 40)

            for inst, label, strike in [
                (f"NFO:{short_ce_sym}", "S-CE", atm_strike),
                (f"NFO:{short_pe_sym}", "S-PE", atm_strike),
                (f"NFO:{long_ce_sym}", "L-CE", upper_wing),
                (f"NFO:{long_pe_sym}", "L-PE", lower_wing),
            ]:
                q = quotes.get(inst)
                if q:
                    rows.append(f"{label:<6} {strike:>6} {q.ltp:>7.1f} {q.bid:>7.1f} {q.ask:>7.1f}")
                else:
                    rows.append(f"{label:<6} {strike:>6} {'N/A':>7} {'N/A':>7} {'N/A':>7}")

            rows.append("-" * 40)
            rows.append(f"Expiry: {expiry_str} | Wings: ±{wing_distance}")

            return '\n'.join(rows)

        except Exception as e:
            logger.warning(f"Could not fetch option quotes: {e}")
            return f"ATM: {atm_strike} | Wings: ±{wing_distance}\n(Live quotes unavailable)"

    # =========================================================================
    # ADVISORY METHODS
    # =========================================================================

    def get_pre_entry_advisory(
        self,
        nifty_spot: float,
        india_vix: float,
        atm_strike: int,
        straddle_premium: float,
        wing_distance: int,
        dte: int,
        expiry_date: date = None,
        net_credit: float = 0.0,
        max_profit: float = 0.0,
        max_loss: float = 0.0,
        atr_14: float = 0.0
    ) -> AdvisoryResult:
        """
        Get pre-entry advisory from Claude.

        Called before entering a new position.

        Args:
            nifty_spot: Current NIFTY spot
            india_vix: Current VIX
            atm_strike: Calculated ATM strike
            straddle_premium: Expected straddle premium
            wing_distance: Calculated wing distance
            dte: Days to expiry
            expiry_date: Expiry date object
            net_credit: Net credit per lot
            max_profit: Max profit per lot
            max_loss: Max loss per lot
            atr_14: 14-day ATR (fetched if 0)

        Returns:
            AdvisoryResult with decision
        """
        try:
            # Fetch ATR if not provided
            if atr_14 == 0.0:
                from src.utils.db import get_today_market_data
                market_data = get_today_market_data()
                if market_data and market_data.atr_14:
                    atr_14 = market_data.atr_14
                else:
                    atr_14 = 150.0  # Default fallback
                    logger.warning("ATR not available, using default 150")

            # Calculate metrics if not provided
            if net_credit == 0.0:
                net_credit = straddle_premium - (straddle_premium * 0.3)  # Rough estimate
            if max_profit == 0.0:
                max_profit = net_credit * 75  # 1 lot
            if max_loss == 0.0:
                max_loss = (wing_distance - net_credit) * 75

            # Get scraped market events and news for Claude context
            events_str = get_events_compact(days=10)
            news_str = get_news_compact(limit=10)

            context = MarketContext(
                nifty_spot=nifty_spot,
                india_vix=india_vix,
                atm_strike=atm_strike,
                straddle_premium=straddle_premium,
                wing_distance=wing_distance,
                dte=dte,
                atr_14=atr_14,
                net_credit=net_credit,
                max_profit=max_profit,
                max_loss=max_loss,
                expiry_date=expiry_date,
                additional_context={
                    'market_events': events_str,
                    'market_news': news_str
                }
            )

            response = self.claude.get_pre_entry_decision(context)

            # Record decision
            prompt = f"Pre-entry check: NIFTY={nifty_spot}, VIX={india_vix}, ATM={atm_strike}, DTE={dte}"
            self._record_decision(response, 'pre_entry', prompt)

            # Fetch actual option quotes for display
            # Use expiry_date if provided, otherwise estimate from dte
            exp_date = expiry_date if expiry_date else (date.today() + timedelta(days=dte))
            option_table = self._get_option_quotes_table(atm_strike, wing_distance, exp_date)

            # Determine emoji based on Claude's recommendation
            rec_emoji = "✅" if response.decision == ClaudeDecision.PROCEED else "⚠️"
            rec_text = "ENTER" if response.decision == ClaudeDecision.PROCEED else "SKIP"

            # Send analysis to user with decision options
            decision_msg = (
                f"🤖 *Pre-Entry Analysis*\n\n"
                f"📊 NIFTY {nifty_spot:,.0f} | 🌡️ VIX {india_vix:.1f} | ⏳ DTE {dte}\n\n"
                f"*🦋 Iron Fly Structure:*\n"
                f"```\n{option_table}```\n\n"
                f"💰 *Est. Net Credit:* ₹{net_credit:.0f}/lot\n"
                f"✅ Max Profit: ₹{max_profit:,.0f} | ⛔ Max Loss: ₹{max_loss:,.0f}\n\n"
                f"*Claude's Analysis:*\n{escape_markdown(response.reasoning[:350])}\n\n"
                f"{rec_emoji} *Recommendation: {rec_text}*\n"
                f"🎯 Confidence: {response.confidence:.0%}\n\n"
                f"⌨️ _Reply: ENTER or SKIP_"
            )
            self.telegram.send(decision_msg)

            # Wait for user response
            from src.api.response_handler import get_response_handler, ResponseType
            handler = get_response_handler()

            user_response = handler.wait_for_response(
                prompt_id=f"pre_entry_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                response_type=ResponseType.CHOICE,
                timeout_seconds=300,  # 5 minutes
                valid_choices=['enter', 'skip', 'proceed', 'yes', 'no']
            )

            # Determine final decision based on user input
            if user_response:
                user_choice = user_response.normalized
                if user_choice in ['enter', 'proceed', 'yes']:
                    # User wants to enter (override if Claude said skip)
                    final_decision = ClaudeDecision.PROCEED
                    if response.decision != ClaudeDecision.PROCEED:
                        self.telegram.send("✅ *User override:* Proceeding with entry despite Claude's SKIP recommendation")
                        logger.info("User overrode Claude SKIP recommendation - proceeding with entry")
                    else:
                        self.telegram.send("✅ *Confirmed:* Proceeding with entry")
                else:
                    # User wants to skip - set cooldown for rest of day
                    final_decision = ClaudeDecision.SKIP
                    set_cooldown('user_skip', 86400)  # 24 hours (will expire next day)
                    if response.decision == ClaudeDecision.PROCEED:
                        self.telegram.send("⏭️ *User override:* Skipping entry for today despite Claude's ENTER recommendation")
                        logger.info("User overrode Claude PROCEED recommendation - skipping entry, cooldown set")
                    else:
                        self.telegram.send("⏭️ *Confirmed:* Skipping entry for today")
                        logger.info("User confirmed SKIP - cooldown set for rest of day")
            else:
                # Timeout - default to SKIP (conservative) and set cooldown
                final_decision = ClaudeDecision.SKIP
                set_cooldown('user_skip', 86400)  # Skip for rest of day on timeout
                self.telegram.send(f"⏰ *Timeout:* No response - skipping entry for today (conservative default)")
                logger.info(f"User response timeout - defaulting to SKIP with cooldown")

            return AdvisoryResult(
                decision=final_decision,
                reasoning=response.reasoning,
                confidence=response.confidence,
                action_required=final_decision != ClaudeDecision.PROCEED,
                suggested_action="Proceed with entry" if final_decision == ClaudeDecision.PROCEED else "Do not enter"
            )

        except Exception as e:
            logger.error(f"Error in pre-entry advisory: {e}")
            self.telegram.send(
                f"⚠️ *Claude Advisory Error*\n\n"
                f"Failed to get pre-entry decision.\n"
                f"Error: {escape_markdown(str(e)[:100])}\n\n"
                f"_Defaulting to SKIP entry._"
            )
            return AdvisoryResult(
                decision=ClaudeDecision.SKIP,
                reasoning=f"Error getting advisory: {str(e)}",
                confidence=0.0,
                action_required=True,
                suggested_action="Skip entry due to advisory error"
            )

    def get_stop_loss_advisory(self) -> AdvisoryResult:
        """
        Get stop loss advisory when position is at 50% max loss.

        Returns:
            AdvisoryResult with decision to hold or exit
        """
        position = get_active_position()
        if not position:
            return AdvisoryResult(
                decision=ClaudeDecision.UNKNOWN,
                reasoning="No active position",
                confidence=0.0,
                action_required=False
            )

        try:
            legs = get_position_legs(position.id)
            current_pnl = self._get_position_pnl(position, legs)

            context = self._build_context(position, current_pnl)
            response = self.claude.get_stop_loss_advisory(context)

            # Record decision
            prompt = f"Stop loss advisory: P&L={current_pnl}, Position={position.id}"
            self._record_decision(response, 'stop_loss_advisory', prompt, position.id)

            # Notify via Telegram
            self.telegram.send(
                f"🤖 *Stop Loss Advisory*\n\n"
                f"Decision: *{response.decision.value}*\n"
                f"Current P&L: ₹{current_pnl:,.2f}\n\n"
                f"Reasoning:\n{escape_markdown(response.reasoning[:300])}..."
            )

            return AdvisoryResult(
                decision=response.decision,
                reasoning=response.reasoning,
                confidence=response.confidence,
                action_required=response.decision == ClaudeDecision.EXIT,
                suggested_action="Exit position" if response.decision == ClaudeDecision.EXIT else "Hold position"
            )

        except Exception as e:
            logger.error(f"Error in stop loss advisory: {e}")
            self.telegram.send(
                f"⚠️ *Claude Advisory Error*\n\n"
                f"Failed to get stop loss decision.\n"
                f"Error: {escape_markdown(str(e)[:100])}\n\n"
                f"_Defaulting to HOLD. Please check position manually._"
            )
            return AdvisoryResult(
                decision=ClaudeDecision.HOLD,
                reasoning=f"Error getting advisory: {str(e)}",
                confidence=0.0,
                action_required=False,
                suggested_action="Hold due to advisory error - check manually"
            )

    def get_wing_approach_advisory(self, direction: str) -> AdvisoryResult:
        """
        Get advisory when NIFTY approaches wing strike.

        Args:
            direction: "CE" for upper wing, "PE" for lower wing

        Returns:
            AdvisoryResult with decision
        """
        position = get_active_position()
        if not position:
            return AdvisoryResult(
                decision=ClaudeDecision.UNKNOWN,
                reasoning="No active position",
                confidence=0.0,
                action_required=False
            )

        try:
            legs = get_position_legs(position.id)
            current_pnl = self._get_position_pnl(position, legs)

            context = self._build_context(position, current_pnl)
            context.additional_context['approaching_wing'] = direction

            response = self.claude.get_wing_approach_advisory(context)

            # Record decision
            prompt = f"Wing approach ({direction}): P&L={current_pnl}, Position={position.id}"
            self._record_decision(response, 'wing_approach', prompt, position.id)

            # Send Telegram alert
            self.telegram.send(
                f"🤖 *Wing Approach Advisory* ({direction})\n\n"
                f"Decision: *{response.decision.value}*\n"
                f"Current P&L: ₹{current_pnl:,.2f}\n\n"
                f"Reasoning:\n{escape_markdown(response.reasoning[:300])}..."
            )

            return AdvisoryResult(
                decision=response.decision,
                reasoning=response.reasoning,
                confidence=response.confidence,
                action_required=response.decision in [ClaudeDecision.EXIT, ClaudeDecision.ADJUST],
                suggested_action=response.decision.value
            )

        except Exception as e:
            logger.error(f"Error in wing approach advisory: {e}")
            self.telegram.send(
                f"⚠️ *Claude Advisory Error*\n\n"
                f"Failed to get wing approach decision.\n"
                f"Error: {escape_markdown(str(e)[:100])}\n\n"
                f"_Defaulting to HOLD. Please check position manually._"
            )
            return AdvisoryResult(
                decision=ClaudeDecision.HOLD,
                reasoning=f"Error getting advisory: {str(e)}",
                confidence=0.0,
                action_required=False,
                suggested_action="Hold due to advisory error - check manually"
            )

    def get_friday_decision(self) -> AdvisoryResult:
        """
        Get Friday close decision.

        Called on Friday afternoon to decide whether to hold over weekend.

        Returns:
            AdvisoryResult with decision
        """
        position = get_active_position()
        if not position:
            return AdvisoryResult(
                decision=ClaudeDecision.UNKNOWN,
                reasoning="No active position",
                confidence=0.0,
                action_required=False
            )

        try:
            legs = get_position_legs(position.id)
            current_pnl = self._get_position_pnl(position, legs)

            context = self._build_context(position, current_pnl)
            response = self.claude.get_friday_decision(context)

            # Record decision
            prompt = f"Friday decision: P&L={current_pnl}, Position={position.id}"
            self._record_decision(response, 'friday_decision', prompt, position.id)

            # Send decision to user
            self.telegram.send(
                f"🤖 *Friday Decision Advisory*\n\n"
                f"Decision: *{response.decision.value}*\n"
                f"Current P&L: ₹{current_pnl:,.2f}\n\n"
                f"Reasoning:\n{escape_markdown(response.reasoning[:300])}..."
            )

            return AdvisoryResult(
                decision=response.decision,
                reasoning=response.reasoning,
                confidence=response.confidence,
                action_required=response.decision == ClaudeDecision.EXIT,
                suggested_action="Exit before weekend" if response.decision == ClaudeDecision.EXIT else "Hold over weekend"
            )

        except Exception as e:
            logger.error(f"Error in Friday decision advisory: {e}")
            self.telegram.send(
                f"⚠️ *Claude Advisory Error*\n\n"
                f"Failed to get Friday decision.\n"
                f"Error: {escape_markdown(str(e)[:100])}\n\n"
                f"_Defaulting to HOLD. Please check position manually._"
            )
            return AdvisoryResult(
                decision=ClaudeDecision.HOLD,
                reasoning=f"Error getting advisory: {str(e)}",
                confidence=0.0,
                action_required=False,
                suggested_action="Hold due to advisory error - check manually"
            )

    def get_vix_spike_advisory(self, current_vix: float, previous_vix: float) -> AdvisoryResult:
        """
        Get advisory for VIX spike.

        Args:
            current_vix: Current VIX value
            previous_vix: VIX at position entry

        Returns:
            AdvisoryResult with decision
        """
        position = get_active_position()
        if not position:
            return AdvisoryResult(
                decision=ClaudeDecision.UNKNOWN,
                reasoning="No active position",
                confidence=0.0,
                action_required=False
            )

        try:
            legs = get_position_legs(position.id)
            current_pnl = self._get_position_pnl(position, legs)

            context = self._build_context(position, current_pnl)
            context.additional_context['vix_change'] = current_vix - previous_vix
            context.additional_context['previous_vix'] = previous_vix

            response = self.claude.get_vix_spike_advisory(context)

            # Record decision
            prompt = f"VIX spike: VIX={previous_vix:.2f}→{current_vix:.2f}, P&L={current_pnl}, Position={position.id}"
            self._record_decision(response, 'vix_spike', prompt, position.id)

            # Notify via Telegram
            self.telegram.send(
                f"🤖 *VIX Spike Advisory*\n\n"
                f"VIX Change: {previous_vix:.2f} → {current_vix:.2f}\n"
                f"Decision: *{response.decision.value}*\n\n"
                f"Reasoning:\n{escape_markdown(response.reasoning[:300])}..."
            )

            return AdvisoryResult(
                decision=response.decision,
                reasoning=response.reasoning,
                confidence=response.confidence,
                action_required=response.decision == ClaudeDecision.EXIT,
                suggested_action=response.decision.value
            )

        except Exception as e:
            logger.error(f"Error in VIX spike advisory: {e}")
            self.telegram.send(
                f"⚠️ *Claude Advisory Error*\n\n"
                f"Failed to get VIX spike decision.\n"
                f"Error: {escape_markdown(str(e)[:100])}\n\n"
                f"_Defaulting to HOLD. Please check position manually._"
            )
            return AdvisoryResult(
                decision=ClaudeDecision.HOLD,
                reasoning=f"Error getting advisory: {str(e)}",
                confidence=0.0,
                action_required=False,
                suggested_action="Hold due to advisory error - check manually"
            )

    def get_gap_open_advisory(
        self,
        gap_size: float,
        gap_direction: str,
        opened_beyond_wing: bool
    ) -> AdvisoryResult:
        """
        Get advisory for gap open beyond wing.

        Called at 9:16 AM if market gaps beyond wing strike.

        Args:
            gap_size: Gap size in points
            gap_direction: "UP" or "DOWN"
            opened_beyond_wing: Whether price opened beyond wing

        Returns:
            AdvisoryResult with decision
        """
        position = get_active_position()
        if not position:
            return AdvisoryResult(
                decision=ClaudeDecision.UNKNOWN,
                reasoning="No active position",
                confidence=0.0,
                action_required=False
            )

        try:
            legs = get_position_legs(position.id)
            current_pnl = self._get_position_pnl(position, legs)

            context = self._build_context(position, current_pnl)
            context.additional_context.update({
                'gap_size': gap_size,
                'gap_direction': gap_direction,
                'beyond_wing': opened_beyond_wing
            })

            response = self.claude.get_gap_advisory(context)

            # Record decision
            prompt = f"Gap open: gap={gap_size:.0f} pts {gap_direction}, beyond_wing={opened_beyond_wing}, Position={position.id}"
            self._record_decision(response, 'gap_beyond_wing', prompt, position.id)

            # Send alert via Telegram
            self.telegram.send(
                f"🤖 *Gap Open Advisory*\n\n"
                f"Gap: {gap_size:.0f} pts {gap_direction}\n"
                f"Beyond Wing: {'Yes' if opened_beyond_wing else 'No'}\n"
                f"Decision: *{response.decision.value}*\n\n"
                f"Reasoning:\n{escape_markdown(response.reasoning[:300])}..."
            )

            return AdvisoryResult(
                decision=response.decision,
                reasoning=response.reasoning,
                confidence=response.confidence,
                action_required=True,  # Gap scenarios always need attention
                suggested_action=response.decision.value
            )

        except Exception as e:
            logger.error(f"Error in gap open advisory: {e}")
            self.telegram.send(
                f"⚠️ *Claude Advisory Error*\n\n"
                f"Failed to get gap open decision.\n"
                f"Error: {escape_markdown(str(e)[:100])}\n\n"
                f"_Defaulting to HOLD. Please check position manually._"
            )
            return AdvisoryResult(
                decision=ClaudeDecision.HOLD,
                reasoning=f"Error getting advisory: {str(e)}",
                confidence=0.0,
                action_required=True,  # Gap scenarios always need attention
                suggested_action="Hold due to advisory error - check manually"
            )

    def get_market_event_advisory(self, event_description: str) -> AdvisoryResult:
        """
        Get advisory for unexpected market event.

        Args:
            event_description: Description of the event

        Returns:
            AdvisoryResult with decision
        """
        position = get_active_position()
        if not position:
            return AdvisoryResult(
                decision=ClaudeDecision.HOLD,
                reasoning="No active position to manage",
                confidence=1.0,
                action_required=False
            )

        try:
            legs = get_position_legs(position.id)
            current_pnl = self._get_position_pnl(position, legs)

            context = self._build_context(position, current_pnl)
            response = self.claude.get_market_event_advisory(context, event_description)

            # Record decision
            prompt = f"Market event: {event_description[:100]}, P&L={current_pnl}, Position={position.id}"
            self._record_decision(response, 'market_event', prompt, position.id)

            # Notify via Telegram
            self.telegram.send(
                f"🤖 *Market Event Advisory*\n\n"
                f"Event: {escape_markdown(event_description[:100])}\n"
                f"Decision: *{response.decision.value}*\n\n"
                f"Reasoning:\n{escape_markdown(response.reasoning[:300])}..."
            )

            return AdvisoryResult(
                decision=response.decision,
                reasoning=response.reasoning,
                confidence=response.confidence,
                action_required=response.decision in [ClaudeDecision.EXIT, ClaudeDecision.ADJUST],
                suggested_action=response.decision.value
            )

        except Exception as e:
            logger.error(f"Error in market event advisory: {e}")
            self.telegram.send(
                f"⚠️ *Claude Advisory Error*\n\n"
                f"Failed to get market event decision.\n"
                f"Error: {escape_markdown(str(e)[:100])}\n\n"
                f"_Defaulting to HOLD. Please check position manually._"
            )
            return AdvisoryResult(
                decision=ClaudeDecision.HOLD,
                reasoning=f"Error getting advisory: {str(e)}",
                confidence=0.0,
                action_required=False,
                suggested_action="Hold due to advisory error - check manually"
            )


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_claude_advisor: Optional[ClaudeAdvisor] = None


def get_claude_advisor(config: Optional[Dict] = None) -> ClaudeAdvisor:
    """Get or create singleton Claude advisor."""
    global _claude_advisor

    if _claude_advisor is None:
        _claude_advisor = ClaudeAdvisor(config=config)

    return _claude_advisor


def reset_claude_advisor() -> None:
    """Reset singleton Claude advisor."""
    global _claude_advisor
    _claude_advisor = None


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
    print("SNAIL Claude Advisor Test")
    print("=" * 60)

    try:
        advisor = ClaudeAdvisor()

        # Test pre-entry advisory
        print("\n[1] Testing pre-entry advisory...")
        result = advisor.get_pre_entry_advisory(
            nifty_spot=24150.00,
            india_vix=12.5,
            atm_strike=24150,
            straddle_premium=320.0,
            wing_distance=300,
            dte=6
        )
        print(f"    Decision: {result.decision.value}")
        print(f"    Confidence: {result.confidence:.0%}")
        print(f"    Action required: {result.action_required}")
        print(f"    Suggested: {result.suggested_action}")

        # Check for active position
        print("\n[2] Checking for active position advisory...")
        position = get_active_position()
        if position:
            print(f"    Found position ID: {position.id}")

            # Test stop loss advisory
            print("\n[3] Testing stop loss advisory...")
            sl_result = advisor.get_stop_loss_advisory()
            print(f"    Decision: {sl_result.decision.value}")
            print(f"    Action required: {sl_result.action_required}")
        else:
            print("    No active position, skipping position-specific tests")

        print("\n" + "=" * 60)
        print("Claude advisor test complete!")
        print("=" * 60 + "\n")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
