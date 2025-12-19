"""
SNAIL Telegram Response Handler

Handles incoming Telegram messages and user responses to trading prompts.

@file        response_handler.py
@description Telegram bot response processing and command handling
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 6
"""

import re
import time
import threading
import os
from datetime import datetime, timedelta
from pathlib import Path as _Path
from typing import Optional, Dict, Any, Callable, List
from dataclasses import dataclass, field
from enum import Enum
from loguru import logger

# Platform-specific imports for file locking
if os.name != 'nt':  # Unix/Linux
    import fcntl
else:
    fcntl = None  # type: ignore[assignment]

from src.api.telegram_alerts import TelegramAlerts, get_telegram
from src.utils.config import get_telegram_config


# =============================================================================
# CONSTANTS
# =============================================================================

# Response timeout settings
DEFAULT_RESPONSE_TIMEOUT = 300  # 5 minutes
POLL_INTERVAL = 2  # seconds

# Message update tracking - use absolute paths based on project root
_PROJECT_ROOT = _Path(__file__).parent.parent.parent
UPDATE_OFFSET_FILE = str(_PROJECT_ROOT / "data" / "telegram_update_offset.txt")
RESPONSE_QUEUE_FILE = str(_PROJECT_ROOT / "data" / "telegram_responses.json")  # Shared response queue for ENTER/SKIP
CALLBACK_QUEUE_FILE = str(_PROJECT_ROOT / "data" / "telegram_callbacks.json")  # Shared queue for button callbacks (HOLD/EXIT/ADJUST)

# Lock files for inter-process synchronization
RESPONSE_LOCK_FILE = str(_PROJECT_ROOT / "data" / ".telegram_responses.lock")
CALLBACK_LOCK_FILE = str(_PROJECT_ROOT / "data" / ".telegram_callbacks.lock")


# =============================================================================
# FILE LOCKING UTILITIES
# =============================================================================

class FileLock:
    """
    Cross-platform file lock for inter-process synchronization.

    Uses fcntl on Unix/Linux and msvcrt on Windows.
    """

    def __init__(self, lock_file: str):
        self.lock_file = lock_file
        self._fd: Optional[int] = None

    def __enter__(self) -> 'FileLock':
        # Ensure parent directory exists
        os.makedirs(os.path.dirname(self.lock_file), exist_ok=True)

        # Open/create lock file
        self._fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR)

        # Platform-specific locking
        if os.name == 'nt':  # Windows
            import msvcrt
            msvcrt.locking(self._fd, msvcrt.LK_LOCK, 1)
        else:  # Unix/Linux
            fcntl.flock(self._fd, fcntl.LOCK_EX)

        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._fd is not None:
            if os.name == 'nt':  # Windows
                import msvcrt
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:  # Unix/Linux
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


# =============================================================================
# ENUMS
# =============================================================================

class ResponseType(Enum):
    """Types of expected responses."""
    YES_NO = "yes_no"
    NUMERIC = "numeric"
    CHOICE = "choice"
    TEXT = "text"
    CONFIRMATION = "confirmation"


class ResponseStatus(Enum):
    """Status of response collection."""
    PENDING = "pending"
    RECEIVED = "received"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class PendingResponse:
    """
    Tracks a pending user response.

    Attributes:
        prompt_id: Unique identifier for the prompt
        response_type: Expected response type
        valid_choices: Valid choices for choice responses
        timeout_at: When to timeout
        callback: Function to call with response
        message_id: Telegram message ID of the prompt
    """
    prompt_id: str
    response_type: ResponseType
    timeout_at: datetime
    callback: Optional[Callable[[str], None]] = None
    valid_choices: List[str] = field(default_factory=list)
    message_id: Optional[int] = None
    status: ResponseStatus = ResponseStatus.PENDING
    response: Optional[str] = None


@dataclass
class UserResponse:
    """
    Captured user response.

    Attributes:
        prompt_id: Prompt this responds to
        response: User's response text
        timestamp: When response was received
        normalized: Normalized/processed response
    """
    prompt_id: str
    response: str
    timestamp: datetime
    normalized: str


# =============================================================================
# RESPONSE PATTERNS
# =============================================================================

