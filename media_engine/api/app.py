"""FastAPI app factory + lifespan.

``build_app`` returns a fully wired ``FastAPI`` instance. The engine is
attached to ``app.state.app_state`` via a lifespan handler so that
``TestClient(build_app())`` doesn't need extra setup, and so production
deployments (``uvicorn media_engine.api.app:get_app`` or
``med api start``) share the exact same code path as tests.

Bootstrap policy (the only writeful endpoint that bypasses auth):
the first time the app starts against a fresh cache (``api_tokens``
empty), it doesn't try to silently create a token — the operator runs
``med api token create --label bootstrap`` once. The CLI talks to the
*cache*, not the running API, so no token is needed to seed the first
one. After that, every endpoint requires a bearer.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import TYPE_CHECKING

from fastapi import FastAPI

from media_engine.api._state import AppState
from media_engine.api.health import router as health_router
from media_engine.api.routes import router
from media_engine.bootstrap import register_all
from media_engine.config import EngineConfig
from media_engine.runtime.engine import Engine
from media_engine.runtime.gc import gc_interval_from_env, periodic_workdir_gc

if TYPE_CHECKING:
    pass


def build_app(
    *, engine: Engine | None = None, config: EngineConfig | None = None
) -> FastAPI:
    """Build a FastAPI app bound to an engine.

    Tests pass an explicit ``engine`` (built against a tmp cache); the
    CLI / production paths pass a ``config`` and let the lifespan open
    a session engine.
    """
    register_all()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        if engine is not None:
            local_engine = engine
            own_engine = False
        else:
            local_engine = Engine.open_session(config or EngineConfig.load())
            own_engine = True
        app.state.app_state = AppState(
            engine=local_engine, cache=local_engine.cache
        )
        # Recovery: if a previous process crashed mid-run, jobs are
        # frozen in "running"/"pending" forever. Flip them to
        # "failed" so clients see a clear terminal state instead of
        # a phantom in-flight row. Scoped to this engine's namespace
        # so a tenant restart can't trip another tenant's jobs.
        with contextlib.suppress(Exception):
            local_engine.cache.fail_orphaned_jobs(
                namespace=local_engine.config.namespace
            )
        # Workdir garbage collection. ``Engine.run`` already cleans up
        # the per-job tmp dir on success/failure paths, but a process
        # crash mid-run leaves residue. The periodic sweep catches
        # those orphans on the same cadence the daemon uses.
        gc_task = asyncio.create_task(
            periodic_workdir_gc(
                local_engine.config.workdir,
                interval=timedelta(seconds=gc_interval_from_env()),
                retention=timedelta(
                    hours=local_engine.config.gc_workdir_retention_hours
                ),
            )
        )
        try:
            yield
        finally:
            gc_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await gc_task
            # Cancel any still-running job tasks **and await them** so
            # the event loop can shut down cleanly (otherwise uvicorn
            # emits ``Task was destroyed but it is pending`` warnings
            # on SIGTERM).
            pending = list(app.state.app_state.job_tasks.values())
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if own_engine:
                with contextlib.suppress(Exception):
                    local_engine.close()

    app = FastAPI(
        title="media_engine",
        version="0.1.0",
        description=(
            "Universal media-processing engine — typed artifacts, "
            "composable operations, pluggable backends."
        ),
        lifespan=lifespan,
    )
    app.include_router(health_router)
    app.include_router(router)
    return app


def get_app() -> FastAPI:
    """Module-level factory for uvicorn ``--factory``.

    Usage: ``uvicorn media_engine.api.app:get_app --factory``.
    """
    return build_app()
