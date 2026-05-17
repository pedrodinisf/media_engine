"""Tests for ops/video/multimodal.py + the Gemini backend.

Op-contract + dispatch tests use a fake backend (always run). The real
Gemini backend smoke is `needs_gemini`-gated.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pydantic import BaseModel

from media_engine.artifacts import (
    Analysis,
    AnyArtifact,
    Audio,
    Kind,
    Video,
)
from media_engine.backends import (
    Backend,
    BackendRegistry,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
from media_engine.ops.video.multimodal import (
    MultimodalVideoParams,
    VideoMultimodal,
    _default_backend_for_model,
    build_multimodal_analysis_artifact,
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
        workdir=engine.storage.ensure_workdir("mm-test"),
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
        emit=engine.event_bus.emit,
        server_manager=engine.server_manager,
        model_pool=engine.model_pool,
    )


def test_op_class_attributes() -> None:
    assert VideoMultimodal.name == "video.multimodal"
    assert VideoMultimodal.input_kinds == (Kind.Video,)
    assert VideoMultimodal.output_kinds == (Kind.Analysis,)
    assert VideoMultimodal.default_backend == "gemini"


def test_params_defaults() -> None:
    p = MultimodalVideoParams(prompt="describe this")
    assert p.model == "gemini-2.5-pro"
    assert p.media_resolution == "medium"
    assert p.temperature == 0.7
    assert p.max_tokens == 8192


def test_backend_selection_by_model_prefix() -> None:
    assert _default_backend_for_model("gemini-2.5-pro") == "gemini"
    assert _default_backend_for_model("gemini-3-flash") == "gemini"
    assert (
        _default_backend_for_model("mlx-community/Qwen2.5-VL-7B-Instruct-4bit")
        == "vllm-mlx"
    )


@pytest.fixture
def fake_mm_backend() -> type[Backend]:
    BackendRegistry.unregister("video.multimodal", "gemini")

    @register_backend
    class _Fake(Backend):
        op_name = "video.multimodal"
        name = "gemini"
        version = "0.0.0-fake"
        requires = BackendRequirements()

        async def execute(
            self,
            inputs: list[AnyArtifact],
            params: BaseModel,
            ctx: OperationContext,
        ) -> list[AnyArtifact]:
            assert isinstance(params, MultimodalVideoParams)
            video = inputs[0]
            assert isinstance(video, Video)
            return [
                build_multimodal_analysis_artifact(
                    video=video,
                    params=params,
                    backend_name=self.name,
                    backend_version=self.version,
                    workdir_path=ctx.workdir,
                    storage=ctx.storage,
                    text=f"A description of the video. Prompt was: {params.prompt}",
                    usage={"input_tokens": 1000, "output_tokens": 50,
                           "cost_cents": 0.12},
                )
            ]

        def cost_estimate(self, inputs, params):
            return CostEstimate(cloud_cents=0.5)

    yield _Fake
    BackendRegistry.unregister("video.multimodal", "gemini")
    from media_engine.bootstrap import register_all
    register_all(force=True)


async def _video(engine: Engine, sample_mp4: Path) -> Video:
    op = AcquireUpload()
    [v] = await op.run([], AcquireUploadParams(source_path=sample_mp4),
                       _ctx_for(engine))
    assert isinstance(v, Video)
    engine.cache.upsert_artifact(v)
    return v


async def test_multimodal_via_fake_backend(
    engine: Engine, sample_mp4: Path, fake_mm_backend
) -> None:
    video = await _video(engine, sample_mp4)
    [analysis] = await engine.run(
        "video.multimodal", inputs=[video.id], prompt="What happens?"
    )
    assert isinstance(analysis, Analysis)
    assert analysis.derived_from == (video.id,)
    assert "What happens?" in analysis.data["text"]
    assert analysis.metadata["usage"]["input_tokens"] == 1000
    assert analysis.metadata["backend"] == "gemini"


async def test_multimodal_cache_hit(
    engine: Engine, sample_mp4: Path, fake_mm_backend, mocker
) -> None:
    video = await _video(engine, sample_mp4)
    [a1] = await engine.run("video.multimodal", inputs=[video.id], prompt="p")
    spy = mocker.spy(fake_mm_backend, "execute")
    [a2] = await engine.run("video.multimodal", inputs=[video.id], prompt="p")
    assert spy.call_count == 0
    assert a1.id == a2.id


async def test_multimodal_param_change_new_id(
    engine: Engine, sample_mp4: Path, fake_mm_backend
) -> None:
    video = await _video(engine, sample_mp4)
    [a] = await engine.run("video.multimodal", inputs=[video.id], prompt="one")
    [b] = await engine.run("video.multimodal", inputs=[video.id], prompt="two")
    assert a.id != b.id


async def test_multimodal_rejects_non_video(
    engine: Engine, sample_m4a: Path, fake_mm_backend
) -> None:
    op = AcquireUpload()
    [audio] = await op.run([], AcquireUploadParams(source_path=sample_m4a),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(audio)
    assert isinstance(audio, Audio)
    with pytest.raises(ValueError, match="kind mismatch"):
        await engine.run("video.multimodal", inputs=[audio.id], prompt="x")


def test_cost_estimate_uses_gemini_pricing(
    engine: Engine, tmp_path: Path
) -> None:
    from datetime import UTC, datetime

    op = VideoMultimodal()
    v = Video(
        id="v" * 64, path=tmp_path / "v.mp4",
        metadata={"duration": 60.0}, created_at=datetime.now(UTC),
    )
    est = op.cost_estimate([v], MultimodalVideoParams(prompt="x", model="gemini-2.5-pro"))
    # 60s @ medium (290 tok/s) → 17400 input tokens; cost > 0.
    assert est.tokens_in == 17400
    assert est.cloud_cents > 0


@pytest.mark.needs_gemini
@pytest.mark.skipif(not GENAI_AVAILABLE, reason="google-genai not installed")
async def test_real_gemini_smoke(engine: Engine, sample_mp4: Path) -> None:
    import os

    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    video = await _video(engine, sample_mp4)
    [analysis] = await engine.run(
        "video.multimodal",
        inputs=[video.id],
        prompt="Describe what you see in one sentence.",
        model="gemini-2.5-flash",
    )
    assert isinstance(analysis, Analysis)
    assert len(analysis.data["text"]) > 0
    assert analysis.metadata["usage"]["total_tokens"] > 0