# Yes/No response patterns
YES_PATTERNS = [
    r'^y(es)?$',
    r'^ok(ay)?$',
    r'^sure$',
    r'^go\s*(ahead)?$',
    r'^proceed$',
    r'^confirm(ed)?$',
    r'^approve(d)?$',
    r'^accept$',
    r'^✓$',
    r'^👍$',
]

NO_PATTERNS = [
    r'^n(o)?$',
    r'^nope$',
    r'^cancel$',
    r'^stop$',
    r'^reject$',
    r'^decline$',
    r'^hold$',
    r'^wait$',
    r'^❌$',
    r'^👎$',
]

# Command patterns
COMMAND_PATTERNS = {
    'status': r'^/status$',
    'positions': r'^/pos(itions)?$',
    'pnl': r'^/pnl$',
    'exit': r'^/exit(\s+now)?$',
    'hold': r'^/hold$',
    'config': r'^/config$',
    'help': r'^/help$',
    'stop': r'^/stop$',
    'resume': r'^/resume$',
}


# =============================================================================
# RESPONSE HANDLER CLASS
# =============================================================================

class TelegramResponseHandler:
    """
    Handles Telegram message polling and response processing.

    This class:
    - Polls for new Telegram messages
    - Matches responses to pending prompts
    - Processes commands
    - Handles timeouts

    Attributes:
        telegram: TelegramAlerts instance
        chat_id: Authorized chat ID
        pending_responses: Dict of pending response requests
        command_handlers: Dict of command callbacks
        update_offset: Last processed update ID
        running: Whether polling is active
    """

    def __init__(
        self,
        telegram: Optional[TelegramAlerts] = None,
        chat_id: Optional[str] = None
    ):
        """
        Initialize response handler.

        Args:
            telegram: TelegramAlerts instance (created if None)
            chat_id: Telegram chat ID (from config if None)
        """
        self.telegram = telegram or get_telegram()

        config = get_telegram_config()
        self.chat_id = chat_id or config.get('chat_id', '')
        self.admin_chat_id = config.get('admin_chat_id', self.chat_id)

        self.pending_responses: Dict[str, PendingResponse] = {}
        self.command_handlers: Dict[str, Callable[[str], None]] = {}
        self.update_offset = self._load_update_offset()

        self._running = False
        self._poll_thread: Optional[threading.Thread] = None

        logger.info("Telegram response handler initialized")

    # =========================================================================
    # UPDATE OFFSET PERSISTENCE
    # =========================================================================

    def _load_update_offset(self) -> int:
        """Load last update offset from file."""
        try:
            from pathlib import Path
            offset_file = Path(UPDATE_OFFSET_FILE)

            if offset_file.exists():
                return int(offset_file.read_text().strip())
            return 0

        except Exception as e:
            logger.warning(f"Could not load update offset: {e}")
            return 0

    def _save_update_offset(self, offset: int) -> None:
        """Save update offset to file."""
        try:
            from pathlib import Path
            offset_file = Path(UPDATE_OFFSET_FILE)
            offset_file.parent.mkdir(parents=True, exist_ok=True)
            offset_file.write_text(str(offset))

        except Exception as e:
            logger.warning(f"Could not save update offset: {e}")

    def _read_shared_responses(self) -> List[Dict[str, Any]]:
        """
        Read responses from shared queue file (written by telegram_poller daemon).

        Uses file locking to prevent race conditions with concurrent processes.

        Returns:
            List of response dicts
        """
        try:
            import json
            from pathlib import Path

            queue_file = Path(RESPONSE_QUEUE_FILE)
            logger.debug(f"Checking shared queue file: {queue_file} (exists={queue_file.exists()})")

            if not queue_file.exists():
                return []

            # Use file lock for atomic read-clear operation
            with FileLock(RESPONSE_LOCK_FILE):
                with open(queue_file, 'r') as f:
                    content = f.read().strip()
                    logger.debug(f"Shared queue content: '{content}'")
                    if not content or content == '[]':
                        return []
                    responses = json.loads(content)

                if responses:
                    logger.info(f"Read {len(responses)} response(s) from shared queue: {responses}")
                    # Clear the file after reading (atomic with read due to lock)
                    queue_file.write_text('[]')
                    logger.debug("Cleared shared queue file")

                    # Filter out stale responses (older than 60 seconds)
                    valid_responses = []
                    now = datetime.now()
                    for resp in responses:
                        ts = resp.get('timestamp')
                        if ts:
                            try:
                                resp_time = datetime.fromisoformat(ts)
                                age_seconds = (now - resp_time).total_seconds()
                                if age_seconds <= 60:
                                    valid_responses.append(resp)
                                else:
                                    logger.warning(f"Rejecting stale response (age={age_seconds:.0f}s): {resp}")
                            except (ValueError, TypeError):
                                valid_responses.append(resp)  # Keep if timestamp parse fails
                        else:
                            valid_responses.append(resp)  # Keep if no timestamp

                    return valid_responses

                return responses if isinstance(responses, list) else []

        except Exception as e:
            logger.warning(f"Could not read shared responses from {RESPONSE_QUEUE_FILE}: {e}")
            return []

    @staticmethod
    def clear_shared_queue() -> int:
        """
        Clear all responses from the shared queue.
        Call this BEFORE sending a new prompt to prevent stale responses.

        Uses file locking to prevent race conditions with concurrent processes.

        Returns:
            Number of responses cleared
        """
        try:
            from pathlib import Path
            import json

            queue_file = Path(RESPONSE_QUEUE_FILE)
            if not queue_file.exists():
                return 0

            # Use file lock for atomic read-clear operation
            with FileLock(RESPONSE_LOCK_FILE):
                with open(queue_file, 'r') as f:
                    content = f.read().strip()
                    if not content or content == '[]':
                        return 0
                    responses = json.loads(content)

                count = len(responses) if isinstance(responses, list) else 0
                if count > 0:
                    queue_file.write_text('[]')
                    logger.warning(f"Cleared {count} stale response(s) from shared queue: {responses}")
                return count

        except Exception as e:
            logger.warning(f"Could not clear shared queue: {e}")
            return 0

    @staticmethod
    def write_shared_response(text: str, chat_id: str, timestamp: Optional[str] = None) -> None:
        """
        Write a response to the shared queue file (called by telegram_poller daemon).

        Uses file locking to prevent race conditions with concurrent processes.

        Args:
            text: Response text
            chat_id: Chat ID the response came from
            timestamp: ISO timestamp (auto-generated if None)
        """
        try:
            import json
            from pathlib import Path
            from datetime import datetime

            queue_file = Path(RESPONSE_QUEUE_FILE)
            queue_file.parent.mkdir(parents=True, exist_ok=True)

            # Use file lock for atomic read-modify-write
            with FileLock(RESPONSE_LOCK_FILE):
                # Read existing
                responses: List[Dict[str, Any]] = []
                if queue_file.exists():
                    try:
                        with open(queue_file, 'r') as f:
                            responses = json.load(f)
                    except (json.JSONDecodeError, IOError):
                        responses = []

                # Add new response
                responses.append({
                    'text': text,
                    'chat_id': chat_id,
                    'timestamp': timestamp or datetime.now().isoformat()
                })

                # Write back
                with open(queue_file, 'w') as f:
                    json.dump(responses, f)

            logger.info(f"Wrote response to shared queue {queue_file}: {text}")

        except Exception as e:
            logger.error(f"Could not write shared response: {e}")

    # =========================================================================
    # CALLBACK QUEUE (for button responses: HOLD/EXIT/ADJUST)
    # =========================================================================

    @staticmethod
    def write_callback(action: str, alert_type: str, position_id: Optional[int] = None, chat_id: Optional[str] = None) -> None:
        """
        Write a button callback to the shared callback queue.
        Called by telegram_poller daemon when user clicks HOLD/EXIT/ADJUST buttons.

        Uses file locking to prevent race conditions with concurrent processes.

        Args:
            action: Action type (hold, exit, adjust, yes, no)
            alert_type: Alert type (stop_loss, wing_approach, friday, vix_warning, exit_confirm)
            position_id: Position ID if applicable
            chat_id: Chat ID
        """
        try:
            import json
            from pathlib import Path
            from datetime import datetime

            queue_file = Path(CALLBACK_QUEUE_FILE)
            queue_file.parent.mkdir(parents=True, exist_ok=True)

            # Use file lock for atomic read-modify-write
            with FileLock(CALLBACK_LOCK_FILE):
                # Read existing
                callbacks: List[Dict[str, Any]] = []
                if queue_file.exists():
                    try:
                        with open(queue_file, 'r') as f:
                            callbacks = json.load(f)
                    except (json.JSONDecodeError, IOError):
                        callbacks = []

                # Add new callback
                callbacks.append({
                    'action': action,
                    'alert_type': alert_type,
                    'position_id': position_id,
                    'chat_id': chat_id,
                    'timestamp': datetime.now().isoformat()
                })

                # Write back
                with open(queue_file, 'w') as f:
                    json.dump(callbacks, f)

            logger.info(f"Wrote callback to queue: {action}:{alert_type}:{position_id}")

        except Exception as e:
            logger.error(f"Could not write callback: {e}")

    @staticmethod
    def read_callbacks() -> List[Dict[str, Any]]:
        """
        Read and clear callbacks from the shared callback queue.

        Uses file locking to prevent race conditions with concurrent processes.

        Returns:
            List of callback dicts
        """
        try:
            import json
            from pathlib import Path
            from datetime import datetime

            queue_file = Path(CALLBACK_QUEUE_FILE)
            if not queue_file.exists():
                return []

            # Use file lock for atomic read-clear operation
            with FileLock(CALLBACK_LOCK_FILE):
                with open(queue_file, 'r') as f:
                    content = f.read().strip()
                    if not content or content == '[]':
                        return []
                    callbacks = json.loads(content)

                if callbacks:
                    # Clear the file after reading (atomic with read due to lock)
                    queue_file.write_text('[]')

                    # Filter out stale callbacks (older than 5 minutes)
                    valid_callbacks: List[Dict[str, Any]] = []
                    now = datetime.now()
                    for cb in callbacks:
                        ts = cb.get('timestamp')
                        if ts:
                            try:
                                cb_time = datetime.fromisoformat(ts)
                                age_seconds = (now - cb_time).total_seconds()
                                if age_seconds <= 300:  # 5 minutes
                                    valid_callbacks.append(cb)
                                else:
                                    logger.warning(f"Rejecting stale callback (age={age_seconds:.0f}s): {cb}")
                            except (ValueError, TypeError):
                                valid_callbacks.append(cb)
                        else:
                            valid_callbacks.append(cb)

                    if valid_callbacks:
                        logger.info(f"Read {len(valid_callbacks)} callback(s) from queue")
                    return valid_callbacks

            return []

        except Exception as e:
            logger.warning(f"Could not read callbacks: {e}")
            return []

    # =========================================================================
    # RESPONSE REGISTRATION
    # =========================================================================

    def register_response(
        self,
        prompt_id: str,
        response_type: ResponseType,
        timeout_seconds: int = DEFAULT_RESPONSE_TIMEOUT,
        valid_choices: Optional[List[str]] = None,
        callback: Optional[Callable[[str], None]] = None,
        message_id: Optional[int] = None
    ) -> None:
        """
        Register expectation for a user response.

        Args:
            prompt_id: Unique prompt identifier
            response_type: Type of expected response
            timeout_seconds: Timeout in seconds
            valid_choices: Valid choices for choice type
            callback: Optional callback function
            message_id: Telegram message ID of prompt
        """
        self.pending_responses[prompt_id] = PendingResponse(
            prompt_id=prompt_id,
            response_type=response_type,
            timeout_at=datetime.now() + timedelta(seconds=timeout_seconds),
            valid_choices=valid_choices or [],
            callback=callback,
            message_id=message_id
        )

        logger.debug(f"Registered pending response: {prompt_id} ({response_type.value})")

    def cancel_response(self, prompt_id: str) -> None:
        """Cancel a pending response expectation."""
        if prompt_id in self.pending_responses:
            self.pending_responses[prompt_id].status = ResponseStatus.CANCELLED
            del self.pending_responses[prompt_id]
            logger.debug(f"Cancelled pending response: {prompt_id}")

    # =========================================================================
    # COMMAND HANDLERS
    # =========================================================================

    def register_command(
        self,
        command: str,
        handler: Callable[[str], None]
    ) -> None:
        """
        Register a command handler.

        Args:
            command: Command name (without /)
            handler: Function to call when command received
        """
        self.command_handlers[command] = handler
        logger.debug(f"Registered command handler: /{command}")

    # =========================================================================
    # MESSAGE PROCESSING
    # =========================================================================

    def _normalize_response(self, text: str) -> str:
        """Normalize response text for matching."""
        return text.strip().lower()

    def _is_yes_response(self, text: str) -> bool:
        """Check if text is a yes response."""
        normalized = self._normalize_response(text)
        return any(re.match(pattern, normalized, re.IGNORECASE) for pattern in YES_PATTERNS)

    def _is_no_response(self, text: str) -> bool:
        """Check if text is a no response."""
        normalized = self._normalize_response(text)
        return any(re.match(pattern, normalized, re.IGNORECASE) for pattern in NO_PATTERNS)

    def _extract_command(self, text: str) -> Optional[str]:
        """Extract command from text if present."""
        for command, pattern in COMMAND_PATTERNS.items():
            if re.match(pattern, text.strip(), re.IGNORECASE):
                return command
        return None

    def _process_message(self, message: Dict[str, Any]) -> None:
        """
        Process a single incoming message.

        Args:
            message: Telegram message dict
        """
        # Extract message details
        chat_id = str(message.get('chat', {}).get('id', ''))
        text = message.get('text', '').strip()
        from_user = message.get('from', {}).get('username', 'unknown')

        # Verify chat ID
        if chat_id not in (self.chat_id, self.admin_chat_id):
            logger.warning(f"Message from unauthorized chat: {chat_id}")
            return

        logger.debug(f"Processing message from {from_user}: {text[:50]}...")

        # Check for command
        command = self._extract_command(text)
        if command and command in self.command_handlers:
            logger.info(f"Executing command: /{command}")
            try:
                self.command_handlers[command](text)
            except Exception as e:
                logger.error(f"Command handler error: {e}")
            return

        # Check against pending responses
        matched_prompt = None
        for prompt_id, pending in list(self.pending_responses.items()):
            if pending.status != ResponseStatus.PENDING:
                continue

            # Check timeout
            if datetime.now() > pending.timeout_at:
                pending.status = ResponseStatus.TIMEOUT
                logger.info(f"Response timeout: {prompt_id}")
                continue

            # Try to match response
            normalized = self._normalize_response(text)

            if pending.response_type == ResponseType.YES_NO:
                if self._is_yes_response(text):
                    pending.response = "yes"
                    pending.status = ResponseStatus.RECEIVED
                    matched_prompt = prompt_id
                    break
                elif self._is_no_response(text):
                    pending.response = "no"
                    pending.status = ResponseStatus.RECEIVED
                    matched_prompt = prompt_id
                    break

            elif pending.response_type == ResponseType.CHOICE:
                valid_lower = [c.lower() for c in pending.valid_choices]
                logger.debug(f"Checking CHOICE: normalized='{normalized}' in valid={valid_lower}")
                if normalized in valid_lower:
                    pending.response = normalized
                    pending.status = ResponseStatus.RECEIVED
                    matched_prompt = prompt_id
                    logger.info(f"CHOICE matched! '{normalized}' for {prompt_id}")
                    break

            elif pending.response_type == ResponseType.NUMERIC:
                if normalized.replace('.', '').replace('-', '').isdigit():
                    pending.response = normalized
                    pending.status = ResponseStatus.RECEIVED
                    matched_prompt = prompt_id
                    break

            elif pending.response_type == ResponseType.TEXT:
                pending.response = text
                pending.status = ResponseStatus.RECEIVED
                matched_prompt = prompt_id
                break

            elif pending.response_type == ResponseType.CONFIRMATION:
                if self._is_yes_response(text):
                    pending.response = "confirmed"
                    pending.status = ResponseStatus.RECEIVED
                    matched_prompt = prompt_id
                    break

        # Execute callback if matched
        if matched_prompt:
            pending = self.pending_responses[matched_prompt]
            logger.info(f"Response received for {matched_prompt}: {pending.response}")

            if pending.callback and pending.response is not None:
                try:
                    pending.callback(pending.response)
                except Exception as e:
                    logger.error(f"Response callback error: {e}")

    # =========================================================================
    # POLLING
    # =========================================================================

    def poll_updates(self, short_poll: bool = False) -> List[Dict[str, Any]]:
        """
        Poll for new Telegram updates.

        Args:
            short_poll: Use short polling (0 timeout) to avoid conflict with daemon

        Returns:
            List of new message updates
        """
        try:
            import requests

            config = get_telegram_config()
            bot_token = config.get('bot_token', '')

            url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
            params = {
                'offset': self.update_offset + 1,
                'timeout': 0 if short_poll else 30,  # Short or long polling
                'allowed_updates': ['message']
            }

            response = requests.get(url, params=params, timeout=35)

            # Handle 409 Conflict (another process is polling)
            if response.status_code == 409:
                logger.debug("Telegram polling conflict (409) - another poller is running")
                return []

            response.raise_for_status()

            result = response.json()
            if not result.get('ok'):
                logger.error(f"Telegram API error: {result}")
                return []

            updates = result.get('result', [])

            # Update offset
            if updates:
                self.update_offset = max(u['update_id'] for u in updates)
                self._save_update_offset(self.update_offset)

            # Extract messages
            messages = [u['message'] for u in updates if 'message' in u]
            return messages

        except Exception as e:
            # Don't spam logs with 409 errors
            if '409' not in str(e):
                logger.error(f"Error polling updates: {e}")
            return []

    def _poll_loop(self) -> None:
        """Background polling loop."""
        logger.info("Starting Telegram polling loop")

        while self._running:
            try:
                messages = self.poll_updates()

                for message in messages:
                    self._process_message(message)

                # Clean up timed out responses
                self._cleanup_timeouts()

                time.sleep(POLL_INTERVAL)

            except Exception as e:
                logger.error(f"Polling loop error: {e}")
                time.sleep(POLL_INTERVAL * 2)

        logger.info("Telegram polling loop stopped")

    def _cleanup_timeouts(self) -> None:
        """Clean up timed out pending responses."""
        now = datetime.now()
        for prompt_id, pending in list(self.pending_responses.items()):
            if pending.status == ResponseStatus.PENDING and now > pending.timeout_at:
                pending.status = ResponseStatus.TIMEOUT
                logger.info(f"Response timeout: {prompt_id}")

    def start_polling(self) -> None:
        """Start background polling thread."""
        if self._running:
            return

        self._running = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="TelegramPoller",
            daemon=True
        )
        self._poll_thread.start()
        logger.info("Telegram polling started")

    def stop_polling(self) -> None:
        """Stop background polling thread."""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
        logger.info("Telegram polling stopped")

    # =========================================================================
    # SYNCHRONOUS RESPONSE COLLECTION
    # =========================================================================

    def wait_for_response(
        self,
        prompt_id: str,
        response_type: ResponseType,
        timeout_seconds: int = DEFAULT_RESPONSE_TIMEOUT,
        valid_choices: Optional[List[str]] = None
    ) -> Optional[UserResponse]:
        """
        Wait synchronously for a user response.

        This blocks until response is received or timeout.
        Checks both direct polling and shared queue (for daemon mode).

        Args:
            prompt_id: Unique prompt identifier
            response_type: Type of expected response
            timeout_seconds: Timeout in seconds
            valid_choices: Valid choices for choice type

        Returns:
            UserResponse if received, None if timeout
        """
        self.register_response(
            prompt_id=prompt_id,
            response_type=response_type,
            timeout_seconds=timeout_seconds,
            valid_choices=valid_choices
        )

        deadline = datetime.now() + timedelta(seconds=timeout_seconds)
        poll_conflict_count = 0
        logger.info(f"Waiting for response '{prompt_id}' with timeout {timeout_seconds}s, valid_choices={valid_choices}")

        while datetime.now() < deadline:
            # First check shared response queue (from telegram_poller daemon)
            shared_responses = self._read_shared_responses()
            for resp in shared_responses:
                # Create a fake message dict and process it
                fake_message = {
                    'chat': {'id': resp.get('chat_id', self.chat_id)},
                    'text': resp.get('text', ''),
                    'message_id': 0
                }
                logger.info(f"Processing shared response: text='{resp.get('text')}', chat_id={resp.get('chat_id')}")
                self._process_message(fake_message)

            # Then try direct polling (with short timeout to not conflict)
            messages = self.poll_updates(short_poll=True)

            # If we get 409 conflict, the daemon is running - rely on shared queue
            if not messages and poll_conflict_count < 3:
                poll_conflict_count += 1

            for message in messages:
                self._process_message(message)

            # Check if our response was received
            pending = self.pending_responses.get(prompt_id)
            if pending and pending.status == ResponseStatus.RECEIVED and pending.response is not None:
                return UserResponse(
                    prompt_id=prompt_id,
                    response=pending.response,
                    timestamp=datetime.now(),
                    normalized=self._normalize_response(pending.response)
                )

            time.sleep(POLL_INTERVAL)

        # Timeout
        self.cancel_response(prompt_id)
        return None

    def ask_yes_no(
        self,
        question: str,
        timeout_seconds: int = DEFAULT_RESPONSE_TIMEOUT,
        prompt_id: Optional[str] = None
    ) -> Optional[bool]:
        """
        Ask a yes/no question and wait for response.

        Args:
            question: Question to ask
            timeout_seconds: Timeout in seconds
            prompt_id: Optional prompt ID (auto-generated if None)

        Returns:
            True for yes, False for no, None for timeout
        """
        prompt_id = prompt_id or f"yn_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

        # Send question
        self.telegram.send(question)

        # Wait for response
        response = self.wait_for_response(
            prompt_id=prompt_id,
            response_type=ResponseType.YES_NO,
            timeout_seconds=timeout_seconds
        )

        if response:
            return response.normalized == 'yes'
        return None

    def ask_confirmation(
        self,
        message: str,
        timeout_seconds: int = DEFAULT_RESPONSE_TIMEOUT
    ) -> bool:
        """
        Ask for confirmation before proceeding.

        Args:
            message: Confirmation message
            timeout_seconds: Timeout in seconds

        Returns:
            True if confirmed, False otherwise (including timeout)
        """
        prompt_id = f"confirm_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

        # Send confirmation request
        full_message = f"{message}\n\nReply YES to confirm, NO to cancel."
        self.telegram.send(full_message)

        # Wait for response
        response = self.wait_for_response(
            prompt_id=prompt_id,
            response_type=ResponseType.CONFIRMATION,
            timeout_seconds=timeout_seconds
        )

        if response and response.normalized == 'confirmed':
            return True

        return False


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_response_handler: Optional[TelegramResponseHandler] = None


