"""``audio.detect_language`` — cheap Whisper language probe.

Runs the same backend as ``audio.transcribe`` but on a short prefix of the
audio (the underlying Whisper backends typically expose a fast
``detect_language`` codepath that skips full decoding). Output:
``Analysis`` with ``data = {"language": "en", "confidence": 0.97,
"alternatives": {"en": 0.97, "de": 0.02, ...}}``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field, model_validator

from media_engine.artifacts import (
    Analysis,
    AnyArtifact,
    Audio,
    Kind,
    compute_derived_artifact_id,
)
from media_engine.backends import BackendRegistry
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)
from media_engine.ops.audio._models import (
    ASSEMBLYAI_MODELS,
    WHISPER_MODELS,
    assemblyai_cost_cents,
    is_assemblyai_model,
)


class DetectLanguageParams(BaseModel):
    model: Annotated[
        str,
        Field(json_schema_extra={"enum": [*WHISPER_MODELS, *ASSEMBLYAI_MODELS]}),
    ] = "mlx-community/whisper-large-v3-mlx"
    # Optional time-window slicing. Whisper's language detector only
    # looks at the first ~30 s of input, so the most common reason to
    # set these is "skip the silent intro" or "detect on a specific
    # segment of a multilingual recording". Same `-ss start` / `-t
    # duration` slicing as the transcribe path — handled inside the
    # mlx-whisper backend via ``maybe_slice_audio``.
    start_s: float | None = Field(default=None, ge=0.0)
    end_s: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _check_range(self) -> DetectLanguageParams:
        if (
            self.start_s is not None
            and self.end_s is not None
            and self.end_s <= self.start_s
        ):
            raise ValueError(
                f"end_s must be > start_s "
                f"(got start={self.start_s}, end={self.end_s})"
            )
        return self


@register_op
class AudioDetectLanguage(Operation):
    """Detect the spoken language of an Audio artifact (cheap, no decode)."""

    name = "audio.detect_language"
    version = "1.0.0"
    input_kinds = (Kind.Audio,)
    output_kinds = (Kind.Analysis,)
    params_model = DetectLanguageParams
    declared_resources = ("apple_neural_engine",)
    default_backend = "mlx-whisper"

    @staticmethod
    def _backend_for_model(model: str) -> str:
        return "assemblyai" if is_assemblyai_model(model) else "mlx-whisper"

    def select_backend(self, params: BaseModel) -> str | None:
        assert isinstance(params, DetectLanguageParams)
        return self._backend_for_model(params.model)

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, DetectLanguageParams)
        if len(inputs) != 1 or not isinstance(inputs[0], Audio):
            raise ValueError(
                f"audio.detect_language expects exactly one Audio input, "
                f"got {[a.kind for a in inputs]}"
            )
        audio: Audio = inputs[0]

        backend_name = ctx.backend or self._backend_for_model(params.model)
        backend_cls = BackendRegistry.get(self.name, backend_name)
        backend = backend_cls()
        return await backend.execute([audio], params, ctx)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        assert isinstance(params, DetectLanguageParams)
        if is_assemblyai_model(params.model) and inputs:
            audio = inputs[0]
            duration = audio.duration if isinstance(audio, Audio) else None
            # AssemblyAI has no language-only endpoint — it transcribes.
            return CostEstimate(
                cloud_cents=assemblyai_cost_cents(params.model, duration, diarize=False)
            )
        # Local detect runs on the first ~30 s of audio; cheap.
        return CostEstimate(local_seconds=2.0)


def build_detect_language_artifact(
    *,
    audio: Audio,
    params: DetectLanguageParams,
    backend_name: str,
    backend_version: str,
    workdir_path: Any,
    storage: Any,
    language: str,
    confidence: float,
    alternatives: dict[str, float],
) -> Analysis:
    """Materialize the Analysis artifact carrying the detect-language result."""
    import json

    derived_id = compute_derived_artifact_id(
        kind=Kind.Analysis,
        op_name="audio.detect_language",
        op_version="1.0.0",
        backend_name=backend_name,
        backend_version=backend_version,
        params=params,
        input_ids=[audio.id],
    )
    payload = {
        "data": {
            "language": language,
            "confidence": confidence,
            "alternatives": alternatives,
        },
        "model": params.model,
    }
    tmp_path = workdir_path / f"detect-{derived_id[:12]}.json"
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    dest = storage.store_file(tmp_path, derived_id, ".json")
    tmp_path.unlink(missing_ok=True)

    return Analysis(
        id=derived_id,
        path=dest,
        metadata=payload,
        derived_from=(audio.id,),
        created_at=datetime.now(UTC),
    )
