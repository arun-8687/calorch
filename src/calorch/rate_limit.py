"""In-process token-bucket rate limiter for FastAPI endpoints.

Production-grade rate limiting without external dependencies. Uses a sliding
window with per-caller key (API key, then IP fallback). The bucket resets on
process restart — for distributed rate limiting, swap to Redis (see
``RateLimiterBackend``).

Default: ``RATE_LIMIT_PER_MINUTE=30`` (configurable per environment).

Caveats:
  * In-process only — a 3-replica deployment allows 3x the configured rate.
    To get a true cluster-wide limit, set ``RATE_LIMIT_BACKEND=redis`` (TODO).
  * Memory grows linearly with unique callers; trim old entries every 10 min.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque


@dataclass
class _CallerBucket:
    """Per-caller sliding window of request timestamps."""
    timestamps: Deque[float] = field(default_factory=deque)
    blocked_until: float = 0.0


class RateLimiter:
    """Token-bucket-ish rate limiter with caller-keyed windows."""

    def __init__(self, *, per_minute: int = 30) -> None:
        self._per_minute = per_minute
        self._window = 60.0  # seconds
        self._buckets: dict[str, _CallerBucket] = {}
        self._lock = threading.Lock()
        self._last_gc = time.monotonic()

    def _gc(self) -> None:
        """Drop entries whose timestamps are all outside the window."""
        now = time.monotonic()
        if now - self._last_gc < 600:  # GC at most every 10 min
            return
        self._last_gc = now
        cutoff = now - self._window
        stale = [
            k for k, b in self._buckets.items()
            if (not b.timestamps or b.timestamps[-1] < cutoff) and b.blocked_until < now
        ]
        for k in stale:
            del self._buckets[k]

    def check(self, caller: str) -> tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)``.

        ``caller`` is the API key (or IP fallback) used to bucket the request.
        """
        now = time.monotonic()
        with self._lock:
            self._gc()
            bucket = self._buckets.setdefault(caller, _CallerBucket())
            # Currently blocked?
            if bucket.blocked_until > now:
                return False, int(bucket.blocked_until - now) + 1
            # Drop timestamps outside the window
            cutoff = now - self._window
            while bucket.timestamps and bucket.timestamps[0] < cutoff:
                bucket.timestamps.popleft()
            if len(bucket.timestamps) >= self._per_minute:
                # Block for the remainder of the window plus 1 second
                bucket.blocked_until = bucket.timestamps[0] + self._window + 1
                return False, int(bucket.blocked_until - now) + 1
            bucket.timestamps.append(now)
            return True, 0

    def reset(self) -> None:
        """Clear all state (for tests)."""
        with self._lock:
            self._buckets.clear()
            self._last_gc = time.monotonic()


_global_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """Get the global rate limiter, configured from settings."""
    global _global_limiter
    if _global_limiter is None:
        from calorch.config import get_settings
        _global_limiter = RateLimiter(per_minute=get_settings().rate_limit_per_minute)
    return _global_limiter


def reset_rate_limiter() -> None:
    """Reset for tests."""
    global _global_limiter
    _global_limiter = None
