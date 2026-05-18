"""Tests for ops/frames/compare.py + the Gemini multi-input backend.

Dispatch tests use a fake backend (always run). Real Gemini smoke is
`needs_gemini`-gated.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pydantic import BaseModel

from media_engine.artifacts import Analysis, AnyArtifact, FrameSet, Kind
from media_engine.backends import (
    Backend,
    BackendRegistry,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
from media_engine.ops.frames.compare import (
    FramesCompare,
    FramesCompareParams,
    build_compare_analysis,
)
from media_engine.runtime.engine import Engine


def _genai_available() -> bool:
    try:
        return importlib.util.find_spec("google.genai") is not None
    except ModuleNotFoundError:
        return False


GENAI_AVAILABLE = _genai_available()


def _ctx_for(engine: Engine) -> OperationContext:
    return OperationContext(
        workdir=engine.storage.ensure_workdir("fc-test"),
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
        emit=engine.event_bus.emit,
        server_manager=engine.server_manager,
        model_pool=engine.model_pool,
    )


def test_op_class_attributes() -> None:
    assert FramesCompare.name == "frames.compare"
    assert FramesCompare.input_kinds == (Kind.FrameSet, Kind.Image)
    assert FramesCompare.variadic_inputs is True
    assert FramesCompare.output_kinds == (Kind.Analysis,)
    assert FramesCompare.default_backend == "gemini"


def test_params_defaults() -> None:
    p = FramesCompareParams()
    assert "differences" in p.prompt
    assert p.model == "gemini-2.5-pro"


@pytest.fixture
def fake_fc_backend() -> type[Backend]:
    BackendRegistry.unregister("frames.compare", "gemini")

    @register_backend
    class _Fake(Backend):
        op_name = "frames.compare"
        name = "gemini"
        version = "0.0.0-fake"
        requires = BackendRequirements()

        async def execute(
            self,
            inputs: list[AnyArtifact],
            params: BaseModel,
            ctx: OperationContext,
        ) -> list[AnyArtifact]:
            assert isinstance(params, FramesCompareParams)
            return [
                build_compare_analysis(
                    inputs=inputs,
                    params=params,
                    backend_name=self.name,
                    backend_version=self.version,
                    workdir_path=ctx.workdir,
                    storage=ctx.storage,
                    text=f"Compared {len(inputs)} inputs.",
                    usage={"input_tokens": 1600, "output_tokens": 80,
                           "cost_cents": 0.2},
                )
            ]

        def cost_estimate(self, inputs, params):
            return CostEstimate(cloud_cents=0.6)

    yield _Fake
    BackendRegistry.unregister("frames.compare", "gemini")
    from media_engine.bootstrap import register_all
    register_all(force=True)


async def _two_framesets(
    engine: Engine, sample_mp4: Path
) -> tuple[FrameSet, FrameSet]:
    [video] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_mp4), _ctx_for(engine)
    )
    engine.cache.upsert_artifact(video)
    [fs_a] = await engine.run("video.sample_frames", inputs=[video.id], fps=1.0)
    [fs_b] = await engine.run("video.sample_frames", inputs=[video.id], fps=4.0)
    assert isinstance(fs_a, FrameSet) and isinstance(fs_b, FrameSet)
    assert fs_a.id != fs_b.id
    return fs_a, fs_b


async def test_compare_via_fake_backend(
    engine: Engine, sample_mp4: Path, fake_fc_backend
) -> None:
    fs_a, fs_b = await _two_framesets(engine, sample_mp4)
    [analysis] = await engine.run(
        "frames.compare", inputs=[fs_a.id, fs_b.id], prompt="what changed?"
    )
    assert isinstance(analysis, Analysis)
    assert analysis.derived_from == (fs_a.id, fs_b.id)
    assert analysis.metadata["compared"] == [fs_a.id, fs_b.id]
    assert "Compared 2 inputs" in analysis.data["text"]


async def test_compare_cache_hit(
    engine: Engine, sample_mp4: Path, fake_fc_backend, mocker
) -> None:
    fs_a, fs_b = await _two_framesets(engine, sample_mp4)
    [a1] = await engine.run("frames.compare", inputs=[fs_a.id, fs_b.id])
    spy = mocker.spy(fake_fc_backend, "execute")
    [a2] = await engine.run("frames.compare", inputs=[fs_a.id, fs_b.id])
    assert spy.call_count == 0
    assert a1.id == a2.id


async def test_compare_param_change_new_id(
    engine: Engine, sample_mp4: Path, fake_fc_backend
) -> None:
    fs_a, fs_b = await _two_framesets(engine, sample_mp4)
    [a] = await engine.run(
        "frames.compare", inputs=[fs_a.id, fs_b.id], prompt="one"
    )
    [b] = await engine.run(
        "frames.compare", inputs=[fs_a.id, fs_b.id], prompt="two"
    )
    assert a.id != b.id


async def test_compare_rejects_single_input(
    engine: Engine, sample_mp4: Path, fake_fc_backend
) -> None:
    fs_a, _ = await _two_framesets(engine, sample_mp4)
    with pytest.raises(ValueError, match="needs ≥2 inputs"):
        await engine.run("frames.compare", inputs=[fs_a.id])


def test_cost_estimate_sums_frames(engine: Engine) -> None:
    from datetime import UTC, datetime

    op = FramesCompare()
    fs_a = FrameSet(
        id="a" * 64, path=Path("/tmp/a.json"),
        metadata={"frame_ids": ["1" * 64, "2" * 64]},
        created_at=datetime.now(UTC),
    )
    fs_b = FrameSet(
        id="b" * 64, path=Path("/tmp/b.json"),
        metadata={"frame_ids": ["3" * 64]},
        created_at=datetime.now(UTC),
    )
    est = op.cost_estimate([fs_a, fs_b], FramesCompareParams())
    assert est.tokens_in == 3 * 258
    assert est.cloud_cents > 0


@pytest.mark.needs_gemini
@pytest.mark.skipif(not GENAI_AVAILABLE, reason="google-genai not installed")
async def test_real_gemini_smoke(engine: Engine, sample_mp4: Path) -> None:
    import os

    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    fs_a, fs_b = await _two_framesets(engine, sample_mp4)
    [analysis] = await engine.run(
        "frames.compare",
        inputs=[fs_a.id, fs_b.id],
        prompt="Note one difference between the two inputs.",
        model="gemini-2.5-flash",
    )
    assert isinstance(analysis, Analysis)
    assert len(analysis.data["text"]) > 0
