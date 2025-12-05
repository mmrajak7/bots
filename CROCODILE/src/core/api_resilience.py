"""API Resilience - Retry Logic with Exponential Backoff and Circuit Breaker"""

import time
import functools
import os
import json
from datetime import datetime, timedelta
from typing import Callable, Any, Optional, Tuple, Dict, List
from loguru import logger
from pathlib import Path


class RetryConfig:
    """Retry configuration"""
    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 16.0,
        exponential_base: float = 2.0
    ):
        """
        Initialize retry configuration

        Args:
            max_attempts: Maximum number of attempts (default: 3)
            base_delay: Initial delay in seconds (default: 1.0)
            max_delay: Maximum delay in seconds (default: 16.0)
            exponential_base: Exponential backoff multiplier (default: 2.0)

        Delay progression with defaults:
        - Attempt 1: 0s (immediate)
        - Attempt 2: 1s delay (base_delay * 2^0)
        - Attempt 3: 2s delay (base_delay * 2^1)
        """
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base

    def calculate_delay(self, attempt: int) -> float:
        """
        Calculate delay for given attempt number using exponential backoff

        Args:
            attempt: Current attempt number (1-indexed)

        Returns:
            Delay in seconds
        """
        if attempt <= 1:
            return 0  # No delay for first attempt

        # Exponential backoff: base_delay * (exponential_base ^ (attempt - 2))
        # attempt=2 → 1s, attempt=3 → 2s, attempt=4 → 4s, etc.
        delay = self.base_delay * (self.exponential_base ** (attempt - 2))

        # Cap at max_delay
        return min(delay, self.max_delay)


