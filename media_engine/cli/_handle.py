"""Engine handle — the seam that makes the CLI daemon-aware.

Every ``med`` command goes through ``open_handle(config)``. If a daemon is
running (socket present and pongs within 50 ms) the handle routes op
execution + reads through it (warm models, shared resource semaphores).
Otherwise it falls back to an in-process ``Engine.open_quick`` — identical
behavior, just cold.

Both flavors share one async surface so command code never branches on
"is the daemon up?".

A subtle but important property: the daemon and a local engine point at the
**same** ``cache.db`` + content-addressed store (both resolve ``cache_db_url``
from the same config). So even when ``run_pipeline`` runs the DAG client-side
and dispatches each node's op through the daemon, the daemon resolves input
ids and persists outputs into the shared store — the client reads the
results straight back. Pipelines get warm models without a RunPipeline RPC.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any, Protocol

from media_engine.artifacts import AnyArtifact, Kind
from media_engine.config import EngineConfig
from media_engine.daemon.client import DaemonClient
from media_engine.ops import CostEstimate
from media_engine.runtime.cache import CostLogEntry, EventLogEntry
from media_engine.runtime.cost_tracker import CostSummary
from media_engine.runtime.dag import (
    DAGResult,
    Pipeline,
    execute_pipeline,
    make_default_semaphores,
)
from media_engine.runtime.engine import Engine
from media_engine.runtime.lineage import LineageNode


def _socket_path(config: EngineConfig):
    return config.daemon_socket or (config.config_dir / "daemon.sock")


class EngineHandle(Protocol):
    """Async surface the CLI needs. Implemented locally and daemon-routed."""

    routed_via_daemon: bool

    async def run(
        self,
        op_name: str,
        *,
        inputs: list[str] | None = None,
        backend: str | None = None,
        **params: Any,
    ) -> list[AnyArtifact]: ...

    async def get_artifact(self, artifact_id: str) -> AnyArtifact | None: ...

    async def list_artifacts(
        self, kind: Kind | None = None, limit: int = 100
    ) -> list[AnyArtifact]: ...

    async def lineage(
        self, artifact_id: str, max_depth: int = 10
    ) -> LineageNode | None: ...

    async def resolve_id(self, prefix: str) -> str: ...

    async def run_pipeline(self, pipeline: Pipeline) -> DAGResult: ...

    def estimate_op_cost(
        self, op_name: str, *, inputs: list[str] | None = None, **params: Any
    ) -> CostEstimate: ...

    def cost_summary(
        self, *, since: datetime | None = None, op_name: str | None = None
    ) -> CostSummary: ...

    def cost_log_entries(
        self,
        *,
        since: datetime | None = None,
        op_name: str | None = None,
        limit: int | None = None,
    ) -> list[CostLogEntry]: ...

    def event_history(
        self,
        *,
        since: datetime | None = None,
        op_run_id: str | None = None,
        limit: int | None = None,
    ) -> list[EventLogEntry]: ...

    async def aclose(self) -> None: ...


class LocalEngineHandle:
    """In-process engine. Sync engine reads are awaited trivially."""

    routed_via_daemon = False

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    async def run(
        self,
        op_name: str,
        *,
        inputs: list[str] | None = None,
        backend: str | None = None,
        **params: Any,
    ) -> list[AnyArtifact]:
        return await self._engine.run(
            op_name, inputs=inputs, backend=backend, **params
        )

    async def get_artifact(self, artifact_id: str) -> AnyArtifact | None:
        return self._engine.get_artifact(artifact_id)

    async def list_artifacts(
        self, kind: Kind | None = None, limit: int = 100
    ) -> list[AnyArtifact]:
        return self._engine.list_artifacts(kind=kind, limit=limit)

    async def lineage(
        self, artifact_id: str, max_depth: int = 10
    ) -> LineageNode | None:
        return self._engine.lineage(artifact_id, max_depth=max_depth)

    async def resolve_id(self, prefix: str) -> str:
        return self._engine.resolve_id(prefix)

    async def run_pipeline(self, pipeline: Pipeline) -> DAGResult:
        return await self._engine.run_pipeline(pipeline)

    def estimate_op_cost(
        self, op_name: str, *, inputs: list[str] | None = None, **params: Any
    ) -> CostEstimate:
        return self._engine.estimate_op_cost(op_name, inputs=inputs, **params)

    def cost_summary(
        self, *, since: datetime | None = None, op_name: str | None = None
    ) -> CostSummary:
        return self._engine.cost_summary(since=since, op_name=op_name)

    def cost_log_entries(
        self,
        *,
        since: datetime | None = None,
        op_name: str | None = None,
        limit: int | None = None,
    ) -> list[CostLogEntry]:
        return self._engine.cost_log_entries(
            since=since, op_name=op_name, limit=limit
        )

    def event_history(
        self,
        *,
        since: datetime | None = None,
        op_run_id: str | None = None,
        limit: int | None = None,
    ) -> list[EventLogEntry]:
        return self._engine.event_log_entries(
            since=since, op_run_id=op_run_id, limit=limit
        )

    async def aclose(self) -> None:
        self._engine.close()


class DaemonEngineHandle:
    """Routes through a running daemon. Heavy work (op exec) lands on the
    daemon's warm process; DAG topology + cost estimation stay client-side."""

    routed_via_daemon = True

    def __init__(self, client: DaemonClient, config: EngineConfig) -> None:
        self._client = client
        self._config = config

    async def run(
        self,
        op_name: str,
        *,
        inputs: list[str] | None = None,
        backend: str | None = None,
        **params: Any,
    ) -> list[AnyArtifact]:
        return await self._client.run_op(
            op_name, inputs=inputs, backend=backend, **params
        )

    async def get_artifact(self, artifact_id: str) -> AnyArtifact | None:
        return await self._client.get_artifact(artifact_id)

    async def list_artifacts(
        self, kind: Kind | None = None, limit: int = 100
    ) -> list[AnyArtifact]:
        return await self._client.list_artifacts(kind=kind, limit=limit)

    async def lineage(
        self, artifact_id: str, max_depth: int = 10
    ) -> LineageNode | None:
        return await self._client.lineage(artifact_id, max_depth=max_depth)

    async def resolve_id(self, prefix: str) -> str:
        return await self._client.resolve_id(prefix)

    async def run_pipeline(self, pipeline: Pipeline) -> DAGResult:
        # Push sources into the shared cache so the daemon can resolve them,
        # then run the DAG client-side dispatching each op to the daemon.
        with Engine.open_quick(self._config) as local:
            for artifact in pipeline.sources.values():
                local.cache.upsert_artifact(artifact)
        return await execute_pipeline(
            pipeline,
            run_op=self._client.run_op,
            semaphores=make_default_semaphores(),
        )

    def estimate_op_cost(
        self, op_name: str, *, inputs: list[str] | None = None, **params: Any
    ) -> CostEstimate:
        # Pure cost arithmetic — no execution, no model load. A transient
        # local engine reads inputs from the shared cache.
        with Engine.open_quick(self._config) as local:
            return local.estimate_op_cost(op_name, inputs=inputs, **params)

    def cost_summary(
        self, *, since: datetime | None = None, op_name: str | None = None
    ) -> CostSummary:
        # Ledger lives in the shared cache.db — a transient local engine
        # reads it without involving the daemon.
        with Engine.open_quick(self._config) as local:
            return local.cost_summary(since=since, op_name=op_name)

    def cost_log_entries(
        self,
        *,
        since: datetime | None = None,
        op_name: str | None = None,
        limit: int | None = None,
    ) -> list[CostLogEntry]:
        with Engine.open_quick(self._config) as local:
            return local.cost_log_entries(
                since=since, op_name=op_name, limit=limit
            )

    def event_history(
        self,
        *,
        since: datetime | None = None,
        op_run_id: str | None = None,
        limit: int | None = None,
    ) -> list[EventLogEntry]:
        with Engine.open_quick(self._config) as local:
            return local.event_log_entries(
                since=since, op_run_id=op_run_id, limit=limit
            )

    async def aclose(self) -> None:
        await self._client.close()


@contextlib.asynccontextmanager
async def open_handle(
    config: EngineConfig,
    *,
    ping_timeout: float = 0.05,
) -> AsyncGenerator[EngineHandle]:
    """Yield a daemon-routed handle when a daemon is up, else a local one.

    The 50 ms ping budget keeps cold-CLI latency unchanged when no daemon
    is running (a missing socket short-circuits without even connecting).
    """
    client = await DaemonClient.connect(_socket_path(config), timeout=ping_timeout)
    if client is not None:
        handle: EngineHandle = DaemonEngineHandle(client, config)
    else:
        handle = LocalEngineHandle(Engine.open_quick(config))
    try:
        yield handle
    finally:
        await handle.aclose()


# Silence "imported but unused" for the type-only re-exports above.
__all__ = [
    "DaemonEngineHandle",
    "EngineHandle",
    "LocalEngineHandle",
    "open_handle",
]
