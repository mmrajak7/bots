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

    def _get_option_quotes_and_structure(self, atm_strike: int, expiry_date: date, dte: int) -> tuple:
        """
        Fetch actual option quotes, calculate wing distance from straddle premium,
        and format as table for Telegram.

        Wing distance = Straddle Premium (CE + PE) rounded to nearest 100.

        Args:
            atm_strike: ATM strike price
            expiry_date: Option expiry date
            dte: Days to expiry

        Returns:
            Tuple of (html_formatted_string, wing_distance, straddle_premium, net_credit, lot_size, max_profit, max_loss)
        """
        try:
            from src.utils.symbol_builder import get_lot_size_from_instruments, load_instruments
            from src.utils.config import get_instruments_path

            # Use provided expiry
            expiry = expiry_date
            expiry_str = expiry.strftime('%d-%b')

            # Get lot size from instruments
            instruments_df = load_instruments(get_instruments_path())
            lot_size = get_lot_size_from_instruments(instruments_df, expiry)
            logger.info(f"Lot size for expiry {expiry}: {lot_size}")

            # First, fetch ATM quotes to get straddle premium
            short_ce_sym = generate_nifty_option_symbol(expiry, atm_strike, 'CE')
            short_pe_sym = generate_nifty_option_symbol(expiry, atm_strike, 'PE')

            atm_quotes = self.kite.quote([f"NFO:{short_ce_sym}", f"NFO:{short_pe_sym}"])

            s_ce = atm_quotes.get(f"NFO:{short_ce_sym}")
            s_pe = atm_quotes.get(f"NFO:{short_pe_sym}")

            # Use BID for SELL orders (what we receive when selling)
            s_ce_bid = s_ce.bid if s_ce else 0
            s_pe_bid = s_pe.bid if s_pe else 0

            # Calculate straddle premium (using bid prices) and wing distance
            straddle_premium = s_ce_bid + s_pe_bid
            wing_distance = round(straddle_premium / 100) * 100

            # Ensure minimum wing distance of 200
            wing_distance = max(wing_distance, 200)

            logger.info(f"Wing distance calculated: {wing_distance} (CE Bid={s_ce_bid:.2f} + PE Bid={s_pe_bid:.2f} = {straddle_premium:.2f})")

            # Calculate wing strikes
            upper_wing = atm_strike + wing_distance
            lower_wing = atm_strike - wing_distance

            # Fetch wing quotes
            long_ce_sym = generate_nifty_option_symbol(expiry, upper_wing, 'CE')
            long_pe_sym = generate_nifty_option_symbol(expiry, lower_wing, 'PE')

            wing_quotes = self.kite.quote([f"NFO:{long_ce_sym}", f"NFO:{long_pe_sym}"])

            l_ce = wing_quotes.get(f"NFO:{long_ce_sym}")
            l_pe = wing_quotes.get(f"NFO:{long_pe_sym}")

            # Use ASK for BUY orders (what we pay when buying)
            l_ce_ask = l_ce.ask if l_ce else 0
            l_pe_ask = l_pe.ask if l_pe else 0

            # Calculate premiums and net credit using Bid/Ask
            # SELL at Bid (receive), BUY at Ask (pay)
            straddle_credit = s_ce_bid + s_pe_bid
            wing_cost = l_ce_ask + l_pe_ask
            net_credit = straddle_credit - wing_cost

            # Calculate max profit and max loss for 1 lot
            max_profit = net_credit * lot_size
            max_loss = (wing_distance - net_credit) * lot_size

            # Calculate Risk:Reward ratio (Risk = Max Loss, Reward = Max Profit)
            # RR = Max Loss / Max Profit (e.g., 1:2 means risk 1 to make 2)
            if max_profit > 0:
                rr_ratio = max_loss / max_profit
                rr_display = f"1:{1/rr_ratio:.1f}" if rr_ratio > 0 else "N/A"
                # Good RR is when reward >= 2x risk (i.e., rr_ratio <= 0.5)
                is_good_rr = rr_ratio <= 0.5
            else:
                rr_ratio = 0
                rr_display = "N/A"
                is_good_rr = False

            # Format RR line with green highlight if good
            if is_good_rr:
                rr_line = f"<b>⚖️ Risk:Reward:</b> {rr_display} ✅ <i>Good entry!</i>"
            else:
                rr_line = f"<b>⚖️ Risk:Reward:</b> {rr_display}"

            # Build HTML table format for Telegram
            # Show Bid for SELL legs, Ask for BUY legs (actual execution prices)
            html = f"""<b>🦋 IRON FLY</b>

<code>┌──────┬─────────┬─────────┐
│ Actn │ Strike  │ Bid/Ask │
├──────┼─────────┼─────────┤
│ SELL │ {atm_strike}CE │ {s_ce_bid:>7.1f} │
│ SELL │ {atm_strike}PE │ {s_pe_bid:>7.1f} │
│ BUY  │ {upper_wing}CE │ {l_ce_ask:>7.1f} │
│ BUY  │ {lower_wing}PE │ {l_pe_ask:>7.1f} │
└──────┴─────────┴─────────┘</code>

<b>💰 Net Credit:</b> ₹{net_credit:.1f} <i>({straddle_credit:.1f} - {wing_cost:.1f})</i>
<b>📈 Max Profit:</b> ₹{max_profit:,.0f} <i>({net_credit:.1f} × {lot_size})</i>
<b>📉 Max Loss:</b> ₹{max_loss:,.0f} <i>({wing_distance} - {net_credit:.1f}) × {lot_size}</i>
{rr_line}"""

            return html, wing_distance, straddle_premium, net_credit, lot_size, max_profit, max_loss

        except Exception as e:
            logger.warning(f"Could not fetch option quotes: {e}")
            import traceback
            traceback.print_exc()
            # Fallback - use defaults
            lot_size = 75
            wing_distance = 300
            html = f"""<b>🦋 IRON FLY</b>
<code>
ATM: {atm_strike} | Wings: ±{wing_distance}
</code>
<i>(Live quotes unavailable: {str(e)[:50]})</i>"""
            return html, wing_distance, 300, 0, lot_size, 0, 0

    # =========================================================================
    # ADVISORY METHODS
    # =========================================================================

    def get_pre_entry_advisory(
        self,
        nifty_spot: float,
        india_vix: float,
        atm_strike: int,
        dte: int,
        expiry_date: date = None,
        atr_14: float = 0.0
    ) -> AdvisoryResult:
        """
        Get pre-entry advisory with R:R filter and optional Claude analysis.

        Flow:
        1. Fetch real option quotes
        2. Check R:R filter (highest priority - skip if not met)
        3. Get Claude advisory (if enabled in config)
        4. Send Telegram message with quotes, events, news
        5. Wait for user response

        Args:
            nifty_spot: Current NIFTY spot
            india_vix: Current VIX
            atm_strike: Calculated ATM strike
            dte: Days to expiry
            expiry_date: Expiry date object
            atr_14: 14-day ATR (fetched if 0)

        Returns:
            AdvisoryResult with decision
        """
        from src.utils.config import get_entry_config
        from src.utils.market_events_scraper import get_events_for_telegram, get_news_for_telegram

        entry_config = get_entry_config()
        min_rr_ratio = entry_config.get('min_rr_ratio', 0.5)
        require_good_rr = entry_config.get('require_good_rr', True)
        use_claude = entry_config.get('use_claude_advisory', False)

        try:
            # Use expiry_date if provided, otherwise estimate from dte
            exp_date = expiry_date if expiry_date else (date.today() + timedelta(days=dte))
            expiry_str = exp_date.strftime('%d-%b')

            # Step 1: Fetch REAL option quotes and calculate wing distance from straddle premium
            option_html, wing_distance, straddle_premium, net_credit, lot_size, max_profit, max_loss = self._get_option_quotes_and_structure(
                atm_strike, exp_date, dte
            )

            # Calculate R:R ratio
            if max_profit > 0:
                rr_ratio = max_loss / max_profit
                rr_display = f"1:{1/rr_ratio:.1f}" if rr_ratio > 0 else "N/A"
            else:
                rr_ratio = float('inf')
                rr_display = "N/A"

            logger.info(f"Pre-entry metrics: straddle={straddle_premium:.2f}, wing={wing_distance}, credit={net_credit:.2f}, R:R={rr_display}")

            # Step 2: R:R Filter (HIGHEST PRIORITY)
            if require_good_rr and rr_ratio > min_rr_ratio:
                # R:R not met - log and skip silently (no Telegram message)
                logger.info(f"R:R filter not met: {rr_display} > required 1:{1/min_rr_ratio:.0f} - skipping entry alert")
                return AdvisoryResult(
                    decision=ClaudeDecision.SKIP,
                    reasoning=f"Risk:Reward ratio {rr_display} does not meet minimum 1:{1/min_rr_ratio:.0f} requirement",
                    confidence=1.0,
                    action_required=True,
                    suggested_action="Skip - R:R filter not met"
                )

            # Step 3: Get Claude advisory (if enabled)
            claude_reasoning = ""
            claude_decision = ClaudeDecision.PROCEED
            claude_confidence = 0.0

            if use_claude:
                # Fetch ATR for Claude context
                if atr_14 == 0.0:
                    from src.utils.db import get_today_market_data
                    market_data = get_today_market_data()
                    if market_data and market_data.atr_14:
                        atr_14 = market_data.atr_14
                    else:
                        atr_14 = 150.0

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
                claude_reasoning = response.reasoning
                claude_decision = response.decision
                claude_confidence = response.confidence

                # Record decision
                prompt = f"Pre-entry: NIFTY={nifty_spot}, VIX={india_vix}, ATM={atm_strike}, Wing={wing_distance}, DTE={dte}"
                self._record_decision(response, 'pre_entry', prompt)

            # Step 4: Get Events and News for Telegram
            events_telegram = get_events_for_telegram()
            news_telegram = get_news_for_telegram(limit=5)

            # Build Telegram message (HTML format)
            # Header
            msg_parts = [
                f"🤖 <b>Pre-Entry Analysis</b>",
                f"",
                f"📊 NIFTY <b>{nifty_spot:,.0f}</b> | 🌡️ VIX <b>{india_vix:.1f}</b> | ⏳ DTE <b>{dte}</b> ({expiry_str})",
                f"",
                option_html
            ]

            # Add Events section (if any)
            if events_telegram:
                msg_parts.append("")
                msg_parts.append(f"<b>📅 Upcoming Events:</b>")
                msg_parts.append(events_telegram)

            # Add News section (if any)
            if news_telegram:
                msg_parts.append("")
                msg_parts.append(f"<b>📰 News:</b>")
                msg_parts.append(news_telegram)

            # Add Claude analysis (if enabled)
            if use_claude and claude_reasoning:
                reasoning_escaped = claude_reasoning.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                rec_emoji = "✅" if claude_decision == ClaudeDecision.PROCEED else "⚠️"
                rec_text = "ENTER" if claude_decision == ClaudeDecision.PROCEED else "SKIP"
                msg_parts.append("")
                msg_parts.append(f"<b>📝 Claude's Analysis:</b>")
                msg_parts.append(reasoning_escaped)
                msg_parts.append(f"")
                msg_parts.append(f"{rec_emoji} <b>Recommendation: {rec_text}</b> ({claude_confidence:.0%})")

            # Add user prompt
            msg_parts.append("")
            msg_parts.append("⌨️ <i>Reply: ENTER or SKIP</i>")

            decision_msg = "\n".join(msg_parts)

            # CRITICAL: Clear any stale responses from queue BEFORE sending prompt
            # This prevents old "enter"/"skip" responses from auto-confirming new prompts
            from src.api.response_handler import get_response_handler, ResponseType, TelegramResponseHandler
            TelegramResponseHandler.clear_shared_queue()

            self.telegram.send(decision_msg, parse_mode="HTML")

            # Step 5: Wait for user response
            handler = get_response_handler()

            user_response = handler.wait_for_response(
                prompt_id=f"pre_entry_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                response_type=ResponseType.CHOICE,
                timeout_seconds=300,  # 5 minutes
                # telegram_bot normalizes all natural language to 'enter' or 'skip'
                valid_choices=['enter', 'skip']
            )

            # Determine final decision based on user input
            if user_response:
                user_choice = user_response.normalized
                if user_choice == 'enter':
                    final_decision = ClaudeDecision.PROCEED
                    self.telegram.send("✅ *Confirmed:* Proceeding with entry")
                    logger.info("User confirmed ENTER")
                else:
                    # User wants to skip - set cooldown for 10 hours
                    final_decision = ClaudeDecision.SKIP
                    set_cooldown('user_skip', 36000)  # 10 hours
                    self.telegram.send("⏭️ *Confirmed:* Skipping entry for today\n⏰ Cooldown: 10 hours")
                    logger.info("User confirmed SKIP - 10h cooldown set")
            else:
                # Timeout - default to SKIP (conservative) and set cooldown
                final_decision = ClaudeDecision.SKIP
                set_cooldown('user_skip', 36000)  # 10 hours
                self.telegram.send("⏰ *Timeout:* No response - skipping entry for today\n⏰ Cooldown: 10 hours")
                logger.info("User response timeout - defaulting to SKIP with 10h cooldown")

            return AdvisoryResult(
                decision=final_decision,
                reasoning=claude_reasoning if use_claude else f"R:R {rr_display} meets filter. User decision: {final_decision.value}",
                confidence=claude_confidence if use_claude else 1.0,
                action_required=final_decision != ClaudeDecision.PROCEED,
                suggested_action="Proceed with entry" if final_decision == ClaudeDecision.PROCEED else "Do not enter"
            )

        except Exception as e:
            logger.error(f"Error in pre-entry advisory: {e}")
            import traceback
            traceback.print_exc()
            self.telegram.send(
                f"⚠️ *Pre-Entry Error*\n\n"
                f"Failed to get entry analysis.\n"
                f"Error: {escape_markdown(str(e)[:100])}\n\n"
                f"_Check logs for details._"
            )
            return AdvisoryResult(
                decision=ClaudeDecision.SKIP,
                reasoning=f"Error: {str(e)}",
                confidence=0.0,
                action_required=True,
                suggested_action="Skip entry due to error"
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
            atm_strike=24200,  # Rounded to 100
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
