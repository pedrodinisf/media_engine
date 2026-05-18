"""Per-node retry policy for the DAG executor.

Cloud-shaped backends default to 3 attempts with exponential backoff +
``Retry-After`` header support (Phase 2 hardens this). Local-shaped
backends default to 1 attempt — reruns happen via the cache, not via
retry inside a single run.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, NamedTuple, TypeVar

T = TypeVar("T")


class RateLimited(Exception):
    """A backend hit a provider rate limit (HTTP 429).

    Carries the server's ``Retry-After`` (seconds) when it sent one so
    ``with_retry`` can wait exactly that long instead of guessing.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Classification(NamedTuple):
    retryable: bool
    retry_after: float | None
    error_class: str
    suggested_action: str


_RATE_LIMIT_RE = re.compile(r"\b429\b|rate.?limit|too many requests", re.I)
_TRANSIENT_RE = re.compile(
    r"\b(408|500|502|503|504)\b|timeout|timed out|temporarily|"
    r"unavailable|overloaded|connection (?:reset|refused|error)",
    re.I,
)
_AUTH_RE = re.compile(
    r"\b(401|403)\b|unauthorized|forbidden|invalid api key|"
    r"permission denied|not set",
    re.I,
)
# Deterministic, caller-fixable errors — never worth a retry.
_TERMINAL_TYPES: tuple[type[BaseException], ...] = (
    ValueError,
    TypeError,
    KeyError,
    FileNotFoundError,
    LookupError,
    NotImplementedError,
)


def _parse_retry_after(message: str) -> float | None:
    m = re.search(r"retry[- ]?after[\"':\s]+(\d+(?:\.\d+)?)", message, re.I)
    return float(m.group(1)) if m else None


def classify_exception(exc: BaseException) -> Classification:
    """Decide whether ``exc`` is worth retrying, and how to advise the user."""
    msg = str(exc)
    if isinstance(exc, RateLimited):
        return Classification(
            True,
            exc.retry_after if exc.retry_after is not None
            else _parse_retry_after(msg),
            "RateLimited",
            "Provider rate limit hit — backing off; "
            "reduce concurrency or add quota.",
        )
    if _RATE_LIMIT_RE.search(msg):
        return Classification(
            True, _parse_retry_after(msg), type(exc).__name__,
            "Provider rate limit hit — backing off; "
            "reduce concurrency or add quota.",
        )
    if _AUTH_RE.search(msg):
        return Classification(
            False, None, type(exc).__name__,
            "Auth/permission error — check the backend's API key / env var.",
        )
    if _TRANSIENT_RE.search(msg):
        return Classification(
            True, _parse_retry_after(msg), type(exc).__name__,
            "Transient backend error — retrying.",
        )
    if isinstance(exc, _TERMINAL_TYPES):
        return Classification(
            False, None, type(exc).__name__,
            "Deterministic error — fix inputs/params; a retry won't help.",
        )
    # Unclassified → treat as possibly-transient and let the policy's
    # attempt budget decide (preserves the pre-Phase-2 retry contract).
    return Classification(
        True, None, type(exc).__name__,
        "Unclassified error — retrying within the policy budget.",
    )


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
            cls = classify_exception(e)
            # Don't burn attempts on deterministic / auth failures.
            if not cls.retryable or attempt >= policy.max_attempts:
                raise
            if on_retry is not None:
                on_retry(attempt, e)
            # Honor a server-provided Retry-After over our backoff curve.
            delay = policy.delay_for(attempt)
            if cls.retry_after is not None:
                delay = max(delay, cls.retry_after)
            await asyncio.sleep(delay)
    # Unreachable (the loop either returns or raises) but keeps mypy happy.
    raise RuntimeError("with_retry exhausted without returning") from last_exc
