"""``Engine`` — public Python API.

Phase 0 ships:
- ``open_quick`` / ``open_session`` factories (one-shot vs long-lived)
- read-only surface (``get_artifact``, ``list_artifacts``, ``lineage``,
  ``resolve_id``)
- ``run(op_name, *, inputs=None, backend=None, **params)`` — single-op
  execution with content-addressed caching

The DAG executor (``run_pipeline``) lands in Phase 1 commit 14; this commit
ships a sequential v1 that just iterates a list of steps.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, cast
from uuid import uuid4

from media_engine.artifacts import (
    AnyArtifact,
    Kind,
    canonical_params_hash,
)
from media_engine.backends import BackendRegistry
from media_engine.config import EngineConfig
from media_engine.ops import CostEstimate, Operation, OperationContext, OpRegistry
from media_engine.runtime.cache import Cache, CostLogEntry, EventLogEntry
from media_engine.runtime.cost_tracker import CostSummary, CostTracker
from media_engine.runtime.dag import (
    DAGResult,
    Pipeline,
    execute_pipeline,
    make_default_semaphores,
)
from media_engine.runtime.disk_guard import assert_free_space
from media_engine.runtime.events import (
    EventBus,
    OpCompleted,
    OpStarted,
    build_op_failed,
)
from media_engine.runtime.lineage import LineageNode
from media_engine.runtime.model_pool import ModelPool
from media_engine.runtime.server_manager import ServerManager
from media_engine.runtime.storage import LocalFSStorage, StorageBackend

if TYPE_CHECKING:
    pass


def _actual_usage(
    outputs: list[AnyArtifact],
) -> tuple[float, int, int]:
    """Sum backend-reported usage across an op's outputs.

    Cloud backends stamp ``metadata['usage'] = {cost_cents, input_tokens,
    output_tokens, ...}``. Local backends report zeros. Returns
    ``(cents, tokens_in, tokens_out)``.
    """
    cents = 0.0
    tin = 0
    tout = 0
    for o in outputs:
        raw = o.metadata.get("usage")
        if isinstance(raw, dict):
            usage: dict[str, Any] = cast("dict[str, Any]", raw)
            cents += float(usage.get("cost_cents", 0.0) or 0.0)
            tin += int(usage.get("input_tokens", 0) or 0)
            tout += int(usage.get("output_tokens", 0) or 0)
    return cents, tin, tout


class Engine:
    """Public engine handle. Use ``open_quick()`` or ``open_session()``."""

    def __init__(
        self,
        config: EngineConfig,
        cache: Cache,
        storage: StorageBackend,
        *,
        event_bus: EventBus | None = None,
        server_manager: ServerManager | None = None,
        model_pool: ModelPool | None = None,
    ) -> None:
        self.config = config
        self.cache = cache
        self.storage = storage
        self.event_bus = event_bus or EventBus()
        self.server_manager = server_manager or ServerManager(
            config.permanent_store / "server-state"
        )
        self.model_pool = model_pool or ModelPool()
        # Lazy: created on first run_pipeline call from inside the running event
        # loop (asyncio.Semaphore needs a loop to bind to).
        self._semaphores: dict[str, asyncio.Semaphore] | None = None
        # Durable event tail + weekly rotation (best-effort; a broken
        # sink must never wedge a producer — EventBus swallows sink errors).
        self.event_bus.add_sink(self._persist_event)
        with contextlib.suppress(Exception):
            self.cache.prune_events(
                older_than=datetime.now(UTC) - timedelta(days=7)
            )

    def _persist_event(self, event: Any) -> None:
        op_name = getattr(event, "op_name", None)
        self.cache.record_event(
            ts=event.timestamp,
            event_type=event.type,
            op_run_id=event.op_run_id,
            op_name=op_name,
            payload_json=event.model_dump_json(),
            namespace=self.config.namespace,
        )

    def _get_semaphores(self) -> dict[str, asyncio.Semaphore]:
        if self._semaphores is None:
            self._semaphores = make_default_semaphores()
        return self._semaphores

    @classmethod
    def open_quick(cls, config: EngineConfig | None = None) -> Self:
        """Stateless one-shot. SQLite open + storage validation. No model loads."""
        cfg = config or EngineConfig.load()
        cfg.validate_storage()
        cache = Cache(cfg.resolve_cache_db_url())
        storage = LocalFSStorage(cfg.permanent_store, cfg.workdir)
        return cls(cfg, cache, storage)

    @classmethod
    def open_session(cls, config: EngineConfig | None = None) -> Self:
        """Long-lived session. Same surface as ``open_quick`` today; intended
        for the daemon (commit 8) — holds warm model pool + server processes
        across many CLI clients."""
        return cls.open_quick(config)

    # ── Read API ──

    def get_artifact(self, artifact_id: str) -> AnyArtifact | None:
        return self.cache.get_artifact(artifact_id, namespace=self.config.namespace)

    def list_artifacts(
        self,
        kind: Kind | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[AnyArtifact]:
        return self.cache.list_artifacts(
            kind=kind, since=since, limit=limit, namespace=self.config.namespace
        )

    def lineage(self, artifact_id: str, max_depth: int = 10) -> LineageNode | None:
        return self.cache.lineage_tree(
            artifact_id, namespace=self.config.namespace, max_depth=max_depth
        )

    def resolve_id(self, prefix: str) -> str:
        """Git-style prefix → full sha256. Raises on miss or ambiguity."""
        matches = self.cache.resolve_id_prefix(prefix, namespace=self.config.namespace)
        if not matches:
            raise LookupError(f"No artifact id starting with {prefix!r}")
        if len(matches) > 1:
            preview = ", ".join(m[:12] for m in matches[:5])
            raise LookupError(
                f"Ambiguous prefix {prefix!r}: matches {len(matches)} ids "
                f"(e.g. {preview})"
            )
        return matches[0]

    # ── Run API ──

    async def run(
        self,
        op_name: str,
        *,
        inputs: list[str] | None = None,
        backend: str | None = None,
        **params: Any,
    ) -> list[AnyArtifact]:
        """Execute a single operation with content-addressed caching.

        Resolves the op + (optional) backend, validates input kinds and
        params, looks up the cache, and either returns cached artifacts or
        runs the op and persists the result.
        """
        op_class = OpRegistry.get(op_name)
        op = op_class()

        input_ids = list(inputs or [])
        resolved_inputs = self._resolve_inputs(input_ids)
        self._validate_input_kinds(
            op_name,
            op_class.input_kinds,
            resolved_inputs,
            variadic=op_class.variadic_inputs,
        )

        params_model = op_class.params_model(**params)
        params_hash = canonical_params_hash(params_model)

        backend_name, backend_version = self._resolve_backend(op_class, backend)

        # Disk-space precondition: refuse to start if the permanent_store
        # filesystem is below the configured floor. Cache hits skip this
        # because they don't write — checked AFTER cache lookup below.
        cached = self.cache.find_cached_run(
            op_name=op_name,
            op_version=op_class.version,
            backend_name=backend_name,
            backend_version=backend_version,
            params_hash=params_hash,
            input_ids=input_ids,
            namespace=self.config.namespace,
        )
        if cached is not None:
            hits: list[AnyArtifact] = []
            for oid in cached:
                a = self.cache.get_artifact(oid, namespace=self.config.namespace)
                if a is not None:
                    hits.append(a)
            if len(hits) == len(cached):
                return hits
            # cache row points at artifacts that no longer exist — fall through
            # and re-run the op (lazy GC of stale rows happens elsewhere).

        # No cache hit → we'll write. Enforce the disk-space gate now.
        assert_free_space(self.config.permanent_store, self.config.min_free_gb)

        job_id = uuid4().hex
        workdir = self.storage.ensure_workdir(job_id)
        ctx = OperationContext(
            workdir=workdir,
            config=self.config,
            storage=self.storage,
            namespace=self.config.namespace,
            emit=self.event_bus.emit,
            server_manager=self.server_manager,
            model_pool=self.model_pool,
            run_op=self.run,
        )

        started_at = datetime.now(UTC)
        self.event_bus.emit(
            OpStarted(
                event_id=uuid4().hex,
                op_run_id=job_id,
                job_id=job_id,
                timestamp=started_at,
                op_name=op_name,
                inputs=list(input_ids),
                params=params_model.model_dump(mode="json"),
            )
        )
        try:
            raw_outputs = await op.run(resolved_inputs, params_model, ctx)
        except BaseException as exc:  # noqa: BLE001 -- envelope, then re-raise
            self.event_bus.emit(
                build_op_failed(exc, op_run_id=job_id, job_id=job_id)
            )
            raise
        finally:
            # Workdir cleanup is best-effort; failures shouldn't mask op errors.
            with contextlib.suppress(Exception):
                self.storage.cleanup_workdir(job_id)
        finished_at = datetime.now(UTC)
        duration = (finished_at - started_at).total_seconds()

        cost = op.cost_estimate(resolved_inputs, params_model)
        run_id = self.cache.record_run(
            op_name=op_name,
            op_version=op_class.version,
            backend_name=backend_name,
            backend_version=backend_version,
            params=params_model.model_dump(mode="json"),
            params_hash=params_hash,
            input_ids=input_ids,
            output_ids=[o.id for o in raw_outputs],
            cost_estimate=cost.model_dump(),
            actual_cost=None,
            duration_seconds=duration,
            started_at=started_at,
            finished_at=finished_at,
            namespace=self.config.namespace,
        )

        # Spend ledger: one row per *actual* execution (cache hits returned
        # above and never reach here). Actual cents/tokens come from
        # backend-reported usage on the outputs when available.
        act_cents, tok_in, tok_out = _actual_usage(raw_outputs)
        self.cache.record_cost(
            op_name=op_name,
            backend_name=backend_name,
            estimated_cents=cost.cloud_cents,
            actual_cents=act_cents,
            tokens_in=tok_in,
            tokens_out=tok_out,
            duration_seconds=duration,
            namespace=self.config.namespace,
            ts=finished_at,
        )

        self.event_bus.emit(
            OpCompleted(
                event_id=uuid4().hex,
                op_run_id=job_id,
                job_id=job_id,
                timestamp=finished_at,
                outputs=[o.id for o in raw_outputs],
                duration_seconds=duration,
                cost={
                    "estimated_cents": cost.cloud_cents,
                    "actual_cents": act_cents,
                },
            )
        )

        final_outputs: list[AnyArtifact] = []
        for o in raw_outputs:
            stamped = o.model_copy(update={"produced_by": run_id})
            self.cache.upsert_artifact(stamped)
            final_outputs.append(stamped)
        return final_outputs

    def cost_summary(
        self,
        *,
        since: datetime | None = None,
        op_name: str | None = None,
    ) -> CostSummary:
        """Per-op spend rollup over the cost ledger (this namespace)."""
        return CostTracker(self.cache).summary(
            since=since, op_name=op_name, namespace=self.config.namespace
        )

    def cost_log_entries(
        self,
        *,
        since: datetime | None = None,
        op_name: str | None = None,
        limit: int | None = None,
    ) -> list[CostLogEntry]:
        """Recent ledger rows (newest first) for this namespace."""
        return CostTracker(self.cache).entries(
            since=since, op_name=op_name,
            namespace=self.config.namespace, limit=limit,
        )

    def event_log_entries(
        self,
        *,
        since: datetime | None = None,
        op_run_id: str | None = None,
        limit: int | None = None,
    ) -> list[EventLogEntry]:
        """Persisted events (newest first) for this namespace."""
        return self.cache.event_log(
            since=since, op_run_id=op_run_id,
            namespace=self.config.namespace, limit=limit,
        )

    def estimate_pipeline_cost(self, pipeline: Pipeline) -> CostEstimate:
        """Sum ``op.cost_estimate`` across a DAG without running it.

        Walks nodes in dependency order. A node whose result is already
        cached contributes zero. Inputs that come from a not-yet-run
        upstream node are unknown at estimate time, so that node is priced
        with empty inputs (ops fall back to a conservative default) — the
        total is a preview, not a guarantee.
        """
        from media_engine.runtime.dag import validate_and_sort

        total = CostEstimate()
        src_ids = {name: a.id for name, a in pipeline.sources.items()}
        for wave in validate_and_sort(pipeline):
            for node in wave:
                op_class = OpRegistry.get(node.op_name)
                op = op_class()
                params_model = op_class.params_model(**node.params)
                input_ids: list[str] = []
                resolvable = True
                for ref in node.input_node_ids:
                    if ref in src_ids:
                        input_ids.append(src_ids[ref])
                    else:
                        # Upstream node output — id unknown pre-run.
                        resolvable = False
                resolved = (
                    self._resolve_inputs(input_ids) if resolvable else []
                )
                params_hash = canonical_params_hash(params_model)
                backend_name, backend_version = self._resolve_backend(
                    op_class, node.backend
                )
                cached = None
                if resolvable:
                    cached = self.cache.find_cached_run(
                        op_name=node.op_name,
                        op_version=op_class.version,
                        backend_name=backend_name,
                        backend_version=backend_version,
                        params_hash=params_hash,
                        input_ids=input_ids,
                        namespace=self.config.namespace,
                    )
                if cached is not None:
                    continue  # cache hit → zero cost
                est = op.cost_estimate(resolved, params_model)
                total = CostEstimate(
                    local_seconds=total.local_seconds + est.local_seconds,
                    cloud_cents=total.cloud_cents + est.cloud_cents,
                    tokens_in=total.tokens_in + est.tokens_in,
                    tokens_out=total.tokens_out + est.tokens_out,
                )
        return total

    def estimate_op_cost(
        self,
        op_name: str,
        *,
        inputs: list[str] | None = None,
        **params: Any,
    ) -> CostEstimate:
        """Predict the cost of a single op without running it."""
        op_class = OpRegistry.get(op_name)
        op = op_class()
        resolved_inputs = self._resolve_inputs(list(inputs or []))
        params_model = op_class.params_model(**params)
        return op.cost_estimate(resolved_inputs, params_model)

    async def run_pipeline(self, pipeline: Pipeline) -> DAGResult:
        """Run a Pipeline through the async DAG executor.

        Source artifacts come from ``pipeline.sources``. The executor enforces
        per-op ``declared_resources`` via the engine's shared semaphore pool
        (one async lock per resource name). Per-node retry policy comes from
        ``DAGNode.retry_policy`` (or the executor's heuristic default).

        Returns a ``DAGResult`` with successes + failures (partial completion).
        Raises only if the graph itself is invalid (cycle / unresolved ref).
        """
        # Persist sources so the inner Engine.run dispatches can resolve them
        # by id. ``upsert_artifact`` is idempotent on (id, namespace).
        for artifact in pipeline.sources.values():
            self.cache.upsert_artifact(artifact)
        return await execute_pipeline(
            pipeline,
            run_op=self.run,
            semaphores=self._get_semaphores(),
        )

    # ── Internals ──

    def _resolve_inputs(self, input_ids: list[str]) -> list[AnyArtifact]:
        out: list[AnyArtifact] = []
        for aid in input_ids:
            a = self.cache.get_artifact(aid, namespace=self.config.namespace)
            if a is None:
                raise LookupError(f"input artifact not found: {aid}")
            out.append(a)
        return out

    @staticmethod
    def _validate_input_kinds(
        op_name: str,
        expected: tuple[Kind, ...],
        resolved: list[AnyArtifact],
        *,
        variadic: bool = False,
    ) -> None:
        if not expected:
            if resolved:
                raise ValueError(
                    f"{op_name} expects no inputs, got {len(resolved)}"
                )
            return
        actual = tuple(a.kind for a in resolved)
        if variadic:
            # One-or-more inputs, each of any declared kind. The op
            # enforces its own minimum arity in run().
            if not actual:
                raise ValueError(
                    f"{op_name} expects ≥1 input ({list(expected)!r}), got 0"
                )
            allowed = set(expected)
            bad = [k for k in actual if k not in allowed]
            if bad:
                raise ValueError(
                    f"{op_name} input kind mismatch: each input must be one "
                    f"of {list(expected)!r}, got {list(actual)!r}"
                )
            return
        if len(actual) != len(expected):
            raise ValueError(
                f"{op_name} expects {len(expected)} input(s) "
                f"({list(expected)!r}), got {len(actual)} ({list(actual)!r})"
            )
        for got, want in zip(actual, expected, strict=True):
            if got is not want:
                raise ValueError(
                    f"{op_name} input kind mismatch: expected {expected}, got {actual}"
                )

    @staticmethod
    def _resolve_backend(
        op_class: type[Operation], requested: str | None
    ) -> tuple[str | None, str | None]:
        op_name = op_class.name
        registered = BackendRegistry.for_op(op_name)
        default = op_class.default_backend
        if not registered and default is None:
            # No backend layer for this op (logic embedded in Operation).
            return (None, None)
        chosen: str | None = requested or default
        if chosen is None:
            raise ValueError(
                f"{op_name} requires a backend; available: {registered}"
            )
        backend_class = BackendRegistry.get(op_name, chosen)
        return (chosen, backend_class.version)

    # ── Lifecycle ──

    def close(self) -> None:
        self.cache.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
