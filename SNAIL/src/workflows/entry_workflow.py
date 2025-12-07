"""
SNAIL Entry Workflow

Orchestrates the complete Iron Fly entry process.

@file        entry_workflow.py
@description Entry workflow orchestration
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 9.2
"""

import sys
from datetime import datetime, date, time
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass
from enum import Enum
from loguru import logger

from src.services.entry_manager import EntryManager, EntryConditions, EntryResult, get_entry_manager
from src.services.claude_advisor import ClaudeAdvisor, AdvisoryResult, get_claude_advisor
from src.api.telegram_alerts import TelegramAlerts, get_telegram
from src.api.response_handler import TelegramResponseHandler, ResponseType, get_response_handler
from src.utils.db import get_active_position, is_on_cooldown, check_system_ready
from src.utils.helpers import is_trading_day, is_market_open
from src.utils.config import get_trading_config, load_config


# =============================================================================
# CONSTANTS
# =============================================================================

# Entry time windows
ENTRY_WINDOW_START = time(9, 20)   # Allow market to settle
ENTRY_WINDOW_END = time(14, 30)    # Leave time for execution


# =============================================================================
# ENUMS
# =============================================================================

class EntryWorkflowState(Enum):
    """Entry workflow states."""
    IDLE = "idle"
    CHECKING_CONDITIONS = "checking_conditions"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class WorkflowResult:
    """
    Entry workflow result.

    Attributes:
        state: Final workflow state
        entry_result: Entry execution result if executed
        conditions: Entry conditions checked
        claude_decision: Claude advisory result
        skipped_reason: Reason if entry was skipped
        error: Error message if failed
    """
    state: EntryWorkflowState
    entry_result: Optional[EntryResult] = None
    conditions: Optional[EntryConditions] = None
    claude_decision: Optional[AdvisoryResult] = None
    skipped_reason: str = ""
    error: str = ""


# =============================================================================
# ENTRY WORKFLOW CLASS
# =============================================================================

