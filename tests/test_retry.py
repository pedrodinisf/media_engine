"""Tests for runtime/retry.py."""

from __future__ import annotations

import pytest

from media_engine.runtime.retry import (
    CLOUD_DEFAULT,
    LOCAL_DEFAULT,
    RetryPolicy,
    with_retry,
)


def test_local_default_one_attempt() -> None:
    assert LOCAL_DEFAULT.max_attempts == 1


def test_cloud_default_three_attempts_exponential() -> None:
    assert CLOUD_DEFAULT.max_attempts == 3
    assert CLOUD_DEFAULT.backoff == "exponential"


def test_delay_for_attempt_fixed() -> None:
    p = RetryPolicy(max_attempts=5, backoff="fixed", initial_delay=0.5, jitter=0.0)
    assert p.delay_for(1) == pytest.approx(0.5)
    assert p.delay_for(3) == pytest.approx(0.5)


def test_delay_for_attempt_exponential() -> None:
    p = RetryPolicy(
        max_attempts=5, backoff="exponential", initial_delay=1.0, jitter=0.0,
    )
    assert p.delay_for(1) == pytest.approx(1.0)
    assert p.delay_for(2) == pytest.approx(2.0)
    assert p.delay_for(3) == pytest.approx(4.0)


def test_delay_for_attempt_capped() -> None:
    p = RetryPolicy(
        max_attempts=10, backoff="exponential", initial_delay=1.0,
        max_delay=5.0, jitter=0.0,
    )
    assert p.delay_for(10) == pytest.approx(5.0)


def test_delay_for_zeroth_attempt_is_zero() -> None:
    p = RetryPolicy(max_attempts=3, initial_delay=10.0, jitter=0.0)
    assert p.delay_for(0) == 0.0


async def test_with_retry_succeeds_first_try() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    result = await with_retry(fn, policy=RetryPolicy(max_attempts=3, jitter=0.0))
    assert result == "ok"
    assert calls == 1


async def test_with_retry_recovers_on_attempt_n() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError(f"flaky {calls}")
        return "ok"

    result = await with_retry(
        fn,
        policy=RetryPolicy(max_attempts=5, backoff="fixed",
                           initial_delay=0.0, jitter=0.0),
    )
    assert result == "ok"
    assert calls == 3


async def test_with_retry_exhausts_and_reraises() -> None:
    async def fn() -> str:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await with_retry(
            fn,
            policy=RetryPolicy(max_attempts=2, backoff="fixed",
                               initial_delay=0.0, jitter=0.0),
        )


async def test_with_retry_calls_on_retry() -> None:
    calls = 0
    retries: list[int] = []

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("flaky")
        return "ok"

    def on_retry(attempt: int, exc: BaseException) -> None:
        retries.append(attempt)

    await with_retry(
        fn,
        policy=RetryPolicy(max_attempts=5, backoff="fixed",
                           initial_delay=0.0, jitter=0.0),
        on_retry=on_retry,
    )
    assert retries == [1, 2]
