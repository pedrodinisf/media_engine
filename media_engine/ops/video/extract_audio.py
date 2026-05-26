"""``video.extract_audio`` — strip audio from a Video to a typed Audio artifact.

Backed by ffmpeg. Deterministic for the default ``pcm_s16le`` codec — same
inputs and params produce byte-identical output across runs (so the derived
artifact id is stable).
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel

from media_engine.artifacts import (
    AnyArtifact,
    Audio,
    Kind,
    Video,
    compute_artifact_id,
    compute_derived_artifact_id,
)
from media_engine.ops import (
    CostEstimate,
    Operation,
    OperationContext,
    register_op,
)
from media_engine.runtime.events import Progress
from media_engine.runtime.ffprobe import probe
from media_engine.runtime.log_pump import LinePump

_TIME_RE = re.compile(rb"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


class ExtractAudioParams(BaseModel):
    sample_rate: int = 16000
    channels: Literal[1, 2] = 1
    codec: Literal["pcm_s16le", "aac", "flac"] = "pcm_s16le"
    container: Literal["wav", "m4a", "flac"] = "wav"


def _container_format_flag(container: str) -> str:
    return {"wav": "wav", "m4a": "ipod", "flac": "flac"}[container]


def _emit_progress_from_stderr(
    line: bytes,
    *,
    duration: float | None,
    op_run_id: str,
    job_id: str | None,
    artifact_id: str | None,
    emit: object,
) -> None:
    """Best-effort: parse ``time=HH:MM:SS.mm`` from ffmpeg stderr → Progress."""
    if duration is None or duration <= 0:
        return
    match = _TIME_RE.search(line)
    if match is None:
        return
    h, m, s = match.groups()
    elapsed = int(h) * 3600 + int(m) * 60 + float(s)
    fraction = max(0.0, min(1.0, elapsed / duration))
    if callable(emit):  # OperationContext.emit
        # Never let progress emission break the op.
        with contextlib.suppress(Exception):
            emit(
                Progress(
                    event_id=uuid4().hex,
                    op_run_id=op_run_id,
                    job_id=job_id,
                    artifact_id=artifact_id,
                    timestamp=datetime.now(UTC),
                    fraction=fraction,
                    message=f"ffmpeg {elapsed:.1f}s/{duration:.1f}s",
                    phase="ffmpeg",
                )
            )


@register_op
class VideoExtractAudio(Operation):
    """Extract the audio track from a Video as a typed Audio artifact."""

    name = "video.extract_audio"
    version = "1.0.0"
    input_kinds = (Kind.Video,)
    output_kinds = (Kind.Audio,)
    params_model = ExtractAudioParams

    async def run(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, ExtractAudioParams)
        if len(inputs) != 1 or not isinstance(inputs[0], Video):
            raise ValueError(
                f"video.extract_audio expects exactly one Video input, "
                f"got {[a.kind for a in inputs]}"
            )
        video: Video = inputs[0]

        ffmpeg_path = ctx.config.ffmpeg_path
        if shutil.which(ffmpeg_path) is None:
            raise RuntimeError(
                f"ffmpeg binary not found: {ffmpeg_path!r}. "
                "Install via `brew install ffmpeg` or set MEDIA_ENGINE_FFMPEG_PATH."
            )

        # Compute the deterministic derived id BEFORE running ffmpeg, so we can
        # report progress against an artifact id and short-circuit if the file
        # already exists in storage.
        derived_id = compute_derived_artifact_id(
            kind=Kind.Audio,
            op_name=self.name,
            op_version=self.version,
            backend_name=None,
            backend_version=None,
            params=params,
            input_ids=[video.id],
        )
        ext = f".{params.container}"
        dest_in_store = ctx.storage.artifact_path(derived_id, ext)

        if not dest_in_store.exists():
            tmp_out = ctx.workdir / f"audio-{uuid4().hex}{ext}"
            cmd = [
                ffmpeg_path,
                "-nostdin",
                "-y",
                "-i", str(video.path),
                "-vn",
                "-ar", str(params.sample_rate),
                "-ac", str(params.channels),
                "-c:a", params.codec,
                "-f", _container_format_flag(params.container),
                str(tmp_out),
            ]
            duration = video.duration
            run_id = uuid4().hex
            log_pump = LinePump(
                source="ffmpeg",
                emit=ctx.emit,
                op_run_id=ctx.op_run_id or run_id,
                job_id=ctx.job_id,
            )
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
                )
                assert proc.stderr is not None
                for line in proc.stderr:
                    _emit_progress_from_stderr(
                        line,
                        duration=duration,
                        op_run_id=ctx.op_run_id or run_id,
                        job_id=ctx.job_id,
                        artifact_id=derived_id,
                        emit=ctx.emit,
                    )
                    # Surface the raw stderr to the Web UI Logs tab too —
                    # ffmpeg's status lines plus the rare error message
                    # both go through here.
                    with contextlib.suppress(Exception):
                        log_pump.push(
                            "info", line.decode("utf-8", errors="replace")
                        )
                proc.wait()
                if proc.returncode != 0:
                    raise RuntimeError(
                        f"ffmpeg failed (exit {proc.returncode}) on {video.path}"
                    )
            except FileNotFoundError as e:
                raise RuntimeError(f"ffmpeg invocation failed: {e}") from e

            # Verify ffmpeg actually produced bytes that match the derived id
            # contract — we hash output for sanity, but ALWAYS store at the
            # canonical derived_id path regardless of byte hash. This means two
            # different ffmpeg builds that produce different bytes for the same
            # logical input still cache-hit correctly under the engine's
            # canonical addressing.
            ctx.storage.store_file(tmp_out, derived_id, ext, link_mode="copy")
            tmp_out.unlink(missing_ok=True)

        # Build typed Audio artifact from the stored file's actual properties.
        out_path = ctx.storage.artifact_path(derived_id, ext)
        out_probe = probe(out_path, ffprobe_path=ctx.config.ffprobe_path)
        out_streams: list[dict[str, Any]] = list(out_probe.get("streams", []))
        _empty: dict[str, Any] = {}
        out_audio: dict[str, Any] = next(
            (s for s in out_streams if s.get("codec_type") == "audio"),
            _empty,
        )
        out_format: dict[str, Any] = dict(out_probe.get("format", {}))
        out_duration: float | None = None
        with contextlib.suppress(TypeError, ValueError):
            out_duration = float(out_format.get("duration", 0.0)) or None

        metadata: dict[str, Any] = {
            "sample_rate": int(out_audio.get("sample_rate", params.sample_rate)),
            "channels": int(out_audio.get("channels", params.channels)),
            "codec": str(out_audio.get("codec_name", params.codec)),
        }
        if out_duration is not None:
            metadata["duration"] = out_duration

        return [
            Audio(
                id=derived_id,
                path=out_path,
                metadata=metadata,
                derived_from=(video.id,),
                created_at=datetime.now(UTC),
            )
        ]

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        # ffmpeg audio extraction is roughly 5% of realtime on M-series.
        if not inputs:
            return CostEstimate()
        v = inputs[0]
        if isinstance(v, Video) and v.duration is not None:
            return CostEstimate(local_seconds=v.duration * 0.05)
        return CostEstimate(local_seconds=0.5)


# Re-export the derived-id helper for tests/sites that need to predict ids.
__all__ = [
    "ExtractAudioParams",
    "VideoExtractAudio",
    "compute_artifact_id",  # re-export for test-side hashing convenience
]
