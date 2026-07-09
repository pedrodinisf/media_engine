"""Operation ABC + ``OperationContext`` + ``CostEstimate``.

An ``Operation`` is a pure-ish typed function: given typed input artifacts and
a Pydantic params model, produce typed output artifacts. Multiple
implementations of the same op live as ``Backend`` subclasses (added when ≥2
impls exist; ops with one impl skip the Backend layer entirely).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact, Kind

if TYPE_CHECKING:
    from media_engine.config import EngineConfig
    from media_engine.runtime.events import Event
    from media_engine.runtime.storage import StorageBackend


class CostEstimate(BaseModel):
    """Predicted (or actual) resource cost of an operation."""

    local_seconds: float = 0.0
    cloud_cents: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0


def _no_op_emit(_: Event) -> None:  # pragma: no cover (default sink)
    pass


@dataclass
class OperationContext:
    """Per-run handle passed to ``Operation.run``.

    Resource serialization is **not** on this context by design:

    * Resource locks (``declared_resources``) are acquired by the DAG
      executor *around* the whole op invocation — ops stay declarative and
      never touch a semaphore. (``runtime.dag._acquire_all``.)

    ``backend`` is the backend name the engine resolved for this run
    (explicit ``backend=`` wins, else ``Operation.select_backend(params)``,
    else ``default_backend``). It is the single source of truth: an op with
    a Backend layer dispatches with ``BackendRegistry.get(self.name,
    ctx.backend)`` so the backend that actually runs is exactly the one the
    cache key + cost ledger + provenance record. ``None`` for ops with no
    Backend layer.

    ``run_op`` is the recursion handle injected by ``Engine.run``: composite
    ops call ``await ctx.run_op("audio.transcribe", inputs=[...])`` to invoke
    a sibling op through the same cache + dispatch layer they themselves are
    running under. ``None`` outside of an Engine.run invocation.
    """

    workdir: Path
    config: EngineConfig
    storage: StorageBackend
    namespace: str = "default"
    emit: Callable[[Event], None] = field(default=_no_op_emit)
    server_manager: Any | None = None
    model_pool: Any | None = None
    run_op: Callable[..., Any] | None = None
    backend: str | None = None
    # Read-only handle to the engine's cache — used by ops that need to
    # enumerate artifacts across runs (e.g. ``search.*`` building an
    # index over every persisted Transcript / Embedding / Document).
    # ``None`` outside of ``Engine.run``.
    cache: Any | None = None
    # The REST/CLI submission id this op is running under. Backends
    # forward it onto every event they emit (Progress, LogLine) so SSE
    # per-job replay can ``WHERE job_id = ?`` and surface them on the
    # Job-detail page. ``None`` outside of ``Engine.run``; the engine
    # falls back to ``op_run_id`` when no explicit submission id was
    # supplied (matches the engine's own event_job_id convention).
    job_id: str | None = None
    # The op-run id the engine assigned for this invocation. Mirrors the
    # `op_run_id` on every Event the engine emits around the op, so
    # backend-emitted events stay correlated.
    op_run_id: str | None = None


class Operation(ABC):
    """Operation contract.

    Subclass attributes:
      * ``name`` — ``<group>.<verb>`` (lowercase). Capability-named, never
        technology-named.
      * ``version`` — semver. Bump invalidates cached results.
      * ``input_kinds`` — tuple of Kind required as inputs (in order).
        For a ``variadic_inputs`` op this is instead the *set* of kinds
        each input may be (order/count not fixed).
      * ``variadic_inputs`` — when True the op takes one-or-more inputs,
        each of which must be one of ``input_kinds`` (the engine skips the
        positional length/order check; the op enforces its own arity in
        ``run``). Used by fan-in ops like ``frames.compare``.
      * ``output_kinds`` — tuple of Kind produced as outputs.
      * ``params_model`` — Pydantic model class describing op params.
      * ``declared_resources`` — names of resources this op needs serialized
        access to (e.g. ``("apple_neural_engine",)``). The DAG executor
        enforces these via per-resource semaphores.
      * ``default_backend`` — name of backend to pick when caller doesn't
        specify. ``None`` for ops with embedded logic (no Backend layer).
      * ``records_cost`` — whether ``Engine.run`` writes a ``cost_log``
        ledger row for this op. ``False`` for thin composite wrappers that
        delegate to a sub-op via ``ctx.run_op`` (the sub-op already billed
        the spend; billing the wrapper too would double-count).
      * ``delegates_to`` — names of ops this op calls via ``ctx.run_op``
        (or equivalent). Static declaration; the doctor + Settings UI
        walk this to compute "if I install/set X, which composites
        light up too?" without having to inspect ``run`` bodies. Empty
        for non-composites and for composites that don't route through
        another registered op (e.g. acquire.upload, video.trim).
    """

    name: ClassVar[str]
    version: ClassVar[str]
    input_kinds: ClassVar[tuple[Kind, ...]]
    variadic_inputs: ClassVar[bool] = False
    output_kinds: ClassVar[tuple[Kind, ...]]
    params_model: ClassVar[type[BaseModel]]
    declared_resources: ClassVar[tuple[str, ...]] = ()
    default_backend: ClassVar[str | None] = None
    records_cost: ClassVar[bool] = True
    delegates_to: ClassVar[tuple[str, ...]] = ()

    def select_backend(self, params: BaseModel) -> str | None:
        """Backend this op will use, derived from ``params`` (e.g. by model
        prefix). The engine consults this when no explicit ``backend=`` was
        given, so the resolved backend matches what ``run`` dispatches.
        Default ``None`` → engine falls back to ``default_backend``."""
        return None

    def validate_params(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> None:
        """Optional pre-run feasibility check on a resolved (inputs, params) pair.

        Raise ``ValueError`` with a human-actionable message when this
        combination cannot succeed — e.g. a frame budget that would fan out
        thousands of calls, or a required input file that doesn't exist.
        Default: no-op.

        Unlike ``run``, this must do **no** work and mutate nothing: it's
        called speculatively by the pipeline preflight
        (``Engine.preview_pipeline`` → ``POST /pipelines/preview``),
        ``/run/preview``, and ``--dry-run`` to surface the error at configure
        time, and again as a backstop at the top of ``run``. It only runs when
        the op's inputs are fully resolvable (source-fed nodes); keep checks
        host-independent so they're valid in an API/preview process that may
        differ from the worker that ultimately runs ``run``."""
        return None

    @abstractmethod
    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]: ...

    @abstractmethod
    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate: ...
