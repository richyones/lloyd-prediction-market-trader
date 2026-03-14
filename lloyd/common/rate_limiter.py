from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Token-bucket rate limiter for async API calls.

    Callers ``await limiter.acquire()`` before each request.  If the bucket
    is empty the coroutine sleeps until a token becomes available.
    """

    def __init__(self, calls_per_minute: int) -> None:
        self._cpm = calls_per_minute
        self._tokens = float(calls_per_minute)
        self._last_refill = time.monotonic()

    async def acquire(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._cpm,
            self._tokens + elapsed * (self._cpm / 60.0),
        )
        self._last_refill = now

        if self._tokens < 1.0:
            wait = (1.0 - self._tokens) * (60.0 / self._cpm)
            await asyncio.sleep(wait)
            self._tokens = 0.0
            self._last_refill = time.monotonic()
        else:
            self._tokens -= 1.0


GEMINI_LIMITER = RateLimiter(calls_per_minute=14)
OPENAI_LIMITER = RateLimiter(calls_per_minute=50)
ANTHROPIC_LIMITER = RateLimiter(calls_per_minute=50)
