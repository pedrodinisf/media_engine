"""``audio.transcribe`` — Audio → Transcript via a pluggable backend.

Default backend: ``mlx-whisper`` (lazy-loaded via ModelPool, runs in
asyncio.to_thread; emits one Progress event per Whisper segment).
Backends ship in ``media_engine.backends.transcribe.*``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field, model_validator

from media_engine.artifacts import (
    AnyArtifact,
    Audio,
    Kind,
    Transcript,
    compute_derived_artifact_id,
)
from media_engine.backends import BackendRegistry
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)
from media_engine.ops.audio._models import WHISPER_MODELS


class TranscribeParams(BaseModel):
    model: Annotated[
        str,
        Field(json_schema_extra={"enum": list(WHISPER_MODELS)}),
    ] = "mlx-community/whisper-large-v3-mlx"
    language: str | None = None  # None = auto-detect
    temperature: float = 0.0
    word_timestamps: bool = True
    # Optional [start_s, end_s) sub-range. Both None → transcribe the full
    # input. The backend ffmpeg-slices to a temp file before invoking
    # mlx-whisper; the slice change propagates into the artifact id hash
    # naturally so a range run gets its own cache key.
    start_s: float | None = Field(default=None, ge=0.0)
    end_s: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _check_range(self) -> TranscribeParams:
        if (
            self.start_s is not None
            and self.end_s is not None
            and self.end_s <= self.start_s
        ):
            raise ValueError(
                f"end_s must be > start_s (got start={self.start_s}, end={self.end_s})"
            )
        return self


@register_op
class AudioTranscribe(Operation):
    """Transcribe an Audio artifact into a typed Transcript with timestamps."""

    name = "audio.transcribe"
    version = "1.0.0"
    input_kinds = (Kind.Audio,)
    output_kinds = (Kind.Transcript,)
    params_model = TranscribeParams
    declared_resources = ("apple_neural_engine",)
    default_backend = "mlx-whisper"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, TranscribeParams)
        if len(inputs) != 1 or not isinstance(inputs[0], Audio):
            raise ValueError(
                f"audio.transcribe expects exactly one Audio input, "
                f"got {[a.kind for a in inputs]}"
            )
        audio: Audio = inputs[0]

        backend_name = self.default_backend
        if backend_name is None:
            raise RuntimeError(
                f"{self.name} has no default backend; register one or "
                f"pass `backend=` to Engine.run."
            )
        backend_cls = BackendRegistry.get(self.name, backend_name)
        backend = backend_cls()
        return await backend.execute([audio], params, ctx)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        if not inputs:
            return CostEstimate()
        audio = inputs[0]
        if isinstance(audio, Audio) and audio.duration is not None:
            # mlx-whisper on M-series Apple Silicon ~ 0.3× real-time.
            return CostEstimate(local_seconds=audio.duration * 0.3)
        return CostEstimate(local_seconds=10.0)


def build_transcript_artifact(
    *,
    audio: Audio,
    params: TranscribeParams,
    backend_name: str,
    backend_version: str,
    workdir_path: Any,
    storage: Any,
    text: str,
    segments: list[dict[str, Any]],
    language: str,
    model: str,
    duration: float | None,
) -> Transcript:
    """Helper used by transcribe backends to materialize a Transcript artifact.

    Computes the derived id, persists the JSON sidecar to the content-addressed
    store, and returns the typed Transcript with a populated metadata dict.
    """
    import json

    derived_id = compute_derived_artifact_id(
        kind=Kind.Transcript,
        op_name="audio.transcribe",
        op_version="1.0.0",
        backend_name=backend_name,
        backend_version=backend_version,
        params=params,
        input_ids=[audio.id],
    )
    payload = {
        "text": text,
        "segments": segments,
        "language": language,
        "model": model,
    }
    tmp_path = workdir_path / f"transcript-{derived_id[:12]}.json"
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    dest = storage.store_file(tmp_path, derived_id, ".json")
    tmp_path.unlink(missing_ok=True)

    metadata: dict[str, Any] = {
        "text": text,
        "segments": segments,
        "language": language,
        "model": model,
    }
    if duration is not None:
        metadata["duration"] = duration

    return Transcript(
        id=derived_id,
        path=dest,
        metadata=metadata,
        derived_from=(audio.id,),
        created_at=datetime.now(UTC),
    )
