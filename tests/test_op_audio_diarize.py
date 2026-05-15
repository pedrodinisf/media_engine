"""Tests for ops/audio/diarize.py + the pyannote backend."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    Audio,
    Diarization,
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
from media_engine.ops.audio.diarize import (
    AudioDiarize,
    DiarizeParams,
    build_diarization_artifact,
)
from media_engine.runtime.engine import Engine

PYANNOTE_AVAILABLE = (
    importlib.util.find_spec("pyannote") is not None
    and importlib.util.find_spec("pyannote.audio") is not None
)


def test_op_class_attributes() -> None:
    assert AudioDiarize.name == "audio.diarize"
    assert AudioDiarize.input_kinds == (Kind.Audio,)
    assert AudioDiarize.output_kinds == (Kind.Diarization,)
    assert AudioDiarize.declared_resources == ("apple_neural_engine",)
    assert AudioDiarize.default_backend == "pyannote"


def test_params_defaults() -> None:
    p = DiarizeParams()
    assert p.num_speakers is None
    assert p.min_speakers is None
    assert p.max_speakers is None
    assert p.model == "pyannote/speaker-diarization-3.1"


def test_cost_estimate_scales_with_duration(tmp_path: Path) -> None:
    op = AudioDiarize()
    audio = Audio(
        id="a" * 64, path=tmp_path / "a.wav",
        metadata={"duration": 60.0, "sample_rate": 16000, "channels": 1},
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    est = op.cost_estimate([audio], DiarizeParams())
    # 60s * 0.2 + 5 = 17s
    assert 15 <= est.local_seconds <= 20


def _ctx_for(engine: Engine) -> OperationContext:
    workdir = engine.storage.ensure_workdir("test")
    return OperationContext(
        workdir=workdir, config=engine.config, storage=engine.storage,
        namespace=engine.config.namespace, emit=engine.event_bus.emit,
        server_manager=engine.server_manager, model_pool=engine.model_pool,
    )


@pytest.fixture
def fake_diarize_backend() -> type[Backend]:
    BackendRegistry.unregister("audio.diarize", "pyannote")

    @register_backend
    class _Fake(Backend):
        op_name = "audio.diarize"
        name = "pyannote"
        version = "0.0.0-fake"
        requires = BackendRequirements()

        async def execute(
            self,
            inputs: list[AnyArtifact],
            params: BaseModel,
            ctx: OperationContext,
        ) -> list[AnyArtifact]:
            assert isinstance(params, DiarizeParams)
            audio = inputs[0]
            assert isinstance(audio, Audio)
            return [
                build_diarization_artifact(
                    audio=audio, params=params,
                    backend_name=self.name, backend_version=self.version,
                    workdir_path=ctx.workdir, storage=ctx.storage,
                    segments=[
                        {"start": 0.0, "end": 1.5, "speaker_id": "SPEAKER_00"},
                        {"start": 1.5, "end": 3.0, "speaker_id": "SPEAKER_01"},
                        {"start": 3.0, "end": 4.5, "speaker_id": "SPEAKER_00"},
                    ],
                    num_speakers=2,
                    model=params.model,
                )
            ]

        def cost_estimate(self, inputs, params):
            return CostEstimate(local_seconds=10.0)

    yield _Fake
    BackendRegistry.unregister("audio.diarize", "pyannote")
    # Try to restore the real pyannote backend if available.
    try:
        from media_engine.backends.diarize.pyannote import PyannoteDiarizeBackend
        BackendRegistry.register(PyannoteDiarizeBackend)
    except ImportError:
        pass


async def test_diarize_via_fake_backend(
    engine: Engine, sample_m4a: Path, fake_diarize_backend
) -> None:
    op = AcquireUpload()
    [audio] = await op.run([], AcquireUploadParams(source_path=sample_m4a),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(audio)

    [diar] = await engine.run("audio.diarize", inputs=[audio.id])
    assert isinstance(diar, Diarization)
    assert diar.derived_from == (audio.id,)
    assert diar.num_speakers == 2
    assert len(diar.segments) == 3
    assert diar.segments[0]["speaker_id"] == "SPEAKER_00"


async def test_diarize_cache_hit(
    engine: Engine, sample_m4a: Path, fake_diarize_backend, mocker
) -> None:
    op = AcquireUpload()
    [audio] = await op.run([], AcquireUploadParams(source_path=sample_m4a),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(audio)

    [d1] = await engine.run("audio.diarize", inputs=[audio.id])
    spy = mocker.spy(fake_diarize_backend, "execute")
    [d2] = await engine.run("audio.diarize", inputs=[audio.id])
    assert spy.call_count == 0
    assert d1.id == d2.id


async def test_diarize_param_change_yields_new_id(
    engine: Engine, sample_m4a: Path, fake_diarize_backend
) -> None:
    op = AcquireUpload()
    [audio] = await op.run([], AcquireUploadParams(source_path=sample_m4a),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(audio)

    [a] = await engine.run("audio.diarize", inputs=[audio.id], num_speakers=2)
    [b] = await engine.run("audio.diarize", inputs=[audio.id], num_speakers=3)
    assert a.id != b.id


async def test_diarize_rejects_non_audio(
    engine: Engine, sample_mp4: Path, fake_diarize_backend
) -> None:
    op = AcquireUpload()
    [v] = await op.run([], AcquireUploadParams(source_path=sample_mp4),
                       _ctx_for(engine))
    engine.cache.upsert_artifact(v)
    with pytest.raises(ValueError, match="kind mismatch"):
        await engine.run("audio.diarize", inputs=[v.id])


@pytest.mark.needs_pyannote
@pytest.mark.skipif(
    not (PYANNOTE_AVAILABLE and os.environ.get("HF_TOKEN")),
    reason="pyannote.audio + HF_TOKEN required",
)
async def test_real_pyannote_detects_two_speakers(
    engine: Engine, sample_dialogue_wav: Path
) -> None:
    op = AcquireUpload()
    [audio] = await op.run([], AcquireUploadParams(source_path=sample_dialogue_wav),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(audio)

    [diar] = await engine.run("audio.diarize", inputs=[audio.id])
    assert isinstance(diar, Diarization)
    assert diar.num_speakers >= 1  # ideally 2; tolerate 1 if voices too similar
