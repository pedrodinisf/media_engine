"""``ffmpeg-recorder`` backend for ``acquire.livestream``.

Ports davos ``grab_video.py`` live mode: a chain of ffmpeg
stream-copy processes, one per segment. A segment boundary is hit on a
fixed clock (``segment_seconds``), a manual request
(``LiveSegmentController.request_split()`` — driven by ``SIGUSR1`` /
``Cmd+Shift+J`` from ``med acquire-live``), the stream ending, or
``max_duration_sec``.

The stream URL is either the page's ``.m3u8`` directly, or sniffed by
reusing the ``playwright-hls`` backend's headless-Chromium helper
(realizing the charter's "playwright-hls + ffmpeg-recorder" pipeline in
one backend; playwright stays a lazy optional dep).

``LiveSegmentController`` is thread-safe (the recording loop runs in
``asyncio.to_thread``); a process-global registry lets the CLI main
thread request a split into the worker thread without signals crossing
threads.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from media_engine.artifacts import AnyArtifact
from media_engine.backends import (
    Backend,
    BackendRequirements,
    register_backend,
)
from media_engine.ops import CostEstimate, OperationContext
from media_engine.ops.acquire.livestream import (
    AcquireLivestreamParams,
    build_segment_video,
)
from media_engine.runtime.events import Progress

BACKEND_NAME = "ffmpeg-recorder"
BACKEND_VERSION = "1.0.0"

_MIN_SEGMENT_BYTES = 1000


class LiveSegmentController:
    """Thread-safe split/stop signaling for a running recording.

    Ported (slimmed) from davos ``LiveSegmentController`` — the keyboard
    listener lives in the CLI now, not here.
    """

    def __init__(self) -> None:
        self._split = threading.Event()
        self._stop = threading.Event()
        self.segment_count = 0

    def request_split(self) -> None:
        if not self._stop.is_set():
            self._split.set()
            self.segment_count += 1

    def should_split(self) -> bool:
        return self._split.is_set()

    def clear_split(self) -> None:
        self._split.clear()

    def stop(self) -> None:
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()


_ACTIVE_LOCK = threading.Lock()
_ACTIVE: list[LiveSegmentController] = []


def _register_active(c: LiveSegmentController) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.append(c)


def _unregister_active(c: LiveSegmentController) -> None:
    with _ACTIVE_LOCK, contextlib.suppress(ValueError):
        _ACTIVE.remove(c)


def request_split_all() -> int:
    """Request a segment boundary on every active recorder.

    Called by ``med acquire-live`` from the main thread (SIGUSR1 handler
    or pynput hotkey). Returns the number of recorders signalled.
    """
    with _ACTIVE_LOCK:
        active = list(_ACTIVE)
    for c in active:
        c.request_split()
    return len(active)


def _resolve_stream_url(url: str) -> str:
    """Direct ``.m3u8`` passes through; otherwise sniff via playwright."""
    if ".m3u8" in url:
        return url
    from media_engine.backends.acquire.playwright_hls import sniff_m3u8

    stream_url, _title = sniff_m3u8(
        url, nav_timeout_ms=30000, settle_ms=15000
    )
    if not stream_url:
        raise RuntimeError(
            f"No HLS stream found at {url!r} "
            "(login/geo-restricted, or not a livestream page)."
        )
    return stream_url


def _emit(ctx: OperationContext, run_id: str, fraction: float, message: str) -> None:
    with contextlib.suppress(Exception):
        ctx.emit(
            Progress(
                event_id=uuid4().hex,
                op_run_id=run_id,
                timestamp=datetime.now(UTC),
                fraction=max(0.0, min(1.0, fraction)),
                message=message,
                phase="ffmpeg-recorder",
            )
        )


def _run_ffmpeg_session(
    *,
    ffmpeg_path: str,
    stream_url: str,
    out_dir: Path,
    segment_seconds: float | None,
    max_seconds: float | None,
    controller: LiveSegmentController,
) -> tuple[int, bool, list[Path]]:
    """One ffmpeg invocation = one recording "session" between user actions.

    With ``segment_seconds`` set the segment muxer produces N output files
    in a single ffmpeg process (correct on a finite VOD playlist *and* a
    live sliding window — never re-reads from the start). Without it,
    ffmpeg writes a single output file until ``-t`` expires, the stream
    ends, or the user requests a split / stop (we SIGINT to finalize).

    Returns (ffmpeg_returncode, split_requested, produced_files).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    base_cmd = [
        ffmpeg_path,
        "-nostdin", "-y",
        "-hide_banner", "-loglevel", "error",
        "-progress", "pipe:1", "-nostats",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        "-multiple_requests", "1",
        "-http_seekable", "0",
    ]
    if max_seconds is not None:
        base_cmd += ["-t", f"{max_seconds:.3f}"]
    base_cmd += ["-i", stream_url, "-c", "copy", "-bsf:a", "aac_adtstoasc"]

    if segment_seconds is not None:
        cmd = base_cmd + [
            "-f", "segment",
            "-segment_time", f"{segment_seconds:.3f}",
            "-segment_format", "mp4",
            "-reset_timestamps", "1",
            str(out_dir / "seg_%04d.mp4"),
        ]
    else:
        cmd = base_cmd + ["-movflags", "+faststart", str(out_dir / "seg.mp4")]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    split_requested = False
    assert proc.stdout is not None
    for line in proc.stdout:
        if controller.stopped() or controller.should_split():
            split_requested = controller.should_split()
            with contextlib.suppress(Exception):
                proc.send_signal(signal.SIGINT)
            break
        if line.startswith("progress=end"):
            break
    proc.wait()
    produced = sorted(p for p in out_dir.glob("seg*.mp4") if p.is_file())
    return proc.returncode, split_requested, produced


