"""Unit tests for `media_engine.runtime.heartbeat`."""

from __future__ import annotations

import asyncio
import contextlib
from typing import cast

import pytest

from media_engine.runtime.events import Event, Progress
from media_engine.runtime.heartbeat import heartbeat


@pytest.mark.asyncio
async def test_heartbeat_emits_at_expected_interval() -> None:
    """Heartbeat ticks once per interval and populates the new optional fields."""
    captured: list[Event] = []

    task = asyncio.create_task(
        heartbeat(
            emit=captured.append,
            op_run_id="op-test",
            job_id="job-test",
            eta_seconds_initial=10.0,
            pool_bytes=lambda: 1_234_567,
            available_memory_gb=lambda: 31.5,
            interval=0.05,
        )
    )
    # 0.18s @ 0.05s interval → at least 3 ticks before we cancel.
    await asyncio.sleep(0.18)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert len(captured) >= 3, f"expected ≥3 heartbeats, got {len(captured)}"
    for ev in captured:
        assert isinstance(ev, Progress)
        p = cast(Progress, ev)
        assert p.phase == "heartbeat"
        assert p.op_run_id == "op-test"
        assert p.job_id == "job-test"
        assert p.available_memory_gb == 31.5
        assert p.pool_bytes_estimate == 1_234_567
        # ETA shrinks toward zero; fraction grows toward one.
        assert p.eta_seconds is not None and 0.0 <= p.eta_seconds <= 10.0
        assert 0.0 <= p.fraction <= 1.0


@pytest.mark.asyncio
async def test_heartbeat_cancels_cleanly() -> None:
    """Cancellation is absorbed silently — no exception propagates."""
    task = asyncio.create_task(
        heartbeat(
            emit=lambda _e: None,
            op_run_id="op",
            job_id=None,
            eta_seconds_initial=0.0,
            interval=0.05,
        )
    )
    await asyncio.sleep(0.01)
    task.cancel()
    # Either CancelledError surfaces (Python 3.11 default) or returns None.
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_heartbeat_handles_zero_eta() -> None:
    """eta_seconds_initial=0 → fraction stays at 0 (no divide-by-zero)."""
    captured: list[Event] = []
    task = asyncio.create_task(
        heartbeat(
            emit=captured.append,
            op_run_id="op",
            job_id=None,
            eta_seconds_initial=0.0,
            interval=0.05,
        )
    )
    await asyncio.sleep(0.12)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert captured
    for ev in captured:
        p = cast(Progress, ev)
        assert p.fraction == 0.0
        assert p.eta_seconds == 0.0


@pytest.mark.asyncio
async def test_heartbeat_swallows_probe_exceptions() -> None:
    """A flaky psutil/pool probe doesn't kill the heartbeat task."""
    captured: list[Event] = []

    def boom() -> float:
        raise RuntimeError("probe failed")

    def boom_int() -> int:
        raise RuntimeError("pool failed")

    task = asyncio.create_task(
        heartbeat(
            emit=captured.append,
            op_run_id="op",
            job_id=None,
            eta_seconds_initial=5.0,
            pool_bytes=boom_int,
            available_memory_gb=boom,
            interval=0.05,
        )
    )
    await asyncio.sleep(0.12)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert captured, "heartbeat task should keep ticking despite probe errors"
    for ev in captured:
        p = cast(Progress, ev)
        assert p.available_memory_gb is None
        assert p.pool_bytes_estimate is None
