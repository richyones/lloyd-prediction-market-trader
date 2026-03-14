from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, TypeVar

import httpx
import structlog

log = structlog.get_logger()

F = TypeVar("F", bound=Callable[..., Any])


def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> Callable[[F], F]:
    """Decorator for async functions: exponential back-off on transient errors.

    Retries on ``httpx.ConnectError`` and on ``httpx.HTTPStatusError`` when the
    status code is 429 or >= 500.  Delay doubles each attempt (1 s, 2 s, 4 s).
    """

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            for attempt in range(1, max_retries + 1):
                try:
                    return await fn(*args, **kwargs)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code not in (429,) and exc.response.status_code < 500:
                        raise
                    last_exc = exc
                except httpx.ConnectError as exc:
                    last_exc = exc

                delay = base_delay * (2 ** (attempt - 1))
                log.warning(
                    "retrying",
                    function=fn.__qualname__,
                    attempt=attempt,
                    max_retries=max_retries,
                    delay_seconds=delay,
                    error=str(last_exc),
                )
                await asyncio.sleep(delay)

            raise last_exc  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorator
