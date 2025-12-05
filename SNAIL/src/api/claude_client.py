"""
SNAIL Claude AI Client

Integration with Anthropic Claude API for trading decision advisory.

@file        claude_client.py
@description Claude AI client for trading decisions
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 5
"""

import os
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from enum import Enum
import anthropic
from loguru import logger

from src.utils.config import get_claude_config, get_prompt_path, PROJECT_ROOT


# =============================================================================
# CONSTANTS
# =============================================================================

# Claude model configuration
DEFAULT_MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.3  # Lower = more deterministic

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY = 2

# Decision types
DECISION_TYPES = [
    'pre_entry',
    'stop_loss_advisory',
    'wing_approach',
    'friday_decision',
    'vix_spike',
    'market_event',
    'gap_beyond_wing',
    'adjustment'
]


# =============================================================================
# ENUMS
# =============================================================================

class DecisionType(Enum):
    """Types of Claude decisions."""
    PRE_ENTRY = "pre_entry"
    STOP_LOSS = "stop_loss_advisory"
    WING_APPROACH = "wing_approach"
    FRIDAY = "friday_decision"
    VIX_SPIKE = "vix_spike"
    MARKET_EVENT = "market_event"
    GAP_BEYOND_WING = "gap_beyond_wing"
    ADJUSTMENT = "adjustment"


class ClaudeDecision(Enum):
    """Possible Claude decisions."""
    PROCEED = "PROCEED"
    HOLD = "HOLD"
    EXIT = "EXIT"
    SKIP = "SKIP"
    WAIT = "WAIT"
    ADJUST = "ADJUST"
    UNKNOWN = "UNKNOWN"


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class ClaudeResponse:
    """
    Structured Claude response.

    Attributes:
        decision: Extracted decision (PROCEED, HOLD, EXIT, etc.)
        reasoning: Claude's reasoning
        confidence: Confidence score (0-1) if provided
        raw_response: Full response text
        prompt_tokens: Tokens used for prompt
        completion_tokens: Tokens used for completion
        timestamp: Response timestamp
        decision_type: Type of decision requested
    """
    decision: ClaudeDecision
    reasoning: str
    raw_response: str
    prompt_tokens: int
    completion_tokens: int
    decision_type: str
    confidence: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def total_tokens(self) -> int:
        """Total tokens used."""
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            'decision': self.decision.value,
            'reasoning': self.reasoning,
            'confidence': self.confidence,
            'raw_response': self.raw_response,
            'prompt_tokens': self.prompt_tokens,
            'completion_tokens': self.completion_tokens,
            'decision_type': self.decision_type,
            'timestamp': self.timestamp.isoformat()
        }


@dataclass
class MarketContext:
    """
    Market context for Claude prompts.

    Attributes:
        nifty_spot: Current NIFTY spot price
        india_vix: Current India VIX
        atm_strike: ATM strike price
        straddle_premium: Total straddle premium
        wing_distance: Wing distance in points
        position_pnl: Current position P&L (if any)
        dte: Days to expiry
        time_str: Current time string
    """
    nifty_spot: float
    india_vix: float
    atm_strike: int = 0
    straddle_premium: float = 0.0
    wing_distance: int = 0
    position_pnl: float = 0.0
    dte: int = 0
    time_str: str = ""
    additional_context: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.time_str:
            self.time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# =============================================================================
# PROMPT LOADING
# =============================================================================

def load_prompt_template(prompt_name: str) -> str:
    """
    Load prompt template from file.

    Args:
        prompt_name: Name of prompt (without .txt extension)

    Returns:
        Prompt template string

    Raises:
        FileNotFoundError: If prompt file doesn't exist
    """
    prompt_path = get_prompt_path(prompt_name)

    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt template not found: {prompt_path}")

    return prompt_path.read_text(encoding='utf-8')


def format_prompt(template: str, context: MarketContext) -> str:
    """
    Format prompt template with market context.

    Args:
        template: Prompt template with placeholders
        context: Market context for substitution

    Returns:
        Formatted prompt string
    """
    # Build substitution dict
    subs = {
        'nifty_spot': f"{context.nifty_spot:,.2f}",
        'india_vix': f"{context.india_vix:.2f}",
        'atm_strike': str(context.atm_strike),
        'straddle_premium': f"{context.straddle_premium:.2f}",
        'wing_distance': str(context.wing_distance),
        'position_pnl': f"{context.position_pnl:,.2f}",
        'dte': str(context.dte),
        'time': context.time_str,
        'date': datetime.now().strftime("%Y-%m-%d"),
    }

    # Add additional context
    subs.update({k: str(v) for k, v in context.additional_context.items()})

    # Perform substitution
    formatted = template
    for key, value in subs.items():
        formatted = formatted.replace(f"{{{key}}}", value)
        formatted = formatted.replace(f"${{{key}}}", value)

    return formatted


