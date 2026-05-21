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

import contextlib
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from fastapi import FastAPI

from media_engine.api._state import AppState
from media_engine.api.health import router as health_router
from media_engine.api.routes import router
from media_engine.bootstrap import register_all
from media_engine.config import EngineConfig
from media_engine.runtime.engine import Engine

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
        try:
            yield
        finally:
            # Cancel any still-running job tasks so the loop can shut down.
            for task in list(app.state.app_state.job_tasks.values()):
                task.cancel()
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
