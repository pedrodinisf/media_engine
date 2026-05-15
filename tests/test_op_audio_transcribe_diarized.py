"""Tests for ops/audio/transcribe_diarized.py — composite transcribe + diarize."""

from __future__ import annotations

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
from media_engine.ops.audio.diarize import (
    DiarizeParams,
    build_diarization_artifact,
)
from media_engine.ops.audio.transcribe import (
    TranscribeParams,
    build_transcript_artifact,
)
from media_engine.ops.audio.transcribe_diarized import (
    AudioTranscribeDiarized,
    _align_speakers,
)
from media_engine.runtime.engine import Engine


def _ctx_for(engine: Engine) -> OperationContext:
    workdir = engine.storage.ensure_workdir("test")
    return OperationContext(
        workdir=workdir, config=engine.config, storage=engine.storage,
        namespace=engine.config.namespace, emit=engine.event_bus.emit,
        server_manager=engine.server_manager, model_pool=engine.model_pool,
    )


def test_op_class_attributes() -> None:
    assert AudioTranscribeDiarized.name == "audio.transcribe_diarized"
    assert AudioTranscribeDiarized.input_kinds == (Kind.Audio,)
    assert AudioTranscribeDiarized.output_kinds == (Kind.Transcript,)


def test_align_speakers_max_overlap() -> None:
    transcript_segs = [
        {"start": 0.0, "end": 1.0, "text": "hi"},
        {"start": 1.0, "end": 2.0, "text": "there"},
        {"start": 2.0, "end": 3.0, "text": "friend"},
    ]
    diarization_segs = [
        {"start": 0.0, "end": 1.5, "speaker_id": "SPEAKER_00"},
        {"start": 1.5, "end": 3.0, "speaker_id": "SPEAKER_01"},
    ]
    aligned = _align_speakers(transcript_segs, diarization_segs)
    speakers = [a["speaker_id"] for a in aligned]
    assert speakers == ["SPEAKER_00", "SPEAKER_00", "SPEAKER_01"]


def test_align_speakers_no_overlap_marks_unknown() -> None:
    transcript_segs = [{"start": 5.0, "end": 6.0, "text": "?"}]
    diarization_segs = [{"start": 0.0, "end": 1.0, "speaker_id": "SPEAKER_00"}]
    aligned = _align_speakers(transcript_segs, diarization_segs)
    assert aligned[0]["speaker_id"] == "UNKNOWN"


@pytest.fixture
def fake_transcribe_and_diarize_backends() -> None:
    BackendRegistry.unregister("audio.transcribe", "mlx-whisper")
    BackendRegistry.unregister("audio.diarize", "pyannote")

    @register_backend
    class _FakeWhisper(Backend):
        op_name = "audio.transcribe"
        name = "mlx-whisper"
        version = "0.0.0-fake"
        requires = BackendRequirements()

        async def execute(
            self, inputs: list[AnyArtifact], params: BaseModel,
            ctx: OperationContext,
        ) -> list[AnyArtifact]:
            assert isinstance(params, TranscribeParams)
            audio = inputs[0]
            assert isinstance(audio, Audio)
            return [
                build_transcript_artifact(
                    audio=audio, params=params,
                    backend_name=self.name, backend_version=self.version,
                    workdir_path=ctx.workdir, storage=ctx.storage,
                    text="hi there friend",
                    segments=[
                        {"id": 0, "start": 0.0, "end": 1.0, "text": "hi"},
                        {"id": 1, "start": 1.0, "end": 2.0, "text": "there"},
                        {"id": 2, "start": 2.0, "end": 3.0, "text": "friend"},
                    ],
                    language="en", model=params.model, duration=audio.duration,
                )
            ]

        def cost_estimate(self, inputs, params):
            return CostEstimate(local_seconds=1.0)

    @register_backend
    class _FakePyannote(Backend):
        op_name = "audio.diarize"
        name = "pyannote"
        version = "0.0.0-fake"
        requires = BackendRequirements()

        async def execute(
            self, inputs: list[AnyArtifact], params: BaseModel,
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
                    ],
                    num_speakers=2,
                    model=params.model,
                )
            ]

        def cost_estimate(self, inputs, params):
            return CostEstimate(local_seconds=1.0)

    yield
    BackendRegistry.unregister("audio.transcribe", "mlx-whisper")
    BackendRegistry.unregister("audio.diarize", "pyannote")
    # Restore real backends.
    from media_engine.backends.transcribe.mlx_whisper import (
        MlxWhisperTranscribeBackend,
    )
    BackendRegistry.register(MlxWhisperTranscribeBackend)
    try:
        from media_engine.backends.diarize.pyannote import PyannoteDiarizeBackend
        BackendRegistry.register(PyannoteDiarizeBackend)
    except ImportError:
        pass


async def test_transcribe_diarized_via_engine(
    engine: Engine, sample_m4a: Path, fake_transcribe_and_diarize_backends
) -> None:
    op = AcquireUpload()
    [audio] = await op.run([], AcquireUploadParams(source_path=sample_m4a),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(audio)

    [transcript] = await engine.run("audio.transcribe_diarized", inputs=[audio.id])
    assert isinstance(transcript, Transcript)
    segments = transcript.metadata["segments"]
    assert len(segments) == 3
    speakers = [s["speaker_id"] for s in segments]
    assert speakers == ["SPEAKER_00", "SPEAKER_00", "SPEAKER_01"]
    assert transcript.metadata["num_speakers"] == 2
    # lineage includes audio + sub-results
    assert audio.id in transcript.derived_from
    assert len(transcript.derived_from) == 3


async def test_transcribe_diarized_cache_hit(
    engine: Engine, sample_m4a: Path, fake_transcribe_and_diarize_backends
) -> None:
    op = AcquireUpload()
    [audio] = await op.run([], AcquireUploadParams(source_path=sample_m4a),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(audio)

    [t1] = await engine.run("audio.transcribe_diarized", inputs=[audio.id])
    [t2] = await engine.run("audio.transcribe_diarized", inputs=[audio.id])
    assert t1.id == t2.id


async def test_transcribe_diarized_requires_run_op(
    engine: Engine, sample_m4a: Path, fake_transcribe_and_diarize_backends
) -> None:
    """Calling op.run() directly without ctx.run_op fails clearly."""
    op = AcquireUpload()
    [audio] = await op.run([], AcquireUploadParams(source_path=sample_m4a),
                           _ctx_for(engine))
    engine.cache.upsert_artifact(audio)

    composite = AudioTranscribeDiarized()
    bare_ctx = OperationContext(
        workdir=engine.storage.ensure_workdir("bare"),
        config=engine.config,
        storage=engine.storage,
        # run_op intentionally not set
    )
    from media_engine.ops.audio.transcribe_diarized import (
        TranscribeDiarizedParams,
    )
    with pytest.raises(RuntimeError, match="ctx.run_op"):
        await composite.run([audio], TranscribeDiarizedParams(), bare_ctx)