# =============================================================================
# RESPONSE PARSING
# =============================================================================

def parse_decision(response_text: str) -> ClaudeDecision:
    """
    Parse decision from Claude response.

    Looks for keywords indicating decision.

    Args:
        response_text: Raw Claude response

    Returns:
        Extracted decision enum
    """
    text_upper = response_text.upper()

    # Check for explicit decisions
    if "DECISION: PROCEED" in text_upper or "PROCEED WITH" in text_upper:
        return ClaudeDecision.PROCEED
    elif "DECISION: EXIT" in text_upper or "RECOMMEND EXIT" in text_upper:
        return ClaudeDecision.EXIT
    elif "DECISION: HOLD" in text_upper or "RECOMMEND HOLD" in text_upper:
        return ClaudeDecision.HOLD
    elif "DECISION: SKIP" in text_upper or "SKIP TODAY" in text_upper:
        return ClaudeDecision.SKIP
    elif "DECISION: WAIT" in text_upper or "RECOMMEND WAIT" in text_upper:
        return ClaudeDecision.WAIT
    elif "DECISION: ADJUST" in text_upper or "RECOMMEND ADJUST" in text_upper:
        return ClaudeDecision.ADJUST

    # Look for less explicit indicators
    proceed_indicators = ['approve', 'favorable', 'good conditions', 'proceed', 'go ahead']
    hold_indicators = ['hold', 'wait', 'monitor', 'not yet', 'patience']
    exit_indicators = ['exit', 'close', 'square off', 'risk too high']
    skip_indicators = ['skip', 'not today', 'avoid', 'unfavorable']

    text_lower = response_text.lower()

    for indicator in exit_indicators:
        if indicator in text_lower:
            return ClaudeDecision.EXIT

    for indicator in skip_indicators:
        if indicator in text_lower:
            return ClaudeDecision.SKIP

    for indicator in hold_indicators:
        if indicator in text_lower:
            return ClaudeDecision.HOLD

    for indicator in proceed_indicators:
        if indicator in text_lower:
            return ClaudeDecision.PROCEED

    return ClaudeDecision.UNKNOWN


def extract_confidence(response_text: str) -> float:
    """
    Extract confidence score from response if present.

    Args:
        response_text: Raw Claude response

    Returns:
        Confidence score (0-1), 0.5 if not found
    """
    import re

    # Look for confidence patterns
    patterns = [
        r'confidence[:\s]+(\d+)%',
        r'confidence[:\s]+(\d+\.\d+)',
        r'(\d+)%\s*confiden',
    ]

    for pattern in patterns:
        match = re.search(pattern, response_text.lower())
        if match:
            value = float(match.group(1))
            if value > 1:
                value = value / 100
            return min(max(value, 0), 1)

    return 0.5  # Default confidence


# =============================================================================
# CLAUDE CLIENT CLASS
# =============================================================================

