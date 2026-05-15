"""``audio.diarize`` — Audio → Diarization (who-spoke-when).

Default backend: ``pyannote`` (pyannote.audio 3.1, MPS-accelerated on Apple
Silicon). Output Diarization carries ``segments: list[{start, end,
speaker_id}]`` and ``num_speakers``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    Audio,
    Diarization,
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


class DiarizeParams(BaseModel):
    num_speakers: int | None = None
    min_speakers: int | None = None
    max_speakers: int | None = None
    model: str = "pyannote/speaker-diarization-3.1"


@register_op
class AudioDiarize(Operation):
    """Identify who spoke when in an Audio artifact."""

    name = "audio.diarize"
    version = "1.0.0"
    input_kinds = (Kind.Audio,)
    output_kinds = (Kind.Diarization,)
    params_model = DiarizeParams
    declared_resources = ("apple_neural_engine",)
    default_backend = "pyannote"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, DiarizeParams)
        if len(inputs) != 1 or not isinstance(inputs[0], Audio):
            raise ValueError(
                f"audio.diarize expects exactly one Audio input, "
                f"got {[a.kind for a in inputs]}"
            )
        audio: Audio = inputs[0]
        backend_name = self.default_backend
        if backend_name is None:
            raise RuntimeError(f"{self.name} has no default backend")
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
            # pyannote on MPS ~ 0.2× real-time after warmup.
            return CostEstimate(local_seconds=audio.duration * 0.2 + 5.0)
        return CostEstimate(local_seconds=15.0)


def build_diarization_artifact(
    *,
    audio: Audio,
    params: DiarizeParams,
    backend_name: str,
    backend_version: str,
    workdir_path: Any,
    storage: Any,
    segments: list[dict[str, Any]],
    num_speakers: int,
    model: str,
) -> Diarization:
    """Materialize the Diarization JSON artifact."""
    import json

    derived_id = compute_derived_artifact_id(
        kind=Kind.Diarization,
        op_name="audio.diarize",
        op_version="1.0.0",
        backend_name=backend_name,
        backend_version=backend_version,
        params=params,
        input_ids=[audio.id],
    )
    payload: dict[str, Any] = {
        "segments": segments,
        "num_speakers": num_speakers,
        "model": model,
    }
    tmp_path = workdir_path / f"diarization-{derived_id[:12]}.json"
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    dest = storage.store_file(tmp_path, derived_id, ".json")
    tmp_path.unlink(missing_ok=True)

    return Diarization(
        id=derived_id,
        path=dest,
        metadata=payload,
        derived_from=(audio.id,),
        created_at=datetime.now(UTC),
    )
