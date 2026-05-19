"""``acquire.livestream`` — record a live HLS stream into segment Videos.

Ports davos ``grab_video.py`` live mode. The page (or direct ``.m3u8``)
is recorded by the ``ffmpeg-recorder`` backend; the recording is split
into one or more ``Video`` artifacts on a fixed clock (``segment_seconds``),
a manual boundary request (``Cmd+Shift+J`` / ``SIGUSR1`` via
``med acquire-live``), stream end, or ``max_duration_sec``.

Identity follows the ``acquire.url`` rule: each segment's id is the
*derived* id over ``{url, quality, max_duration_sec, segment_seconds}``
plus the segment index — not the recorded bytes (a live stream is never
byte-reproducible). Re-running with identical params is therefore an
engine cache hit returning the prior recording; change a param (or the
op version) to force a fresh capture. This is the same deliberate
trade-off as commit 23 (see ``acquire/url.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    Kind,
    Video,
    compute_derived_artifact_id,
)
from media_engine.backends import BackendRegistry
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)
from media_engine.ops.acquire.upload import extract_probe_metadata
from media_engine.runtime.ffprobe import classify, probe

OP_NAME = "acquire.livestream"
OP_VERSION = "1.0.0"


class AcquireLivestreamParams(BaseModel):
    url: str
    quality: str = "best"
    max_duration_sec: int | None = None
    segment_seconds: int | None = None


@register_op
class AcquireLivestream(Operation):
    """Record a live HLS stream into one or more segment Video artifacts."""

    name = OP_NAME
    version = OP_VERSION
    input_kinds = ()
    output_kinds = (Kind.Video,)
    params_model = AcquireLivestreamParams
    default_backend = "ffmpeg-recorder"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, AcquireLivestreamParams)
        if inputs:
            raise ValueError(
                f"acquire.livestream takes no inputs, "
                f"got {[a.kind for a in inputs]}"
            )
        backend_name = ctx.backend or self.default_backend
        if backend_name is None:
            raise RuntimeError(
                f"{self.name} has no backend; pass `backend=` to Engine.run."
            )
        backend_cls = BackendRegistry.get(self.name, backend_name)
        return await backend_cls().execute([], params, ctx)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        assert isinstance(params, AcquireLivestreamParams)
        # If the caller bounded the recording we can estimate wall time;
        # otherwise it runs until the stream ends (unknowable up front).
        if params.max_duration_sec is not None:
            return CostEstimate(local_seconds=float(params.max_duration_sec))
        return CostEstimate(local_seconds=60.0)


def build_segment_video(
    *,
    params: AcquireLivestreamParams,
    backend_name: str,
    backend_version: str,
    segment_index: int,
    segment_path: Path,
    ctx: OperationContext,
    source_url: str,
) -> Video:
    """Persist one recorded segment as a content-addressed ``Video``.

    The segment index is folded into the derived-id params so each
    segment of a recording gets a distinct id while the whole recording
    still keys deterministically off the op params.
    """
    id_params = {
        **params.model_dump(mode="json"),
        "_segment_index": segment_index,
    }
    derived_id = compute_derived_artifact_id(
        kind=Kind.Video,
        op_name=OP_NAME,
        op_version=OP_VERSION,
        backend_name=backend_name,
        backend_version=backend_version,
        params=id_params,
        input_ids=[],
    )
    ext = segment_path.suffix or ".mp4"
    dest = ctx.storage.store_file(segment_path, derived_id, ext)

    metadata: dict[str, Any] = {
        "url": source_url,
        "live": True,
        "segment_index": segment_index,
    }
    try:
        probe_data = probe(dest, ffprobe_path=ctx.config.ffprobe_path)
        kind = classify(probe_data)
        metadata.update(extract_probe_metadata(probe_data, kind))
    except Exception:
        pass

    return Video(
        id=derived_id,
        path=dest,
        metadata=metadata,
        created_at=datetime.now(UTC),
    )