@register_backend
class FfmpegRecorderBackend(Backend):
    op_name = "acquire.livestream"
    name = BACKEND_NAME
    version = BACKEND_VERSION
    requires = BackendRequirements(binaries=["ffmpeg"])

    async def execute(
        self,
        inputs: list[AnyArtifact],
        params: BaseModel,
        ctx: OperationContext,
    ) -> list[AnyArtifact]:
        assert isinstance(params, AcquireLivestreamParams)
        import shutil

        ffmpeg_path = ctx.config.ffmpeg_path
        if shutil.which(ffmpeg_path) is None:
            raise RuntimeError(
                f"ffmpeg binary not found: {ffmpeg_path!r}. "
                "Install via `brew install ffmpeg`."
            )

        run_id = uuid4().hex
        _emit(ctx, run_id, 0.0, "resolving stream")
        stream_url = await asyncio.to_thread(_resolve_stream_url, params.url)

        controller = LiveSegmentController()
        _register_active(controller)
        scratch = ctx.workdir / f"live-{run_id}"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            videos = await asyncio.to_thread(
                self._record_loop,
                params=params,
                stream_url=stream_url,
                ffmpeg_path=ffmpeg_path,
                scratch=scratch,
                controller=controller,
                ctx=ctx,
                run_id=run_id,
            )
        finally:
            _unregister_active(controller)
            shutil.rmtree(scratch, ignore_errors=True)

        if not videos:
            raise RuntimeError(
                f"acquire.livestream produced no segments for {params.url!r} "
                "(stream never yielded data)."
            )
        return videos

    def _record_loop(
        self,
        *,
        params: AcquireLivestreamParams,
        stream_url: str,
        ffmpeg_path: str,
        scratch: Path,
        controller: LiveSegmentController,
        ctx: OperationContext,
        run_id: str,
    ) -> list[AnyArtifact]:
        """Outer loop = recording sessions, separated by manual splits / stop.

        Each session is one ffmpeg process (single output, or many files
        via the segment muxer when ``segment_seconds`` is set). That makes
        the loop trivially terminating on a finite playlist — the segment
        muxer reads the source once.
        """
        videos: list[AnyArtifact] = []
        segment_index = 0
        elapsed = 0.0
        max_dur = params.max_duration_sec
        session = 0
        while True:
            if controller.stopped():
                break
            max_seconds: float | None = None
            if max_dur is not None:
                remaining = max_dur - elapsed
                if remaining <= 0.05:
                    break
                max_seconds = remaining

            sess_dir = scratch / f"s{session:04d}"
            started = time.monotonic()
            _rc, split_requested, produced = _run_ffmpeg_session(
                ffmpeg_path=ffmpeg_path,
                stream_url=stream_url,
                out_dir=sess_dir,
                segment_seconds=(
                    float(params.segment_seconds)
                    if params.segment_seconds is not None
                    else None
                ),
                max_seconds=max_seconds,
                controller=controller,
            )
            elapsed += time.monotonic() - started

            usable = [
                p for p in produced
                if p.exists() and p.stat().st_size >= _MIN_SEGMENT_BYTES
            ]
            for p in usable:
                videos.append(
                    build_segment_video(
                        params=params,
                        backend_name=self.name,
                        backend_version=self.version,
                        segment_index=segment_index,
                        segment_path=p,
                        ctx=ctx,
                        source_url=params.url,
                    )
                )
                _emit(
                    ctx, run_id,
                    (elapsed / max_dur) if max_dur else 0.5,
                    f"segment {segment_index} saved",
                )
                segment_index += 1

            if controller.stopped():
                break
            if max_dur is not None and elapsed >= max_dur - 0.05:
                break
            if split_requested:
                controller.clear_split()
                session += 1
                continue
            # ffmpeg exited on its own — stream ended (or `-t` expired).
            break
        return videos

    def cost_estimate(
        self, inputs: list[AnyArtifact], params: BaseModel
    ) -> CostEstimate:
        assert isinstance(params, AcquireLivestreamParams)
        if params.max_duration_sec is not None:
            return CostEstimate(local_seconds=float(params.max_duration_sec))
        return CostEstimate(local_seconds=60.0)


__all__ = [
    "BACKEND_NAME",
    "BACKEND_VERSION",
    "FfmpegRecorderBackend",
    "LiveSegmentController",
    "request_split_all",
]
