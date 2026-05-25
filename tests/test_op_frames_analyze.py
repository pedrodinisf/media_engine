"""Tests for ops/frames/analyze.py + its backends.

Op-contract + dispatch tests use a fake backend (always run). The real
Gemini backend smoke is `needs_gemini`-gated; vllm-mlx is `needs_vllm`.
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
from media_engine.ops.frames.analyze import (
    FramesAnalyze,
    FramesAnalyzeParams,
    _backend_for_model,
    build_frames_analysis,
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
        workdir=engine.storage.ensure_workdir("fa-test"),
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
        emit=engine.event_bus.emit,
        server_manager=engine.server_manager,
        model_pool=engine.model_pool,
    )


def test_op_class_attributes() -> None:
    assert FramesAnalyze.name == "frames.analyze"
    assert FramesAnalyze.input_kinds == (Kind.FrameSet,)
    assert FramesAnalyze.output_kinds == (Kind.Analysis,)
    assert FramesAnalyze.default_backend == "gemini"


def test_params_defaults() -> None:
    p = FramesAnalyzeParams(prompt="describe")
    assert p.model == "gemini-2.5-pro"
    assert p.media_resolution == "medium"
    assert p.temperature == 0.2


def test_backend_for_model() -> None:
    assert _backend_for_model("gemini-2.5-pro") == "gemini"
    assert _backend_for_model("mlx-community/Qwen2.5-VL-7B-Instruct-4bit") == "vllm-mlx"


@pytest.fixture
def fake_fa_backend() -> type[Backend]:
    BackendRegistry.unregister("frames.analyze", "gemini")

    @register_backend
    class _Fake(Backend):
        op_name = "frames.analyze"
        name = "gemini"
        version = "0.0.0-fake"
        requires = BackendRequirements()

        async def execute(
            self,
            inputs: list[AnyArtifact],
            params: BaseModel,
            ctx: OperationContext,
        ) -> list[AnyArtifact]:
            assert isinstance(params, FramesAnalyzeParams)
            fs = inputs[0]
            assert isinstance(fs, FrameSet)
            return [
                build_frames_analysis(
                    frameset=fs,
                    params=params,
                    backend_name=self.name,
                    backend_version=self.version,
                    workdir_path=ctx.workdir,
                    storage=ctx.storage,
                    text=f"Analyzed {fs.frame_count} frames. Prompt: {params.prompt}",
                    usage={"input_tokens": 800, "output_tokens": 40,
                           "cost_cents": 0.1},
                )
            ]

        def cost_estimate(self, inputs, params):
            return CostEstimate(cloud_cents=0.3)

    yield _Fake
    BackendRegistry.unregister("frames.analyze", "gemini")
    from media_engine.bootstrap import register_all
    register_all(force=True)


async def _frameset(engine: Engine, sample_mp4: Path) -> FrameSet:
    [video] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_mp4), _ctx_for(engine)
    )
    engine.cache.upsert_artifact(video)
    [fs] = await engine.run("video.sample_frames", inputs=[video.id], fps=2.0)
    assert isinstance(fs, FrameSet)
    return fs


async def test_analyze_via_fake_backend(
    engine: Engine, sample_mp4: Path, fake_fa_backend
) -> None:
    fs = await _frameset(engine, sample_mp4)
    [analysis] = await engine.run(
        "frames.analyze", inputs=[fs.id], prompt="What is shown?"
    )
    assert isinstance(analysis, Analysis)
    assert analysis.derived_from == (fs.id,)
    assert "What is shown?" in analysis.data["text"]
    assert analysis.metadata["usage"]["input_tokens"] == 800
    assert analysis.metadata["backend"] == "gemini"


async def test_analyze_cache_hit(
    engine: Engine, sample_mp4: Path, fake_fa_backend, mocker
) -> None:
    fs = await _frameset(engine, sample_mp4)
    [a1] = await engine.run("frames.analyze", inputs=[fs.id], prompt="p")
    spy = mocker.spy(fake_fa_backend, "execute")
    [a2] = await engine.run("frames.analyze", inputs=[fs.id], prompt="p")
    assert spy.call_count == 0
    assert a1.id == a2.id


async def test_analyze_param_change_new_id(
    engine: Engine, sample_mp4: Path, fake_fa_backend
) -> None:
    fs = await _frameset(engine, sample_mp4)
    [a] = await engine.run("frames.analyze", inputs=[fs.id], prompt="one")
    [b] = await engine.run("frames.analyze", inputs=[fs.id], prompt="two")
    assert a.id != b.id


async def test_analyze_rejects_non_frameset(
    engine: Engine, sample_mp4: Path, fake_fa_backend
) -> None:
    [video] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample_mp4), _ctx_for(engine)
    )
    engine.cache.upsert_artifact(video)
    with pytest.raises(ValueError, match="kind mismatch"):
        await engine.run("frames.analyze", inputs=[video.id], prompt="x")


async def test_analyze_rejects_incompatible_backend_for_model(
    engine: Engine, sample_mp4: Path, fake_fa_backend
) -> None:
    """B-008: passing --backend vllm-mlx with a gemini default model must
    fail loudly at submit time, not hit a confusing model-load error
    deep inside the backend.
    """
    fs = await _frameset(engine, sample_mp4)
    with pytest.raises(ValueError, match="incompatible|routes to"):
        await engine.run(
            "frames.analyze",
            inputs=[fs.id],
            prompt="x",
            backend="vllm-mlx",  # router would have picked gemini for gemini-2.5-pro
        )


async def test_analyze_accepts_matching_explicit_backend(
    engine: Engine, sample_mp4: Path, fake_fa_backend
) -> None:
    """B-008 sanity: an explicit --backend that matches the router's
    pick is accepted (no false-positive reject)."""
    fs = await _frameset(engine, sample_mp4)
    [analysis] = await engine.run(
        "frames.analyze",
        inputs=[fs.id],
        prompt="x",
        backend="gemini",
    )
    assert analysis is not None


def test_cost_estimate_gemini_vs_local(engine: Engine) -> None:
    op = FramesAnalyze()
    from datetime import UTC, datetime

    fs = FrameSet(
        id="f" * 64, path=Path("/tmp/fs.json"),
        metadata={"frame_ids": ["a" * 64, "b" * 64]},
        created_at=datetime.now(UTC),
    )
    cloud = op.cost_estimate([fs], FramesAnalyzeParams(prompt="x"))
    assert cloud.cloud_cents > 0
    assert cloud.tokens_in == 2 * 258
    local = op.cost_estimate(
        [fs],
        FramesAnalyzeParams(
            prompt="x", model="mlx-community/Qwen2.5-VL-7B-Instruct-4bit"
        ),
    )
    assert local.local_seconds > 0
    assert local.cloud_cents == 0


@pytest.mark.needs_gemini
@pytest.mark.skipif(not GENAI_AVAILABLE, reason="google-genai not installed")
async def test_real_gemini_smoke(engine: Engine, sample_mp4: Path) -> None:
    import os

    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    fs = await _frameset(engine, sample_mp4)
    [analysis] = await engine.run(
        "frames.analyze",
        inputs=[fs.id],
        prompt="Describe these frames in one sentence.",
        model="gemini-2.5-flash",
    )
    assert isinstance(analysis, Analysis)
    assert len(analysis.data["text"]) > 0
