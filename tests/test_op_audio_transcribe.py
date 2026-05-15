"""Tests for ops/audio/transcribe.py + the mlx-whisper backend.

Two layers:
1. Op contract / dispatch tests use a fake backend (always run).
2. The real mlx-whisper backend smoke-runs against ``sample_speech.wav``
   under the ``needs_mlx`` marker (skipped when mlx-whisper isn't installed).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    Audio,
    Kind,
    Transcript,
)
from media_engine.backends import (
    Backend,
    BackendRegistry,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
from media_engine.ops.audio.transcribe import (
    AudioTranscribe,
    TranscribeParams,
    build_transcript_artifact,
)
from media_engine.runtime.engine import Engine

MLX_AVAILABLE = importlib.util.find_spec("mlx_whisper") is not None


# ─────────────────────────────────────────────────────────────────
# Op contract (no real backend needed)
# ─────────────────────────────────────────────────────────────────


def test_op_class_attributes() -> None:
    assert AudioTranscribe.name == "audio.transcribe"
    assert AudioTranscribe.input_kinds == (Kind.Audio,)
    assert AudioTranscribe.output_kinds == (Kind.Transcript,)
    assert AudioTranscribe.declared_resources == ("apple_neural_engine",)
    assert AudioTranscribe.default_backend == "mlx-whisper"


def test_params_defaults() -> None:
    p = TranscribeParams()
    assert p.model == "mlx-community/whisper-large-v3-mlx"
    assert p.language is None
    assert p.temperature == 0.0
    assert p.word_timestamps is True


def test_cost_estimate_scales_with_duration(tmp_path: Path) -> None:
    op = AudioTranscribe()
    audio = Audio(
        id="a" * 64,
        path=tmp_path / "a.wav",
        metadata={"duration": 100.0, "sample_rate": 16000, "channels": 1},
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    est = op.cost_estimate([audio], TranscribeParams())
    # 100s * 0.3 = 30s
    assert 25 <= est.local_seconds <= 35


# ─────────────────────────────────────────────────────────────────
# Dispatch via Engine.run with a stand-in backend
# ─────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_transcribe_backend() -> type[Backend]:
    """Register a deterministic stand-in backend; clean up afterwards."""
    BackendRegistry.clear()

    @register_backend
    class _FakeWhisper(Backend):
        op_name = "audio.transcribe"
        name = "mlx-whisper"
        version = "0.0.0-fake"
        requires = BackendRequirements()

        async def execute(
            self,
            inputs: list[AnyArtifact],
            params: BaseModel,
            ctx: OperationContext,
        ) -> list[AnyArtifact]:
            assert isinstance(params, TranscribeParams)
            audio = inputs[0]
            assert isinstance(audio, Audio)
            return [
                build_transcript_artifact(
                    audio=audio,
                    params=params,
                    backend_name=self.name,
                    backend_version=self.version,
                    workdir_path=ctx.workdir,
                    storage=ctx.storage,
                    text="hello world",
                    segments=[
                        {"id": 0, "start": 0.0, "end": 1.0, "text": "hello"},
                        {"id": 1, "start": 1.0, "end": 2.0, "text": "world"},
                    ],
                    language="en",
                    model=params.model,
                    duration=audio.duration,
                )
            ]

        def cost_estimate(self, inputs, params):
            return CostEstimate(local_seconds=1.0)

    yield _FakeWhisper
    BackendRegistry.clear()


async def _acquire_audio(engine: Engine, sample_m4a: Path) -> Audio:
    op = AcquireUpload()
    [a] = await op.run([], AcquireUploadParams(source_path=sample_m4a),
                       _ctx_for(engine))
    assert isinstance(a, Audio)
    engine.cache.upsert_artifact(a)
    return a


def _ctx_for(engine: Engine) -> OperationContext:
    workdir = engine.storage.ensure_workdir("test")
    return OperationContext(
        workdir=workdir, config=engine.config, storage=engine.storage,
        namespace=engine.config.namespace, emit=engine.event_bus.emit,
        server_manager=engine.server_manager, model_pool=engine.model_pool,
    )


async def test_engine_run_transcribe_via_fake_backend(
    engine: Engine, sample_m4a: Path, fake_transcribe_backend
) -> None:
    audio = await _acquire_audio(engine, sample_m4a)
    [transcript] = await engine.run("audio.transcribe", inputs=[audio.id])
    assert isinstance(transcript, Transcript)
    assert transcript.derived_from == (audio.id,)
    assert transcript.metadata["text"] == "hello world"
    assert len(transcript.segments) == 2
    assert transcript.language == "en"


async def test_engine_run_transcribe_cache_hit(
    engine: Engine, sample_m4a: Path, fake_transcribe_backend, mocker
) -> None:
    audio = await _acquire_audio(engine, sample_m4a)
    [t1] = await engine.run("audio.transcribe", inputs=[audio.id])
    spy = mocker.spy(fake_transcribe_backend, "execute")
    [t2] = await engine.run("audio.transcribe", inputs=[audio.id])
    assert spy.call_count == 0  # cache hit; backend.execute not invoked again
    assert t1.id == t2.id


async def test_engine_run_transcribe_param_change_yields_new_id(
    engine: Engine, sample_m4a: Path, fake_transcribe_backend
) -> None:
    audio = await _acquire_audio(engine, sample_m4a)
    [t_a] = await engine.run("audio.transcribe", inputs=[audio.id], temperature=0.0)
    [t_b] = await engine.run("audio.transcribe", inputs=[audio.id], temperature=0.5)
    assert t_a.id != t_b.id


async def test_op_rejects_non_audio_input(
    engine: Engine, sample_mp4: Path, fake_transcribe_backend
) -> None:
    """Engine validates input kind before dispatching."""
    op = AcquireUpload()
    [v] = await op.run([], AcquireUploadParams(source_path=sample_mp4),
                       _ctx_for(engine))
    engine.cache.upsert_artifact(v)
    with pytest.raises(ValueError, match="kind mismatch"):
        await engine.run("audio.transcribe", inputs=[v.id])


# ─────────────────────────────────────────────────────────────────
# Real mlx-whisper smoke (skipped without the optional dep)
# ─────────────────────────────────────────────────────────────────


@pytest.mark.needs_mlx
@pytest.mark.skipif(not MLX_AVAILABLE, reason="mlx-whisper not installed")
async def test_real_mlx_whisper_transcribes_speech(
    engine: Engine, sample_speech_wav: Path
) -> None:
    # Acquire the speech wav as an Audio artifact.
    op = AcquireUpload()
    [a] = await op.run([], AcquireUploadParams(source_path=sample_speech_wav),
                       _ctx_for(engine))
    engine.cache.upsert_artifact(a)
    assert isinstance(a, Audio)

    # Use a small model to keep this test cheap if the user has it cached.
    [transcript] = await engine.run(
        "audio.transcribe",
        inputs=[a.id],
        model="mlx-community/whisper-tiny-mlx",
    )
    assert isinstance(transcript, Transcript)
    text = transcript.metadata["text"].lower()
    assert "fox" in text or "dog" in text or len(text) > 5
    assert transcript.language is not None
    assert len(transcript.segments) > 0
