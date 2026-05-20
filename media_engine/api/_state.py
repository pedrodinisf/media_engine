"""App-level state passed to every route via FastAPI dependency injection.

One ``AppState`` per FastAPI app — attached to the lifespan and
retrieved by the ``get_state`` dependency. Holding the engine + cache
here (instead of module-level singletons) means tests can spin up a
fresh state per ``TestClient`` instance without globals leaking.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import cast

from media_engine.runtime.cache import Cache
from media_engine.runtime.engine import Engine


@dataclass
class AppState:
    """Shared engine + bookkeeping for the REST surface."""

    engine: Engine
    cache: Cache
    job_tasks: dict[str, asyncio.Task[None]] = field(
        default_factory=lambda: cast(dict[str, asyncio.Task[None]], {})
    )

    def track_job(self, job_id: str, task: asyncio.Task[None]) -> None:
        self.job_tasks[job_id] = task
        task.add_done_callback(lambda _t: self.job_tasks.pop(job_id, None))

    def untrack_job(self, job_id: str) -> None:
        self.job_tasks.pop(job_id, None)

    def pop_job_task(self, job_id: str) -> asyncio.Task[None] | None:
        return self.job_tasks.pop(job_id, None)
