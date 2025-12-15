"""Tests for Gmail API rate limiter."""

import time

import pytest

from triage.gmail.rate_limiter import RateLimiter


class TestRateLimiter:
    """Tests for RateLimiter class."""

    def test_acquire_within_capacity(self):
        """Should acquire tokens immediately when within capacity."""
        limiter = RateLimiter(capacity=100, refill_rate=100)

        start = time.monotonic()
        assert limiter.acquire(10) is True
        elapsed = time.monotonic() - start

        assert elapsed < 0.1  # Should be nearly instant
        assert limiter.available_tokens == pytest.approx(90, abs=5)

    def test_acquire_blocks_when_exhausted(self):
        """Should block when tokens exhausted, then succeed after refill."""
        limiter = RateLimiter(capacity=10, refill_rate=100)

        # Exhaust tokens
        limiter.acquire(10)

        # Next acquire should block briefly for refill
        start = time.monotonic()
        assert limiter.acquire(5) is True
        elapsed = time.monotonic() - start

        # Should have waited ~50ms for 5 tokens at 100/sec
        assert 0.03 < elapsed < 0.2

    def test_acquire_timeout(self):
        """Should return False when timeout exceeded."""
        limiter = RateLimiter(capacity=10, refill_rate=10)

        # Exhaust tokens
        limiter.acquire(10)

        # Try to acquire with short timeout
        start = time.monotonic()
        result = limiter.acquire(10, timeout=0.1)
        elapsed = time.monotonic() - start

        assert result is False
        assert elapsed < 0.2

    def test_acquire_too_many_tokens_raises(self):
        """Should raise ValueError if requesting more than capacity."""
        limiter = RateLimiter(capacity=10, refill_rate=10)

        with pytest.raises(ValueError, match="Cannot acquire 20 tokens"):
            limiter.acquire(20)

    def test_record_429_enters_backoff(self):
        """Should enter backoff state after 429."""
        limiter = RateLimiter(capacity=100, refill_rate=100)

        assert limiter.in_backoff is False

        limiter.record_429()

        assert limiter.in_backoff is True
        # Tokens drained to 0, but may have tiny refill by time we check
        assert limiter.available_tokens < 1

    def test_record_429_exponential_backoff(self):
        """Consecutive 429s should increase backoff time."""
        limiter = RateLimiter(capacity=100, refill_rate=100)

        # First 429: 1 second backoff
        limiter.record_429()
        assert limiter._consecutive_429s == 1

        # Simulate waiting out the first backoff
        limiter._backoff_until = 0

        # Second 429: 2 second backoff
        limiter.record_429()
        assert limiter._consecutive_429s == 2

        # Third 429: 4 second backoff
        limiter._backoff_until = 0
        limiter.record_429()
        assert limiter._consecutive_429s == 3

    def test_record_429_with_retry_after(self):
        """Should use Retry-After header when provided."""
        limiter = RateLimiter(capacity=100, refill_rate=100)

        start = time.monotonic()
        limiter.record_429(retry_after=0.5)

        # Backoff should be ~0.5 seconds from now
        expected_backoff_until = start + 0.5
        assert limiter._backoff_until == pytest.approx(expected_backoff_until, abs=0.1)

    def test_record_success_resets_backoff(self):
        """Success should reset consecutive 429 counter."""
        limiter = RateLimiter(capacity=100, refill_rate=100)

        # Simulate multiple 429s
        limiter.record_429()
        limiter._backoff_until = 0
        limiter.record_429()
        assert limiter._consecutive_429s == 2

        # Success resets counter
        limiter.record_success()
        assert limiter._consecutive_429s == 0

    def test_acquire_waits_for_backoff(self):
        """Acquire should wait if in backoff state."""
        limiter = RateLimiter(capacity=100, refill_rate=100)

        # Enter short backoff
        limiter._backoff_until = time.monotonic() + 0.2
        limiter._tokens = 100  # Ensure tokens available

        start = time.monotonic()
        assert limiter.acquire(1) is True
        elapsed = time.monotonic() - start

        # Should have waited for backoff
        assert elapsed >= 0.15

    def test_thread_safety(self):
        """Rate limiter should be thread-safe."""
        import threading

        limiter = RateLimiter(capacity=100, refill_rate=1000)
        acquired_count = 0
        lock = threading.Lock()

        def acquire_tokens():
            nonlocal acquired_count
            for _ in range(10):
                if limiter.acquire(1, timeout=1.0):
                    with lock:
                        acquired_count += 1

        threads = [threading.Thread(target=acquire_tokens) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 50 acquires should succeed (5 threads * 10 each)
        assert acquired_count == 50

    def test_refill_over_time(self):
        """Tokens should refill over time."""
        limiter = RateLimiter(capacity=100, refill_rate=100)

        # Exhaust tokens
        limiter.acquire(100)
        assert limiter.available_tokens < 5

        # Wait for partial refill
        time.sleep(0.2)

        # Should have ~20 tokens now (0.2s * 100/s)
        assert 15 < limiter.available_tokens < 30

    def test_capacity_limit(self):
        """Tokens should not exceed capacity."""
        limiter = RateLimiter(capacity=100, refill_rate=1000)

        # Even with high refill rate and waiting, shouldn't exceed capacity
        time.sleep(0.2)

        assert limiter.available_tokens <= 100
