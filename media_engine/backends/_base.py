"""Backend ABC + ``BackendRegistry`` + ``BackendRequirements``.

A Backend is a per-op implementation. The cache key includes
``(backend.name, backend.version)`` so swapping backends produces a new
artifact id (a transcript from mlx-whisper is not interchangeable with one
from Gemini).

For ops with one implementation, the Operation embeds logic directly and
skips the Backend layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar, Literal, TypeVar

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact
from media_engine.ops import CostEstimate, OperationContext

if TYPE_CHECKING:
    pass


class BackendRequirements(BaseModel):
    """Declared dependencies for a backend.

    Used by health/readiness checks and at backend selection time. The DAG
    executor surfaces actionable errors when requirements are missing
    (e.g. ``HF_TOKEN unset for backend pyannote``).
    """

    env: list[str] = []
    binaries: list[str] = []
    services: list[str] = []
    hardware: list[str] = []
    min_memory_gb: float = 0.0


class Backend(ABC):
    """Backend contract."""

    op_name: ClassVar[str]
    name: ClassVar[str]
    version: ClassVar[str]
    requires: ClassVar[BackendRequirements] = BackendRequirements()

    @abstractmethod
    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]: ...

    @abstractmethod
    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate: ...

    @classmethod
    def health(cls) -> Literal["ok", "degraded", "unavailable"]:
        """Default: always ``ok``. Subclasses override to check env vars,
        binaries, services, hardware."""
        return "ok"


class BackendRegistry:
    """Registry indexed by ``(op_name, backend_name) → Backend class``."""

    _backends: dict[tuple[str, str], type[Backend]] = {}

    @classmethod
    def register(cls, backend_class: type[Backend]) -> type[Backend]:
        key = (backend_class.op_name, backend_class.name)
        if key in cls._backends and cls._backends[key] is not backend_class:
            raise ValueError(
                f"Backend {backend_class.name!r} for op {backend_class.op_name!r} "
                f"already registered as {cls._backends[key].__qualname__}"
            )
        cls._backends[key] = backend_class
        return backend_class

    @classmethod
    def get(cls, op_name: str, backend_name: str) -> type[Backend]:
        try:
            return cls._backends[(op_name, backend_name)]
        except KeyError as e:
            available = ", ".join(sorted(cls.for_op(op_name))) or "(none)"
            raise LookupError(
                f"No backend {backend_name!r} for op {op_name!r}. "
                f"Available: {available}"
            ) from e

    @classmethod
    def for_op(cls, op_name: str) -> list[str]:
        return sorted(name for (op, name) in cls._backends if op == op_name)

    @classmethod
    def list_all(cls) -> list[type[Backend]]:
        return sorted(
            cls._backends.values(), key=lambda b: (b.op_name, b.name)
        )

    @classmethod
    def has(cls, op_name: str, backend_name: str) -> bool:
        return (op_name, backend_name) in cls._backends

    @classmethod
    def clear(cls) -> None:
        """For tests only — wipe the registry."""
        cls._backends.clear()


T = TypeVar("T", bound=type[Backend])


def register_backend(backend_class: T) -> T:
    """Decorator: register a Backend subclass."""
    BackendRegistry.register(backend_class)
    return backend_class