class EntryWorkflow:
    """
    Orchestrates Iron Fly entry workflow.

    Workflow steps:
    1. Pre-checks (trading day, market hours, no active position)
    2. Entry conditions check (VIX, DTE, cooldown)
    3. Claude pre-entry advisory
    4. Optional user confirmation
    5. Entry execution
    6. Result notification

    Attributes:
        entry_manager: Entry manager service
        claude_advisor: Claude advisor service
        telegram: Telegram alerts
        response_handler: Telegram response handler
        config: Trading configuration
        require_user_confirmation: Whether to require user confirmation
    """

    def __init__(
        self,
        entry_manager: Optional[EntryManager] = None,
        claude_advisor: Optional[ClaudeAdvisor] = None,
        telegram: Optional[TelegramAlerts] = None,
        response_handler: Optional[TelegramResponseHandler] = None,
        config: Optional[Dict[str, Any]] = None,
        require_user_confirmation: bool = False
    ):
        """
        Initialize entry workflow.

        Args:
            entry_manager: Entry manager
            claude_advisor: Claude advisor
            telegram: Telegram alerts
            response_handler: Response handler
            config: Configuration
            require_user_confirmation: Require user confirmation before entry
        """
        self.config = config or load_config()
        self.trading_config = get_trading_config()

        self.entry_manager = entry_manager or get_entry_manager()
        self.claude_advisor = claude_advisor or get_claude_advisor()
        self.telegram = telegram or get_telegram()
        self.response_handler = response_handler

        self.require_user_confirmation = require_user_confirmation
        self._state = EntryWorkflowState.IDLE

        logger.info("Entry workflow initialized")

    @property
    def state(self) -> EntryWorkflowState:
        """Get current workflow state."""
        return self._state

    # =========================================================================
    # PRE-CHECKS
    # =========================================================================

    def _pre_checks(self) -> Tuple[bool, str]:
        """
        Perform pre-entry checks.

        Returns:
            (passed, reason)
        """
        # Check system status (TDD Section 4.3)
        is_ready, status_msg = check_system_ready(['normal'])
        if not is_ready:
            return False, f"System not ready: {status_msg}"

        # Check trading day
        if not is_trading_day():
            return False, "Not a trading day"

        # Check market hours
        current_time = datetime.now().time()

        if current_time < ENTRY_WINDOW_START:
            return False, f"Before entry window (opens at {ENTRY_WINDOW_START})"

        if current_time > ENTRY_WINDOW_END:
            return False, f"After entry window (closed at {ENTRY_WINDOW_END})"

        # Check for existing position
        active = get_active_position()
        if active:
            return False, f"Active position exists (ID: {active.id})"

        # Check cooldown
        if is_on_cooldown('entry'):
            return False, "Entry on cooldown (1-day after exit)"

        return True, "Pre-checks passed"

    # =========================================================================
    # MAIN WORKFLOW
    # =========================================================================

    def run(
        self,
        skip_claude: bool = False,
        skip_confirmation: bool = False
    ) -> WorkflowResult:
        """
        Execute entry workflow.

        Args:
            skip_claude: Skip Claude advisory (for testing)
            skip_confirmation: Skip user confirmation

        Returns:
            WorkflowResult with execution details
        """
        logger.info("Starting entry workflow...")
        self._state = EntryWorkflowState.CHECKING_CONDITIONS

        try:
            # Step 1: Pre-checks
            passed, reason = self._pre_checks()
            if not passed:
                logger.info(f"Pre-checks failed: {reason}")
                self._state = EntryWorkflowState.SKIPPED
                return WorkflowResult(
                    state=self._state,
                    skipped_reason=reason
                )

            # Step 2: Entry conditions check
            conditions = self.entry_manager.check_entry_conditions()

            if not conditions.can_enter:
                logger.info(f"Entry conditions not met: {conditions.reason}")
                self._state = EntryWorkflowState.SKIPPED
                return WorkflowResult(
                    state=self._state,
                    conditions=conditions,
                    skipped_reason=conditions.reason
                )

            logger.info(f"Entry conditions met: NIFTY={conditions.nifty_spot:.2f}, VIX={conditions.india_vix:.2f}")

            # Step 3: Claude pre-entry advisory
            claude_decision = None

            if not skip_claude:
                self._state = EntryWorkflowState.AWAITING_APPROVAL

                # Calculate preliminary metrics for Claude
                atm_ce_inst = f"NFO:NIFTY{conditions.expiry.strftime('%y%m%d') if conditions.expiry else ''}"
                # Get preliminary straddle premium estimate
                straddle_premium = conditions.india_vix * 20  # Rough estimate

                claude_decision = self.claude_advisor.get_pre_entry_advisory(
                    nifty_spot=conditions.nifty_spot,
                    india_vix=conditions.india_vix,
                    atm_strike=conditions.atm_strike,
                    straddle_premium=straddle_premium,
                    wing_distance=int(round(straddle_premium / 100) * 100),
                    dte=conditions.dte
                )

                if claude_decision.action_required:
                    # Claude advises against entry
                    logger.info(f"Claude advises against entry: {claude_decision.suggested_action}")
                    self._state = EntryWorkflowState.SKIPPED
                    return WorkflowResult(
                        state=self._state,
                        conditions=conditions,
                        claude_decision=claude_decision,
                        skipped_reason=f"Claude: {claude_decision.suggested_action}"
                    )

            # Step 4: User confirmation (if required)
            if self.require_user_confirmation and not skip_confirmation:
                if self.response_handler is None:
                    self.response_handler = get_response_handler()

                confirmation_msg = (
                    f"📊 *Entry Confirmation Required*\n\n"
                    f"NIFTY: ₹{conditions.nifty_spot:,.2f}\n"
                    f"VIX: {conditions.india_vix:.2f}\n"
                    f"ATM Strike: {conditions.atm_strike}\n"
                    f"Expiry: {conditions.expiry} (DTE: {conditions.dte})\n\n"
                    f"Proceed with Iron Fly entry?"
                )

                confirmed = self.response_handler.ask_confirmation(
                    confirmation_msg,
                    timeout_seconds=120
                )

                if not confirmed:
                    logger.info("User did not confirm entry")
                    self._state = EntryWorkflowState.SKIPPED
                    return WorkflowResult(
                        state=self._state,
                        conditions=conditions,
                        claude_decision=claude_decision,
                        skipped_reason="User did not confirm"
                    )

            # Step 5: Execute entry
            self._state = EntryWorkflowState.EXECUTING
            logger.info("Executing entry...")

            entry_result = self.entry_manager.execute_entry(
                conditions=conditions,
                require_claude_approval=False  # Already got approval above
            )

            if entry_result.success:
                self._state = EntryWorkflowState.COMPLETED
                logger.info(f"Entry successful! Position ID: {entry_result.position_id}")
            else:
                self._state = EntryWorkflowState.FAILED
                logger.error(f"Entry failed: {entry_result.error}")

            return WorkflowResult(
                state=self._state,
                entry_result=entry_result,
                conditions=conditions,
                claude_decision=claude_decision,
                error=entry_result.error if not entry_result.success else ""
            )

        except Exception as e:
            logger.error(f"Entry workflow error: {e}")
            import traceback
            traceback.print_exc()

            self._state = EntryWorkflowState.FAILED
            return WorkflowResult(
                state=self._state,
                error=str(e)
            )

    def run_if_appropriate(self) -> Optional[WorkflowResult]:
        """
        Run entry workflow only if conditions are likely met.

        Performs quick pre-checks before running full workflow.

        Returns:
            WorkflowResult or None if not appropriate to run
        """
        # Quick pre-checks
        passed, reason = self._pre_checks()
        if not passed:
            logger.debug(f"Entry workflow not appropriate: {reason}")
            return None

        return self.run()


# =============================================================================
# STANDALONE EXECUTION
# =============================================================================

def run_entry_workflow(
    skip_claude: bool = False,
    skip_confirmation: bool = True
) -> WorkflowResult:
    """
    Run entry workflow as standalone function.

    Args:
        skip_claude: Skip Claude advisory
        skip_confirmation: Skip user confirmation

    Returns:
        WorkflowResult
    """
    workflow = EntryWorkflow()
    return workflow.run(
        skip_claude=skip_claude,
        skip_confirmation=skip_confirmation
    )


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    from dotenv import load_dotenv

    load_dotenv()

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    print("\n" + "=" * 60)
    print("SNAIL Entry Workflow")
    print("=" * 60)

    try:
        workflow = EntryWorkflow(require_user_confirmation=False)

        # Quick check
        print("\n[1] Pre-checks...")
        passed, reason = workflow._pre_checks()
        print(f"    Passed: {passed}")
        print(f"    Reason: {reason}")

        if passed:
            print("\n[2] Would run entry workflow here...")
            print("    (Not executing to avoid actual trading)")
            print("    Run with --execute flag to actually trade")
        else:
            print("\n    Skipping workflow execution")

        print(f"\n{'='*60}")
        print(f"Entry workflow check complete")
        print(f"{'='*60}\n")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
