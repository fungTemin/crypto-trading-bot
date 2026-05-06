from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Awaitable
from typing import TypeVar

T = TypeVar("T")


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exponential: bool = True,
    jitter: bool = True,
) -> T:
    """Retry an async callable with exponential backoff.

    Args:
        fn: Async callable to retry.
        max_retries: Maximum number of retries (total attempts = max_retries + 1).
        base_delay: Initial delay in seconds.
        max_delay: Maximum delay cap.
        exponential: If True, delay doubles each retry.
        jitter: If True, add random jitter to avoid thundering herd.

    Returns:
        The return value of fn on success.

    Raises:
        The last exception if all retries are exhausted.
    """
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt) if exponential else base_delay
                delay = min(delay, max_delay)
                if jitter:
                    delay = delay * (0.5 + random.random())
                await asyncio.sleep(delay)

    raise last_exc  # type: ignore[misc]
