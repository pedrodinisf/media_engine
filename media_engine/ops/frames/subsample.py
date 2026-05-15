"""``frames.subsample`` — uniformly reduce a FrameSet to ≤ ``max_n`` frames.

Ports framepulse's ``_subsample_frames`` (uniform stride) into the engine's
typed-artifact world. Preserves the original frame indices in metadata so
downstream ops can recover timestamps from the parent FrameSet's fps.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    FrameSet,
    Kind,
    compute_derived_artifact_id,
)
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)


class SubsampleParams(BaseModel):
    max_n: int = 30


def _uniform_indices(count: int, max_n: int) -> list[int]:
    """Pick ``min(count, max_n)`` evenly-spaced indices from ``range(count)``."""
    if count <= max_n:
        return list(range(count))
    if max_n <= 1:
        return [0] if max_n == 1 else []
    step = (count - 1) / (max_n - 1)
    return [round(i * step) for i in range(max_n)]


@register_op
class FramesSubsample(Operation):
    """Reduce a FrameSet to ≤ max_n frames via uniform stride."""

    name = "frames.subsample"
    version = "1.0.0"
    input_kinds = (Kind.FrameSet,)
    output_kinds = (Kind.FrameSet,)
    params_model = SubsampleParams

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, SubsampleParams)
        if len(inputs) != 1 or not isinstance(inputs[0], FrameSet):
            raise ValueError(
                f"frames.subsample expects exactly one FrameSet input, "
                f"got {[a.kind for a in inputs]}"
            )
        if params.max_n < 0:
            raise ValueError(f"max_n must be >= 0 (got {params.max_n})")
        frameset: FrameSet = inputs[0]

        frame_ids = list(frameset.metadata.get("frame_ids", []))
        original_indices = list(
            frameset.metadata.get("original_indices", range(len(frame_ids)))
        )
        if len(original_indices) != len(frame_ids):
            original_indices = list(range(len(frame_ids)))

        chosen = _uniform_indices(len(frame_ids), params.max_n)
        kept_ids = [frame_ids[i] for i in chosen]
        kept_original = [original_indices[i] for i in chosen]

        derived_id = compute_derived_artifact_id(
            kind=Kind.FrameSet,
            op_name=self.name,
            op_version=self.version,
            backend_name=None,
            backend_version=None,
            params=params,
            input_ids=[frameset.id],
        )
        # FrameSet path is a directory in storage. We don't move the actual
        # frame files (they're already in the cache via their own ids); we
        # just store a manifest JSON describing the subset.
        import json
        manifest_payload: dict[str, Any] = {
            "frame_ids": kept_ids,
            "original_indices": kept_original,
            "fps": frameset.metadata.get("fps"),
            "parent_frameset_id": frameset.id,
        }
        tmp = ctx.workdir / f"frameset-{derived_id[:12]}.json"
        tmp.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2))
        dest = ctx.storage.store_file(tmp, derived_id, ".json")
        tmp.unlink(missing_ok=True)

        return [
            FrameSet(
                id=derived_id,
                path=dest,
                metadata=manifest_payload,
                derived_from=(frameset.id,),
                created_at=datetime.now(UTC),
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(local_seconds=0.1)
