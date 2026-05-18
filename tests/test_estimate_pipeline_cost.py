"""Engine.estimate_pipeline_cost — DAG cost preview."""

from __future__ import annotations

from pathlib import Path

from media_engine.ops import OperationContext
from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
from media_engine.runtime.dag import DAGNode, Pipeline
from media_engine.runtime.engine import Engine


def _ctx(engine: Engine) -> OperationContext:
    return OperationContext(
        workdir=engine.storage.ensure_workdir("ep"),
        config=engine.config, storage=engine.storage,
        namespace=engine.config.namespace,
    )


async def test_estimate_sums_nodes(
    engine: Engine, sample_mp4: Path
) -> None:
    op = AcquireUpload()
    [v] = await op.run([], AcquireUploadParams(source_path=sample_mp4),
                       _ctx(engine))
    engine.cache.upsert_artifact(v)

    pipeline = Pipeline(
        name="t",
        sources={"vid": v},
        nodes=[
            DAGNode(
                id="frames",
                op_name="video.sample_frames",
                params={"fps": 2.0},
                input_node_ids=["vid"],
            ),
            DAGNode(
                id="sub",
                op_name="frames.subsample",
                params={"max_n": 3},
                input_node_ids=["frames"],
            ),
        ],
    )
    est = engine.estimate_pipeline_cost(pipeline)
    # Both nodes are local → local_seconds accrues, no cloud spend.
    assert est.local_seconds > 0
    assert est.cloud_cents == 0


async def test_cache_hit_is_free(
    engine: Engine, sample_mp4: Path
) -> None:
    op = AcquireUpload()
    [v] = await op.run([], AcquireUploadParams(source_path=sample_mp4),
                       _ctx(engine))
    engine.cache.upsert_artifact(v)
    pipeline = Pipeline(
        name="t",
        sources={"vid": v},
        nodes=[
            DAGNode(
                id="frames",
                op_name="video.sample_frames",
                params={"fps": 2.0},
                input_node_ids=["vid"],
            )
        ],
    )
    before = engine.estimate_pipeline_cost(pipeline)
    assert before.local_seconds > 0

    # Materialize the cache entry, then re-estimate → that node is free.
    await engine.run("video.sample_frames", inputs=[v.id], fps=2.0)
    after = engine.estimate_pipeline_cost(pipeline)
    assert after.local_seconds == 0
