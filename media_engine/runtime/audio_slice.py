"""Audio range pre-slice — shared between mlx-whisper and pyannote backends.

When an audio op (``audio.transcribe``, ``audio.diarize``,
``audio.transcribe_diarized``) is invoked with ``start_s`` / ``end_s``
params set, the engine ffmpeg-slices the input to a temp file in the
per-job workdir and passes that path to the underlying ML library. We
do this instead of asking each library to handle ranges itself because:

* mlx-whisper exposes no time-range parameter.
* pyannote needs the audio loaded into a torch waveform; passing a
  shorter file is simpler than slicing after load.
* Workdir-scoped temp files are GC'd by ``runtime/gc.py`` on job end —
  no new lifecycle to track.
* Stream-copy (``-c copy``) avoids re-encoding; sub-second on any
  reasonable audio file.

Modelled on ``media_engine/ops/video/trim.py`` (same ffmpeg invocation
shape, just minus the video codec flags).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:  # pragma: no cover — typing only
    from media_engine.ops import OperationContext


_SLICE_TIMEOUT_SECONDS = 120


def maybe_slice_audio(
    audio_path: str | Path,
    *,
    start_s: float | None,
    end_s: float | None,
    ctx: OperationContext,
) -> str:
    """Return the (possibly sliced) audio path.

    If both ``start_s`` and ``end_s`` are ``None`` the original path
    is returned unchanged — no ffmpeg invocation. Otherwise a temp
    file is written to ``ctx.workdir`` and its path returned.

    Raises ``RuntimeError`` when ffmpeg is missing from PATH or the
    subprocess exits non-zero; the error surfaces as a Job failure
    via the existing ``_classify_error`` path in ``api/jobs.py``.
    """
    if start_s is None and end_s is None:
        return str(audio_path)

    ffmpeg = ctx.config.ffmpeg_path
    if shutil.which(ffmpeg) is None:
        raise RuntimeError(
            f"ffmpeg binary not found on PATH: {ffmpeg!r}. "
            "Install via `brew install ffmpeg` or set MEDIA_ENGINE_FFMPEG_PATH."
        )

    src = Path(audio_path)
    out = ctx.workdir / f"slice-{uuid4().hex}{src.suffix or '.wav'}"
    cmd: list[str] = [ffmpeg, "-nostdin", "-y"]
    if start_s is not None:
        cmd += ["-ss", f"{start_s}"]
    if end_s is not None:
        cmd += ["-to", f"{end_s}"]
    cmd += ["-i", str(src), "-c", "copy", str(out)]

    try:
        subprocess.run(
            cmd, capture_output=True, check=True, timeout=_SLICE_TIMEOUT_SECONDS
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace").strip()
        raise RuntimeError(
            f"ffmpeg audio slice failed for {src}: {stderr or '(no stderr)'}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"ffmpeg audio slice timed out after {_SLICE_TIMEOUT_SECONDS}s: {src}"
        ) from e

    return str(out)


__all__ = ["maybe_slice_audio"]
