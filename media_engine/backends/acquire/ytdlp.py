"""``yt-dlp`` backend for ``acquire.url``.

Subprocess wrapper around the ``yt-dlp`` CLI (the plan's chosen shape —
no Python import, so this module is trivially import-clean and registered
unconditionally). Progress lines (``[download]  NN.N%``) are parsed and
re-emitted as ``Progress`` events; the produced file is located by
globbing the per-job workdir, then handed to the shared
``build_acquired_video`` so the artifact id / store path / ffprobe
metadata match every other ``acquire.url`` backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import subprocess
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact
from media_engine.backends import (
    Backend,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.acquire.url import AcquireURLParams, build_acquired_video
from media_engine.runtime.events import Progress

BACKEND_NAME = "yt-dlp"
BACKEND_VERSION = "1.0.0"

_PCT_RE = re.compile(r"\[download\]\s+([\d.]+)%")


def _format_selector(quality: str) -> str:
    """Map the op's ``quality`` to a yt-dlp ``-f`` selector.

    ``best`` → best video+audio merged (fallback to best single file);
    anything else is passed through verbatim as a yt-dlp format string.
    """
    return "bv*+ba/b" if quality == "best" else quality


def _emit(ctx: OperationContext, run_id: str, fraction: float, message: str) -> None:
    with contextlib.suppress(Exception):
        ctx.emit(
            Progress(
                event_id=uuid4().hex,
                op_run_id=run_id,
                timestamp=datetime.now(UTC),
                fraction=max(0.0, min(1.0, fraction)),
                message=message,
                phase="yt-dlp",
            )
        )


def _download(
    *,
    url: str,
    quality: str,
    out_template: str,
    ctx: OperationContext,
    run_id: str,
) -> None:
    cmd = [
        "yt-dlp",
        "-f", _format_selector(quality),
        "--no-playlist",
        "--no-part",
        "--newline",
        "--progress",
        "-o", out_template,
        url,
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        del tail[:-20]
        m = _PCT_RE.search(line)
        if m:
            _emit(ctx, run_id, float(m.group(1)) / 100.0, "downloading")
    code = proc.wait()
    if code != 0:
        raise RuntimeError(
            f"yt-dlp failed (exit {code}) for {url!r}:\n" + "\n".join(tail[-10:])
        )


@register_backend
class YtdlpAcquireBackend(Backend):
    op_name = "acquire.url"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(binaries=["yt-dlp"])

    @classmethod
    def health(cls):  # type: ignore[override]
        return "ok" if shutil.which("yt-dlp") else "unavailable"

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, AcquireURLParams)
        if shutil.which("yt-dlp") is None:
            raise RuntimeError(
                "yt-dlp binary not found. Install via "
                "`uv sync --extra acquire-url` (or `pipx install yt-dlp`)."
            )

        run_id = uuid4().hex
        scratch = ctx.workdir / f"ytdlp-{run_id}"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            _emit(ctx, run_id, 0.0, "starting yt-dlp")
            await asyncio.to_thread(
                _download,
                url=params.url,
                quality=params.quality,
                out_template=str(scratch / "dl.%(ext)s"),
                ctx=ctx,
                run_id=run_id,
            )
            produced = sorted(p for p in scratch.iterdir() if p.is_file())
            if not produced:
                raise RuntimeError(
                    f"yt-dlp produced no file for {params.url!r}"
                )
            # Prefer a real media container over sidecars yt-dlp may leave.
            media = next(
                (p for p in produced if p.suffix not in {".json", ".txt", ".vtt"}),
                produced[0],
            )
            _emit(ctx, run_id, 1.0, "downloaded")
            video = build_acquired_video(
                params=params,
                backend_name=self.name,
                backend_version=self.version,
                downloaded_path=media,
                ctx=ctx,
                source_url=params.url,
            )
            return [video]
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        return CostEstimate(local_seconds=30.0)


__all__ = ["BACKEND_NAME", "BACKEND_VERSION", "YtdlpAcquireBackend"]
