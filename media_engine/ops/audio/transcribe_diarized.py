"""``audio.transcribe_diarized`` — composite Audio → Transcript[+speakers].

Internally invokes ``audio.transcribe`` and ``audio.diarize`` through the
engine (so each sub-result is cached independently), then aligns each
transcript segment to the diarization segment with the largest temporal
overlap. The result is a Transcript whose ``segments`` carry an extra
``speaker_id`` field — the same shape the ``speakered_txt`` parser
emits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field, model_validator

from media_engine.artifacts import (
    AnyArtifact,
    Audio,
    Diarization,
    Kind,
    Transcript,
    compute_derived_artifact_id,
)
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)
from media_engine.ops.audio._models import DIARIZE_MODELS, WHISPER_MODELS


class TranscribeDiarizedParams(BaseModel):
    transcribe_model: Annotated[
        str,
        Field(json_schema_extra={"enum": list(WHISPER_MODELS)}),
    ] = "mlx-community/whisper-large-v3-mlx"
    diarize_model: Annotated[
        str,
        Field(json_schema_extra={"enum": list(DIARIZE_MODELS)}),
    ] = "pyannote/speaker-diarization-3.1"
    language: str | None = None
    num_speakers: int | None = None
    transcribe_backend: str | None = None  # default backend if unset
    diarize_backend: str | None = None
    # Forwarded to both audio.transcribe + audio.diarize sub-op calls in
    # run() — each sub-op slices the audio independently. Two ffmpeg copy
    # calls per composite run when a range is set; stream-copy is
    # sub-second, the trade-off buys clean cache semantics (each sub-op
    # gets its own derived artifact id).
    start_s: float | None = Field(default=None, ge=0.0)
    end_s: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _check_range(self) -> TranscribeDiarizedParams:
        if (
            self.start_s is not None
            and self.end_s is not None
            and self.end_s <= self.start_s
        ):
            raise ValueError(
                f"end_s must be > start_s (got start={self.start_s}, end={self.end_s})"
            )
        return self


def _align_speakers(
    transcript_segments: list[dict[str, Any]],
    diarization_segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Stamp each transcript segment with the diarization speaker that has
    maximum temporal overlap. Falls through to ``"UNKNOWN"`` when neither
    side has overlap."""
    out: list[dict[str, Any]] = []
    for seg in transcript_segments:
        ts, te = float(seg.get("start", 0.0)), float(seg.get("end", 0.0))
        best_speaker = "UNKNOWN"
        best_overlap = 0.0
        for d in diarization_segments:
            ds, de = float(d.get("start", 0.0)), float(d.get("end", 0.0))
            overlap = max(0.0, min(te, de) - max(ts, ds))
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = str(d.get("speaker_id", "UNKNOWN"))
        annotated = dict(seg)
        annotated["speaker_id"] = best_speaker
        out.append(annotated)
    return out


@register_op
class AudioTranscribeDiarized(Operation):
    """Run audio.transcribe + audio.diarize and align speakers per segment."""

    name = "audio.transcribe_diarized"
    version = "1.0.0"
    input_kinds = (Kind.Audio,)
    output_kinds = (Kind.Transcript,)
    params_model = TranscribeDiarizedParams
    declared_resources = ("apple_neural_engine",)
    delegates_to = ("audio.transcribe", "audio.diarize")

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, TranscribeDiarizedParams)
        if len(inputs) != 1 or not isinstance(inputs[0], Audio):
            raise ValueError(
                f"audio.transcribe_diarized expects exactly one Audio input, "
                f"got {[a.kind for a in inputs]}"
            )
        if ctx.run_op is None:
            raise RuntimeError(
                "audio.transcribe_diarized requires ctx.run_op (call via "
                "Engine.run, not Operation.run directly)."
            )
        audio: Audio = inputs[0]

        transcribe_kwargs: dict[str, Any] = {
            "model": params.transcribe_model,
        }
        if params.language is not None:
            transcribe_kwargs["language"] = params.language
        if params.transcribe_backend is not None:
            transcribe_kwargs["backend"] = params.transcribe_backend

        diarize_kwargs: dict[str, Any] = {"model": params.diarize_model}
        if params.num_speakers is not None:
            diarize_kwargs["num_speakers"] = params.num_speakers
        if params.diarize_backend is not None:
            diarize_kwargs["backend"] = params.diarize_backend

        # Forward the sub-range to both sub-ops so each derives its own
        # content-addressed artifact id from the same time window.
        if params.start_s is not None:
            transcribe_kwargs["start_s"] = params.start_s
            diarize_kwargs["start_s"] = params.start_s
        if params.end_s is not None:
            transcribe_kwargs["end_s"] = params.end_s
            diarize_kwargs["end_s"] = params.end_s

        transcribe_outs = await ctx.run_op(
            "audio.transcribe", inputs=[audio.id], **transcribe_kwargs
        )
        diarize_outs = await ctx.run_op(
            "audio.diarize", inputs=[audio.id], **diarize_kwargs
        )
        transcript: Transcript = transcribe_outs[0]
        diarization: Diarization = diarize_outs[0]

        merged_segments = _align_speakers(
            transcript.metadata.get("segments", []),
            diarization.metadata.get("segments", []),
        )

        derived_id = compute_derived_artifact_id(
            kind=Kind.Transcript,
            op_name=self.name,
            op_version=self.version,
            backend_name=None,
            backend_version=None,
            params=params,
            input_ids=[audio.id, transcript.id, diarization.id],
        )
        payload: dict[str, Any] = {
            "text": transcript.metadata.get("text", ""),
            "segments": merged_segments,
            "language": transcript.metadata.get("language"),
            "model": transcript.metadata.get("model"),
            "diarization_model": diarization.metadata.get("model"),
            "num_speakers": diarization.metadata.get("num_speakers"),
        }
        import json
        tmp = ctx.workdir / f"transcript_diarized-{derived_id[:12]}.json"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        dest = ctx.storage.store_file(tmp, derived_id, ".json")
        tmp.unlink(missing_ok=True)

        return [
            Transcript(
                id=derived_id,
                path=dest,
                metadata=payload,
                derived_from=(audio.id, transcript.id, diarization.id),
                created_at=datetime.now(UTC),
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        if not inputs:
            return CostEstimate()
        audio = inputs[0]
        if isinstance(audio, Audio) and audio.duration is not None:
            return CostEstimate(local_seconds=audio.duration * 0.5 + 5.0)
        return CostEstimate(local_seconds=25.0)