class SNAILClaudeClient:
    """
    SNAIL-specific Claude API client.

    Provides:
    - Prompt template management
    - Decision-specific queries
    - Response parsing and structuring
    - Usage tracking

    Attributes:
        api_key: Anthropic API key
        model: Model to use
        client: Anthropic client instance
        usage_log: Log file path for usage tracking
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize Claude client.

        Args:
            config: Configuration dictionary (uses config file if None)
        """
        if config is None:
            config = get_claude_config()

        self.api_key = config.get('api_key') or os.getenv('ANTHROPIC_API_KEY')
        self.model = config.get('model', DEFAULT_MODEL)
        self.max_tokens = config.get('max_tokens', MAX_TOKENS)
        self.temperature = config.get('temperature', DEFAULT_TEMPERATURE)

        if not self.api_key:
            raise ValueError("Claude API key not configured")

        self.client = anthropic.Anthropic(api_key=self.api_key)

        # Usage logging
        self.usage_log = PROJECT_ROOT / 'logs' / 'claude_usage.log'
        self.usage_log.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"Claude client initialized with model: {self.model}")

    # =========================================================================
    # CORE API METHODS
    # =========================================================================

    def _call_api(self, prompt: str, system_prompt: Optional[str] = None) -> Dict[str, Any]:
        """
        Make API call to Claude.

        Args:
            prompt: User prompt
            system_prompt: Optional system prompt

        Returns:
            API response dictionary

        Raises:
            Exception: If API call fails after retries
        """
        last_error = None

        for attempt in range(MAX_RETRIES):
            try:
                messages = [{"role": "user", "content": prompt}]

                kwargs = {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "messages": messages,
                    "temperature": self.temperature,
                }

                if system_prompt:
                    kwargs["system"] = system_prompt

                response = self.client.messages.create(**kwargs)

                # Safely extract content from response
                content = ""
                if response.content and len(response.content) > 0:
                    content = response.content[0].text
                else:
                    logger.warning("Claude returned empty content response")

                return {
                    'content': content,
                    'prompt_tokens': response.usage.input_tokens,
                    'completion_tokens': response.usage.output_tokens,
                    'model': response.model,
                    'stop_reason': response.stop_reason
                }

            except anthropic.RateLimitError as e:
                last_error = e
                logger.warning(f"Rate limit hit, waiting {RETRY_DELAY * (attempt + 1)}s...")
                time.sleep(RETRY_DELAY * (attempt + 1))

            except anthropic.APIError as e:
                last_error = e
                logger.error(f"API error: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)

            except Exception as e:
                last_error = e
                logger.error(f"Unexpected error: {e}")
                break

        raise last_error or Exception("API call failed")

    def _log_usage(self, response: ClaudeResponse) -> None:
        """Log API usage for cost tracking."""
        try:
            log_entry = {
                'timestamp': response.timestamp.isoformat(),
                'decision_type': response.decision_type,
                'prompt_tokens': response.prompt_tokens,
                'completion_tokens': response.completion_tokens,
                'total_tokens': response.total_tokens,
                'decision': response.decision.value
            }

            with open(self.usage_log, 'a') as f:
                f.write(json.dumps(log_entry) + '\n')

        except Exception as e:
            logger.warning(f"Failed to log usage: {e}")

    # =========================================================================
    # DECISION METHODS
    # =========================================================================

    def get_decision(
        self,
        decision_type: DecisionType,
        context: MarketContext,
        additional_prompt: str = ""
    ) -> ClaudeResponse:
        """
        Get a trading decision from Claude.

        Args:
            decision_type: Type of decision needed
            context: Market context
            additional_prompt: Additional context to append

        Returns:
            Structured Claude response
        """
        # Load and format prompt
        template = load_prompt_template(decision_type.value)
        prompt = format_prompt(template, context)

        if additional_prompt:
            prompt += f"\n\nAdditional context:\n{additional_prompt}"

        # System prompt for trading advisor
        system_prompt = (
            "You are a professional options trading advisor for NIFTY Iron Fly strategies. "
            "Provide clear, actionable decisions with concise reasoning. "
            "Always include a clear DECISION: PROCEED/HOLD/EXIT/SKIP at the start of your response. "
            "Consider risk management, market conditions, and the specific strategy parameters."
        )

        logger.info(f"Requesting {decision_type.value} decision from Claude")

        # Make API call
        api_response = self._call_api(prompt, system_prompt)

        # Parse response
        raw_text = api_response['content']
        decision = parse_decision(raw_text)
        confidence = extract_confidence(raw_text)

        response = ClaudeResponse(
            decision=decision,
            reasoning=raw_text,
            confidence=confidence,
            raw_response=raw_text,
            prompt_tokens=api_response['prompt_tokens'],
            completion_tokens=api_response['completion_tokens'],
            decision_type=decision_type.value
        )

        # Log usage
        self._log_usage(response)

        logger.info(f"Claude decision: {decision.value} (confidence: {confidence:.0%})")

        return response

    def get_pre_entry_decision(self, context: MarketContext) -> ClaudeResponse:
        """Get pre-entry decision for Iron Fly."""
        return self.get_decision(DecisionType.PRE_ENTRY, context)

    def get_stop_loss_advisory(self, context: MarketContext) -> ClaudeResponse:
        """Get stop loss advisory when at 50% max loss."""
        return self.get_decision(DecisionType.STOP_LOSS, context)

    def get_wing_approach_advisory(self, context: MarketContext) -> ClaudeResponse:
        """Get advisory when NIFTY approaches wing strike."""
        return self.get_decision(DecisionType.WING_APPROACH, context)

    def get_friday_decision(self, context: MarketContext) -> ClaudeResponse:
        """Get Friday exit decision."""
        return self.get_decision(DecisionType.FRIDAY, context)

    def get_vix_spike_advisory(self, context: MarketContext) -> ClaudeResponse:
        """Get advisory for VIX spike."""
        return self.get_decision(DecisionType.VIX_SPIKE, context)

    def get_market_event_advisory(
        self,
        context: MarketContext,
        event_description: str
    ) -> ClaudeResponse:
        """Get advisory for market event."""
        return self.get_decision(
            DecisionType.MARKET_EVENT,
            context,
            additional_prompt=f"Event: {event_description}"
        )

    def get_gap_advisory(self, context: MarketContext) -> ClaudeResponse:
        """Get advisory for gap beyond wing at open."""
        return self.get_decision(DecisionType.GAP_BEYOND_WING, context)

    def get_adjustment_advisory(
        self,
        context: MarketContext,
        current_position: Dict[str, Any]
    ) -> ClaudeResponse:
        """Get position adjustment advisory."""
        position_str = json.dumps(current_position, indent=2)
        return self.get_decision(
            DecisionType.ADJUSTMENT,
            context,
            additional_prompt=f"Current position:\n{position_str}"
        )

    # =========================================================================
    # UTILITY METHODS
    # =========================================================================

    def test_connection(self) -> bool:
        """Test API connection with minimal call."""
        try:
            response = self._call_api("Say 'connected' in one word.")
            return 'connected' in response['content'].lower()
        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            return False

    def get_usage_stats(self, days: int = 7) -> Dict[str, Any]:
        """
        Get usage statistics from log.

        Args:
            days: Number of days to analyze

        Returns:
            Usage statistics dictionary
        """
        try:
            from datetime import timedelta

            cutoff = datetime.now() - timedelta(days=days)
            total_tokens = 0
            call_count = 0
            decision_counts: Dict[str, int] = {}

            if self.usage_log.exists():
                with open(self.usage_log) as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                            entry_time = datetime.fromisoformat(entry['timestamp'])

                            if entry_time >= cutoff:
                                total_tokens += entry.get('total_tokens', 0)
                                call_count += 1
                                dt = entry.get('decision_type', 'unknown')
                                decision_counts[dt] = decision_counts.get(dt, 0) + 1

                        except (json.JSONDecodeError, KeyError):
                            continue

            return {
                'days': days,
                'total_tokens': total_tokens,
                'call_count': call_count,
                'decision_counts': decision_counts,
                'avg_tokens_per_call': total_tokens / call_count if call_count > 0 else 0
            }

        except Exception as e:
            logger.error(f"Failed to get usage stats: {e}")
            return {}


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_claude_client: Optional[SNAILClaudeClient] = None


def get_claude_client(config: Optional[Dict] = None) -> SNAILClaudeClient:
    """Get or create singleton Claude client."""
    global _claude_client

    if _claude_client is None:
        _claude_client = SNAILClaudeClient(config)

    return _claude_client


def reset_claude_client() -> None:
    """Reset singleton Claude client."""
    global _claude_client
    _claude_client = None


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    import sys
    from dotenv import load_dotenv

    load_dotenv()

    logger.remove()
    logger.add(sys.stderr, level="DEBUG")

    print("\n" + "=" * 60)
    print("SNAIL Claude Client Test")
    print("=" * 60)

    try:
        client = SNAILClaudeClient()

        # Test connection
        print("\n[1] Testing connection...")
        if client.test_connection():
            print("    ✓ Connected to Claude API")
        else:
            print("    ✗ Connection failed")
            sys.exit(1)

        # Test pre-entry decision
        print("\n[2] Testing pre-entry decision...")
        context = MarketContext(
            nifty_spot=24150.00,
            india_vix=12.5,
            atm_strike=24150,
            straddle_premium=320.0,
            wing_distance=300,
            dte=6
        )

        response = client.get_pre_entry_decision(context)
        print(f"    Decision: {response.decision.value}")
        print(f"    Confidence: {response.confidence:.0%}")
        print(f"    Tokens used: {response.total_tokens}")
        print(f"    Reasoning preview: {response.reasoning[:200]}...")

        # Test parsing
        print("\n[3] Decision parsing tests:")
        test_responses = [
            "DECISION: PROCEED - Conditions favorable",
            "I recommend we HOLD for now",
            "Given the risk, we should EXIT immediately",
            "Market conditions suggest we SKIP today",
        ]
        for test in test_responses:
            parsed = parse_decision(test)
            print(f"    '{test[:40]}...' -> {parsed.value}")

        # Get usage stats
        print("\n[4] Usage statistics:")
        stats = client.get_usage_stats(30)
        print(f"    Calls (30 days): {stats.get('call_count', 0)}")
        print(f"    Total tokens: {stats.get('total_tokens', 0)}")

        print("\n" + "=" * 60)
        print("Claude client test complete!")
        print("=" * 60 + "\n")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
