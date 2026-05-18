"""Async TaskGroup DAG executor.

A ``Pipeline`` is a list of typed nodes (each one an op invocation) plus a
``sources`` map naming the artifacts the caller is feeding in. The executor:

1. Topologically sorts the nodes; rejects cycles eagerly with a clear path.
2. Runs ready waves concurrently via ``asyncio.TaskGroup``.
3. Honors per-node ``declared_resources`` by acquiring shared semaphores
   from the engine before invoking the op.
4. Retries each node per ``RetryPolicy`` (cloud → 3 attempts default;
   local → 1).
5. Cascades failures: when a node fails, its descendants are marked
   ``FailedDependency`` and skipped — independent siblings keep running.

The result is a ``DAGResult`` carrying outputs for every successful node
plus failure info for the rest (partial-completion semantics).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from media_engine.artifacts import AnyArtifact
from media_engine.ops import OpRegistry
from media_engine.runtime.retry import CLOUD_DEFAULT, LOCAL_DEFAULT, RetryPolicy, with_retry

# ─────────────────────────────────────────────────────────────────
# Pipeline shape
# ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DAGNode:
    """One op invocation inside a Pipeline.

    ``input_node_ids`` maps an op-input position (string key, opaque to
    the executor) to either a node id (the executor wires the producing
    node's outputs in) or a source name from ``Pipeline.sources``.
    ``depends_on`` lists additional explicit dependencies (rare; usually
    derivable from ``input_node_ids``).
    """

    id: str
    op_name: str
    params: dict[str, Any] = field(default_factory=lambda: {})
    backend: str | None = None
    input_node_ids: list[str] = field(default_factory=lambda: [])
    depends_on: list[str] = field(default_factory=lambda: [])
    retry_policy: RetryPolicy | None = None  # None → policy_for_op default


@dataclass(frozen=True)
class Pipeline:
    name: str
    sources: dict[str, AnyArtifact]
    nodes: list[DAGNode]
    outputs: list[str] = field(default_factory=lambda: [])  # node ids; empty = all


# ─────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────


@dataclass
class NodeSuccess:
    node_id: str
    artifacts: list[AnyArtifact]


@dataclass
class NodeFailure:
    node_id: str
    error_class: str
    message: str
    failed_dependency: bool = False  # True when skipped because an upstream failed


@dataclass
class DAGResult:
    successes: dict[str, NodeSuccess]
    failures: dict[str, NodeFailure]

    @property
    def all_succeeded(self) -> bool:
        return not self.failures

    def outputs_for(self, node_id: str) -> list[AnyArtifact]:
        if node_id in self.successes:
            return self.successes[node_id].artifacts
        return []


# ─────────────────────────────────────────────────────────────────
# Topological sort
# ─────────────────────────────────────────────────────────────────


class CycleError(RuntimeError):
    """Raised when the pipeline graph has a cycle. Carries the cycle path."""


def validate_and_sort(pipeline: Pipeline) -> list[list[DAGNode]]:
    """Return ready-waves: each wave is a list of nodes whose deps are all
    satisfied by the previous wave's completion."""
    by_id: dict[str, DAGNode] = {n.id: n for n in pipeline.nodes}
    if len(by_id) != len(pipeline.nodes):
        seen: set[str] = set()
        for n in pipeline.nodes:
            if n.id in seen:
                raise ValueError(f"duplicate node id: {n.id!r}")
            seen.add(n.id)
    sources = set(pipeline.sources.keys())

    deps: dict[str, set[str]] = {}
    for node in pipeline.nodes:
        node_deps: set[str] = set()
        for ref in (*node.input_node_ids, *node.depends_on):
            if ref in by_id:
                node_deps.add(ref)
            elif ref in sources:
                continue
            else:
                raise ValueError(
                    f"node {node.id!r} references unknown input/dep {ref!r}"
                )
        deps[node.id] = node_deps

    # Kahn topo sort with ready-wave grouping.
    waves: list[list[DAGNode]] = []
    remaining = dict(deps)
    completed: set[str] = set()
    while remaining:
        ready = [
            by_id[node_id]
            for node_id, d in remaining.items()
            if d.issubset(completed)
        ]
        if not ready:
            raise CycleError(
                f"cycle in pipeline graph; remaining nodes: {sorted(remaining)}"
            )
        # Stable ordering across runs.
        ready.sort(key=lambda n: n.id)
        waves.append(ready)
        for n in ready:
            completed.add(n.id)
            remaining.pop(n.id)
    return waves


# ─────────────────────────────────────────────────────────────────
# Resource semaphores
# ─────────────────────────────────────────────────────────────────


DEFAULT_RESOURCE_CAPACITIES: dict[str, int] = {
    "apple_neural_engine": 1,
    "apple_gpu": 1,
    "cloud_concurrent": 8,
}


def make_default_semaphores() -> dict[str, asyncio.Semaphore]:
    """Build the engine's default semaphore pool. Phase 5's resources.yaml
    overrides these capacities; Phase 1/2 use the defaults."""
    return {
        name: asyncio.Semaphore(cap)
        for name, cap in DEFAULT_RESOURCE_CAPACITIES.items()
    }


@contextlib.asynccontextmanager
async def _acquire_all(
    semaphores: dict[str, asyncio.Semaphore], names: tuple[str, ...]
):
    """Acquire each named semaphore in lexicographic order (deadlock-safe).

    Falls through silently for resources that aren't in the pool — declaring
    a resource the engine doesn't know about is allowed, just unenforced.
    """
    if not names:
        yield
        return
    held: list[asyncio.Semaphore] = []
    try:
        for name in sorted(names):
            sem = semaphores.get(name)
            if sem is None:
                continue
            await sem.acquire()
            held.append(sem)
        yield
    finally:
        for sem in held:
            sem.release()


# ─────────────────────────────────────────────────────────────────
# Executor
# ─────────────────────────────────────────────────────────────────


def _policy_for_op(node: DAGNode) -> RetryPolicy:
    if node.retry_policy is not None:
        return node.retry_policy
    op_class = OpRegistry.get(node.op_name)
    backend_name = node.backend or op_class.default_backend or ""
    # A backend may declare its own policy — that wins over the heuristic.
    if backend_name:
        from media_engine.backends import BackendRegistry

        if BackendRegistry.has(node.op_name, backend_name):
            declared = BackendRegistry.get(
                node.op_name, backend_name
            ).retry_policy
            if declared is not None:
                return declared
    # Heuristic: ops with a backend that smells cloud → retry; else local.
    if any(
        tag in backend_name
        for tag in ("openai", "gemini", "claude", "anthropic")
    ):
        return CLOUD_DEFAULT
    return LOCAL_DEFAULT


async def execute_pipeline(
    pipeline: Pipeline,
    *,
    run_op: Callable[..., Awaitable[list[AnyArtifact]]],
    semaphores: dict[str, asyncio.Semaphore] | None = None,
) -> DAGResult:
    """Execute the pipeline; return per-node outcomes (partial completion)."""
    waves = validate_and_sort(pipeline)
    semaphores = semaphores or {}

    successes: dict[str, NodeSuccess] = {}
    failures: dict[str, NodeFailure] = {}

    # node_id → op input artifact ids resolved before run.
    def _resolve_inputs(node: DAGNode) -> tuple[list[str] | None, str | None]:
        ids: list[str] = []
        for ref in node.input_node_ids:
            if ref in pipeline.sources:
                ids.append(pipeline.sources[ref].id)
            elif ref in successes:
                ids.extend(a.id for a in successes[ref].artifacts)
            elif ref in failures:
                return None, f"upstream node {ref!r} failed"
            else:
                return None, f"unresolved input {ref!r}"
        # Explicit depends_on still cascades failures even though it doesn't
        # contribute to inputs.
        for dep in node.depends_on:
            if dep in failures:
                return None, f"upstream node {dep!r} failed"
        return ids, None

    for wave in waves:
        async with asyncio.TaskGroup() as tg:
            for node in wave:
                resolved, err = _resolve_inputs(node)
                if err is not None:
                    failures[node.id] = NodeFailure(
                        node_id=node.id,
                        error_class="FailedDependency",
                        message=err,
                        failed_dependency=True,
                    )
                    continue
                tg.create_task(_run_node(
                    node=node,
                    input_ids=resolved or [],
                    run_op=run_op,
                    semaphores=semaphores,
                    successes=successes,
                    failures=failures,
                ))

    return DAGResult(successes=successes, failures=failures)


async def _run_node(
    *,
    node: DAGNode,
    input_ids: list[str],
    run_op: Callable[..., Awaitable[list[AnyArtifact]]],
    semaphores: dict[str, asyncio.Semaphore],
    successes: dict[str, NodeSuccess],
    failures: dict[str, NodeFailure],
) -> None:
    op_class = OpRegistry.get(node.op_name)
    declared = op_class.declared_resources
    policy = _policy_for_op(node)

    async def _attempt() -> list[AnyArtifact]:
        async with _acquire_all(semaphores, declared):
            return await run_op(
                node.op_name,
                inputs=input_ids,
                backend=node.backend,
                **node.params,
            )

    try:
        outputs = await with_retry(_attempt, policy=policy)
    except BaseException as e:  # noqa: BLE001
        failures[node.id] = NodeFailure(
            node_id=node.id,
            error_class=type(e).__name__,
            message=str(e),
        )
        return
    successes[node.id] = NodeSuccess(node_id=node.id, artifacts=outputs)
