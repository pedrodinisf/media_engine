"""Exception classification + Retry-After honoring in with_retry."""

from __future__ import annotations

import time

import pytest

from media_engine.runtime.retry import (
    RateLimited,
    RetryPolicy,
    classify_exception,
    with_retry,
)


def test_rate_limit_detected_from_message() -> None:
    c = classify_exception(RuntimeError("HTTP 429 Too Many Requests"))
    assert c.retryable is True
    assert "rate limit" in c.suggested_action.lower()


def test_rate_limited_carries_retry_after() -> None:
    c = classify_exception(RateLimited("slow down", retry_after=2.5))
    assert c.retryable is True
    assert c.retry_after == 2.5
    assert c.error_class == "RateLimited"


def test_retry_after_parsed_from_message() -> None:
    c = classify_exception(RuntimeError("429: retry-after 7"))
    assert c.retry_after == 7.0


def test_auth_is_not_retryable() -> None:
    c = classify_exception(RuntimeError("401 Unauthorized: invalid api key"))
    assert c.retryable is False
    assert "key" in c.suggested_action.lower()


def test_transient_is_retryable() -> None:
    assert classify_exception(RuntimeError("503 unavailable")).retryable
    assert classify_exception(TimeoutError("timed out")).retryable


def test_deterministic_errors_not_retryable() -> None:
    assert classify_exception(ValueError("bad schema")).retryable is False
    assert classify_exception(KeyError("x")).retryable is False
    assert classify_exception(FileNotFoundError("nope")).retryable is False


def test_unclassified_retryable_preserves_budget() -> None:
    c = classify_exception(RuntimeError("flaky"))
    assert c.retryable is True


async def test_with_retry_skips_nonretryable() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("deterministic")

    with pytest.raises(ValueError, match="deterministic"):
        await with_retry(
            fn,
            policy=RetryPolicy(max_attempts=5, initial_delay=0.0, jitter=0.0),
        )
    assert calls == 1  # not retried


async def test_with_retry_honors_retry_after() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RateLimited("429", retry_after=0.25)
        return "ok"

    start = time.monotonic()
    out = await with_retry(
        fn,
        policy=RetryPolicy(
            max_attempts=3, backoff="fixed", initial_delay=0.0, jitter=0.0
        ),
    )
    assert out == "ok"
    # Slept ~retry_after even though the policy delay was 0.
    assert time.monotonic() - start >= 0.2
