"""Tests for the in-process rate limiter."""
from __future__ import annotations

import time

import pytest

from calorch.rate_limit import RateLimiter


def test_allows_up_to_per_minute():
    """First N requests within the window should pass."""
    limiter = RateLimiter(per_minute=5)
    for _ in range(5):
        allowed, retry = limiter.check("caller-1")
        assert allowed is True
        assert retry == 0


def test_blocks_after_limit_exceeded():
    """6th request in the window should be blocked."""
    limiter = RateLimiter(per_minute=5)
    for _ in range(5):
        limiter.check("caller-1")
    allowed, retry = limiter.check("caller-1")
    assert allowed is False
    assert retry >= 1


def test_separate_caller_buckets():
    """Each caller has independent counters."""
    limiter = RateLimiter(per_minute=3)
    for _ in range(3):
        assert limiter.check("alice")[0] is True
    # Alice is now blocked
    assert limiter.check("alice")[0] is False
    # Bob has a fresh bucket
    assert limiter.check("bob")[0] is True


def test_window_slides():
    """Old timestamps drop off the deque so the limit is per-window, not per-forever."""
    limiter = RateLimiter(per_minute=2)
    limiter._window = 0.2
    # First two pass
    assert limiter.check("caller")[0] is True
    assert limiter.check("caller")[0] is True
    # Third call: blocked AND sets blocked_until
    allowed, _ = limiter.check("caller")
    assert allowed is False
    # Verify the timestamps are still tracked
    assert len(limiter._buckets["caller"].timestamps) == 2
    # Wait for the entire window + block duration to clear
    time.sleep(1.5)
    # Now the call should be allowed
    assert limiter.check("caller")[0] is True


def test_blocked_caller_returns_retry_after():
    """When blocked, the retry_after should be in the future."""
    limiter = RateLimiter(per_minute=1)
    limiter.check("caller")
    allowed, retry = limiter.check("caller")
    assert allowed is False
    assert 0 < retry <= 62  # ~window + 1


def test_reset_clears_state():
    limiter = RateLimiter(per_minute=2)
    for _ in range(2):
        limiter.check("alice")
    assert limiter.check("alice")[0] is False
    limiter.reset()
    assert limiter.check("alice")[0] is True


def test_gc_removes_stale_buckets():
    """Buckets with no recent activity should be garbage-collected."""
    limiter = RateLimiter(per_minute=10)
    limiter._window = 0.05
    # Force GC to run on the next call
    limiter._last_gc = 0
    limiter.check("alice")
    time.sleep(0.1)
    # Reset the GC throttle so the next check() actually runs GC
    limiter._last_gc = 0
    limiter.check("bob")
    # Alice's stale entry should be gone
    assert "alice" not in limiter._buckets
