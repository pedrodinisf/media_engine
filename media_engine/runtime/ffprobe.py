"""Thin ffprobe subprocess wrapper.

Two helpers:
  ``probe(path)`` — returns parsed JSON from ``ffprobe -show_format -show_streams``.
  ``classify(probe)`` — picks the appropriate Kind (Video / Audio / Image).

All failures surface as ``FFprobeError`` with an actionable message.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from media_engine.artifacts import Kind

_IMAGE_CODECS = {"mjpeg", "png", "jpeg", "webp", "tiff", "bmp", "gif"}


class FFprobeError(RuntimeError):
    """ffprobe failed (binary missing, parse failure, or unrecognized media)."""


def probe(path: Path, ffprobe_path: str = "ffprobe") -> dict[str, Any]:
    """Return parsed ffprobe JSON. Raises FFprobeError on any failure."""
    if shutil.which(ffprobe_path) is None:
        raise FFprobeError(
            f"ffprobe binary not found: {ffprobe_path!r}. "
            f"Install via `brew install ffmpeg` or set MEDIA_ENGINE_FFPROBE_PATH."
        )
    if not path.exists():
        raise FileNotFoundError(path)

    try:
        proc = subprocess.run(
            [
                ffprobe_path,
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace").strip()
        raise FFprobeError(f"ffprobe failed for {path}: {stderr or 'no stderr'}") from e
    except subprocess.TimeoutExpired as e:
        raise FFprobeError(f"ffprobe timed out for {path}") from e

    try:
        data: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise FFprobeError(f"ffprobe output unparseable for {path}: {e}") from e

    if not data.get("streams"):
        raise FFprobeError(
            f"{path} is not recognized as media (no streams found). "
            f"For documents/HTML, use document.parse / web.fetch (Phase 3+)."
        )
    return data


def classify(probe_data: dict[str, Any]) -> Kind:
    """Classify probe output as Video / Audio / Image."""
    streams = probe_data.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if video_streams:
        v = video_streams[0]
        codec = v.get("codec_name", "")
        nb_frames = v.get("nb_frames")
        is_image = codec in _IMAGE_CODECS or nb_frames in ("1", 1)
        if audio_streams:
            return Kind.Video
        return Kind.Image if is_image else Kind.Video
    if audio_streams:
        return Kind.Audio

    raise FFprobeError(
        "Unrecognized media: streams contain neither video nor audio "
        f"({[s.get('codec_type') for s in streams]})."
    )
