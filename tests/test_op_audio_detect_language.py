"""Tests for ops/audio/detect_language.py + mlx-whisper backend."""

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
)
from media_engine.backends import (
    Backend,
    BackendRegistry,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
from media_engine.ops.audio.detect_language import (
    AudioDetectLanguage,
    DetectLanguageParams,
    build_detect_language_artifact,
)
from media_engine.runtime.engine import Engine

MLX_AVAILABLE = importlib.util.find_spec("mlx_whisper") is not None


def test_op_class_attributes() -> None:
    assert AudioDetectLanguage.name == "audio.detect_language"
    assert AudioDetectLanguage.input_kinds == (Kind.Audio,)
    assert AudioDetectLanguage.output_kinds == (Kind.Analysis,)
    assert AudioDetectLanguage.default_backend == "mlx-whisper"


def test_cost_estimate_is_cheap() -> None:
    op = AudioDetectLanguage()
    est = op.cost_estimate([], DetectLanguageParams())
    assert est.local_seconds <= 5


@pytest.fixture
def fake_detect_backend() -> type[Backend]:
    BackendRegistry.clear()

    @register_backend
    class _Fake(Backend):
        op_name = "audio.detect_language"
        name = "mlx-whisper"
        version = "0.0.0-fake"
        requires = BackendRequirements()

        async def execute(
            self,
            inputs: list[AnyArtifact],
            params: BaseModel,
            ctx: OperationContext,
        ) -> list[AnyArtifact]:
            assert isinstance(params, DetectLanguageParams)
            audio = inputs[0]
            assert isinstance(audio, Audio)
            return [
                build_detect_language_artifact(
                    audio=audio,
                    params=params,
                    backend_name=self.name,
                    backend_version=self.version,
                    workdir_path=ctx.workdir,
                    storage=ctx.storage,
                    language="en",
                    confidence=0.97,
                    alternatives={"en": 0.97, "de": 0.02, "fr": 0.01},
                )
            ]

        def cost_estimate(self, inputs, params):
            return CostEstimate(local_seconds=2.0)

    yield _Fake
    BackendRegistry.clear()


def _ctx_for(engine: Engine) -> OperationContext:
    workdir = engine.storage.ensure_workdir("test")
    return OperationContext(
        workdir=workdir, config=engine.config, storage=engine.storage,
        namespace=engine.config.namespace, emit=engine.event_bus.emit,
        server_manager=engine.server_manager, model_pool=engine.model_pool,
    )


async def test_detect_language_via_fake_backend(
    engine: Engine, sample_m4a: Path, fake_detect_backend
) -> None:
    op = AcquireUpload()
    [audio] = await op.run([], AcquireUploadParams(source_path=sample_m4a),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(audio)

    [analysis] = await engine.run("audio.detect_language", inputs=[audio.id])
    assert isinstance(analysis, Analysis)
    data = analysis.metadata["data"]
    assert data["language"] == "en"
    assert data["confidence"] == 0.97
    assert "de" in data["alternatives"]


async def test_detect_language_cache_hit(
    engine: Engine, sample_m4a: Path, fake_detect_backend, mocker
) -> None:
    op = AcquireUpload()
    [audio] = await op.run([], AcquireUploadParams(source_path=sample_m4a),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(audio)

    [a1] = await engine.run("audio.detect_language", inputs=[audio.id])
    spy = mocker.spy(fake_detect_backend, "execute")
    [a2] = await engine.run("audio.detect_language", inputs=[audio.id])
    assert spy.call_count == 0
    assert a1.id == a2.id


@pytest.mark.needs_mlx
@pytest.mark.skipif(not MLX_AVAILABLE, reason="mlx-whisper not installed")
async def test_real_mlx_whisper_detect_language(
    engine: Engine, sample_speech_wav: Path
) -> None:
    op = AcquireUpload()
    [audio] = await op.run(
        [], AcquireUploadParams(source_path=sample_speech_wav), _ctx_for(engine)
    )
    engine.cache.upsert_artifact(audio)

    [analysis] = await engine.run(
        "audio.detect_language",
        inputs=[audio.id],
        model="mlx-community/whisper-tiny-mlx",
    )
    data = analysis.metadata["data"]
    assert data["language"] in ("en", "english")
    assert data["confidence"] > 0
