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

    def select_backend(self, params: BaseModel) -> str | None:
        """Backend this op will use, derived from ``params`` (e.g. by model
        prefix). The engine consults this when no explicit ``backend=`` was
        given, so the resolved backend matches what ``run`` dispatches.
        Default ``None`` → engine falls back to ``default_backend``."""
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
