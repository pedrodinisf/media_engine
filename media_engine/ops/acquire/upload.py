"""``acquire.upload`` — register a local file as a typed Artifact.

Streams sha256 → ffprobe → classify → store in content-addressed permanent
store → return the typed artifact (Video / Audio / Image). Idempotent:
re-uploading the same file returns the same id and uses the existing on-disk
copy.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    Audio,
    Image,
    Kind,
    Video,
    compute_artifact_id,
)
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)
from media_engine.runtime.ffprobe import classify, probe


class AcquireUploadParams(BaseModel):
    source_path: Path
    original_filename: str | None = None
    link_mode: Literal["copy", "hardlink"] = "copy"


def _extract_metadata(probe_data: dict[str, Any], kind: Kind) -> dict[str, Any]:
    """Pull common fields from ffprobe JSON into the artifact metadata dict."""
    fmt = probe_data.get("format", {})
    streams = probe_data.get("streams", [])
    md: dict[str, Any] = {}

    if "duration" in fmt:
        with contextlib.suppress(TypeError, ValueError):
            md["duration"] = float(fmt["duration"])

    if kind in {Kind.Video, Kind.Image}:
        v = next((s for s in streams if s.get("codec_type") == "video"), None)
        if v is not None:
            if "width" in v:
                md["width"] = int(v["width"])
            if "height" in v:
                md["height"] = int(v["height"])
            if "codec_name" in v:
                md["codec"] = str(v["codec_name"])
            if "r_frame_rate" in v and isinstance(v["r_frame_rate"], str):
                num, _, den = v["r_frame_rate"].partition("/")
                try:
                    fps = float(num) / float(den) if den and den != "0" else float(num)
                    md["fps"] = fps
                except (ValueError, ZeroDivisionError):
                    pass

    if kind in {Kind.Video, Kind.Audio}:
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if a is not None:
            if "sample_rate" in a:
                md["sample_rate"] = int(a["sample_rate"])
            if "channels" in a:
                md["channels"] = int(a["channels"])
            if kind is Kind.Audio and "codec_name" in a:
                md["codec"] = str(a["codec_name"])

    return md


def _construct_artifact(
    *, sha: str, dest: Path, kind: Kind, metadata: dict[str, Any]
) -> AnyArtifact:
    now = datetime.now(UTC)
    if kind is Kind.Video:
        return Video(id=sha, path=dest, metadata=metadata, created_at=now)
    if kind is Kind.Audio:
        return Audio(id=sha, path=dest, metadata=metadata, created_at=now)
    if kind is Kind.Image:
        return Image(id=sha, path=dest, metadata=metadata, created_at=now)
    raise ValueError(f"acquire.upload does not produce {kind!r}")


@register_op
class AcquireUpload(Operation):
    """Ingest a local file into the content-addressed store."""

    name = "acquire.upload"
    version = "1.0.0"
    input_kinds = ()
    output_kinds = (Kind.Video, Kind.Audio, Kind.Image)
    params_model = AcquireUploadParams

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, AcquireUploadParams)
        src = params.source_path
        if not src.exists():
            raise FileNotFoundError(src)

        # 1. Hash → content address.
        sha = compute_artifact_id(src)

        # 2. Probe + classify.
        probe_data = probe(src, ffprobe_path=ctx.config.ffprobe_path)
        kind = classify(probe_data)

        # 3. Store atomically; idempotent if file already present.
        ext = src.suffix
        dest = ctx.storage.store_file(src, sha, ext, link_mode=params.link_mode)

        # 4. Build typed artifact.
        metadata = _extract_metadata(probe_data, kind)
        if params.original_filename is not None:
            metadata["original_filename"] = params.original_filename
        return [_construct_artifact(sha=sha, dest=dest, kind=kind, metadata=metadata)]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        assert isinstance(params, AcquireUploadParams)
        try:
            size_mb = params.source_path.stat().st_size / (1024 * 1024)
        except OSError:
            size_mb = 0.0
        # Streaming hash + ffprobe + filesystem ops scale roughly with size.
        return CostEstimate(local_seconds=size_mb / 200.0)
