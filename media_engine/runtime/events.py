"""Event types + EventBus emitted by Operations.

Phase 0 (commit 4) shipped only the type definitions. Phase 1 (commit 8)
adds a minimal in-process ``EventBus`` so the daemon can stream events
to subscribers. Real producers (ops emitting Progress) come on-line as
ops land in commits 10+.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, Field

from media_engine.artifacts import AnyArtifact


class _BaseEvent(BaseModel):
    event_id: str
    op_run_id: str
    job_id: str | None = None
    artifact_id: str | None = None
    timestamp: datetime


class OpStarted(_BaseEvent):
    type: Literal["op_started"] = "op_started"
    op_name: str
    inputs: list[str] = []
    params: dict[str, object] = {}


class Progress(_BaseEvent):
    type: Literal["progress"] = "progress"
    fraction: float
    message: str = ""
    phase: str | None = None


class ArtifactReady(_BaseEvent):
    type: Literal["artifact_ready"] = "artifact_ready"
    artifact: AnyArtifact


class OpCompleted(_BaseEvent):
    type: Literal["op_completed"] = "op_completed"
    outputs: list[str] = []
    duration_seconds: float
    cost: dict[str, float] = {}


class OpFailed(_BaseEvent):
    type: Literal["op_failed"] = "op_failed"
    error_class: str
    message: str
    retryable: bool = False
    suggested_action: str = ""
    traceback: str | None = None


class LogLine(_BaseEvent):
    type: Literal["log_line"] = "log_line"
    level: str
    source: str
    line: str


Event: TypeAlias = Annotated[
    OpStarted | Progress | ArtifactReady | OpCompleted | OpFailed | LogLine,
    Field(discriminator="type"),
]


class EventBus:
    """Minimal in-process pub-sub for ``Event`` instances.

    Multiple subscribers; each gets its own bounded queue. Producers call
    ``emit(event)`` (sync, never blocks). Subscribers iterate
    ``async for event in bus.subscribe()``. Dropping a slow subscriber
    does NOT block emit — overflowed queues drop their oldest entries.

    This is in-process only; cross-process event delivery (to CLI clients
    of the daemon) is the daemon's responsibility (it subscribes here and
    forwards over the socket).
    """

    DEFAULT_QUEUE_SIZE = 1024

    def __init__(self, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subscribers: list[asyncio.Queue[Event]] = []

    def emit(self, event: Event) -> None:
        for q in self._subscribers:
            if q.full():
                # Drop the oldest to keep emit non-blocking. Slow subscribers
                # lose history rather than wedging the producer.
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)

    async def subscribe(self) -> AsyncIterator[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.append(q)
        try:
            while True:
                yield await q.get()
        finally:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
