"""Per-node retry policy for the DAG executor.

Cloud-shaped backends default to 3 attempts with exponential backoff +
``Retry-After`` header support (Phase 2 hardens this). Local-shaped
backends default to 1 attempt — reruns happen via the cache, not via
retry inside a single run.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    backoff: Literal["exponential", "fixed"] = "exponential"
    initial_delay: float = 1.0
    max_delay: float = 60.0
    jitter: float = 0.1  # +/- 10% by default

    def delay_for(self, attempt: int) -> float:
        """Delay (seconds) before the ``attempt``-th retry (1-indexed)."""
        if attempt < 1:
            return 0.0
        if self.backoff == "fixed":
            base = self.initial_delay
        else:
            base = self.initial_delay * (2 ** (attempt - 1))
        base = min(base, self.max_delay)
        if self.jitter > 0:
            base = base * (1.0 + random.uniform(-self.jitter, self.jitter))
        return max(0.0, base)


LOCAL_DEFAULT = RetryPolicy(max_attempts=1)
CLOUD_DEFAULT = RetryPolicy(max_attempts=3, backoff="exponential", initial_delay=1.0)


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """Run ``fn`` with the given retry policy.

    On each attempt-failure that's not the last, sleeps according to
    ``policy.delay_for(attempt)`` and (if ``on_retry`` is set) reports the
    pending retry. The last failure re-raises.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max(1, policy.max_attempts) + 1):
        try:
            return await fn()
        except BaseException as e:  # noqa: BLE001 -- intentionally broad
            last_exc = e
            if attempt >= policy.max_attempts:
                raise
            if on_retry is not None:
                on_retry(attempt, e)
            await asyncio.sleep(policy.delay_for(attempt))
    # Unreachable (the loop either returns or raises) but keeps mypy happy.
    raise RuntimeError("with_retry exhausted without returning") from last_exc
