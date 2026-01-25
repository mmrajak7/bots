"""
Circuit Breaker - API resilience pattern for FIFTY bot

Prevents cascading failures by temporarily blocking calls to
failing services after consecutive failures.

States:
- CLOSED: Normal operation, calls pass through
- OPEN: Circuit tripped, calls fail immediately
- HALF_OPEN: Testing if service recovered

Usage:
    breaker = CircuitBreaker('kite_api')

    @breaker.protect
    def call_kite_api():
        return kite.get_positions()

    # Or manual usage:
    if breaker.can_execute():
        try:
            result = kite.get_positions()
            breaker.record_success()
        except Exception as e:
            breaker.record_failure()
            raise
"""

from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Optional, Callable, TypeVar, Any
from functools import wraps
from threading import Lock
from loguru import logger

from src.utils.config_manager import config


class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Blocking calls
    HALF_OPEN = "half_open"  # Testing recovery


class CircuitBreaker:
    """Simple circuit breaker implementation"""

    # Class-level registry of all breakers
    _breakers: Dict[str, 'CircuitBreaker'] = {}
    _lock = Lock()

    def __init__(self, name: str, failure_threshold: int = None, reset_timeout: int = None):
        """
        Initialize circuit breaker.

        Args:
            name: Unique name for this breaker
            failure_threshold: Consecutive failures before opening (default from config)
            reset_timeout: Seconds before attempting reset (default from config)
        """
        self.name = name
        self.failure_threshold = failure_threshold or config.get(
            'api_resilience.circuit_breaker.consecutive_failures.standard_operations', 5
        )
        self.reset_timeout = reset_timeout or config.get(
            'api_resilience.circuit_breaker.auto_reset.cooldown_minutes', 30
        ) * 60  # Convert to seconds

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: Optional[datetime] = None
        self.last_success_time: Optional[datetime] = None
        self._state_lock = Lock()

        # Register this breaker
        with CircuitBreaker._lock:
            CircuitBreaker._breakers[name] = self

        logger.debug(f"CircuitBreaker '{name}' initialized (threshold={self.failure_threshold})")

    def can_execute(self) -> bool:
        """Check if a call can be made"""
        with self._state_lock:
            if self.state == CircuitState.CLOSED:
                return True

            if self.state == CircuitState.OPEN:
                # Check if we should transition to half-open
                if self._should_attempt_reset():
                    self.state = CircuitState.HALF_OPEN
                    logger.info(f"CircuitBreaker '{self.name}': OPEN -> HALF_OPEN (testing)")
                    return True
                return False

            if self.state == CircuitState.HALF_OPEN:
                # Allow one test call
                return True

            return False

    def record_success(self) -> None:
        """Record a successful call"""
        with self._state_lock:
            self.failure_count = 0
            self.last_success_time = datetime.now()

            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.CLOSED
                logger.info(f"CircuitBreaker '{self.name}': HALF_OPEN -> CLOSED (recovered)")

    def record_failure(self) -> None:
        """Record a failed call"""
        with self._state_lock:
            self.failure_count += 1
            self.last_failure_time = datetime.now()

            if self.state == CircuitState.HALF_OPEN:
                # Failed during recovery test - reopen
                self.state = CircuitState.OPEN
                logger.warning(f"CircuitBreaker '{self.name}': HALF_OPEN -> OPEN (recovery failed)")

            elif self.state == CircuitState.CLOSED:
                if self.failure_count >= self.failure_threshold:
                    self.state = CircuitState.OPEN
                    logger.warning(
                        f"CircuitBreaker '{self.name}': CLOSED -> OPEN "
                        f"(threshold {self.failure_threshold} reached)"
                    )

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset"""
        if self.last_failure_time is None:
            return True
        elapsed = (datetime.now() - self.last_failure_time).total_seconds()
        return elapsed >= self.reset_timeout

    def force_reset(self) -> None:
        """Manually reset the circuit breaker"""
        with self._state_lock:
            old_state = self.state
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            logger.info(f"CircuitBreaker '{self.name}': {old_state.value} -> CLOSED (forced)")

    def get_status(self) -> Dict[str, Any]:
        """Get current status"""
        with self._state_lock:
            return {
                'name': self.name,
                'state': self.state.value,
                'failure_count': self.failure_count,
                'failure_threshold': self.failure_threshold,
                'last_failure': self.last_failure_time.isoformat() if self.last_failure_time else None,
                'last_success': self.last_success_time.isoformat() if self.last_success_time else None,
            }

    def protect(self, func: Callable) -> Callable:
        """Decorator to protect a function with this circuit breaker"""
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not self.can_execute():
                raise CircuitBreakerOpenError(
                    f"Circuit breaker '{self.name}' is OPEN - call blocked"
                )

            try:
                result = func(*args, **kwargs)
                self.record_success()
                return result
            except Exception as e:
                self.record_failure()
                raise

        return wrapper

    @classmethod
    def get_breaker(cls, name: str) -> Optional['CircuitBreaker']:
        """Get a breaker by name"""
        with cls._lock:
            return cls._breakers.get(name)

    @classmethod
    def get_all_status(cls) -> Dict[str, Dict[str, Any]]:
        """Get status of all breakers"""
        with cls._lock:
            return {name: breaker.get_status() for name, breaker in cls._breakers.items()}


class CircuitBreakerOpenError(Exception):
    """Raised when trying to execute through an open circuit breaker"""
    pass


# Pre-configured breakers for common use cases
def get_kite_breaker() -> CircuitBreaker:
    """Get or create the Kite API circuit breaker"""
    breaker = CircuitBreaker.get_breaker('kite_api')
    if breaker is None:
        breaker = CircuitBreaker(
            'kite_api',
            failure_threshold=config.get(
                'api_resilience.circuit_breaker.consecutive_failures.critical_operations', 3
            ),
            reset_timeout=config.get(
                'api_resilience.circuit_breaker.auto_reset.cooldown_minutes', 30
            ) * 60
        )
    return breaker


def get_telegram_breaker() -> CircuitBreaker:
    """Get or create the Telegram API circuit breaker"""
    breaker = CircuitBreaker.get_breaker('telegram_api')
    if breaker is None:
        breaker = CircuitBreaker(
            'telegram_api',
            failure_threshold=5,
            reset_timeout=300  # 5 minutes
        )
    return breaker
