"""Engine.preview_pipeline — per-node cost + pre-run feasibility (Phase 8).

The headline fix: fps × duration > max_frames surfaces in the preflight,
BEFORE the pipeline runs — not in the Job-failed view afterwards.
"""

from __future__ import annotations

from pathlib import Path

from media_engine.ops import OperationContext
from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
from media_engine.runtime.dag import DAGNode, Pipeline
from media_engine.runtime.engine import Engine


def _ctx(engine: Engine) -> OperationContext:
    return OperationContext(
        workdir=engine.storage.ensure_workdir("pp"),
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
    )


async def _ingest(engine: Engine, path: Path):
    op = AcquireUpload()
    [v] = await op.run([], AcquireUploadParams(source_path=path), _ctx(engine))
    engine.cache.upsert_artifact(v)
    return v


async def test_preview_surfaces_frame_budget_before_run(
    engine: Engine, sample_mp4: Path
) -> None:
    v = await _ingest(engine, sample_mp4)
    # fps=8 over any real video vastly exceeds max_frames=1.
    pipeline = Pipeline(
        name="t",
        sources={"source": v},
        nodes=[
            DAGNode(
                id="result",
                op_name="video.comprehend",
                params={"fps": 8.0, "max_frames": 1},
                input_node_ids=["source"],
            )
        ],
    )
    [p] = engine.preview_pipeline(pipeline)
    assert p.feasibility_error is not None
    assert "max_frames" in p.feasibility_error
    assert p.embedded is True  # composite — no single backend
    assert p.resolvable is True


async def test_preview_within_budget_has_no_feasibility_error(
    engine: Engine, sample_mp4: Path
) -> None:
    v = await _ingest(engine, sample_mp4)
    pipeline = Pipeline(
        name="t",
        sources={"source": v},
        nodes=[
            DAGNode(
                id="result",
                op_name="video.comprehend",
                params={"fps": 0.1, "max_frames": 2000},
                input_node_ids=["source"],
            )
        ],
    )
    [p] = engine.preview_pipeline(pipeline)
    assert p.feasibility_error is None


async def test_preview_marks_downstream_node_not_resolvable(
    engine: Engine, sample_mp4: Path
) -> None:
    v = await _ingest(engine, sample_mp4)
    pipeline = Pipeline(
        name="t",
        sources={"source": v},
        nodes=[
            DAGNode(
                id="audio",
                op_name="video.extract_audio",
                params={},
                input_node_ids=["source"],
            ),
            DAGNode(
                id="transcript",
                op_name="audio.transcribe",
                params={},
                input_node_ids=["audio"],  # upstream output — unknown id pre-run
            ),
        ],
    )
    by_id = {p.id: p for p in engine.preview_pipeline(pipeline)}
    # source-fed node is resolvable; the downstream node is not preflighted.
    assert by_id["audio"].resolvable is True
    assert by_id["transcript"].resolvable is False
    assert by_id["transcript"].feasibility_error is None


async def test_preview_surfaces_backend_model_conflict(
    engine: Engine, sample_mp4: Path
) -> None:
    # frames.analyze default model=gemini-2.5-pro + explicit backend=vllm-mlx is
    # internally inconsistent (B-008) — the preflight catches it as a node error.
    v = await _ingest(engine, sample_mp4)
    pipeline = Pipeline(
        name="t",
        sources={"source": v},
        nodes=[
            DAGNode(
                id="frames",
                op_name="video.sample_frames",
                params={"fps": 1.0},
                input_node_ids=["source"],
            ),
            DAGNode(
                id="analyze",
                op_name="frames.analyze",
                params={"prompt": "x", "model": "gemini-2.5-pro"},
                backend="vllm-mlx",
                input_node_ids=["frames"],
            ),
        ],
    )
    by_id = {p.id: p for p in engine.preview_pipeline(pipeline)}
    assert by_id["analyze"].feasibility_error is not None
    assert "incompatible" in by_id["analyze"].feasibility_error
