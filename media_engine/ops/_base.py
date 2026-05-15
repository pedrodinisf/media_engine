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
    from contextlib import AbstractAsyncContextManager

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

    Phase 0 fills the synchronous fields (workdir, config, namespace, storage,
    emit). The async / resource fields below — semaphore acquisition, backend
    selection, server manager, model pool — become non-trivial in Phase 1+
    when the daemon and DAG executor land.

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
    acquire_resource: Callable[[str], AbstractAsyncContextManager[None]] | None = None
    select_backend: Callable[[str, str | None], Any] | None = None
    server_manager: Any | None = None
    model_pool: Any | None = None
    run_op: Callable[..., Any] | None = None


class Operation(ABC):
    """Operation contract.

    Subclass attributes:
      * ``name`` — ``<group>.<verb>`` (lowercase). Capability-named, never
        technology-named.
      * ``version`` — semver. Bump invalidates cached results.
      * ``input_kinds`` — tuple of Kind required as inputs (in order).
      * ``output_kinds`` — tuple of Kind produced as outputs.
      * ``params_model`` — Pydantic model class describing op params.
      * ``declared_resources`` — names of resources this op needs serialized
        access to (e.g. ``("apple_neural_engine",)``). The DAG executor
        enforces these via per-resource semaphores.
      * ``default_backend`` — name of backend to pick when caller doesn't
        specify. ``None`` for ops with embedded logic (no Backend layer).
    """

    name: ClassVar[str]
    version: ClassVar[str]
    input_kinds: ClassVar[tuple[Kind, ...]]
    output_kinds: ClassVar[tuple[Kind, ...]]
    params_model: ClassVar[type[BaseModel]]
    declared_resources: ClassVar[tuple[str, ...]] = ()
    default_backend: ClassVar[str | None] = None

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
