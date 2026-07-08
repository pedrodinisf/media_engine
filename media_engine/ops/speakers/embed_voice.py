"""``speakers.embed_voice`` — (Audio + Diarization) → SpeakerEmbedding.

Derives a voice fingerprint (embedding vector) for every diarization turn, so
downstream ``speakers.cluster`` / ``speakers.match`` can reason about *who* is
speaking acoustically rather than by transcript text. One SpeakerEmbedding
artifact per recording holds the full list of per-turn vectors (mirrors how
Diarization holds all its segments).

Default backend: ``pyannote`` (pyannote.audio embedding model, MPS-accelerated
on Apple Silicon; loaded lazily via the ModelPool).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field, model_validator

from media_engine.artifacts import (
    AnyArtifact,
    Audio,
    Diarization,
    Kind,
    SpeakerEmbedding,
    compute_derived_artifact_id,
)
from media_engine.backends import BackendRegistry
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)
from media_engine.ops.speakers._models import EMBED_VOICE_MODELS

OP_NAME = "speakers.embed_voice"
OP_VERSION = "1.0.0"


class EmbedVoiceParams(BaseModel):
    model: Annotated[
        str,
        Field(json_schema_extra={"enum": list(EMBED_VOICE_MODELS)}),
    ] = "pyannote/embedding"
    # Turns shorter than this yield unreliable embeddings — skipped (and
    # counted) rather than polluting the fingerprint. 0 disables the filter.
    min_turn_seconds: float = Field(default=0.5, ge=0.0)
    # Range slicing, same shape as audio.diarize so a composite could forward.
    start_s: float | None = Field(default=None, ge=0.0)
    end_s: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _check_range(self) -> EmbedVoiceParams:
        if (
            self.start_s is not None
            and self.end_s is not None
            and self.end_s <= self.start_s
        ):
            raise ValueError(
                f"end_s must be > start_s (got start={self.start_s}, end={self.end_s})"
            )
        return self


def _split_inputs(inputs: list[AnyArtifact]) -> tuple[Audio, Diarization]:
    """Pull the Audio + Diarization out of the input list (order-independent)."""
    audio = next((a for a in inputs if isinstance(a, Audio)), None)
    diar = next((a for a in inputs if isinstance(a, Diarization)), None)
    if audio is None or diar is None or len(inputs) != 2:
        raise ValueError(
            f"speakers.embed_voice expects exactly one Audio and one "
            f"Diarization input, got {[a.kind for a in inputs]}"
        )
    return audio, diar


@register_op
class SpeakersEmbedVoice(Operation):
    """Embed each diarization turn into a voice fingerprint."""

    name = OP_NAME
    version = OP_VERSION
    # Two inputs (Audio, Diarization) in any order — variadic lets the engine
    # validate kind membership; run() pins arity + roles.
    input_kinds = (Kind.Audio, Kind.Diarization)
    variadic_inputs = True
    output_kinds = (Kind.SpeakerEmbedding,)
    params_model = EmbedVoiceParams
    declared_resources = ("apple_neural_engine",)
    default_backend = "pyannote"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, EmbedVoiceParams)
        _split_inputs(inputs)  # validate arity/roles before touching a backend
        backend_name = ctx.backend or self.default_backend
        if backend_name is None:
            raise RuntimeError(
                f"{self.name} has no backend; register one or pass "
                f"`backend=` to Engine.run."
            )
        backend_cls = BackendRegistry.get(self.name, backend_name)
        return await backend_cls().execute(inputs, params, ctx)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        diar = next((a for a in inputs if isinstance(a, Diarization)), None)
        n_turns = len(diar.segments) if diar is not None else 0
        # ~50 ms per turn on MPS after warm-up, plus a fixed load cost.
        return CostEstimate(local_seconds=0.05 * n_turns + 3.0)


def build_speaker_embedding_artifact(
    *,
    audio: Audio,
    diarization: Diarization,
    params: EmbedVoiceParams,
    backend_name: str,
    backend_version: str,
    workdir_path: Any,
    storage: Any,
    turns: list[dict[str, Any]],
    model: str,
) -> SpeakerEmbedding:
    """Persist the per-turn vectors as one SpeakerEmbedding JSON sidecar."""
    derived_id = compute_derived_artifact_id(
        kind=Kind.SpeakerEmbedding,
        op_name=OP_NAME,
        op_version=OP_VERSION,
        backend_name=backend_name,
        backend_version=backend_version,
        params=params,
        input_ids=[audio.id, diarization.id],
    )
    dims = len(turns[0]["vector"]) if turns else 0
    payload: dict[str, Any] = {
        "turns": turns,
        "model": model,
        "dimensions": dims,
        "source_audio_id": audio.id,
        "diarization_id": diarization.id,
    }
    tmp = workdir_path / f"speaker-embedding-{derived_id[:12]}.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    dest = storage.store_file(tmp, derived_id, ".json")
    tmp.unlink(missing_ok=True)
    return SpeakerEmbedding(
        id=derived_id,
        path=dest,
        metadata=payload,
        derived_from=(audio.id, diarization.id),
        created_at=datetime.now(UTC),
    )


__all__ = [
    "OP_NAME",
    "OP_VERSION",
    "EmbedVoiceParams",
    "SpeakersEmbedVoice",
    "build_speaker_embedding_artifact",
]
