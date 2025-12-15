"""Token bucket rate limiter for Gmail API.

Gmail API quota: 250 units per second per user.
- labels.list: 1 unit
- labels.create: 5 units
- messages.batchModify: 5 units
- messages.modify: 5 units

This rate limiter uses a token bucket algorithm with:
- Bucket capacity = 250 tokens (one second of quota)
- Refill rate = 250 tokens/second
- Exponential backoff on 429 errors
"""

import logging
import time
from threading import Lock

logger = logging.getLogger(__name__)

# Gmail API quota units per operation
QUOTA_LABELS_LIST = 1
QUOTA_LABELS_CREATE = 5
QUOTA_MESSAGES_BATCH_MODIFY = 5
QUOTA_MESSAGES_MODIFY = 5

# Default bucket settings
DEFAULT_CAPACITY = 250  # Max tokens (one second of quota)
DEFAULT_REFILL_RATE = 250  # Tokens per second
DEFAULT_POLL_INTERVAL = 0.1  # Seconds between checks when waiting for tokens


class RateLimiter:
    """Token bucket rate limiter for Gmail API calls.

    Thread-safe implementation that tracks quota consumption and
    blocks when quota is exhausted.
    """

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        refill_rate: float = DEFAULT_REFILL_RATE,
    ):
        """Initialize rate limiter.

        Args:
            capacity: Maximum tokens in bucket (burst capacity)
            refill_rate: Tokens added per second
        """
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = Lock()

        # Backoff state for 429 handling
        self._backoff_until: float = 0.0
        self._consecutive_429s = 0

    def _refill(self) -> None:
        """Refill tokens based on elapsed time. Must be called with lock held."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
        self._last_refill = now

    def acquire(self, tokens: int = 1, timeout: float | None = None) -> bool:
        """Acquire tokens, blocking if necessary.

        Args:
            tokens: Number of tokens to acquire
            timeout: Maximum time to wait (None = wait forever)

        Returns:
            True if tokens acquired, False if timeout
        """
        if tokens > self.capacity:
            raise ValueError(
                f"Cannot acquire {tokens} tokens (capacity={self.capacity})"
            )

        start = time.monotonic()
        deadline = None if timeout is None else start + timeout

        while True:
            is_in_backoff = False
            with self._lock:
                # Check backoff first
                now = time.monotonic()
                if now < self._backoff_until:
                    wait_time = self._backoff_until - now
                    logger.debug(f"Rate limiter in backoff, waiting {wait_time:.1f}s")
                    is_in_backoff = True
                else:
                    self._refill()

                    if self._tokens >= tokens:
                        self._tokens -= tokens
                        return True

                    # Calculate wait time for tokens to refill
                    needed = tokens - self._tokens
                    wait_time = needed / self.refill_rate

            # Check timeout
            if deadline is not None and time.monotonic() + wait_time > deadline:
                return False

            # Wait outside the lock
            if is_in_backoff:
                # Sleep for full backoff duration (no need to poll)
                time.sleep(wait_time)
            else:
                # Poll periodically for token refill
                time.sleep(min(wait_time, DEFAULT_POLL_INTERVAL))

    def record_429(self, retry_after: float | None = None) -> None:
        """Record a 429 response and enter backoff.

        Args:
            retry_after: Retry-After header value in seconds (if provided)
        """
        with self._lock:
            self._consecutive_429s += 1

            # Exponential backoff: 1s, 2s, 4s, 8s, ... up to 60s
            if retry_after is not None:
                backoff = retry_after
            else:
                backoff = min(60.0, 2 ** (self._consecutive_429s - 1))

            self._backoff_until = time.monotonic() + backoff
            self._tokens = 0.0  # Drain bucket

            logger.warning(
                f"Rate limit hit (429), backing off {backoff:.1f}s "
                f"(consecutive: {self._consecutive_429s})"
            )

    def record_success(self) -> None:
        """Record a successful request, resetting backoff state."""
        with self._lock:
            if self._consecutive_429s > 0:
                self._consecutive_429s = 0
                logger.debug("Rate limiter backoff reset after success")

    @property
    def available_tokens(self) -> float:
        """Get current available tokens (for monitoring)."""
        with self._lock:
            self._refill()
            return self._tokens

    @property
    def in_backoff(self) -> bool:
        """Check if currently in backoff (for monitoring)."""
        with self._lock:
            return time.monotonic() < self._backoff_until


# Global rate limiter instance (shared across all Gmail clients)
_global_limiter: RateLimiter | None = None
_global_lock = Lock()


def get_rate_limiter() -> RateLimiter:
    """Get the global rate limiter instance."""
    global _global_limiter
    # Double-checked locking: check without lock first for performance
    if _global_limiter is not None:
        return _global_limiter
    with _global_lock:
        # Check again after acquiring lock (another thread may have initialized)
        if _global_limiter is None:
            _global_limiter = RateLimiter()
            logger.info(
                f"Initialized Gmail rate limiter: "
                f"capacity={DEFAULT_CAPACITY}, refill={DEFAULT_REFILL_RATE}/s"
            )
        return _global_limiter
