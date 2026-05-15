"""Tests for runtime/events.py EventBus."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from media_engine.runtime.events import EventBus, OpStarted, Progress


def _started(op_run_id: str = "r1", op_name: str = "x.y") -> OpStarted:
    return OpStarted(
        event_id=uuid4().hex,
        op_run_id=op_run_id,
        timestamp=datetime.now(UTC),
        op_name=op_name,
    )


def _progress(op_run_id: str = "r1", fraction: float = 0.5) -> Progress:
    return Progress(
        event_id=uuid4().hex,
        op_run_id=op_run_id,
        timestamp=datetime.now(UTC),
        fraction=fraction,
        message="working",
    )


@pytest.mark.asyncio
async def test_subscribe_receives_emitted_events() -> None:
    bus = EventBus()

    async def consume() -> list:
        out = []
        async for e in bus.subscribe():
            out.append(e)
            if len(out) == 2:
                return out
        return out

    task = asyncio.create_task(consume())
    # Let the subscriber register before we emit.
    await asyncio.sleep(0)
    bus.emit(_started())
    bus.emit(_progress())
    received = await asyncio.wait_for(task, timeout=1.0)
    assert len(received) == 2
    assert received[0].type == "op_started"
    assert received[1].type == "progress"


@pytest.mark.asyncio
async def test_emit_with_no_subscribers_is_noop() -> None:
    bus = EventBus()
    bus.emit(_started())  # must not raise


@pytest.mark.asyncio
async def test_subscriber_count() -> None:
    bus = EventBus()
    assert bus.subscriber_count == 0

    async def consume() -> None:
        async for _ in bus.subscribe():
            return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    assert bus.subscriber_count == 1
    bus.emit(_started())
    await asyncio.wait_for(task, timeout=1.0)
    # subscriber removed itself on exit
    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_full_queue_drops_oldest_to_keep_emit_nonblocking() -> None:
    bus = EventBus(queue_size=2)
    received: list = []

    async def consume() -> None:
        async for e in bus.subscribe():
            received.append(e)
            if len(received) == 2:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    # Emit 5 events; subscriber's queue holds 2 — emit must not block.
    for i in range(5):
        bus.emit(_progress(fraction=i / 4.0))
    await asyncio.wait_for(task, timeout=1.0)
    # We should have received 2 events (newest 2 thanks to drop-oldest semantics).
    assert len(received) == 2
