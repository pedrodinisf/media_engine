"""Tests for ``speakers.embed_voice`` — backend-stubbed so they run in CI.

The real pyannote embedding path is exercised separately (marked
``needs_pyannote``); here a fake backend returns canned per-turn vectors so we
can lock the op contract, cache behaviour, and the SpeakerEmbedding shape.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    Audio,
    Diarization,
    Kind,
    SpeakerEmbedding,
)
from media_engine.backends import (
    Backend,
    BackendRegistry,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.speakers.embed_voice import (
    EmbedVoiceParams,
    SpeakersEmbedVoice,
    build_speaker_embedding_artifact,
)
from media_engine.runtime.engine import Engine

_OP = "speakers.embed_voice"


def _audio(tmp_path: Path) -> Audio:
    p = tmp_path / "a.wav"
    p.write_bytes(b"RIFFfake")
    return Audio(
        id="a" * 64, path=p,
        metadata={"duration": 6.0, "sample_rate": 16000, "channels": 1},
        created_at=datetime.now(UTC),
    )


def _diarization(tmp_path: Path) -> Diarization:
    p = tmp_path / "d.json"
    p.write_text("{}")
    return Diarization(
        id="d" * 64, path=p,
        metadata={
            "segments": [
                {"start": 0.0, "end": 1.5, "speaker_id": "SPEAKER_00"},
                {"start": 1.5, "end": 3.0, "speaker_id": "SPEAKER_01"},
                {"start": 3.0, "end": 4.5, "speaker_id": "SPEAKER_00"},
            ],
            "num_speakers": 2,
            "model": "pyannote/speaker-diarization-3.1",
        },
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def fake_embed_backend():
    with contextlib.suppress(Exception):
        BackendRegistry.unregister(_OP, "pyannote")

    @register_backend
    class _Fake(Backend):
        op_name = _OP
        name = "pyannote"
        version = "0.0.0-fake"
        requires = BackendRequirements()

        async def execute(
            self, inputs: list[AnyArtifact], params: BaseModel,
            ctx: OperationContext,
        ) -> list[AnyArtifact]:
            assert isinstance(params, EmbedVoiceParams)
            audio = next(a for a in inputs if isinstance(a, Audio))
            diar = next(a for a in inputs if isinstance(a, Diarization))
            turns = []
            # Canned per-speaker vectors: SPEAKER_00 near [1,0,0], _01 near [0,1,0].
            canned = {"SPEAKER_00": [1.0, 0.0, 0.0], "SPEAKER_01": [0.0, 1.0, 0.0]}
            for seg in diar.segments:
                if (seg["end"] - seg["start"]) < params.min_turn_seconds:
                    continue
                turns.append({
                    "speaker_id": seg["speaker_id"],
                    "start": seg["start"], "end": seg["end"],
                    "vector": canned[seg["speaker_id"]],
                })
            return [build_speaker_embedding_artifact(
                audio=audio, diarization=diar, params=params,
                backend_name=self.name, backend_version=self.version,
                workdir_path=ctx.workdir, storage=ctx.storage,
                turns=turns, model=params.model,
            )]

        def cost_estimate(self, inputs, params):
            return CostEstimate(local_seconds=1.0)

    yield _Fake
    with contextlib.suppress(Exception):
        BackendRegistry.unregister(_OP, "pyannote")


def test_op_invariants() -> None:
    assert SpeakersEmbedVoice.name == _OP
    assert SpeakersEmbedVoice.input_kinds == (Kind.Audio, Kind.Diarization)
    assert SpeakersEmbedVoice.variadic_inputs is True
    assert SpeakersEmbedVoice.output_kinds == (Kind.SpeakerEmbedding,)
    assert SpeakersEmbedVoice.default_backend == "pyannote"
    assert "backend" not in EmbedVoiceParams.model_fields


def test_params_defaults() -> None:
    p = EmbedVoiceParams()
    assert p.model == "pyannote/embedding"
    assert p.min_turn_seconds == 0.5


def test_params_reject_bad_range() -> None:
    with pytest.raises(ValueError, match="end_s must be"):
        EmbedVoiceParams(start_s=5.0, end_s=2.0)


def _seed(engine: Engine, tmp_path: Path) -> tuple[Audio, Diarization]:
    audio, diar = _audio(tmp_path), _diarization(tmp_path)
    engine.cache.upsert_artifact(audio)
    engine.cache.upsert_artifact(diar)
    return audio, diar


async def test_success_via_engine_run(
    engine: Engine, tmp_path: Path, fake_embed_backend
) -> None:
    audio, diar = _seed(engine, tmp_path)
    [emb] = await engine.run(_OP, inputs=[audio.id, diar.id])
    assert isinstance(emb, SpeakerEmbedding)
    assert emb.dimensions == 3
    assert len(emb.turns) == 3
    assert set(emb.derived_from) == {audio.id, diar.id}
    assert emb.metadata["source_audio_id"] == audio.id


async def test_inputs_order_independent(
    engine: Engine, tmp_path: Path, fake_embed_backend
) -> None:
    audio, diar = _seed(engine, tmp_path)
    [a] = await engine.run(_OP, inputs=[audio.id, diar.id])
    [b] = await engine.run(_OP, inputs=[diar.id, audio.id])
    assert a.id == b.id  # derived id sorts input ids


async def test_cache_hit_on_rerun(
    engine: Engine, tmp_path: Path, fake_embed_backend, mocker
) -> None:
    audio, diar = _seed(engine, tmp_path)
    [e1] = await engine.run(_OP, inputs=[audio.id, diar.id])
    spy = mocker.spy(fake_embed_backend, "execute")
    [e2] = await engine.run(_OP, inputs=[audio.id, diar.id])
    assert spy.call_count == 0
    assert e1.id == e2.id


async def test_param_change_yields_new_id(
    engine: Engine, tmp_path: Path, fake_embed_backend
) -> None:
    audio, diar = _seed(engine, tmp_path)
    [a] = await engine.run(_OP, inputs=[audio.id, diar.id], min_turn_seconds=0.5)
    [b] = await engine.run(_OP, inputs=[audio.id, diar.id], min_turn_seconds=2.0)
    assert a.id != b.id


def test_run_rejects_missing_diarization(tmp_path: Path) -> None:
    import asyncio
    op = SpeakersEmbedVoice()
    audio = _audio(tmp_path)
    ctx = OperationContext(
        workdir=tmp_path, config=None, storage=None,  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="exactly one Audio and one"):
        asyncio.run(op.run([audio], EmbedVoiceParams(), ctx))
