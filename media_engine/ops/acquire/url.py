"""``acquire.url`` — fetch a remote video by URL into the typed store.

Two backends: ``yt-dlp`` (default — handles YouTube and the long tail of
sites yt-dlp supports) and ``playwright-hls`` (headless Chromium that
sniffs the ``.m3u8`` a page streams, then ffmpeg stream-copies it —
ported from davos ``grab_video.py``; use ``--backend playwright-hls`` for
sites yt-dlp can't crack).

Identity is the *derived* id over ``{url, quality}`` + backend (not the
downloaded bytes — those aren't reproducible across yt-dlp / network /
time). So re-acquiring the same URL is a cache hit and downstream
reanalysis (change the profile, same URL) reuses the upstream artifact.
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

OP_NAME = "acquire.url"
OP_VERSION = "1.0.0"


class AcquireURLParams(BaseModel):
    url: str
    quality: str = "best"


@register_op
class AcquireURL(Operation):
    """Download a remote video by URL into the content-addressed store."""

    name = OP_NAME
    version = OP_VERSION
    input_kinds = ()
    output_kinds = (Kind.Video,)
    params_model = AcquireURLParams
    default_backend = "yt-dlp"

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, AcquireURLParams)
        if inputs:
            raise ValueError(
                f"acquire.url takes no inputs, got {[a.kind for a in inputs]}"
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
        # Network + container remux — duration unknown pre-fetch.
        return CostEstimate(local_seconds=30.0)


def build_acquired_video(
    *,
    params: AcquireURLParams,
    backend_name: str,
    backend_version: str,
    downloaded_path: Path,
    ctx: OperationContext,
    source_url: str,
    title: str | None = None,
) -> Video:
    """Persist a freshly downloaded file as a content-addressed ``Video``.

    Shared by every ``acquire.url`` backend so the derived id, store path,
    and ffprobe-populated metadata are computed identically regardless of
    which backend fetched the bytes. The id is keyed on ``(url, quality)``
    + backend, never the bytes — see this module's docstring.
    """
    derived_id = compute_derived_artifact_id(
        kind=Kind.Video,
        op_name=OP_NAME,
        op_version=OP_VERSION,
        backend_name=backend_name,
        backend_version=backend_version,
        params=params,
        input_ids=[],
    )
    ext = downloaded_path.suffix or ".mp4"
    dest = ctx.storage.store_file(downloaded_path, derived_id, ext)

    metadata: dict[str, Any] = {"url": source_url}
    try:
        probe_data = probe(dest, ffprobe_path=ctx.config.ffprobe_path)
        kind = classify(probe_data)
        metadata.update(extract_probe_metadata(probe_data, kind))
    except Exception:
        # A non-probeable download still yields a usable artifact; downstream
        # ops that need duration/codec will surface their own error.
        pass
    if title:
        metadata["title"] = title

    return Video(
        id=derived_id,
        path=dest,
        metadata=metadata,
        created_at=datetime.now(UTC),
    )
