"""Server-Sent Events adapter over the engine's ``EventBus``.

Each ``GET /jobs/{id}/events`` opens an SSE stream. The pumper:

  1. Replays any events the job has already emitted (queried from the
     persistent ``events`` table by ``job_id``) — fixes B-001 where a
     client subscribing AFTER the job's ``OpStarted`` saw an empty
     stream forever.
  2. Subscribes to the in-process ``EventBus`` for live frames, dedup'd
     by ``event_id`` against the replayed set so an event that lands in
     both paths during the subscribe/replay window is delivered once.
  3. Yields live frames matching ``job_id`` (or every frame if
     ``job_id is None`` — the global tail mode).

The stream stays open until the client disconnects; pipelines emit
many ``OpStarted`` / ``Progress`` / ``OpCompleted`` events as nodes
flow through the DAG, and only the consumer knows when it has seen
enough. Pair it with ``GET /jobs/{id}`` to poll terminal status.

We don't filter by ``op_run_id`` because a job can contain multiple
op runs (pipelines fan out); filtering by ``job_id`` is the right
scope.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from media_engine.runtime.events import EventBus

if TYPE_CHECKING:
    from media_engine.runtime.cache import Cache


async def job_event_stream(
    bus: EventBus,
    job_id: str | None,
    *,
    cache: Cache | None = None,
    namespace: str | None = None,
    keepalive_seconds: float = 15.0,
    queue_size: int = 256,
) -> AsyncIterator[dict[str, str]]:
    """Yield SSE-shaped dicts for events belonging to ``job_id``.

    ``job_id=None`` opens a global stream (every event).

    When ``cache`` + ``job_id`` are both provided, the pumper replays
    any persisted events for that job BEFORE switching to live mode
    (B-001 fix: the per-job subscriber would otherwise miss events
    fired between ``POST /run`` returning and the EventSource handshake
    completing). The replay uses the ``events.job_id`` column and
    ordered ``ts ASC`` to preserve causal order; live events whose
    ``event_id`` was already in the replay set are skipped to avoid
    double-delivery in the subscribe/replay race window.
    """
    # Subscribe to the bus BEFORE we replay — that way any event that
    # fires during replay is captured live and not lost. The dedup set
    # filters out the replay/live overlap.
    queue: asyncio.Queue[dict[str, str]] = asyncio.Queue(maxsize=queue_size)

    async def _pump() -> None:
        async for event in bus.subscribe():
            if job_id is not None and event.job_id != job_id:
                continue
            frame = {
                "event": event.type,
                "data": event.model_dump_json(),
                "id": event.event_id,
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

    # Replay phase — fetch from cache. record_event now persists with
    # event_id as the row PK, so dedup by id is straightforward.
    replayed_ids: set[str] = set()
    if cache is not None and job_id is not None:
        try:
            replayed = cache.event_log(
                job_id=job_id, namespace=namespace, order="asc"
            )
        except Exception:  # noqa: BLE001 — replay is best-effort
            replayed = []
        for entry in replayed:
            yield {
                "event": entry.type,
                "data": entry.payload_json,
                "id": entry.id,
            }
            replayed_ids.add(entry.id)

    try:
        while True:
            try:
                frame = await asyncio.wait_for(
                    queue.get(), timeout=keepalive_seconds
                )
            except TimeoutError:
                # SSE comment line — keeps proxies happy without
                # polluting the event channel.
                yield {"comment": "keepalive"}
                continue
            # Drop duplicates that were both replayed AND emitted live.
            frame_id = frame.get("id", "")
            if frame_id and frame_id in replayed_ids:
                continue
            yield frame
    finally:
        pumper.cancel()
        # Wait for cancellation to propagate so the ``bus.subscribe()``
        # generator's finally-clause (which unregisters from the
        # EventBus subscriber list) actually runs before we return.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pumper
