"""Server-Sent Events adapter over the engine's ``EventBus``.

Each ``GET /jobs/{id}/events`` opens an SSE stream; we subscribe to the
in-process ``EventBus`` and forward frames whose ``job_id`` matches.
The stream stays open until the client disconnects — pipelines can
emit many ``OpStarted`` / ``Progress`` / ``OpCompleted`` events as
nodes flow through the DAG, and only the consumer knows when it has
seen enough. Pair it with ``GET /jobs/{id}`` to poll terminal status
when needed.

We don't filter by ``op_run_id`` because a job can contain multiple op
runs (pipelines fan out); filtering by ``job_id`` is the right scope.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from media_engine.runtime.events import EventBus


async def job_event_stream(
    bus: EventBus,
    job_id: str,
    *,
    keepalive_seconds: float = 15.0,
    queue_size: int = 256,
) -> AsyncIterator[dict[str, str]]:
    """Yield SSE-shaped dicts for events belonging to ``job_id``.

    Closes when the consumer disconnects (``sse-starlette`` raises
    ``CancelledError`` on disconnect, which we let propagate so the
    underlying ``subscribe()`` generator unregisters cleanly). Emits a
    periodic ``: keepalive`` comment if no events arrive within
    ``keepalive_seconds`` — relevant behind reverse proxies that idle out.

    The internal queue is bounded (``queue_size``) so a slow client
    can't make us accumulate every progress frame in memory — when
    the queue is full we drop the oldest frame to keep the producer
    non-blocking. This mirrors the back-pressure ``EventBus`` already
    applies to its subscribers.
    """
    queue: asyncio.Queue[dict[str, str]] = asyncio.Queue(maxsize=queue_size)

    async def _pump() -> None:
        async for event in bus.subscribe():
            if event.job_id != job_id:
                continue
            frame = {
                "event": event.type,
                "data": event.model_dump_json(),
            }
            if queue.full():
                # Drop the oldest queued frame so the producer never
                # blocks. Slow consumers lose history rather than
                # backing up the event bus.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(frame)

    pumper = asyncio.create_task(_pump())
    try:
        while True:
            try:
                frame = await asyncio.wait_for(
                    queue.get(), timeout=keepalive_seconds
                )
            except TimeoutError:
                # SSE comment line — keeps proxies happy without polluting
                # the event channel a consumer is subscribed to.
                yield {"comment": "keepalive"}
                continue
            yield frame
    finally:
        pumper.cancel()
