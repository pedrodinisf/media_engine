"""Phase 6 commit 41 — multipart upload + URL-probe REST endpoints.

The Web UI's Ingest panel needs two affordances the existing CLI surface
doesn't expose over HTTP:

- ``POST /acquire/upload`` — stream a local file as multipart, run
  ffprobe + classify, then either return the typed preview (``commit=False``)
  or kick the ``acquire.upload`` job (``commit=True``). Bypasses the
  POST /run path because the bytes live in the request body, not in the
  cache yet.
- ``POST /acquire/url/probe`` — call yt-dlp's ``--dump-single-json`` to
  fetch a URL's metadata (title, duration, thumbnail) without
  downloading. Lets the URL tab show "yes this resolves; here's what
  you'll get" before the user commits.

Both endpoints are bearer-gated and scope to ``token.namespace``. The
upload streams in 64 KB chunks and enforces ``config.max_upload_mb`` —
larger bodies abort with 413 before the engine sees them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, HttpUrl

from media_engine.api._state import AppState
from media_engine.api.jobs import submit_run_op
from media_engine.api.routes import JobAck, get_state, require_token
from media_engine.artifacts import Kind
from media_engine.runtime.cache import ApiTokenInfo
from media_engine.runtime.ffprobe import classify, probe

router = APIRouter()


# ─────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────


class UploadPreview(BaseModel):
    """Pre-commit summary returned when ``commit=False`` on /acquire/upload."""

    kind: str = Field(description="Resolved artifact kind (video/audio/image)")
    duration_s: float | None = None
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    size_bytes: int
    sha256_prefix: str = Field(description="First 16 hex chars of the file sha256")


class URLProbeRequest(BaseModel):
    url: HttpUrl


class URLProbeResponse(BaseModel):
    title: str | None = None
    duration_s: float | None = None
    uploader: str | None = None
    thumbnail_url: str | None = None
    formats_available: int = 0
    resolvable: bool = True
    reason: str | None = Field(
        default=None,
        description="When resolvable=False, a human-readable hint.",
    )


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


async def _stream_upload_to_tmp(
    upload: UploadFile,
    workdir: Path,
    *,
    max_bytes: int,
) -> tuple[Path, int, str]:
    """Stream the multipart body to a temp file with size + sha bookkeeping.

    Returns ``(path, size_bytes, sha256_prefix)``. Aborts with 413 the
    moment we cross ``max_bytes`` — never holds the whole file in memory.
    """
    import hashlib

    workdir.mkdir(parents=True, exist_ok=True)
    suffix = Path(upload.filename or "upload").suffix
    tmp_path = workdir / f"upload-{uuid4().hex}{suffix}"
    hasher = hashlib.sha256()
    size = 0
    with tmp_path.open("wb") as out:
        while True:
            chunk = await upload.read(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                # Clean up the partial file before raising so we don't
                # leave half-written garbage in the workdir.
                out.close()
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=(
                        f"upload exceeds {max_bytes // (1024 * 1024)} MiB "
                        f"(MEDIA_ENGINE_MAX_UPLOAD_MB)"
                    ),
                )
            hasher.update(chunk)
            out.write(chunk)
    return tmp_path, size, hasher.hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────
# POST /acquire/upload
# ─────────────────────────────────────────────────────────────────


@router.post(
    "/acquire/upload",
    response_model=UploadPreview | JobAck,
)
async def post_acquire_upload(
    state: Annotated[AppState, Depends(get_state)],
    token: Annotated[ApiTokenInfo, Depends(require_token)],
    file: Annotated[UploadFile, File(description="The local file to ingest")],
    commit: Annotated[bool, Form()] = True,
) -> UploadPreview | JobAck:
    """Stream a local file in, optionally submitting an ``acquire.upload`` job.

    Two modes:

    - ``commit=False`` → returns ``UploadPreview`` (kind + ffprobe summary +
      sha-prefix). UI uses this to show the user what they're about to
      ingest before they say go.
    - ``commit=True`` → also submits an ``acquire.upload`` job and returns
      ``JobAck { job_id }``; the file is moved into the workdir the op
      will read from, then deleted by the op's idempotent store_file.
    """
    max_bytes = state.engine.config.max_upload_mb * 1024 * 1024
    upload_workdir = state.engine.storage.ensure_workdir(
        f"upload-{token.id}-{uuid4().hex[:8]}"
    )
    tmp_path, size, sha_prefix = await _stream_upload_to_tmp(
        file, upload_workdir, max_bytes=max_bytes
    )

    # Probe + classify — the probe call validates that the bytes are
    # actually a media file (else we 400 before persisting).
    try:
        probe_data = probe(tmp_path, ffprobe_path=state.engine.config.ffprobe_path)
        kind = classify(probe_data)
    except Exception as e:  # noqa: BLE001 — surface as 400
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        with contextlib.suppress(OSError):
            tmp_path.parent.rmdir()
        raise HTTPException(
            status_code=400,
            detail=f"could not probe upload as media: {e}",
        ) from None

    fmt = probe_data.get("format", {})
    streams = probe_data.get("streams", [])
    duration_s: float | None = None
    if "duration" in fmt:
        with contextlib.suppress(TypeError, ValueError):
            duration_s = float(fmt["duration"])

    width: int | None = None
    height: int | None = None
    codec: str | None = None
    if kind in {Kind.Video, Kind.Image}:
        v = next((s for s in streams if s.get("codec_type") == "video"), None)
        if v is not None:
            width = int(v["width"]) if "width" in v else None
            height = int(v["height"]) if "height" in v else None
            codec = str(v.get("codec_name") or "") or None
    elif kind is Kind.Audio:
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if a is not None:
            codec = str(a.get("codec_name") or "") or None

    preview = UploadPreview(
        kind=kind.value,
        duration_s=duration_s,
        codec=codec,
        width=width,
        height=height,
        size_bytes=size,
        sha256_prefix=sha_prefix,
    )

    if not commit:
        # Preview-only — the tmp file is no longer needed; the user is
        # expected to commit (= upload again) or walk away. We delete to
        # avoid double-spending the disk-guard budget.
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        with contextlib.suppress(OSError):
            tmp_path.parent.rmdir()
        return preview

    # Commit path — fire acquire.upload against the tmp file. The op's
    # store_file is idempotent so a re-upload with the same bytes hits
    # the cache. We pass the original filename through so the artifact's
    # metadata.original_filename survives.
    #
    # link_mode=copy: the workdir tmp file lives on the API process's
    # filesystem, but the permanent_store may be on a different volume
    # (default /Volumes/UNIVERSE_V/MEDIA/...). A hardlink requires the
    # same filesystem, so we copy unconditionally — the tmp file is
    # cleaned up by the workdir GC sweep regardless.
    job_id = submit_run_op(
        state,
        op_name="acquire.upload",
        inputs=[],
        backend=None,
        params={
            "source_path": str(tmp_path),
            "original_filename": file.filename or None,
            "link_mode": "copy",
        },
        namespace=token.namespace,
    )
    return JobAck(job_id=job_id)


# ─────────────────────────────────────────────────────────────────
# POST /acquire/url/probe
# ─────────────────────────────────────────────────────────────────


def _yt_dlp_dump(url: str) -> tuple[dict[str, Any] | None, str | None]:
    """Run ``yt-dlp --dump-single-json``. Returns ``(info, error_message)``."""
    import shutil
    import subprocess

    binary = shutil.which("yt-dlp")
    if binary is None:
        return None, "yt-dlp not installed (uv sync --extra acquire-url)"
    try:
        proc = subprocess.run(  # noqa: S603 — argv-form, no shell
            [
                binary,
                "--dump-single-json",
                "--no-warnings",
                "--no-playlist",
                "--simulate",
                url,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return None, "yt-dlp timed out (15 s) — the site may be slow or blocked"
    except subprocess.CalledProcessError as e:
        stderr_tail = (e.stderr or "").strip().splitlines()[-1:]
        return None, f"yt-dlp failed: {''.join(stderr_tail) or e.returncode}"
    try:
        data: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return None, f"yt-dlp emitted non-JSON: {e}"
    return data, None


@router.post("/acquire/url/probe", response_model=URLProbeResponse)
async def post_acquire_url_probe(
    body: URLProbeRequest,
    _state: Annotated[AppState, Depends(get_state)],
    _token: Annotated[ApiTokenInfo, Depends(require_token)],
) -> URLProbeResponse:
    """Resolve a URL via yt-dlp's metadata-only probe.

    No bytes hit the disk. Returns title + duration + uploader so the
    UI can confirm "yes, this is the right thing" before committing
    via ``POST /run op=acquire.url``.
    """
    info, err = await asyncio.to_thread(_yt_dlp_dump, str(body.url))
    if info is None:
        return URLProbeResponse(resolvable=False, reason=err)
    duration = info.get("duration")
    return URLProbeResponse(
        title=info.get("title"),
        duration_s=float(duration) if isinstance(duration, (int, float)) else None,
        uploader=info.get("uploader") or info.get("channel"),
        thumbnail_url=info.get("thumbnail"),
        formats_available=len(info.get("formats") or []),
        resolvable=True,
    )