class CircuitBreaker:
    """
    Circuit Breaker Pattern Implementation

    Prevents cascading failures by tracking API operation failures and
    automatically halting operations when failure threshold is exceeded.

    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Circuit breaker tripped, requests blocked
    - HALF_OPEN: Recovery mode, allowing limited requests to test if issue resolved

    Persistence: State persists to disk to survive bot restarts
    """

    _instance = None  # Singleton instance
    _state_file = "data/circuit_breaker_state.json"

    def __new__(cls):
        """Singleton pattern - only one circuit breaker instance"""
        if cls._instance is None:
            cls._instance = super(CircuitBreaker, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Initialize circuit breaker (only once due to singleton)"""
        if self._initialized:
            return

        # Load config
        from src.utils.config_manager import config
        self.config = config

        # Circuit breaker settings
        cb_config = self.config.get('api_resilience.circuit_breaker', {})
        self.enabled = cb_config.get('enabled', True)

        # Failure thresholds
        consecutive_failures = cb_config.get('consecutive_failures', {})
        self.critical_failure_threshold = consecutive_failures.get('critical_operations', 3)
        self.standard_failure_threshold = consecutive_failures.get('standard_operations', 5)

        # Time window for failure tracking
        self.failure_window = cb_config.get('failure_window', 300)  # seconds

        # Auto-reset settings
        auto_reset = cb_config.get('auto_reset', {})
        self.auto_reset_enabled = auto_reset.get('enabled', True)
        self.cooldown_minutes = auto_reset.get('cooldown_minutes', 30)

        # Manual reset file
        self.manual_reset_file = cb_config.get('manual_reset_file', 'data/circuit_breaker_reset.flag')

        # Actions when tripped
        actions = cb_config.get('actions', {})
        self.halt_trading = actions.get('halt_trading', True)
        self.halt_order_placement = actions.get('halt_order_placement', True)
        self.allow_exits = actions.get('allow_exits', True)
        self.allow_gtt_updates = actions.get('allow_gtt_updates', True)
        self.send_critical_alert = actions.get('send_critical_alert', True)

        # Tracked operations
        self.tracked_operations = cb_config.get('tracked_operations', [
            "Place Order",
            "Place GTT Order",
            "Cancel GTT Order",
            "Get Margins",
            "Get Historical Data",
            "Get Instrument LTP"
        ])

        # State tracking
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self.failure_counts: Dict[str, int] = {}  # operation_name -> consecutive failure count
        self.failure_timestamps: Dict[str, List[datetime]] = {}  # operation_name -> list of failure times
        self.tripped_at: Optional[datetime] = None
        self.trip_reason: Optional[str] = None

        # Load persisted state
        self._load_state()

        self._initialized = True

        logger.info(
            f"Circuit Breaker initialized: Enabled={self.enabled}, "
            f"CriticalThreshold={self.critical_failure_threshold}, "
            f"StandardThreshold={self.standard_failure_threshold}, "
            f"State={self.state}"
        )

    def _load_state(self):
        """Load circuit breaker state from disk"""
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, 'r') as f:
                    data = json.load(f)

                self.state = data.get('state', 'CLOSED')
                self.failure_counts = data.get('failure_counts', {})

                # Convert timestamp strings back to datetime objects
                self.failure_timestamps = {}
                for op, timestamps in data.get('failure_timestamps', {}).items():
                    self.failure_timestamps[op] = [
                        datetime.fromisoformat(ts) for ts in timestamps
                    ]

                tripped_at_str = data.get('tripped_at')
                self.tripped_at = datetime.fromisoformat(tripped_at_str) if tripped_at_str else None
                self.trip_reason = data.get('trip_reason')

                logger.info(f"Circuit breaker state loaded from disk: State={self.state}")

        except Exception as e:
            logger.warning(f"Failed to load circuit breaker state: {e}. Starting fresh.")

    def _save_state(self):
        """Persist circuit breaker state to disk"""
        try:
            # Convert datetime objects to ISO strings for JSON serialization
            failure_timestamps_serializable = {}
            for op, timestamps in self.failure_timestamps.items():
                failure_timestamps_serializable[op] = [
                    ts.isoformat() for ts in timestamps
                ]

            data = {
                'state': self.state,
                'failure_counts': self.failure_counts,
                'failure_timestamps': failure_timestamps_serializable,
                'tripped_at': self.tripped_at.isoformat() if self.tripped_at else None,
                'trip_reason': self.trip_reason,
                'updated_at': datetime.now().isoformat()
            }

            # Ensure directory exists
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)

            with open(self._state_file, 'w') as f:
                json.dump(data, f, indent=2)

        except Exception as e:
            logger.error(f"Failed to save circuit breaker state: {e}")

    def _clean_old_failures(self, operation_name: str):
        """Remove failures older than the time window"""
        if operation_name not in self.failure_timestamps:
            return

        cutoff_time = datetime.now() - timedelta(seconds=self.failure_window)

        # Keep only recent failures
        self.failure_timestamps[operation_name] = [
            ts for ts in self.failure_timestamps[operation_name]
            if ts > cutoff_time
        ]

        # Update failure count
        self.failure_counts[operation_name] = len(self.failure_timestamps[operation_name])

    def _check_manual_reset(self) -> bool:
        """Check if manual reset file exists"""
        if os.path.exists(self.manual_reset_file):
            logger.info(f"Manual reset file detected: {self.manual_reset_file}")
            try:
                os.remove(self.manual_reset_file)
                logger.info("Manual reset file removed")
                return True
            except Exception as e:
                logger.error(f"Failed to remove manual reset file: {e}")
                return False
        return False

    def _check_auto_reset(self) -> bool:
        """Check if circuit breaker should auto-reset after cooldown"""
        if not self.auto_reset_enabled or self.tripped_at is None:
            return False

        cooldown_elapsed = datetime.now() - self.tripped_at
        cooldown_threshold = timedelta(minutes=self.cooldown_minutes)

        if cooldown_elapsed > cooldown_threshold:
            logger.info(
                f"Auto-reset cooldown elapsed: {cooldown_elapsed.total_seconds()/60:.1f} minutes"
            )
            return True

        return False

    def record_success(self, operation_name: str):
        """
        Record successful operation execution

        Resets consecutive failure count for this operation
        """
        if not self.enabled:
            return

        if operation_name in self.tracked_operations:
            # Reset failure tracking for this operation
            if operation_name in self.failure_counts:
                logger.debug(
                    f"Circuit Breaker: {operation_name} succeeded - "
                    f"resetting failure count (was {self.failure_counts[operation_name]})"
                )
                self.failure_counts[operation_name] = 0
                self.failure_timestamps[operation_name] = []

    def record_failure(self, operation_name: str, error: Exception):
        """
        Record operation failure and check if circuit breaker should trip

        Args:
            operation_name: Name of the failed operation
            error: Exception that caused the failure
        """
        if not self.enabled:
            return

        # Only track configured operations
        if operation_name not in self.tracked_operations:
            return

        # Initialize tracking if needed
        if operation_name not in self.failure_timestamps:
            self.failure_timestamps[operation_name] = []

        # Record failure
        self.failure_timestamps[operation_name].append(datetime.now())

        # Clean old failures outside time window
        self._clean_old_failures(operation_name)

        # Update failure count
        consecutive_failures = self.failure_counts.get(operation_name, 0) + 1
        self.failure_counts[operation_name] = consecutive_failures

        # Determine threshold for this operation
        is_critical_op = operation_name in [
            "Place Order", "Place GTT Order", "Cancel GTT Order"
        ]
        threshold = (
            self.critical_failure_threshold if is_critical_op
            else self.standard_failure_threshold
        )

        logger.warning(
            f"Circuit Breaker: {operation_name} failed "
            f"({consecutive_failures}/{threshold} consecutive failures)"
        )

        # Check if threshold exceeded
        if consecutive_failures >= threshold:
            self._trip_circuit(operation_name, consecutive_failures, error)

        # Save state
        self._save_state()

    def _trip_circuit(self, operation_name: str, failure_count: int, error: Exception):
        """Trip the circuit breaker"""
        if self.state == "OPEN":
            logger.warning("Circuit breaker already OPEN, skipping re-trip")
            return

        self.state = "OPEN"
        self.tripped_at = datetime.now()
        self.trip_reason = (
            f"{operation_name} failed {failure_count} consecutive times. "
            f"Last error: {str(error)}"
        )

        logger.critical(
            f"🚨 CIRCUIT BREAKER TRIPPED 🚨\n"
            f"Reason: {self.trip_reason}\n"
            f"Trading operations HALTED"
        )

        # Save state
        self._save_state()

        # Send critical alert
        if self.send_critical_alert:
            self._send_trip_alert()

    def _send_trip_alert(self):
        """Send Telegram alert that circuit breaker tripped"""
        try:
            from src.reporting.telegram_client import telegram

            alert_msg = (
                f"🚨 **CIRCUIT BREAKER TRIPPED** 🚨\n\n"
                f"**Reason:**\n{self.trip_reason}\n\n"
                f"**Actions Taken:**\n"
            )

            if self.halt_trading:
                alert_msg += "• ❌ Signal processing HALTED\n"
            if self.halt_order_placement:
                alert_msg += "• ❌ Order placement HALTED\n"
            if self.allow_exits:
                alert_msg += "• ✅ Position exits still allowed (safety)\n"
            if self.allow_gtt_updates:
                alert_msg += "• ✅ GTT updates still allowed (safety)\n"

            alert_msg += f"\n**Auto-Reset:**\n"
            if self.auto_reset_enabled:
                alert_msg += f"Enabled - will reset after {self.cooldown_minutes} minutes\n"
            else:
                alert_msg += "Disabled - manual intervention required\n"

            alert_msg += f"\n**Manual Reset:**\n"
            alert_msg += f"Create file: `{self.manual_reset_file}` to reset immediately\n"

            alert_msg += f"\n⚠️ **Bot will NOT trade until circuit breaker is reset**"

            telegram.send_alert(alert_msg, critical=True)

        except Exception as e:
            logger.error(f"Failed to send circuit breaker trip alert: {e}")

    def reset(self, reason: str = "Manual reset"):
        """Reset circuit breaker to CLOSED state"""
        logger.info(f"Circuit breaker reset: {reason}")

        self.state = "CLOSED"
        self.failure_counts = {}
        self.failure_timestamps = {}
        self.tripped_at = None
        self.trip_reason = None

        self._save_state()

        # Send alert
        try:
            from src.reporting.telegram_client import telegram
            telegram.send_alert(
                f"✅ **Circuit Breaker Reset**\n\n"
                f"Reason: {reason}\n"
                f"Trading operations resumed",
                critical=False
            )
        except Exception as e:
            logger.error(f"Failed to send reset alert: {e}")

    def check_if_allowed(self, operation_name: str) -> Tuple[bool, Optional[str]]:
        """
        Check if an operation is allowed to execute

        Args:
            operation_name: Name of the operation to check

        Returns:
            (is_allowed, block_reason)
        """
        if not self.enabled:
            return True, None

        # Check for manual reset
        if self._check_manual_reset():
            self.reset("Manual reset file detected")
            return True, None

        # Check for auto-reset
        if self.state == "OPEN" and self._check_auto_reset():
            self.reset(f"Auto-reset after {self.cooldown_minutes} minute cooldown")
            return True, None

        # If circuit breaker is CLOSED, allow all operations
        if self.state == "CLOSED":
            return True, None

        # Circuit breaker is OPEN - check if this operation is still allowed
        if self.state == "OPEN":
            # Allow certain operations even when circuit breaker is open (safety)
            if operation_name in ["Exit Position", "Update GTT"] and self.allow_exits:
                logger.info(
                    f"Circuit breaker OPEN but allowing {operation_name} (safety operation)"
                )
                return True, None

            # Block trading operations
            if "Signal" in operation_name or "Order" in operation_name:
                block_reason = (
                    f"Circuit breaker OPEN: {self.trip_reason}. "
                    f"Tripped at: {self.tripped_at.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                return False, block_reason

        return True, None

    def get_status(self) -> Dict[str, Any]:
        """Get current circuit breaker status"""
        return {
            'enabled': self.enabled,
            'state': self.state,
            'tripped_at': self.tripped_at.isoformat() if self.tripped_at else None,
            'trip_reason': self.trip_reason,
            'failure_counts': self.failure_counts,
            'auto_reset_enabled': self.auto_reset_enabled,
            'cooldown_minutes': self.cooldown_minutes
        }


# Global circuit breaker instance
circuit_breaker = CircuitBreaker()


def with_retry(
    operation_name: str,
    config: Optional[RetryConfig] = None,
    alert_on_failure: bool = True,
    critical: bool = False
) -> Callable:
    """
    Decorator to add retry logic with exponential backoff to any function

    Args:
        operation_name: Human-readable operation name for logging
        config: RetryConfig instance (uses default if None)
        alert_on_failure: Send Telegram alert after all retries exhausted
        critical: Mark alert as critical (higher priority)

    Usage:
        @with_retry("Place GTT Order", critical=True)
        def place_gtt_order(self, payload):
            return self.kite.place_gtt(payload)

    Returns:
        Decorated function with retry logic
    """
    if config is None:
        config = RetryConfig()  # Use defaults: 3 attempts, 1s base delay

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            # CIRCUIT BREAKER CHECK: Is this operation allowed?
            is_allowed, block_reason = circuit_breaker.check_if_allowed(operation_name)
            if not is_allowed:
                error_msg = f"Operation blocked by circuit breaker: {block_reason}"
                logger.error(f"{operation_name} - {error_msg}")
                raise RuntimeError(error_msg)

            last_exception = None

            for attempt in range(1, config.max_attempts + 1):
                try:
                    # Calculate delay for this attempt
                    if attempt > 1:
                        delay = config.calculate_delay(attempt)
                        logger.warning(
                            f"{operation_name} - Retry attempt {attempt}/{config.max_attempts} "
                            f"after {delay:.1f}s delay"
                        )
                        time.sleep(delay)
                    else:
                        logger.debug(f"{operation_name} - Attempt {attempt}/{config.max_attempts}")

                    # Execute the function
                    result = func(*args, **kwargs)

                    # Success!
                    if attempt > 1:
                        logger.info(
                            f"{operation_name} - ✅ Succeeded on attempt {attempt}/{config.max_attempts}"
                        )

                    # CIRCUIT BREAKER: Record success
                    circuit_breaker.record_success(operation_name)

                    return result

                except Exception as e:
                    last_exception = e

                    logger.warning(
                        f"{operation_name} - ❌ Attempt {attempt}/{config.max_attempts} failed: {str(e)}"
                    )

                    # If this was the last attempt, we'll raise after the loop
                    if attempt == config.max_attempts:
                        break

            # All retries exhausted - send alert and raise exception
            # Try to extract response text for better debugging
            response_text = None
            try:
                if hasattr(last_exception, 'response') and last_exception.response is not None:
                    response_text = last_exception.response.text[:300]  # First 300 chars
            except:
                pass

            error_msg = (
                f"⚠️ **API Failure: {operation_name}**\n"
                f"Failed after {attempt} attempt(s)\n"
                f"Error: {str(last_exception)}"
            )
            if response_text:
                error_msg += f"\nResponse: {response_text}"

            logger.error(error_msg)

            # CIRCUIT BREAKER: Record failure after all retries exhausted
            circuit_breaker.record_failure(operation_name, last_exception)

            # Check if this is a recoverable tick size error (caller will handle retry)
            is_tick_size_error = "tick size" in str(last_exception).lower()

            # Send Telegram alert if enabled (skip for tick size errors - they're auto-corrected)
            if alert_on_failure and not is_tick_size_error:
                try:
                    # Import here to avoid circular dependency
                    from src.reporting.telegram_client import telegram
                    telegram.send_alert(error_msg, critical=critical)
                except Exception as alert_error:
                    logger.error(f"Failed to send failure alert: {alert_error}")
            elif is_tick_size_error:
                logger.info(f"Tick size error detected - skipping alert (caller will auto-correct)")

            # Re-raise the original exception
            raise last_exception

        return wrapper
    return decorator


def with_retry_return_none(
    operation_name: str,
    config: Optional[RetryConfig] = None,
    alert_on_failure: bool = True,
    critical: bool = False
) -> Callable:
    """
    Decorator to add retry logic but return None on failure instead of raising

    Useful for non-critical operations where graceful degradation is acceptable

    Args:
        operation_name: Human-readable operation name for logging
        config: RetryConfig instance (uses default if None)
        alert_on_failure: Send Telegram alert after all retries exhausted
        critical: Mark alert as critical (higher priority)

    Usage:
        @with_retry_return_none("Fetch Historical Data")
        def get_historical_data(self, instrument_token):
            return self.kite.historical_data(instrument_token)

    Returns:
        Decorated function that returns None on failure instead of raising
    """
    if config is None:
        config = RetryConfig()

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Optional[Any]:
            try:
                # Use the standard retry decorator
                retry_func = with_retry(
                    operation_name=operation_name,
                    config=config,
                    alert_on_failure=alert_on_failure,
                    critical=critical
                )(func)

                return retry_func(*args, **kwargs)

            except Exception as e:
                logger.error(
                    f"{operation_name} - Returning None after all retries failed: {e}"
                )
                return None

        return wrapper
    return decorator


# Predefined configs for different operation types
class RetryConfigs:
    """Predefined retry configurations for different operation types"""

    # Critical operations (GTT updates, position closures)
    CRITICAL = RetryConfig(
        max_attempts=3,
        base_delay=1.0,
        max_delay=16.0,
        exponential_base=2.0
    )

    # Standard operations (order placement, data fetching)
    STANDARD = RetryConfig(
        max_attempts=3,
        base_delay=1.0,
        max_delay=16.0,
        exponential_base=2.0
    )

    # Non-critical operations (informational queries)
    NON_CRITICAL = RetryConfig(
        max_attempts=2,
        base_delay=0.5,
        max_delay=8.0,
        exponential_base=2.0
    )


# Convenience decorators with predefined configs
def critical_api_call(operation_name: str) -> Callable:
    """Decorator for critical API calls (GTT, position management)"""
    return with_retry(operation_name, config=RetryConfigs.CRITICAL, critical=True)


def standard_api_call(operation_name: str) -> Callable:
    """Decorator for standard API calls (orders, data fetching)"""
    return with_retry(operation_name, config=RetryConfigs.STANDARD, critical=False)


def non_critical_api_call(operation_name: str) -> Callable:
    """Decorator for non-critical API calls (informational queries)"""
    return with_retry_return_none(operation_name, config=RetryConfigs.NON_CRITICAL, critical=False)