def get_response_handler() -> TelegramResponseHandler:
    """Get or create singleton response handler."""
    global _response_handler

    if _response_handler is None:
        _response_handler = TelegramResponseHandler()

    return _response_handler


def reset_response_handler() -> None:
    """Reset singleton response handler."""
    global _response_handler
    if _response_handler:
        _response_handler.stop_polling()
    _response_handler = None


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
    print("SNAIL Telegram Response Handler Test")
    print("=" * 60)

    try:
        handler = TelegramResponseHandler()

        # Register test command
        def handle_status(text):
            print(f"Status command received: {text}")
            handler.telegram.send("✅ System is running!")

        handler.register_command('status', handle_status)

        # Test yes/no pattern matching
        print("\n[1] Response pattern tests:")
        test_responses = ['yes', 'Yes', 'Y', 'ok', 'proceed', 'no', 'No', 'N', 'cancel', 'stop']
        for resp in test_responses:
            is_yes = handler._is_yes_response(resp)
            is_no = handler._is_no_response(resp)
            print(f"    '{resp}': yes={is_yes}, no={is_no}")

        # Test command extraction
        print("\n[2] Command extraction tests:")
        test_commands = ['/status', '/positions', '/pos', '/pnl', '/exit', '/help', 'hello']
        for cmd in test_commands:
            extracted = handler._extract_command(cmd)
            print(f"    '{cmd}': {extracted}")

        print("\n[3] Interactive test (send messages to test bot):")
        print("    Starting polling... (Ctrl+C to stop)")

        handler.start_polling()

        # Simple interaction loop
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n    Stopping...")

        handler.stop_polling()

        print("\n" + "=" * 60)
        print("Response handler test complete!")
        print("=" * 60 + "\n")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
