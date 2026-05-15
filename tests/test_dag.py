"""Tests for runtime/dag.py — async TaskGroup DAG executor.

Covers:
- topological sort + cycle detection
- linear / diamond pipelines run end-to-end via Engine.run_pipeline
- resource semaphore (enforces sequential execution despite parallel graph)
- failure isolation (sibling completes; downstream skipped with FailedDependency)
- per-node retry (flakey op succeeds on attempt N)
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    Audio,
    Video,
    compute_artifact_id,
)
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)
from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
from media_engine.runtime.dag import (
    CycleError,
    DAGNode,
    Pipeline,
    _validate_and_sort,
    make_default_semaphores,
)
from media_engine.runtime.engine import Engine
from media_engine.runtime.retry import RetryPolicy


def _ctx_for(engine: Engine) -> OperationContext:
    return OperationContext(
        workdir=engine.storage.ensure_workdir("dag-test"),
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
        emit=engine.event_bus.emit,
        server_manager=engine.server_manager,
        model_pool=engine.model_pool,
    )


# ─────────────────────────────────────────────────────────────────
# Topological sort
# ─────────────────────────────────────────────────────────────────


def _video_artifact(tmp_path: Path) -> Video:
    f = tmp_path / "fake.mp4"
    f.write_bytes(b"\x00")
    return Video(
        id=compute_artifact_id(f), path=f, created_at=datetime.now(UTC),
    )


def test_topo_sort_linear(tmp_path: Path) -> None:
    pipeline = Pipeline(
        name="t",
        sources={"src": _video_artifact(tmp_path)},
        nodes=[
            DAGNode(id="a", op_name="acquire.upload", input_node_ids=[]),
            DAGNode(id="b", op_name="acquire.upload", input_node_ids=["a"]),
            DAGNode(id="c", op_name="acquire.upload", input_node_ids=["b"]),
        ],
    )
    waves = _validate_and_sort(pipeline)
    assert [n.id for n in waves[0]] == ["a"]
    assert [n.id for n in waves[1]] == ["b"]
    assert [n.id for n in waves[2]] == ["c"]


def test_topo_sort_diamond(tmp_path: Path) -> None:
    pipeline = Pipeline(
        name="d",
        sources={"src": _video_artifact(tmp_path)},
        nodes=[
            DAGNode(id="a", op_name="acquire.upload"),
            DAGNode(id="b", op_name="acquire.upload", input_node_ids=["a"]),
            DAGNode(id="c", op_name="acquire.upload", input_node_ids=["a"]),
            DAGNode(id="d", op_name="acquire.upload", input_node_ids=["b", "c"]),
        ],
    )
    waves = _validate_and_sort(pipeline)
    assert {n.id for n in waves[0]} == {"a"}
    assert {n.id for n in waves[1]} == {"b", "c"}  # parallel
    assert {n.id for n in waves[2]} == {"d"}


def test_topo_sort_cycle_raises(tmp_path: Path) -> None:
    pipeline = Pipeline(
        name="c",
        sources={},
        nodes=[
            DAGNode(id="a", op_name="acquire.upload", input_node_ids=["b"]),
            DAGNode(id="b", op_name="acquire.upload", input_node_ids=["a"]),
        ],
    )
    with pytest.raises(CycleError, match="cycle"):
        _validate_and_sort(pipeline)


def test_topo_sort_unknown_dep_raises(tmp_path: Path) -> None:
    pipeline = Pipeline(
        name="u",
        sources={},
        nodes=[
            DAGNode(id="a", op_name="acquire.upload", input_node_ids=["nope"]),
        ],
    )
    with pytest.raises(ValueError, match="unknown input/dep"):
        _validate_and_sort(pipeline)


def test_topo_sort_duplicate_id_raises(tmp_path: Path) -> None:
    pipeline = Pipeline(
        name="d",
        sources={},
        nodes=[
            DAGNode(id="a", op_name="acquire.upload"),
            DAGNode(id="a", op_name="acquire.upload"),
        ],
    )
    with pytest.raises(ValueError, match="duplicate node id"):
        _validate_and_sort(pipeline)


# ─────────────────────────────────────────────────────────────────
# End-to-end pipelines
# ─────────────────────────────────────────────────────────────────


async def test_linear_pipeline_via_engine(
    engine: Engine, sample_mp4: Path
) -> None:
    """acquire.upload → video.extract_audio."""
    # Start with a Video artifact already known to the engine.
    op = AcquireUpload()
    [video] = await op.run([], AcquireUploadParams(source_path=sample_mp4),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(video)

    pipeline = Pipeline(
        name="lin",
        sources={"src": video},
        nodes=[
            DAGNode(
                id="extract",
                op_name="video.extract_audio",
                input_node_ids=["src"],
            ),
        ],
    )
    result = await engine.run_pipeline(pipeline)
    assert result.all_succeeded
    audios = result.outputs_for("extract")
    assert len(audios) == 1
    assert isinstance(audios[0], Audio)


async def test_diamond_pipeline_runs_in_parallel(
    engine: Engine, sample_mp4: Path
) -> None:
    """video → (extract_audio, extract_audio with diff params) → … in parallel."""
    op = AcquireUpload()
    [video] = await op.run([], AcquireUploadParams(source_path=sample_mp4),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(video)

    pipeline = Pipeline(
        name="diamond",
        sources={"src": video},
        nodes=[
            DAGNode(
                id="a16",
                op_name="video.extract_audio",
                input_node_ids=["src"],
                params={"sample_rate": 16000},
            ),
            DAGNode(
                id="a44",
                op_name="video.extract_audio",
                input_node_ids=["src"],
                params={"sample_rate": 44100},
            ),
        ],
    )
    result = await engine.run_pipeline(pipeline)
    assert result.all_succeeded
    a16 = result.outputs_for("a16")[0]
    a44 = result.outputs_for("a44")[0]
    assert a16.id != a44.id


# ─────────────────────────────────────────────────────────────────
# Resource semaphores
# ─────────────────────────────────────────────────────────────────


class _SlowParams(BaseModel):
    delay: float = 0.1


@register_op
class _SlowOp(Operation):
    """Test-only op that sleeps for ``delay`` seconds. Holds apple_gpu."""

    name = "test.slow_op"
    version = "1.0.0"
    input_kinds = ()
    output_kinds = ()
    params_model = _SlowParams
    declared_resources = ("apple_gpu",)

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, _SlowParams)
        await asyncio.sleep(params.delay)
        return []

    def cost_estimate(self, inputs, params):
        return CostEstimate(local_seconds=0.1)


async def test_resource_semaphore_serializes_parallel_nodes(
    engine: Engine,
) -> None:
    """Two nodes that both declare apple_gpu (capacity 1) should run sequentially
    even though the graph allows parallel."""
    # Differentiate params so cache lookup doesn't fold the second run into
    # a hit — the test is about runtime concurrency, not the cache.
    pipeline = Pipeline(
        name="r",
        sources={},
        nodes=[
            DAGNode(id="a", op_name="test.slow_op", params={"delay": 0.2}),
            DAGNode(id="b", op_name="test.slow_op", params={"delay": 0.21}),
        ],
    )
    start = time.monotonic()
    result = await engine.run_pipeline(pipeline)
    elapsed = time.monotonic() - start
    assert result.all_succeeded
    # Sequential → ~0.41 s. Parallel → ~0.21 s. Allow generous CI slack.
    assert elapsed >= 0.35


def test_default_semaphores_capacities() -> None:
    sems = make_default_semaphores()
    assert "apple_neural_engine" in sems
    assert "apple_gpu" in sems
    assert "cloud_concurrent" in sems


# ─────────────────────────────────────────────────────────────────
# Failure isolation
# ─────────────────────────────────────────────────────────────────


class _BoomParams(BaseModel):
    pass


@register_op
class _BoomOp(Operation):
    """Test-only op that always raises."""

    name = "test.boom"
    version = "1.0.0"
    input_kinds = ()
    output_kinds = ()
    params_model = _BoomParams

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        raise RuntimeError("intentional failure")

    def cost_estimate(self, inputs, params):
        return CostEstimate()


async def test_failure_isolated_sibling_completes(engine: Engine) -> None:
    pipeline = Pipeline(
        name="fail",
        sources={},
        nodes=[
            DAGNode(id="bad", op_name="test.boom"),
            DAGNode(id="good", op_name="test.slow_op", params={"delay": 0.0}),
        ],
    )
    result = await engine.run_pipeline(pipeline)
    assert not result.all_succeeded
    assert "bad" in result.failures
    assert result.failures["bad"].error_class == "RuntimeError"
    assert "good" in result.successes


async def test_failure_cascades_to_dependents_as_failed_dependency(
    engine: Engine,
) -> None:
    pipeline = Pipeline(
        name="cascade",
        sources={},
        nodes=[
            DAGNode(id="bad", op_name="test.boom"),
            DAGNode(id="downstream",
                    op_name="test.slow_op",
                    depends_on=["bad"],
                    params={"delay": 0.0}),
        ],
    )
    result = await engine.run_pipeline(pipeline)
    assert "bad" in result.failures
    assert "downstream" in result.failures
    assert result.failures["downstream"].failed_dependency is True
    assert result.failures["downstream"].error_class == "FailedDependency"


# ─────────────────────────────────────────────────────────────────
# Retry policy
# ─────────────────────────────────────────────────────────────────


_FLAKEY_ATTEMPTS: dict[str, int] = {"count": 0}


class _FlakeyParams(BaseModel):
    succeed_on: int = 2  # 1-indexed attempt that finally succeeds


@register_op
class _FlakeyOp(Operation):
    """Test-only op that fails the first ``succeed_on - 1`` attempts."""

    name = "test.flakey"
    version = "1.0.0"
    input_kinds = ()
    output_kinds = ()
    params_model = _FlakeyParams

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, _FlakeyParams)
        _FLAKEY_ATTEMPTS["count"] += 1
        if _FLAKEY_ATTEMPTS["count"] < params.succeed_on:
            raise RuntimeError(f"transient (attempt {_FLAKEY_ATTEMPTS['count']})")
        return []

    def cost_estimate(self, inputs, params):
        return CostEstimate()


async def test_retry_policy_recovers(engine: Engine) -> None:
    _FLAKEY_ATTEMPTS["count"] = 0
    pipeline = Pipeline(
        name="retry",
        sources={},
        nodes=[
            DAGNode(
                id="f",
                op_name="test.flakey",
                params={"succeed_on": 3},
                retry_policy=RetryPolicy(
                    max_attempts=5, backoff="fixed",
                    initial_delay=0.0, jitter=0.0,
                ),
            ),
        ],
    )
    result = await engine.run_pipeline(pipeline)
    assert result.all_succeeded
    assert _FLAKEY_ATTEMPTS["count"] == 3


async def test_retry_exhausted_marks_failure(engine: Engine) -> None:
    _FLAKEY_ATTEMPTS["count"] = 0
    pipeline = Pipeline(
        name="exhaust",
        sources={},
        nodes=[
            DAGNode(
                id="f",
                op_name="test.flakey",
                params={"succeed_on": 99},
                retry_policy=RetryPolicy(
                    max_attempts=2, backoff="fixed",
                    initial_delay=0.0, jitter=0.0,
                ),
            ),
        ],
    )
    result = await engine.run_pipeline(pipeline)
    assert "f" in result.failures
    assert _FLAKEY_ATTEMPTS["count"] == 2
